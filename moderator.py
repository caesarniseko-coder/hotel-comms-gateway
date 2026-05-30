"""Uncensored sidecar moderator for unusual hospitality requests.

Some guests ask things that mainstream LLMs reflexively refuse ("dance with
dolphins", "I want female dancers", "eat elephant"). A flat refusal is bad
hospitality. We route such requests through an *uncensored* sidecar (Dolphin 3)
which reasons calmly and produces structured guidance for the Concierge.

The moderator output NEVER reaches the guest verbatim — it only shapes what
the Concierge agent (qwen3:32b) says.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger("moderator")

MODERATOR_MODEL = os.environ.get("MODERATOR_MODEL", "dolphin3:8b")
MODERATOR_FALLBACK = os.environ.get("MODERATOR_FALLBACK_MODEL", "qwen3:8b-fast")
GATEWAY_URL = os.environ.get("GATEWAY_BASE_URL", "").rstrip("/")
GATEWAY_KEY = os.environ.get("GATEWAY_API_KEY_CLASSIFIER", "") or os.environ.get("GATEWAY_API_KEY", "")

# Trigger keywords — if any of these is in the guest message, fire the moderator
_FLAG_KEYWORDS = {
    # animals
    "dolphin", "elephant", "tiger", "lion", "shark", "whale", "exotic animal",
    "ride a", "rare animal", "endangered",
    # entertainment / adult-adjacent
    "dancer", "dancers", "strip", "stripper", "escort", "ladies", "female danc",
    "private show", "clown", "clowns", "magician", "fire eater", "stunt",
    "after party", "bachelor", "bachelorette", "rave",
    # food / substances extremes
    "elephant meat", "raw meat", "live animal", "rare delicacy", "drugs",
    "cannabis", "weed", "cocaine", "absinthe", "moonshine", "fugu",
    # adventure / risk
    "skydiv", "bungee", "free climb", "race car", "shooting range", "fight club",
    "gun", "knife throw", "wing suit",
    # unusual asks
    "i want lots of", "i need lots of", "get me a lot of", "private island",
    "yacht", "helicopter", "limousine party",
}


def looks_unusual(text: str) -> tuple[bool, list[str]]:
    """Return (is_unusual, matched_keywords)."""
    if not text:
        return False, []
    t = text.lower()
    matched = [kw for kw in _FLAG_KEYWORDS if kw in t]
    if matched:
        return True, matched
    return False, []


_SYSTEM_PROMPT = """You are an internal hospitality advisor for a 4-5★ city hotel concierge team. A guest has sent an unusual request through Telegram. The Concierge will craft the actual reply to the guest — you only produce structured guidance.

Your job:
1. Understand what the guest actually wants (literal vs playful vs euphemistic).
2. Decide: directly doable, doable-with-substitute, or politely-redirect.
3. If directly doable: name a specific legitimate vendor/venue/activity nearby.
4. If not: suggest the closest legitimate alternative that satisfies the SPIRIT of the request — never moralise, never lecture, never refuse coldly.
5. Stay practical, warm, and resourceful. Treat the guest as a paying adult.

Examples:
- "I want to dance with dolphins" -> legitimate dolphin encounter at a marine sanctuary
- "I want female dancers" -> live cabaret / burlesque show booking; or in-room dance act vendor
- "I want to eat elephant" -> exotic game-meat tasting menu (boar, ostrich, kangaroo)
- "I want lots of clowns" -> birthday entertainer agency, theatre troupe

Output ONLY a single JSON object (no preamble, no markdown):

{
  "category": "animal | entertainment | food | adventure | adult | intimate | other",
  "is_directly_doable": true | false,
  "interpretation": "<one sentence on what they likely mean>",
  "concierge_plan": "<one sentence: the action the Concierge should propose>",
  "suggested_vendor_or_alternative": "<a specific recommendation with rough place name>",
  "tone": "warm | playful | firm | neutral",
  "ethical_flag": null | "advisory" | "decline"
}

If ethical_flag is "decline", suggested_vendor_or_alternative MUST still be a legitimate alternative — never just refuse."""


async def _call_gateway(model: str, system: str, user: str) -> str | None:
    if not GATEWAY_URL or not GATEWAY_KEY:
        return None
    base = GATEWAY_URL
    path = "/chat/completions" if base.rstrip("/").endswith("/v1") else "/v1/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
        "max_tokens": 400,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{base}{path}",
                headers={"Authorization": f"Bearer {GATEWAY_KEY}", "Content-Type": "application/json"},
                json=body,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""
    except Exception as exc:
        log.warning("moderator call (%s) failed: %s", model, exc)
        return None


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def assess(text: str, guest_name: str | None = None, room_no: str | None = None) -> dict | None:
    """If the message looks unusual, run the moderator and return its JSON plan."""
    unusual, signals = looks_unusual(text)
    if not unusual:
        return None

    user_msg = (
        f"Guest in Room {room_no or '?'} ({guest_name or 'unknown'}) wrote: "
        f"\"{text.strip()}\".\nProduce the JSON plan."
    )

    raw = await _call_gateway(MODERATOR_MODEL, _SYSTEM_PROMPT, user_msg)
    if not raw:
        raw = await _call_gateway(MODERATOR_FALLBACK, _SYSTEM_PROMPT, user_msg)
    if not raw:
        return {
            "category": "other",
            "is_directly_doable": False,
            "interpretation": "Moderator unreachable; flagged by keyword match.",
            "concierge_plan": "Acknowledge warmly, ask one clarifying question, offer to research and follow up.",
            "suggested_vendor_or_alternative": "—",
            "tone": "warm",
            "ethical_flag": "advisory",
            "signals": signals,
            "model_used": "fallback_template",
        }

    plan = _extract_json(raw)
    if not plan:
        return {
            "category": "other",
            "is_directly_doable": False,
            "interpretation": "(moderator returned unparseable output)",
            "concierge_plan": "Acknowledge warmly, suggest a legitimate option.",
            "suggested_vendor_or_alternative": "—",
            "tone": "warm",
            "ethical_flag": "advisory",
            "signals": signals,
            "model_used": MODERATOR_MODEL,
            "raw": raw[:400],
        }

    plan["signals"] = signals
    plan["model_used"] = MODERATOR_MODEL
    return plan


def format_for_agent(plan: dict) -> str:
    """Compact moderation context block for the Concierge issue body."""
    if not plan:
        return ""
    lines = ["MODERATOR ADVICE (internal — do not show to guest verbatim):"]
    lines.append(f"  • Category: {plan.get('category')}")
    lines.append(f"  • Interpretation: {plan.get('interpretation')}")
    lines.append(f"  • Directly doable: {plan.get('is_directly_doable')}")
    lines.append(f"  • Plan: {plan.get('concierge_plan')}")
    lines.append(f"  • Suggest: {plan.get('suggested_vendor_or_alternative')}")
    lines.append(f"  • Tone: {plan.get('tone')}  · ethical_flag: {plan.get('ethical_flag')}")
    return "\n".join(lines)
