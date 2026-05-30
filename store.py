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

-- Workspace: staff identity (Telegram user → hotel role)
CREATE TABLE IF NOT EXISTS staff_members (
    tg_user_id     TEXT PRIMARY KEY,
    tg_username    TEXT,
    display_name   TEXT,
    department     TEXT,
    role_title     TEXT,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    last_active    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_staff_dept ON staff_members(department);
CREATE INDEX IF NOT EXISTS idx_staff_username ON staff_members(tg_username);

-- Workspace: dept group registry (Telegram chat → department)
CREATE TABLE IF NOT EXISTS dept_groups (
    tg_chat_id     TEXT PRIMARY KEY,
    chat_title     TEXT,
    department     TEXT NOT NULL,
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dept_groups_dept ON dept_groups(department);

-- Workspace: key-value bag (e.g. bootstrap admin claimed flag, throttle state)
CREATE TABLE IF NOT EXISTS kv_state (
    key            TEXT PRIMARY KEY,
    value          TEXT,
    updated_at     INTEGER NOT NULL
);

-- Workspace: rolling map from Paperclip issue → origin (staff DM, dept group, guest channel)
CREATE TABLE IF NOT EXISTS issue_origin (
    issue_id       TEXT PRIMARY KEY,
    origin_kind    TEXT NOT NULL,
    tg_chat_id     TEXT,
    tg_user_id     TEXT,
    department     TEXT,
    created_at     INTEGER NOT NULL
);

-- Workflow events log — every transition timestamped for analytics
CREATE TABLE IF NOT EXISTS workflow_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id       TEXT,
    parent_issue   TEXT,
    event_type     TEXT NOT NULL,
    actor          TEXT,
    actor_kind     TEXT,
    intent         TEXT,
    department     TEXT,
    priority       TEXT,
    room_number    TEXT,
    payload        TEXT,
    ts             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_issue ON workflow_events(issue_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON workflow_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON workflow_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_room ON workflow_events(room_number);

-- Hotel-side: which Telegram guest user is currently in which room
CREATE TABLE IF NOT EXISTS room_assignments (
    tg_user_id     TEXT PRIMARY KEY,
    room_number    TEXT NOT NULL,
    guest_name     TEXT,
    check_in_at    INTEGER NOT NULL,
    check_out_at   INTEGER,
    tg_chat_id     TEXT,
    profile_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_room_active ON room_assignments(room_number) WHERE check_out_at IS NULL;
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


def list_thread_map(updated_since_days: int = 14) -> list[tuple[str, str, str]]:
    """Return [(channel, sender_id, issue_id), ...] for threads created in the window."""
    cutoff = int(time.time()) - updated_since_days * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT channel, sender_id, issue_id FROM thread_map WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        return [(r["channel"], r["sender_id"], r["issue_id"]) for r in rows]


# ---- staff identity ---------------------------------------------------------

def get_staff_by_tg_user(tg_user_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM staff_members WHERE tg_user_id=?", (tg_user_id,)).fetchone()
        return dict(row) if row else None


def get_staff_by_username(tg_username: str) -> Optional[dict]:
    if not tg_username:
        return None
    uname = tg_username.lstrip("@").lower()
    with _conn() as c:
        row = c.execute("SELECT * FROM staff_members WHERE LOWER(tg_username)=?", (uname,)).fetchone()
        return dict(row) if row else None


def upsert_staff(
    tg_user_id: str,
    tg_username: Optional[str],
    display_name: str,
    department: Optional[str] = None,
    role_title: Optional[str] = None,
    is_admin: Optional[bool] = None,
) -> dict:
    now = int(time.time())
    with _conn() as c:
        existing = c.execute(
            "SELECT * FROM staff_members WHERE tg_user_id=?", (tg_user_id,)
        ).fetchone()
        if existing:
            fields = []
            vals: list = []
            if tg_username is not None:
                fields.append("tg_username=?"); vals.append(tg_username)
            if display_name:
                fields.append("display_name=?"); vals.append(display_name)
            if department is not None:
                fields.append("department=?"); vals.append(department)
            if role_title is not None:
                fields.append("role_title=?"); vals.append(role_title)
            if is_admin is not None:
                fields.append("is_admin=?"); vals.append(1 if is_admin else 0)
            fields.append("last_active=?"); vals.append(now)
            vals.append(tg_user_id)
            c.execute(f"UPDATE staff_members SET {', '.join(fields)} WHERE tg_user_id=?", vals)
        else:
            c.execute(
                "INSERT INTO staff_members(tg_user_id, tg_username, display_name, department, role_title, is_admin, created_at, last_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tg_user_id, tg_username, display_name, department, role_title,
                 1 if is_admin else 0, now, now),
            )
        row = c.execute("SELECT * FROM staff_members WHERE tg_user_id=?", (tg_user_id,)).fetchone()
        return dict(row)


def list_staff(department: Optional[str] = None) -> list[dict]:
    with _conn() as c:
        if department:
            rows = c.execute("SELECT * FROM staff_members WHERE department=?", (department,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM staff_members").fetchall()
        return [dict(r) for r in rows]


def touch_staff(tg_user_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE staff_members SET last_active=? WHERE tg_user_id=?",
                  (int(time.time()), tg_user_id))


# ---- dept groups ------------------------------------------------------------

def get_dept_group(tg_chat_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM dept_groups WHERE tg_chat_id=?", (tg_chat_id,)).fetchone()
        return dict(row) if row else None


def get_chat_for_dept(department: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT tg_chat_id FROM dept_groups WHERE department=? ORDER BY created_at DESC LIMIT 1",
            (department,),
        ).fetchone()
        return row["tg_chat_id"] if row else None


def set_dept_group(tg_chat_id: str, chat_title: Optional[str], department: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO dept_groups(tg_chat_id, chat_title, department, created_at) VALUES (?, ?, ?, ?)",
            (tg_chat_id, chat_title, department, int(time.time())),
        )


def list_dept_groups() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM dept_groups").fetchall()]


# ---- kv state ---------------------------------------------------------------

def kv_get(key: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO kv_state(key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, int(time.time())),
        )


# ---- issue origin -----------------------------------------------------------

def set_issue_origin(
    issue_id: str,
    origin_kind: str,
    tg_chat_id: Optional[str] = None,
    tg_user_id: Optional[str] = None,
    department: Optional[str] = None,
) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO issue_origin(issue_id, origin_kind, tg_chat_id, tg_user_id, department, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (issue_id, origin_kind, tg_chat_id, tg_user_id, department, int(time.time())),
        )


def get_issue_origin(issue_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM issue_origin WHERE issue_id=?", (issue_id,)).fetchone()
        return dict(row) if row else None


def list_issue_origins(since_days: int = 30) -> list[dict]:
    cutoff = int(time.time()) - since_days * 86400
    with _conn() as c:
        rows = c.execute("SELECT * FROM issue_origin WHERE created_at >= ?", (cutoff,)).fetchall()
        return [dict(r) for r in rows]


# ---- room assignments -------------------------------------------------------

def check_in_guest(
    tg_user_id: str,
    room_number: str,
    guest_name: Optional[str] = None,
    tg_chat_id: Optional[str] = None,
    profile_json: Optional[str] = None,
) -> dict:
    """Check a guest in. Idempotent — overwrites prior assignment."""
    now = int(time.time())
    with _conn() as c:
        # Ensure profile_json column exists (migration for older DBs)
        try:
            c.execute("ALTER TABLE room_assignments ADD COLUMN profile_json TEXT")
        except sqlite3.OperationalError:
            pass
        c.execute(
            "INSERT OR REPLACE INTO room_assignments(tg_user_id, room_number, guest_name, check_in_at, check_out_at, tg_chat_id, profile_json) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (tg_user_id, room_number, guest_name, now, tg_chat_id, profile_json),
        )
        row = c.execute("SELECT * FROM room_assignments WHERE tg_user_id=?", (tg_user_id,)).fetchone()
        return dict(row)


def check_out_guest(tg_user_id: str) -> Optional[dict]:
    now = int(time.time())
    with _conn() as c:
        c.execute("UPDATE room_assignments SET check_out_at=? WHERE tg_user_id=? AND check_out_at IS NULL",
                  (now, tg_user_id))
        row = c.execute("SELECT * FROM room_assignments WHERE tg_user_id=?", (tg_user_id,)).fetchone()
        return dict(row) if row else None


def get_room_for_guest(tg_user_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM room_assignments WHERE tg_user_id=? AND check_out_at IS NULL",
            (tg_user_id,),
        ).fetchone()
        return dict(row) if row else None


def get_guests_in_room(room_number: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM room_assignments WHERE room_number=? AND check_out_at IS NULL ORDER BY check_in_at DESC",
            (room_number,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_active_rooms() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM room_assignments WHERE check_out_at IS NULL ORDER BY check_in_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---- workflow events --------------------------------------------------------

def log_event(
    *,
    event_type: str,
    issue_id: Optional[str] = None,
    parent_issue: Optional[str] = None,
    actor: Optional[str] = None,
    actor_kind: Optional[str] = None,
    intent: Optional[str] = None,
    department: Optional[str] = None,
    priority: Optional[str] = None,
    room_number: Optional[str] = None,
    payload: Optional[dict] = None,
) -> int:
    import json as _json
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO workflow_events(issue_id, parent_issue, event_type, actor, actor_kind, "
            "intent, department, priority, room_number, payload, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue_id, parent_issue, event_type, actor, actor_kind,
                intent, department, priority, room_number,
                _json.dumps(payload, default=str) if payload is not None else None,
                int(time.time()),
            ),
        )
        return cur.lastrowid


def list_events_for_issue(issue_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM workflow_events WHERE issue_id=? OR parent_issue=? ORDER BY ts ASC",
            (issue_id, issue_id),
        ).fetchall()
        return [dict(r) for r in rows]


def events_since(since_ts: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM workflow_events WHERE ts >= ? ORDER BY ts ASC",
                         (since_ts,)).fetchall()
        return [dict(r) for r in rows]


def analytics_summary(since_ts: int) -> dict:
    """Aggregate event counts + averages since a timestamp."""
    with _conn() as c:
        types = dict(c.execute(
            "SELECT event_type, COUNT(*) FROM workflow_events WHERE ts>=? GROUP BY event_type",
            (since_ts,)).fetchall())
        intents = dict(c.execute(
            "SELECT COALESCE(intent,'?'), COUNT(*) FROM workflow_events "
            "WHERE ts>=? AND event_type='created' GROUP BY intent",
            (since_ts,)).fetchall())
        depts = dict(c.execute(
            "SELECT COALESCE(department,'?'), COUNT(*) FROM workflow_events "
            "WHERE ts>=? AND event_type='created' GROUP BY department",
            (since_ts,)).fetchall())
        agents = dict(c.execute(
            "SELECT COALESCE(actor,'?'), COUNT(*) FROM workflow_events "
            "WHERE ts>=? AND event_type='agent_reply' GROUP BY actor",
            (since_ts,)).fetchall())
        rooms = dict(c.execute(
            "SELECT COALESCE(room_number,'?'), COUNT(*) FROM workflow_events "
            "WHERE ts>=? AND event_type='guest_message' GROUP BY room_number",
            (since_ts,)).fetchall())
        # Resolution times — created → first agent_reply, per issue
        rt_rows = c.execute(
            "SELECT issue_id, MIN(ts) as t_create FROM workflow_events "
            "WHERE event_type='created' AND ts>=? GROUP BY issue_id",
            (since_ts,)).fetchall()
        replied_rows = c.execute(
            "SELECT issue_id, MIN(ts) as t_reply FROM workflow_events "
            "WHERE event_type='agent_reply' AND ts>=? GROUP BY issue_id",
            (since_ts,)).fetchall()
        created_map = {r['issue_id']: r['t_create'] for r in rt_rows}
        reply_map = {r['issue_id']: r['t_reply'] for r in replied_rows}
        deltas = [reply_map[i] - created_map[i] for i in created_map if i in reply_map]
        avg_resolution = (sum(deltas) / len(deltas)) if deltas else None
        return {
            "events_by_type": types,
            "issues_by_intent": intents,
            "issues_by_department": depts,
            "replies_by_agent": agents,
            "messages_by_room": rooms,
            "avg_response_seconds": round(avg_resolution, 1) if avg_resolution is not None else None,
            "resolved_count": len(deltas),
            "open_count": max(0, len(created_map) - len(deltas)),
        }


# ---- idempotency ------------------------------------------------------------

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
