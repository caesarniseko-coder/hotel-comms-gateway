"""Autonomous-operations seeder.

Creates standing daily-ops issues for each HOD so heartbeats have concrete
work to act on. Without this, an agent woken by its heartbeat has no assigned
issue and hallucinates.
"""
from __future__ import annotations

import datetime
import logging
import os
from typing import Any

import paperclip_client as pc
import store
import staff

log = logging.getLogger("autonomy")

STAFF_PROJECT_ID = os.environ.get("PAPERCLIP_STAFF_PROJECT_ID", "")


# (agent_name, title-suffix, body-framing)
DAILY_SEEDS: list[tuple[str, str, str]] = [
    ("GM",
     "Daily ops briefing",
     "Open the day. Post a short briefing: occupancy, ADR, ALOS, top arrivals, "
     "today's three priorities. Then read the latest replies from your HODs "
     "(DOR, D F&B, DOSM, Chief Engineer, etc.) on their own issues — when "
     "needed, give one-line direction. Mark `STATUS: in_review` when the brief "
     "is posted."),
    ("DOR",
     "Rooms division — today's posture",
     "Read today's arrivals / departures / OOO/OOS picture. Post a brief "
     "(numbers, VIPs, surge windows). If you need amenity prep or a defect "
     "fixed, `@HANDOFF: Engineering` or `@HANDOFF: F&B`."),
    ("Front Office Mgr",
     "Front office — desk pulse",
     "Post a 15-minute desk pulse: queue length, no-shows, late check-out asks, "
     "early arrivals. Flag any guest issue immediately. End with "
     "`STATUS: in_review`."),
    ("Reservations Mgr",
     "Reservations — pickup pulse",
     "Pickup last 24h by segment + channel. Any group movements. Anomalies. "
     "If a soft date emerges, `@HANDOFF: Commercial`."),
    ("Concierge",
     "Guest experience — open touches",
     "Scan VIP arrivals today and any open guest-recovery threads. Post one "
     "actionable update — a confirmation, a recommendation, or a recovery "
     "follow-up. Brief, warm, specific."),
    ("Executive Housekeeper",
     "Housekeeping — floor status",
     "Rooms board: vacant-ready, in-progress, OOO/OOS, deep-cleans today. Any "
     "defects → `@HANDOFF: Engineering`. Any amenity issues → `@HANDOFF: F&B`."),
    ("Housekeeping Sup",
     "HK floor — live board",
     "Live rooms-board update: cleaned, inspected, vacant-ready, in-progress. "
     "Priority turns (VIP/early arrival). Defects flagged."),
    ("D F&B",
     "F&B — daily pulse",
     "Covers vs forecast for the next daypart. Banquets today. Cost run-rate. "
     "If you need any guest-side coordination → `@HANDOFF: Rooms`."),
    ("Exec Chef",
     "Kitchen — line + allergens",
     "Prep status, 86'd items, allergen-flagged in-house guests, today's "
     "specials. Note any supplier issue."),
    ("Restaurant Mgr",
     "Restaurant — pre-service brief",
     "Pre-service: covers booked, VIPs/special occasions, staffing, today's "
     "specials. Concise — one screen."),
    ("DOSM",
     "Commercial — demand pulse",
     "Pickup pace, channel mix, RFP pipeline. Any soft-period flag → propose "
     "a tactical campaign and brief Revenue Mgr."),
    ("Revenue Mgr",
     "Revenue — rate posture",
     "BAR levels for the next 14 days + restrictions. Pickup last 24h. "
     "Comp-set position. One specific rate move if warranted."),
    ("DOF",
     "Finance — daily pulse",
     "Yesterday's revenue (rooms, F&B, other) vs forecast/budget. AR ageing. "
     "Cash position. Any billing issue → resolution proposed."),
    ("HR Director",
     "People — staffing today",
     "Roster vs forecast by department. Open positions, time-to-fill, training "
     "due, welfare flags. If under-staffed → `@HANDOFF: <Department>` to "
     "coordinate."),
    ("Chief Engineer",
     "Engineering — open WOs",
     "Open work orders by priority. OOO/OOS rooms with cause + ETA. BMS / "
     "life-safety status. Today's PM schedule. Note any guest-side ETA → "
     "`@HANDOFF: Rooms`."),
    ("Security Mgr",
     "Security — incident pulse",
     "Yesterday's incidents, today's VIPs needing coverage, drills/audits "
     "scheduled, CCTV/access health. Any material → escalate to GM."),
    ("Spa Mgr",
     "Spa — today's bookings",
     "Bookings vs capacity per therapist, no-shows, package uptake, gym/pool "
     "utilisation. Flag a roster gap if any."),
]


def _today_str() -> str:
    return datetime.date.today().isoformat()


def _agent_dept(agent_name: str) -> str:
    # Reverse lookup from staff.DEPT_AGENT
    for dept, name in staff.DEPT_AGENT.items():
        if name == agent_name:
            return dept
    # Common defaults
    mapping = {
        "Front Office Mgr": staff.DEPT_ROOMS,
        "Reservations Mgr": staff.DEPT_ROOMS,
        "Concierge": staff.DEPT_ROOMS,
        "Executive Housekeeper": staff.DEPT_ROOMS,
        "Housekeeping Sup": staff.DEPT_ROOMS,
        "Exec Chef": staff.DEPT_FNB,
        "Restaurant Mgr": staff.DEPT_FNB,
        "Revenue Mgr": staff.DEPT_COMMERCIAL,
    }
    return mapping.get(agent_name, staff.DEPT_MANAGEMENT)


async def seed_day(force: bool = False) -> dict[str, Any]:
    """Create today's standing issues. Idempotent: skips if a seed for today exists."""
    if not STAFF_PROJECT_ID:
        return {"ok": False, "error": "PAPERCLIP_STAFF_PROJECT_ID not set"}

    key = f"seed_day:{_today_str()}"
    if not force and store.kv_get(key):
        return {"ok": True, "skipped": True, "reason": "already seeded today"}

    created: list[dict[str, Any]] = []
    for agent_name, suffix, body in DAILY_SEEDS:
        dept = _agent_dept(agent_name)
        title = f"[{_today_str()}] {agent_name} — {suffix}"
        full_body = (
            f"**Date:** {_today_str()}\n"
            f"**Owner:** {agent_name}\n"
            f"**Channel:** telegram_staff\n\n"
            f"---\n\n{body}"
        )
        try:
            issue = await pc.create_issue(
                project_id=STAFF_PROJECT_ID,
                title=title,
                body=full_body,
                assignee_slug=agent_name,
                channel="telegram_staff",
                to=str(staff.admin_user_id() or "0"),
                extra_metadata={"seeded": True, "date": _today_str(), "agent_name": agent_name},
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
            created.append({"agent": agent_name, "issue_id": issue_id})
            log.info("seeded %s: %s", agent_name, issue_id)
        except Exception as exc:
            log.warning("seed for %s failed: %s", agent_name, exc)
            created.append({"agent": agent_name, "error": str(exc)[:200]})

    store.kv_set(key, _today_str())
    return {"ok": True, "created": created, "count": len(created)}
