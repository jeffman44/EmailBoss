"""Cold-open probe, run in a fresh interpreter by test_bench.py.

Times import + create_app + the first board page + the first /api/health,
which is what the desktop shell waits on before it can paint. Prints one
JSON line: {"cold_open_s": ...}.
"""
import json
import os
import sys
import time

t0 = time.perf_counter()
root = sys.argv[1]
os.environ.update({"PB_DATA_ROOT": root, "PB_LICENSE_DEV": "1", "PB_TESTING": "1"})

from plannerboard import create_app  # noqa: E402
from plannerboard.config import Config  # noqa: E402

doc = {"user": {"name": "Pat Example", "email": "pat@example.com"},
       "profession": {"template": "general"},
       "paths": {"data_root": root}}
cfg = Config(doc)
cfg.paths.ensure()
cfg.write()
cfg = Config.load(cfg.source_path)
app = create_app(config=cfg, testing=True)
c = app.test_client()
status_index = c.get("/").status_code
status_health = c.get("/api/health").status_code
elapsed = time.perf_counter() - t0
print(json.dumps({"cold_open_s": round(elapsed, 3), "index_status": status_index,
                  "health_status": status_health}))
