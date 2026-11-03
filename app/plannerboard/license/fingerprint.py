"""Stable machine fingerprint.

Inputs (all hashed, never sent raw):
    hostname, primary MAC (uuid.getnode), platform.system/machine,
    and the OS machine id when readable:
        Linux   /etc/machine-id or /var/lib/dbus/machine-id
        macOS   IOPlatformUUID via ioreg
        Windows HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid

Output is sha256 hex. No usernames, no paths, no serials beyond the above.
Pure stdlib.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import socket
import subprocess
import uuid

_CACHE: dict[str, str] = {}


def _os_machine_id() -> str:
    system = platform.system()
    try:
        if system == "Linux":
            for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        val = fh.read().strip()
                    if val:
                        return val
        elif system == "Darwin":
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            if m:
                return m.group(1)
        elif system == "Windows":
            try:
                import winreg  # type: ignore[import-not-found]

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
                ) as k:
                    val, _ = winreg.QueryValueEx(k, "MachineGuid")
                    return str(val)
            except OSError:
                pass
    except Exception:  # pragma: no cover - best effort only
        pass
    return ""


def _mac() -> str:
    node = uuid.getnode()
    # uuid.getnode falls back to a random 48-bit number with the multicast
    # bit set when no hardware address is found; that would not be stable.
    if (node >> 40) & 1:
        return ""
    return f"{node:012x}"


def fingerprint_components() -> dict[str, str]:
    """Return the raw components (for diagnostics only; never transmitted)."""
    return {
        "hostname": socket.gethostname() or "",
        "mac": _mac(),
        "platform": f"{platform.system()}|{platform.machine()}",
        "machine_id": _os_machine_id(),
    }


def machine_fingerprint(force: bool = False) -> str:
    """sha256 hex of the joined components. Cached for the process lifetime."""
    if not force and "fp" in _CACHE:
        return _CACHE["fp"]
    comps = fingerprint_components()
    material = "\n".join(f"{k}={comps[k]}" for k in sorted(comps))
    fp = hashlib.sha256(material.encode("utf-8")).hexdigest()
    _CACHE["fp"] = fp
    return fp


if __name__ == "__main__":  # pragma: no cover
    print(machine_fingerprint())
