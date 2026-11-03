"""Background work (triage cycle, assistant pass, weekly digest) must not write
while the license is read-only. The foreground API is already gated by the
license middleware; this covers the threads."""
from __future__ import annotations

import logging

_PROVIDER = None
_BUILDER = None
_LOGGED = False
_log = logging.getLogger("plannerboard.license_gate")


def set_license_provider(fn, builder=None):
    """fn() -> license client (or None). builder() -> a client built from the
    runtime (store + cfg), tried when fn() gives None. The app wiring installs
    both."""
    global _PROVIDER, _BUILDER, _LOGGED
    _PROVIDER, _BUILDER, _LOGGED = fn, builder, False


def _call(fn):
    try:
        return fn() if callable(fn) else None
    except Exception:
        return None


def _client(ext=None, explicit=None):
    if explicit is not None:
        return explicit
    if ext is not None:
        try:
            c = ext.get("license")
        except Exception:
            c = None
        if c is None:
            c = getattr(getattr(ext, "backing", None), "license", None)
        if c is not None:
            return c
    c = _call(_PROVIDER)
    if c is None:
        c = _call(_BUILDER)
    return c


def is_read_only(ext=None, client=None) -> bool:
    """True when the license client reports read_only. With the app wired (a
    provider installed) and no client resolvable, fails CLOSED: read-only, logged
    once. A bare process with no license wiring at all (unit use) is not blocked.
    A client whose status() raises does not block."""
    global _LOGGED
    c = _client(ext, client)
    if c is None:
        if _PROVIDER is None and _BUILDER is None:
            return False
        if not _LOGGED:
            _LOGGED = True
            _log.warning("license client could not be resolved; background writes are blocked (read-only)")
        return True
    try:
        st = c.status() if callable(getattr(c, "status", None)) else c
        if callable(st):
            st = st()
        return bool(isinstance(st, dict) and st.get("read_only"))
    except Exception:
        return False
