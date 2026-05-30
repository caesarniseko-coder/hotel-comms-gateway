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

-- FTS5 virtual table over workflow event payloads for /search
CREATE VIRTUAL TABLE IF NOT EXISTS workflow_events_fts USING fts5(
    issue_id UNINDEXED,
    actor UNINDEXED,
    event_type UNINDEXED,
    room_number UNINDEXED,
    body,
    content='workflow_events',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS workflow_events_ai AFTER INSERT ON workflow_events BEGIN
    INSERT INTO workflow_events_fts(rowid, issue_id, actor, event_type, room_number, body)
    VALUES (new.id, new.issue_id, new.actor, new.event_type, new.room_number,
            COALESCE(new.payload, ''));
END;

-- Per-topic open threads — same guest can have parallel issues, one per topic
CREATE TABLE IF NOT EXISTS guest_topic_threads (
    channel        TEXT NOT NULL,
    sender_id      TEXT NOT NULL,
    topic          TEXT NOT NULL,
    issue_id       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    agent_name     TEXT,
    created_at     INTEGER NOT NULL,
    last_message_at INTEGER NOT NULL,
    closed_at      INTEGER,
    PRIMARY KEY (channel, sender_id, topic, issue_id)
);
CREATE INDEX IF NOT EXISTS idx_topic_threads_open ON guest_topic_threads(channel, sender_id, topic) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_topic_threads_issue ON guest_topic_threads(issue_id);

-- Cross-stay guest memory (keyed by tg_user_id)
CREATE TABLE IF NOT EXISTS guest_memory (
    tg_user_id     TEXT PRIMARY KEY,
    first_contact  INTEGER NOT NULL,
    last_contact   INTEGER NOT NULL,
    message_count  INTEGER NOT NULL DEFAULT 0,
    stays_count    INTEGER NOT NULL DEFAULT 0,
    sentiment_avg  REAL,
    vip_flag       INTEGER NOT NULL DEFAULT 0,
    last_room      TEXT,
    preferences    TEXT,             -- JSON array of stated prefs
    notes          TEXT,             -- free-form notes added by agents/admin
    total_spend_usd REAL NOT NULL DEFAULT 0
);

-- Per-room memory (history of occupants, defects, services)
CREATE TABLE IF NOT EXISTS room_memory (
    room_number    TEXT PRIMARY KEY,
    occupied       INTEGER NOT NULL DEFAULT 0,
    current_guest_tg TEXT,
    last_checkout  INTEGER,
    last_service   INTEGER,
    open_wos       INTEGER NOT NULL DEFAULT 0,
    complaints_count INTEGER NOT NULL DEFAULT 0,
    notes          TEXT
);

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


# ---- per-topic threads (parallel issues per guest) -------------------------

# Threads older than this and not heard from are considered stale → new issue
TOPIC_THREAD_FRESHNESS_SECONDS = 30 * 60  # 30 minutes


def find_open_topic_thread(
    channel: str,
    sender_id: str,
    topic: str,
) -> Optional[dict]:
    """Return the most-recent OPEN thread for (channel, sender, topic), if fresh."""
    cutoff = int(time.time()) - TOPIC_THREAD_FRESHNESS_SECONDS
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM guest_topic_threads "
            "WHERE channel=? AND sender_id=? AND topic=? AND status='open' "
            "AND last_message_at >= ? "
            "ORDER BY last_message_at DESC LIMIT 1",
            (channel, sender_id, topic, cutoff),
        ).fetchone()
        return dict(row) if row else None


def upsert_topic_thread(
    *,
    channel: str,
    sender_id: str,
    topic: str,
    issue_id: str,
    agent_name: Optional[str] = None,
) -> dict:
    now = int(time.time())
    with _conn() as c:
        existing = c.execute(
            "SELECT * FROM guest_topic_threads WHERE channel=? AND sender_id=? AND topic=? AND issue_id=?",
            (channel, sender_id, topic, issue_id),
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE guest_topic_threads SET last_message_at=?, agent_name=COALESCE(?, agent_name) "
                "WHERE channel=? AND sender_id=? AND topic=? AND issue_id=?",
                (now, agent_name, channel, sender_id, topic, issue_id),
            )
        else:
            c.execute(
                "INSERT INTO guest_topic_threads(channel, sender_id, topic, issue_id, status, "
                "agent_name, created_at, last_message_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
                (channel, sender_id, topic, issue_id, agent_name, now, now),
            )
        row = c.execute(
            "SELECT * FROM guest_topic_threads WHERE channel=? AND sender_id=? AND topic=? AND issue_id=?",
            (channel, sender_id, topic, issue_id),
        ).fetchone()
        return dict(row)


def close_topic_thread(issue_id: str) -> None:
    now = int(time.time())
    with _conn() as c:
        c.execute(
            "UPDATE guest_topic_threads SET status='closed', closed_at=? WHERE issue_id=?",
            (now, issue_id),
        )


def list_open_topic_threads(
    channel: Optional[str] = None,
    sender_id: Optional[str] = None,
) -> list[dict]:
    where = ["status='open'"]
    args: list = []
    if channel:
        where.append("channel=?"); args.append(channel)
    if sender_id:
        where.append("sender_id=?"); args.append(sender_id)
    where_clause = " AND ".join(where)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM guest_topic_threads WHERE {where_clause} ORDER BY last_message_at DESC",
            args,
        ).fetchall()
        return [dict(r) for r in rows]


# ---- guest_memory + room_memory --------------------------------------------

def remember_guest(
    tg_user_id: str,
    *,
    sentiment_score: Optional[float] = None,
    last_room: Optional[str] = None,
    preferences: Optional[list[str]] = None,
    notes: Optional[str] = None,
    vip: Optional[bool] = None,
) -> dict:
    """Increment guest-memory counters and update metadata."""
    import json as _json
    now = int(time.time())
    with _conn() as c:
        existing = c.execute(
            "SELECT * FROM guest_memory WHERE tg_user_id=?", (tg_user_id,)
        ).fetchone()
        if existing:
            mc = existing["message_count"] + 1
            # Running average of sentiment
            avg = existing["sentiment_avg"]
            if sentiment_score is not None:
                if avg is None:
                    avg = sentiment_score
                else:
                    avg = (avg * existing["message_count"] + sentiment_score) / mc
            fields = ["last_contact=?", "message_count=?"]
            vals: list = [now, mc]
            if avg is not None:
                fields.append("sentiment_avg=?"); vals.append(avg)
            if last_room:
                fields.append("last_room=?"); vals.append(last_room)
            if preferences is not None:
                fields.append("preferences=?"); vals.append(_json.dumps(preferences))
            if notes is not None:
                fields.append("notes=?"); vals.append(notes)
            if vip is not None:
                fields.append("vip_flag=?"); vals.append(1 if vip else 0)
            vals.append(tg_user_id)
            c.execute(f"UPDATE guest_memory SET {', '.join(fields)} WHERE tg_user_id=?", vals)
        else:
            c.execute(
                "INSERT INTO guest_memory(tg_user_id, first_contact, last_contact, message_count, "
                "stays_count, sentiment_avg, vip_flag, last_room, preferences, notes) "
                "VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?, ?)",
                (tg_user_id, now, now,
                 sentiment_score, 1 if vip else 0, last_room,
                 _json.dumps(preferences) if preferences else None, notes),
            )
        row = c.execute("SELECT * FROM guest_memory WHERE tg_user_id=?", (tg_user_id,)).fetchone()
        return dict(row)


def get_guest_memory(tg_user_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM guest_memory WHERE tg_user_id=?", (tg_user_id,)).fetchone()
        return dict(row) if row else None


def bump_guest_stays(tg_user_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE guest_memory SET stays_count = stays_count + 1 WHERE tg_user_id=?",
                  (tg_user_id,))


def remember_room(
    room_number: str,
    *,
    occupied: Optional[bool] = None,
    current_guest_tg: Optional[str] = None,
    last_checkout: Optional[int] = None,
    last_service: Optional[int] = None,
    open_wos_delta: int = 0,
    complaint: bool = False,
) -> dict:
    now = int(time.time())
    with _conn() as c:
        existing = c.execute("SELECT * FROM room_memory WHERE room_number=?",
                             (room_number,)).fetchone()
        if existing:
            fields = []
            vals: list = []
            if occupied is not None:
                fields.append("occupied=?"); vals.append(1 if occupied else 0)
            if current_guest_tg is not None:
                fields.append("current_guest_tg=?"); vals.append(current_guest_tg)
            if last_checkout is not None:
                fields.append("last_checkout=?"); vals.append(last_checkout)
            if last_service is not None:
                fields.append("last_service=?"); vals.append(last_service)
            if open_wos_delta:
                fields.append("open_wos=open_wos+?"); vals.append(open_wos_delta)
            if complaint:
                fields.append("complaints_count=complaints_count+1")
            if fields:
                vals.append(room_number)
                c.execute(f"UPDATE room_memory SET {', '.join(fields)} WHERE room_number=?", vals)
        else:
            c.execute(
                "INSERT INTO room_memory(room_number, occupied, current_guest_tg, last_checkout, "
                "last_service, open_wos, complaints_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (room_number, 1 if occupied else 0, current_guest_tg,
                 last_checkout, last_service, max(0, open_wos_delta), 1 if complaint else 0),
            )
        row = c.execute("SELECT * FROM room_memory WHERE room_number=?", (room_number,)).fetchone()
        return dict(row)


def get_room_memory(room_number: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM room_memory WHERE room_number=?",
                        (room_number,)).fetchone()
        return dict(row) if row else None


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


def search_events(query: str, limit: int = 20) -> list[dict]:
    """Full-text search across workflow events."""
    if not query:
        return []
    # Escape FTS5 special chars by quoting
    q = '"' + query.replace('"', '') + '"'
    with _conn() as c:
        try:
            rows = c.execute(
                "SELECT e.* FROM workflow_events e JOIN workflow_events_fts f ON e.id = f.rowid "
                "WHERE workflow_events_fts MATCH ? ORDER BY e.ts DESC LIMIT ?",
                (q, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


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
