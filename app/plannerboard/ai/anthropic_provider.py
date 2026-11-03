"""Anthropic Messages API over raw HTTPS (urllib, no SDK).

JSON output is obtained by forcing a single tool whose `input_schema` is the
caller's json_schema; the model's tool input IS the parsed object, so no
fence-stripping or brace-hunting is needed on this path.
"""
from __future__ import annotations

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import (ERR_NETWORK, ERR_PARSE, ERR_TIMEOUT, LLMResult,
                   map_http_error)
from .usage import estimate_cost

DEFAULT_MODEL = "claude-sonnet-4-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
TIMEOUT_S = 60
JSON_TOOL_NAME = "emit_result"

KNOWN_MODELS = [
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-opus-4-1",
    "claude-sonnet-4",
    "claude-3-7-sonnet-latest",
    "claude-3-5-haiku-latest",
]


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "", base_url: str = "",
                 timeout: int = TIMEOUT_S, opener=None):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or DEFAULT_MODEL
        self.url = (base_url or "").rstrip("/") + "/v1/messages" if base_url else API_URL
        self.timeout = timeout
        # `opener` is injectable for tests; defaults to urllib.request.urlopen.
        self._open = opener or urlopen

    # -- protocol --------------------------------------------------------
    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 temperature: float = 0.2, json_schema: dict | None = None,
                 model: str | None = None) -> LLMResult:
        model = (model or self.model).strip()
        if not self.api_key:
            return LLMResult.failure("auth: missing API key", model=model)

        body: dict = {
            "model": model,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            body["system"] = system
        wrapped = False
        if json_schema:
            tool_schema, wrapped = _ensure_object_schema(json_schema)
            body["tools"] = [{
                "name": JSON_TOOL_NAME,
                "description": "Return the result as structured data matching the schema.",
                "input_schema": tool_schema,
            }]
            body["tool_choice"] = {"type": "tool", "name": JSON_TOOL_NAME}

        req = Request(self.url, data=json.dumps(body).encode("utf-8"), headers={
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
        }, method="POST")

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

        text_parts: list[str] = []
        parsed = None
        for block in data.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use" and block.get("name") == JSON_TOOL_NAME:
                parsed = block.get("input")
                if wrapped and isinstance(parsed, dict) and "result" in parsed:
                    parsed = parsed["result"]
        text = "\n".join(p for p in text_parts if p).strip()
        if parsed is not None and not text:
            text = json.dumps(parsed)
        # If the model answered in prose despite the forced tool, `parsed` stays
        # None and the registry runs its tolerant parser on `text`.

        usage = data.get("usage") or {}
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        cost, est = estimate_cost(data.get("model") or model, in_tok, out_tok)
        return LLMResult(text=text, parsed=parsed, input_tokens=in_tok,
                         output_tokens=out_tok, cost_usd=cost, latency_ms=latency,
                         model=data.get("model") or model, ok=True,
                         cost_estimated=est)


def _ensure_object_schema(schema: dict) -> tuple[dict, bool]:
    """Anthropic tool input must be an object schema. Wrap arrays/scalars.

    Returns (schema, wrapped). When wrapped, the model's input is
    {"result": <value>} and the caller unwraps it.
    """
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema, False
    return {"type": "object", "properties": {"result": schema},
            "required": ["result"]}, True


def _vendor_message(err_body: str) -> str:
    try:
        d = json.loads(err_body)
        return (d.get("error") or {}).get("message") or err_body
    except Exception:
        return err_body


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
