"""SQLite persistence. Single connection + asyncio lock; low-throughput admin data."""
import json
import sqlite3
import threading
import time
from typing import Any

import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  auth0_user_id TEXT DEFAULT '',
  account_id TEXT DEFAULT '',
  account_name TEXT DEFAULT '',
  refresh_token TEXT NOT NULL,
  auth_kind TEXT DEFAULT 'otp',        -- otp | oauth (Auth0 social login)
  access_token TEXT DEFAULT '',
  id_token TEXT DEFAULT '',
  token_expires_at REAL DEFAULT 0,
  proxy TEXT DEFAULT '',
  max_concurrent INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',          -- active | disabled
  fail_count INTEGER DEFAULT 0,
  cooldown_until REAL DEFAULT 0,
  stats_json TEXT DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  name TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  account_email TEXT DEFAULT '',
  model TEXT DEFAULT '',
  kind TEXT DEFAULT '',
  status TEXT DEFAULT '',
  latency_ms INTEGER DEFAULT 0,
  session_id TEXT DEFAULT '',
  error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON request_log(ts DESC);
CREATE TABLE IF NOT EXISTS conversations (
  prefix_hash TEXT PRIMARY KEY,     -- sha256 of canonical prior messages
  session_id TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  updated_at REAL NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        # lightweight migrations for existing DBs
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(accounts)")}
        if "auth_kind" not in cols:
            _conn.execute("ALTER TABLE accounts ADD COLUMN auth_kind TEXT DEFAULT 'otp'")
        _conn.commit()
    return _conn


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        cur = conn().execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid or cur.rowcount


# ---------- settings ----------

def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_json_setting(key: str, default: Any) -> Any:
    raw = get_setting(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def set_json_setting(key: str, value: Any) -> None:
    set_setting(key, json.dumps(value, ensure_ascii=False))


# ---------- accounts ----------

def list_accounts() -> list[dict]:
    return q("SELECT * FROM accounts ORDER BY id")


def get_account(acc_id: int) -> dict | None:
    return one("SELECT * FROM accounts WHERE id=?", (acc_id,))


def upsert_account(data: dict) -> int:
    now = time.time()
    existing = one("SELECT id FROM accounts WHERE email=?", (data["email"],))
    fields = {
        "auth0_user_id": data.get("auth0_user_id", ""),
        "account_id": data.get("account_id", ""),
        "account_name": data.get("account_name", ""),
        "refresh_token": data["refresh_token"],
        "auth_kind": data.get("auth_kind", "otp"),
        "access_token": data.get("access_token", ""),
        "id_token": data.get("id_token", ""),
        "token_expires_at": data.get("token_expires_at", 0),
        "proxy": data.get("proxy", ""),
        "status": "active",
        "fail_count": 0,
        "cooldown_until": 0,
        "updated_at": now,
    }
    if existing:
        sets = ",".join(f"{k}=?" for k in fields)
        execute(f"UPDATE accounts SET {sets} WHERE id=?",
                (*fields.values(), existing["id"]))
        return existing["id"]
    fields["email"] = data["email"]
    fields["max_concurrent"] = data.get("max_concurrent", 0)
    fields["created_at"] = now
    cols = ",".join(fields)
    ph = ",".join("?" for _ in fields)
    return execute(f"INSERT INTO accounts({cols}) VALUES({ph})", tuple(fields.values()))


def update_account(acc_id: int, patch: dict) -> None:
    if not patch:
        return
    patch["updated_at"] = time.time()
    sets = ",".join(f"{k}=?" for k in patch)
    execute(f"UPDATE accounts SET {sets} WHERE id=?", (*patch.values(), acc_id))


def delete_account(acc_id: int) -> None:
    execute("DELETE FROM accounts WHERE id=?", (acc_id,))


def bump_account_stats(acc_id: int, patch: dict) -> None:
    row = get_account(acc_id)
    if not row:
        return
    try:
        stats = json.loads(row.get("stats_json") or "{}")
    except Exception:
        stats = {}
    stats.update(patch)
    update_account(acc_id, {"stats_json": json.dumps(stats)})


# ---------- api keys ----------

def list_api_keys() -> list[dict]:
    return q("SELECT * FROM api_keys ORDER BY id")


def add_api_key(key: str, name: str = "") -> int:
    return execute("INSERT INTO api_keys(key,name,created_at) VALUES(?,?,?)",
                   (key, name, time.time()))


def api_key_valid(key: str) -> bool:
    if config.API_KEY and key == config.API_KEY:
        return True
    return bool(one("SELECT id FROM api_keys WHERE key=? AND enabled=1", (key,)))


def delete_api_key(kid: int) -> None:
    execute("DELETE FROM api_keys WHERE id=?", (kid,))


# ---------- request log (ring buffer) ----------

def log_request(**kw) -> None:
    execute(
        "INSERT INTO request_log(ts,account_email,model,kind,status,latency_ms,session_id,error) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (time.time(), kw.get("account_email", ""), kw.get("model", ""),
         kw.get("kind", ""), kw.get("status", ""), kw.get("latency_ms", 0),
         kw.get("session_id", ""), kw.get("error", "")[:500]))
    # trim to last 3000 rows
    execute("DELETE FROM request_log WHERE id < (SELECT MIN(id) FROM "
            "(SELECT id FROM request_log ORDER BY id DESC LIMIT 3000))")


def recent_logs(limit: int = 200) -> list[dict]:
    return q("SELECT * FROM request_log ORDER BY id DESC LIMIT ?", (limit,))


# ---------- conversation affinity ----------

def get_conversation(prefix_hash: str, ttl: float) -> dict | None:
    row = one("SELECT * FROM conversations WHERE prefix_hash=?", (prefix_hash,))
    if not row:
        return None
    if row["updated_at"] < time.time() - ttl:
        execute("DELETE FROM conversations WHERE prefix_hash=?", (prefix_hash,))
        return None
    return row


def put_conversation(prefix_hash: str, session_id: str, account_id: int) -> None:
    execute(
        "INSERT INTO conversations(prefix_hash,session_id,account_id,updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(prefix_hash) DO UPDATE SET "
        "session_id=excluded.session_id, account_id=excluded.account_id, "
        "updated_at=excluded.updated_at",
        (prefix_hash, session_id, account_id, time.time()))
    # cheap GC: drop rows idle > 24h
    execute("DELETE FROM conversations WHERE updated_at < ?",
            (time.time() - 86400,))


def delete_conversation(prefix_hash: str) -> None:
    execute("DELETE FROM conversations WHERE prefix_hash=?", (prefix_hash,))
