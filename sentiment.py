"""Lightweight sentiment analysis + acknowledgement-tone picker.

No external model — keyword + pattern heuristics. Fast enough to run on every
inbound and pick a contextual "we're on it" reply while the real agent works.
"""
from __future__ import annotations

import re
from typing import Any

URGENCY_KEYWORDS = (
    "now", "asap", "immediately", "urgent", "still waiting", "where is",
    "where's", "how long", "quickly", "fast", "right now", "i need",
)
FRUSTRATION_KEYWORDS = (
    "ridiculous", "unacceptable", "terrible", "awful", "horrible", "rude",
    "annoyed", "frustrated", "complain", "complaint", "this is bad",
    "won't stay", "no service", "ignore", "ignoring",
)
ANGER_KEYWORDS = (
    "refund", "manager", "speak to", "supervisor", "useless",
    "disgusting", "worst", "scam", "fraud", "leaving", "checking out",
    "never coming back", "i'm done", "fed up",
)


def _normalised(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def score(text: str) -> dict[str, Any]:
    """Return {tone, score, signals}."""
    if not text:
        return {"tone": "calm", "score": 0, "signals": []}
    t = _normalised(text)
    signals: list[str] = []
    pts = 0
    for w in URGENCY_KEYWORDS:
        if w in t:
            pts += 1
            signals.append(f"urgency:{w}")
    for w in FRUSTRATION_KEYWORDS:
        if w in t:
            pts += 2
            signals.append(f"frustration:{w}")
    for w in ANGER_KEYWORDS:
        if w in t:
            pts += 3
            signals.append(f"anger:{w}")
    excl = text.count("!")
    if excl >= 1:
        pts += min(excl, 3)
        signals.append(f"exclam:{excl}")
    # All-caps short bursts
    upper = sum(1 for c in text if c.isupper())
    if len(text) > 4 and (upper / max(1, len(text))) > 0.4:
        pts += 1
        signals.append("ALL_CAPS")
    # Repetition: "where is", "still", "again"
    if any(p in t for p in ("again", "still", "?? ", "??")):
        pts += 1
        signals.append("repetition")
    if pts >= 6:
        tone = "angry"
    elif pts >= 3:
        tone = "frustrated"
    elif pts >= 1:
        tone = "urgent"
    else:
        tone = "calm"
    return {"tone": tone, "score": pts, "signals": signals}


def pick_ack(*, text: str, guest_name: str, room_number: str, follow_up_index: int) -> str:
    """Pick a contextual immediate-acknowledgement message for the guest."""
    s = score(text)
    name = guest_name or "—"
    room = room_number or "your room"
    fu = follow_up_index

    # Escalate based on combined sentiment + repetition
    if s["tone"] == "angry" or fu >= 3:
        return (
            f"I am so sorry, {name}. I hear you and this isn't acceptable. "
            f"I'm pulling our Duty Manager onto this right now — they will reach "
            f"you in Room {room} within 60 seconds with a real answer and a "
            f"resolution. Please bear with me one more moment. 🙏"
        )
    if s["tone"] == "frustrated" or fu == 2:
        return (
            f"Apologies, {name}. I hear the urgency — I'm on this personally "
            f"right now and I will be back to you within 60 seconds with an "
            f"exact ETA from our team. 🏃"
        )
    if s["tone"] == "urgent" or fu == 1:
        return (
            f"On it, {name} — checking with our team for you right now. "
            f"Back to you within 60 seconds with a real ETA. 🛎"
        )
    return (
        f"Got it, {name} — our team is on this. Back to you within a minute. 🛎"
    )


def should_escalate_to_gm(*, sentiment: dict, follow_up_index: int) -> bool:
    """Return True if this thread warrants pulling the GM in with high priority."""
    if follow_up_index >= 3:
        return True
    if sentiment.get("tone") == "angry":
        return True
    if sentiment.get("score", 0) >= 5:
        return True
    return False
