"""Prompt registry + the `LLM` facade every feature calls.

A prompt is a YAML file in `prompts/`:

    id: triage.classify            # dotted id, unique
    version: 1
    description: one line
    system: |                      # rendered with profile vars
    user_template: |               # rendered with profile vars + call vars
    output_schema: {...} | null    # JSON schema -> structured output; null = plain text
    max_tokens: 200
    temperature: 0.1
    post:                          # validators, run in order; any failure -> None
      - json                       # parse structured output (tolerant)
      - labels                     # result[label_field] must be in the allowed list
      - labels:a|b|c               #   ...explicit list form
      - grounding                  # drop bullets/sentences with identifiers not in vars.source_text
      - max_words:120              # truncate text output
    label_field: classification    # optional, default "classification"
    feature: classify              # optional cfg.ai.features gate

Templates are rendered with `str.format_map` over a dict that returns "" for
unknown keys, so a missing var never raises. Literal braces in a template
(JSON examples) must be doubled.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .base import LLMProvider, LLMResult, NullProvider
from .guards import filter_bullets, ground_bullets, ground_text, parse_json
from .usage import UsageMeter

try:  # frozen builds: resources live under paths.RESOURCE_DIR
    from ..paths import PROMPTS_DIR as PROMPT_DIR
except Exception:  # pragma: no cover - import-order safety net
    PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
if not Path(PROMPT_DIR).is_dir():
    PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# prompt-id prefix -> cfg.ai.features key
_FEATURE_BY_PREFIX = {
    "triage.classify": "classify",
    "triage.summarize_bullets": "summarize",
    "triage.suggest_project": "classify",
    "triage.match_rerank": "match_rerank",
    "reply.": "draft_reply",
    "followup.": "draft_reply",
    "assistant.": "assistant",
}


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


@dataclass
class PromptSpec:
    id: str
    version: int = 1
    description: str = ""
    system: str = ""
    user_template: str = ""
    output_schema: dict | None = None
    max_tokens: int = 400
    temperature: float = 0.2
    post: list[str] = field(default_factory=list)
    label_field: str = "classification"
    feature: str = ""
    path: str = ""

    @property
    def wants_json(self) -> bool:
        return bool(self.output_schema) or "json" in self.post


def load_prompts(prompt_dir: str | os.PathLike | None = None) -> dict[str, PromptSpec]:
    """Load every prompts/*.yaml into {id: PromptSpec}."""
    d = Path(prompt_dir) if prompt_dir else PROMPT_DIR
    out: dict[str, PromptSpec] = {}
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.yaml")):
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict) or not data.get("id"):
            continue
        spec = PromptSpec(
            id=str(data["id"]),
            version=int(data.get("version") or 1),
            description=str(data.get("description") or ""),
            system=str(data.get("system") or ""),
            user_template=str(data.get("user_template") or ""),
            output_schema=data.get("output_schema") or None,
            max_tokens=int(data.get("max_tokens") or 400),
            temperature=float(data.get("temperature") if data.get("temperature") is not None else 0.2),
            post=[str(x) for x in (data.get("post") or [])],
            label_field=str(data.get("label_field") or "classification"),
            feature=str(data.get("feature") or ""),
            path=str(p),
        )
        out[spec.id] = spec
    return out


def build_profile_vars(cfg: dict, profession: dict) -> dict:
    """The template vars every prompt may use (DESIGN.md section 6)."""
    cfg = cfg or {}
    profession = profession or {}
    user = cfg.get("user") or {}
    ent = profession.get("entity") or {}
    pp = profession.get("prompt_profile") or {}
    tri = profession.get("triage") or {}
    vocab = profession.get("vocab") or {}
    classifications = list(tri.get("classifications") or [])
    acronyms = [str(a) for a in (vocab.get("acronyms") or [])]
    keywords = [str(k) for k in (vocab.get("keywords") or [])]
    work_types = [wt.get("label") or wt.get("id") for wt in (profession.get("work_types") or [])]
    stages: list[str] = []
    for wf in ((profession.get("workflows") or {}).get("workflows") or []):
        for st in wf.get("stages") or []:
            if st.get("id") and st["id"] not in stages:
                stages.append(st["id"])
    return {
        "user_name": user.get("name") or "the user",
        "user_email": user.get("email") or "",
        "company": user.get("company") or "the company",
        "role": user.get("role") or pp.get("role_noun") or "professional",
        "industry": pp.get("industry") or "general business",
        "internal_domains": ", ".join(user.get("internal_domains") or []) or "(none)",
        "entity_project": ent.get("project") or "Project",
        "entity_projects": ent.get("projects") or "Projects",
        "entity_client": ent.get("client") or "Client",
        "entity_location": ent.get("location") or "Location",
        "entity_contact": ent.get("contact") or "Contact",
        "entity_quote": ent.get("quote") or "Proposal",
        "classifications": ", ".join(classifications) or "informational",
        "vocab_keywords": ", ".join(keywords) or "(none)",
        "acronyms": ", ".join(acronyms) or "(none)",
        "work_types": ", ".join(str(w) for w in work_types) or "(none)",
        "stages": ", ".join(stages) or "(none)",
        "currency": ((profession.get("money") or {}).get("currency")) or "USD",
        # raw lists for validators (not meant for templates)
        "_classifications": classifications,
        "_acronyms": acronyms,
    }


def render(template: str, vars: dict) -> str:
    return (template or "").format_map(_SafeDict(vars or {}))


def _feature_for(prompt_id: str, spec: PromptSpec) -> str:
    if spec.feature:
        return spec.feature
    for prefix, feat in _FEATURE_BY_PREFIX.items():
        if prompt_id.startswith(prefix):
            return feat
    return ""


class LLM:
    """Facade: render -> budget gate -> provider -> validators -> ledger.

    `run()` returns `(value, LLMResult)` on success or `None` on any failure
    or fallback condition (provider off, budget hit, feature disabled, model
    error, validator rejected). Callers treat None as "use the rules".
    """

    def __init__(self, provider: LLMProvider | None, meter: UsageMeter | None,
                 cfg: dict, profession: dict, registry: dict[str, PromptSpec] | None = None,
                 availability_ttl_s: int = 60):
        self.provider = provider or NullProvider()
        self.meter = meter
        self.cfg = cfg or {}
        self.profession = profession or {}
        self.registry = registry if registry is not None else load_prompts()
        self.profile_vars = build_profile_vars(self.cfg, self.profession)
        self._avail_cache: tuple[float, bool] | None = None
        self._avail_ttl = availability_ttl_s
        self._lock = threading.Lock()
        self.last_error = ""
        self.credits_exhausted = False

    # -- state -----------------------------------------------------------
    @property
    def provider_name(self) -> str:
        return getattr(self.provider, "name", "none")

    @property
    def model(self) -> str:
        return getattr(self.provider, "model", "") or (self.cfg.get("ai") or {}).get("model") or ""

    @property
    def budget_hit(self) -> bool:
        return bool(self.meter and self.meter.budget_hit)

    @property
    def available(self) -> bool:
        """Provider reachable AND budget not hit. Reachability is cached."""
        if self.budget_hit:
            return False
        return self._provider_available()

    def _provider_available(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._avail_cache and now - self._avail_cache[0] < self._avail_ttl:
                return self._avail_cache[1]
        try:
            ok = bool(self.provider.available())
        except Exception:
            ok = False
        with self._lock:
            self._avail_cache = (now, ok)
        return ok

    def invalidate(self) -> None:
        with self._lock:
            self._avail_cache = None

    def feature_enabled(self, feature: str) -> bool:
        if not feature:
            return True
        feats = (self.cfg.get("ai") or {}).get("features") or {}
        return bool(feats.get(feature, True))

    def status(self) -> dict:
        today = self.meter.today() if self.meter else {"cost_usd": 0.0, "calls": 0, "by_task": {}}
        return {
            "provider": self.provider_name,
            "model": self.model,
            "ok": self._provider_available() if self.provider_name != "none" else False,
            "today_cost_usd": round(today["cost_usd"], 4),
            "calls_today": today["calls"],
            "budget_usd": self.meter.daily_budget_usd if self.meter else 0.0,
            "budget_hit": self.budget_hit,
            "last_error": self.last_error,
            "credits_exhausted": getattr(self, "credits_exhausted", False),
            "balance": getattr(self.provider, "last_balance", None),
        }

    # -- the call ----------------------------------------------------------
    def run(self, prompt_id: str, vars: dict | None = None, **overrides) -> tuple[Any, LLMResult] | None:
        spec = self.registry.get(prompt_id)
        if spec is None:
            self.last_error = f"unknown prompt {prompt_id}"
            return None
        if not self.feature_enabled(_feature_for(prompt_id, spec)):
            return None
        if not self._provider_available():
            return None
        if self.meter and not self.meter.allow():
            return None

        merged = dict(self.profile_vars)
        merged.update(vars or {})
        system = render(spec.system, merged)
        user = render(spec.user_template, merged)
        max_tokens = int(overrides.get("max_tokens") or spec.max_tokens)
        temperature = overrides.get("temperature")
        temperature = spec.temperature if temperature is None else float(temperature)
        schema = overrides.get("json_schema", spec.output_schema)
        model = overrides.get("model")

        try:
            kw = dict(max_tokens=max_tokens, temperature=temperature, json_schema=schema, model=model)
            if getattr(self.provider, "name", "") == "emailboss":
                kw["task"] = prompt_id
            result = self.provider.complete(system, user, **kw)
        except Exception as e:  # a provider bug must never take the board down
            result = LLMResult.failure(f"provider exception: {e}")
        if self.meter:
            self.meter.record(result, task=prompt_id, provider=self.provider_name)
        if not result.ok:
            self.last_error = result.error
            self.invalidate() if result.error.startswith(("network", "timeout", "auth")) else None
            if result.error.startswith("credits"):
                self.credits_exhausted = True
            return None

        value: Any = result.parsed if (spec.wants_json and result.parsed is not None) else result.text
        for validator in spec.post:
            value = self._apply(validator, value, spec, merged, result)
            if value is None:
                self.last_error = f"{prompt_id}: validator '{validator}' rejected the output"
                return None
        return value, result

    # -- validators ------------------------------------------------------
    def _apply(self, validator: str, value: Any, spec: PromptSpec, vars: dict,
               result: LLMResult) -> Any:
        name, _, arg = validator.partition(":")
        name = name.strip()
        if name == "json":
            if isinstance(value, (dict, list)):
                parsed = value
            else:
                parsed = parse_json(value if isinstance(value, str) else result.text)
            if parsed is None:
                return None
            if isinstance(spec.output_schema, dict) and isinstance(parsed, dict):
                for key in spec.output_schema.get("required") or []:
                    if key not in parsed:
                        return None
            return parsed
        if name == "labels":
            allowed = [a.strip() for a in arg.split("|") if a.strip()] if arg else list(vars.get("_classifications") or [])
            if not isinstance(value, dict):
                return None
            label = value.get(spec.label_field)
            if allowed and label not in allowed:
                # Try a case-insensitive repair before giving up.
                low = {a.lower(): a for a in allowed}
                if isinstance(label, str) and label.strip().lower() in low:
                    value[spec.label_field] = low[label.strip().lower()]
                else:
                    return None
            return value
        if name == "grounding":
            source = str(vars.get("source_text") or "")
            acr = vars.get("_acronyms") or []
            if isinstance(value, list):
                return filter_bullets([str(b) for b in value], source, acr, limit=max(4, len(value)))
            if isinstance(value, dict):
                if isinstance(value.get("bullets"), list):
                    value["bullets"] = filter_bullets([str(b) for b in value["bullets"]], source, acr)
                for key in ("body", "draft", "text"):
                    if isinstance(value.get(key), str):
                        value[key] = ground_text(value[key], source, acr)
                return value
            if isinstance(value, str):
                return ground_text(value, source, acr)
            return value
        if name == "max_words":
            try:
                n = int(arg)
            except ValueError:
                return value
            if isinstance(value, str):
                words = value.split()
                return " ".join(words[:n]) if len(words) > n else value
            if isinstance(value, dict):
                for key in ("body", "draft", "text", "digest"):
                    if isinstance(value.get(key), str):
                        words = value[key].split()
                        if len(words) > n:
                            value[key] = " ".join(words[:n])
            return value
        # Unknown validator names are ignored rather than fatal.
        return value


# Convenience for tests and tooling.
def prompt_ids(prompt_dir: str | os.PathLike | None = None) -> list[str]:
    return sorted(load_prompts(prompt_dir).keys())


_PLACEHOLDER_RE = re.compile(r'(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})')


def template_placeholders(spec: PromptSpec) -> set[str]:
    """Every {var} referenced by a prompt (for linting the shipped set)."""
    return set(_PLACEHOLDER_RE.findall(spec.system)) | set(_PLACEHOLDER_RE.findall(spec.user_template))
