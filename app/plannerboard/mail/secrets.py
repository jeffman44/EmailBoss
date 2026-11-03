"""Keyring wrapper, service "EmailBoss", with environment fallback.

Ported from v2/imap_capture.py lines 247-311 (lazy keyring import, store /
get / delete with (ok, where, err) results, loud warning on the env path)
and generalized: any key name, env fallback ``PB_<KEY>`` plus the two named
aliases from DESIGN.md section 2 (``PB_IMAP_PASSWORD``, ``PB_AI_API_KEY``).

Secrets are NEVER written to the JSON config. When no keyring backend is
usable, the caller is told so plainly instead of a silent file fallback. For
tests, ``use_memory_backend()`` swaps in a process-local dict.
"""
from __future__ import annotations

import os
import threading

KEYRING_SERVICE = "EmailBoss"

ENV_ALIASES = {
    "imap_password": "PB_IMAP_PASSWORD",
    "ai_api_key": "PB_AI_API_KEY",
}

_lock = threading.RLock()
_memory: dict[str, str] | None = None
_warnings: list[str] = []


def _keyring():
    try:
        import keyring  # optional dependency
        return keyring
    except Exception:
        return None


def use_memory_backend(enabled: bool = True) -> None:
    """Process-local store (tests, or machines with no keyring at all)."""
    global _memory
    with _lock:
        _memory = {} if enabled else None


def warnings() -> list[str]:
    with _lock:
        return list(_warnings)


def _warn(text: str) -> None:
    with _lock:
        if text not in _warnings:
            _warnings.append(text)


def env_name(key: str) -> str:
    return ENV_ALIASES.get(key) or "PB_" + key.upper()


def set_secret(key: str, value: str, account: str | None = None) -> tuple[bool, str | None, str | None]:
    """Returns (ok, where, err). ``where`` is 'keyring' or 'memory'."""
    if not key or value is None:
        return False, None, "key and value are required"
    name = f"{key}:{account}" if account else key
    with _lock:
        if _memory is not None:
            _memory[name] = value
            return True, "memory", None
    kr = _keyring()
    if kr is None:
        return False, None, (f"the 'keyring' package is not installed; set the "
                             f"{env_name(key)} environment variable instead")
    try:
        kr.set_password(KEYRING_SERVICE, name, value)
        return True, "keyring", None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def get_secret(key: str, account: str | None = None) -> tuple[str | None, str | None]:
    """Returns (value_or_None, source). source is 'keyring', 'memory', 'env' or None."""
    name = f"{key}:{account}" if account else key
    with _lock:
        if _memory is not None:
            v = _memory.get(name)
            if v:
                return v, "memory"
    kr = _keyring()
    if kr is not None:
        try:
            v = kr.get_password(KEYRING_SERVICE, name)
            if v:
                return v, "keyring"
        except Exception as e:
            _warn(f"keyring unreadable ({type(e).__name__}); falling back to {env_name(key)}")
    v = os.environ.get(env_name(key))
    if v:
        _warn(f"{key} taken from the {env_name(key)} environment variable; it is "
              "not in the OS credential store.")
        return v, "env"
    return None, None


def delete_secret(key: str, account: str | None = None) -> bool:
    name = f"{key}:{account}" if account else key
    with _lock:
        if _memory is not None:
            return _memory.pop(name, None) is not None
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(KEYRING_SERVICE, name)
        return True
    except Exception:
        return False


def backend_status() -> dict:
    with _lock:
        mem = _memory is not None
    return {
        "service": KEYRING_SERVICE,
        "backend": "memory" if mem else ("keyring" if _keyring() else "none"),
        "warnings": warnings(),
    }
