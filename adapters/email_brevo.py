"""Email channel adapter via Brevo — inbound parsed-email webhook + transactional send."""
from __future__ import annotations

import os
from typing import Any
import httpx

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_INBOUND_SECRET = os.environ.get("BREVO_INBOUND_SECRET", "")
BREVO_FROM_EMAIL = os.environ.get("BREVO_FROM_EMAIL", "hotel@grand-hotel.example")
BREVO_FROM_NAME = os.environ.get("BREVO_FROM_NAME", "Grand Hotel")


def parse_inbound(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a Brevo inbound-email webhook. Return {sender_id, sender_name, text, to, subject}."""
    items = payload.get("items") or [payload]
    if not items:
        return None
    item = items[0]
    from_addr = (item.get("From") or {}).get("Address") or item.get("from")
    if not from_addr:
        return None
    text = item.get("RawTextBody") or item.get("text") or item.get("RawHtmlBody") or ""
    subject = item.get("Subject") or item.get("subject") or ""
    to_addr = ((item.get("To") or [{}])[0]).get("Address") or item.get("to") or ""
    return {
        "sender_id": from_addr.lower(),
        "sender_name": (item.get("From") or {}).get("Name") or from_addr,
        "text": text.strip(),
        "subject": subject,
        "to": from_addr,
        "reply_to": to_addr,
        "message_id": item.get("MessageId") or "",
    }


async def send(*, to: str, text: str, subject: str = "Re: Your message") -> None:
    if not BREVO_API_KEY:
        return
    payload = {
        "sender": {"email": BREVO_FROM_EMAIL, "name": BREVO_FROM_NAME},
        "to": [{"email": to}],
        "subject": subject[:200],
        "textContent": text,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
