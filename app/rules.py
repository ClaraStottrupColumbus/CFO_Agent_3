# rules.py — Deterministic budget rules. Pure pandas, never calls the API.
#
# Three threshold rules over stable tables. Detection is entirely API-free —
# only the narrative in alerts.py needs the model, and it falls back to the
# finding title when the network is unavailable.
#
# There were five. `driver_stale` and `scenario_ebitda_floor` were removed
# because both fired on every evaluation and trained the CFO to mute the other
# three. Staleness is a STATE, not an event: nothing happened when a driver
# crossed its limit, and it is already fully visible on the Drivers page as a
# badge, a summary count and a card rule. The EBITDA floor was the same shape —
# a scenario below the floor stays below it until someone acts, so it re-fired
# the same finding every window. What it computed is not lost: the re-run still
# runs, on the home screen, through `_rerun_active_scenario`.
#
# The finding shape is preserved exactly from the reference:
#   {rule_id, entity, period, metric_value, threshold, severity, title}
# URGENCY_ANALYSIS_PROMPT is .format(**finding), so renaming a key breaks prompt
# formatting at runtime rather than at import.

from __future__ import annotations

import json
import threading
from pathlib import Path

import pandas as pd

from . import budget, drivers, scenarios
from .tools import _budget_year, _driver_eur_prices, _load, _opex_plan_rows

RULES_FILE = Path(__file__).resolve().parent.parent / "data" / "rules.json"

# Defaults are set so the seeded story hooks fire: chicken meal, sea freight and
# fish meal have all moved >10% since the September lock (generate_data.py).
DEFAULT_RULES = [
    {"id": "driver_moved_since_lock", "enabled": True, "threshold": 10.0,
     "label": "Driver moved since lock by more than (%)", "direction": "higher_is_worse"},
    {"id": "budget_line_variance", "enabled": True, "threshold": -5.0,
     "label": "Product line revenue vs budget below (%)", "direction": "lower_is_worse"},
    {"id": "unit_cost_breach", "enabled": True, "threshold": 4.0,
     "label": "Unit cost above the locked assumption by (%)", "direction": "higher_is_worse"},
]
RULE_IDS = tuple(r["id"] for r in DEFAULT_RULES)
RULE_DIRECTION = {r["id"]: r["direction"] for r in DEFAULT_RULES}

# Per-rule dedup config (§7). Driver rules carry no period and re-fire after
# 72h; period-bucketed rules keep the period and use a 720h window, which
# reproduces the reference's permanent-per-period behaviour through the same
# code path. One mechanism, two behaviours.
DEDUP = {
    "driver_moved_since_lock": {"window_hours": 72, "include_period": False, "bucket": 5.0},
    "budget_line_variance": {"window_hours": 720, "include_period": True, "bucket": 5.0},
    "unit_cost_breach": {"window_hours": 720, "include_period": True, "bucket": 2.0},
}

_lock = threading.Lock()


def get_rules() -> list[dict]:
    with _lock:
        if RULES_FILE.exists():
            try:
                stored = {r["id"]: r for r in json.loads(RULES_FILE.read_text()).get("rules", [])}
                return [{**d, **{k: v for k, v in stored.get(d["id"], {}).items()
                                 if k in ("threshold", "enabled")}} for d in DEFAULT_RULES]
            except (json.JSONDecodeError, OSError, KeyError, TypeError):
                pass
        return [dict(r) for r in DEFAULT_RULES]


def save_rules(rules: list[dict]) -> list[dict]:
    """Replace the rule set. Only known ids are accepted."""
    by_id = {}
    for r in rules or []:
        rid = r.get("id")
        if rid not in RULE_IDS:
            raise ValueError(f"Unknown rule id '{rid}'.")
        try:
            threshold = float(r.get("threshold"))
        except (TypeError, ValueError):
            raise ValueError(f"Rule '{rid}': threshold must be a number.")
        by_id[rid] = {"threshold": threshold, "enabled": bool(r.get("enabled", True))}
    merged = [{**d, **by_id.get(d["id"], {})} for d in DEFAULT_RULES]
    with _lock:
        RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = RULES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"rules": merged}, indent=2))
        tmp.replace(RULES_FILE)
    return merged


def _severity(value: float, threshold: float, direction: str) -> str:
    """Severity, direction-aware.

    The reference hard-codes `value <= critical_at`, i.e. lower-is-worse. Budget
    drift is higher-is-worse — a driver moving UP 20% is the bad case — so
    copying it as-is would silently mark every drift finding 'warning' and never
    'critical'.
    """
    if direction == "higher_is_worse":
        return "critical" if value >= threshold * 1.5 else "warning"
    return "critical" if value <= threshold * 1.5 else "warning"


def _fmt_eur(v: float) -> str:
    return f"€{v / 1_000_000:.1f}M" if abs(v) >= 1_000_000 else f"€{v / 1_000:.0f}k"


def evaluate(rules: list[dict] | None = None) -> list[dict]:
    """Evaluate every enabled rule. Returns findings; never raises."""
    cfg = {r["id"]: r for r in (rules if rules is not None else get_rules())}
    findings: list[dict] = []

    def enabled(rule_id: str) -> dict | None:
        r = cfg.get(rule_id)
        return r if r and r.get("enabled") else None

    def add(rule_id: str, entity: str, period: str, value: float,
            threshold: float, title: str, context: dict | None = None) -> None:
        findings.append({
            "rule_id": rule_id, "entity": entity, "period": period,
            "metric_value": round(float(value), 1), "threshold": threshold,
            "severity": _severity(value, threshold, RULE_DIRECTION[rule_id]),
            "title": title,
            # Richer context than the reference's fixed key set; alerts.py passes
            # it through so driver unit / hedge coverage / product line survive.
            "context": context or {},
        })

    try:
        bva, _ = _load("budget_vs_actuals")
    except (FileNotFoundError, OSError):
        return findings
    closed = bva[bva["revenue_actual_eur"].notna()]
    latest = str(closed["month"].max()) if len(closed) else ""

    status = drivers.driver_status()

    # ---- 1. A locked assumption has moved ----
    if (r := enabled("driver_moved_since_lock")):
        th = float(r["threshold"])
        for d in status:
            drift = d.get("drift_pct")
            if drift is None or abs(drift) <= th:
                continue
            word = "adverse" if d.get("adverse") else "favourable"
            add("driver_moved_since_lock", d["driver_id"], latest, abs(drift), th,
                f"{d['name']} has moved {abs(drift):.1f}% since the budget was locked "
                f"({word})",
                {"unit": d.get("unit"), "locked_value": d.get("locked_value"),
                 "latest_value": d.get("latest_value"), "adverse": d.get("adverse"),
                 "hedge_coverage": d.get("hedge_coverage"), "category": d.get("category")})

    # ---- 2. A product line is missing its revenue budget ----
    if (r := enabled("budget_line_variance")) and latest:
        th = float(r["threshold"])
        cur = closed[closed["month"] == latest]
        grouped = cur.groupby("product_line", as_index=False)[
            ["revenue_actual_eur", "revenue_budget_eur"]].sum(min_count=1)
        for _, row in grouped.iterrows():
            bud = float(row["revenue_budget_eur"] or 0.0)
            if not bud or pd.isna(row["revenue_budget_eur"]):
                continue    # a region opened mid-year has no budget — not a breach
            v = (float(row["revenue_actual_eur"]) - bud) / bud * 100.0
            if v < th:
                add("budget_line_variance", str(row["product_line"]), latest, v, th,
                    f"{row['product_line']} revenue is {abs(v):.1f}% below budget in {latest}",
                    {"actual_eur": float(row["revenue_actual_eur"]), "budget_eur": bud})

    # ---- 3. Unit cost has breached the locked assumption ----
    # The separation-of-drivers rule: it catches cost inflation that volume
    # growth hides inside the absolute amount.
    if (r := enabled("unit_cost_breach")) and latest:
        th = float(r["threshold"])
        scenario = scenarios.get_active()
        assumed = _assumed_unit_costs(scenario)
        cur = closed[closed["month"] == latest]
        grouped = cur.groupby("product_line", as_index=False)[
            ["cogs_actual_eur", "volume_tonnes_actual"]].sum()
        for _, row in grouped.iterrows():
            line = str(row["product_line"])
            volume = float(row["volume_tonnes_actual"] or 0.0)
            base = assumed.get(line)
            if not base or volume <= 0:
                continue
            actual = float(row["cogs_actual_eur"]) / volume
            v = (actual - base) / base * 100.0
            if v > th:
                add("unit_cost_breach", line, latest, v, th,
                    f"{line} unit cost is €{actual:.0f}/t against the locked €{base:.0f}/t "
                    f"({v:+.1f}%) in {latest}",
                    {"actual_unit_cost": actual, "assumed_unit_cost": base,
                     "scenario": (scenario or {}).get("name")})

    return findings


def _assumed_unit_costs(scenario: dict | None) -> dict:
    """€/tonne per product line implied by the active scenario."""
    if not scenario:
        return {}
    out = {}
    for row in scenario.get("by_product_line") or []:
        volume = float(row.get("volume_tonnes") or 0.0)
        if volume > 0:
            out[str(row.get("product_line"))] = float(row.get("cogs_eur") or 0.0) / volume
    return out


def _rerun_active_scenario() -> dict | None:
    """Re-run the active scenario on the latest observed prices.

    Assumptions are expressed as percentage moves from each driver's LOCKED
    value, so re-running is just "what percentage has each driver actually
    moved" fed through the same engine.

    No rule calls this any more — `scenario_ebitda_floor` was removed. It is not
    dead: `main.budget_state` is its caller, and it is what puts the cumulative
    EBITDA effect of everything that has drifted on the home screen. A standing
    number on a screen is the right home for it; a re-firing alert was not.
    """
    scenario = scenarios.get_active()
    if not scenario:
        return None
    try:
        bva, _ = _load("budget_vs_actuals")
        bom, _ = _load("bill_of_materials")
    except (FileNotFoundError, OSError):
        return None

    year = _budget_year(bva)
    plan = bva[bva["month"].str.startswith(year)]
    if plan.empty:
        return None

    lock_prices = scenario.get("driver_prices_used") or {}
    if not lock_prices:
        return None
    now_prices, _q = _driver_eur_prices()

    moves = {}
    for driver_id, locked in lock_prices.items():
        latest = now_prices.get(driver_id)
        if latest is None or not locked:
            continue
        moves[driver_id] = (float(latest) - float(locked)) / float(locked) * 100.0

    baseline_rows = [
        {"month": str(r["month"]), "product_line": str(r["product_line"]),
         "volume_tonnes": float(r["volume_tonnes_budget"] or 0.0),
         "revenue_eur": float(r["revenue_budget_eur"] or 0.0),
         "cogs_eur": float(r["cogs_budget_eur"] or 0.0),
         "opex_eur": float(r["opex_budget_eur"] or 0.0)}
        for _, r in plan.iterrows() if pd.notna(r["revenue_budget_eur"])
    ]
    bom_rows = [{"product_line": str(r["product_line"]), "driver_id": str(r["driver_id"]),
                 "qty_per_tonne": float(r["qty_per_tonne"])} for _, r in bom.iterrows()]

    catalog = drivers.load_catalog()
    hedges = ({} if catalog.empty else
              dict(zip(catalog["driver_id"].astype(str), catalog["hedge_coverage"].astype(float))))
    tau = float(scenario.get("price_pass_through") or 0.0)

    # Only the DRIVER block is replaced by what the market actually did. The
    # CFO's volume, price and opex decisions are theirs and carry through, or
    # the re-run would silently answer a different question from the scenario.
    stored = budget.normalise_assumptions(scenario.get("assumptions"))
    opex_rows = _opex_plan_rows(year)
    opex_pct = float(scenario.get("opex_inflation_pct") or 0.0)
    baseline = budget.project_pnl(baseline_rows, bom_rows, lock_prices,
                                  {**stored, "drivers": {}}, hedges=hedges,
                                  price_pass_through=tau, opex_rows=opex_rows,
                                  opex_inflation_pct=opex_pct)
    projected = budget.project_pnl(baseline_rows, bom_rows, lock_prices,
                                   {**stored, "drivers": moves}, hedges=hedges,
                                   price_pass_through=tau, opex_rows=opex_rows,
                                   opex_inflation_pct=opex_pct)
    margin = projected["totals"].get("ebitda_margin_pct")
    if margin is None:
        return None
    return {
        "scenario_name": scenario.get("name") or "locked budget",
        "year": year,
        "margin_pct": float(margin),
        "baseline_margin_pct": baseline["totals"].get("ebitda_margin_pct"),
        "ebitda_eur": projected["totals"]["ebitda_eur"],
        "delta_ebitda_eur": projected["totals"]["ebitda_eur"] - baseline["totals"]["ebitda_eur"],
        "moves": moves,
    }
