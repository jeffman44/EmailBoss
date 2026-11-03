"""Bring-your-own AI layer: providers, prompt registry, guards, usage meter,
and the assistant pass. See DESIGN.md section 6.

Typical wiring (done by create_app):

    from plannerboard.ai import build_llm
    llm = build_llm(cfg, profession, store, secret_getter)
    app.extensions["pb"]["llm"] = llm
"""
from __future__ import annotations

from .base import LLMProvider, LLMResult, NullProvider
from .factory import PROVIDER_CATALOG, make_provider
from .registry import LLM, build_profile_vars, load_prompts
from .usage import UsageMeter, estimate_cost

__all__ = [
    "LLM", "LLMProvider", "LLMResult", "NullProvider", "PROVIDER_CATALOG",
    "UsageMeter", "build_llm", "build_profile_vars", "estimate_cost",
    "load_prompts", "make_provider",
]


def build_llm(cfg: dict, profession: dict, store, secret_getter=None) -> LLM:
    """Provider from config + usage meter on the store + prompt registry."""
    provider = make_provider(cfg, secret_getter)
    budget = float(((cfg or {}).get("ai") or {}).get("daily_budget_usd") or 0.0)
    meter = UsageMeter(store, budget)
    return LLM(provider, meter, cfg, profession)
