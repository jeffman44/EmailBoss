"""
Desktop shell for Email Boss: one native window, no browser, no port the
user ever sees.

Ported from the EmailBoss prototype (May 2026: Flask in a background thread,
pywebview renders it in a native OS window with a JS bridge). The engine is
unchanged; it is served on a loopback port (the previous launch's when free, so the
window keeps its localStorage; else a new free one) and the window is pointed
at it. On Windows the window is Edge WebView2, on macOS WKWebView, on Linux
WebKitGTK.

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
    """True once something answers `url` with a non-5xx status. A 401/403 from
    the session-gated /api/* counts: it proves the engine is up (the probe
    carries no cookie, so the gate always refuses it)."""
    import urllib.error
    import urllib.request
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.15)
    print(f"[desktop] engine did not answer at {url}: {last}")
    return False


WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
_WV2_CLIENT = r"SOFTWARE\{arch}Microsoft\EdgeUpdate\Clients\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}"


def webview2_version():
    """Installed WebView2 runtime version string, or None when it is missing.
    Not Windows: returns "n/a" (the check does not apply)."""
    if os.name != "nt":
        return "n/a"
    try:
        import winreg
    except Exception:  # noqa: BLE001
        return "n/a"
    for hive, arch in ((winreg.HKEY_LOCAL_MACHINE, "WOW6432Node\\"), (winreg.HKEY_LOCAL_MACHINE, ""),
                       (winreg.HKEY_CURRENT_USER, "")):
        try:
            with winreg.OpenKey(hive, _WV2_CLIENT.format(arch=arch)) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
            if pv and str(pv) != "0.0.0.0":
                return str(pv)
        except OSError:
            continue
    return None


def show_message(title, text, offer_url=None):
    """Plain native message box (the packaged exe has no console). With
    offer_url the box asks Yes/No to open that page. Falls back to stderr."""
    print(f"[desktop] {title}: {text}")
    if os.name != "nt":
        return False
    try:
        import ctypes
        MB_YESNO, MB_OK, MB_ICONERROR = 0x4, 0x0, 0x10
        flags = MB_ICONERROR | (MB_YESNO if offer_url else MB_OK)
        body = text + (f"\n\nOpen the download page now?\n{offer_url}" if offer_url else "")
        rc = ctypes.windll.user32.MessageBoxW(None, body, title, flags)
        if offer_url and rc == 6:  # IDYES
            webbrowser.open(offer_url)
            return True
    except Exception as e:  # noqa: BLE001
        print(f"[desktop] could not show a message box: {e}")
    return False


def ensure_webview2():
    """False (after telling the user how to fix it) when the Edge WebView2
    runtime is missing. PB_SKIP_WEBVIEW2_CHECK=1 bypasses the registry probe."""
    if os.environ.get("PB_SKIP_WEBVIEW2_CHECK") == "1":
        return True
    if webview2_version() is not None:
        return True
    show_message(
        "Email Boss needs Microsoft Edge WebView2",
        "Email Boss shows its window with the Microsoft Edge WebView2 runtime, and it is not installed on this "
        "computer. It is free and comes from Microsoft: install the Evergreen Runtime, then start Email Boss again.",
        offer_url=WEBVIEW2_URL)
    return False


def preferred_port(data_root):
    """Port used by the last launch (data_root/desktop_port), or 0."""
    try:
        v = int((Path(data_root) / "desktop_port").read_text().strip())
        return v if 1024 <= v <= 65535 else 0
    except Exception:  # noqa: BLE001
        return 0


def remember_port(data_root, port):
    try:
        (Path(data_root) / "desktop_port").write_text(str(int(port)))
    except Exception:  # noqa: BLE001
        pass


def serve_stable(app, host, data_root):
    """Serve on the previous launch's port when it is free, else a new free
    one. A stable origin matters: the board keeps its settings in the window's
    localStorage, which is per origin including the port."""
    srv = None
    port = preferred_port(data_root)
    if port:
        try:
            srv = serve_engine(app, host, port)
        except (OSError, SystemExit):   # werkzeug make_server sys.exit()s on EADDRINUSE
            srv = None
    if srv is None:
        port = free_port(host)
        srv = serve_engine(app, host, port)
    remember_port(data_root, port)
    return srv, port


def close_all_windows():
    """Ask every pywebview window to close; webview.start() then returns."""
    try:
        import webview
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


class DesktopState:
    """Where the running window's engine lives, for a second launch to find."""

    def __init__(self, data_root):
        self.path = Path(data_root) / "desktop.json"

    def write(self, url, pid=None, token=""):
        try:
            self.path.write_text(json.dumps({"url": url, "pid": pid or os.getpid(), "version": __version__,
                                             "token": token}))
            try:
                os.chmod(self.path, 0o600)
            except Exception:  # noqa: BLE001
                pass
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

    def running(self):
        """(url, token) of a live engine from a previous launch, or None."""
        doc = self.read()
        if not doc or not doc.get("url"):
            return None
        if not wait_for_http(doc["url"] + "api/health", timeout=1.5):
            return None
        return doc["url"], str(doc.get("token") or "")

    def running_url(self):
        r = self.running()
        return r[0] if r else None


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


def open_window_safe(url, bridge_ref, bridge):
    """open_window, but a failure to create the native window (WebView2 cannot
    start, profile folder not writable, pythonnet cannot load) is shown in a
    message box and logged instead of vanishing with a windowed exe."""
    try:
        open_window(url, bridge_ref, bridge)
    except Exception as e:  # noqa: BLE001
        show_message("Email Boss could not open its window",
                     f"{type(e).__name__}: {e}\n\nSee logs\\server.log in the Email Boss data folder. "
                     "If Microsoft Edge WebView2 was just installed, restart Windows and try again.",
                     offer_url=WEBVIEW2_URL if os.name == "nt" else None)
        raise


def run(app, rt, cfg, on_exit=None):
    """Serve `app` on a loopback port and show it in a native window.

    Blocks until the window is closed. Returns 0.
    """
    try:
        import webview  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"[desktop] pywebview unavailable ({e}); install plannerboard[desktop]")
        return 2

    if not ensure_webview2():
        return 3

    host = "127.0.0.1"
    state = DesktopState(cfg.paths.data_root)
    srv, port = serve_stable(app, host, cfg.paths.data_root)
    url = f"http://{host}:{port}/"
    if not wait_for_http(url + "api/health"):
        srv.shutdown()
        return 1
    state.write(url, token=getattr(rt, "session_token", ""))
    print(f"[desktop] engine on {url} (window only)")
    if ENGINE_READY_HOOK:
        try:
            ENGINE_READY_HOOK(url)
        except Exception as e:  # noqa: BLE001
            print(f"[desktop] ready hook failed: {e}")

    ref = {}
    bridge = Bridge(ref, url, cfg.paths.data_root)
    # Land on the auth bootstrap so the window holds the per-launch session
    # cookie; nothing else can call /api/* without it.
    token = getattr(rt, "session_token", "") or getattr(app, "pb_session_token", "")
    first = f"{url}auth/{token}" if token else url
    try:
        app.extensions["pb_exit_hook"] = close_all_windows   # the updater's clean exit
    except Exception:  # noqa: BLE001
        pass
    try:
        open_window_safe(first, ref, bridge)
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


def attach_window(url, token: str = ""):
    """Open a window on an engine that is already running (second launch)."""
    if not ensure_webview2():
        return 3
    ref = {}
    first = f"{url}auth/{token}" if token else url
    open_window_safe(first, ref, Bridge(ref, url, ""))
    return 0


def main(argv=None):
    from .start import main as start_main
    return start_main(["--desktop"] + list(argv or sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main() or 0)
