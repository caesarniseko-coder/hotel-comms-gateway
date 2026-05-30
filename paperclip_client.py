"""Thin async client for Paperclip board API.

Uses session-cookie auth (paperclip_auth.py). Resolves agent slugs → UUIDs at first
use and caches the map.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any
import httpx

import paperclip_auth

PAPERCLIP_API_BASE = os.environ["PAPERCLIP_API_BASE"].rstrip("/")
COMPANY_ID = os.environ["PAPERCLIP_COMPANY_ID"]


class _AgentCache:
    def __init__(self) -> None:
        self.map: dict[str, str] = {}
        self.lock = asyncio.Lock()


_agents = _AgentCache()


async def _auth_headers() -> dict[str, str]:
    h = await paperclip_auth.cookie_header()
    h["Content-Type"] = "application/json"
    return h


async def _request(
    method: str,
    path: str,
    *,
    json_body: Any | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{PAPERCLIP_API_BASE}{path}"
    for attempt in range(2):
        headers = await _auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.request(method, url, headers=headers, json=json_body, params=params)
        if r.status_code == 401 and attempt == 0:
            await paperclip_auth.invalidate()
            continue
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"paperclip {method} {path} → {r.status_code}: {r.text[:500]}",
                request=r.request,
                response=r,
            )
        return r.json() if r.content else {}
    raise RuntimeError(f"paperclip {method} {path} retries exhausted")


async def _agents_by_name() -> dict[str, str]:
    """Map agent.name → agent.id."""
    async with _agents.lock:
        if _agents.map:
            return _agents.map
        data = await _request("GET", f"/api/companies/{COMPANY_ID}/agents")
        items = data if isinstance(data, list) else data.get("items") or data.get("agents") or []
        _agents.map = {a["name"]: a["id"] for a in items if a.get("name") and a.get("id")}
        return _agents.map


async def slug_to_id(slug: str) -> str:
    """Despite the legacy parameter name, this looks up by Paperclip agent.name."""
    m = await _agents_by_name()
    aid = m.get(slug)
    if not aid:
        async with _agents.lock:
            _agents.map = {}
        m = await _agents_by_name()
        aid = m.get(slug)
    if not aid:
        raise KeyError(f"unknown agent slug: {slug}")
    return aid


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
    assignee_id = await slug_to_id(assignee_slug)
    description = body
    if extra_metadata or channel or to:
        meta = {"channel": channel, "to": to, **(extra_metadata or {})}
        description += "\n\n<!-- comms-meta: " + json.dumps(meta) + " -->"
    payload: dict[str, Any] = {
        "projectId": project_id,
        "title": title[:240],
        "description": description,
        "assigneeAgentId": assignee_id,
    }
    if priority:
        payload["priority"] = priority
    return await _request("POST", f"/api/companies/{COMPANY_ID}/issues", json_body=payload)


async def add_comment(
    *,
    issue_id: str,
    body: str,
    author_kind: str = "external",
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Paperclip's addComment takes {body, reopen?, interrupt?}. We tuck metadata
    # into an HTML comment in the body so the outbound webhook can read it back.
    text = body
    if extra_metadata:
        text += "\n\n<!-- comms-meta: " + json.dumps(extra_metadata) + " -->"
    return await _request(
        "POST",
        f"/api/issues/{issue_id}/comments",
        json_body={"body": text},
    )


async def get_issue(issue_id: str) -> dict[str, Any]:
    return await _request("GET", f"/api/issues/{issue_id}")


async def list_issues(
    *,
    assignee_agent_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {"limit": str(limit)}
    if assignee_agent_id:
        params["assigneeAgentId"] = assignee_agent_id
    if project_id:
        params["projectId"] = project_id
    if status:
        params["status"] = status
    data = await _request("GET", f"/api/companies/{COMPANY_ID}/issues", params=params)
    return data if isinstance(data, list) else data.get("items") or []


async def list_issues_by_agent_name(name: str, **kwargs) -> list[dict[str, Any]]:
    aid = await slug_to_id(name)
    return await list_issues(assignee_agent_id=aid, **kwargs)


async def update_issue(issue_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    return await _request("PATCH", f"/api/issues/{issue_id}", json_body=fields)


async def set_issue_status(issue_id: str, status: str) -> dict[str, Any]:
    return await update_issue(issue_id, {"status": status})


async def reassign_issue(issue_id: str, agent_name: str) -> dict[str, Any]:
    aid = await slug_to_id(agent_name)
    return await update_issue(issue_id, {"assigneeAgentId": aid})


async def wake_agent(agent_name: str, reason: str = "external_trigger") -> dict[str, Any]:
    """POST /api/agents/{id}/wakeup so the agent runs NOW instead of waiting."""
    aid = await slug_to_id(agent_name)
    return await _request(
        "POST",
        f"/api/agents/{aid}/wakeup",
        params={"companyId": COMPANY_ID},
        json_body={"reason": reason},
    )


# ---- project lookup ---------------------------------------------------------

_projects_cache: dict[str, str] = {}
_projects_lock = asyncio.Lock()


async def project_id_by_name(name: str) -> str | None:
    """Map project name (case-insensitive) → id. Cached for the lifetime of the process."""
    async with _projects_lock:
        if not _projects_cache:
            data = await _request("GET", f"/api/companies/{COMPANY_ID}/projects")
            items = data if isinstance(data, list) else data.get("items", [])
            for p in items:
                if p.get("name") and p.get("id"):
                    _projects_cache[p["name"].lower()] = p["id"]
    return _projects_cache.get(name.lower())
