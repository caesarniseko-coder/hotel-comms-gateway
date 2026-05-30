"""Web chat adapter — REST endpoint + embeddable JS widget reads replies via polling."""
from __future__ import annotations

import os
import hmac
import hashlib
from typing import Any

WEB_CHAT_SECRET = os.environ.get("WEB_CHAT_SECRET", "")


def verify_token(session_id: str, token: str) -> bool:
    if not WEB_CHAT_SECRET:
        return True
    expected = hmac.new(
        WEB_CHAT_SECRET.encode(), session_id.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, token)


def mint_token(session_id: str) -> str:
    return hmac.new(
        WEB_CHAT_SECRET.encode(), session_id.encode(), hashlib.sha256
    ).hexdigest()


def parse_inbound(payload: dict[str, Any]) -> dict[str, Any] | None:
    session_id = (payload.get("sessionId") or "").strip()
    text = (payload.get("text") or "").strip()
    token = (payload.get("token") or "").strip()
    if not session_id or not text:
        return None
    if WEB_CHAT_SECRET and not verify_token(session_id, token):
        return None
    return {
        "sender_id": session_id,
        "sender_name": payload.get("displayName") or f"web:{session_id[:8]}",
        "text": text,
        "to": session_id,
    }


# Web replies are not pushed; the widget polls /api/web/poll. The store relays the
# Paperclip outbound webhook by appending to an in-memory queue keyed by sessionId
# — see app.py for the queue impl. `send` here is a no-op kept for symmetry.
async def send(*, to: str, text: str) -> None:
    return None
