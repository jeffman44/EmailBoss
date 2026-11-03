"""
Desktop shell for Email Boss: one native window, no browser, no port the
user ever sees.

Ported from the EmailBoss prototype (May 2026: Flask in a background thread,
pywebview renders it in a native OS window with a JS bridge). The engine is
unchanged; it is served on an ephemeral loopback port chosen at launch and
the window is pointed at it. On Windows the window is Edge WebView2, on
macOS WKWebView, on Linux WebKitGTK.

    python -m plannerboard.start --desktop
    python -m plannerboard.desktop            (same thing)

Second launch while a window is already open: the running instance's URL is
read from <data_root>/desktop.json and a second window is opened on it
instead of starting another engine.

JS bridge (window.pywebview.api):
    pick_folder()          native folder picker -> path or None
    pick_files(multiple)   native file picker   -> [paths]
    save_file(name)        native save dialog   -> path or None
    notify(title, body)    OS toast through the tray icon when it is running
    open_external(url)     open a link in the system browser
    app_info()             version, url, data root
"""

import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__

WINDOW_TITLE = "Email Boss"
ENGINE_READY_HOOK = None  # start.py sets this: called with the url once the engine answers
DEFAULT_SIZE = (1400, 900)
MIN_SIZE = (960, 640)


def free_port(host="127.0.0.1"):
    """An ephemeral port the OS says is free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def wait_for_http(url, timeout=20.0):
    import urllib.request
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.15)
    print(f"[desktop] engine did not answer at {url}: {last}")
    return False


class DesktopState:
    """Where the running window's engine lives, for a second launch to find."""

    def __init__(self, data_root):
        self.path = Path(data_root) / "desktop.json"

    def write(self, url, pid=None):
        try:
            self.path.write_text(json.dumps({"url": url, "pid": pid or os.getpid(), "version": __version__}))
        except Exception as e:  # noqa: BLE001
            print(f"[desktop] could not write {self.path}: {e}")

    def read(self):
        try:
            return json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001
            return None

    def clear(self):
        try:
            self.path.unlink()
        except Exception:  # noqa: BLE001
            pass

    def running_url(self):
        """URL of a live engine from a previous launch, or None."""
        doc = self.read()
        if not doc or not doc.get("url"):
            return None
        return doc["url"] if wait_for_http(doc["url"] + "api/health", timeout=1.5) else None


class Bridge:
    """Callable from the page as window.pywebview.api.<name>(...)."""

    def __init__(self, ref, url, data_root):
        self._ref = ref
        self.url = url
        self.data_root = str(data_root)

    def _win(self):
        return self._ref.get("win")

    def pick_folder(self):
        import webview
        res = self._win().create_file_dialog(webview.FOLDER_DIALOG)
        return res[0] if res else None

    def pick_files(self, multiple=True):
        import webview
        res = self._win().create_file_dialog(webview.OPEN_DIALOG, allow_multiple=bool(multiple))
        return list(res) if res else []

    def save_file(self, name="export.txt"):
        import webview
        res = self._win().create_file_dialog(webview.SAVE_DIALOG, save_filename=name)
        if not res:
            return None
        return res if isinstance(res, str) else res[0]

    def open_external(self, url):
        if isinstance(url, str) and url.lower().startswith(("http://", "https://", "mailto:")):
            webbrowser.open(url)
            return True
        return False

    def notify(self, title, body=""):
        try:
            from plyer import notification  # optional
            notification.notify(title=str(title)[:64], message=str(body)[:256], app_name=WINDOW_TITLE)
            return True
        except Exception:  # noqa: BLE001
            return False

    def app_info(self):
        return {"version": __version__, "url": self.url, "data_root": self.data_root, "desktop": True}


def serve_engine(app, host, port):
    """Run the Flask engine on a daemon thread; returns the werkzeug server."""
    from werkzeug.serving import make_server
    srv = make_server(host, port, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="pb-engine")
    t.start()
    return srv


def open_window(url, bridge_ref, bridge, title=WINDOW_TITLE):
    import webview
    win = webview.create_window(
        title,
        url=url,
        js_api=bridge,
        width=DEFAULT_SIZE[0],
        height=DEFAULT_SIZE[1],
        min_size=MIN_SIZE,
        text_select=True,
        zoomable=True,
        confirm_close=False,
    )
    bridge_ref["win"] = win
    kwargs = {"private_mode": False}  # keep cookies / localStorage between runs
    if os.name == "nt":
        kwargs["gui"] = "edgechromium"
    webview.start(**kwargs)


def run(app, rt, cfg, on_exit=None):
    """Serve `app` on an ephemeral loopback port and show it in a native window.

    Blocks until the window is closed. Returns 0.
    """
    try:
        import webview  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"[desktop] pywebview unavailable ({e}); install plannerboard[desktop]")
        return 2

    host = "127.0.0.1"
    port = free_port(host)
    url = f"http://{host}:{port}/"
    state = DesktopState(cfg.paths.data_root)
    srv = serve_engine(app, host, port)
    if not wait_for_http(url + "api/health"):
        srv.shutdown()
        return 1
    state.write(url)
    print(f"[desktop] engine on {url} (ephemeral; window only)")
    if ENGINE_READY_HOOK:
        try:
            ENGINE_READY_HOOK(url)
        except Exception as e:  # noqa: BLE001
            print(f"[desktop] ready hook failed: {e}")

    ref = {}
    bridge = Bridge(ref, url, cfg.paths.data_root)
    try:
        open_window(url, ref, bridge)
    finally:
        state.clear()
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        if on_exit:
            try:
                on_exit()
            except Exception:  # noqa: BLE001
                pass
    return 0


def attach_window(url):
    """Open a window on an engine that is already running (second launch)."""
    ref = {}
    open_window(url, ref, Bridge(ref, url, ""))
    return 0


def main(argv=None):
    from .start import main as start_main
    return start_main(["--desktop"] + list(argv or sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main() or 0)
