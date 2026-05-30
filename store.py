"""SQLite-backed thread map + idempotency store. Single file at /data/store.sqlite."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.environ.get("STORE_DB_PATH", "/data/store.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_map (
    channel     TEXT NOT NULL,
    sender_id   TEXT NOT NULL,
    issue_id    TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (channel, sender_id)
);

CREATE TABLE IF NOT EXISTS idempotency (
    key         TEXT PRIMARY KEY,
    seen_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_idempotency_seen_at ON idempotency(seen_at);
"""


def _ensure_dir() -> None:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)


@contextmanager
def _conn():
    _ensure_dir()
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def get_issue_id(channel: str, sender_id: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT issue_id FROM thread_map WHERE channel=? AND sender_id=?",
            (channel, sender_id),
        ).fetchone()
        return row["issue_id"] if row else None


def set_issue_id(channel: str, sender_id: str, issue_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO thread_map(channel, sender_id, issue_id, created_at) VALUES (?, ?, ?, ?)",
            (channel, sender_id, issue_id, int(time.time())),
        )


def seen(key: str, ttl_seconds: int = 7 * 86400) -> bool:
    """Return True if `key` was already seen. Otherwise mark and return False."""
    now = int(time.time())
    with _conn() as c:
        c.execute("DELETE FROM idempotency WHERE seen_at < ?", (now - ttl_seconds,))
        row = c.execute("SELECT 1 FROM idempotency WHERE key=?", (key,)).fetchone()
        if row:
            return True
        c.execute("INSERT INTO idempotency(key, seen_at) VALUES (?, ?)", (key, now))
        return False
