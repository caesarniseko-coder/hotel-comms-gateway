"""Paperclip session-login auth — keeps a signed session cookie alive for board writes."""
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
_REFRESH_AFTER = 6 * 24 * 3600


class _SessionState:
    def __init__(self) -> None:
        self.cookie_value: Optional[str] = None
        self.minted_at: float = 0.0
        self.lock = asyncio.Lock()


_state = _SessionState()


def _extract_cookie(set_cookie_headers: list[str]) -> Optional[str]:
    """Return the raw value of the session cookie (signed, url-encoded form)."""
    for header in set_cookie_headers:
        if header.startswith(_COOKIE_NAME + "="):
            first_pair = header.split(";", 1)[0]
            return first_pair.split("=", 1)[1]
    return None


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
        headers = r.headers.get_list("set-cookie")
        cookie_value = _extract_cookie(headers)
        if not cookie_value:
            raise RuntimeError(
                f"sign-in returned no session cookie. headers={headers!r} body={r.text[:300]}"
            )
        return cookie_value


async def get_cookie() -> str:
    async with _state.lock:
        if _state.cookie_value and (time.time() - _state.minted_at) < _REFRESH_AFTER:
            return _state.cookie_value
        cookie = await _sign_in()
        _state.cookie_value = cookie
        _state.minted_at = time.time()
        return cookie


async def cookie_header() -> dict[str, str]:
    cookie = await get_cookie()
    return {
        "Cookie": f"{_COOKIE_NAME}={cookie}",
        "Origin": PAPERCLIP_API_BASE,
        "Referer": f"{PAPERCLIP_API_BASE}/",
    }


async def invalidate() -> None:
    async with _state.lock:
        _state.cookie_value = None
        _state.minted_at = 0.0
