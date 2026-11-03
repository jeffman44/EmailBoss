"""Background work (triage cycle, assistant pass, weekly digest) must not write
while the license is read-only. The foreground API is already gated by the
license middleware; this covers the threads."""
from __future__ import annotations

_PROVIDER = None


def set_license_provider(fn):
    """fn() -> license client (or None). The app wiring may install one."""
    global _PROVIDER
    _PROVIDER = fn


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
    return _PROVIDER() if callable(_PROVIDER) else None


def is_read_only(ext=None, client=None) -> bool:
    """True only when a license client exists AND reports read_only. A missing
    client, or a client that errors, never blocks work."""
    c = _client(ext, client)
    if c is None:
        return False
    try:
        st = c.status() if callable(getattr(c, "status", None)) else c
        if callable(st):
            st = st()
        return bool(isinstance(st, dict) and st.get("read_only"))
    except Exception:
        return False
