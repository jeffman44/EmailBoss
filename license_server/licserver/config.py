"""Environment-driven settings. Every knob is documented in README.md."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .keys import DEFAULT_PRIVATE_KEY_PATH

PKG_DIR = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("LIC_DB_PATH", str(PKG_DIR.parent / "licserver.db")))
    private_key_path: str = field(default_factory=lambda: os.environ.get("LIC_PRIVATE_KEY_PATH", str(DEFAULT_PRIVATE_KEY_PATH)))
    base_url: str = field(default_factory=lambda: os.environ.get("LIC_BASE_URL", "http://localhost:8600").rstrip("/"))
    product_name: str = field(default_factory=lambda: os.environ.get("LIC_PRODUCT_NAME", "Email Boss"))
    secure_cookies: bool = field(default_factory=lambda: _env_bool("LIC_SECURE_COOKIES", False))
    trial_enabled: bool = field(default_factory=lambda: _env_bool("TRIAL_ENABLED", False))
    trial_days: int = field(default_factory=lambda: int(os.environ.get("TRIAL_DAYS", "14")))
    admin_emails: tuple[str, ...] = field(default_factory=lambda: tuple(
        e.strip().lower() for e in os.environ.get("LIC_ADMIN_EMAILS", "").split(",") if e.strip()))
    stripe_webhook_secret: str = field(default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", ""))
    allow_unsigned_stripe: bool = field(default_factory=lambda: _env_bool("LIC_ALLOW_UNSIGNED_STRIPE", False))
    trusted_proxy: bool = field(default_factory=lambda: _env_bool("LIC_TRUSTED_PROXY", False))
    stripe_plan_map: dict = field(default_factory=lambda: _parse_plan_map(os.environ.get("STRIPE_PLAN_MAP", "")))
    download_urls: dict = field(default_factory=lambda: {
        k[len("DOWNLOAD_URL_"):].lower(): v for k, v in os.environ.items() if k.startswith("DOWNLOAD_URL_") and v
    })
    login_rate_limit: int = field(default_factory=lambda: int(os.environ.get("LIC_LOGIN_RATE_LIMIT", "10")))
    login_rate_window: int = field(default_factory=lambda: int(os.environ.get("LIC_LOGIN_RATE_WINDOW", "300")))
    activation_days: int = field(default_factory=lambda: int(os.environ.get("LIC_ACTIVATION_DAYS", "30")))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    ai_markup: float = field(default_factory=lambda: float(os.environ.get("AI_MARKUP", "4.0")))
    ai_min_credits: int = field(default_factory=lambda: int(os.environ.get("AI_MIN_CREDITS_PER_CALL", "1")))
    ai_trial_credits: int = field(default_factory=lambda: int(os.environ.get("AI_TRIAL_CREDITS", "300")))
    ai_rate_per_min: int = field(default_factory=lambda: int(os.environ.get("AI_RATE_PER_MIN", "60")))
    ai_max_tokens: int = field(default_factory=lambda: int(os.environ.get("AI_MAX_TOKENS", "4000")))
    ai_model_fast: str = field(default_factory=lambda: os.environ.get("AI_MODEL_FAST", "claude-haiku-4-5"))
    ai_model_smart: str = field(default_factory=lambda: os.environ.get("AI_MODEL_SMART", "claude-sonnet-4-5"))
    stripe_credit_packs: dict = field(default_factory=lambda: _parse_credit_packs(os.environ.get("STRIPE_CREDIT_PACKS", "")))
    stripe_secret_key: str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", ""))


def _parse_credit_packs(raw: str) -> dict:
    """STRIPE_CREDIT_PACKS="price_a:500:500,price_b:3000:2500" -> {price_id: {"credits", "usd_cents"}}
    (credits, then price in US cents; cents default to credits since 1 credit = 1 cent)."""
    out: dict = {}
    for part in raw.split(","):
        bits = [b.strip() for b in part.strip().split(":")]
        if len(bits) >= 2 and bits[0] and bits[1].isdigit() and int(bits[1]) > 0:
            cents = int(bits[2]) if len(bits) > 2 and bits[2].isdigit() else int(bits[1])
            out[bits[0]] = {"credits": int(bits[1]), "usd_cents": cents}
    return out


def _parse_plan_map(raw: str) -> dict:
    """STRIPE_PLAN_MAP="price_abc:solo:1,price_def:team:5" -> {price_id: (plan, seats)}"""
    out: dict = {}
    for part in raw.split(","):
        bits = part.strip().split(":")
        if len(bits) >= 2 and bits[0]:
            plan = bits[1].strip() or "solo"
            seats = int(bits[2]) if len(bits) > 2 and bits[2].strip().isdigit() else None
            out[bits[0].strip()] = (plan, seats)
    return out
