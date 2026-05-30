"""Paperclip session-login auth — keeps a session cookie alive for board writes."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional
import httpx

PAPERCLIP_API_BASE = os.environ["PAPERCLIP_API_BASE"].rstrip("/")
PAPERCLIP_EMAIL = os.environ.get("PAPERCLIP_EMAIL", "")
PAPERCLIP_PASSWORD = os.environ.get("PAPERCLIP_PASSWORD", "")

_COOKIE_NAME = "__Secure-paperclip-default.session_token"
_REFRESH_AFTER = 6 * 24 * 3600  # better-auth sessions last ~7d


class _SessionState:
    def __init__(self) -> None:
        self.token: Optional[str] = None
        self.minted_at: float = 0.0
        self.lock = asyncio.Lock()


_state = _SessionState()


async def _sign_in() -> str:
    if not PAPERCLIP_EMAIL or not PAPERCLIP_PASSWORD:
        raise RuntimeError("PAPERCLIP_EMAIL / PAPERCLIP_PASSWORD not configured")
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            f"{PAPERCLIP_API_BASE}/api/auth/sign-in/email",
            json={"email": PAPERCLIP_EMAIL, "password": PAPERCLIP_PASSWORD},
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        token = data.get("token")
        if not token:
            raise RuntimeError(f"sign-in returned no token: {data}")
        return token


async def get_token() -> str:
    """Return a session token, refreshing if stale."""
    async with _state.lock:
        if _state.token and (time.time() - _state.minted_at) < _REFRESH_AFTER:
            return _state.token
        token = await _sign_in()
        _state.token = token
        _state.minted_at = time.time()
        return token


async def cookie_header() -> dict[str, str]:
    """Return headers dict for an authenticated board request."""
    token = await get_token()
    return {
        "Cookie": f"{_COOKIE_NAME}={token}",
        "Origin": PAPERCLIP_API_BASE,
        "Referer": f"{PAPERCLIP_API_BASE}/",
    }


async def invalidate() -> None:
    async with _state.lock:
        _state.token = None
        _state.minted_at = 0.0
