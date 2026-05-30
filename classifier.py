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

# Intent -> hotel agent slug
ROUTE = {
    "reservations": "reservations-mgr",
    "front_office": "front-office-mgr",
    "concierge": "concierge",
    "f_and_b": "restaurant-mgr",
    "housekeeping": "executive-housekeeper",
    "maintenance": "chief-engineer",
    "billing": "dof",
    "spa": "spa-mgr",
    "security": "security-mgr",
    "complaint_escalate": "gm",
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
    """Return (intent, assignee_slug, priority_or_None). Defaults to concierge on any failure."""
    if not GATEWAY_BASE_URL or not GATEWAY_API_KEY:
        log.info("classifier disabled (no gateway creds) — defaulting to concierge")
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
