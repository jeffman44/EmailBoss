"""Does SQLCipher work here, frozen or not? (2.0 plan Phase 1 encryption decision.)

Checks, in a temporary folder:
1. ``sqlcipher3`` imports and reports a cipher version
2. a keyed database takes FTS5 (unicode61 + prefix) and FTS5 trigram tables
3. a marker string written to it is not readable in the file bytes
4. reopening with the right key reads it back; a wrong key cannot
5. ``secure_delete`` and WAL are accepted

Prints one JSON line and exits 0 only when every check passed. CI runs it
as a script and again as a PyInstaller one-file exe on windows-latest.
"""
import json
import os
import sys
import tempfile


def main() -> int:
    out = {"python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False)), "checks": {}}
    ok = True
    try:
        from sqlcipher3 import dbapi2 as sc
    except Exception as e:  # noqa: BLE001
        out["checks"]["import"] = f"FAIL {type(e).__name__}: {e}"
        print(json.dumps(out))
        return 1
    out["checks"]["import"] = "ok"
    key = "ab" * 32
    marker = "EMAILBOSS-MARKER-7f3a9"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "probe.sqlite")
        try:
            c = sc.connect(path)
            c.execute(f"PRAGMA key = \"x'{key}'\"")
            out["cipher_version"] = (c.execute("PRAGMA cipher_version").fetchone() or [None])[0]
            out["sqlite_version"] = sc.sqlite_version
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA secure_delete=ON")
            c.execute("CREATE VIRTUAL TABLE f USING fts5(subject, body, tokenize='unicode61 remove_diacritics 2', prefix='2 3')")
            c.execute("CREATE VIRTUAL TABLE t USING fts5(codes, tokenize='trigram')")
            c.execute("INSERT INTO f VALUES (?, ?)", ("lobby retrofit", marker))
            c.execute("INSERT INTO t VALUES (?)", ("VFD-4471",))
            c.commit()
            hits = c.execute("SELECT count(*) FROM f WHERE f MATCH 'lob*'").fetchone()[0]
            tri = c.execute("SELECT count(*) FROM t WHERE t MATCH '4471'").fetchone()[0]
            out["checks"]["fts5"] = "ok" if hits == 1 else f"FAIL prefix hits={hits}"
            out["checks"]["trigram"] = "ok" if tri == 1 else f"FAIL trigram hits={tri}"
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
            raw = b"".join(open(p, "rb").read() for p in (path, path + "-wal") if os.path.exists(p))
            out["checks"]["ciphertext"] = "ok" if marker.encode() not in raw else "FAIL marker readable on disk"
            c = sc.connect(path)
            c.execute(f"PRAGMA key = \"x'{key}'\"")
            back = c.execute("SELECT body FROM f").fetchone()[0]
            c.close()
            out["checks"]["reopen"] = "ok" if back == marker else "FAIL wrong value"
            try:
                c = sc.connect(path)
                c.execute(f"PRAGMA key = \"x'{'cd' * 32}'\"")
                c.execute("SELECT count(*) FROM f").fetchone()
                out["checks"]["wrong_key"] = "FAIL wrong key opened the file"
            except Exception:
                out["checks"]["wrong_key"] = "ok"
            finally:
                try:
                    c.close()
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            out["checks"]["run"] = f"FAIL {type(e).__name__}: {e}"
    ok = all(v == "ok" for v in out["checks"].values())
    out["ok"] = ok
    print(json.dumps(out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
