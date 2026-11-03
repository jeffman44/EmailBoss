"""Build the configured LLMProvider from `cfg["ai"]` + a secret getter."""
from __future__ import annotations

import os
from typing import Any, Callable

from .anthropic_provider import KNOWN_MODELS as ANTHROPIC_MODELS
from .anthropic_provider import DEFAULT_MODEL as ANTHROPIC_DEFAULT
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider, NullProvider
from .emailboss_provider import LABEL as EMAILBOSS_LABEL
from .emailboss_provider import PROVIDER_ID as EMAILBOSS_ID
from .emailboss_provider import EmailBossProvider
from .ollama_provider import KNOWN_MODELS as OLLAMA_MODELS
from .ollama_provider import DEFAULT_MODEL as OLLAMA_DEFAULT
from .ollama_provider import OllamaProvider
from .openai_provider import BASE_URL_PRESETS
from .openai_provider import KNOWN_MODELS as OPENAI_MODELS
from .openai_provider import DEFAULT_MODEL as OPENAI_DEFAULT
from .openai_provider import OpenAIProvider

SECRET_KEY = "ai_api_key"
ENV_KEY = "PB_AI_API_KEY"

# What /api/ai/providers advertises. `required` / `optional` name the
# config fields the setup wizard must collect for each provider.
PROVIDER_CATALOG: list[dict] = [
    {"id": EMAILBOSS_ID, "label": EMAILBOSS_LABEL, "required": [], "optional": [],
     "models": ["emailboss/smart", "emailboss/fast"], "default_model": "emailboss/smart",
     "needs_key": False, "builtin": True,
     "blurb": "Works out of the box. Comes with free credits; buy more inside the app."},
    {"id": "none", "label": "Off (rules only)", "required": [], "optional": [],
     "models": [], "default_model": "", "needs_key": False},
    {"id": "anthropic", "label": "Anthropic (Claude)", "required": ["api_key"],
     "optional": ["model"], "models": ANTHROPIC_MODELS,
     "default_model": ANTHROPIC_DEFAULT, "needs_key": True},
    {"id": "openai", "label": "OpenAI compatible (OpenAI, Groq, Together, OpenRouter, LM Studio)",
     "required": ["api_key"], "optional": ["model", "base_url"],
     "models": OPENAI_MODELS, "default_model": OPENAI_DEFAULT, "needs_key": True,
     "base_url_presets": BASE_URL_PRESETS},
    {"id": "ollama", "label": "Ollama (local)", "required": [], "optional": ["model", "base_url"],
     "models": OLLAMA_MODELS, "default_model": OLLAMA_DEFAULT, "needs_key": False},
]


def _default_secret_getter(key: str) -> str:
    return os.environ.get(ENV_KEY, "") if key == SECRET_KEY else ""


def make_provider(cfg: dict, secret_getter: Callable[[str], Any] | None = None,
                  *, overrides: dict | None = None,
                  license_creds: Callable[[], dict] | None = None) -> LLMProvider:
    """`cfg` is the full plannerboard.json dict (or just its `ai` block).

    `secret_getter(name)` returns the keyring secret; the env var
    PB_AI_API_KEY is the fallback either way. `overrides` lets /api/ai/test
    try a key/model/base_url that is not saved yet.
    """
    ai = dict(cfg.get("ai") or cfg or {})
    if overrides:
        ai.update({k: v for k, v in overrides.items() if v not in (None, "")})
    provider = (ai.get("provider") or "none").strip().lower()
    model = (ai.get("model") or "").strip()
    base_url = (ai.get("base_url") or "").strip()

    api_key = (ai.get("api_key") or "").strip()  # only ever present via overrides
    if not api_key:
        getter = secret_getter or _default_secret_getter
        try:
            api_key = (getter(SECRET_KEY) or "").strip()
        except Exception:
            api_key = ""
        if not api_key:
            api_key = os.environ.get(ENV_KEY, "").strip()

    if provider in ("", "none", "off", "disabled"):
        return NullProvider("AI is turned off")
    if provider in (EMAILBOSS_ID, "builtin", "email_boss"):
        if license_creds is None:
            return NullProvider("Email Boss AI needs an active license")
        return EmailBossProvider(license_creds)
    if provider == "anthropic":
        if not api_key:
            return NullProvider("Anthropic API key not set")
        return AnthropicProvider(api_key, model=model)
    if provider in ("openai", "openai_compatible", "groq", "together", "openrouter", "lmstudio"):
        if provider != "openai" and not base_url:
            base_url = provider
        p = OpenAIProvider(api_key, model=model, base_url=base_url)
        if not p.available():
            return NullProvider("API key not set for OpenAI-compatible provider")
        return p
    if provider == "ollama":
        return OllamaProvider(model=model, base_url=base_url)
    return NullProvider(f"Unknown AI provider '{provider}'")
