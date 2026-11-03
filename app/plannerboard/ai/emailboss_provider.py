"""Email Boss AI: the built-in, metered provider (docs/AI_CREDITS.md).

The app never holds a vendor key. Each call goes to the license server's
relay (`POST {server_url}/v1/ai/complete`) authenticated with the signed
activation token the license client already stores. The server forwards to
the model vendor, meters tokens and charges the license's credit wallet.

`creds()` is a callable returning the live license material so the provider
can be built before the license client exists and still pick up activation
later: {"lid", "fingerprint", "activation", "server_url", "status"}.
"""
from __future__ import annotations

import json
import socket
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import ERR_NETWORK, ERR_PARSE, ERR_TIMEOUT, LLMResult, map_http_error

PROVIDER_ID = "emailboss"
LABEL = "Email Boss AI (built in)"
TIMEOUT_S = 75
ERR_CREDITS = "credits"       # 402: wallet empty -> UI offers "Buy credits"
ERR_LICENSE = "license"       # 403: activation / license problem
ERR_RELAY_OFF = "relay_off"   # 503: the relay is not configured server-side
LOW_CREDITS = 50

# Tiers the relay understands. The app picks by task; the server maps a tier
# to a vendor model, so a vendor rename never needs an app release.
TIER_FAST = "fast"
TIER_SMART = "smart"
FAST_TASKS = ("triage.classify", "triage.summarize_bullets", "reply.no_reply_classifier",
              "triage.match_rerank", "search.parse", "triage.suggest_project")


def tier_for(task: str) -> str:
    return TIER_FAST if (task or "") in FAST_TASKS else TIER_SMART


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


class EmailBossProvider:
    name = PROVIDER_ID

    def __init__(self, creds: Callable[[], dict], timeout: int = TIMEOUT_S, opener=None):
        self._creds = creds
        self.timeout = timeout
        self._open = opener or urlopen
        self.model = "emailboss/smart"
        self.last_balance: int | None = None
        self.last_error_code: str = ""

    # ---------------------------------------------------------------- helpers
    def creds(self) -> dict:
        try:
            c = self._creds() or {}
        except Exception:
            c = {}
        return {k: (c.get(k) or "") for k in ("lid", "fingerprint", "activation", "server_url", "status")}

    def available(self) -> bool:
        c = self.creds()
        return bool(c["lid"] and c["activation"] and c["server_url"]) and c["status"] in ("ok", "grace", "dev")

    def _post(self, path: str, body: dict, timeout: int | None = None) -> tuple[int, dict]:
        c = self.creds()
        req = Request(f"{c['server_url'].rstrip('/')}{path}", data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json", "User-Agent": "EmailBoss-app"},
                      method="POST")
        try:
            with self._open(req, timeout=timeout or self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except HTTPError as e:
            try:
                data = json.loads(e.read().decode("utf-8", "replace") or "{}")
            except Exception:
                data = {}
            return e.code, data if isinstance(data, dict) else {}

    def _auth(self) -> dict:
        c = self.creds()
        return {"lid": c["lid"], "fingerprint": c["fingerprint"], "activation": c["activation"]}

    # ---------------------------------------------------------------- balance
    def balance(self) -> dict:
        """{balance, packs, ...} from the relay; {} on any failure."""
        if not self.available():
            return {}
        try:
            code, data = self._post("/v1/ai/balance", self._auth(), timeout=15)
        except (URLError, OSError, socket.timeout, TimeoutError):
            return {}
        if code == 200 and isinstance(data, dict):
            self.last_balance = int(data.get("balance") or 0)
            return data
        return {}

    def checkout_url(self, pack: str) -> tuple[str, str]:
        """(url, error) for a credit pack purchase."""
        if not self.available():
            return "", "license not active"
        try:
            code, data = self._post("/v1/ai/checkout", {**self._auth(), "pack": pack}, timeout=20)
        except (URLError, OSError, socket.timeout, TimeoutError) as e:
            return "", f"{ERR_NETWORK}: {getattr(e, 'reason', e)}"
        if code == 200 and data.get("url"):
            return str(data["url"]), ""
        return "", str(data.get("error") or data.get("message") or f"HTTP {code}")

    # ---------------------------------------------------------------- complete
    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 temperature: float = 0.2, json_schema: dict | None = None,
                 model: str | None = None, task: str = "") -> LLMResult:
        tier = TIER_FAST if (model or "").endswith("fast") else (tier_for(task) if task else TIER_SMART)
        label = f"emailboss/{tier}"
        if not self.available():
            self.last_error_code = ERR_LICENSE
            return LLMResult.failure(f"{ERR_LICENSE}: Email Boss AI needs an active license", model=label)
        body = {**self._auth(), "task": task or "", "tier": tier, "system": system or "",
                "user": user, "max_tokens": int(max_tokens), "temperature": float(temperature)}
        if json_schema:
            body["json_schema"] = json_schema
        t0 = time.monotonic()
        try:
            code, data = self._post("/v1/ai/complete", body)
        except (socket.timeout, TimeoutError):
            return LLMResult.failure(f"{ERR_TIMEOUT}: no response in {self.timeout}s", model=label, latency_ms=_ms(t0))
        except (URLError, OSError) as e:
            return LLMResult.failure(f"{ERR_NETWORK}: {getattr(e, 'reason', e)}", model=label, latency_ms=_ms(t0))
        latency = _ms(t0)
        if code == 402:
            self.last_error_code = ERR_CREDITS
            self.last_balance = int(data.get("balance") or 0)
            return LLMResult.failure(f"{ERR_CREDITS}: out of AI credits", model=label, latency_ms=latency)
        if code == 403:
            self.last_error_code = ERR_LICENSE
            return LLMResult.failure(f"{ERR_LICENSE}: {data.get('message') or 'license rejected by the relay'}",
                                     model=label, latency_ms=latency)
        if code == 503:
            self.last_error_code = ERR_RELAY_OFF
            return LLMResult.failure(f"{ERR_RELAY_OFF}: Email Boss AI is not available right now",
                                     model=label, latency_ms=latency)
        if code != 200:
            return LLMResult.failure(map_http_error(code, str(data.get("message") or data.get("error") or "")),
                                     model=label, latency_ms=latency)
        if not isinstance(data, dict) or not data.get("ok", True):
            return LLMResult.failure(f"{ERR_PARSE}: {data.get('error') if isinstance(data, dict) else 'bad body'}",
                                     model=label, latency_ms=latency)
        self.last_error_code = ""
        if data.get("balance") is not None:
            self.last_balance = int(data.get("balance") or 0)
        credits = int(data.get("credits_charged") or 0)
        return LLMResult(text=str(data.get("text") or ""), parsed=data.get("parsed"),
                         input_tokens=int(data.get("input_tokens") or 0),
                         output_tokens=int(data.get("output_tokens") or 0),
                         cost_usd=round(credits / 100.0, 4), latency_ms=int(data.get("latency_ms") or latency),
                         model=f"emailboss/{data.get('model_tier') or tier}", ok=True, cost_estimated=True)
