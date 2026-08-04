# scenarios.py — Persisted budget scenarios.
#
# build_budget_scenario MUST persist: without a stored scenario the monthly
# revision has nothing to revise against and the EBITDA-floor rule has no object
# to evaluate. Scenario persistence is load-bearing, not a convenience.
#
# Creation stays conversational — the agent writes a scenario through a tool
# during a revision — so this module is only persistence plus the
# re-run-with-latest-prices path. There is deliberately no scenario-authoring
# form anywhere in the app.

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from . import budget

SCENARIOS_FILE = Path(__file__).resolve().parent.parent / "data" / "scenarios.json"

_lock = threading.Lock()


def _load_all() -> list[dict]:
    if not SCENARIOS_FILE.exists():
        return []
    try:
        data = json.loads(SCENARIOS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("scenarios", []) if isinstance(data, dict) else (data or [])


def _save_all(items: list[dict]) -> None:
    SCENARIOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCENARIOS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"scenarios": items}, indent=2))
    tmp.replace(SCENARIOS_FILE)


def list_scenarios() -> list[dict]:
    with _lock:
        items = _load_all()
    # Normalise on READ, not just on write. Scenarios saved before the volume /
    # price / opex blocks existed are flat {driver_id: pct} dicts; without this
    # they would read as an empty drivers block and silently project the
    # baseline — a re-run that looks like it worked and shows no shock at all.
    for s in items:
        s["assumptions"] = budget.normalise_assumptions(s.get("assumptions"))
    items.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return items


def get_scenario(scenario_id: str) -> dict | None:
    return next((s for s in list_scenarios() if s.get("id") == scenario_id), None)


def get_active() -> dict | None:
    """The scenario the budget currently rests on — what the monthly revision
    compares against and what the EBITDA-floor rule evaluates."""
    items = list_scenarios()
    return next((s for s in items if s.get("active")), None) or (items[0] if items else None)


def save_scenario(fields: dict) -> dict:
    """Create or replace a scenario. Returns the stored record."""
    name = " ".join(str(fields.get("name") or "").split()) or "Untitled scenario"
    now = time.time()
    scenario_id = fields.get("id") or uuid.uuid4().hex

    record = {
        "id": scenario_id,
        "name": name[:80],
        "note": fields.get("note") or None,
        "baseline": fields.get("baseline") or "budget",
        "assumptions": budget.normalise_assumptions(fields.get("assumptions")),
        # What the drivers were priced at before the percentages applied. Older
        # records predate the field and are all locked-basis by construction.
        "basis": fields.get("basis") or "locked",
        "price_pass_through": float(fields.get("price_pass_through") or 0.0),
        "opex_inflation_pct": float(fields.get("opex_inflation_pct") or 0.0),
        "totals": fields.get("totals") or {},
        "by_month": fields.get("by_month") or [],
        "by_product_line": fields.get("by_product_line") or [],
        "driver_impact_eur": fields.get("driver_impact_eur") or {},
        "opex_bridge": fields.get("opex_bridge") or {},
        "ebitda_bridge": fields.get("ebitda_bridge") or {},
        "driver_prices_used": fields.get("driver_prices_used") or {},
        "source_records": fields.get("source_records") or [],
        "created_at": now,
        "updated_at": now,
        "active": bool(fields.get("active", False)),
    }

    with _lock:
        items = _load_all()
        existing = next((s for s in items if s.get("id") == scenario_id), None)
        if existing:
            record["created_at"] = existing.get("created_at", now)
            record["active"] = bool(fields.get("active", existing.get("active", False)))
            items[items.index(existing)] = record
        else:
            items.append(record)
        if record["active"]:
            for s in items:
                if s.get("id") != scenario_id:
                    s["active"] = False
        # The first scenario ever saved becomes the active one, so the monthly
        # revision and the EBITDA-floor rule always have something to evaluate.
        elif not any(s.get("active") for s in items):
            record["active"] = True
        _save_all(items)
    return record


def set_active(scenario_id: str) -> dict | None:
    with _lock:
        items = _load_all()
        target = next((s for s in items if s.get("id") == scenario_id), None)
        if not target:
            return None
        for s in items:
            s["active"] = s.get("id") == scenario_id
        target["updated_at"] = time.time()
        _save_all(items)
        return target


def delete_scenario(scenario_id: str) -> bool:
    with _lock:
        items = _load_all()
        target = next((s for s in items if s.get("id") == scenario_id), None)
        if not target:
            return False
        was_active = target.get("active")
        items.remove(target)
        if was_active and items:
            items[0]["active"] = True
        _save_all(items)
        return True


def summary(scenario: dict) -> dict:
    """The compact shape the scenarios list view and the home screen render."""
    totals = scenario.get("totals") or {}
    spec = budget.normalise_assumptions(scenario.get("assumptions"))
    return {
        "id": scenario.get("id"),
        "name": scenario.get("name"),
        "note": scenario.get("note"),
        "active": bool(scenario.get("active")),
        "basis": scenario.get("basis") or "locked",
        "updated_at": scenario.get("updated_at"),
        # Across all four blocks — a volume-only scenario is not "0 assumptions".
        "assumption_count": sum(len(v) for v in spec.values()),
        "revenue_eur": totals.get("revenue_eur"),
        "ebitda_eur": totals.get("ebitda_eur"),
        "ebitda_margin_pct": totals.get("ebitda_margin_pct"),
    }
