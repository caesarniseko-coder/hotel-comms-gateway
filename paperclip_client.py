"""Thin async client for Paperclip board API (user-level token)."""
from __future__ import annotations

import os
from typing import Any
import httpx

PAPERCLIP_API_BASE = os.environ["PAPERCLIP_API_BASE"].rstrip("/")
PAPERCLIP_API_KEY = os.environ["PAPERCLIP_API_KEY"]
COMPANY_ID = os.environ["PAPERCLIP_COMPANY_ID"]


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {PAPERCLIP_API_KEY}",
        "Content-Type": "application/json",
    }


async def create_issue(
    *,
    project_id: str,
    title: str,
    body: str,
    assignee_slug: str,
    channel: str,
    to: str,
    priority: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "companyId": COMPANY_ID,
        "projectId": project_id,
        "title": title[:240],
        "body": body,
        "assigneeSlug": assignee_slug,
        "metadata": {"channel": channel, "to": to, **(extra_metadata or {})},
    }
    if priority:
        payload["priority"] = priority
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{PAPERCLIP_API_BASE}/api/v1/issues",
            headers=_headers(),
            json=payload,
        )
        r.raise_for_status()
        return r.json()


async def add_comment(
    *,
    issue_id: str,
    body: str,
    author_kind: str = "external",
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "body": body,
        "authorKind": author_kind,
        "metadata": extra_metadata or {},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{PAPERCLIP_API_BASE}/api/v1/issues/{issue_id}/comments",
            headers=_headers(),
            json=payload,
        )
        r.raise_for_status()
        return r.json()


async def get_issue(issue_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{PAPERCLIP_API_BASE}/api/v1/issues/{issue_id}",
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json()
