"""2.0 Phase 0 baseline benchmark against a generated 50k-message mailbox.

Runs only with ``EB_BENCH=1`` (``make bench``, ``tasks.ps1 bench``, the
``bench`` CI job on windows-latest). Writes ``bench.json`` (path from
``EB_BENCH_OUT``, default ``app/bench.json``) and enforces every non-null
bound in ``thresholds.json``.

Metrics:

- ``cold_open_s``        fresh interpreter: import, create_app, first board page, first /api/health
- ``sync_full_s``        first sync of the whole mailbox through the real capture path
                         (Gmail provider, 1.3 default cap of 100 per cycle, mail cache + sync ledger, triage off)
- ``sync_cycle_ms_p50``  median capture cycle during that sync
- ``reconcile_ms``       one reconcile pass over the full ledger
- ``list_page_ms_p50``   GET /api/mail/client/messages (50 rows) through the API
- ``open_message_ms_p50`` GET /api/mail/client/message through the API
- ``search_ms_p50``      local store search over the synced mailbox (Phase 1)

The fake server answers in-process, so network latency is excluded: these
numbers are the app's own cost, which is what 2.0 changes.
"""
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("EB_BENCH") != "1",
                                reason="benchmark: set EB_BENCH=1 (make bench)")

HERE = Path(__file__).resolve().parent
APP = HERE.parent.parent
N = int(os.environ.get("EB_BENCH_N") or 50_000)


def _ms(seconds):
    return round(seconds * 1000.0, 2)


@pytest.fixture(scope="module")
def results():
    out = {"n_messages": N, "platform": platform.platform(), "python": platform.python_version(),
           "machine": platform.machine(), "metrics": {}}
    yield out
    dest = Path(os.environ.get("EB_BENCH_OUT") or (APP / "bench.json"))
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nbench written to {dest}\n" + json.dumps(out["metrics"], indent=2))


def _bound(name):
    doc = json.loads((HERE / "thresholds.json").read_text(encoding="utf-8"))
    return (doc.get("metrics") or {}).get(name)


def _check(results, name, value):
    results["metrics"][name] = value
    bound = _bound(name)
    if bound is not None:
        assert value <= bound, f"{name} = {value} exceeds the frozen bound {bound}"


def test_cold_open(tmp_path, results):
    runs = []
    for i in range(3):
        root = tmp_path / f"cold{i}"
        root.mkdir()
        p = subprocess.run([sys.executable, str(HERE / "cold_open.py"), str(root)],
                           capture_output=True, text=True, cwd=str(APP), timeout=300)
        assert p.returncode == 0, p.stderr[-2000:]
        doc = json.loads(p.stdout.strip().splitlines()[-1])
        assert doc["index_status"] == 200 and doc["health_status"] == 200
        runs.append(doc["cold_open_s"])
    _check(results, "cold_open_s", round(statistics.median(runs), 3))


@pytest.fixture(scope="module")
def mailbox(tmp_path_factory):
    from plannerboard.mail import secrets
    from tests.test_sync_truth import FakeGmail
    secrets.use_memory_backend(True)
    # Measure what ships: the store encrypts by default (conftest turns that
    # off per test for the unit suite; the module-scoped bench store is
    # created here, before any per-test override).
    os.environ.pop("PB_STORE_ENCRYPT", None)
    server = FakeGmail()
    for i in range(N):
        server.deliver(f"m{i:06d}", labels=("INBOX",) if i % 5 else ("SENT",))
    yield server, tmp_path_factory.mktemp("bench_mail")
    secrets.use_memory_backend(False)


@pytest.fixture(scope="module")
def synced(mailbox, results):
    from plannerboard.mail.gmail import GmailProvider
    from plannerboard.mail.runner import ProviderThread
    server, root = mailbox
    cache = root / "mail_cache"
    prov = GmailProvider({"mail": {"gmail": {"client_id": "cid"}}}, cache, http=server)
    prov.tokens.save({"access_token": "AT", "expires_at": 9_999_999_999})
    t = ProviderThread(prov, {"paths": {"mail_cache": str(cache)}, "mail": {"reconcile_seconds": 10 ** 9,
                                                                  "store": {"backfill": False}}},
                       store=None, profession={}, run_triage=False, poll_seconds=60)
    cycles = []
    started = time.perf_counter()
    for _ in range(N // prov.max_per_cycle + 10):
        c0 = time.perf_counter()
        t.run_once()
        cycles.append(time.perf_counter() - c0)
        if not t.state.get("pending_ids"):
            break
    total = time.perf_counter() - started
    results["sync_cycles"] = len(cycles)
    results["store_encrypted"] = bool(getattr(prov.sync, "encrypted", False))
    return prov, cache, total, cycles


def test_sync_full(synced, results):
    prov, cache, total, cycles = synced
    counts = prov.sync.local_counts("gmail")
    assert counts.get("inbox", 0) + counts.get("sent", 0) == N, counts
    _check(results, "sync_full_s", round(total, 2))
    _check(results, "sync_cycle_ms_p50", _ms(statistics.median(cycles)))


def test_reconcile(synced, results):
    from plannerboard.mail import reconcile
    prov, cache, _total, _cycles = synced
    t0 = time.perf_counter()
    out = reconcile.run(prov, prov.sync)
    took = time.perf_counter() - t0
    assert out["folders"]["inbox"]["not_local"] == 0 and out["folders"]["sent"]["not_local"] == 0
    _check(results, "reconcile_ms", _ms(took))


@pytest.fixture(scope="module")
def api(synced):
    flask = pytest.importorskip("flask")
    from tests.fakes import FakeStore
    from plannerboard.api.mail_client_api import bp
    prov, cache, _t, _c = synced
    app = flask.Flask(__name__)
    cfg = {"mail": {"provider": "gmail", "client_enabled": True, "send_enabled": False},
           "user": {"email": "sam@acme.example"},
           "paths": {"data_root": str(cache.parent), "mail_cache": str(cache)}}
    app.extensions["pb"] = {"store": FakeStore(), "cfg": cfg, "profession": {}, "llm": None,
                            "mail_client_provider": prov}
    app.register_blueprint(bp)
    return app.test_client()


def test_list_page(api, results):
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        r = api.get("/api/mail/client/messages?folder=INBOX&limit=50")
        times.append(time.perf_counter() - t0)
        assert r.status_code == 200 and len(r.get_json()["messages"]) == 50
    _check(results, "list_page_ms_p50", _ms(statistics.median(times)))


def test_open_message(api, results):
    times = []
    for i in range(1, 101):
        ref = f"m{(i * 487) % N:06d}"
        t0 = time.perf_counter()
        r = api.get(f"/api/mail/client/message?ref={ref}")
        times.append(time.perf_counter() - t0)
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
    _check(results, "open_message_ms_p50", _ms(statistics.median(times)))


def test_search(synced, results):
    from plannerboard.mail import index
    prov, _cache, _t, _c = synced
    queries = ["message", "\"Message m012345\"", "from:dana", "body m04", "has:attachment",
               "message -m000001", "in:sent message", "m049"]
    times = []
    for _ in range(5):
        for q in queries:
            t0 = time.perf_counter()
            index.search(prov.sync, q, limit=50)
            times.append(time.perf_counter() - t0)
    assert index.search(prov.sync, "\"Message m012345\"")["messages"][0]["ref"] == "m012345"
    _check(results, "search_ms_p50", _ms(statistics.median(times)))
