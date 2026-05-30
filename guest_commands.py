"""Slash-command handlers for the guest-facing Telegram bot.

Guests open `t.me/<guest-bot>` and use these commands to claim a room and
interact with the hotel team.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import json

import guest_profile
import store

log = logging.getLogger("guest_commands")

# Room numbers: 2-4 digits, e.g. "408", "1203", "12A"
_ROOM_RE = re.compile(r"^[0-9]{1,4}[A-Za-z]?$")


def _split_command(text: str) -> tuple[str, str]:
    head, _, rest = text.strip().partition(" ")
    cmd = head.lstrip("/").split("@", 1)[0].lower()
    return cmd, rest.strip()


def _display(msg_from: dict) -> str:
    name = (msg_from.get("first_name") or "").strip()
    if msg_from.get("last_name"):
        name = (name + " " + msg_from["last_name"]).strip()
    return name or msg_from.get("username") or f"tg:{msg_from.get('id')}"


async def handle_start(tg_user_id: str, msg_from: dict) -> str:
    name = _display(msg_from)
    existing = store.get_room_for_guest(tg_user_id)
    if existing:
        return (
            f"👋 Welcome back, {name}!\n\n"
            f"You're checked into Room *{existing['room_number']}*. "
            f"Send any message and our concierge team will help — wifi password, "
            f"restaurant reservations, room service, anything you need.\n\n"
            f"/checkout when you're leaving."
        )
    return (
        f"🏨 Welcome to *Grand Hotel*, {name}!\n\n"
        f"To get started, please claim your room with:\n"
        f"`/checkin <room number>`\n\n"
        f"Examples:\n"
        f"  `/checkin 408`\n"
        f"  `/checkin 1203 Tanaka`\n\n"
        f"After that, just send any message and our team will reply within "
        f"a minute — restaurant bookings, room service, recommendations, "
        f"maintenance issues, anything.\n\n"
        f"Type `/help` to see all guest commands."
    )


async def handle_help() -> str:
    return (
        "*Grand Hotel — Guest commands*\n\n"
        "• `/checkin <room>` — claim your room (e.g. `/checkin 408`)\n"
        "• `/checkin <room> <name>` — also set your name\n"
        "• `/checkout` — end your stay\n"
        "• `/room` — show your current room\n"
        "• `/help` — this list\n\n"
        "After /checkin, just send any message. Our concierge team replies "
        "within a minute. We're here 24/7."
    )


async def handle_checkin(tg_user_id: str, msg_from: dict, args: str) -> str:
    if not args:
        return (
            "Usage: `/checkin <room number>`\n"
            "e.g. `/checkin 408` or `/checkin 1203 Tanaka`"
        )
    parts = args.split(maxsplit=1)
    room = parts[0]
    if not _ROOM_RE.match(room):
        return (
            f"❓ `{room}` doesn't look like a room number. "
            f"Try e.g. `/checkin 408`."
        )
    name = parts[1].strip() if len(parts) > 1 else _display(msg_from)
    profile = guest_profile.synthesize(tg_user_id=tg_user_id, name=name, room=room)
    rec = store.check_in_guest(
        tg_user_id=tg_user_id,
        room_number=room,
        guest_name=name,
        tg_chat_id=tg_user_id,
        profile_json=json.dumps(profile),
    )
    # Bump stays counter for cross-stay memory
    try:
        store.remember_guest(tg_user_id=tg_user_id, last_room=room)
        store.bump_guest_stays(tg_user_id)
    except Exception:
        pass
    nights = profile["nights"]
    night_word = "night" if nights == 1 else "nights"
    loyalty = profile.get("loyalty_tier")
    loyalty_line = (
        f"As a *{loyalty}-tier* guest, " if loyalty and loyalty != "None" else ""
    )
    return (
        f"✅ Checked into *Room {rec['room_number']}* — welcome, {name}!\n\n"
        f"{loyalty_line}your booking `{profile['booking_ref']}` is confirmed for "
        f"{nights} {night_word} — check-out {profile['departure']}.\n"
        f"Rate: ${profile['rate_per_night_usd']}/night ({profile['payment_status']}).\n\n"
        f"Send any message now and our team will reply — restaurant bookings, "
        f"room service, recommendations, anything. We're around the clock.\n\n"
        f"_(/checkout when you're leaving.)_"
    )


async def handle_checkout(tg_user_id: str) -> str:
    rec = store.get_room_for_guest(tg_user_id)
    if not rec:
        return "You're not currently checked in. Use `/checkin <room>` to start."
    store.check_out_guest(tg_user_id)
    return (
        f"👋 Thank you for staying with us, {rec.get('guest_name') or 'guest'}. "
        f"You've checked out of Room *{rec['room_number']}*. We hope to see you again soon."
    )


async def handle_room(tg_user_id: str) -> str:
    rec = store.get_room_for_guest(tg_user_id)
    if not rec:
        return "You're not currently checked in. Use `/checkin <room>` to start."
    return (
        f"🛏 You're in *Room {rec['room_number']}* "
        f"({rec.get('guest_name') or 'guest'}). Send any message — our team replies "
        f"within a minute."
    )


async def dispatch(*, text: str, msg_from: dict, chat: dict) -> Optional[str]:
    cmd, args = _split_command(text)
    tg_user_id = str(msg_from.get("id"))
    if cmd == "start":
        return await handle_start(tg_user_id, msg_from)
    if cmd in ("help", "?"):
        return await handle_help()
    if cmd == "checkin":
        return await handle_checkin(tg_user_id, msg_from, args)
    if cmd == "checkout":
        return await handle_checkout(tg_user_id)
    if cmd == "room":
        return await handle_room(tg_user_id)
    return None
