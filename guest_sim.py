"""Synthetic guest population that drives the guest pipeline end-to-end.

These guests have no real Telegram chat — they exist only as records in
`room_assignments` (with synthetic profiles) and as callers of the same
`_route_guest_room_message` flow that real Telegram-bot guests use.

The admin sees every move in the staff bot mirror, tagged `[SIM]`.

Lifecycle per persona:
  - check-in (synthesized profile via guest_profile.synthesize)
  - send 5–12 messages over their stay (mix of routine + unusual + compound)
  - check-out

Pacing follows the world clock from world.py so meal times bring food
requests, evening brings entertainment + recovery cases, etc.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
from typing import Any, Awaitable, Callable

import guest_profile
import store
import world

log = logging.getLogger("guest_sim")

SIM_TICK_SECONDS = int(os.environ.get("SIM_TICK_SECONDS", "45"))
MAX_ACTIVE_GUESTS = int(os.environ.get("SIM_MAX_GUESTS", "6"))
NEW_GUEST_PROB = float(os.environ.get("SIM_NEW_GUEST_PROB", "0.18"))
SIM_MESSAGES_PER_TICK = float(os.environ.get("SIM_MSGS_PER_TICK", "1.6"))

# ---- persona pool ----------------------------------------------------------

PERSONAS = [
    {"first_name": "Mr. Tanaka",       "country": "JP", "style": "polite,business"},
    {"first_name": "Mrs. Lina Park",   "country": "KR", "style": "warm,family"},
    {"first_name": "Dr. Müller",       "country": "DE", "style": "exact,demanding"},
    {"first_name": "Mr. Roberto Silva","country": "BR", "style": "friendly,curious"},
    {"first_name": "Ms. Yara Hassan",  "country": "EG", "style": "polite,particular"},
    {"first_name": "Mr. Wei Zhang",    "country": "CN", "style": "brief,direct"},
    {"first_name": "Ms. Sophie Laurent","country": "FR", "style": "refined,picky"},
    {"first_name": "Mr. Diego Romano", "country": "IT", "style": "charming,playful"},
    {"first_name": "Ms. Priya Patel",  "country": "IN", "style": "warm,detail-oriented"},
    {"first_name": "Mr. James Carter", "country": "US", "style": "casual,relaxed"},
    {"first_name": "Mrs. Olga Petrov", "country": "RU", "style": "VIP,demanding"},
    {"first_name": "Mr. Hiroshi Saito","country": "JP", "style": "shy,formal"},
    {"first_name": "Ms. Carla Mendes", "country": "PT", "style": "lively,party"},
    {"first_name": "Mr. Antoine Bauer","country": "BE", "style": "quiet,reserved"},
]

# ---- message library by intent --------------------------------------------

MESSAGES = {
    "f_and_b": [
        "where is my food?",
        "I'm hungry — can someone send up a sandwich please?",
        "is breakfast still being served?",
        "I'd like a glass of red wine in the room",
        "can I get a vegetarian option for dinner tonight?",
        "the soup arrived cold, can I get a hot one?",
        "I want fresh fruit and yogurt please",
        "I'd like to make a dinner reservation for 19:30, table for two",
        "do you have gluten-free pastries?",
        "I want a late-night cheese plate to my room",
        "what's today's special?",
        "I'm craving sushi — what can you do?",
    ],
    "maintenance": [
        "the AC isn't cooling, can someone check?",
        "the wifi keeps dropping",
        "the TV remote is dead",
        "there's a slow leak under the bathroom sink",
        "the lamp by the bed isn't working",
        "the safe won't open, I tried the code twice",
        "my key card stopped working",
        "the toilet won't flush properly",
        "the minibar fridge feels warm",
        "the curtain track is stuck",
    ],
    "housekeeping": [
        "could I get extra pillows please?",
        "I need fresh towels",
        "can someone bring an iron and board?",
        "I'd like turn-down service tonight",
        "could you refresh the toiletries?",
        "the bedding could use a change please",
        "I'd like a hypoallergenic pillow",
        "can I get a children's amenity kit?",
        "extra hangers please",
        "I need an extra blanket",
    ],
    "concierge": [
        "where is the gym and what time does it close?",
        "what's the wifi password?",
        "where is the pool?",
        "recommend a quiet Japanese restaurant nearby",
        "how do I get to the airport tomorrow morning at 06:00?",
        "what's the best bar within walking distance?",
        "is there an ATM nearby?",
        "directions to the theatre district please",
        "are there shows on tonight in town?",
        "where can I rent a bicycle?",
        "I'd like a taxi to the city centre in 30 min",
        "what's the late-checkout policy?",
    ],
    "spa": [
        "do you have a couples massage available tonight at 19:00?",
        "I'd like a 60-minute deep-tissue massage tomorrow morning",
        "is the spa open right now?",
        "what facial treatments do you offer?",
        "I want a sauna session before dinner",
    ],
    "billing": [
        "I see a $42 minibar charge I don't recognise on my folio",
        "can you confirm my room rate for the stay?",
        "could you split the bill for me?",
        "I think I was double-charged for breakfast",
        "can I get a copy of my folio so far?",
    ],
    "complaint_escalate": [
        "this is unacceptable, I want to speak to a manager",
        "I'm very disappointed with the service — I want a refund",
        "I will leave a terrible review if this isn't fixed",
        "this hotel is the worst, I'm checking out tomorrow",
        "I've waited 40 minutes for a simple request and nothing",
    ],
    "_unusual_": [
        "I want to dance with dolphins tomorrow",
        "I want lots of female dancers in the room tonight",
        "I want to eat elephant tonight",
        "I need a private island for the weekend",
        "get me a helicopter to dinner please",
        "I want a yacht party for 30 people",
        "I want to ride an elephant",
        "find me clowns for a private party",
        "I want a bachelor party with everything",
        "where can I get absinthe?",
    ],
    "_compound_": [
        "I need milk and sweets AND fresh towels",
        "can I get a snack AND fix the AC AND extra pillows please",
        "the wifi is slow AND I want room service AND directions to the museum",
        "I want ice cream AND a massage tonight AND extra hangers",
    ],
}


def _weighted_intent(world_hour: int) -> str:
    """Pick an intent reasonable for the world hour."""
    base_weights = {
        "f_and_b": 30, "maintenance": 10, "housekeeping": 15,
        "concierge": 20, "spa": 5, "billing": 3,
        "complaint_escalate": 4, "_unusual_": 7, "_compound_": 6,
    }
    # Meal-time bump
    if 7 <= world_hour <= 10:
        base_weights["f_and_b"] = 60
    elif 12 <= world_hour <= 14:
        base_weights["f_and_b"] = 50
    elif 18 <= world_hour <= 21:
        base_weights["f_and_b"] = 55
        base_weights["_unusual_"] = 12   # evening = playful asks
    elif 22 <= world_hour or world_hour <= 5:
        base_weights["maintenance"] = 15
        base_weights["complaint_escalate"] = 8
        base_weights["_unusual_"] = 10
    intents, weights = zip(*base_weights.items())
    return random.choices(intents, weights=weights, k=1)[0]


# ---- runtime state ---------------------------------------------------------

_STATE_KEY = "guest_sim_state"
_TASK: asyncio.Task | None = None
_LOCK = asyncio.Lock()


def _load_state() -> dict[str, Any]:
    raw = store.kv_get(_STATE_KEY)
    if not raw:
        return {"running": False, "next_sim_id": 1, "ticks": 0}
    try:
        return json.loads(raw)
    except Exception:
        return {"running": False, "next_sim_id": 1, "ticks": 0}


def _save_state(s: dict[str, Any]) -> None:
    store.kv_set(_STATE_KEY, json.dumps(s, default=str))


def _active_sim_rooms() -> list[dict]:
    """Currently-checked-in synthetic guests (tg_user_id starts with `sim:`)."""
    return [r for r in store.list_active_rooms() if str(r.get("tg_user_id", "")).startswith("sim:")]


def _pick_room_number() -> str:
    """Pick a room number not currently occupied."""
    used = {r["room_number"] for r in store.list_active_rooms()}
    for _ in range(50):
        n = random.choice(range(101, 1599))
        room_no = str(n)
        if room_no not in used:
            return room_no
    return str(random.randint(101, 1599))


async def _check_in_one(handler_fn) -> dict | None:
    """Bring a new synthetic guest online."""
    state = _load_state()
    sim_id = state.get("next_sim_id", 1)
    persona = random.choice(PERSONAS)
    name = persona["first_name"]
    room = _pick_room_number()
    tg_id = f"sim:{sim_id:04d}"
    profile = guest_profile.synthesize(tg_user_id=tg_id, name=name, room=room)
    rec = store.check_in_guest(
        tg_user_id=tg_id,
        room_number=room,
        guest_name=name,
        tg_chat_id=tg_id,
        profile_json=json.dumps(profile),
    )
    try:
        store.remember_guest(tg_user_id=tg_id, last_room=room)
        store.bump_guest_stays(tg_id)
    except Exception:
        pass
    state["next_sim_id"] = sim_id + 1
    _save_state(state)
    # Mirror the check-in
    try:
        await handler_fn(
            event="checkin",
            persona=persona,
            tg_id=tg_id,
            room_no=room,
            profile=profile,
            text=None,
        )
    except Exception as exc:
        log.warning("checkin mirror failed: %s", exc)
    return rec


async def _send_message(handler_fn, room: dict) -> None:
    """Pick an intent + message for an active guest, route through pipeline."""
    persona_match = next((p for p in PERSONAS if p["first_name"] == room.get("guest_name")), None)
    persona = persona_match or random.choice(PERSONAS)
    world_state = world.get_state()
    world_hour = world_state.get("world_hour", 12)
    intent = _weighted_intent(world_hour)
    message = random.choice(MESSAGES.get(intent) or MESSAGES["f_and_b"])
    try:
        await handler_fn(
            event="message",
            persona=persona,
            tg_id=room["tg_user_id"],
            room_no=room["room_number"],
            profile=None,
            text=message,
            intent_hint=intent,
        )
    except Exception as exc:
        log.exception("sim message handler failed: %s", exc)
        try:
            from app import _record_error
            _record_error("sim_send_message", exc)
        except Exception:
            pass


async def _check_out_one(handler_fn, room: dict) -> None:
    store.check_out_guest(room["tg_user_id"])
    try:
        await handler_fn(
            event="checkout",
            persona=None,
            tg_id=room["tg_user_id"],
            room_no=room["room_number"],
            profile=None,
            text=None,
        )
    except Exception:
        pass


async def tick(handler_fn) -> dict:
    """One sim tick — may check in, send messages, check out."""
    state = _load_state()
    state["ticks"] = state.get("ticks", 0) + 1
    actions = {"checkins": 0, "messages": 0, "checkouts": 0}

    actives = _active_sim_rooms()

    # 1) maybe check in a new guest
    if len(actives) < MAX_ACTIVE_GUESTS and random.random() < NEW_GUEST_PROB:
        rec = await _check_in_one(handler_fn)
        if rec:
            actions["checkins"] += 1
            actives = _active_sim_rooms()

    # 2) send messages for active guests
    if actives:
        n_msgs = max(1, int(round(random.gauss(SIM_MESSAGES_PER_TICK, 1.0))))
        n_msgs = max(0, min(n_msgs, len(actives) * 2))
        # Slight preference for guests with fewer prior messages this stay
        random.shuffle(actives)
        for room_rec in actives[:n_msgs]:
            await _send_message(handler_fn, room_rec)
            actions["messages"] += 1

    # 3) check out random oldest guest with low prob
    if actives and random.random() < 0.10:
        # oldest
        oldest = max(actives, key=lambda r: int(state.get("ticks", 0)) - 0)
        await _check_out_one(handler_fn, oldest)
        actions["checkouts"] += 1

    _save_state(state)
    return actions


async def _run_loop(handler_fn) -> None:
    log.info("guest sim loop starting (tick=%ss)", SIM_TICK_SECONDS)
    while True:
        state = _load_state()
        if not state.get("running"):
            return
        try:
            res = await tick(handler_fn)
            if any(res.values()):
                log.info("sim tick: %s", res)
        except Exception as exc:
            log.exception("sim tick failed: %s", exc)
        await asyncio.sleep(SIM_TICK_SECONDS)


def is_running() -> bool:
    return _load_state().get("running", False)


def get_state() -> dict[str, Any]:
    state = _load_state()
    state["active_guests"] = len(_active_sim_rooms())
    return state


async def start_sim(handler_fn) -> dict:
    global _TASK
    state = _load_state()
    if state.get("running"):
        return {"ok": True, "already_running": True}
    state["running"] = True
    state["started_at"] = datetime.datetime.utcnow().isoformat()
    _save_state(state)
    if _TASK is None or _TASK.done():
        _TASK = asyncio.create_task(_run_loop(handler_fn))
    return {"ok": True, "started": True, "tick_seconds": SIM_TICK_SECONDS}


async def force_burst(handler_fn, n_guests: int = 3, msgs_per_guest: int = 2) -> dict:
    """Force-check-in N guests immediately + send M messages each. For instant demo."""
    res = {"checkins": 0, "messages": 0}
    for _ in range(n_guests):
        rec = await _check_in_one(handler_fn)
        if rec:
            res["checkins"] += 1
    # Send messages for all active guests
    actives = _active_sim_rooms()
    for room_rec in actives[:n_guests * msgs_per_guest]:
        await _send_message(handler_fn, room_rec)
        res["messages"] += 1
    return res


async def stop_sim() -> dict:
    global _TASK
    state = _load_state()
    if not state.get("running"):
        return {"ok": True, "already_stopped": True}
    state["running"] = False
    _save_state(state)
    if _TASK and not _TASK.done():
        _TASK.cancel()
    return {"ok": True, "stopped_at": datetime.datetime.utcnow().isoformat()}


async def checkout_all_sim() -> int:
    """Force-checkout every active simulated guest."""
    actives = _active_sim_rooms()
    for r in actives:
        store.check_out_guest(r["tg_user_id"])
    return len(actives)
