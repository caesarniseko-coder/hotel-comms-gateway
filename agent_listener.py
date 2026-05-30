"""Active-participation logic — for each non-command group message, decide
whether an AI manager should respond, and if so, route to the right one.
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional, Tuple

import staff

# (chat_id, agent_name) → last fire timestamp
_throttle: dict[tuple[str, str], float] = {}
THROTTLE_SEC_PER_AGENT = int(os.environ.get("AGENT_LISTEN_THROTTLE_SEC", "60"))
GROUP_MIN_LEN = int(os.environ.get("GROUP_MIN_LEN", "8"))

# keyword → (department, agent_name, priority)
KEYWORDS: list[tuple[re.Pattern, str, str, Optional[str]]] = [
    # complaints / escalations — highest priority
    (re.compile(r"\b(complaint|complain|angry|upset|refund|threat|lawyer|press|media|trip ?advisor|review bomb)\b", re.I),
     staff.DEPT_MANAGEMENT, "GM", "high"),
    # safety / security
    (re.compile(r"\b(fire|smoke|alarm|evacuat\w*|injur\w*|medical|ambulance|police|incident|theft|stolen|fight|harass)\b", re.I),
     staff.DEPT_SECURITY, "Security Mgr", "high"),
    # engineering / maintenance
    (re.compile(r"\b(ac|aircon|air ?conditioning|hvac|heat|cooling|leak|plumb\w*|water|toilet|drain|sink|electric\w*|power|outlet|bulb|light|tv|television|wifi|wi-?fi|internet|router|fridge|minibar|safe|door|lock|key card|broken|not working|out of order|repair|fix|maintenance|engineer)\b", re.I),
     staff.DEPT_ENGINEERING, "Chief Engineer", None),
    # housekeeping
    (re.compile(r"\b(housekeep\w*|clean\w*|laundry|linen|towel|bedding|pillow|amenit\w*|turndown|vacant|ooo|oos|deep clean|stayover|departure)\b", re.I),
     staff.DEPT_ROOMS, "Executive Housekeeper", None),
    # front office / check-in
    (re.compile(r"\b(check[- ]?in|check[- ]?out|late checkout|early arrival|key|room ?key|reception|front desk|folio|guest list|arrival list)\b", re.I),
     staff.DEPT_ROOMS, "Front Office Mgr", None),
    # reservations
    (re.compile(r"\b(booking|reservation|rate|allotment|group block|wholesaler|ota|booking ?\.com|expedia|agoda|cancellation|no[- ]?show)\b", re.I),
     staff.DEPT_ROOMS, "Reservations Mgr", None),
    # f&b kitchen
    (re.compile(r"\b(menu|allergen|allerg\w*|dietary|halal|kosher|vegan|gluten|kitchen|chef|prep|cover|86\b|special meal)\b", re.I),
     staff.DEPT_FNB, "Exec Chef", None),
    # f&b service / restaurant / room service / food in general
    (re.compile(r"\b(food|meal|hungry|snack|room service|in[- ]room dining|order|delivery|where('| i)s my (food|order|meal))\b", re.I),
     staff.DEPT_FNB, "Restaurant Mgr", None),
    (re.compile(r"\b(restaurant|breakfast|lunch|dinner|bar|server|waiter|wine|cocktail|reservation book|cover count)\b", re.I),
     staff.DEPT_FNB, "Restaurant Mgr", None),
    # spa
    (re.compile(r"\b(spa|massage|wellness|sauna|gym|pool|therap\w*)\b", re.I),
     staff.DEPT_SPA, "Spa Mgr", None),
    # commercial
    (re.compile(r"\b(campaign|promo\w*|pickup|pace|revpar|adr|forecast|comp ?set|yield|bar level|mlos|cta)\b", re.I),
     staff.DEPT_COMMERCIAL, "Revenue Mgr", None),
    (re.compile(r"\b(corporate|mice|group rfp|sales lead|partnership|agent|wholesale)\b", re.I),
     staff.DEPT_COMMERCIAL, "DOSM", None),
    # finance
    (re.compile(r"\b(billing|invoice|charge|folio|refund|payment|credit|debit|cash|ar |a/r|ap |a/p|cost|p&?l|budget)\b", re.I),
     staff.DEPT_FINANCE, "DOF", None),
    # HR
    (re.compile(r"\b(roster|shift|staff\w*|training|onboarding|leave|sick|labour|labor|payroll|harass|grievance)\b", re.I),
     staff.DEPT_HR, "HR Director", None),
    # concierge / guest experience
    (re.compile(r"\b(recommend\w*|restaurant nearby|attraction|tour|driver|taxi|airport|transfer|booking advice|vip|repeat guest|loyalty)\b", re.I),
     staff.DEPT_ROOMS, "Concierge", None),
]


def classify_by_keywords(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (department, agent_name, priority) or (None, None, None)."""
    if not text:
        return None, None, None
    # First pass — pick the highest-priority match
    matches = []
    for pat, dept, agent, pri in KEYWORDS:
        if pat.search(text):
            matches.append((dept, agent, pri))
    if not matches:
        return None, None, None
    # Priority order: high > medium > low > None
    pri_rank = {"high": 0, "urgent": 0, "medium": 1, "low": 2, None: 3}
    matches.sort(key=lambda m: pri_rank.get(m[2], 3))
    return matches[0]


def should_respond(*, chat_id: str, agent_name: str, text: str) -> bool:
    if not text or len(text.strip()) < GROUP_MIN_LEN:
        return False
    key = (chat_id, agent_name)
    now = time.time()
    last = _throttle.get(key, 0.0)
    if now - last < THROTTLE_SEC_PER_AGENT:
        return False
    _throttle[key] = now
    return True


def is_mention(text: str, bot_username: str) -> bool:
    if not bot_username:
        return False
    return f"@{bot_username.lower()}" in (text or "").lower()
