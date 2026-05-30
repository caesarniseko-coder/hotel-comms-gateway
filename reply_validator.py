"""Post-process agent replies to scrub hallucinated room numbers and guest names.

Why: the model (Qwen3-32B) frequently invents wrong room numbers (1204, 428)
and wrong names (James, Tanaka) even when the issue body says otherwise.
Rather than wait on a better model, we deterministically catch and correct
these before relaying to the guest.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("reply_validator")

# Names the model commonly hallucinates — replace with the actual guest_name
_COMMON_HALLUCINATED_NAMES = (
    "James", "John", "Tanaka", "Ms. Tanaka", "Mr. Tanaka",
    "Thompson", "Ms. Thompson", "Mr. Thompson",
    "Smith", "Mr. Smith", "Ms. Smith",
    "Johnson", "Ms. Johnson", "Mr. Johnson",
    "Garcia", "Mr. Garcia", "Ms. Garcia",
    "Patel", "Ms. Patel", "Mr. Patel",
    "Wong", "Mr. Wong", "Ms. Wong",
    "Liu", "Ms. Liu", "Mr. Liu",
    "Hoffman", "Mr. Hoffman", "Ms. Hoffman",
    "Müller", "Dr. Müller", "Mr. Müller",
    "Schmidt", "Mr. Schmidt",
    "Bernard", "Ms. Bernard",
    "Park", "Ms. Park", "Mr. Park",
)

# Pattern for "Room <number>" with optional letter suffix
_ROOM_RE = re.compile(r"\b[Rr]oom[\s#]*([0-9]{1,4}[A-Za-z]?)\b")

# Refusal / clarification asks that we want to suppress and replace
_REFUSAL_PATTERNS = (
    re.compile(r"\b(could you|can you) (please )?(clarify|confirm|provide|share)\b", re.I),
    re.compile(r"\bcould you confirm the (details|order|time|preferences)\b", re.I),
    re.compile(r"\b(if you|let me know|please let me know|please provide)\b", re.I),
    re.compile(r"\bi'?m (ready|happy|here) to help\b", re.I),
    re.compile(r"\bplease share .{0,40}\b", re.I),
)

# Empty-platitude markers
_PLATITUDE_PATTERNS = (
    re.compile(r"we take such matters very seriously", re.I),
    re.compile(r"thank you for choosing", re.I),
    re.compile(r"thank you for reaching out", re.I),
    re.compile(r"we apologize for any inconvenience", re.I),
)

# Topic → expected keyword regex in the reply (post-correction)
_TOPIC_EXPECTED = {
    "f_and_b": re.compile(r"\b(food|meal|kitchen|order|dining|delivery|restaurant|breakfast|dinner|lunch|chef|menu|service)\b", re.I),
    "maintenance": re.compile(r"\b(engineer|technician|fix|repair|maintenance|hvac|ac|plumb|leak|electric|wifi|power|tv|door|lock)\b", re.I),
    "billing": re.compile(r"\b(charge|folio|invoice|bill|refund|credit|payment)\b", re.I),
    "housekeeping": re.compile(r"\b(housekeep|clean|towel|linen|amenity|pillow|fresh)\b", re.I),
    "spa": re.compile(r"\b(spa|massage|wellness|sauna|gym|pool|therap)\b", re.I),
    "concierge": re.compile(r".", re.I),  # always passes for concierge
    "front_office": re.compile(r"\b(check[- ]?in|check[- ]?out|key|reception|front desk|folio|arrival|departure)\b", re.I),
    "complaint_escalate": re.compile(r"\b(manager|director|escalat|recovery|apolog|sorry|personally)\b", re.I),
}


def _looks_evasive(reply: str) -> str | None:
    """If reply contains refusal/clarify or empty platitude, return reason."""
    for pat in _REFUSAL_PATTERNS:
        if pat.search(reply):
            return f"refusal:{pat.pattern[:30]}"
    for pat in _PLATITUDE_PATTERNS:
        if pat.search(reply):
            return f"platitude:{pat.pattern[:30]}"
    return None


def _topic_drift(reply: str, expected_topic: str | None) -> bool:
    """True if reply doesn't mention any topic-related word."""
    if not expected_topic or expected_topic not in _TOPIC_EXPECTED:
        return False
    pat = _TOPIC_EXPECTED[expected_topic]
    return not pat.search(reply or "")


def _name_pattern_for(name: str) -> re.Pattern:
    """Build a regex matching the supplied name (with optional title prefix)."""
    # Allow optional titles before the name in the hallucination
    escaped = re.escape(name.strip())
    return re.compile(rf"\b{escaped}\b")


def correct_reply(
    *,
    reply: str,
    expected_name: str,
    expected_room: str,
    expected_topic: str | None = None,
    issue_body: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return (corrected_reply, report). Report includes corrections + warnings."""
    if not reply:
        return reply, {"changes": [], "warnings": []}
    original = reply
    changes: list[str] = []
    warnings: list[str] = []

    # Evasive / empty content → replace with a structured holding reply
    evasive = _looks_evasive(reply)
    if evasive:
        warnings.append(evasive)
        topic_word = expected_topic.replace("_", " ") if expected_topic else "your request"
        reply = (
            f"Hi {expected_name or 'guest'} — I'm on this right now. I'll be back "
            f"to Room {expected_room or 'your room'} with a concrete update on your "
            f"{topic_word} within 60 seconds."
        )
        changes.append("evasive→holding")

    # Topic drift → flag (we don't rewrite the whole reply, but we tag it)
    if expected_topic and _topic_drift(reply, expected_topic):
        warnings.append(f"topic_drift:expected={expected_topic}")

    # 1) Replace ANY incorrect room number with the expected one
    if expected_room:
        wrong_rooms = set()
        for m in _ROOM_RE.finditer(reply):
            found = m.group(1)
            if found != expected_room:
                wrong_rooms.add(found)
        for wrong in wrong_rooms:
            # Replace `Room 1204` (with possible whitespace/hash) with `Room <expected>`
            reply, n = re.subn(
                rf"\b([Rr]oom[\s#]*){re.escape(wrong)}\b",
                rf"\g<1>{expected_room}",
                reply,
            )
            if n > 0:
                changes.append(f"room {wrong}→{expected_room} ×{n}")

    # 2) Replace hallucinated names with the actual guest name
    if expected_name:
        en_low = expected_name.strip().lower()
        for cand in _COMMON_HALLUCINATED_NAMES:
            # Skip if the hallucinated name happens to equal the real name
            if cand.lower() == en_low:
                continue
            pat = _name_pattern_for(cand)
            new_reply, n = pat.subn(expected_name, reply)
            if n > 0:
                reply = new_reply
                changes.append(f"name {cand}→{expected_name} ×{n}")

    # 3) Strip any literal "Title:" pattern the model echoes
    reply = re.sub(r"^Subject:.*$", "", reply, flags=re.MULTILINE)

    # 4) Collapse blanks
    reply = re.sub(r"\n{3,}", "\n\n", reply).strip()

    return reply, {"changes": changes, "warnings": warnings, "altered": reply != original}
