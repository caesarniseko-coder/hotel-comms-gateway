"""Hotel Comms Gateway — FastAPI app.

Bridges Telegram / Email / Slack / Web ↔ Paperclip ↔ Grand Hotel agents.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from adapters import telegram, email_brevo, slack, web
import classifier
import paperclip_client as pc
import store

GUEST_PROJECT_ID = os.environ["PAPERCLIP_GUEST_PROJECT_ID"]
STAFF_PROJECT_ID = os.environ.get("PAPERCLIP_STAFF_PROJECT_ID", GUEST_PROJECT_ID)
TG_SECRET = os.environ.get("TG_SECRET", "")
BREVO_INBOUND_SECRET = os.environ.get("BREVO_INBOUND_SECRET", "")

app = FastAPI(title="Hotel Comms Gateway")


@app.on_event("startup")
async def _startup() -> None:
    store.init()


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "hotel-comms-gateway"}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "ts": int(time.time())}


# ---------------------------------------------------------------------------
# Inbound: route a guest message to Paperclip
# ---------------------------------------------------------------------------

async def _route_guest_message(
    *,
    channel: str,
    sender_id: str,
    sender_name: str,
    text: str,
    to: str,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue_id = store.get_issue_id(channel, sender_id)
    if issue_id:
        await pc.add_comment(
            issue_id=issue_id,
            body=f"**{sender_name}** ({channel}):\n\n{text}",
            author_kind="external",
            extra_metadata={"sender": sender_name, "channel": channel},
        )
        return {"issue_id": issue_id, "created": False}

    intent, assignee, priority = await classifier.classify(text)
    title = f"[{channel}] {sender_name} — {text[:60]}"
    issue = await pc.create_issue(
        project_id=GUEST_PROJECT_ID,
        title=title,
        body=f"**Channel:** {channel}\n**From:** {sender_name}\n**Intent:** {intent}\n\n---\n\n{text}",
        assignee_slug=assignee,
        channel=channel,
        to=to,
        priority=priority,
        extra_metadata={"sender_name": sender_name, "intent": intent, **(extra_metadata or {})},
    )
    issue_id = str(issue.get("id") or issue.get("issueId") or "")
    if issue_id:
        store.set_issue_id(channel, sender_id, issue_id)
    return {"issue_id": issue_id, "created": True, "intent": intent, "assignee": assignee}


# --- Telegram --------------------------------------------------------------

@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    if TG_SECRET and x_telegram_bot_api_secret_token != TG_SECRET:
        raise HTTPException(status_code=401, detail="bad telegram secret")
    update = await request.json()
    parsed = telegram.parse_update(update)
    if not parsed:
        return JSONResponse({"ok": True, "skipped": True})
    if parsed.get("update_id") and store.seen(f"tg:{parsed['update_id']}"):
        return JSONResponse({"ok": True, "duplicate": True})
    result = await _route_guest_message(
        channel="telegram",
        sender_id=parsed["sender_id"],
        sender_name=parsed["sender_name"],
        text=parsed["text"],
        to=parsed["to"],
    )
    return JSONResponse({"ok": True, **result})


# --- Brevo email -----------------------------------------------------------

@app.post("/webhook/brevo")
async def brevo_webhook(
    request: Request,
    x_brevo_signature: str | None = Header(default=None),
) -> JSONResponse:
    if BREVO_INBOUND_SECRET and x_brevo_signature != BREVO_INBOUND_SECRET:
        raise HTTPException(status_code=401, detail="bad brevo signature")
    payload = await request.json()
    parsed = email_brevo.parse_inbound(payload)
    if not parsed:
        return JSONResponse({"ok": True, "skipped": True})
    if parsed.get("message_id") and store.seen(f"brevo:{parsed['message_id']}"):
        return JSONResponse({"ok": True, "duplicate": True})
    result = await _route_guest_message(
        channel="email",
        sender_id=parsed["sender_id"],
        sender_name=parsed["sender_name"],
        text=f"Subject: {parsed['subject']}\n\n{parsed['text']}",
        to=parsed["to"],
        extra_metadata={"subject": parsed["subject"], "reply_to": parsed["reply_to"]},
    )
    return JSONResponse({"ok": True, **result})


# --- Slack staff side ------------------------------------------------------

@app.post("/slack/command")
async def slack_command(request: Request) -> PlainTextResponse:
    body = await request.body()
    sig = request.headers.get("x-slack-signature", "")
    ts = request.headers.get("x-slack-request-timestamp", "")
    if not slack.verify_signature(ts, body, sig):
        raise HTTPException(status_code=401, detail="bad slack signature")
    form = dict((await request.form()).multi_items())
    parsed = slack.parse_command(form)
    if not parsed:
        return PlainTextResponse("Usage: `/ask <agent-slug> <message>`")
    # Create / append staff thread keyed by (channel, slug)
    channel = "slack"
    sender_key = f"{parsed['to']}::{parsed['agent_slug']}"
    issue_id = store.get_issue_id(channel, sender_key)
    if issue_id:
        await pc.add_comment(
            issue_id=issue_id,
            body=f"**{parsed['sender_name']}** (slack):\n\n{parsed['text']}",
            author_kind="external",
        )
    else:
        issue = await pc.create_issue(
            project_id=STAFF_PROJECT_ID,
            title=f"[slack] {parsed['sender_name']} → {parsed['agent_slug']}",
            body=f"**Channel:** slack\n**From:** {parsed['sender_name']}\n\n---\n\n{parsed['text']}",
            assignee_slug=parsed["agent_slug"],
            channel=channel,
            to=parsed["to"],
        )
        issue_id = str(issue.get("id") or issue.get("issueId") or "")
        if issue_id:
            store.set_issue_id(channel, sender_key, issue_id)
    return PlainTextResponse(f":memo: Routed to *{parsed['agent_slug']}*. Reply will appear here.")


# --- Web chat --------------------------------------------------------------

# In-memory queue keyed by sessionId for /api/web/poll.
_web_inbox: dict[str, deque[str]] = defaultdict(deque)
_web_lock = asyncio.Lock()


@app.get("/api/web/token")
async def web_token(session_id: str) -> dict[str, Any]:
    return {"token": web.mint_token(session_id)}


@app.post("/api/web/send")
async def web_send(request: Request) -> dict[str, Any]:
    payload = await request.json()
    parsed = web.parse_inbound(payload)
    if not parsed:
        raise HTTPException(status_code=400, detail="bad payload")
    result = await _route_guest_message(
        channel="web",
        sender_id=parsed["sender_id"],
        sender_name=parsed["sender_name"],
        text=parsed["text"],
        to=parsed["to"],
    )
    return {"ok": True, **result}


@app.get("/api/web/poll")
async def web_poll(session_id: str, token: str = "") -> dict[str, Any]:
    if os.environ.get("WEB_CHAT_SECRET") and not web.verify_token(session_id, token):
        raise HTTPException(status_code=401, detail="bad token")
    async with _web_lock:
        msgs = list(_web_inbox.get(session_id, ()))
        _web_inbox[session_id] = deque()
    return {"messages": msgs}


# ---------------------------------------------------------------------------
# Outbound: Paperclip → channel
# ---------------------------------------------------------------------------

@app.post("/webhook/paperclip")
async def paperclip_webhook(request: Request) -> dict[str, Any]:
    event = await request.json()
    event_type = event.get("type") or event.get("event")
    if event_type not in ("issue.comment.created", "comment.created"):
        return {"ok": True, "skipped": True, "type": event_type}

    comment = event.get("comment") or event.get("data", {}).get("comment") or {}
    issue = event.get("issue") or event.get("data", {}).get("issue") or {}

    author_type = (comment.get("authorType") or comment.get("author_kind") or "").lower()
    if author_type not in ("agent", "ai", "assistant"):
        return {"ok": True, "skipped": True, "reason": "non-agent author"}

    comment_id = str(comment.get("id") or "")
    if comment_id and store.seen(f"pc-cmt:{comment_id}"):
        return {"ok": True, "duplicate": True}

    metadata = issue.get("metadata") or {}
    channel = (metadata.get("channel") or "").lower()
    to = metadata.get("to") or ""
    body = comment.get("body") or ""
    if not channel or not to or not body:
        return {"ok": True, "skipped": True, "reason": "missing metadata"}

    if channel == "telegram":
        await telegram.send(to=to, text=body)
    elif channel == "email":
        subj = metadata.get("subject") or "Re: Your message"
        await email_brevo.send(to=to, text=body, subject=f"Re: {subj}"[:200])
    elif channel == "slack":
        await slack.send(to=to, text=body)
    elif channel == "web":
        async with _web_lock:
            _web_inbox[to].append(body)
    else:
        return {"ok": True, "skipped": True, "reason": f"unknown channel {channel}"}

    return {"ok": True, "channel": channel}
