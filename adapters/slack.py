"""Slack channel adapter — slash command + chat.postMessage. Staff side."""
from __future__ import annotations

import os
import hmac
import hashlib
import time
from typing import Any
import httpx

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")


def verify_signature(timestamp: str, body: bytes, signature: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > 60 * 5:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest("v0=" + digest, signature)


def parse_command(form: dict[str, str]) -> dict[str, Any] | None:
    """Parse `/ask <slug> <text>` form. Return {sender_id, sender_name, text, to, agent_slug}."""
    text = (form.get("text") or "").strip()
    if not text:
        return None
    parts = text.split(None, 1)
    agent_slug = parts[0].lower() if len(parts) > 1 else "gm"
    body = parts[1] if len(parts) > 1 else parts[0]
    return {
        "sender_id": form.get("user_id") or "",
        "sender_name": form.get("user_name") or "slack-user",
        "text": body,
        "to": form.get("channel_id") or "",
        "agent_slug": agent_slug,
        "trigger_id": form.get("trigger_id") or "",
    }


async def send(*, to: str, text: str, thread_ts: str | None = None) -> None:
    if not SLACK_BOT_TOKEN:
        return
    payload: dict[str, Any] = {"channel": to, "text": text[:3500]}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
        )
        r.raise_for_status()
