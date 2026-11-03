"""
System-tray status icon for Email Boss. Port of the reference v2/tray.py
with the install-specific alarm fields replaced by the generic health shape.

    python -m plannerboard.tray --port 8080

Dot colour, polled from /api/health every 30 s:
    green   status ok, every watcher alive, nothing needing a restart
    amber   reachable but degraded (a watcher down, restart_needed, db not writable)
    red     unreachable
    grey    not answered yet

Quit closes the ICON, not the board. "Restart server" posts /api/restart.
Everything here is best-effort; no failure may affect the server process.
"""

import argparse
import os
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

POLL_SECONDS = 30
COLORS = {"green": (46, 160, 67), "amber": (210, 153, 34), "red": (218, 54, 51), "grey": (120, 120, 120)}


def fetch_health(host, port, token=None, timeout=5):
    url = f"http://{host}:{port}/api/health"
    token = token or os.environ.get("PB_SESSION_TOKEN") or os.environ.get("PB_API_TOKEN")
    try:
        req = urllib.request.Request(url, headers={"X-Requested-With": "EmailBoss"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore")), None
    except urllib.error.URLError as e:
        return None, str(getattr(e, "reason", e))
    except Exception as e:
        return None, str(e)


def classify(health, err):
    if health is None:
        return "red", f"Email Boss: UNREACHABLE ({err or 'no response'})"
    problems = []
    if str(health.get("status", "ok")).lower() != "ok":
        problems.append(f"status={health.get('status')}")
    if health.get("db_write_ok") is False:
        problems.append("database not writable")
    down = [w.get("name") for w in (health.get("watchers") or []) if isinstance(w, dict) and w.get("down")]
    if down:
        problems.append("down: " + ", ".join(str(d) for d in down[:4]))
    if health.get("restart_needed"):
        problems.append(f"restart_needed: {len(health['restart_needed'])} module(s)")
    lic = health.get("license") or {}
    if lic.get("status") in ("expired", "read_only", "invalid"):
        problems.append(f"license={lic.get('status')}")
    if problems:
        return "amber", "Email Boss: DEGRADED\n" + "\n".join(problems[:6])
    n = health.get("project_count")
    return "green", f"Email Boss: OK" + (f" ({n} projects)" if n else "")


def make_icon_image(color_name, size=64):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 8
    d.ellipse([pad, pad, size - pad, size - pad], fill=COLORS.get(color_name, COLORS["grey"]) + (255,),
              outline=(30, 30, 30, 255), width=max(2, size // 24))
    return img


def restart_server(host, port, token=None):
    def go():
        try:
            req = urllib.request.Request(f"http://{host}:{port}/api/restart", data=b"{}", method="POST")
            req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Email Boss tray status icon")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--token", default=None)
    ap.add_argument("--poll", type=int, default=POLL_SECONDS)
    a = ap.parse_args(argv)
    a.token = a.token or os.environ.get("PB_SESSION_TOKEN") or None
    try:
        import pystray
        from PIL import Image  # noqa: F401
    except Exception as e:
        print(f"tray: pystray/Pillow unavailable ({e}); nothing to do. pip install plannerboard[tray]")
        return 0
    state = {"color": "grey", "tip": "Email Boss: starting"}
    url = f"http://{a.host}:{a.port}/" + (f"auth/{a.token}" if a.token else "")
    icon = pystray.Icon("plannerboard", make_icon_image("grey"), "Email Boss", menu=pystray.Menu(
        pystray.MenuItem(lambda i: state["tip"].splitlines()[0], lambda i, it: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open board", lambda i, it: webbrowser.open(url), default=True),
        pystray.MenuItem("Restart server", lambda i, it: restart_server(a.host, a.port, a.token)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit (icon only)", lambda i, it: i.stop())))

    def poll():
        while True:
            try:
                color, tip = classify(*fetch_health(a.host, a.port, a.token))
                if color != state["color"]:
                    icon.icon = make_icon_image(color)
                state["color"], state["tip"] = color, tip
                icon.title = tip[:127]
            except Exception:
                pass
            time.sleep(max(5, a.poll))

    threading.Thread(target=poll, daemon=True, name="tray-poll").start()
    icon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
