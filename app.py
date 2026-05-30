"""Hotel Comms Gateway — FastAPI app.

Bridges Telegram (staff workspace + guest channel), Email, Slack, and Web ↔
Paperclip ↔ Grand Hotel agents.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from adapters import telegram, email_brevo, slack, web
from adapters.telegram import STAFF as STAFF_BOT, GUEST as GUEST_BOT
import agent_listener
import classifier
import commands
import guest_commands
import guest_profile
import intent_split
import memory as guest_mem
import paperclip_client as pc
import poller
import reply_validator
import search as web_search
import sentiment
import staff
import store
import world

log = logging.getLogger("app")

_META_RE = re.compile(r"<!--\s*comms-meta:\s*(\{.*?\})\s*-->", re.DOTALL)
_STATUS_RE = re.compile(r"^\s*STATUS:\s*.*$", re.MULTILINE | re.IGNORECASE)
_HANDOFF_LINE_RE = re.compile(r"^\s*@HANDOFF:\s*.*$", re.MULTILINE | re.IGNORECASE)


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


# Strip field labels — both `**Word**:` and `**Word:**` variants
_FIELD_LEAK_RE = re.compile(
    r"^\s*\*\*\s*(Channel|Room|Guest|Intent|Guest profile|Source|Reporter|Speaker|Routed to|Owner|Date|Department|Handoff from|From|Asking|To|Subject|Priority)\s*:?\s*\*\*\s*:?\s*.*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_for_guest(text: str) -> str:
    """Remove internal workflow markers before relaying to a real human channel."""
    t = _strip_meta(text or "")
    t = _STATUS_RE.sub("", t)
    t = _HANDOFF_LINE_RE.sub("", t)
    t = _FIELD_LEAK_RE.sub("", t)
    # Drop a leading `---` separator the agent sometimes echoes
    t = re.sub(r"^\s*---\s*$", "", t, flags=re.MULTILINE)
    # Collapse multiple blank lines
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


GUEST_PROJECT_ID = os.environ["PAPERCLIP_GUEST_PROJECT_ID"]
STAFF_PROJECT_ID = os.environ.get("PAPERCLIP_STAFF_PROJECT_ID", GUEST_PROJECT_ID)
TG_SECRET = os.environ.get("TG_SECRET", "")
TG_GUEST_SECRET = os.environ.get("TG_GUEST_SECRET", "")
BREVO_INBOUND_SECRET = os.environ.get("BREVO_INBOUND_SECRET", "")

app = FastAPI(title="Hotel Comms Gateway")

_poller_task: asyncio.Task | None = None
_bot_username: str | None = None
_last_errors: deque[dict] = deque(maxlen=20)
_last_updates: deque[dict] = deque(maxlen=20)


def _record_error(stage: str, exc: Exception) -> None:
    import traceback
    _last_errors.append({
        "ts": int(time.time()),
        "stage": stage,
        "error": type(exc).__name__,
        "detail": str(exc)[:600],
        "tb": traceback.format_exc().splitlines()[-8:],
    })

# In-memory queue keyed by sessionId for the web channel /api/web/poll.
_web_inbox: dict[str, deque[str]] = defaultdict(deque)
_web_lock = asyncio.Lock()


async def _admin_mirror(text: str) -> None:
    """Fan out a short notification to the workspace admin's DM (staff bot)."""
    admin_id = staff.admin_user_id()
    if not admin_id:
        return
    try:
        await STAFF_BOT.send(to=str(admin_id), text=text)
    except Exception as exc:
        log.warning("admin mirror failed: %s", exc)


async def _dispatch_to_channel(channel: str, to: str, body: str) -> None:
    """Outbound dispatcher used by the poller for *guest* channels.

    `body` arrives already stripped of comms-meta. For guest channels we strip
    additional internal markers (STATUS:, @HANDOFF:) so the guest sees a clean
    natural-language reply.
    """
    channel = (channel or "").lower()
    if channel == "telegram":
        await STAFF_BOT.send(to=to, text=_strip_for_guest(body))
    elif channel == "telegram_guest":
        clean = _strip_for_guest(body)
        room = store.get_room_for_guest(to) or {}
        expected_room = room.get("room_number") or ""
        expected_name = room.get("guest_name") or ""

        # Pull the topic from the most recent issue meta
        expected_topic = None
        try:
            issue_id_x = store.get_issue_id("telegram_guest", to)
            if issue_id_x:
                issue_x = await pc.get_issue(issue_id_x)
                meta_x = issue_x.get("metadata") or _extract_meta(issue_x.get("description") or "")
                expected_topic = (meta_x or {}).get("intent")
        except Exception:
            pass

        # VALIDATE — correct hallucinated room/name + reject evasive content
        corrected, report = reply_validator.correct_reply(
            reply=clean,
            expected_name=expected_name,
            expected_room=expected_room,
            expected_topic=expected_topic,
        )

        await GUEST_BOT.send(to=to, text=corrected)

        room_label = f"Room {expected_room}" if expected_room else f"tg:{to}"
        guest_name = expected_name or "guest"

        # Look up the actual agent name from the most recent thread issue
        agent_label = "Agent"
        try:
            issue_id = store.get_issue_id("telegram_guest", to)
            if issue_id:
                issue = await pc.get_issue(issue_id)
                aid = issue.get("assigneeAgentId")
                if aid:
                    name_map = await pc._agents_by_name()
                    for n, idv in name_map.items():
                        if idv == aid:
                            agent_label = n
                            break
        except Exception:
            pass

        mirror_text = f"🛏 {room_label} ({guest_name}) ← {agent_label}:\n{corrected[:1200]}"
        if report.get("changes"):
            mirror_text += f"\n_(🛠 auto-corrected: {', '.join(report['changes'])})_"
        if report.get("warnings"):
            mirror_text += f"\n_(⚠️ {', '.join(report['warnings'])})_"
        await _admin_mirror(mirror_text)

        if report.get("altered"):
            try:
                store.log_event(
                    event_type="reply_corrected",
                    issue_id=store.get_issue_id("telegram_guest", to),
                    actor="reply_validator",
                    actor_kind="system",
                    room_number=expected_room,
                    payload={"changes": report["changes"]},
                )
            except Exception:
                pass
    elif channel == "email":
        await email_brevo.send(to=to, text=_strip_for_guest(body))
    elif channel == "slack":
        await slack.send(to=to, text=_strip_for_guest(body))
    elif channel == "web":
        async with _web_lock:
            _web_inbox[to].append(_strip_for_guest(body))
    elif channel == "telegram_staff":
        # Handled by send_workspace path; ignore here.
        return
    else:
        raise ValueError(f"unknown channel {channel}")


async def _dispatch_workspace(origin: dict, body: str) -> None:
    """Relay an agent comment from a workspace issue back to staff."""
    dept = origin.get("department")
    label = staff.dept_label(dept) if dept else "Manager"
    agent = origin.get("agent_name") or label
    prefix = f"💬 {agent} →\n"

    # Set of recipients (deduplicated)
    targets: set[str] = set()
    if origin.get("tg_chat_id"):
        targets.add(str(origin["tg_chat_id"]))

    # Bound dept group
    if dept:
        bound = store.get_chat_for_dept(dept)
        if bound:
            targets.add(str(bound))

    # Admin DM — always fan out, so the user sees the workspace even without groups
    admin_id = staff.admin_user_id()
    if admin_id:
        targets.add(str(admin_id))

    # Handoff parsing — for autonomous cross-agent flow
    await _process_handoffs(origin=origin, body=body)

    for chat in targets:
        try:
            await telegram.send(to=chat, text=prefix + body)
        except Exception as exc:
            log.warning("workspace dispatch: post to %s failed: %s", chat, exc)


_HANDOFF_RE = re.compile(r"@HANDOFF:\s*([A-Za-z &\-]+)", re.IGNORECASE)


async def _process_handoffs(*, origin: dict, body: str) -> None:
    """If an agent's reply contains `@HANDOFF: <Department>`, create a child issue."""
    matches = _HANDOFF_RE.findall(body or "")
    if not matches:
        return
    seen_targets: set[str] = set()
    for raw in matches:
        target_norm = raw.strip().lower()
        # Map the natural-language target → a dept slug we know
        target_dept = None
        for dept_slug in staff.ALL_DEPTS:
            if dept_slug in target_norm or staff.dept_label(dept_slug).lower() in target_norm:
                target_dept = dept_slug
                break
        if not target_dept:
            continue
        if target_dept in seen_targets:
            continue
        seen_targets.add(target_dept)
        agent_name = staff.dept_agent_for(target_dept)
        if not agent_name:
            continue
        parent_issue_id = (origin or {}).get("issue_id") or "?"
        try:
            child = await pc.create_issue(
                project_id=STAFF_PROJECT_ID,
                title=f"[handoff] → {agent_name}: {(body[:60] or '').strip()}",
                body=(
                    f"**Handoff from:** {origin.get('agent_name') or 'a colleague'}\n"
                    f"**Source issue:** `{parent_issue_id[:8]}`\n"
                    f"**Department:** {staff.dept_label(target_dept)}\n\n"
                    f"---\n\n{body}"
                ),
                assignee_slug=agent_name,
                channel="telegram_staff",
                to=str(origin.get("tg_chat_id") or staff.admin_user_id() or "0"),
                extra_metadata={
                    "handoff_from_issue": parent_issue_id,
                    "handoff_dept": target_dept,
                    "agent_name": agent_name,
                },
            )
            child_id = str(child.get("id") or "")
            if child_id:
                store.set_issue_origin(
                    issue_id=child_id,
                    origin_kind="handoff",
                    tg_chat_id=str(origin.get("tg_chat_id") or "") or None,
                    tg_user_id=None,
                    department=target_dept,
                )
            # Trace the handoff to admin + log event
            from_agent = origin.get("agent_name") or "Concierge"
            await _admin_mirror(
                f"↪️ *Handoff* — {from_agent} → *{agent_name}* "
                f"({staff.dept_label(target_dept)}). "
                f"Child issue `{(child_id or '?')[:8]}` created. "
                f"{agent_name} will respond on next heartbeat."
            )
            store.log_event(
                event_type="handoff",
                issue_id=child_id,
                parent_issue=parent_issue_id,
                actor=from_agent,
                actor_kind="agent",
                department=target_dept,
                payload={"to_agent": agent_name},
            )
            # Wake the target agent immediately for fast cascades
            try:
                await pc.wake_agent(agent_name, reason="handoff")
            except Exception as exc:
                log.warning("wake on handoff failed: %s", exc)
        except Exception as exc:
            log.warning("handoff to %s failed: %s", target_dept, exc)


@app.on_event("startup")
async def _startup() -> None:
    global _poller_task, _bot_username
    store.init()
    # Cache the bot's @username for mention detection
    try:
        me = await telegram.get_me()
        _bot_username = me.get("username")
        log.info("telegram bot username: @%s", _bot_username)
    except Exception as exc:
        log.warning("get_me failed: %s", exc)
    _poller_task = asyncio.create_task(
        poller.run_loop(_dispatch_to_channel, send_workspace=_dispatch_workspace)
    )
    # If the world driver was running before a restart, resume it
    try:
        if world.is_running():
            await world.start_day()
            log.info("world driver auto-resumed (was running before restart)")
    except Exception as exc:
        log.warning("world driver auto-resume failed: %s", exc)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _poller_task:
        _poller_task.cancel()


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "hotel-comms-gateway",
        "bot": _bot_username,
        "version": "staff-workspace-v1",
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "ts": int(time.time())}


@app.get("/debug/errors")
async def debug_errors() -> dict[str, Any]:
    return {"errors": list(_last_errors), "bot": _bot_username}


@app.get("/debug/net")
async def debug_net() -> dict[str, Any]:
    """Probe outbound network from the Space."""
    import socket
    import httpx
    targets = [
        "api.telegram.org",
        "huggingface.co",
        "caesarniseko-paperclip.hf.space",
        "google.com",
        "1.1.1.1",
    ]
    out: dict[str, Any] = {}
    for host in targets:
        info: dict[str, Any] = {}
        try:
            ips = list({a[4][0] for a in socket.getaddrinfo(host, 443)})
            info["dns"] = ips
        except Exception as exc:
            info["dns_error"] = f"{type(exc).__name__}: {exc}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"https://{host}/")
                info["http_status"] = r.status_code
        except Exception as exc:
            info["http_error"] = f"{type(exc).__name__}: {exc}"
        out[host] = info
    return out


@app.get("/debug/updates")
async def debug_updates() -> dict[str, Any]:
    return {"updates": list(_last_updates)}


@app.post("/debug/send")
async def debug_send(request: Request) -> dict[str, Any]:
    """Send a test message — POST {chat_id, text}"""
    payload = await request.json()
    try:
        r = await telegram.send(to=str(payload["chat_id"]), text=payload.get("text", "ping"))
        return {"ok": True, "telegram_response": r}
    except Exception as exc:
        _record_error("debug_send", exc)
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}


# ---------------------------------------------------------------------------
# Guest-channel helper (web, email DM, telegram private DMs from non-staff)
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


async def _route_group_message(
    *,
    chat: dict,
    msg_from: dict,
    text: str,
    message_id: int | None,
) -> dict[str, Any]:
    """Active-participation: if a keyword/mention warrants it, route to the agent."""
    chat_id = str(chat["id"])

    # 1) If the chat is bound to a department, give that dept's agent priority
    bound = store.get_dept_group(chat_id)
    bound_dept = bound["department"] if bound else None

    # 2) Decide whether/who responds
    is_mention = agent_listener.is_mention(text, _bot_username or "")
    dept, agent_name, priority = agent_listener.classify_by_keywords(text)

    if is_mention and not agent_name and bound_dept:
        agent_name = staff.dept_agent_for(bound_dept)
        dept = bound_dept

    if not agent_name and bound_dept and len(text.strip()) >= 80:
        # Long messages in a bound dept group: use the dept's primary agent
        agent_name = staff.dept_agent_for(bound_dept)
        dept = bound_dept

    if not agent_name:
        return {"skipped": True, "reason": "no agent matched"}

    # 3) Throttle per (chat, agent)
    if not is_mention and not agent_listener.should_respond(
        chat_id=chat_id, agent_name=agent_name, text=text
    ):
        return {"skipped": True, "reason": "throttled"}

    sender = msg_from
    sender_name = (
        (sender.get("first_name") or "") + " " + (sender.get("last_name") or "")
    ).strip() or sender.get("username") or f"tg:{sender.get('id')}"

    s = store.get_staff_by_tg_user(str(sender.get("id")))
    role_line = ""
    if s and s.get("role_title"):
        role_line = f" ({s['role_title']}, {staff.dept_label(s.get('department'))})"

    title = f"[group:{chat.get('title') or chat_id}] {sender_name}{role_line}: {text[:60]}"
    body = (
        f"**Source:** Telegram group `{chat.get('title') or chat_id}`\n"
        f"**Speaker:** {sender_name}{role_line}\n"
        f"**Routed to:** {agent_name}"
        + (f" ({staff.dept_label(dept)})" if dept else "")
        + (f" — *priority {priority}*" if priority else "")
        + f"\n\n---\n\n{text}"
    )
    issue = await pc.create_issue(
        project_id=STAFF_PROJECT_ID,
        title=title,
        body=body,
        assignee_slug=agent_name,
        channel="telegram_staff",
        to=chat_id,
        priority=priority,
        extra_metadata={
            "reporter_name": sender_name,
            "reporter_tg_user_id": str(sender.get("id")),
            "routed_dept": dept,
            "from_group_message": True,
            "message_id": message_id,
        },
    )
    issue_id = str(issue.get("id") or "")
    if issue_id:
        store.set_issue_origin(
            issue_id=issue_id,
            origin_kind="staff_group",
            tg_chat_id=chat_id,
            tg_user_id=str(sender.get("id")),
            department=dept,
        )
    return {"created": True, "issue_id": issue_id, "agent": agent_name, "dept": dept}


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------

@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    if TG_SECRET and x_telegram_bot_api_secret_token != TG_SECRET:
        raise HTTPException(status_code=401, detail="bad telegram secret")
    try:
        update = await request.json()
        _last_updates.append({"ts": int(time.time()), "update": update})
        return await _handle_telegram_update(update)
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        log.exception("telegram webhook failed: %s", exc)
        _record_error("telegram_webhook", exc)
        return JSONResponse({
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc)[:600],
            "tb": tb.splitlines()[-6:],
        })


async def _handle_telegram_update(update: dict) -> JSONResponse:
    parsed = telegram.parse_update(update)
    if not parsed:
        return JSONResponse({"ok": True, "skipped": True})
    if parsed.get("update_id") and store.seen(f"tg:{parsed['update_id']}"):
        return JSONResponse({"ok": True, "duplicate": True})

    chat = parsed["chat"]
    chat_type = parsed["chat_type"]
    msg_from = parsed["from"]
    tg_user_id = str(msg_from.get("id"))
    text = parsed["text"]

    # --- 1) Slash commands -------------------------------------------------
    if parsed["is_command"]:
        try:
            reply = await commands.dispatch(text=text, msg_from=msg_from, chat=chat)
        except Exception as exc:
            log.exception("command dispatch failed: %s", exc)
            reply = f"⚠️ Command failed: `{type(exc).__name__}: {exc}`"
        if reply:
            await telegram.send(
                to=parsed["to"],
                text=reply,
                message_thread_id=parsed.get("message_thread_id"),
            )
        return JSONResponse({"ok": True, "handled": "command"})

    # --- 2) Non-command in a group chat → active-listener route ----------
    if chat_type in ("group", "supergroup"):
        try:
            result = await _route_group_message(
                chat=chat,
                msg_from=msg_from,
                text=text,
                message_id=parsed.get("message_id"),
            )
            return JSONResponse({"ok": True, "group": result})
        except Exception as exc:
            log.exception("group route failed: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)[:300]})

    # --- 3) Non-command in a DM ------------------------------------------
    # If sender is a known staff member: treat as a "/report"-style report
    s = store.get_staff_by_tg_user(tg_user_id)
    if s and s.get("department"):
        try:
            # Reuse /report flow programmatically
            reply = await commands.handle_report(
                tg_user_id=tg_user_id, msg_from=msg_from, chat=chat, args=text
            )
        except Exception as exc:
            reply = f"⚠️ Couldn't file: {exc}"
        await telegram.send(to=parsed["to"], text=reply)
        return JSONResponse({"ok": True, "handled": "dm_report"})

    # Unknown sender in DM → guest flow (existing behavior)
    result = await _route_guest_message(
        channel="telegram",
        sender_id=parsed["sender_id"],
        sender_name=parsed["sender_name"],
        text=text,
        to=parsed["to"],
    )
    return JSONResponse({"ok": True, "handled": "guest", **result})


# ---------------------------------------------------------------------------
# Guest bot — Telegram webhook (per-room private channels)
# ---------------------------------------------------------------------------

@app.post("/webhook/telegram_guest")
async def telegram_guest_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    if TG_GUEST_SECRET and x_telegram_bot_api_secret_token != TG_GUEST_SECRET:
        raise HTTPException(status_code=401, detail="bad guest secret")
    try:
        update = await request.json()
        _last_updates.append({"ts": int(time.time()), "update": update, "bot": "guest"})
        return await _handle_guest_update(update)
    except Exception as exc:
        import traceback
        log.exception("guest webhook failed: %s", exc)
        _record_error("guest_webhook", exc)
        return JSONResponse({
            "ok": False, "error": type(exc).__name__,
            "detail": str(exc)[:600],
            "tb": traceback.format_exc().splitlines()[-6:],
        })


async def _handle_guest_update(update: dict) -> JSONResponse:
    parsed = telegram.parse_update(update)
    if not parsed:
        return JSONResponse({"ok": True, "skipped": True})
    if parsed.get("update_id") and store.seen(f"tgg:{parsed['update_id']}"):
        return JSONResponse({"ok": True, "duplicate": True})

    msg_from = parsed["from"]
    chat = parsed["chat"]
    tg_user_id = str(msg_from.get("id"))
    text = parsed["text"]

    # --- Commands (checkin/checkout/help/start/room) -----------------------
    if parsed["is_command"]:
        try:
            reply = await guest_commands.dispatch(text=text, msg_from=msg_from, chat=chat)
        except Exception as exc:
            log.exception("guest command failed: %s", exc)
            reply = f"Something went wrong: {exc}"
        if reply:
            await GUEST_BOT.send(to=parsed["to"], text=reply)
        # If this was a successful /checkin, mirror the profile card to admin
        cmd_lower = text.strip().split(maxsplit=1)[0].lstrip("/").lower()
        if cmd_lower.startswith("checkin"):
            room = store.get_room_for_guest(tg_user_id)
            if room and room.get("profile_json"):
                try:
                    profile = json.loads(room["profile_json"])
                    await _admin_mirror(guest_profile.format_card(profile))
                except Exception as exc:
                    log.warning("admin checkin mirror failed: %s", exc)
        return JSONResponse({"ok": True, "handled": "guest_command"})

    # --- Free-form message: must be checked into a room --------------------
    room = store.get_room_for_guest(tg_user_id)
    if not room:
        await GUEST_BOT.send(
            to=parsed["to"],
            text=(
                "Welcome to Grand Hotel! To get started, please claim your room:\n"
                "  /checkin <room number>\ne.g. /checkin 408"
            ),
        )
        return JSONResponse({"ok": True, "handled": "checkin_prompt"})

    # Track follow-up count for this thread
    fu_key = f"followup:tg:{tg_user_id}"
    raw_fu = store.kv_get(fu_key)
    follow_up_index = int(raw_fu) if (raw_fu and raw_fu.isdigit()) else 0

    # Sentiment-aware immediate acknowledgement
    sent = sentiment.score(text)
    ack = sentiment.pick_ack(
        text=text,
        guest_name=room.get("guest_name") or "",
        room_number=room["room_number"],
        follow_up_index=follow_up_index,
    )
    try:
        await GUEST_BOT.send(to=parsed["to"], text=ack)
    except Exception as exc:
        log.warning("guest ack failed: %s", exc)

    # Mirror inbound to admin with profile snippet + sentiment tag
    profile_line = ""
    if room.get("profile_json"):
        try:
            p = json.loads(room["profile_json"])
            profile_line = (
                f" — {p.get('loyalty_tier')} tier, {p.get('segment')}, "
                f"night 1 of {p.get('nights')}"
            )
        except Exception:
            pass
    tone_emoji = {"calm": "💬", "urgent": "⚡", "frustrated": "😤", "angry": "🔥"}.get(sent["tone"], "💬")
    fu_label = f" · follow-up #{follow_up_index + 1}" if follow_up_index > 0 else ""
    await _admin_mirror(
        f"{tone_emoji} Room {room['room_number']} ({room.get('guest_name') or 'guest'}{profile_line}{fu_label}) →\n"
        f"{text[:1200]}\n"
        f"_(sentiment: {sent['tone']}, score {sent['score']})_"
    )

    # Increment follow-up counter for next time
    store.kv_set(fu_key, str(follow_up_index + 1))

    # Try splitting message into multiple parallel topic-clauses
    clauses = intent_split.split_message(text)
    results: list[dict[str, Any]] = []
    assignees: list[str] = []
    if len(clauses) >= 2:
        # Multi-topic message — spawn one issue per distinct intent
        await _admin_mirror(
            f"🪓 *Multi-topic message detected* — splitting into {len(clauses)} parallel "
            f"requests: {', '.join(t for t, _ in clauses)}"
        )
        for intent_label, clause_text in clauses:
            r = await _route_guest_room_message(
                room=room,
                msg_from=msg_from,
                text=clause_text,
                sentiment_info=sent,
                follow_up_index=follow_up_index,
            )
            results.append(r)
            if r.get("assignee"):
                assignees.append(r["assignee"])
    else:
        # Single topic — original path
        r = await _route_guest_room_message(
            room=room,
            msg_from=msg_from,
            text=text,
            sentiment_info=sent,
            follow_up_index=follow_up_index,
        )
        results.append(r)
        if r.get("assignee"):
            assignees.append(r["assignee"])

    # Wake all involved agents
    for assignee in set(assignees):
        try:
            await pc.wake_agent(assignee, reason="guest_message")
            await _admin_mirror(f"⚡ {assignee} woken on demand.")
        except Exception as exc:
            log.warning("wake_agent(%s) failed: %s", assignee, exc)

    result = results[0] if results else {}

    # Auto-escalate to GM if sentiment / repetition warrants it
    if sentiment.should_escalate_to_gm(sentiment=sent, follow_up_index=follow_up_index):
        try:
            esc = await pc.create_issue(
                project_id=STAFF_PROJECT_ID,
                title=f"[ESCALATION] Room {room['room_number']} — {sent['tone']} guest, {follow_up_index + 1} msgs",
                body=(
                    f"Auto-escalated by sentiment heuristic.\n\n"
                    f"Guest: {room.get('guest_name')} in Room {room['room_number']} "
                    f"({(json.loads(room['profile_json']) if room.get('profile_json') else {}).get('loyalty_tier', '?')} tier).\n"
                    f"Tone: {sent['tone']} (score {sent['score']}, signals: {', '.join(sent['signals'])}).\n"
                    f"Follow-ups so far: {follow_up_index + 1}.\n\n"
                    f"Latest message: \"{text[:300]}\"\n\n"
                    f"Action: take over personally, contact guest within 60 sec, propose recovery."
                ),
                assignee_slug="GM",
                channel="telegram_staff",
                to=str(staff.admin_user_id() or "0"),
                priority="high",
                extra_metadata={
                    "escalation_for": result.get("issue_id"),
                    "room": room["room_number"],
                    "guest_name": room.get("guest_name"),
                    "tone": sent["tone"],
                },
            )
            esc_id = str(esc.get("id") or "")
            if esc_id:
                store.set_issue_origin(
                    issue_id=esc_id,
                    origin_kind="autonomy",
                    tg_chat_id=None,
                    tg_user_id=staff.admin_user_id(),
                    department=staff.DEPT_MANAGEMENT,
                )
                store.log_event(
                    event_type="escalation",
                    issue_id=esc_id,
                    parent_issue=result.get("issue_id"),
                    actor="system",
                    actor_kind="system",
                    department=staff.DEPT_MANAGEMENT,
                    priority="high",
                    room_number=room["room_number"],
                    payload={"tone": sent["tone"], "follow_ups": follow_up_index + 1},
                )
            try:
                await pc.wake_agent("GM", reason="auto_escalation")
            except Exception:
                pass
            await _admin_mirror(
                f"🚨 *ESCALATION* — GM auto-engaged for Room {room['room_number']} "
                f"(tone: {sent['tone']}, msg #{follow_up_index + 1}). "
                f"Issue `{esc_id[:8]}` created with priority *high*."
            )
        except Exception as exc:
            log.warning("auto-escalation failed: %s", exc)

    return JSONResponse({"ok": True, "handled": "guest_message", **result})


async def _route_guest_room_message(
    *,
    room: dict,
    msg_from: dict,
    text: str,
    sentiment_info: dict | None = None,
    follow_up_index: int = 0,
) -> dict[str, Any]:
    """Create/append a Paperclip issue for a guest message, assigned by intent.
    Emits pipeline trace events to the admin mirror so the admin sees the whole flow.

    Issue body is written as natural prose (no labelled fields) to eliminate the
    model's tendency to echo labels in its reply.
    """
    tg_user_id = str(msg_from.get("id"))
    sender_name = room.get("guest_name") or (
        (msg_from.get("first_name") or "") + " " + (msg_from.get("last_name") or "")
    ).strip() or f"Room {room['room_number']}"
    room_no = room["room_number"]

    # Pull the synthesized profile for agent context
    profile = None
    if room.get("profile_json"):
        try:
            profile = json.loads(room["profile_json"])
        except Exception:
            profile = None

    def _profile_prose() -> str:
        if not profile:
            return ""
        loyalty = profile.get("loyalty_tier") or "—"
        prefs = profile.get("preferences") or []
        prefs_part = (
            f" Their stated preferences: {', '.join(prefs)}." if prefs else ""
        )
        return (
            f"Profile: {loyalty}-tier guest, "
            f"{profile.get('segment','—')} traveller, "
            f"{profile.get('prior_stays','0')} prior stays. "
            f"Booking {profile.get('booking_ref','—')} "
            f"({profile.get('nights','?')} nights, check-out "
            f"{profile.get('departure','—')}, ${profile.get('rate_per_night_usd','?')}/night, "
            f"{profile.get('payment_status','—')})."
            f"{prefs_part}"
        )

    tone_note = ""
    if sentiment_info and sentiment_info.get("tone") in ("urgent", "frustrated", "angry"):
        tone_note = (
            f" The guest seems {sentiment_info['tone']} "
            f"(signals: {', '.join(sentiment_info.get('signals', []))}). "
            f"Respond with extra care and a concrete ETA."
        )
    if follow_up_index > 0:
        tone_note += (
            f" This is follow-up message #{follow_up_index + 1} in the thread — "
            f"the guest has been waiting and is checking again."
        )

    # Detect topic intent for the STATE lock
    quick_intent, _, _ = await classifier.classify(text)

    # Update + pull structured memory
    store.remember_guest(
        tg_user_id=tg_user_id,
        sentiment_score=(sentiment_info or {}).get("score"),
        last_room=room_no,
    )
    store.remember_room(
        room_number=room_no,
        occupied=True,
        current_guest_tg=tg_user_id,
        complaint=(sentiment_info or {}).get("tone") in ("frustrated", "angry"),
    )
    memory_block = guest_mem.derive_memory_block(
        tg_user_id=tg_user_id,
        room_number=room_no,
        guest_name=sender_name,
        sentiment_info=sentiment_info,
        follow_up_index=follow_up_index,
    )

    state_lock = (
        f"\n\n<STATE>\n"
        f"  room: {room_no}\n"
        f"  guest_name: {sender_name}\n"
        f"  topic: {quick_intent}\n"
        f"  channel: telegram_guest\n"
        f"</STATE>\n"
        + (f"\n{memory_block}\n" if memory_block else "")
        + f"FACT-CHECK: Your reply MUST use room={room_no} and name={sender_name}. "
        f"The guest's topic is {quick_intent}. Do not change these."
    )

    # Per-topic threading: only append if there's an OPEN thread on the SAME topic
    existing_thread = store.find_open_topic_thread(
        channel="telegram_guest", sender_id=tg_user_id, topic=quick_intent
    )
    if existing_thread:
        existing_issue_id = existing_thread["issue_id"]
        comment_body = (
            f"{sender_name} (the guest in Room {room_no}) sent another message about "
            f"{quick_intent.replace('_',' ')}: \"{text}\"."
            + (f" {tone_note}" if tone_note else "")
            + "\n\nReply directly to the guest. "
            f"Use their actual name ({sender_name}) and their actual room number ({room_no})."
            + state_lock
        )
        await pc.add_comment(
            issue_id=existing_issue_id,
            body=comment_body,
            author_kind="external",
            extra_metadata={"room": room_no, "sender": sender_name, "agent_name": None},
        )
        store.upsert_topic_thread(
            channel="telegram_guest", sender_id=tg_user_id,
            topic=quick_intent, issue_id=existing_issue_id,
        )
        # Wake the assigned agent
        try:
            issue_full = await pc.get_issue(existing_issue_id)
            aid = issue_full.get("assigneeAgentId")
            if aid:
                name_map = await pc._agents_by_name()
                for n, idv in name_map.items():
                    if idv == aid:
                        await pc.wake_agent(n, reason="guest_follow_up")
                        break
        except Exception as exc:
            log.warning("wake on follow-up failed: %s", exc)
        await _admin_mirror(
            f"📌 *Same-topic follow-up* — appended to thread `{existing_issue_id[:8]}` "
            f"(topic: {quick_intent}) for Room {room_no}."
        )
        return {"issue_id": existing_issue_id, "created": False}
    # Different topic → fall through to create a NEW issue

    # Log inbound guest message
    store.log_event(
        event_type="guest_message",
        actor=sender_name,
        actor_kind="guest",
        room_number=room_no,
        payload={"text": text[:500]},
    )

    # PIPELINE STAGE 1: classify intent (reuse quick_intent if already classified)
    intent, assignee, priority = quick_intent, None, None
    intent, assignee, priority = await classifier.classify(text)
    store.log_event(
        event_type="classified",
        actor="classifier",
        actor_kind="system",
        intent=intent,
        priority=priority,
        room_number=room_no,
        payload={"agent": assignee, "text": text[:200]},
    )
    classifier_model = os.environ.get("CLASSIFIER_MODEL", "qwen3:8b-fast")
    pri_label = f" *priority {priority}*" if priority else ""
    await _admin_mirror(
        f"🔍 *Pipeline 1/3* — Classifier ({classifier_model}) → intent: *{intent}*, "
        f"route: *{assignee}*{pri_label}"
    )

    # PIPELINE STAGE 2: create the issue — natural prose body, no labels to mimic.
    title = f"[Room {room_no}] {sender_name}: {text[:60]}"
    profile_prose = _profile_prose()

    # If the topic needs external info, fetch search snippets and embed them
    search_block = ""
    if intent in ("concierge",) and any(
        kw in text.lower()
        for kw in (
            "where", "nearby", "near the hotel", "near", "recommend", "best",
            "around", "directions", "how do i get", "what's on", "event",
            "show", "theatre", "museum", "shop", "open", "hours", "closes",
            "restaurant", "bar", "cafe", "coffee", "store",
        )
    ):
        try:
            results = await web_search.search(text, n=5)
            if results:
                search_block = (
                    "\n\nWeb search snippets (use these for the reply, cite hours/places):\n"
                    + web_search.format_snippets(results, limit=5)
                    + "\n"
                )
                await _admin_mirror(
                    f"🔎 Web search injected ({len(results)} hits): `{text[:60]}`"
                )
        except Exception as exc:
            log.warning("web search failed: %s", exc)

    body = (
        f"A guest in Room {room_no}, name {sender_name}, contacted us via "
        f"Telegram. They wrote: \"{text}\".\n\n"
        f"This message is routed under the {intent} intent."
        + (f" {tone_note}" if tone_note else "")
        + "\n\n"
        + (f"{profile_prose}\n\n" if profile_prose else "")
        + search_block
        + "Reply directly to the guest now. "
        f"Use their EXACT name ({sender_name}) and EXACT room number ({room_no}) — "
        f"do NOT invent any other name or room number. Be brief (1–3 sentences), warm, "
        f"specific, with a concrete ETA. If this isn't your domain, write "
        f"@HANDOFF: <Department> on its own line, after a brief acknowledgement to "
        f"the guest with an ETA. Always end with STATUS: in_review on its own line "
        f"(the system strips it before showing to the guest)."
        + state_lock
    )

    issue = await pc.create_issue(
        project_id=GUEST_PROJECT_ID,
        title=title,
        body=body,
        assignee_slug=assignee,
        channel="telegram_guest",
        to=tg_user_id,
        priority=priority,
        extra_metadata={
            "room": room_no,
            "guest_name": sender_name,
            "intent": intent,
            "profile": profile,
        },
    )
    issue_id = str(issue.get("id") or "")
    if issue_id:
        store.set_issue_id("telegram_guest", tg_user_id, issue_id)
        # Register this issue as the open thread for THIS topic
        store.upsert_topic_thread(
            channel="telegram_guest", sender_id=tg_user_id,
            topic=intent, issue_id=issue_id, agent_name=assignee,
        )
        store.log_event(
            event_type="created",
            issue_id=issue_id,
            actor=assignee,
            actor_kind="system",
            intent=intent,
            priority=priority,
            room_number=room_no,
            payload={"title": title[:200]},
        )

    # PIPELINE STAGE 3: issue posted to Paperclip board
    company_id = os.environ.get("PAPERCLIP_COMPANY_ID", "")
    paperclip_base = os.environ.get("PAPERCLIP_API_BASE", "").rstrip("/")
    board_url = f"{paperclip_base}/companies/{company_id}/issues/{issue_id}" if (issue_id and paperclip_base) else ""
    await _admin_mirror(
        f"📋 *Pipeline 2/3* — Issue `{issue_id[:8]}` created in Paperclip → assigned to *{assignee}*.\n"
        + (f"Board: {board_url}" if board_url else "")
        + f"\n⚙️ *Pipeline 3/3* — Agent ({assignee}) starts on next heartbeat. "
        f"Reply will follow to guest + this chat."
    )

    return {"issue_id": issue_id, "created": True, "intent": intent, "assignee": assignee}


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


# --- Slack ------------------------------------------------------------------

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


# --- Web chat ---------------------------------------------------------------

@app.get("/api/web/token")
async def web_token(session_id: str) -> dict[str, Any]:
    return {"token": web.mint_token(session_id)}


@app.post("/api/web/send")
async def web_send(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        parsed = web.parse_inbound(payload)
        if not parsed:
            return JSONResponse({"ok": False, "error": "bad payload"}, status_code=400)
        result = await _route_guest_message(
            channel="web",
            sender_id=parsed["sender_id"],
            sender_name=parsed["sender_name"],
            text=parsed["text"],
            to=parsed["to"],
        )
        return JSONResponse({"ok": True, **result})
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:1500]},
            status_code=500,
        )


@app.get("/api/web/poll")
async def web_poll(session_id: str, token: str = "") -> dict[str, Any]:
    if os.environ.get("WEB_CHAT_SECRET") and not web.verify_token(session_id, token):
        raise HTTPException(status_code=401, detail="bad token")
    async with _web_lock:
        msgs = list(_web_inbox.get(session_id, ()))
        _web_inbox[session_id] = deque()
    return {"messages": msgs}


# --- Outbound (Paperclip → channel) — webhook fallback ----------------------

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
    if not metadata:
        metadata = _extract_meta(issue.get("description") or "")
    channel = (metadata.get("channel") or "").lower()
    to = metadata.get("to") or ""
    body = _strip_meta(comment.get("body") or "")
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
