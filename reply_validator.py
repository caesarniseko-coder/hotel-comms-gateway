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
    issue_body: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return (corrected_reply, report). Report = which corrections were applied."""
    if not reply:
        return reply, {"changes": []}
    original = reply
    changes: list[str] = []

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

    return reply, {"changes": changes, "altered": reply != original}
