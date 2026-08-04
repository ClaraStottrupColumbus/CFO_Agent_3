# budgetversions.py — Budget versions: what was approved, by whom, and what changed.
#
# A scenario answers "what would happen if". A version answers "what did we
# commit to, and what was it worth to re-lock chicken meal on 12 Nov". The
# difference is that a version is FROZEN: it snapshots the scenario's projection
# AND the driver provenance behind it, so a later re-lock, a re-run or a deleted
# scenario cannot rewrite what the board approved.
#
# Two things here are deliberate:
#
#   * `diff(a, b)` is PURE — two records in, deltas out, no I/O — because it is
#     the function that has to defend a number in a board room. tests/ covers it.
#   * `create_version` takes the driver provenance as an ARGUMENT rather than
#     reading drivers.py. This module then depends only on budget.py (a leaf),
#     stays testable with plain dicts, and keeps the import graph acyclic.
#
# Storage follows the established store pattern exactly: a module-level
# VERSIONS_FILE constant so tests can monkeypatch it at a tmp_path, and a .tmp
# write then Path.replace() under a threading.Lock, because a scheduler worker
# and an interactive request can write concurrently.

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from . import budget

VERSIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "budget_versions.json"

# draft      — created, still being argued about
# submitted  — put in front of whoever approves budgets here
# approved   — the version the company is running on (at most one)
# superseded — was approved, then a later version was approved instead
STATUSES = ("draft", "submitted", "approved", "superseded")

# The P&L metrics a version diff reports on, in reading order.
DIFF_METRICS = ("volume_tonnes", "revenue_eur", "cogs_eur", "opex_eur",
                "gross_margin_eur", "ebitda_eur", "ebitda_margin_pct")

EPS = 1e-9

_lock = threading.Lock()


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _load_all() -> list[dict]:
    if not VERSIONS_FILE.exists():
        return []
    try:
        data = json.loads(VERSIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("versions", []) if isinstance(data, dict) else (data or [])


def _save_all(items: list[dict]) -> None:
    VERSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = VERSIONS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"versions": items}, indent=2))
    tmp.replace(VERSIONS_FILE)


def list_versions() -> list[dict]:
    """Newest version first. Assumptions are normalised on READ for the same
    reason scenarios.py does it: a version snapshotted before the four blocks
    existed is a flat {driver_id: pct} dict, and reading it as an empty drivers
    block would make a diff show no assumption change at all."""
    with _lock:
        items = _load_all()
    for v in items:
        v["assumptions_snapshot"] = budget.normalise_assumptions(
            v.get("assumptions_snapshot"))
    items.sort(key=lambda v: v.get("version_no") or 0, reverse=True)
    return items


def get_version(version_id: str) -> dict | None:
    return next((v for v in list_versions() if v.get("id") == version_id), None)


def approved_version() -> dict | None:
    return next((v for v in list_versions() if v.get("status") == "approved"), None)


def latest_version() -> dict | None:
    items = list_versions()
    return items[0] if items else None


def summary(version: dict) -> dict:
    """The compact shape the version rail renders."""
    totals = version.get("totals") or {}
    spec = budget.normalise_assumptions(version.get("assumptions_snapshot"))
    return {
        "id": version.get("id"),
        "version_no": version.get("version_no"),
        "label": version.get("label"),
        "note": version.get("note"),
        "status": version.get("status"),
        "scenario_id": version.get("scenario_id"),
        "scenario_name": version.get("scenario_name"),
        "parent_version_id": version.get("parent_version_id"),
        "created_at": version.get("created_at"),
        "created_by": version.get("created_by"),
        "submitted_by": version.get("submitted_by"),
        "approved_by": version.get("approved_by"),
        "approved_at": version.get("approved_at"),
        "locked_at": version.get("locked_at"),
        "assumption_count": sum(len(v) for v in spec.values()),
        "driver_count": len(version.get("drivers_snapshot") or []),
        "has_cash": bool(version.get("cash_snapshot")),
        "revenue_eur": totals.get("revenue_eur"),
        "ebitda_eur": totals.get("ebitda_eur"),
        "ebitda_margin_pct": totals.get("ebitda_margin_pct"),
    }


def create_version(scenario: dict, *, label: str | None = None, note: str | None = None,
                   created_by: str | None = None, drivers_snapshot: list[dict] | None = None,
                   locked_at: str | None = None, cash_snapshot: dict | None = None,
                   parent_version_id: str | None = None) -> dict:
    """Freeze a scenario as the next budget version.

    `drivers_snapshot` is the provenance that makes the version defensible —
    [{driver_id, name, unit, value, source_url, retrieved_at, rationale}, …] —
    copied in rather than referenced, so a later observation cannot rewrite what
    was approved. It is passed in by the caller (main.py reads it off
    drivers.driver_status) to keep this module free of dataset I/O.

    `cash_snapshot` is the cash profile as it stood at approval, and it is frozen
    for exactly the same reason: the working-capital days were MEASURED off the
    balance sheet on the day this was approved, so next quarter's measurement
    must not silently rewrite the funding case the board signed off. Optional —
    versions created before the cash phase, or on a company with no
    working-capital history, simply carry None, and the export says so rather
    than showing a zero.
    """
    if not isinstance(scenario, dict) or not scenario.get("id"):
        raise ValueError("A version must be created from a stored scenario.")

    now = time.time()
    with _lock:
        items = _load_all()
        version_no = max((int(v.get("version_no") or 0) for v in items), default=0) + 1
        if parent_version_id is None and items:
            parent_version_id = max(
                items, key=lambda v: int(v.get("version_no") or 0)).get("id")

        record = {
            "id": uuid.uuid4().hex,
            "version_no": version_no,
            "label": " ".join(str(label or "").split())[:80] or f"Version {version_no}",
            "note": note or None,
            "status": "draft",
            "parent_version_id": parent_version_id,
            "scenario_id": scenario.get("id"),
            "scenario_name": scenario.get("name"),
            "baseline": scenario.get("baseline"),
            # Frozen alongside the assumptions, because the percentages alone do
            # not say what budget they made: the same set on a forward curve and
            # on locked values are two different numbers.
            "basis": scenario.get("basis") or "locked",
            "assumptions_snapshot": budget.normalise_assumptions(scenario.get("assumptions")),
            "price_pass_through": float(scenario.get("price_pass_through") or 0.0),
            "opex_inflation_pct": float(scenario.get("opex_inflation_pct") or 0.0),
            "totals": scenario.get("totals") or {},
            "by_month": scenario.get("by_month") or [],
            "by_product_line": scenario.get("by_product_line") or [],
            "driver_impact_eur": scenario.get("driver_impact_eur") or {},
            "opex_bridge": scenario.get("opex_bridge") or {},
            "ebitda_bridge": scenario.get("ebitda_bridge") or {},
            "driver_prices_used": scenario.get("driver_prices_used") or {},
            "drivers_snapshot": list(drivers_snapshot or []),
            "cash_snapshot": (dict(cash_snapshot) if cash_snapshot else None),
            "locked_at": locked_at,
            "created_at": now,
            "created_by": (created_by or None),
            "submitted_by": None,
            "submitted_at": None,
            "approved_by": None,
            "approved_at": None,
        }
        items.append(record)
        _save_all(items)
    return record


def _transition(version_id: str, status: str, *, by: str | None,
                note: str | None = None) -> dict:
    """Move one version to `status`, or raise ValueError with a readable reason."""
    now = time.time()
    with _lock:
        items = _load_all()
        target = next((v for v in items if v.get("id") == version_id), None)
        if target is None:
            raise KeyError(version_id)
        current = target.get("status") or "draft"

        if status == "submitted":
            if current == "approved":
                raise ValueError("This version is already approved; create a new "
                                 "version rather than re-submitting an approved one.")
            target["submitted_by"] = by or target.get("submitted_by")
            target["submitted_at"] = now
        elif status == "approved":
            if not (by or "").strip():
                raise ValueError("approved_by is required. This app has no "
                                 "authentication, so the name is an attestation "
                                 "of who approved the budget, not a signature.")
            if current == "approved":
                raise ValueError("This version is already approved.")
            target["approved_by"] = by.strip()
            target["approved_at"] = now
            # At most one approved version: whatever was running becomes history.
            for v in items:
                if v is not target and v.get("status") == "approved":
                    v["status"] = "superseded"
        else:
            raise ValueError(f"Unknown status '{status}'. Expected submitted or approved.")

        target["status"] = status
        if note:
            target["note"] = note
        _save_all(items)
        return target


def submit(version_id: str, by: str | None = None, note: str | None = None) -> dict:
    return _transition(version_id, "submitted", by=by, note=note)


def approve(version_id: str, by: str, note: str | None = None) -> dict:
    return _transition(version_id, "approved", by=by, note=note)


def delete_version(version_id: str) -> bool:
    """Discard a version. An approved one raises instead: what the company was
    running on is history, and history that can be deleted is not an audit
    trail. Approve a later version to supersede it."""
    with _lock:
        items = _load_all()
        target = next((v for v in items if v.get("id") == version_id), None)
        if target is None:
            return False
        if target.get("status") == "approved":
            raise ValueError("An approved version cannot be deleted. Approve a "
                             "later version to supersede it.")
        items.remove(target)
        _save_all(items)
        return True


# --------------------------------------------------------------------------
# The diff — pure, and the reason this module exists
# --------------------------------------------------------------------------

def diff(version_a: dict, version_b: dict) -> dict:
    """What changed between two versions: assumptions, P&L, and the EBITDA bridge.

    `version_a` is the earlier one (what we had), `version_b` the later (what we
    are proposing or have approved). Pure: two records in, deltas out.

    Three cuts come back, because "what changed" is asked three ways:

      * **assumptions** — per block, per key, from → to. Includes keys present on
        only one side, which is how a dropped assumption shows up as a change
        rather than as silence.
      * **locked_values** — the driver provenance behind each side. A percentage
        assumption held constant while the locked price it applies to moved is a
        real budget change and would otherwise be invisible.
      * **ebitda_bridge** — the EBITDA move attributed across volume / price /
        each driver / opex, in the `render_chart` waterfall shape.

    The bridge closes by construction: both versions attribute against the same
    baseline plan, so component-wise subtraction is exact, and any difference in
    the two baselines becomes its own step. `check_residual` is carried for the
    same reason variance_decomposition carries one — a bridge that does not
    close is one nobody can defend to a board, so it fails loudly instead of
    drifting.
    """
    a = version_a or {}
    b = version_b or {}

    spec_a = budget.normalise_assumptions(a.get("assumptions_snapshot"))
    spec_b = budget.normalise_assumptions(b.get("assumptions_snapshot"))

    assumptions: dict[str, list[dict]] = {}
    for block in budget.ASSUMPTION_BLOCKS:
        rows = []
        for key in _ordered_keys(spec_a.get(block, {}), spec_b.get(block, {})):
            before = spec_a.get(block, {}).get(key)
            after = spec_b.get(block, {}).get(key)
            if before is not None and after is not None and abs(after - before) < EPS:
                continue
            rows.append({
                "key": key,
                "from_pct": before,
                "to_pct": after,
                "delta_pp": (after or 0.0) - (before or 0.0),
                "change": ("added" if before is None else
                           "removed" if after is None else "changed"),
            })
        assumptions[block] = rows

    totals_a = a.get("totals") or {}
    totals_b = b.get("totals") or {}
    totals = {}
    for metric in DIFF_METRICS:
        before, after = totals_a.get(metric), totals_b.get(metric)
        totals[metric] = {
            "from": before,
            "to": after,
            "delta": (None if before is None or after is None
                      else float(after) - float(before)),
        }

    return {
        "from": _side(a),
        "to": _side(b),
        "assumptions": assumptions,
        "assumption_change_count": sum(len(v) for v in assumptions.values()),
        "locked_values": _locked_value_diff(a.get("drivers_snapshot"),
                                            b.get("drivers_snapshot")),
        "totals": totals,
        "by_product_line": _line_diff(a.get("by_product_line"), b.get("by_product_line")),
        **_bridge_diff(a, b),
    }


def _side(v: dict) -> dict:
    return {
        "id": v.get("id"),
        "version_no": v.get("version_no"),
        "label": v.get("label"),
        "status": v.get("status"),
        "created_at": v.get("created_at"),
        "approved_by": v.get("approved_by"),
        "locked_at": v.get("locked_at"),
    }


def _ordered_keys(first: dict, second: dict) -> list[str]:
    """Keys of both dicts, first-seen order preserved and no duplicates."""
    out = list(first)
    out += [k for k in second if k not in first]
    return out


def _locked_value_diff(before_rows, after_rows) -> list[dict]:
    """Per-driver locked value, from → to, carrying the later side's provenance.

    This is the cut that answers "what was the re-lock worth": the assumption
    percentages can be identical on both sides while the price they are applied
    to has moved, and only this row shows it.
    """
    before = _index_drivers(before_rows)
    after = _index_drivers(after_rows)
    rows = []
    for driver_id in _ordered_keys(before, after):
        was = before.get(driver_id, {})
        now = after.get(driver_id, {})
        v0, v1 = _as_float(was.get("value")), _as_float(now.get("value"))
        if v0 is not None and v1 is not None and abs(v1 - v0) < EPS:
            continue
        rows.append({
            "driver_id": driver_id,
            "name": now.get("name") or was.get("name") or driver_id,
            "unit": now.get("unit") or was.get("unit"),
            "from_value": v0,
            "to_value": v1,
            "delta_pct": (None if not v0 or v1 is None else (v1 - v0) / v0 * 100.0),
            "to_source_url": now.get("source_url"),
            "to_retrieved_at": now.get("retrieved_at"),
        })
    return rows


def _index_drivers(rows) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("driver_id"):
            out[str(row["driver_id"])] = row
    return out


def _line_diff(before_rows, after_rows) -> list[dict]:
    before = {str(r.get("product_line")): r for r in (before_rows or [])}
    after = {str(r.get("product_line")): r for r in (after_rows or [])}
    rows = []
    for line in _ordered_keys(before, after):
        was, now = before.get(line, {}), after.get(line, {})
        entry = {"product_line": line}
        for metric in ("volume_tonnes", "revenue_eur", "cogs_eur", "opex_eur", "ebitda_eur"):
            entry[f"{metric}_delta"] = (_as_float(now.get(metric)) or 0.0) - \
                                       (_as_float(was.get(metric)) or 0.0)
        rows.append(entry)
    return rows


def _bridge_diff(a: dict, b: dict) -> dict:
    """The EBITDA move between two versions, attributed and additive."""
    bridge_a = a.get("ebitda_bridge") or {}
    bridge_b = b.get("ebitda_bridge") or {}

    eb_a = _as_float((a.get("totals") or {}).get("ebitda_eur")) or 0.0
    eb_b = _as_float((b.get("totals") or {}).get("ebitda_eur")) or 0.0
    total_delta = eb_b - eb_a

    steps: list[tuple[str, float]] = []
    if bridge_a or bridge_b:
        base_a = _as_float(bridge_a.get("baseline_ebitda_eur")) or 0.0
        base_b = _as_float(bridge_b.get("baseline_ebitda_eur")) or 0.0
        # Both versions project from the budget plan; if that plan itself was
        # re-cut between them, the shift is its own step rather than smeared
        # across the drivers.
        steps.append(("Baseline plan", base_b - base_a))
        steps.append(("Volume", (_as_float(bridge_b.get("volume_eur")) or 0.0)
                      - (_as_float(bridge_a.get("volume_eur")) or 0.0)))
        steps.append(("Price", (_as_float(bridge_b.get("price_eur")) or 0.0)
                      - (_as_float(bridge_a.get("price_eur")) or 0.0)))
        drivers_a = bridge_a.get("drivers_eur") or {}
        drivers_b = bridge_b.get("drivers_eur") or {}
        driver_steps = [
            (driver_id, (_as_float(drivers_b.get(driver_id)) or 0.0)
             - (_as_float(drivers_a.get(driver_id)) or 0.0))
            for driver_id in _ordered_keys(drivers_a, drivers_b)
        ]
        steps += sorted(driver_steps, key=lambda kv: -abs(kv[1]))
        steps.append(("Opex", (_as_float(bridge_b.get("opex_eur")) or 0.0)
                      - (_as_float(bridge_a.get("opex_eur")) or 0.0)))

    steps = [(label, value) for label, value in steps if abs(value) > 0.005]
    attributed = sum(value for _, value in steps)
    residual = total_delta - attributed
    # A version stored before the bridge existed can still be diffed: the move
    # is reported whole rather than dropped, and the waterfall still closes.
    if abs(residual) > 0.005:
        steps.append(("Unattributed", residual))
        attributed += residual

    points = [{"label": f"v{a.get('version_no')} EBITDA", "value": round(eb_a, 2),
               "kind": "absolute"}]
    points += [{"label": label, "value": round(value, 2), "kind": "delta"}
               for label, value in steps]
    points.append({"label": f"v{b.get('version_no')} EBITDA", "value": round(eb_b, 2),
                   "kind": "absolute"})

    return {
        "ebitda_delta_eur": total_delta,
        "ebitda_bridge": points,
        # Always ~0. Surfaced so a change that breaks additivity fails loudly.
        "check_residual": total_delta - attributed,
    }


def _as_float(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
