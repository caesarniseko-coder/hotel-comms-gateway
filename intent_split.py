"""Split a single guest message into multiple sub-requests with distinct intents.

Example:
  "I want milk and sweets and also a swimming suit and ice cream"
  → [
      (f_and_b, "milk and sweets"),
      (concierge, "a swimming suit"),
      (f_and_b, "ice cream"),
    ]

Then dedup by intent (one issue per intent in the same message).
"""
from __future__ import annotations

import re
from typing import Iterable

import agent_listener


# Splitters: "and also", "and", ",", "; ", ". " — but only if a topic shifts
_CLAUSE_RE = re.compile(r"\s*(?:,|;|\bplus\b|\band also\b|\band\b|\bas well as\b)\s+", re.I)


def _classify(clause: str) -> str | None:
    """Return intent slug for a clause, or None if no keyword match."""
    _, agent, _ = agent_listener.classify_by_keywords(clause)
    if not agent:
        return None
    # Map agent → intent slug (mirror of classifier.ROUTE)
    agent_to_intent = {
        "Restaurant Mgr": "f_and_b",
        "Exec Chef": "f_and_b",
        "Front Office Mgr": "front_office",
        "Reservations Mgr": "reservations",
        "Concierge": "concierge",
        "Chief Engineer": "maintenance",
        "Executive Housekeeper": "housekeeping",
        "Housekeeping Sup": "housekeeping",
        "DOF": "billing",
        "Spa Mgr": "spa",
        "Security Mgr": "security",
        "GM": "complaint_escalate",
    }
    return agent_to_intent.get(agent)


def split_message(text: str) -> list[tuple[str, str]]:
    """Return [(intent, clause_text), ...] — distinct intents only.

    If the message contains only one detectable intent, returns a single-element list.
    If no intent at all, returns []. Caller falls back to a single classification.
    """
    if not text or len(text) < 8:
        return []
    # Split on clause-level conjunctions
    raw_clauses = [c.strip() for c in _CLAUSE_RE.split(text) if c.strip()]
    if not raw_clauses:
        return []

    # Classify each clause
    intents_seen: dict[str, str] = {}  # intent → concatenated text
    for clause in raw_clauses:
        intent = _classify(clause)
        if intent:
            intents_seen[intent] = (intents_seen.get(intent, "") + " " + clause).strip()

    # If only 0 or 1 distinct intent, return as-is
    if len(intents_seen) <= 1:
        return [(i, t) for i, t in intents_seen.items()]

    # Multi-intent — return all
    return [(intent, text_part) for intent, text_part in intents_seen.items()]
