"""Tiny SQLite layer. One shared connection guarded by a lock (the box has 1 vCPU slice)."""
import sqlite3
import threading
import time

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    quota_bytes INTEGER,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    can_write INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER
);
CREATE TABLE IF NOT EXISTS shares (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vpath TEXT NOT NULL,
    is_dir INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    downloads INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trash (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vpath TEXT NOT NULL,
    stored TEXT NOT NULL,
    is_dir INTEGER NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                config.ensure_dirs()
                c = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA foreign_keys=ON")
                c.execute("PRAGMA busy_timeout=5000")
                c.executescript(SCHEMA)
                _conn = c
    return _conn


def all(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    with _lock:
        r = conn().execute(sql, args).fetchone()
        return dict(r) if r else None


def run(sql: str, args=()) -> int:
    """Execute a write; returns lastrowid."""
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid


def backup(keep: int = 14) -> None:
    """Daily snapshot of the database to DATA_DIR/backups (files themselves are not copied)."""
    import datetime
    d = config.DATA_DIR / "backups"
    d.mkdir(exist_ok=True)
    target = d / f"mydrive-{datetime.date.today():%Y%m%d}.db"
    if target.exists():
        return
    with _lock:
        dst = sqlite3.connect(target)
        conn().backup(dst)
        dst.close()
    for old in sorted(d.glob("mydrive-*.db"))[:-keep]:
        old.unlink()


def now() -> int:
    return int(time.time())
