"""Multi-bot Telegram adapter.

Supports two bots:
  • STAFF  — @kutchan_hotel_concierge_bot — staff workspace + admin commands
  • GUEST  — @kutchan_hotel_guests_bot   — per-room guest channel

Outbound goes through the Cloudflare Worker relay (TG_RELAY_URL) because HF
Spaces blocks api.telegram.org egress.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional
import httpx

log = logging.getLogger("telegram")

# Shared relay (works for any bot — the token is in the URL path)
TG_RELAY_URL = os.environ.get("TG_RELAY_URL", "").rstrip("/")
TG_RELAY_SECRET = os.environ.get("TG_RELAY_SECRET", "")
_API_ROOT = TG_RELAY_URL or "https://api.telegram.org"
_RELAY_HEADERS: dict[str, str] = {"X-Relay-Secret": TG_RELAY_SECRET} if TG_RELAY_SECRET else {}
_TIMEOUT = httpx.Timeout(connect=20.0, read=20.0, write=10.0, pool=15.0)


class TgBot:
    def __init__(self, *, name: str, token_env: str, secret_env: str = "") -> None:
        self.name = name
        self._token_env = token_env
        self._secret_env = secret_env

    @property
    def token(self) -> str:
        return os.environ.get(self._token_env, "")

    @property
    def secret(self) -> str:
        return os.environ.get(self._secret_env, "")

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def api(self) -> str:
        api_root = (os.environ.get("TG_RELAY_URL") or "https://api.telegram.org").rstrip("/")
        return f"{api_root}/bot{self.token}"

    @staticmethod
    def _relay_headers() -> dict[str, str]:
        s = os.environ.get("TG_RELAY_SECRET", "")
        return {"X-Relay-Secret": s} if s else {}

    async def send(
        self,
        *,
        to: str,
        text: str,
        reply_to_message_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        parse_mode: str = "",
    ) -> dict[str, Any]:
        if not self.configured:
            return {"skipped": True, "bot": self.name, "reason": "not configured"}
        payload: dict[str, Any] = {
            "chat_id": to,
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=self._relay_headers()) as client:
            r = await client.post(f"{self.api}/sendMessage", json=payload)
            if r.status_code == 200:
                return r.json()
            body = r.text or ""
            if r.status_code == 400 and "parse_mode" in payload:
                payload.pop("parse_mode", None)
                r2 = await client.post(f"{self.api}/sendMessage", json=payload)
                if r2.status_code == 200:
                    return r2.json()
                raise RuntimeError(
                    f"[{self.name}] sendMessage failed (after plaintext retry): "
                    f"{r2.status_code} {r2.text[:400]}"
                )
            raise RuntimeError(
                f"[{self.name}] sendMessage failed: {r.status_code} {body[:400]}"
            )

    async def get_me(self) -> dict[str, Any]:
        if not self.configured:
            return {}
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=self._relay_headers()) as client:
            r = await client.get(f"{self.api}/getMe")
            r.raise_for_status()
            return r.json().get("result", {})

    async def set_webhook(
        self,
        *,
        url: str,
        secret_token: str,
        allowed_updates: list[str] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=self._relay_headers()) as client:
            r = await client.post(
                f"{self.api}/setWebhook",
                json={
                    "url": url,
                    "secret_token": secret_token,
                    "allowed_updates": allowed_updates or ["message"],
                    "drop_pending_updates": False,
                },
            )
            r.raise_for_status()
            return r.json()


# Singleton instances — env vars are read lazily on each call.
STAFF = TgBot(name="staff", token_env="TG_BOT_TOKEN", secret_env="TG_SECRET")
GUEST = TgBot(name="guest", token_env="TG_GUEST_BOT_TOKEN", secret_env="TG_GUEST_SECRET")


# ---- update parsing (bot-agnostic) ------------------------------------------

def parse_update(update: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an inbound Telegram update into a normalised dict, or None to skip."""
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


# ---- back-compat module-level helpers (default to STAFF bot) ----------------

async def send(**kw: Any) -> dict[str, Any]:
    return await STAFF.send(**kw)


async def get_me() -> dict[str, Any]:
    return await STAFF.get_me()
