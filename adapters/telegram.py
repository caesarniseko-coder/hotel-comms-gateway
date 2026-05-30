"""Telegram channel adapter — inbound parser + outbound send."""
from __future__ import annotations

import os
import socket
from typing import Any, Optional
import httpx

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_SECRET = os.environ.get("TG_SECRET", "")
# HF Spaces blocks egress to api.telegram.org — route through a CF Worker relay.
TG_RELAY_URL = os.environ.get("TG_RELAY_URL", "").rstrip("/")
TG_RELAY_SECRET = os.environ.get("TG_RELAY_SECRET", "")
_API_BASE = TG_RELAY_URL or "https://api.telegram.org"
TG_API = f"{_API_BASE}/bot{TG_BOT_TOKEN}"

_RELAY_HEADERS: dict[str, str] = {"X-Relay-Secret": TG_RELAY_SECRET} if TG_RELAY_SECRET else {}

_TIMEOUT = httpx.Timeout(connect=20.0, read=20.0, write=10.0, pool=15.0)


def parse_update(update: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an incoming Telegram update. Returns rich info, or None to skip."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    text = msg.get("text") or msg.get("caption")
    if not text:
        return None
    chat = msg.get("chat", {}) or {}
    sender = msg.get("from", {}) or {}
    name = (
        sender.get("first_name", "")
        + ((" " + sender.get("last_name", "")) if sender.get("last_name") else "")
    ).strip() or sender.get("username") or f"tg:{sender.get('id')}"
    return {
        "sender_id": str(sender.get("id") or chat.get("id")),
        "sender_name": name,
        "sender_username": sender.get("username"),
        "from": sender,
        "chat": chat,
        "chat_id": str(chat.get("id")),
        "chat_type": chat.get("type", "private"),
        "chat_title": chat.get("title"),
        "text": text,
        "to": str(chat.get("id")),
        "update_id": str(update.get("update_id", "")),
        "message_id": msg.get("message_id"),
        "message_thread_id": msg.get("message_thread_id"),
        "is_command": text.startswith("/"),
        "entities": msg.get("entities") or [],
        "raw": msg,
    }


async def send(
    *,
    to: str,
    text: str,
    reply_to_message_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
    parse_mode: str = "",
) -> dict[str, Any]:
    if not TG_BOT_TOKEN:
        return {"skipped": True}
    payload: dict[str, Any] = {
        "chat_id": to,
        "text": text[:4096],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_RELAY_HEADERS) as client:
        r = await client.post(f"{TG_API}/sendMessage", json=payload)
        if r.status_code == 200:
            return r.json()

        # Surface Telegram's error body
        body = ""
        try:
            body = r.text
        except Exception:
            pass

        # Any 400 with parse_mode set → retry plain
        if r.status_code == 400 and "parse_mode" in payload:
            payload.pop("parse_mode", None)
            r2 = await client.post(f"{TG_API}/sendMessage", json=payload)
            if r2.status_code == 200:
                return r2.json()
            raise RuntimeError(
                f"telegram sendMessage failed (after plaintext retry): {r2.status_code} {r2.text[:400]}"
            )

        raise RuntimeError(
            f"telegram sendMessage failed: {r.status_code} {body[:400]}"
        )


async def get_me() -> dict[str, Any]:
    if not TG_BOT_TOKEN:
        return {}
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_RELAY_HEADERS) as client:
        r = await client.get(f"{TG_API}/getMe")
        r.raise_for_status()
        return r.json().get("result", {})
