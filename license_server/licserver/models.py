"""SQLite persistence (stdlib sqlite3, WAL, one connection per call).

Tables:
    accounts(id, email, pw_hash, created, is_admin, verified, ai_trial_granted)
    email_verifications(token, account_id, created, expires, used)
    sessions(token, account_id, csrf, created, expires)
    password_resets(token, account_id, created, expires, used)
    licenses(lid, account_id, plan, seats, features_json, issued, expires, revoked, stripe_sub_id, key)
    activations(id, lid, fingerprint, app_version, first_seen, last_seen, valid_until, revoked, secret_hash)
    audit_log(id, ts, actor, action, target, detail)
    ai_wallets(lid, balance, trial_granted, updated)
    ai_ledger(id, lid, fingerprint, ts, task, tier, model, in_tok, out_tok, vendor_usd, credits, ok, error)
    ai_purchases(id, lid, stripe_session, credits, usd_cents, ts)
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    pw_hash     TEXT NOT NULL,
    created     TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    verified    INTEGER NOT NULL DEFAULT 0,
    ai_trial_granted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS email_verifications (
    token       TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created     TEXT NOT NULL,
    expires     TEXT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    csrf        TEXT NOT NULL,
    created     TEXT NOT NULL,
    expires     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets (
    token       TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created     TEXT NOT NULL,
    expires     TEXT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS licenses (
    lid           TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    plan          TEXT NOT NULL CHECK (plan IN ('trial','solo','team')),
    seats         INTEGER NOT NULL DEFAULT 1,
    features_json TEXT NOT NULL DEFAULT '[]',
    issued        TEXT NOT NULL,
    expires       TEXT NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0,
    stripe_sub_id TEXT,
    key           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_licenses_account ON licenses(account_id);
CREATE INDEX IF NOT EXISTS ix_licenses_stripe ON licenses(stripe_sub_id);
CREATE TABLE IF NOT EXISTS activations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lid         TEXT NOT NULL REFERENCES licenses(lid) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    app_version TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    secret_hash TEXT NOT NULL DEFAULT '',
    UNIQUE (lid, fingerprint)
);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    ts       TEXT NOT NULL,
    type     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL DEFAULT '',
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ai_wallets (
    lid           TEXT PRIMARY KEY REFERENCES licenses(lid) ON DELETE CASCADE,
    balance       INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    trial_granted INTEGER NOT NULL DEFAULT 0,
    updated       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lid         TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    ts          TEXT NOT NULL,
    task        TEXT NOT NULL DEFAULT '',
    tier        TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    in_tok      INTEGER NOT NULL DEFAULT 0,
    out_tok     INTEGER NOT NULL DEFAULT 0,
    vendor_usd  REAL NOT NULL DEFAULT 0,
    credits     INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 1,
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_ai_ledger_lid_ts ON ai_ledger(lid, ts);
CREATE TABLE IF NOT EXISTS proof_nonces (
    lid    TEXT NOT NULL,
    nonce  TEXT NOT NULL,
    ts     INTEGER NOT NULL,
    PRIMARY KEY (lid, nonce)
);
CREATE INDEX IF NOT EXISTS ix_proof_nonces_ts ON proof_nonces(ts);
CREATE TABLE IF NOT EXISTS trial_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    net     TEXT NOT NULL DEFAULT '',
    lid     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_trial_log_ts ON trial_log(ts);
CREATE TABLE IF NOT EXISTS ai_purchases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    lid            TEXT NOT NULL,
    stripe_session TEXT NOT NULL UNIQUE,
    credits        INTEGER NOT NULL,
    usd_cents      INTEGER NOT NULL DEFAULT 0,
    ts             TEXT NOT NULL
);
"""

SESSION_TTL = timedelta(days=14)
RESET_TTL = timedelta(hours=2)
VERIFY_TTL = timedelta(days=7)

# Columns added after the first release: (table, column, DDL). init() adds
# any that an older database lacks.
MIGRATIONS = (
    ("accounts", "ai_trial_granted", "ALTER TABLE accounts ADD COLUMN ai_trial_granted INTEGER NOT NULL DEFAULT 0"),
    ("activations", "secret_hash", "ALTER TABLE activations ADD COLUMN secret_hash TEXT NOT NULL DEFAULT ''"),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def new_id(prefix: str = "", nbytes: int = 12) -> str:
    return f"{prefix}{secrets.token_urlsafe(nbytes)}"


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self.init()

    # ------------------------------------------------------------ core

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as c:
            if self.path != ":memory:":
                c.execute("PRAGMA journal_mode = WAL")
            c.executescript(SCHEMA)
            for table, col, ddl in MIGRATIONS:
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    c.execute(ddl)

    def _one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        with self.connect() as c:
            row = c.execute(sql, params).fetchone()
            return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def _exec(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self.connect() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    # ------------------------------------------------------------ audit

    def audit(self, action: str, actor: str = "", target: str = "", detail: Any = "") -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, sort_keys=True, default=str)
        self._exec("INSERT INTO audit_log(ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                   (iso(utcnow()), actor, action, target, detail))

    def audit_entries(self, limit: int = 200) -> list[dict]:
        return self._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ accounts

    def create_account(self, email: str, pw_hash: str, is_admin: bool = False, verified: bool = False) -> dict:
        aid = new_id("acc_")
        self._exec("INSERT INTO accounts(id, email, pw_hash, created, is_admin, verified) VALUES (?,?,?,?,?,?)",
                   (aid, email.lower().strip(), pw_hash, iso(utcnow()), int(is_admin), int(verified)))
        return self.get_account(aid)  # type: ignore[return-value]

    def get_account(self, aid: str) -> Optional[dict]:
        return self._one("SELECT * FROM accounts WHERE id = ?", (aid,))

    def get_account_by_email(self, email: str) -> Optional[dict]:
        return self._one("SELECT * FROM accounts WHERE email = ?", (email.lower().strip(),))

    def list_accounts(self, limit: int = 500) -> list[dict]:
        return self._all(
            "SELECT a.*, (SELECT COUNT(*) FROM licenses l WHERE l.account_id = a.id) AS license_count "
            "FROM accounts a ORDER BY created DESC LIMIT ?", (limit,))

    def set_password(self, aid: str, pw_hash: str) -> None:
        self._exec("UPDATE accounts SET pw_hash = ? WHERE id = ?", (pw_hash, aid))
        self._exec("DELETE FROM sessions WHERE account_id = ?", (aid,))

    def set_admin(self, aid: str, is_admin: bool) -> None:
        self._exec("UPDATE accounts SET is_admin = ? WHERE id = ?", (int(is_admin), aid))

    def mark_verified(self, aid: str) -> None:
        self._exec("UPDATE accounts SET verified = 1 WHERE id = ?", (aid,))

    def create_verification(self, aid: str) -> str:
        token = secrets.token_urlsafe(32)
        now = utcnow()
        self._exec("INSERT INTO email_verifications(token, account_id, created, expires) VALUES (?,?,?,?)",
                   (self._reset_digest(token), aid, iso(now), iso(now + VERIFY_TTL)))
        return token

    def consume_verification(self, token: str) -> Optional[str]:
        """Account id for a live verification token (marks it used), else None."""
        digest = self._reset_digest(token)
        row = self._one("SELECT * FROM email_verifications WHERE token = ? AND used = 0", (digest,))
        if not row:
            return None
        exp = parse_iso(row["expires"])
        if not exp or exp < utcnow():
            return None
        with self._lock, self.connect() as c:
            cur = c.execute("UPDATE email_verifications SET used = 1 WHERE token = ? AND used = 0", (digest,))
            if cur.rowcount != 1:
                return None
        return row["account_id"]

    def account_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM accounts")
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------ sessions

    def create_session(self, aid: str) -> dict:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = utcnow()
        self._exec("INSERT INTO sessions(token, account_id, csrf, created, expires) VALUES (?,?,?,?,?)",
                   (token, aid, csrf, iso(now), iso(now + SESSION_TTL)))
        return {"token": token, "csrf": csrf, "account_id": aid, "expires": iso(now + SESSION_TTL)}

    def get_session(self, token: str) -> Optional[dict]:
        if not token:
            return None
        row = self._one("SELECT * FROM sessions WHERE token = ?", (token,))
        if not row:
            return None
        exp = parse_iso(row["expires"])
        if not exp or exp < utcnow():
            self.delete_session(token)
            return None
        return row

    def delete_session(self, token: str) -> None:
        self._exec("DELETE FROM sessions WHERE token = ?", (token,))

    # ------------------------------------------------------------ password resets

    @staticmethod
    def _reset_digest(token: str) -> str:
        # Only the digest is stored: a leaked table yields no live reset links.
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_reset(self, aid: str) -> str:
        token = secrets.token_urlsafe(32)
        now = utcnow()
        self._exec("INSERT INTO password_resets(token, account_id, created, expires) VALUES (?,?,?,?)",
                   (self._reset_digest(token), aid, iso(now), iso(now + RESET_TTL)))
        return token

    def consume_reset(self, token: str) -> Optional[str]:
        """Return the account id if the token is live; marks it used. The
        UPDATE is conditional so two concurrent consumers cannot both win."""
        digest = self._reset_digest(token)
        row = self._one("SELECT * FROM password_resets WHERE token = ? AND used = 0", (digest,))
        if not row:
            return None
        exp = parse_iso(row["expires"])
        if not exp or exp < utcnow():
            return None
        with self._lock, self.connect() as c:
            cur = c.execute("UPDATE password_resets SET used = 1 WHERE token = ? AND used = 0", (digest,))
            if cur.rowcount != 1:
                return None
        return row["account_id"]

    def peek_reset(self, token: str) -> Optional[dict]:
        row = self._one("SELECT * FROM password_resets WHERE token = ? AND used = 0", (self._reset_digest(token),))
        if not row:
            return None
        exp = parse_iso(row["expires"])
        return row if exp and exp >= utcnow() else None

    # ------------------------------------------------------------ licenses

    def insert_license(self, lid: str, account_id: str, plan: str, seats: int, features: list[str],
                       issued: str, expires: str, key: str, stripe_sub_id: str | None = None) -> dict:
        self._exec(
            "INSERT INTO licenses(lid, account_id, plan, seats, features_json, issued, expires, revoked, stripe_sub_id, key) "
            "VALUES (?,?,?,?,?,?,?,0,?,?)",
            (lid, account_id, plan, int(seats), json.dumps(sorted(set(features))), issued, expires, stripe_sub_id, key))
        return self.get_license(lid)  # type: ignore[return-value]

    def update_license(self, lid: str, **fields: Any) -> Optional[dict]:
        if "features" in fields:
            fields["features_json"] = json.dumps(sorted(set(fields.pop("features"))))
        allowed = {"plan", "seats", "features_json", "expires", "revoked", "stripe_sub_id", "key", "issued"}
        cols = [k for k in fields if k in allowed]
        if cols:
            sql = "UPDATE licenses SET " + ", ".join(f"{k} = ?" for k in cols) + " WHERE lid = ?"
            self._exec(sql, tuple(fields[k] for k in cols) + (lid,))
        return self.get_license(lid)

    def _lic(self, row: Optional[dict]) -> Optional[dict]:
        if row is None:
            return None
        row["features"] = json.loads(row.get("features_json") or "[]")
        row["revoked"] = bool(row.get("revoked"))
        return row

    def get_license(self, lid: str) -> Optional[dict]:
        return self._lic(self._one("SELECT * FROM licenses WHERE lid = ?", (lid,)))

    def get_license_by_stripe_sub(self, sub_id: str) -> Optional[dict]:
        return self._lic(self._one("SELECT * FROM licenses WHERE stripe_sub_id = ?", (sub_id,)))

    def licenses_for_account(self, aid: str) -> list[dict]:
        return [self._lic(r) for r in self._all(  # type: ignore[misc]
            "SELECT * FROM licenses WHERE account_id = ? ORDER BY issued DESC", (aid,))]

    def list_licenses(self, limit: int = 500) -> list[dict]:
        return [self._lic(r) for r in self._all(  # type: ignore[misc]
            "SELECT l.*, a.email FROM licenses l JOIN accounts a ON a.id = l.account_id "
            "ORDER BY l.issued DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------ activations

    def active_activations(self, lid: str) -> list[dict]:
        return self._all("SELECT * FROM activations WHERE lid = ? AND revoked = 0 ORDER BY first_seen", (lid,))

    def all_activations(self, lid: str | None = None, limit: int = 500) -> list[dict]:
        if lid:
            return self._all("SELECT * FROM activations WHERE lid = ? ORDER BY last_seen DESC LIMIT ?", (lid, limit))
        return self._all(
            "SELECT ac.*, l.plan, a.email FROM activations ac JOIN licenses l ON l.lid = ac.lid "
            "JOIN accounts a ON a.id = l.account_id ORDER BY ac.last_seen DESC LIMIT ?", (limit,))

    def get_activation(self, lid: str, fingerprint: str) -> Optional[dict]:
        return self._one("SELECT * FROM activations WHERE lid = ? AND fingerprint = ?", (lid, fingerprint))

    def get_activation_by_id(self, act_id: int) -> Optional[dict]:
        return self._one("SELECT * FROM activations WHERE id = ?", (act_id,))

    def claim_seat(self, lid: str, fingerprint: str, seats: int, app_version: str, valid_until: str) -> tuple[bool, int]:
        """Count active seats and insert/refresh this device's activation in
        ONE transaction (BEGIN IMMEDIATE under the process lock), so parallel
        /v1/activate calls cannot overshoot ``seats``. Returns (claimed,
        used_before). A device that already holds a seat always refreshes."""
        now = iso(utcnow())
        with self._lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                mine = c.execute("SELECT revoked FROM activations WHERE lid = ? AND fingerprint = ?",
                                 (lid, fingerprint)).fetchone()
                used = c.execute("SELECT COUNT(*) FROM activations WHERE lid = ? AND revoked = 0", (lid,)).fetchone()[0]
                holds_seat = mine is not None and not mine[0]
                if not holds_seat and used >= int(seats):
                    c.execute("ROLLBACK")
                    return False, int(used)
                c.execute(
                    "INSERT INTO activations(lid, fingerprint, app_version, first_seen, last_seen, valid_until, revoked) "
                    "VALUES (?,?,?,?,?,?,0) "
                    "ON CONFLICT(lid, fingerprint) DO UPDATE SET app_version = excluded.app_version, "
                    "last_seen = excluded.last_seen, valid_until = excluded.valid_until, revoked = 0",
                    (lid, fingerprint, app_version or "", now, now, valid_until))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return True, int(used)

    def upsert_activation(self, lid: str, fingerprint: str, app_version: str, valid_until: str) -> dict:
        now = iso(utcnow())
        with self._lock, self.connect() as c:
            c.execute(
                "INSERT INTO activations(lid, fingerprint, app_version, first_seen, last_seen, valid_until, revoked) "
                "VALUES (?,?,?,?,?,?,0) "
                "ON CONFLICT(lid, fingerprint) DO UPDATE SET app_version = excluded.app_version, "
                "last_seen = excluded.last_seen, valid_until = excluded.valid_until, revoked = 0",
                (lid, fingerprint, app_version or "", now, now, valid_until))
        return self.get_activation(lid, fingerprint)  # type: ignore[return-value]

    def set_activation_secret(self, lid: str, fingerprint: str, secret_hash: str) -> None:
        """Store the sha256 of a freshly issued device secret on the activation row."""
        self._exec("UPDATE activations SET secret_hash = ? WHERE lid = ? AND fingerprint = ?",
                   (secret_hash, lid, fingerprint))

    NONCE_TTL_S = 600

    def claim_nonce(self, lid: str, nonce: str, now_ts: int | None = None) -> bool:
        """Remember a device-proof nonce for NONCE_TTL_S. True the first time
        it is seen for this license, False on a replay. Old rows are pruned."""
        now_ts = int(time.time() if now_ts is None else now_ts)
        with self._lock, self.connect() as c:
            c.execute("DELETE FROM proof_nonces WHERE ts < ?", (now_ts - self.NONCE_TTL_S,))
            try:
                c.execute("INSERT INTO proof_nonces(lid, nonce, ts) VALUES (?,?,?)", (lid, nonce, now_ts))
            except sqlite3.IntegrityError:
                return False
        return True

    def trial_log_add(self, net: str, lid: str) -> None:
        self._exec("INSERT INTO trial_log(ts, net, lid) VALUES (?,?,?)", (iso(utcnow()), net, lid))

    def trial_counts_today(self, net: str) -> tuple[int, int]:
        """(all trials started since 00:00 UTC, those from this network)."""
        day = utcnow().strftime("%Y-%m-%dT00:00:00Z")
        a = self._one("SELECT COUNT(*) AS n FROM trial_log WHERE ts >= ?", (day,))
        b = self._one("SELECT COUNT(*) AS n FROM trial_log WHERE ts >= ? AND net = ?", (day, net))
        return int(a["n"]), int(b["n"])

    def delete_account(self, aid: str) -> None:
        """Remove an account and (by cascade) its licenses, activations and tokens."""
        self._exec("DELETE FROM accounts WHERE id = ?", (aid,))

    def claim_webhook_event(self, event_id: str, etype: str = "") -> bool:
        """True the first time an event id is seen, False on a replay."""
        if not event_id:
            return True
        with self._lock, self.connect() as c:
            try:
                c.execute("INSERT INTO webhook_events(event_id, ts, type) VALUES (?,?,?)",
                          (event_id, iso(utcnow()), etype))
            except sqlite3.IntegrityError:
                return False
        return True

    def revoke_activation(self, lid: str, fingerprint: str) -> bool:
        with self._lock, self.connect() as c:
            cur = c.execute("UPDATE activations SET revoked = 1 WHERE lid = ? AND fingerprint = ? AND revoked = 0",
                            (lid, fingerprint))
            return cur.rowcount > 0

    def revoke_all_activations(self, lid: str) -> int:
        with self._lock, self.connect() as c:
            return c.execute("UPDATE activations SET revoked = 1 WHERE lid = ?", (lid,)).rowcount

    # ------------------------------------------------------------ AI credits

    def get_wallet(self, lid: str) -> Optional[dict]:
        row = self._one("SELECT * FROM ai_wallets WHERE lid = ?", (lid,))
        if row:
            row["trial_granted"] = bool(row["trial_granted"])
        return row

    def ensure_wallet(self, lid: str, trial_credits: int = 0, *, grant_trial: bool | None = None) -> dict:
        """Create the wallet on first use. Trial credits are granted at most
        ONCE PER ACCOUNT (not per license), and only when the wallet is
        created (or on the explicit /v1/trial path): the account's
        ai_trial_granted flag flips in the same transaction that funds it.

        ``grant_trial`` None (default) grants only when the owning account is
        verified; True grants regardless of verification (the /v1/trial path,
        which proves the email by mailing the key); False never grants."""
        trial = max(0, int(trial_credits))
        granted = 0
        with self._lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                created = c.execute("INSERT OR IGNORE INTO ai_wallets(lid, balance, trial_granted, updated) "
                                    "VALUES (?,0,0,?)", (lid, iso(utcnow()))).rowcount == 1
                if trial and grant_trial is not False and (created or grant_trial is True):
                    acct = c.execute("SELECT a.id, a.verified, a.ai_trial_granted FROM licenses l "
                                     "JOIN accounts a ON a.id = l.account_id WHERE l.lid = ?", (lid,)).fetchone()
                    eligible = acct is not None and not acct["ai_trial_granted"] and (
                        grant_trial is True or bool(acct["verified"]))
                    if eligible:
                        cur = c.execute("UPDATE accounts SET ai_trial_granted = 1 WHERE id = ? AND ai_trial_granted = 0",
                                        (acct["id"],))
                        if cur.rowcount == 1:
                            c.execute("UPDATE ai_wallets SET balance = balance + ?, trial_granted = 1, updated = ? "
                                      "WHERE lid = ?", (trial, iso(utcnow()), lid))
                            granted = trial
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        if granted:
            self.audit("ai.trial_grant", actor="system", target=lid, detail={"credits": granted})
        return self.get_wallet(lid)  # type: ignore[return-value]

    def release_webhook_event(self, event_id: str) -> None:
        """Forget a claimed event so Stripe's retry is processed again."""
        if event_id:
            self._exec("DELETE FROM webhook_events WHERE event_id = ?", (event_id,))

    def refund(self, lid: str, credits: int) -> int:
        """Return held credits to the wallet. Returns the new balance."""
        n = max(0, int(credits))
        with self._lock, self.connect() as c:
            if n:
                c.execute("UPDATE ai_wallets SET balance = balance + ?, updated = ? WHERE lid = ?",
                          (n, iso(utcnow()), lid))
            row = c.execute("SELECT balance FROM ai_wallets WHERE lid = ?", (lid,)).fetchone()
            return int(row[0]) if row else 0

    def vendor_usd_today(self, lid: str) -> float:
        """Vendor cost recorded for this license since 00:00 UTC today."""
        day = utcnow().strftime("%Y-%m-%dT00:00:00Z")
        row = self._one("SELECT COALESCE(SUM(vendor_usd), 0) AS usd FROM ai_ledger WHERE lid = ? AND ts >= ?",
                        (lid, day))
        return float(row["usd"]) if row else 0.0

    def debit(self, lid: str, credits: int) -> Optional[int]:
        """Atomically subtract ``credits``. Returns the new balance, or None
        (nothing changed) when the balance is too low or there is no wallet."""
        n = int(credits)
        if n < 0:
            raise ValueError("credits must be >= 0")
        with self._lock, self.connect() as c:
            cur = c.execute("UPDATE ai_wallets SET balance = balance - ?, updated = ? WHERE lid = ? AND balance >= ?",
                            (n, iso(utcnow()), lid, n))
            if cur.rowcount != 1:
                return None
            return int(c.execute("SELECT balance FROM ai_wallets WHERE lid = ?", (lid,)).fetchone()[0])

    def debit_upto(self, lid: str, credits: int) -> tuple[int, int]:
        """Subtract min(credits, balance) in one statement. Returns
        (charged, new_balance). Used after a vendor call already happened."""
        n = int(credits)
        with self._lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT balance FROM ai_wallets WHERE lid = ?", (lid,)).fetchone()
            if not row:
                c.execute("ROLLBACK")
                return 0, 0
            take = max(0, min(n, int(row[0])))
            c.execute("UPDATE ai_wallets SET balance = balance - ?, updated = ? WHERE lid = ?",
                      (take, iso(utcnow()), lid))
            c.execute("COMMIT")
            return take, int(row[0]) - take

    def credit(self, lid: str, credits: int, reason: str = "", actor: str = "system", trial_credits: int = 0) -> int:
        """Add credits (creating the wallet if needed). Returns the new balance."""
        n = int(credits)
        if n <= 0:
            raise ValueError("credits must be positive")
        self.ensure_wallet(lid, trial_credits)
        with self._lock, self.connect() as c:
            c.execute("UPDATE ai_wallets SET balance = balance + ?, updated = ? WHERE lid = ?",
                      (n, iso(utcnow()), lid))
            bal = int(c.execute("SELECT balance FROM ai_wallets WHERE lid = ?", (lid,)).fetchone()[0])
        self.audit("ai.credit", actor=actor, target=lid, detail={"credits": n, "reason": reason, "balance": bal})
        return bal

    def ledger_insert(self, lid: str, fingerprint: str, task: str, tier: str, model: str, in_tok: int,
                      out_tok: int, vendor_usd: float, credits: int, ok: bool, error: str = "") -> int:
        return self._exec(
            "INSERT INTO ai_ledger(lid, fingerprint, ts, task, tier, model, in_tok, out_tok, vendor_usd, credits, ok, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, fingerprint[:16], iso(utcnow()), task[:64], tier[:16], model[:64], int(in_tok), int(out_tok),
             float(vendor_usd), int(credits), int(bool(ok)), error[:64]))

    def ledger_totals(self, lid: str, days: int = 30) -> dict:
        since = iso(utcnow() - timedelta(days=days))
        row = self._one(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(ok),0) AS ok_calls, COALESCE(SUM(in_tok),0) AS in_tok, "
            "COALESCE(SUM(out_tok),0) AS out_tok, COALESCE(SUM(credits),0) AS credits, "
            "COALESCE(SUM(vendor_usd),0) AS vendor_usd FROM ai_ledger WHERE lid = ? AND ts >= ?", (lid, since))
        return row or {"calls": 0, "ok_calls": 0, "in_tok": 0, "out_tok": 0, "credits": 0, "vendor_usd": 0.0}

    def record_purchase(self, lid: str, stripe_session: str, credits: int, usd_cents: int = 0,
                        trial_credits: int = 0) -> bool:
        """Credit a paid pack exactly once per Stripe session id. True when
        applied, False on a replay."""
        n = int(credits)
        if n <= 0 or not stripe_session:
            raise ValueError("credits and stripe_session required")
        self.ensure_wallet(lid, trial_credits)
        with self._lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("INSERT INTO ai_purchases(lid, stripe_session, credits, usd_cents, ts) VALUES (?,?,?,?,?)",
                          (lid, stripe_session, n, int(usd_cents or 0), iso(utcnow())))
            except sqlite3.IntegrityError:
                c.execute("ROLLBACK")
                return False
            c.execute("UPDATE ai_wallets SET balance = balance + ?, updated = ? WHERE lid = ?",
                      (n, iso(utcnow()), lid))
            c.execute("COMMIT")
        self.audit("ai.purchase", actor="stripe", target=lid,
                   detail={"credits": n, "usd_cents": int(usd_cents or 0), "session": stripe_session})
        return True
