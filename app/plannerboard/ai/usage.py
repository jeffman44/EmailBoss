"""Usage ledger + daily budget cap (DESIGN.md section 6).

Prices are USD per million tokens (input, output). Unknown models cost 0 and
are flagged `estimated=False` so the UI can say "cost unknown" rather than
"free". Local models (Ollama, LM Studio) are listed at 0 on purpose.
"""
from __future__ import annotations

import threading
from datetime import date
from typing import Any

from .base import LLMResult

# model prefix -> (usd per 1M input tokens, usd per 1M output tokens)
# Matching is longest-prefix so "claude-sonnet-4-5-20250929" hits
# "claude-sonnet-4-5". Keep the list conservative; it is an estimate.
PRICE_TABLE: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-haiku": (0.25, 1.25),
    # OpenAI
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5": (1.25, 10.0),
    "o4-mini": (1.10, 4.40),
    "o3": (2.0, 8.0),
    # Groq / Together hosted open models (approximate)
    "llama-3.3-70b": (0.59, 0.79),
    "llama-3.1-8b": (0.05, 0.08),
    "mixtral-8x7b": (0.24, 0.24),
    "deepseek-r1": (0.55, 2.19),
    "deepseek-chat": (0.27, 1.10),
    "qwen2.5-72b": (1.20, 1.20),
    "gemma2-9b": (0.20, 0.20),
}

# Models that are local by construction: cost 0 and *known* to be 0.
LOCAL_MODEL_PREFIXES = ("ollama/", "local/", "lmstudio/")


def lookup_price(model: str) -> tuple[float, float] | None:
    """Longest-prefix match into PRICE_TABLE; None when unknown."""
    m = (model or "").lower().strip()
    if not m:
        return None
    if m.startswith(LOCAL_MODEL_PREFIXES):
        return (0.0, 0.0)
    best = None
    for prefix, price in PRICE_TABLE.items():
        if m.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, price)
    return best[1] if best else None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> tuple[float, bool]:
    """Return (cost_usd, estimated). estimated=False means unknown model -> 0."""
    price = lookup_price(model)
    if price is None:
        return 0.0, False
    cost = (max(0, input_tokens) * price[0] + max(0, output_tokens) * price[1]) / 1_000_000.0
    return round(cost, 6), True


class UsageMeter:
    """Daily budget gate + ledger writer.

    `allow()` is consulted before every model call; `record()` after. The
    budget is re-read from the store once per call so a second process (or a
    restart) cannot double-spend. `budget_hit` is what /api/health reports.
    """

    def __init__(self, store: Any, daily_budget_usd: float = 2.0):
        self.store = store
        self.daily_budget_usd = float(daily_budget_usd or 0.0)
        self._lock = threading.Lock()
        self._budget_hit = False
        self._today = date.today().isoformat()

    # -- state -----------------------------------------------------------
    @property
    def budget_hit(self) -> bool:
        self._roll_day()
        return self._budget_hit

    def _roll_day(self) -> None:
        today = date.today().isoformat()
        if today != self._today:
            self._today = today
            self._budget_hit = False

    def today(self) -> dict:
        try:
            data = self.store.usage_today() or {}
        except Exception:
            data = {}
        return {
            "cost_usd": float(data.get("cost_usd") or 0.0),
            "calls": int(data.get("calls") or 0),
            "by_task": dict(data.get("by_task") or {}),
        }

    # -- gate ------------------------------------------------------------
    def allow(self) -> bool:
        """True when another call may be made today."""
        self._roll_day()
        if self.daily_budget_usd <= 0:
            # 0 or negative budget means "no cap" (local models).
            self._budget_hit = False
            return True
        spent = self.today()["cost_usd"]
        with self._lock:
            self._budget_hit = spent >= self.daily_budget_usd
            return not self._budget_hit

    # -- ledger ----------------------------------------------------------
    def record(self, result: LLMResult, task: str, provider: str = "") -> LLMResult:
        """Fill in cost if the provider left it at 0, write the ledger row."""
        if result is None:
            return result
        if not result.cost_usd and (result.input_tokens or result.output_tokens):
            cost, est = estimate_cost(result.model, result.input_tokens, result.output_tokens)
            result.cost_usd = cost
            result.cost_estimated = est
        try:
            self.store.record_usage(
                task, provider or "", result.model or "",
                int(result.input_tokens or 0), int(result.output_tokens or 0),
                float(result.cost_usd or 0.0), int(result.latency_ms or 0),
                bool(result.ok),
            )
        except Exception:
            # Ledger failure must never fail the feature.
            pass
        if self.daily_budget_usd > 0:
            spent = self.today()["cost_usd"]
            with self._lock:
                self._budget_hit = spent >= self.daily_budget_usd
        return result
