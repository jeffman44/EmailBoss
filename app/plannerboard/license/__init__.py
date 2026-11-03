"""Licensing client for Email Boss.

Public surface:
    fingerprint.machine_fingerprint()  stable sha256 machine id
    client.LicenseClient               verify key + activation, heartbeat, offline grace
    client.middleware(app)             Flask before_request: 402 on writes when read-only
"""
from .client import DEFAULT_PUBLIC_KEY_PEM, LicenseClient, LicenseError, middleware  # noqa: F401
from .fingerprint import machine_fingerprint  # noqa: F401
