"""Intent classifier — one short call to qwen3:8b-fast via the self-hosted LLM gateway."""
from __future__ import annotations

import logging
import os
import re
import httpx

GATEWAY_BASE_URL = os.environ.get("GATEWAY_BASE_URL", "").rstrip("/")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY_CLASSIFIER", "")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "qwen3:8b-fast")

log = logging.getLogger("classifier")

LABELS = [
    "reservations",
    "front_office",
    "concierge",
    "f_and_b",
    "housekeeping",
    "maintenance",
    "billing",
    "spa",
    "security",
    "complaint_escalate",
]

# Intent -> hotel agent NAME (must match Paperclip's stored agent.name exactly).
# Paperclip's agent list API exposes `name` but not `urlKey`/slug, so we route by name.
ROUTE = {
    "reservations": "Reservations Mgr",
    "front_office": "Front Office Mgr",
    "concierge": "Concierge",
    "f_and_b": "Restaurant Mgr",
    "housekeeping": "Executive Housekeeper",
    "maintenance": "Chief Engineer",
    "billing": "DOF",
    "spa": "Spa Mgr",
    "security": "Security Mgr",
    "complaint_escalate": "GM",
}

PRIORITY = {"complaint_escalate": "high", "security": "high"}

_SYSTEM = (
    "You are an intent classifier for a hotel front desk. Read the guest message "
    "and reply with EXACTLY ONE label from this set, no punctuation, no explanation:\n"
    + "\n".join(f"- {label}" for label in LABELS)
    + "\n\nRules:\n"
    "- Pick `complaint_escalate` only for clearly serious complaints (threats, repeat issues, refund demands, legal mentions).\n"
    "- Pick `concierge` when ambiguous or for local recommendations / general help.\n"
    "- Pick `front_office` for check-in / check-out / room key / wifi / general front-desk questions.\n"
    "- Pick `maintenance` for in-room defects (AC, plumbing, lighting, TV, door, safe).\n"
    "- Pick `billing` for charge disputes, folio questions, refund (not threat-level).\n"
)

_LABEL_RE = re.compile(r"\b(" + "|".join(LABELS) + r")\b", re.I)


async def classify(text: str) -> tuple[str, str, str | None]:
    """Return (intent, assignee_slug, priority_or_None).

    Tries keyword classification first (free, deterministic). If no keyword
    matches, optionally calls the LLM classifier. Falls back to concierge.
    """
    import agent_listener
    kw_dept, kw_agent, kw_pri = agent_listener.classify_by_keywords(text)
    if kw_agent:
        # Map dept slug → intent label for consistency
        dept_to_intent = {
            "rooms": "front_office", "fnb": "f_and_b", "engineering": "maintenance",
            "commercial": "reservations", "management": "complaint_escalate",
            "security": "security", "spa": "spa", "hr": "housekeeping",
            "finance": "billing",
        }
        intent_label = dept_to_intent.get(kw_dept, "concierge")
        # Refine FnB routing: pick Restaurant Mgr (room service) for food requests,
        # Exec Chef only for menu/allergen/cooking questions.
        if intent_label == "f_and_b":
            t = (text or "").lower()
            if any(k in t for k in ("allergen", "allerg", "menu", "ingredient", "vegan", "halal", "kosher", "gluten")):
                kw_agent = "Exec Chef"
            else:
                kw_agent = "Restaurant Mgr"
        log.info("classifier (keyword) → intent=%s agent=%s pri=%s", intent_label, kw_agent, kw_pri)
        return intent_label, kw_agent, kw_pri

    if not GATEWAY_BASE_URL or not GATEWAY_API_KEY:
        log.info("classifier (no keyword match, no gateway) → concierge")
        return "concierge", ROUTE["concierge"], None
    body = {
        "model": CLASSIFIER_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": text.strip()[:2000]},
        ],
        "temperature": 0.0,
        "max_tokens": 16,
    }
    base = GATEWAY_BASE_URL
    path = "/chat/completions" if base.rstrip("/").endswith("/v1") else "/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{base}{path}",
                headers={
                    "Authorization": f"Bearer {GATEWAY_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as exc:
        log.warning("classifier call failed (%s) — defaulting to concierge", exc)
        return "concierge", ROUTE["concierge"], None

    m = _LABEL_RE.search(content.lower())
    intent = m.group(1).lower() if m else "concierge"
    return intent, ROUTE[intent], PRIORITY.get(intent)
