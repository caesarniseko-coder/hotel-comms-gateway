"""Outbound poller — watches Paperclip issues created by this gateway for new agent
comments, then relays them back to the originating channel.

Avoids needing a Paperclip outbound-webhook config.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable

import paperclip_client as pc
import store

log = logging.getLogger("poller")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
SESSION_TTL_DAYS = int(os.environ.get("THREAD_TTL_DAYS", "14"))

_META_RE = re.compile(r"<!--\s*comms-meta:\s*(\{.*?\})\s*-->", re.DOTALL)

# (channel, sender_id) → last_seen_comment_ms
_high_water: dict[tuple[str, str], int] = {}


def _extract_meta(text: str) -> dict[str, Any]:
    if not text:
        return {}
    m = _META_RE.search(text)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def _strip_meta(text: str) -> str:
    return _META_RE.sub("", text or "").strip()


def _is_agent_comment(comment: dict[str, Any]) -> bool:
    kind = (
        comment.get("authorKind")
        or comment.get("author_kind")
        or comment.get("authorType")
        or ""
    ).lower()
    if kind in ("agent", "assistant", "ai"):
        return True
    # Some Paperclip responses use a structured `author` object
    author = comment.get("author") or {}
    if (author.get("kind") or "").lower() in ("agent", "assistant", "ai"):
        return True
    if author.get("agentId") or author.get("agent_id"):
        return True
    return False


def _ts(c: dict[str, Any]) -> int:
    raw = c.get("createdAt") or c.get("created_at") or c.get("timestamp") or ""
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw:
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return 0
    return 0


async def _poll_once(
    send_to_channel: Callable[[str, str, str], Awaitable[None]],
    send_workspace: Callable[[dict, str], Awaitable[None]] | None = None,
) -> int:
    """Walk tracked threads + workspace issues, dispatch new agent comments."""
    dispatched = 0

    # 1) Guest-channel threads (existing behavior)
    rows = store.list_thread_map(updated_since_days=SESSION_TTL_DAYS)
    for channel, sender_id, issue_id in rows:
        try:
            issue = await pc.get_issue(issue_id)
        except Exception as exc:
            log.warning("poller: get_issue %s failed (%s)", issue_id, exc)
            continue
        meta = issue.get("metadata") or _extract_meta(issue.get("description") or "")
        if not meta.get("channel") or not meta.get("to"):
            continue
        comments = issue.get("comments") or []
        if not comments:
            comments = await _fetch_comments(issue_id)
        key = ("guest", channel, sender_id)
        watermark = _high_water.get(key, 0)
        new_high = watermark
        for c in comments:
            ts = _ts(c)
            if ts <= watermark or not _is_agent_comment(c):
                continue
            body = _strip_meta(c.get("body") or c.get("content") or "")
            if not body:
                continue
            try:
                await send_to_channel(meta["channel"], meta["to"], body)
                dispatched += 1
            except Exception as exc:
                log.warning("poller: dispatch failed for %s/%s: %s", channel, sender_id, exc)
                continue
            if ts > new_high:
                new_high = ts
        _high_water[key] = new_high

    # 2) Workspace issues (staff-originated)
    if send_workspace is not None:
        origins = store.list_issue_origins(since_days=SESSION_TTL_DAYS)
        for o in origins:
            if o["origin_kind"] not in ("staff_report", "staff_dm", "staff_group", "ask"):
                continue
            issue_id = o["issue_id"]
            try:
                issue = await pc.get_issue(issue_id)
            except Exception as exc:
                log.warning("poller: get_issue %s failed (%s)", issue_id, exc)
                continue
            comments = issue.get("comments") or []
            if not comments:
                comments = await _fetch_comments(issue_id)
            key = ("ws", issue_id)
            watermark = _high_water.get(key, 0)
            new_high = watermark
            for c in comments:
                ts = _ts(c)
                if ts <= watermark or not _is_agent_comment(c):
                    continue
                body = _strip_meta(c.get("body") or c.get("content") or "")
                if not body:
                    continue
                try:
                    await send_workspace(o, body)
                    dispatched += 1
                except Exception as exc:
                    log.warning("poller: workspace dispatch failed for issue %s: %s", issue_id, exc)
                    continue
                if ts > new_high:
                    new_high = ts
            _high_water[key] = new_high

    return dispatched


async def _fetch_comments(issue_id: str) -> list[dict[str, Any]]:
    try:
        data = await pc._request("GET", f"/api/issues/{issue_id}/comments")
        return data if isinstance(data, list) else data.get("items", [])
    except Exception as exc:
        log.warning("poller: list comments %s failed (%s)", issue_id, exc)
        return []


async def run_loop(
    send_to_channel: Callable[[str, str, str], Awaitable[None]],
    send_workspace: Callable[[dict, str], Awaitable[None]] | None = None,
) -> None:
    log.info("poller starting (interval=%ss)", POLL_INTERVAL_SECONDS)
    while True:
        start = time.time()
        try:
            n = await _poll_once(send_to_channel, send_workspace=send_workspace)
            if n:
                log.info("poller dispatched %d comments", n)
        except Exception as exc:
            log.exception("poller: tick failed: %s", exc)
        elapsed = time.time() - start
        await asyncio.sleep(max(1.0, POLL_INTERVAL_SECONDS - elapsed))
