"""Staff identity + role resolution + department mapping."""
from __future__ import annotations

import re
from typing import Optional

import store

# Departments (must match Paperclip projects + dept_group registry)
DEPT_ROOMS = "rooms"
DEPT_FNB = "fnb"
DEPT_ENGINEERING = "engineering"
DEPT_COMMERCIAL = "commercial"
DEPT_MANAGEMENT = "management"
DEPT_SECURITY = "security"
DEPT_SPA = "spa"
DEPT_HR = "hr"
DEPT_FINANCE = "finance"

ALL_DEPTS = [
    DEPT_ROOMS, DEPT_FNB, DEPT_ENGINEERING, DEPT_COMMERCIAL,
    DEPT_MANAGEMENT, DEPT_SECURITY, DEPT_SPA, DEPT_HR, DEPT_FINANCE,
]

DEPT_LABEL = {
    DEPT_ROOMS: "Rooms",
    DEPT_FNB: "Food & Beverage",
    DEPT_ENGINEERING: "Engineering",
    DEPT_COMMERCIAL: "Commercial",
    DEPT_MANAGEMENT: "Management",
    DEPT_SECURITY: "Security",
    DEPT_SPA: "Spa & Wellness",
    DEPT_HR: "Human Resources",
    DEPT_FINANCE: "Finance",
}

# Department → primary AI manager-agent (by Paperclip agent.name)
DEPT_AGENT = {
    DEPT_ROOMS: "DOR",
    DEPT_FNB: "D F&B",
    DEPT_ENGINEERING: "Chief Engineer",
    DEPT_COMMERCIAL: "DOSM",
    DEPT_MANAGEMENT: "GM",
    DEPT_SECURITY: "Security Mgr",
    DEPT_SPA: "Spa Mgr",
    DEPT_HR: "HR Director",
    DEPT_FINANCE: "DOF",
}

# Role aliases — what a staff member might type for /iam <role>
ROLE_ALIASES: dict[str, tuple[str, str]] = {
    # alias -> (department, role_title)
    "gm": (DEPT_MANAGEMENT, "General Manager"),
    "general manager": (DEPT_MANAGEMENT, "General Manager"),
    "manager": (DEPT_MANAGEMENT, "Manager"),
    "dor": (DEPT_ROOMS, "Director of Rooms"),
    "director of rooms": (DEPT_ROOMS, "Director of Rooms"),
    "fom": (DEPT_ROOMS, "Front Office Manager"),
    "front office": (DEPT_ROOMS, "Front Office Agent"),
    "front desk": (DEPT_ROOMS, "Front Desk"),
    "reception": (DEPT_ROOMS, "Reception"),
    "concierge": (DEPT_ROOMS, "Concierge"),
    "reservations": (DEPT_ROOMS, "Reservations"),
    "housekeeping": (DEPT_ROOMS, "Housekeeping"),
    "hk": (DEPT_ROOMS, "Housekeeping"),
    "exec hk": (DEPT_ROOMS, "Executive Housekeeper"),
    "executive housekeeper": (DEPT_ROOMS, "Executive Housekeeper"),
    "housekeeping supervisor": (DEPT_ROOMS, "Housekeeping Supervisor"),
    "hk sup": (DEPT_ROOMS, "Housekeeping Supervisor"),
    "engineering": (DEPT_ENGINEERING, "Engineering"),
    "engineer": (DEPT_ENGINEERING, "Engineer"),
    "maintenance": (DEPT_ENGINEERING, "Maintenance"),
    "chief engineer": (DEPT_ENGINEERING, "Chief Engineer"),
    "f&b": (DEPT_FNB, "F&B"),
    "fnb": (DEPT_FNB, "F&B"),
    "food and beverage": (DEPT_FNB, "F&B"),
    "chef": (DEPT_FNB, "Chef"),
    "exec chef": (DEPT_FNB, "Executive Chef"),
    "executive chef": (DEPT_FNB, "Executive Chef"),
    "kitchen": (DEPT_FNB, "Kitchen"),
    "restaurant": (DEPT_FNB, "Restaurant"),
    "server": (DEPT_FNB, "Server"),
    "waiter": (DEPT_FNB, "Server"),
    "bartender": (DEPT_FNB, "Bartender"),
    "sales": (DEPT_COMMERCIAL, "Sales"),
    "marketing": (DEPT_COMMERCIAL, "Marketing"),
    "dosm": (DEPT_COMMERCIAL, "Director of Sales & Marketing"),
    "revenue": (DEPT_COMMERCIAL, "Revenue"),
    "revenue manager": (DEPT_COMMERCIAL, "Revenue Manager"),
    "security": (DEPT_SECURITY, "Security"),
    "loss prevention": (DEPT_SECURITY, "Loss Prevention"),
    "spa": (DEPT_SPA, "Spa"),
    "wellness": (DEPT_SPA, "Wellness"),
    "therapist": (DEPT_SPA, "Therapist"),
    "hr": (DEPT_HR, "HR"),
    "human resources": (DEPT_HR, "HR"),
    "finance": (DEPT_FINANCE, "Finance"),
    "dof": (DEPT_FINANCE, "Director of Finance"),
    "accounting": (DEPT_FINANCE, "Accounting"),
}


def resolve_role(text: str) -> Optional[tuple[str, str]]:
    """Return (department, role_title) for a free-text role string."""
    if not text:
        return None
    norm = re.sub(r"\s+", " ", text.strip().lower())
    if norm in ROLE_ALIASES:
        return ROLE_ALIASES[norm]
    # Try substring match
    for alias, route in ROLE_ALIASES.items():
        if alias in norm:
            return route
    return None


# ---- admin bootstrap --------------------------------------------------------

_BOOTSTRAP_KEY = "admin_bootstrap_user_id"


def is_admin_claimed() -> bool:
    return store.kv_get(_BOOTSTRAP_KEY) is not None


def claim_admin(tg_user_id: str) -> bool:
    """First /start wins admin. Returns True if this call did the claim."""
    if is_admin_claimed():
        return False
    store.kv_set(_BOOTSTRAP_KEY, tg_user_id)
    return True


def _env_admins() -> set[str]:
    """Comma-separated list of Telegram user IDs from BOOTSTRAP_ADMIN_TG_USER_IDS."""
    import os
    raw = os.environ.get("BOOTSTRAP_ADMIN_TG_USER_IDS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_admin(tg_user_id: str) -> bool:
    if not tg_user_id:
        return False
    if tg_user_id in _env_admins():
        return True
    if admin_user_id() == tg_user_id:
        return True
    staff_row = store.get_staff_by_tg_user(tg_user_id)
    return bool(staff_row and staff_row.get("is_admin"))


def admin_user_id() -> "Optional[str]":  # type: ignore[override]
    # Hardcoded admin from env wins, otherwise the bootstrap-claimed one in DB
    envs = _env_admins()
    if envs:
        return sorted(envs)[0]
    return store.kv_get(_BOOTSTRAP_KEY)


# ---- formatting helpers -----------------------------------------------------

def display_name_for(staff: dict) -> str:
    return staff.get("display_name") or staff.get("tg_username") or "Staff"


def dept_label(dept: Optional[str]) -> str:
    return DEPT_LABEL.get(dept or "", dept or "Unknown")


def dept_agent_for(dept: str) -> Optional[str]:
    return DEPT_AGENT.get(dept)
