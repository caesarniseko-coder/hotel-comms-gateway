"""Structured memory: per-guest + per-room state assembled into a compact
context block the agent reads on every comment.
"""
from __future__ import annotations

import datetime
import json
from typing import Optional

import store


def _ago(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    now = datetime.datetime.utcnow().timestamp()
    delta = max(0, int(now - ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def derive_memory_block(
    *,
    tg_user_id: str,
    room_number: Optional[str],
    guest_name: Optional[str],
    sentiment_info: Optional[dict] = None,
    follow_up_index: int = 0,
) -> str:
    """Return a 3-6 line memory snapshot for the agent. Empty if no useful data."""
    lines: list[str] = []

    guest = store.get_guest_memory(tg_user_id)
    if guest:
        msg_count = guest.get("message_count", 0)
        stays = guest.get("stays_count", 0)
        sent_avg = guest.get("sentiment_avg")
        last_contact = guest.get("last_contact")
        vip = guest.get("vip_flag", 0)
        prefs_raw = guest.get("preferences")
        prefs = []
        if prefs_raw:
            try:
                prefs = json.loads(prefs_raw)
            except Exception:
                prefs = []

        guest_line_parts = []
        if stays > 0:
            guest_line_parts.append(f"returning guest ({stays} prior stays)")
        if msg_count > 1:
            guest_line_parts.append(f"{msg_count} messages this stay")
        if sent_avg is not None and sent_avg > 1.5:
            guest_line_parts.append(f"avg sentiment score {sent_avg:.1f} (escalating)")
        if vip:
            guest_line_parts.append("FLAGGED VIP")
        if last_contact and msg_count > 1:
            guest_line_parts.append(f"last contact {_ago(last_contact)}")
        if guest_line_parts:
            lines.append(f"Memory — Guest: {'; '.join(guest_line_parts)}.")
        if prefs:
            lines.append(f"Known preferences: {', '.join(prefs)}.")

    if room_number:
        room = store.get_room_memory(room_number)
        if room:
            room_line = []
            if room.get("open_wos"):
                room_line.append(f"{room['open_wos']} open work orders")
            if room.get("complaints_count"):
                room_line.append(f"{room['complaints_count']} complaint(s) on file")
            if room.get("last_service"):
                room_line.append(f"last service {_ago(room['last_service'])}")
            if room_line:
                lines.append(f"Memory — Room {room_number}: {'; '.join(room_line)}.")

    if follow_up_index >= 2:
        lines.append(
            f"⚠️ This is the guest's {follow_up_index + 1}th message in this thread — "
            f"they're checking again, do not stall."
        )
    if sentiment_info and sentiment_info.get("tone") in ("frustrated", "angry"):
        lines.append(
            f"⚠️ Sentiment is {sentiment_info['tone']} (score {sentiment_info.get('score')}). "
            f"Lead with a real apology, name an action, give a 60-sec ETA."
        )

    if not lines:
        return ""
    return "MEMORY CONTEXT:\n" + "\n".join(f"  • {l}" for l in lines)
