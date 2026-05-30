"""Synthesize plausible guest-profile data on check-in.

The hotel-comms gateway doesn't talk to a real PMS yet, so we fabricate the
booking / payment / loyalty info the moment a guest checks in. The profile is
stable for that guest's stay (re-running for the same tg_user_id returns a
deterministic profile derived from their Telegram id).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import random
from typing import Any

# Deterministic per-user pools
SEGMENTS = ["Business", "Leisure", "Group", "Corporate"]
LOYALTY_TIERS = [
    ("Diamond", 0.10),
    ("Platinum", 0.20),
    ("Gold", 0.30),
    ("Silver", 0.20),
    ("None", 0.20),
]
PREFERENCES = [
    "Feather-free pillows", "High floor preferred", "Quiet side of building",
    "Extra towels in room", "Mediterranean breakfast", "Vegetarian meals",
    "Lactose-free", "Late riser — no morning calls", "Espresso machine in room",
    "Reusable water bottle", "Adapter for EU plugs",
]
PAYMENT_STATUSES = [
    ("Paid in full (credit card on file)", 0.55),
    ("Credit card pre-auth ${hold}", 0.25),
    ("Direct billing — corporate account", 0.15),
    ("Cash deposit on arrival ${hold}", 0.05),
]


def _seed_for(tg_user_id: str, name: str) -> int:
    h = hashlib.sha256((tg_user_id + ":" + (name or "")).encode()).digest()
    return int.from_bytes(h[:4], "big")


def _weighted(seq, rng: random.Random):
    keys = [k for k, _ in seq]
    weights = [w for _, w in seq]
    return rng.choices(keys, weights=weights, k=1)[0]


def synthesize(tg_user_id: str, name: str, room: str) -> dict[str, Any]:
    rng = random.Random(_seed_for(tg_user_id, name))
    today = datetime.date.today()
    nights = rng.choice([1, 1, 2, 2, 2, 3, 3, 4, 5, 7])
    departure = today + datetime.timedelta(days=nights)
    segment = rng.choice(SEGMENTS)
    loyalty = _weighted(LOYALTY_TIERS, rng)
    rate = rng.choice([195, 215, 235, 245, 265, 285, 320, 380, 425, 495])
    booking_ref = f"GH-{today.strftime('%Y%m%d')}-{rng.randint(1000,9999)}"
    pref_count = rng.choice([0, 1, 2, 2, 3])
    prefs = rng.sample(PREFERENCES, k=pref_count) if pref_count else []
    pay_template = _weighted(PAYMENT_STATUSES, rng)
    pay_status = pay_template.replace("${hold}", f"${rng.randint(8,40)*50}")
    prior_stays = rng.choice([0, 0, 0, 1, 2, 3, 5, 8, 12])

    return {
        "booking_ref": booking_ref,
        "room": room,
        "guest_name": name,
        "check_in": today.isoformat(),
        "departure": departure.isoformat(),
        "nights": nights,
        "segment": segment,
        "loyalty_tier": loyalty,
        "rate_per_night_usd": rate,
        "total_paid_usd": rate * nights,
        "payment_status": pay_status,
        "preferences": prefs,
        "prior_stays": prior_stays,
        "synthesized_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def format_card(profile: dict[str, Any]) -> str:
    """Human-readable card for admin mirror + agent context."""
    prefs = profile.get("preferences") or []
    prefs_line = ", ".join(prefs) if prefs else "—"
    loyalty = profile.get("loyalty_tier") or "—"
    return (
        f"🛏 *Check-in — Room {profile.get('room')}*\n"
        f"Guest: *{profile.get('guest_name')}*  ({loyalty} tier)\n"
        f"Booking: `{profile.get('booking_ref')}`  ({profile.get('segment')})\n"
        f"Stay: {profile.get('nights')} night(s) — check-out {profile.get('departure')}\n"
        f"Rate: ${profile.get('rate_per_night_usd')}/night  (paid: ${profile.get('total_paid_usd')})\n"
        f"Payment: {profile.get('payment_status')}\n"
        f"Preferences: {prefs_line}\n"
        f"Prior stays: {profile.get('prior_stays')}"
    )


def format_context(profile: dict[str, Any]) -> str:
    """One-paragraph context line for the issue body the Concierge reads."""
    prefs = profile.get("preferences") or []
    prefs_part = (" Preferences: " + ", ".join(prefs) + ".") if prefs else ""
    return (
        f"Guest profile — {profile.get('guest_name')} in Room {profile.get('room')}. "
        f"Booking {profile.get('booking_ref')} ({profile.get('segment')}), "
        f"{profile.get('loyalty_tier')} tier, {profile.get('prior_stays')} prior stays. "
        f"{profile.get('nights')} nights, check-out {profile.get('departure')}, "
        f"rate ${profile.get('rate_per_night_usd')}/night, {profile.get('payment_status')}."
        f"{prefs_part}"
    )
