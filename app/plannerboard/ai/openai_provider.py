"""OpenAI-compatible Chat Completions provider.

`base_url` is configurable so the same class serves OpenAI, Groq, Together,
OpenRouter and a local LM Studio server. JSON output uses
`response_format={"type":"json_schema", ...}` when the server supports it;
servers that reject it (HTTP 400 naming response_format) are retried once in
prompt-JSON mode and the reply is parsed with the tolerant parser.
"""
from __future__ import annotations

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import (ERR_NETWORK, ERR_PARSE, ERR_TIMEOUT, LLMResult,
                   map_http_error)
from .guards import parse_json
from .usage import estimate_cost

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
TIMEOUT_S = 60

# Presets the setup wizard offers. Keys are what `ai.base_url` may be set to
# by name; values are the real endpoints.
BASE_URL_PRESETS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "lmstudio": "http://localhost:1234/v1",
}

KNOWN_MODELS = [
    "gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "gpt-5-mini", "gpt-5",
    "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "deepseek-chat",
]

# Servers known to accept response_format json_schema. Anything else starts
# in "object" mode (plain JSON mode) which is far more widely implemented.
_SCHEMA_HOSTS = ("api.openai.com", "api.groq.com", "openrouter.ai", "localhost", "127.0.0.1")


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str = "", base_url: str = "",
                 timeout: int = TIMEOUT_S, json_mode: str | None = None, opener=None):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or DEFAULT_MODEL
        url = (base_url or "").strip()
        url = BASE_URL_PRESETS.get(url.lower(), url) if url else DEFAULT_BASE_URL
        self.base_url = url.rstrip("/")
        self.timeout = timeout
        # json_mode: "schema" | "object" | "prompt"
        if json_mode is None:
            json_mode = "schema" if any(h in self.base_url for h in _SCHEMA_HOSTS) else "object"
        self.json_mode = json_mode
        self._open = opener or urlopen

    @property
    def is_local(self) -> bool:
        return "localhost" in self.base_url or "127.0.0.1" in self.base_url

    def available(self) -> bool:
        # Local servers (LM Studio) usually need no key.
        return bool(self.api_key) or self.is_local

    # -- protocol --------------------------------------------------------
    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 temperature: float = 0.2, json_schema: dict | None = None,
                 model: str | None = None) -> LLMResult:
        model = (model or self.model).strip()
        if not self.available():
            return LLMResult.failure("auth: missing API key", model=model)
        mode = self.json_mode if json_schema else "none"
        result = self._once(system, user, max_tokens, temperature, json_schema, model, mode)
        if (not result.ok and json_schema and mode != "prompt"
                and result.error.startswith("bad_request")
                and "response_format" in result.error.lower()):
            # Server does not implement structured output; degrade once.
            self.json_mode = "prompt"
            result = self._once(system, user, max_tokens, temperature, json_schema, model, "prompt")
        return result

    def _once(self, system, user, max_tokens, temperature, json_schema, model, mode) -> LLMResult:
        user_text = user
        if json_schema and mode == "prompt":
            user_text = (user + "\n\nRespond with ONLY a JSON value matching this schema, "
                         "no prose, no code fences:\n" + json.dumps(json_schema))
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_text})
        body: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }
        if json_schema and mode == "schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": _strictish(json_schema)},
            }
        elif json_schema and mode == "object":
            body["response_format"] = {"type": "json_object"}
            # json_object mode requires the word JSON in the prompt.
            body["messages"][-1]["content"] += (
                "\n\nRespond with ONLY JSON matching: " + json.dumps(json_schema))

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = Request(self.base_url + "/chat/completions",
                      data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")

        t0 = time.monotonic()
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", "replace")
            except Exception:
                err_body = ""
            return LLMResult.failure(map_http_error(e.code, _vendor_message(err_body)),
                                     model=model, latency_ms=_ms(t0))
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
        if isinstance(data, dict) and data.get("error") and not data.get("choices"):
            msg = data["error"].get("message") if isinstance(data["error"], dict) else str(data["error"])
            return LLMResult.failure(f"server_error: {msg}", model=model, latency_ms=latency)

        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        content = msg.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = (content or "").strip()
        if msg.get("refusal"):
            return LLMResult.failure(f"bad_request: refusal: {msg['refusal']}",
                                     model=model, latency_ms=latency)
        parsed = parse_json(text) if json_schema else None

        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        used_model = data.get("model") or model
        if self.is_local:
            used_model = "local/" + used_model
        cost, est = estimate_cost(used_model, in_tok, out_tok)
        return LLMResult(text=text, parsed=parsed, input_tokens=in_tok,
                         output_tokens=out_tok, cost_usd=cost, latency_ms=latency,
                         model=used_model, ok=True, cost_estimated=est)


def _strictish(schema: dict) -> dict:
    """OpenAI structured outputs want additionalProperties:false on objects.
    Apply it recursively without mutating the caller's schema."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
        props = out.get("properties") or {}
        out["properties"] = {k: _strictish(v) for k, v in props.items()}
    if "items" in out and isinstance(out["items"], dict):
        out["items"] = _strictish(out["items"])
    return out


def _vendor_message(err_body: str) -> str:
    try:
        d = json.loads(err_body)
        err = d.get("error")
        if isinstance(err, dict):
            return err.get("message") or err_body
        return str(err) if err else err_body
    except Exception:
        return err_body


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
