"""Local Ollama provider.

Port of `_call` / `is_available` from the reference install's ai.py
(v2/ai.py lines 43-56 and 279-306, 349-362): /api/generate with
stream:false, `think:false` for reasoning model families, and a one-shot
fallback model when the configured model is not pulled (HTTP 404).
JSON output uses Ollama's structured `format` field with the schema.
"""
from __future__ import annotations

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import ERR_NETWORK, ERR_PARSE, ERR_TIMEOUT, LLMResult, map_http_error
from .guards import parse_json

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:4b"
FALLBACK_MODEL = "llama3.2:3b"
# Families that accept "think": false (ai.py:55). Sending it to a
# non-thinking model is a 400, so it is gated by prefix.
THINKING_PREFIXES = ("qwen3", "deepseek-r1", "gpt-oss")
TIMEOUT_S = 60

KNOWN_MODELS = ["qwen3.5:4b", "qwen2.5:7b", "llama3.2:3b", "llama3.1:8b",
                "mistral:7b", "gemma3:4b", "phi4-mini"]


class OllamaProvider:
    name = "ollama"

    def __init__(self, model: str = "", base_url: str = "", timeout: int = TIMEOUT_S,
                 fallback_model: str = FALLBACK_MODEL, opener=None):
        self.model = (model or "").strip() or DEFAULT_MODEL
        self.host = ((base_url or "").strip() or DEFAULT_HOST).rstrip("/")
        self.timeout = timeout
        self.fallback_model = fallback_model
        self._open = opener or urlopen
        self._warned_missing: set[str] = set()

    # -- protocol --------------------------------------------------------
    def available(self) -> bool:
        """Is the Ollama daemon answering? (ai.py:349-362)"""
        try:
            req = Request(self.host + "/api/tags")
            with self._open(req, timeout=3) as resp:
                return getattr(resp, "status", 200) == 200
        except (HTTPError, URLError, OSError, socket.timeout, TimeoutError):
            return False

    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 temperature: float = 0.2, json_schema: dict | None = None,
                 model: str | None = None) -> LLMResult:
        model = (model or self.model).strip()
        return self._call(system, user, max_tokens, temperature, json_schema, model,
                          allow_fallback=True)

    def _call(self, system, user, max_tokens, temperature, json_schema, model,
              allow_fallback: bool) -> LLMResult:
        # /api/generate takes a single prompt; the system text goes in `system`.
        body: dict = {
            "model": model,
            "prompt": user,
            "stream": False,
            "options": {"temperature": float(temperature), "num_predict": int(max_tokens)},
        }
        if system:
            body["system"] = system
        if model.startswith(THINKING_PREFIXES):
            body["think"] = False
        if json_schema:
            body["format"] = json_schema

        req = Request(self.host + "/api/generate", data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.monotonic()
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as e:
            # Model not pulled yet: run once on the fallback rather than
            # blanking the feature, and remember to say so (ai.py:297-304).
            if e.code == 404 and allow_fallback and model != self.fallback_model:
                if model not in self._warned_missing:
                    self._warned_missing.add(model)
                    print(f"[ai] ollama model {model} not found; using {self.fallback_model}. "
                          f"Run: ollama pull {model}")
                return self._call(system, user, max_tokens, temperature, json_schema,
                                  self.fallback_model, allow_fallback=False)
            try:
                err_body = e.read().decode("utf-8", "replace")
            except Exception:
                err_body = ""
            return LLMResult.failure(map_http_error(e.code, err_body), model=model,
                                     latency_ms=_ms(t0))
        except (socket.timeout, TimeoutError):
            return LLMResult.failure(f"{ERR_TIMEOUT}: no response in {self.timeout}s",
                                     model=model, latency_ms=_ms(t0))
        except (URLError, OSError) as e:
            return LLMResult.failure(f"{ERR_NETWORK}: {getattr(e, 'reason', e)}",
                                     model=model, latency_ms=_ms(t0))
        latency = _ms(t0)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return LLMResult.failure(f"{ERR_PARSE}: non-JSON response body",
                                     model=model, latency_ms=latency)
        text = (data.get("response") or "").strip()
        parsed = parse_json(text) if json_schema else None
        in_tok = int(data.get("prompt_eval_count") or 0)
        out_tok = int(data.get("eval_count") or 0)
        return LLMResult(text=text, parsed=parsed, input_tokens=in_tok, output_tokens=out_tok,
                         cost_usd=0.0, latency_ms=latency, model="ollama/" + model, ok=True,
                         cost_estimated=True)


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
