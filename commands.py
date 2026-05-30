"""Telegram slash-command handlers for the Grand Hotel staff workspace.

Every handler returns a reply string (or None for no reply). Handlers may also
post messages via the Telegram adapter directly (e.g., to a different chat).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from adapters import telegram as tg
import autonomy
import paperclip_client as pc
import staff
import store
import world

log = logging.getLogger("commands")

GUEST_PROJECT_ID = os.environ.get("PAPERCLIP_GUEST_PROJECT_ID", "")
STAFF_PROJECT_ID = os.environ.get("PAPERCLIP_STAFF_PROJECT_ID", "")


def _split_command(text: str) -> tuple[str, str]:
    """`/cmd@botname rest of the text` → ('cmd', 'rest of the text')."""
    head, _, rest = text.strip().partition(" ")
    cmd = head.lstrip("/").split("@", 1)[0].lower()
    return cmd, rest.strip()


def _short_issue_id(full: str) -> str:
    return (full or "")[:8]


def _display_for_tg_user(msg_from: dict) -> str:
    name = (msg_from.get("first_name") or "").strip()
    if msg_from.get("last_name"):
        name = (name + " " + msg_from["last_name"]).strip()
    return name or msg_from.get("username") or f"tg:{msg_from.get('id')}"


# ---- handlers ---------------------------------------------------------------

async def handle_start(tg_user_id: str, msg_from: dict, chat_type: str) -> str:
    name = _display_for_tg_user(msg_from)
    # Bootstrap admin on first /start
    is_first_admin = staff.claim_admin(tg_user_id)
    is_admin_flag = staff.is_admin(tg_user_id)
    store.upsert_staff(
        tg_user_id=tg_user_id,
        tg_username=msg_from.get("username"),
        display_name=name,
        is_admin=is_admin_flag if is_first_admin else None,
    )

    bootstrap_line = (
        "\n\n🔑 You are the initial *workspace admin* — you can run `/register @user role`, "
        "and `/dept <slug>` inside department group chats."
    ) if is_first_admin else ""

    return (
        f"👋 Welcome to *Grand Hotel — Staff Workspace*, {name}!\n\n"
        f"This bot is your operational hub. The AI managers ({len(staff.DEPT_AGENT)} of them) "
        f"and your colleagues all live here.\n\n"
        f"*Get started:*\n"
        f"• `/iam <role>` — tell me what you do (e.g. `/iam Housekeeping`)\n"
        f"• `/help` — full command list\n"
        f"{bootstrap_line}"
    )


async def handle_help() -> str:
    return (
        "*Grand Hotel — Staff commands*\n\n"
        "*Identity*\n"
        "• `/iam <role>` — claim your role (e.g. `/iam Engineering`)\n"
        "• `/me` — show your registered role\n\n"
        "*Work*\n"
        "• `/report <text>` — file a new task (auto-routed to the right department)\n"
        "• `/tasks` — see your open tasks\n"
        "• `/take <id>` — claim a task\n"
        "• `/done <id> [notes]` — mark complete\n"
        "• `/handoff <id> @user` — pass to a colleague\n"
        "• `/status` — department snapshot\n\n"
        "*AI managers*\n"
        "• `/ask <Agent> <question>` — ping a manager (GM, DOR, Chief Engineer…)\n\n"
        "*Autonomous operations* (admin)\n"
        "• `/start_day` — begin the autonomous operating day (events fire continuously)\n"
        "• `/stop_day` — halt the autonomous loop\n"
        "• `/world` — show current world state (occupancy, ADR, recent events)\n"
        "• `/seed_day` — re-seed daily standing tasks\n\n"
        "*Group setup* (admin)\n"
        "• `/dept <slug>` — bind this Telegram group to a department\n"
        "• `/register @user <role>` — assign a role to a colleague"
    )


async def handle_iam(tg_user_id: str, msg_from: dict, args: str) -> str:
    if not args:
        return "Usage: `/iam <role>` — e.g. `/iam Housekeeping` or `/iam Chief Engineer`"
    route = staff.resolve_role(args)
    if not route:
        return (
            f"❓ I don't recognise the role *{args}*. Try one of:\n"
            "Housekeeping, Front Office, Concierge, Reservations, "
            "Engineering, F&B, Chef, Restaurant, Sales, Revenue, "
            "Security, Spa, HR, Finance, GM"
        )
    dept, title = route
    store.upsert_staff(
        tg_user_id=tg_user_id,
        tg_username=msg_from.get("username"),
        display_name=_display_for_tg_user(msg_from),
        department=dept,
        role_title=title,
    )
    return (
        f"✅ Registered: *{_display_for_tg_user(msg_from)}* as *{title}* "
        f"in *{staff.dept_label(dept)}*.\n\n"
        f"You'll see relevant tasks for this department. Try `/tasks` or `/report <text>`."
    )


async def handle_me(tg_user_id: str) -> str:
    s = store.get_staff_by_tg_user(tg_user_id)
    if not s or not s.get("department"):
        return "You're not registered yet. Run `/iam <role>` first."
    return (
        f"*{s.get('display_name') or 'Staff'}*\n"
        f"Role: {s.get('role_title')}\n"
        f"Department: {staff.dept_label(s.get('department'))}"
        + (" — *admin*" if s.get("is_admin") else "")
    )


async def handle_dept(tg_user_id: str, chat: dict, args: str) -> str:
    if not staff.is_admin(tg_user_id):
        return "🔒 Only the workspace admin can bind a group to a department."
    if chat.get("type") not in ("group", "supergroup"):
        return "Run `/dept <slug>` *inside* the Telegram group you want to bind."
    slug = args.strip().lower()
    if slug not in staff.ALL_DEPTS:
        return (
            f"Unknown department `{args}`. Choices: "
            + ", ".join(f"`{d}`" for d in staff.ALL_DEPTS)
        )
    store.set_dept_group(str(chat["id"]), chat.get("title"), slug)
    return (
        f"✅ This group is now bound to *{staff.dept_label(slug)}*.\n\n"
        f"Agent activity for this department will appear here, and AI managers "
        f"will listen for relevant cues in chat."
    )


async def handle_report(tg_user_id: str, msg_from: dict, chat: dict, args: str) -> str:
    if not args:
        return "Usage: `/report <what happened>` — e.g. `/report Room 412 AC not cooling`"
    s = store.get_staff_by_tg_user(tg_user_id)
    if not s or not s.get("department"):
        return "Please run `/iam <role>` first so I know who's reporting."

    # Classify by keyword (cheap heuristic) — fallback to reporter's own dept
    from agent_listener import classify_by_keywords  # local import to avoid cycle
    intent_dept, agent_name, priority = classify_by_keywords(args)
    if not agent_name:
        agent_name = staff.dept_agent_for(s["department"]) or "GM"
        intent_dept = s["department"]

    title = f"[report] {s.get('display_name')}: {args[:80]}"
    body = (
        f"**Reported by:** {s.get('display_name')} ({s.get('role_title')}, "
        f"{staff.dept_label(s.get('department'))})\n"
        f"**Channel:** telegram\n"
        f"**Department routed to:** {staff.dept_label(intent_dept)} → {agent_name}\n\n"
        f"---\n\n{args}"
    )
    extra = {
        "reporter_tg_user_id": tg_user_id,
        "reporter_name": s.get("display_name"),
        "reporter_dept": s.get("department"),
        "routed_dept": intent_dept,
    }
    issue = await pc.create_issue(
        project_id=STAFF_PROJECT_ID,
        title=title,
        body=body,
        assignee_slug=agent_name,
        channel="telegram_staff",
        to=str(chat["id"]),
        priority=priority,
        extra_metadata=extra,
    )
    issue_id = str(issue.get("id") or "")
    if issue_id:
        store.set_issue_origin(
            issue_id=issue_id,
            origin_kind="staff_report",
            tg_chat_id=str(chat["id"]),
            tg_user_id=tg_user_id,
            department=intent_dept,
        )
    pri_txt = f" *priority: {priority}*" if priority else ""
    return (
        f"📋 *Task created* `{_short_issue_id(issue_id)}` → assigned to *{agent_name}* "
        f"({staff.dept_label(intent_dept)}){pri_txt}\n\n"
        f"_{args[:200]}_\n\n"
        f"They'll respond shortly. Track with `/tasks`."
    )


async def handle_tasks(tg_user_id: str) -> str:
    s = store.get_staff_by_tg_user(tg_user_id)
    if not s or not s.get("department"):
        return "Run `/iam <role>` first."
    dept = s["department"]
    agent_name = staff.dept_agent_for(dept)
    if not agent_name:
        return f"No AI manager mapped to *{staff.dept_label(dept)}*."
    try:
        issues = await pc.list_issues_by_agent_name(agent_name, limit=15)
    except Exception as exc:
        return f"⚠️ Couldn't fetch tasks: {exc}"
    if not issues:
        return f"✨ No open tasks for *{staff.dept_label(dept)}* right now."
    open_statuses = ("todo", "backlog", "in_progress", "blocked", "in_review")
    open_ones = [i for i in issues if (i.get("status") or "").lower() in open_statuses]
    if not open_ones:
        return f"✨ No open tasks for *{staff.dept_label(dept)}* right now."
    lines = [f"*Open tasks — {staff.dept_label(dept)}* ({len(open_ones)})\n"]
    for i in open_ones[:12]:
        sid = _short_issue_id(str(i.get("id", "")))
        st = (i.get("status") or "").lower()
        pri = (i.get("priority") or "").lower()
        emoji = {"high": "🔴", "urgent": "🔴", "medium": "🟡", "low": "🟢"}.get(pri, "•")
        title = (i.get("title") or "").strip().replace("\n", " ")[:75]
        lines.append(f"{emoji} `{sid}` [{st}] {title}")
    lines.append("\nTake one with `/take <id>` or close with `/done <id> [notes]`.")
    return "\n".join(lines)


async def _resolve_short_id(short: str) -> Optional[str]:
    """Given a short 8-char prefix, find the full issue id from recent origins."""
    short = short.strip("` ").lower()
    if len(short) >= 32:
        return short
    for o in store.list_issue_origins(since_days=30):
        if o["issue_id"].lower().startswith(short):
            return o["issue_id"]
    return None


async def handle_take(tg_user_id: str, args: str) -> str:
    s = store.get_staff_by_tg_user(tg_user_id)
    if not s or not s.get("department"):
        return "Run `/iam <role>` first."
    short = args.split(maxsplit=1)[0] if args else ""
    if not short:
        return "Usage: `/take <id>` (from `/tasks`)"
    issue_id = await _resolve_short_id(short)
    if not issue_id:
        return f"❓ No recent task matches `{short}`."
    try:
        await pc.add_comment(
            issue_id=issue_id,
            body=f"*Claimed by {s.get('display_name')}* ({s.get('role_title')}). Working on it now.",
        )
        await pc.set_issue_status(issue_id, "in_progress")
    except Exception as exc:
        return f"⚠️ Couldn't take: {exc}"
    return f"✅ Taken `{_short_issue_id(issue_id)}`. Marked *in_progress*."


async def handle_done(tg_user_id: str, args: str) -> str:
    s = store.get_staff_by_tg_user(tg_user_id)
    if not s or not s.get("department"):
        return "Run `/iam <role>` first."
    parts = args.split(maxsplit=1)
    if not parts:
        return "Usage: `/done <id> [resolution notes]`"
    short = parts[0]
    notes = parts[1] if len(parts) > 1 else "Resolved."
    issue_id = await _resolve_short_id(short)
    if not issue_id:
        return f"❓ No recent task matches `{short}`."
    try:
        await pc.add_comment(
            issue_id=issue_id,
            body=f"✅ *Done by {s.get('display_name')}* ({s.get('role_title')}):\n\n{notes}",
        )
        await pc.set_issue_status(issue_id, "done")
    except Exception as exc:
        return f"⚠️ Couldn't close: {exc}"
    return f"✅ Closed `{_short_issue_id(issue_id)}`."


async def handle_handoff(tg_user_id: str, args: str) -> str:
    s = store.get_staff_by_tg_user(tg_user_id)
    if not s or not s.get("department"):
        return "Run `/iam <role>` first."
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: `/handoff <id> @username` or `/handoff <id> <agent-name>`"
    short, target = parts[0], parts[1].strip()
    issue_id = await _resolve_short_id(short)
    if not issue_id:
        return f"❓ No recent task matches `{short}`."

    target_staff = None
    agent_name = None
    if target.startswith("@"):
        target_staff = store.get_staff_by_username(target)
        if not target_staff or not target_staff.get("department"):
            return f"❓ I don't know {target} (or they haven't run `/iam` yet)."
        agent_name = staff.dept_agent_for(target_staff["department"])
    else:
        agent_name = target

    try:
        if agent_name:
            await pc.reassign_issue(issue_id, agent_name)
        note = (
            f"🤝 *Handoff from {s.get('display_name')}* → "
            f"{(target_staff and target_staff.get('display_name')) or target}.\n"
            f"Reason / context: handed off via Telegram."
        )
        await pc.add_comment(issue_id=issue_id, body=note)
    except Exception as exc:
        return f"⚠️ Couldn't hand off: {exc}"
    return f"🤝 Handed `{_short_issue_id(issue_id)}` → {target}."


async def handle_ask(tg_user_id: str, msg_from: dict, chat: dict, args: str) -> str:
    if not args:
        return (
            "Usage: `/ask <Agent> <question>`\n"
            "Agents: GM, DOR, DOSM, DOF, HR Director, Chief Engineer, Security Mgr, "
            "Spa Mgr, D F&B, Exec Chef, Restaurant Mgr, Concierge, Front Office Mgr, "
            "Reservations Mgr, Housekeeping Sup, Executive Housekeeper, Revenue Mgr"
        )
    # First token is agent identifier — accept up to 3 words for "Chief Engineer" etc.
    parts = args.split()
    matched_agent: Optional[str] = None
    matched_consumed = 0
    KNOWN = list({v for v in staff.DEPT_AGENT.values()} | {
        "Concierge", "Front Office Mgr", "Reservations Mgr", "Executive Housekeeper",
        "Housekeeping Sup", "Restaurant Mgr", "Exec Chef", "Revenue Mgr",
    })
    for n in (3, 2, 1):
        if len(parts) >= n:
            candidate = " ".join(parts[:n])
            for k in KNOWN:
                if k.lower() == candidate.lower():
                    matched_agent = k
                    matched_consumed = n
                    break
            if matched_agent:
                break
    if not matched_agent:
        return f"❓ Couldn't match an agent name. Try `/ask GM …` or `/ask Chief Engineer …`."
    question = " ".join(parts[matched_consumed:]).strip()
    if not question:
        return f"Usage: `/ask {matched_agent} <question>`"

    s = store.get_staff_by_tg_user(tg_user_id) or {}
    asker = s.get("display_name") or _display_for_tg_user(msg_from)
    title = f"[ask] {asker} → {matched_agent}: {question[:60]}"
    body = (
        f"**From:** {asker}"
        + (f" ({s.get('role_title')}, {staff.dept_label(s.get('department'))})" if s.get('department') else "")
        + f"\n**Asking:** {matched_agent}\n**Channel:** telegram_staff\n\n---\n\n{question}"
    )
    issue = await pc.create_issue(
        project_id=STAFF_PROJECT_ID,
        title=title,
        body=body,
        assignee_slug=matched_agent,
        channel="telegram_staff",
        to=str(chat["id"]),
        extra_metadata={"asker_tg_user_id": tg_user_id, "asker_name": asker, "is_ask": True},
    )
    issue_id = str(issue.get("id") or "")
    if issue_id:
        store.set_issue_origin(
            issue_id=issue_id,
            origin_kind="ask",
            tg_chat_id=str(chat["id"]),
            tg_user_id=tg_user_id,
            department=staff.DEPT_MANAGEMENT,
        )
    return f"💭 Asked *{matched_agent}* — reply will appear here in ~10s."


async def handle_status(tg_user_id: str, chat: dict) -> str:
    s = store.get_staff_by_tg_user(tg_user_id)
    # Prefer this group's bound department, else the user's
    dept = None
    bound = store.get_dept_group(str(chat["id"]))
    if bound:
        dept = bound["department"]
    elif s and s.get("department"):
        dept = s["department"]
    if not dept:
        return "Use `/status` in a department group, or `/iam <role>` first."
    agent_name = staff.dept_agent_for(dept)
    try:
        issues = await pc.list_issues_by_agent_name(agent_name, limit=20)
    except Exception as exc:
        return f"⚠️ {exc}"
    counts: dict[str, int] = {}
    for i in issues:
        st = (i.get("status") or "unknown").lower()
        counts[st] = counts.get(st, 0) + 1
    lines = [f"*Status — {staff.dept_label(dept)}* (manager: {agent_name})\n"]
    if not counts:
        lines.append("No tasks tracked right now.")
    else:
        for st in ("todo", "backlog", "in_progress", "blocked", "in_review", "done"):
            if counts.get(st):
                lines.append(f"  • {st}: *{counts[st]}*")
    staff_in_dept = store.list_staff(department=dept)
    if staff_in_dept:
        lines.append(f"\n*{len(staff_in_dept)} staff* in department:")
        for m in staff_in_dept[:8]:
            lines.append(f"  • {m.get('display_name')} ({m.get('role_title')})")
    return "\n".join(lines)


async def handle_start_day(tg_user_id: str, args: str) -> str:
    if not staff.is_admin(tg_user_id):
        return "🔒 Admin only."
    # Make sure standing seeds exist
    seed_result = await autonomy.seed_day(force=False)
    started = await world.start_day()
    s = world.get_state()
    if started.get("already_running"):
        return (
            f"☀️ The day is already running.\n"
            f"World clock: {s['world_hour']:02d}:{s['world_minute']:02d}, "
            f"occupancy {int(s['occupancy']*100)}%, ADR ${s['adr']:.0f}.\n"
            f"Use /stop_day to halt or /world to inspect."
        )
    return (
        f"🌅 *Operating day STARTED*\n"
        f"World tick: every {started.get('tick_seconds')}s (real) = +30 min world time.\n"
        f"Synthetic events will start firing into your agents' issues. "
        f"You'll see pulses cascading into this chat as agents react.\n\n"
        f"Run /stop_day to halt. /world to inspect current state."
    )


async def handle_stop_day(tg_user_id: str, args: str) -> str:
    if not staff.is_admin(tg_user_id):
        return "🔒 Admin only."
    result = await world.stop_day()
    if result.get("already_stopped"):
        return "🌙 The day is already stopped."
    return (
        "🌙 *Operating day STOPPED*\n"
        "World driver halted. Heartbeats remain enabled but no new events will "
        "be injected. Restart with /start_day."
    )


async def handle_world(tg_user_id: str) -> str:
    s = world.get_state()
    log_lines = ""
    log_items = s.get("events_log") or []
    if log_items:
        log_lines = "\n\nRecent events:\n" + "\n".join(
            f"  • {e.get('agent','?')}: {e.get('headline','?')}" for e in log_items[-8:]
        )
    return (
        f"🏨 *Grand Hotel — world state*\n\n"
        f"Driver: {'🟢 RUNNING' if s.get('running') else '🔴 STOPPED'}\n"
        f"Day: {s.get('day_index', 1)}\n"
        f"World time: {s['world_hour']:02d}:{s['world_minute']:02d}\n"
        f"Occupancy: {int(s['occupancy']*100)}%\n"
        f"ADR: ${s['adr']:.0f}\n"
        f"Pickup 24h: {s['pickup_24h']}\n"
        f"OOO rooms: {s['ooo_count']}\n"
        f"Open WOs: {s['open_wos']}\n"
        f"Incidents today: {s.get('incidents_today', 0)}"
        f"{log_lines}"
    )


async def handle_seed_day(tg_user_id: str, args: str) -> str:
    if not staff.is_admin(tg_user_id):
        return "🔒 Admin only."
    force = "force" in args.lower()
    result = await autonomy.seed_day(force=force)
    if result.get("skipped"):
        return (
            "📅 Already seeded for today. Use `/seed_day force` to re-create the "
            "standing issues anyway."
        )
    n_ok = sum(1 for c in result.get("created", []) if "issue_id" in c and c["issue_id"])
    return (
        f"🌅 *Day seeded* — {n_ok} standing issues created across HODs.\n"
        f"Agents will pick them up on their heartbeats and start posting "
        f"operational pulses into this chat. Watch the next ~5-15 min."
    )


async def handle_register(tg_user_id: str, args: str) -> str:
    if not staff.is_admin(tg_user_id):
        return "🔒 Admin only."
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: `/register @username <role>`"
    uname = parts[0].lstrip("@")
    role_text = parts[1]
    target = store.get_staff_by_username(uname)
    if not target:
        return (
            f"❓ {parts[0]} hasn't started the bot yet. Ask them to send `/start` first, "
            f"then re-run this command."
        )
    route = staff.resolve_role(role_text)
    if not route:
        return f"❓ Unknown role `{role_text}`."
    dept, title = route
    store.upsert_staff(
        tg_user_id=target["tg_user_id"],
        tg_username=target.get("tg_username"),
        display_name=target.get("display_name") or uname,
        department=dept,
        role_title=title,
    )
    return f"✅ Registered *{uname}* as *{title}* in *{staff.dept_label(dept)}*."


# ---- entry point ------------------------------------------------------------

async def dispatch(*, text: str, msg_from: dict, chat: dict) -> Optional[str]:
    cmd, args = _split_command(text)
    tg_user_id = str(msg_from.get("id"))
    store.touch_staff(tg_user_id)
    chat_type = chat.get("type", "private")

    if cmd in ("start",):
        return await handle_start(tg_user_id, msg_from, chat_type)
    if cmd in ("help", "?"):
        return await handle_help()
    if cmd == "iam":
        return await handle_iam(tg_user_id, msg_from, args)
    if cmd == "me":
        return await handle_me(tg_user_id)
    if cmd == "dept":
        return await handle_dept(tg_user_id, chat, args)
    if cmd == "report":
        return await handle_report(tg_user_id, msg_from, chat, args)
    if cmd == "tasks":
        return await handle_tasks(tg_user_id)
    if cmd == "take":
        return await handle_take(tg_user_id, args)
    if cmd == "done":
        return await handle_done(tg_user_id, args)
    if cmd == "handoff":
        return await handle_handoff(tg_user_id, args)
    if cmd == "ask":
        return await handle_ask(tg_user_id, msg_from, chat, args)
    if cmd == "status":
        return await handle_status(tg_user_id, chat)
    if cmd == "register":
        return await handle_register(tg_user_id, args)
    if cmd in ("seed_day", "seedday", "seed"):
        return await handle_seed_day(tg_user_id, args)
    if cmd in ("start_day", "startday", "go", "begin"):
        return await handle_start_day(tg_user_id, args)
    if cmd in ("stop_day", "stopday", "halt", "pause"):
        return await handle_stop_day(tg_user_id, args)
    if cmd in ("world", "state", "snapshot"):
        return await handle_world(tg_user_id)
    return None  # unknown command — caller decides whether to ignore
