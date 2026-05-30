"""Telegram channel adapter — inbound webhook parser + sendMessage."""
from __future__ import annotations

import os
from typing import Any
import httpx

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_SECRET = os.environ.get("TG_SECRET", "")
TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


def parse_update(update: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a Telegram webhook update. Return {sender_id, sender_name, text, to} or None."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    text = msg.get("text") or msg.get("caption")
    if not text:
        return None
    chat = msg.get("chat", {})
    sender = msg.get("from", {})
    name = (
        sender.get("first_name", "")
        + ((" " + sender.get("last_name", "")) if sender.get("last_name") else "")
    ).strip() or sender.get("username") or f"tg:{sender.get('id')}"
    return {
        "sender_id": str(sender.get("id") or chat.get("id")),
        "sender_name": name,
        "text": text,
        "to": str(chat.get("id")),
        "update_id": str(update.get("update_id", "")),
    }


async def send(*, to: str, text: str) -> None:
    if not TG_BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": to, "text": text[:4096], "parse_mode": "HTML"},
        )
        r.raise_for_status()
