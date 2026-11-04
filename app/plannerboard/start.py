"""
Email Boss entry point: lock, migrate, background threads, serve.

Ported from the reference v2/start.py (logging shim 55-112, port probe
116-128, instance lock 228-252, main 255-399) and server.py 11534-11640
(daily backup loop). Generic: the data root comes from config, the bind
address defaults to loopback, and every optional layer (mail runner, triage
pipeline, assistant) is imported if present and logged if not.

Usage:
    python -m plannerboard.start                 # config from $PB_CONFIG / data root
    python -m plannerboard.start --port 8081
    python -m plannerboard.start --config /path/plannerboard.json
    python -m plannerboard.start --no-tray
"""

import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import Config

BACKUP_RETENTION_DAYS = 30


# ── logging (start.py:55-112) ─────────────────────────────────────────────────

class _StreamToLog:
    """File-like shim so print() lands in the rotating log. Never raises."""

    def __init__(self, logger, level, console=None):
        self._logger, self._level, self._console = logger, level, console
        self._buf = ""

    def write(self, buf):
        try:
            if self._console:
                self._console.write(buf)
            self._buf += buf
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._logger.log(self._level, line.rstrip())
        except Exception:
            pass

    def flush(self):
        try:
            if self._console:
                self._console.flush()
            if self._buf.strip():
                self._logger.log(self._level, self._buf.rstrip())
            self._buf = ""
        except Exception:
            pass

    def isatty(self):
        return False


def setup_logging(log_file, max_bytes=5_000_000, backups=5, capture_std=True):
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(getattr(h, "baseFilename", None) == str(log_file.resolve()) for h in root.handlers):
        root.addHandler(handler)
    if capture_std:
        out = sys.stdout if sys.stdout is not None and sys.stdout.isatty() else None
        err = sys.stderr if sys.stderr is not None and sys.stderr.isatty() else None
        sys.stdout = _StreamToLog(logging.getLogger("board"), logging.INFO, out)
        sys.stderr = _StreamToLog(logging.getLogger("board"), logging.ERROR, err)
    return log_file


# ── single instance (start.py:116-128, 228-252) ───────────────────────────────

def port_taken(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(1.0)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        try:
            s.close()
        except Exception:
            pass


def acquire_instance_lock(path):
    """Exclusive OS-level lock held for the life of the process. Returns the
    open file (keep the reference) or None when another process holds it."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except Exception:
            pass
        return fh
    except Exception as e:
        print(f"[lock] could not use the lock file ({e}); continuing without the single-instance guard")
        return object()


# ── background loops ────────────────────────────────────────────────────────────

def backup_database(rt):
    """Snapshot the database into paths.backups and prune files older than
    BACKUP_RETENTION_DAYS. Uses the sqlite backup API (WAL-consistent)."""
    import sqlite3
    dest_dir = Path(rt.paths.backups)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_dir / f"{rt.paths.db_file.stem}_{stamp}.db"
    src = sqlite3.connect(str(rt.paths.db_file))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    pruned = 0
    for f in dest_dir.glob(f"{rt.paths.db_file.stem}_*.db"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                pruned += 1
        except OSError:
            pass
    rt.backup_state.update({"last_run": rt.hours.now_iso(), "last_error": None, "last_file": str(dest),
                            "pruned": pruned})
    rt.heartbeat("db_backup")
    return dest


def purge_mail_store(rt):
    """Daily: apply the mail body retention window (mail.store.body_days) to
    the local store, the processed mail cache and old backups together."""
    try:
        from .mail import lifecycle
        from .mail.runner import mail_cache_dir
        from .mail.store import open_store
        cfg = rt.config.doc
        cache = mail_cache_dir(cfg)
        st = open_store(cache) if (cache / "mail_store.sqlite").exists() else None
        if st is None:
            return None
        out = lifecycle.purge(st, cfg, cache, rt.paths.backups)
        rt.heartbeat("mail_purge", note=f"{out.get('bodies_dropped', 0)} bodies past the window")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[purge] failed: {type(e).__name__}: {e}")
        return None


def start_backup_thread(rt):
    rt.register_watcher("db_backup", 24 * 60, note=f"database -> {rt.paths.backups}")

    def loop():
        last_day = None
        while True:
            today = datetime.now().date()
            if today != last_day:
                try:
                    backup_database(rt)
                    last_day = today
                except Exception as e:
                    rt.backup_state["last_error"] = f"{type(e).__name__}: {e}"
                    print(f"[backup] failed: {e}")
                purge_mail_store(rt)
            time.sleep(600)

    threading.Thread(target=loop, daemon=True, name="db-backup").start()


def install_triage_pipeline(rt):
    """The mail layer (mail.runner.wire_runtime, called by create_app) installs
    Runtime.run_triage_cycle. When it did not, bind triage.pipeline.run_cycle
    directly so the loop still works with a bare drop folder."""
    if getattr(rt, "run_triage_cycle", None) is not None:
        return True
    try:
        from .triage import pipeline  # type: ignore
    except Exception as e:
        print(f"[triage] pipeline not installed ({type(e).__name__}); /api/triage/run answers 501")
        return False
    runner = getattr(pipeline, "run_cycle", None)
    if runner is None:
        print("[triage] pipeline module has no run_cycle")
        return False

    def run(**kw):
        return runner(rt.store, rt.config.doc, rt.profession(), llm=getattr(rt, "llm", None),
                      mail_cache=rt.paths.mail_cache, **kw)

    rt.run_triage_cycle = run
    return True


def start_triage_thread(rt):
    """Periodic triage over the drop folder. When a capture thread is running
    for a provider it already triages after every fetch; this loop still runs
    for the pa_folder / none case and as a safety net."""
    interval = max(60, int(rt.config.get("mail.poll_seconds", 300) or 300))
    rt.register_watcher("triage_loop", interval // 60, note="mail cache -> triage cards")
    if rt.run_triage_cycle is None:
        return

    def loop():
        time.sleep(10)
        while True:
            try:
                rt.run_triage_cycle()
                rt.triage_state["last_run"] = rt.hours.now_iso()
                rt.triage_state["last_error"] = None
                rt.triage_state["run_count"] += 1
                rt.heartbeat("triage_loop")
            except Exception as e:
                rt.triage_state["last_error"] = f"{type(e).__name__}: {e}"
                rt.triage_state["error_count"] += 1
                print(f"[triage] cycle failed: {e}")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="triage-loop").start()


def start_license_heartbeat(rt, app):
    """Daily heartbeat against the license server (offline failures are
    absorbed by the grace window). Skipped in dev mode."""
    try:
        from .license import client as lic
    except Exception:
        return False
    if lic.LicenseClient.dev_mode():
        print("[license] PB_LICENSE_DEV=1: developer mode, heartbeat skipped")
        return False
    rt.register_watcher("license_heartbeat", 24 * 60, note="daily activation refresh")

    def loop():
        time.sleep(30)
        while True:
            try:
                res = lic.client_from_app(app).heartbeat()
                rt.heartbeat("license_heartbeat", note=str(res.get("skipped") or res.get("error") or "ok"))
            except Exception as e:
                print(f"[license] heartbeat failed: {e}")
            time.sleep(6 * 3600)

    threading.Thread(target=loop, daemon=True, name="license-heartbeat").start()
    return True


def start_update_check(rt, app):
    """Daily signed-manifest check (updates.enabled). Never installs anything."""
    try:
        from . import updater
        return updater.start_update_thread(rt, app)
    except Exception as e:
        print(f"[update] not started: {type(e).__name__}: {e}")
        return False


def mail_thread_summary(rt):
    runner = getattr(rt, "mail_runner", None)
    if runner is None or not getattr(runner, "threads", None):
        return f"provider={rt.config.mail_provider}: no capture thread"
    return "capture threads: " + ", ".join(runner.threads)


def open_browser_later(url, delay=1.5):
    def go():
        time.sleep(delay)
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            print(f"[browser] could not open {url}: {e}")
    threading.Thread(target=go, daemon=True, name="open-browser").start()


def start_tray(port, bind, token=""):
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except Exception as e:
        print(f"Tray icon: not started (pystray/Pillow unavailable: {e})")
        return None
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ)
        if token:
            env["PB_SESSION_TOKEN"] = token  # the tray polls /api/health with it
        if getattr(sys, "frozen", False):
            # PyInstaller bundle: the executable IS this module; re-enter it in tray mode.
            cmd = [sys.executable, "--tray-only", "--port", str(port), "--bind", bind]
        else:
            cmd = [sys.executable, "-m", "plannerboard.tray", "--port", str(port), "--host", bind]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, **kwargs)
        print(f"Tray icon: started (pid {proc.pid})")
        return proc
    except Exception as e:
        print(f"Tray icon: failed to start ({e})")
        return None


_INSTANCE_LOCK = None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="plannerboard", description="Email Boss")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--bind", default=None)
    parser.add_argument("--config", default=None, help="path to plannerboard.json")
    parser.add_argument("--data-root", default=None, help="data folder (overrides PB_DATA_ROOT)")
    parser.add_argument("--dev-license", action="store_true", help=argparse.SUPPRESS)   # dev channel only
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser on first run")
    parser.add_argument("--no-threads", action="store_true", help="serve only (no loops)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--version", action="version", version=f"Email Boss {__version__}")
    parser.add_argument("--tray-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--desktop", action="store_true",
                        help="native window on a loopback port (no browser)")
    args = parser.parse_args(argv)
    if getattr(sys, "frozen", False) and not args.tray_only and os.environ.get("PB_SERVER_MODE") != "1":
        args.desktop = True  # the packaged app IS the desktop app; PB_SERVER_MODE=1 for headless

    if args.tray_only:
        from . import tray
        return tray.main(["--port", str(args.port or 8080), "--host", args.bind or "127.0.0.1"]) or 0

    if args.data_root:
        os.environ["PB_DATA_ROOT"] = str(Path(args.data_root).expanduser())
    if args.dev_license:
        os.environ["PB_LICENSE_DEV"] = "1"

    cfg = Config.load(args.config)
    cfg.paths.ensure()
    port = args.port or cfg.port
    bind = args.bind or cfg.bind
    setup_logging(cfg.paths.logs / "server.log", capture_std=not args.debug)
    print("=" * 60)
    print(f"Email Boss {__version__} starting: pid {os.getpid()}")
    print(f"Config: {cfg.source_path or '(none: setup required)'}  profession={cfg.profession_template}")
    if cfg.load_error:
        print(f"Config LOAD ERROR: {cfg.load_error}")
    print(f"Data root: {cfg.paths.data_root}")

    restart_wait = bool(os.environ.pop("PB_RESTART_WAIT", None))
    if args.desktop:
        from . import desktop
        state = desktop.DesktopState(cfg.paths.data_root)
        if restart_wait:
            # /api/restart started us: the old process is still dying and its
            # desktop.json still answers. Wait for it, do not attach to it.
            for _ in range(30):
                if not state.running():
                    break
                time.sleep(0.5)
        running = state.running()
        if running:
            print(f"[desktop] engine already running at {running[0]}; opening a window on it")
            return desktop.attach_window(running[0], running[1])
        bind = "127.0.0.1"
        port = 0  # chosen by desktop.run()

    probe_host = "127.0.0.1" if bind in ("0.0.0.0", "") else bind
    if restart_wait:
        # Restarted by /api/restart on a platform without execv: the old
        # process is still letting go of the port.
        for _ in range(30):
            if not port_taken(port, probe_host):
                break
            time.sleep(0.5)
    if port and port_taken(port, probe_host):
        print(f"Port {port} is already serving; another instance owns it. Exiting cleanly.")
        return 0
    global _INSTANCE_LOCK
    _INSTANCE_LOCK = acquire_instance_lock(cfg.paths.lock_file)
    for _ in range(30 if restart_wait else 0):
        if _INSTANCE_LOCK is not None:
            break
        time.sleep(0.5)     # the old process still holds the lock file
        _INSTANCE_LOCK = acquire_instance_lock(cfg.paths.lock_file)
    if _INSTANCE_LOCK is None:
        print("Another Email Boss instance holds the lock file. Exiting cleanly.")
        return 0

    from . import create_app
    app = create_app(config=cfg, testing=args.no_threads)
    rt = app.extensions["pb"]
    print(f"Database: {rt.paths.db_file}")
    print(f"License: {rt.license_status().get('status')}")
    print(f"AI: {rt.ai_status().get('provider')}")

    tray_proc = None
    if not args.no_threads:
        start_backup_thread(rt)
        install_triage_pipeline(rt)
        start_triage_thread(rt)
        start_license_heartbeat(rt, app)
        start_update_check(rt, app)
        print(f"Mail: {mail_thread_summary(rt)}")
    if args.desktop:
        from . import desktop

        def _cleanup():
            runner = getattr(rt, "mail_runner", None)
            if runner is not None:
                runner.stop_all()
            if tray_proc:
                tray_proc.terminate()

        # tray needs the real port, which desktop.run() picks; start it from there
        def _after_engine(url):
            nonlocal tray_proc
            if not args.no_tray and not cfg.setup_required:
                from urllib.parse import urlparse
                u = urlparse(url)
                tray_proc = start_tray(u.port, u.hostname, getattr(rt, "session_token", ""))
                rt.tray_proc = tray_proc

        desktop.ENGINE_READY_HOOK = _after_engine
        return desktop.run(app, rt, cfg, on_exit=_cleanup)

    if not args.no_tray and not cfg.setup_required:
        tray_proc = start_tray(port, "127.0.0.1" if bind == "0.0.0.0" else bind, getattr(rt, "session_token", ""))
        rt.tray_proc = tray_proc

    if bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {bind}; every /api request needs the bearer token")
    url = f"http://{'127.0.0.1' if bind in ('0.0.0.0', '') else bind}:{port}/"
    auth_url = f"{url}auth/{getattr(rt, 'session_token', '')}"
    print(f"Serving on {url}" + ("  (setup required)" if cfg.setup_required else ""))
    print(f"Open this link once in your browser (it signs this browser in): {auth_url}")
    if not args.no_browser and os.environ.get("PB_NO_BROWSER") != "1":
        open_browser_later(auth_url)
    try:
        app.run(host=bind, port=port, debug=False, threaded=True, use_reloader=False)
    finally:
        runner = getattr(rt, "mail_runner", None)
        if runner is not None:
            try:
                runner.stop_all()
            except Exception:
                pass
        if tray_proc:
            try:
                tray_proc.terminate()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
