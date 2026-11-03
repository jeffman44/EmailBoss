"""LLM provider contract (DESIGN.md section 6).

Every provider speaks the same two-method protocol so the registry, the usage
meter and the assistant pass never care which vendor is behind the key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class LLMResult:
    text: str = ""
    parsed: Any | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    ok: bool = True
    error: str = ""
    # True when the cost came from a known price-table row; False when the
    # model was unknown and the cost was set to 0 (usage.py).
    cost_estimated: bool = field(default=False, repr=False)

    @classmethod
    def failure(cls, error: str, model: str = "", latency_ms: int = 0) -> "LLMResult":
        return cls(text="", parsed=None, model=model, ok=False, error=error,
                   latency_ms=latency_ms)


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 temperature: float = 0.2, json_schema: dict | None = None,
                 model: str | None = None) -> LLMResult: ...

    def available(self) -> bool: ...


class NullProvider:
    """Provider used when `ai.provider` is `none` or the key is missing.

    `available()` is always False so every caller takes its rules fallback.
    """

    name = "none"

    def __init__(self, reason: str = "AI provider not configured"):
        self.reason = reason

    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 temperature: float = 0.2, json_schema: dict | None = None,
                 model: str | None = None) -> LLMResult:
        return LLMResult.failure(self.reason)

    def available(self) -> bool:
        return False


# Error categories every provider maps HTTP / transport failures onto, so the
# UI can say "bad key" instead of echoing a vendor stack trace.
ERR_AUTH = "auth"
ERR_RATE_LIMIT = "rate_limit"
ERR_BAD_REQUEST = "bad_request"
ERR_NOT_FOUND = "not_found"
ERR_SERVER = "server_error"
ERR_NETWORK = "network"
ERR_TIMEOUT = "timeout"
ERR_PARSE = "parse"


def map_http_error(code: int, body: str = "") -> str:
    """HTTP status -> `<category>: <short vendor message>`."""
    snippet = (body or "").strip().replace("\n", " ")[:200]
    if code in (401, 403):
        cat = ERR_AUTH
    elif code == 429:
        cat = ERR_RATE_LIMIT
    elif code == 404:
        cat = ERR_NOT_FOUND
    elif 400 <= code < 500:
        cat = ERR_BAD_REQUEST
    else:
        cat = ERR_SERVER
    return f"{cat}: HTTP {code} {snippet}".strip()
