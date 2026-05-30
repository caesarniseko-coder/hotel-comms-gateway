"""Synthetic-world driver — keeps the hotel team busy.

Maintains an evolving operational state (occupancy, time of day, recent events)
and periodically injects events as comments on agents' standing issues. This is
the engine that drives autonomous operation: the agents act when there's input,
and this module manufactures inputs.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import time
from typing import Any, Optional

import paperclip_client as pc
import staff
import store

log = logging.getLogger("world")

# ---- world state (in-memory, persisted to kv_state) -------------------------

_DEFAULT_STATE = {
    "running": False,
    "day_index": 1,
    "world_hour": 8,        # 24h clock
    "world_minute": 0,
    "occupancy": 0.74,      # 0..1
    "adr": 235.0,
    "pickup_24h": 8,
    "ooo_count": 3,
    "open_wos": 11,
    "fb_covers_today": 78,
    "incidents_today": 0,
    "started_at": None,
    "last_tick": None,
    "events_log": [],       # last N events
}

_LOCK = asyncio.Lock()
_DRIVER_TASK: asyncio.Task | None = None

# Tunables (env overridable)
TICK_SECONDS = int(os.environ.get("WORLD_TICK_SECONDS", "120"))           # 2 min real = 30 min world
EVENT_PROBABILITY_PER_TICK = float(os.environ.get("WORLD_EVENT_PROB", "0.55"))
RESEED_AFTER_RESOLVED = True


# ---- events catalog ---------------------------------------------------------

# Each event: (weight, dept, agent_name, headline template, body template, priority)
EVENT_CATALOG: list[tuple[float, str, str, str, str, Optional[str]]] = [
    # ROOMS / Front Office
    (3.0, "rooms", "Front Office Mgr",
     "Walk-in at front desk",
     "A walk-in just arrived ({adult_count} adults). They're asking about an "
     "upgrade to a suite at last-minute rate. Acknowledge, decide, and brief.",
     None),
    (2.5, "rooms", "Front Office Mgr",
     "Early arrival request",
     "Guest {pax} arrived at 11:40 — booked for 15:00 check-in. Their room "
     "is still in-progress. Decide: hold lobby, offer amenity, expedite turn?",
     None),
    (2.5, "rooms", "Front Office Mgr",
     "Late check-out request",
     "Guest in 1{room2} (corp account) requesting 16:00 check-out for free. "
     "Today's pickup is moderate. Decide and confirm.",
     None),
    (2.0, "rooms", "Concierge",
     "Guest concierge request",
     "Guest in room {room} asks: '{concierge_question}'. Reply naturally.",
     None),
    (1.5, "rooms", "Concierge",
     "VIP arriving tonight",
     "VIP guest {vip_name} ({loyalty_tier}) arriving 19:00, staying 3 nights "
     "on the {suite_floor}th floor. Plan the amenity / personal touch.",
     None),
    # HOUSEKEEPING
    (2.5, "rooms", "Housekeeping Sup",
     "Floor update needed",
     "Rooms board needs its {hour}:00 update. Departures cleaned vs in-progress, "
     "vacant-ready, any priority turns.",
     None),
    (1.5, "rooms", "Executive Housekeeper",
     "Deep-clean window",
     "Today's deep-clean rotation: 5 rooms scheduled. Linen par stock at 78%. "
     "Decide if we need to defer any deep-cleans given today's occupancy.",
     None),
    # ENGINEERING
    (3.0, "engineering", "Chief Engineer",
     "AC fault reported",
     "Guest in room {room} reports AC not cooling. Outdoor temp {outdoor_c}°C. "
     "Dispatch and ETA. If room becomes OOS, hand off to Rooms.",
     "high"),
    (2.0, "engineering", "Chief Engineer",
     "Plumbing leak",
     "Slow leak reported from bathroom in room {room}. Inspect, fix or OOS.",
     None),
    (1.5, "engineering", "Chief Engineer",
     "Lift inspection due",
     "Service lift #2 quarterly inspection due in 48h. Confirm vendor + window.",
     None),
    # F&B
    (2.0, "fnb", "Restaurant Mgr",
     "Dinner cover spike",
     "Restaurant reservations book just added 18 covers for 20:00 (corporate "
     "group). Total now {dinner_covers}. Staffing OK?",
     None),
    (2.0, "fnb", "Exec Chef",
     "Allergen flag — multiple",
     "3 in-house guests flagged: 1 gluten-free, 1 nut-allergy, 1 vegan. Today's "
     "menu compatibility — confirm substitutions and prep notes.",
     None),
    (1.5, "fnb", "D F&B",
     "Banquet RFP inbound",
     "Reservations passed a corporate group RFP — 80 covers, gala dinner for "
     "{rfp_date}. Quote a menu and price.",
     None),
    # COMMERCIAL
    (2.0, "commercial", "Revenue Mgr",
     "Pickup spike",
     "Last 60 min: +5 reservations on Booking.com for arrival within 7 days. "
     "Check rate parity and decide if BAR moves.",
     None),
    (1.5, "commercial", "DOSM",
     "Comp-set rate move",
     "Comp set raised mid-week rates by 8% overnight. Decide our response.",
     None),
    (1.0, "commercial", "Reservations Mgr",
     "Group inquiry",
     "Wholesaler asking for 12 rooms over a soft date next month. Allotment ok?",
     None),
    # FINANCE
    (1.5, "finance", "DOF",
     "Billing dispute",
     "Guest in 1{room2} disputes a $42 minibar charge from last night. Folio "
     "shows the charge. Decide: comp, investigate, or hold the dispute.",
     None),
    # HR
    (1.0, "hr", "HR Director",
     "Roster gap",
     "Tonight's PM housekeeping shift: 2 attendants short due to sick calls. "
     "Approve overtime or call agency. Brief HK.",
     None),
    # SPA
    (1.0, "spa", "Spa Mgr",
     "Couples spa booking",
     "Walk-up inquiry: couples massage tonight 19:00. Therapist availability?",
     None),
    # SECURITY
    (1.5, "security", "Security Mgr",
     "Lost item",
     "Housekeeping found a watch in 1{room2} after departure. Log lost-and-found, "
     "attempt contact.",
     None),
    (0.7, "security", "Security Mgr",
     "Minor incident",
     "Two guests in the lobby raised voices about a queue. De-escalated by "
     "FO. Decide: log only, brief GM?",
     None),
    # MANAGEMENT
    (1.5, "management", "GM",
     "Hourly briefing",
     "It's {hour}:00. Post the hourly state of operations: occupancy, top "
     "events of the last hour, anything that needs your direction.",
     None),
    (1.0, "management", "GM",
     "Guest complaint escalated",
     "A {complaint_topic} complaint escalated to your level. Decide on "
     "recovery offer (room move, F&B credit, comp) and brief the team.",
     "high"),
]


# ---- helpers ----------------------------------------------------------------

def _rand_room() -> str:
    floor = random.randint(2, 12)
    num = random.randint(1, 30)
    return f"{floor}{num:02d}"


_NAMES = ["Mr. Tanaka", "Ms. Liu", "Mr. Hoffman", "Mrs. Patel", "Mr. García",
          "Dr. Müller", "Ms. Park", "Mr. Wong", "Ms. Bernard", "Mr. Schmidt"]

_VIP_NAMES = ["Mr. Saito (Diamond)", "Ms. Chen (Platinum)", "Mr. Bauer (Diamond)",
              "Ms. Okafor (Platinum)", "Mr. Romano (Diamond)"]

_CONCIERGE_Qs = [
    "What time does the gym close tonight?",
    "Can you recommend a Japanese restaurant nearby?",
    "I need a taxi to the airport at 06:00 tomorrow.",
    "Are towels provided at the pool?",
    "Where can I find a quiet place to take a call?",
    "What's the best local sushi place?",
    "Can I get a late dinner in the room?",
    "How do I get to the conference centre?",
]

_COMPLAINT_TOPICS = ["noise next door", "slow check-in this afternoon",
                      "AC problem persisted overnight", "breakfast wait time",
                      "wifi speed", "billing dispute escalation"]


def _fill_template(text: str) -> str:
    return text.format(
        room=_rand_room(),
        room2=f"{random.randint(1,9)}{random.randint(0,9)}",
        pax=random.choice(_NAMES),
        adult_count=random.randint(1, 3),
        hour=f"{datetime.datetime.now().hour:02d}",
        outdoor_c=random.randint(8, 34),
        dinner_covers=random.randint(45, 110),
        concierge_question=random.choice(_CONCIERGE_Qs),
        vip_name=random.choice(_VIP_NAMES),
        loyalty_tier=random.choice(["Diamond", "Platinum", "VIP-Corp"]),
        suite_floor=random.randint(8, 12),
        rfp_date=(datetime.date.today() + datetime.timedelta(days=random.randint(20, 60))).isoformat(),
        complaint_topic=random.choice(_COMPLAINT_TOPICS),
    )


# ---- state I/O --------------------------------------------------------------

_STATE_KEY = "world_state"


def _load_state() -> dict[str, Any]:
    raw = store.kv_get(_STATE_KEY)
    if not raw:
        return dict(_DEFAULT_STATE)
    try:
        return {**_DEFAULT_STATE, **json.loads(raw)}
    except Exception:
        return dict(_DEFAULT_STATE)


def _save_state(s: dict[str, Any]) -> None:
    store.kv_set(_STATE_KEY, json.dumps(s, default=str))


def get_state() -> dict[str, Any]:
    return _load_state()


# ---- world tick -------------------------------------------------------------

def _advance_clock(state: dict[str, Any]) -> None:
    state["world_minute"] += 30
    if state["world_minute"] >= 60:
        state["world_minute"] -= 60
        state["world_hour"] += 1
    if state["world_hour"] >= 24:
        state["world_hour"] = 0
        state["day_index"] = state.get("day_index", 1) + 1
    # Time-of-day occupancy drift
    h = state["world_hour"]
    if 8 <= h <= 11:    # checkout window
        state["occupancy"] = max(0.30, state["occupancy"] - 0.04)
    elif 14 <= h <= 18: # checkin window
        state["occupancy"] = min(0.95, state["occupancy"] + 0.05)
    state["last_tick"] = datetime.datetime.utcnow().isoformat()


def _pick_event() -> tuple[float, str, str, str, str, Optional[str]]:
    weights = [e[0] for e in EVENT_CATALOG]
    return random.choices(EVENT_CATALOG, weights=weights, k=1)[0]


async def _find_active_issue(agent_name: str) -> Optional[str]:
    """Find the most recent open standing issue for this agent."""
    try:
        issues = await pc.list_issues_by_agent_name(agent_name, limit=10)
    except Exception as exc:
        log.warning("list issues failed for %s: %s", agent_name, exc)
        return None
    open_statuses = ("todo", "backlog", "in_progress", "in_review", "blocked")
    for i in issues:
        if (i.get("status") or "").lower() in open_statuses:
            return str(i.get("id"))
    return None


async def _create_event_issue(
    *,
    agent_name: str,
    dept: str,
    headline: str,
    body: str,
    priority: Optional[str],
) -> Optional[str]:
    try:
        issue = await pc.create_issue(
            project_id=os.environ["PAPERCLIP_STAFF_PROJECT_ID"],
            title=f"[event] {headline}",
            body=body,
            assignee_slug=agent_name,
            channel="telegram_staff",
            to=str(staff.admin_user_id() or "0"),
            priority=priority,
            extra_metadata={
                "event": True,
                "agent_name": agent_name,
                "dept": dept,
            },
        )
        issue_id = str(issue.get("id") or "")
        if issue_id:
            store.set_issue_origin(
                issue_id=issue_id,
                origin_kind="autonomy",
                tg_chat_id=None,
                tg_user_id=staff.admin_user_id(),
                department=dept,
            )
        return issue_id
    except Exception as exc:
        log.warning("create event issue failed: %s", exc)
        return None


async def tick_once() -> dict[str, Any]:
    async with _LOCK:
        state = _load_state()
        _advance_clock(state)

        emitted: list[dict[str, Any]] = []
        # Always: GM hourly briefing if minute==0 and hour∈[8..21]
        if state["world_minute"] == 0 and 8 <= state["world_hour"] <= 22:
            body = (
                f"It's {state['world_hour']:02d}:00. Post your hourly operations "
                f"brief: occupancy {int(state['occupancy']*100)}%, ADR ${state['adr']:.0f}, "
                f"pickup-24h {state['pickup_24h']}, OOO {state['ooo_count']}, "
                f"open WOs {state['open_wos']}. Highlight any concerns from the "
                f"last hour. Direct your HODs if needed."
            )
            iid = await _create_event_issue(
                agent_name="GM", dept="management",
                headline=f"Hourly briefing — {state['world_hour']:02d}:00",
                body=body, priority=None,
            )
            if iid:
                emitted.append({"agent": "GM", "issue_id": iid, "headline": "hourly briefing"})

        # Random events
        if random.random() < EVENT_PROBABILITY_PER_TICK:
            n_events = random.choices([1, 2, 3], weights=[6, 3, 1])[0]
            for _ in range(n_events):
                _, dept, agent_name, headline_tpl, body_tpl, priority = _pick_event()
                headline = _fill_template(headline_tpl)
                body = _fill_template(body_tpl)
                iid = await _create_event_issue(
                    agent_name=agent_name, dept=dept,
                    headline=headline, body=body, priority=priority,
                )
                if iid:
                    emitted.append({"agent": agent_name, "issue_id": iid, "headline": headline})
                    if priority == "high":
                        state["incidents_today"] = state.get("incidents_today", 0) + 1
                state["pickup_24h"] = max(0, state["pickup_24h"] + random.choice([-1, 0, 0, 1, 1, 2]))

        # Tail of events log
        log_entries = state.get("events_log") or []
        log_entries.extend(emitted)
        state["events_log"] = log_entries[-30:]

        _save_state(state)
        return {"emitted": emitted, "world_time": f"{state['world_hour']:02d}:{state['world_minute']:02d}",
                "occupancy": state["occupancy"], "state_key": _STATE_KEY}


# ---- runner -----------------------------------------------------------------

async def _driver_loop() -> None:
    log.info("world driver loop starting (tick=%ss)", TICK_SECONDS)
    while True:
        try:
            state = _load_state()
            if not state.get("running"):
                log.info("driver paused, exiting loop")
                return
            result = await tick_once()
            if result.get("emitted"):
                log.info("tick → %d event(s) emitted", len(result["emitted"]))
        except Exception as exc:
            log.exception("driver tick failed: %s", exc)
        await asyncio.sleep(TICK_SECONDS)


def is_running() -> bool:
    return _load_state().get("running", False)


async def start_day() -> dict[str, Any]:
    global _DRIVER_TASK
    state = _load_state()
    if state.get("running"):
        return {"ok": True, "already_running": True}
    state["running"] = True
    state["started_at"] = datetime.datetime.utcnow().isoformat()
    state["events_log"] = []
    state["incidents_today"] = 0
    _save_state(state)
    if _DRIVER_TASK is None or _DRIVER_TASK.done():
        _DRIVER_TASK = asyncio.create_task(_driver_loop())
    return {"ok": True, "started": state["started_at"], "tick_seconds": TICK_SECONDS}


async def stop_day() -> dict[str, Any]:
    global _DRIVER_TASK
    state = _load_state()
    if not state.get("running"):
        return {"ok": True, "already_stopped": True}
    state["running"] = False
    _save_state(state)
    if _DRIVER_TASK and not _DRIVER_TASK.done():
        _DRIVER_TASK.cancel()
    return {"ok": True, "stopped_at": datetime.datetime.utcnow().isoformat()}
