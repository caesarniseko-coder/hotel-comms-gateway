"""Background watchdog — finds in-progress issues where the agent has gone
silent and re-wakes the assignee. Keeps autonomous ops from quietly stalling.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time

import paperclip_client as pc
import store

log = logging.getLogger("watchdog")

WATCHDOG_INTERVAL_SECONDS = int(os.environ.get("WATCHDOG_INTERVAL_SECONDS", "60"))
STALL_THRESHOLD_SECONDS = int(os.environ.get("WATCHDOG_STALL_SECONDS", "90"))


def _parse_iso(s: str) -> float:
    if not s:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


async def _scan_once() -> int:
    """Wake any in-progress issues that haven't been replied to recently."""
    now = time.time()
    woken = 0
    # Walk all guest topic threads still marked open
    threads = store.list_open_topic_threads()
    name_map = await pc._agents_by_name()
    id_to_name = {v: k for k, v in name_map.items()}
    for t in threads:
        issue_id = t["issue_id"]
        try:
            issue = await pc.get_issue(issue_id)
        except Exception:
            continue
        status = (issue.get("status") or "").lower()
        if status in ("done", "cancelled", "closed"):
            store.close_topic_thread(issue_id)
            continue
        # Did the agent reply recently?
        updated = _parse_iso(issue.get("updatedAt") or "")
        if updated and (now - updated) < STALL_THRESHOLD_SECONDS:
            continue
        # Find assignee and wake them
        aid = issue.get("assigneeAgentId")
        if not aid:
            continue
        agent_name = id_to_name.get(aid)
        if not agent_name:
            continue
        try:
            await pc.wake_agent(agent_name, reason="watchdog")
            woken += 1
            log.info("watchdog woke %s on %s (stalled %ds)", agent_name, issue_id[:8], int(now - updated))
        except Exception as exc:
            log.warning("watchdog wake %s failed: %s", agent_name, exc)
    return woken


async def run_loop() -> None:
    log.info("watchdog starting (interval=%ss, stall=%ss)",
             WATCHDOG_INTERVAL_SECONDS, STALL_THRESHOLD_SECONDS)
    while True:
        try:
            n = await _scan_once()
            if n:
                log.info("watchdog re-woke %d stalled agent(s)", n)
        except Exception as exc:
            log.exception("watchdog tick failed: %s", exc)
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
