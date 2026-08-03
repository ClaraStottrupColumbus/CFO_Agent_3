# profile.py — The company profile: what the agent knows about this CFO's business.
#
# Net-new; neither sibling has an onboarding step. Persisted to
# data/company_profile.json with the same merge-over-defaults + atomic-write
# pattern as config.py.
#
# The agent's original `proposal` is kept SEPARATE from the confirmed profile.
# That lets the UI diff the CFO's edits against what was proposed, and lets
# research be re-run without losing their corrections.

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROFILE_FILE = DATA_DIR / "company_profile.json"
DEMO_PROFILE_FILE = DATA_DIR / "demo_profile.json"

DRIVER_CATEGORIES = ("ingredient", "logistics", "energy", "packaging", "fx", "labour", "other")

DEFAULT_PROFILE: dict = {
    "version": 1,
    "setup_complete": False,
    "created_at": 0,
    "updated_at": 0,
    "description": "",
    "company": {"name": "", "industry": "", "reporting_currency": "EUR",
                "fiscal_year_start_month": 1, "budget_year": 2027},
    "product_lines": [],
    "markets": [],
    "cost_drivers": [],
    "proposal": None,
}

_lock = threading.Lock()


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read() -> dict:
    if not PROFILE_FILE.exists():
        return dict(DEFAULT_PROFILE)
    try:
        return _merge(DEFAULT_PROFILE, json.loads(PROFILE_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_PROFILE)


def _write(profile: dict) -> dict:
    profile["updated_at"] = time.time()
    if not profile.get("created_at"):
        profile["created_at"] = profile["updated_at"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROFILE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profile, indent=2))
    tmp.replace(PROFILE_FILE)
    return profile


def get_profile() -> dict:
    with _lock:
        return _read()


def setup_complete() -> bool:
    return bool(get_profile().get("setup_complete"))


# --------------------------------------------------------------------------
# Validation — shared by the proposal tool and the CFO's confirmation
# --------------------------------------------------------------------------

def _clean_driver(raw: dict, *, confirmed: bool) -> dict | str:
    """Return a cleaned driver dict, or an error string naming what is wrong."""
    if not isinstance(raw, dict):
        return "each cost driver must be an object"
    driver_id = str(raw.get("driver_id") or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", driver_id):
        return (f"driver_id {raw.get('driver_id')!r} must be lower_snake_case, "
                f"starting with a letter (e.g. chicken_meal)")
    name = " ".join(str(raw.get("name") or "").split()) or driver_id
    category = str(raw.get("category") or "other").strip().lower()
    if category not in DRIVER_CATEGORIES:
        category = "other"
    direction = str(raw.get("adverse_direction") or "up").strip().lower()
    if direction not in ("up", "down"):
        direction = "up"
    try:
        stale = int(raw.get("stale_after_days") or 7)
    except (TypeError, ValueError):
        stale = 7

    sources = []
    for s in raw.get("sources") or []:
        if isinstance(s, dict) and s.get("url"):
            sources.append({"url": str(s["url"]), "title": s.get("title"),
                            "accessed": s.get("accessed")})
        elif isinstance(s, str):
            sources.append({"url": s, "title": None, "accessed": None})

    return {
        "driver_id": driver_id,
        "name": name[:80],
        "category": category,
        "unit": str(raw.get("unit") or "").strip()[:32],
        "quote_currency": str(raw.get("quote_currency") or "EUR").strip().upper()[:8],
        "why": str(raw.get("why") or "").strip()[:280],
        "search_hint": str(raw.get("search_hint") or "").strip()[:160],
        "sources": sources,
        "current_value": raw.get("current_value"),
        "assumption_value": raw.get("assumption_value"),
        "adverse_direction": direction,
        "stale_after_days": max(1, min(365, stale)),
        "hedge_coverage": _fraction(raw.get("hedge_coverage")),
        "confirmed_by_cfo": bool(confirmed),
    }


def _fraction(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _clean_named(items, extra_key: str | None = None) -> list[dict]:
    out = []
    for item in items or []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())
        if not name:
            continue
        entry = {"name": name[:80]}
        if extra_key:
            entry[extra_key] = item.get(extra_key)
        else:
            entry["note"] = item.get("note")
        out.append(entry)
    return out


def _clean_company(raw: dict, fallback: dict) -> dict:
    raw = raw or {}
    try:
        month = int(raw.get("fiscal_year_start_month") or fallback.get("fiscal_year_start_month") or 1)
    except (TypeError, ValueError):
        month = 1
    try:
        year = int(raw.get("budget_year") or fallback.get("budget_year") or 2027)
    except (TypeError, ValueError):
        year = 2027
    return {
        "name": " ".join(str(raw.get("name") or fallback.get("name") or "").split())[:120],
        "industry": " ".join(str(raw.get("industry") or fallback.get("industry") or "").split())[:120],
        "reporting_currency": str(raw.get("reporting_currency")
                                  or fallback.get("reporting_currency") or "EUR").upper()[:8],
        "fiscal_year_start_month": max(1, min(12, month)),
        "budget_year": max(2000, min(2100, year)),
    }


# --------------------------------------------------------------------------
# The proposal path (called by the propose_watchlist tool)
# --------------------------------------------------------------------------

def save_proposal(raw: dict, *, model: str | None = None,
                  source_records: list | None = None) -> dict:
    """Validate the agent's proposal and store it WITHOUT completing setup.

    Leaves setup_complete false: a proposal is a suggestion, and the CFO's
    confirmation is a separate, deliberate act.
    """
    if not isinstance(raw, dict):
        return {"error": "The proposal must be an object."}
    drivers_in = raw.get("cost_drivers")
    if not isinstance(drivers_in, list) or not drivers_in:
        return {"error": "cost_drivers must be a non-empty list of drivers."}

    cleaned, seen = [], set()
    for entry in drivers_in:
        result = _clean_driver(entry, confirmed=False)
        if isinstance(result, str):
            return {"error": result}
        if not result["sources"]:
            return {"error": f"Driver '{result['driver_id']}' has no source. Every proposed "
                             f"driver needs at least one URL you actually fetched."}
        if result["driver_id"] in seen:
            return {"error": f"Duplicate driver_id '{result['driver_id']}'."}
        seen.add(result["driver_id"])
        cleaned.append(result)

    with _lock:
        profile = _read()
        profile["proposal"] = {
            "generated_at": time.time(),
            "model": model,
            "raw": {
                "company": _clean_company(raw.get("company"), profile.get("company") or {}),
                "product_lines": _clean_named(raw.get("product_lines")),
                "markets": _clean_named(raw.get("markets"), extra_key="currency"),
                "cost_drivers": cleaned,
            },
            "source_records": source_records or [],
        }
        _write(profile)

    # A compact ack — the full proposal is already in the model's context, and
    # the UI reads it off the profile. Same side-channel discipline as
    # render_chart's _chart_spec.
    return {"proposed": True, "drivers": len(cleaned),
            "next_step": "The CFO will review and confirm this watchlist.",
            "source_file": PROFILE_FILE.name}


# --------------------------------------------------------------------------
# The confirmation path (called by POST /api/profile)
# --------------------------------------------------------------------------

def confirm_profile(edited: dict) -> dict:
    """Validate the CFO-edited profile, mark every driver confirmed, and complete setup."""
    if not isinstance(edited, dict):
        raise ValueError("The profile must be an object.")

    drivers_in = edited.get("cost_drivers")
    if not isinstance(drivers_in, list) or not drivers_in:
        raise ValueError("Add at least one cost driver before finishing setup.")

    cleaned, seen = [], set()
    for entry in drivers_in:
        result = _clean_driver(entry, confirmed=True)
        if isinstance(result, str):
            raise ValueError(result)
        if result["driver_id"] in seen:
            raise ValueError(f"Duplicate driver_id '{result['driver_id']}'.")
        seen.add(result["driver_id"])
        cleaned.append(result)

    with _lock:
        profile = _read()
        profile["description"] = str(edited.get("description")
                                     or profile.get("description") or "")[:4000]
        profile["company"] = _clean_company(edited.get("company"), profile.get("company") or {})
        profile["product_lines"] = _clean_named(edited.get("product_lines"))
        profile["markets"] = _clean_named(edited.get("markets"), extra_key="currency")
        profile["cost_drivers"] = cleaned
        profile["setup_complete"] = True
        _write(profile)
        return profile


def set_description(text: str) -> dict:
    """Persist the CFO's free-text description before the research turn runs,
    so a failed proposal never loses what they typed."""
    with _lock:
        profile = _read()
        profile["description"] = str(text or "")[:4000]
        _write(profile)
        return profile


def reset_setup() -> dict:
    """Re-open setup for editing without discarding the profile."""
    with _lock:
        profile = _read()
        profile["setup_complete"] = False
        _write(profile)
        return profile


def load_demo_profile() -> dict | None:
    """The bundled animal-feed persona, for the no-API-key offline path."""
    if not DEMO_PROFILE_FILE.exists():
        return None
    try:
        return json.loads(DEMO_PROFILE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def public_view(profile: dict | None = None) -> dict:
    """What GET /api/profile returns. `setup_complete` is spelled exactly this
    way end to end — the frontend gate reads the same key, and a mismatch would
    be a permanent, undiagnosable lock-out rather than a leak."""
    p = profile or get_profile()
    return {
        "setup_complete": bool(p.get("setup_complete")),
        "description": p.get("description") or "",
        "company": p.get("company") or {},
        "product_lines": p.get("product_lines") or [],
        "markets": p.get("markets") or [],
        "cost_drivers": p.get("cost_drivers") or [],
        "proposal": p.get("proposal"),
        "updated_at": p.get("updated_at"),
    }
