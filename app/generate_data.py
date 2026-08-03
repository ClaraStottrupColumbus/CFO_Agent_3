# generate_data.py — Seeded demo datasets for the budgeting agent.
#
# Deterministic (RNG_SEED = 42) so every run of the demo tells the same story
# and the numbers in a report can be reproduced by hand from the CSVs. Written
# as both CSV and Parquet: tools prefer Parquet, tests read the CSVs.
#
# Reporting currency is EUR, but several inputs are quoted in USD — driver_prices
# stores the price in the driver's OWN quote currency and the tools convert via
# the eur_usd driver. That is deliberate: it makes the FX story real arithmetic
# the model must delegate rather than a cosmetic label.
#
# Unit price and unit cost are NOT stored. They are derived in tools.py as
# revenue/volume and cogs/volume, so there is one source of truth — which is
# what makes the variance decomposition trustworthy.
#
# The story hooks (§5 of the implementation plan):
#   * chicken meal +28% (USD) over six months, assumption locked in September
#     -> drift rule fires; Poultry Feed uses the most per tonne, so its unit cost
#        rises ~9% against a ~3% selling-price increase.
#   * sea freight +40% over two months, only 15% hedged -> biggest exposure.
#   * wheat -8% -> a partial offset, so the news is not uniformly bad.
#   * EUR weakening vs USD -> soybean meal costs more in EUR on a flat USD price.
#   * Premium Pet volume +12% vs budget -> a non-trivial mix effect.
#   * Adriatic opened mid-2026 with no prior-year budget -> "the data isn't there".
#   * No payroll dataset at all -> the agent must say so, not invent wage detail.

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

RNG_SEED = 42
ANCHOR = "2026-12"          # last closed month
HISTORY_MONTHS = 36         # 2024-01 .. 2026-12
BUDGET_YEAR = 2027
LOCK_MONTH = "2026-09"      # when the CFO froze the 2027 assumptions

# --------------------------------------------------------------------------
# Static catalog
# --------------------------------------------------------------------------

PRODUCT_LINES = [
    # name, segment, pack format, target margin %, base monthly tonnes
    #
    # List prices are NOT hard-coded. They are derived in generate() as
    # unit_cost(LOCK_MONTH) / (1 - target_margin), i.e. the CFO priced each line
    # to its target margin when the budget was locked. That keeps every line
    # solvent by construction — hand-picked prices left Aqua Feed underwater
    # once the fish-meal path was applied — and it makes the subsequent margin
    # erosion the *story* rather than a seeding accident.
    ("Poultry Feed",           "Livestock",   "25kg bag", 14.0, 4200.0),
    ("Swine Feed",             "Livestock",   "25kg bag", 13.0, 3100.0),
    ("Cattle Feed",            "Livestock",   "bulk",     12.0, 2600.0),
    ("Premium Pet Nutrition",  "Companion",   "12kg bag", 28.0,  620.0),
    ("Aqua Feed",              "Aquaculture", "20kg bag", 19.0,  880.0),
]

REGIONS = [
    # name, currency, demand factor, price factor, opened (None = always)
    ("Iberia",         "EUR", 0.30, 1.00, None),
    ("Nordics",        "EUR", 0.22, 1.06, None),
    ("Benelux",        "EUR", 0.24, 1.02, None),
    ("Central Europe", "EUR", 0.18, 0.97, None),
    ("Adriatic",       "EUR", 0.06, 0.99, "2026-07"),   # no prior-year budget
]

# driver_id, name, category, unit, quote ccy, baseline, hedge, adverse dir,
# stale days, search hint
DRIVERS = [
    ("chicken_meal",   "Chicken meal",      "ingredient", "USD/t",    "USD",  520.0, 0.30, "up",  7,
     "chicken meal poultry by-product meal price index Europe"),
    ("wheat",          "Feed wheat",        "ingredient", "EUR/t",    "EUR",  245.0, 0.45, "up",  7,
     "feed wheat price EUR per tonne Matif"),
    ("soybean_meal",   "Soybean meal",      "ingredient", "USD/t",    "USD",  430.0, 0.25, "up",  7,
     "soybean meal CBOT price USD per tonne"),
    ("maize",          "Maize",             "ingredient", "EUR/t",    "EUR",  210.0, 0.35, "up",  7,
     "feed maize corn price EUR per tonne Europe"),
    ("rapeseed_meal",  "Rapeseed meal",     "ingredient", "EUR/t",    "EUR",  265.0, 0.20, "up",  7,
     "rapeseed meal price EUR per tonne Europe"),
    ("fish_meal",      "Fish meal",         "ingredient", "USD/t",    "USD", 1580.0, 0.10, "up", 14,
     "fish meal price index Peru USD per tonne"),
    ("vitamin_premix", "Vitamin premix",    "ingredient", "EUR/kg",   "EUR",    6.40, 0.00, "up", 30,
     "feed vitamin premix price index"),
    ("sea_freight",    "Sea freight",       "logistics",  "USD/FEU",  "USD", 1850.0, 0.15, "up",  7,
     "container freight rate index FEU Europe"),
    ("road_freight",   "Road freight",      "logistics",  "EUR/1000km", "EUR", 780.0, 0.00, "up", 14,
     "European road freight rate index"),
    ("energy_gas",     "Natural gas",       "energy",     "EUR/MWh",  "EUR",   38.0, 0.50, "up",  3,
     "TTF natural gas price EUR per MWh"),
    ("energy_power",   "Electricity",       "energy",     "EUR/MWh",  "EUR",   95.0, 0.40, "up",  3,
     "European wholesale electricity price EUR per MWh"),
    ("packaging_film", "Packaging film",    "packaging",  "EUR/t",    "EUR", 1450.0, 0.00, "up", 30,
     "polyethylene packaging film price index Europe"),
    ("eur_usd",        "EUR/USD rate",      "fx",         "USD per EUR", "USD", 1.12, 0.00, "down", 1,
     "EUR USD exchange rate"),
    ("wage_inflation", "Wage inflation",    "labour",     "%/yr",     "EUR",    3.4, 0.00, "up", 30,
     "eurozone manufacturing wage growth rate"),
]

# product_line -> {driver_id: qty per tonne of finished feed, in the driver's unit}
BOM = {
    "Poultry Feed": {
        "chicken_meal": 0.220, "wheat": 0.340, "soybean_meal": 0.180, "maize": 0.150,
        "vitamin_premix": 4.0, "packaging_film": 0.0060,
        "energy_gas": 0.090, "energy_power": 0.050,
        "sea_freight": 0.0120, "road_freight": 0.0016,
    },
    "Swine Feed": {
        "wheat": 0.420, "maize": 0.200, "soybean_meal": 0.160, "rapeseed_meal": 0.120,
        "chicken_meal": 0.030, "vitamin_premix": 3.2, "packaging_film": 0.0055,
        "energy_gas": 0.080, "energy_power": 0.045,
        "sea_freight": 0.0090, "road_freight": 0.0016,
    },
    "Cattle Feed": {
        "wheat": 0.280, "maize": 0.240, "rapeseed_meal": 0.220, "soybean_meal": 0.140,
        "vitamin_premix": 2.6, "packaging_film": 0.0015,
        "energy_gas": 0.070, "energy_power": 0.040,
        "sea_freight": 0.0070, "road_freight": 0.0020,
    },
    "Premium Pet Nutrition": {
        "chicken_meal": 0.300, "fish_meal": 0.100, "wheat": 0.120, "maize": 0.100,
        "vitamin_premix": 18.0, "packaging_film": 0.0220,
        "energy_gas": 0.140, "energy_power": 0.095,
        "sea_freight": 0.0180, "road_freight": 0.0034,
    },
    "Aqua Feed": {
        "fish_meal": 0.320, "soybean_meal": 0.200, "wheat": 0.140,
        "vitamin_premix": 12.0, "packaging_film": 0.0140,
        "energy_gas": 0.120, "energy_power": 0.082,
        "sea_freight": 0.0260, "road_freight": 0.0022,
    },
}

# Conversion cost the BOM does not capture (labour, maintenance, plant overhead).
CONVERSION_COST_PER_TONNE = {
    "Poultry Feed": 44.0, "Swine Feed": 41.0, "Cattle Feed": 38.0,
    "Premium Pet Nutrition": 205.0, "Aqua Feed": 96.0,
}

COST_CENTRES = [
    # name, monthly base EUR, headcount, driver link
    ("Production",          410_000.0, 148, "wage_inflation"),
    ("Logistics",           268_000.0,  61, "road_freight"),
    ("Sales & Marketing",   224_000.0,  47, "wage_inflation"),
    ("R&D / Nutrition",     151_000.0,  26, "wage_inflation"),
    ("Quality & Compliance", 96_000.0,  19, "wage_inflation"),
    ("G&A",                 178_000.0,  31, "wage_inflation"),
]


# --------------------------------------------------------------------------
# Month helpers
# --------------------------------------------------------------------------

def _month_range(start: str, count: int) -> list[str]:
    y, m = (int(p) for p in start.split("-"))
    out = []
    for _ in range(count):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _months_between(a: str, b: str) -> int:
    ay, am = (int(p) for p in a.split("-"))
    by, bm = (int(p) for p in b.split("-"))
    return (by - ay) * 12 + (bm - am)


HISTORY = _month_range("2024-01", HISTORY_MONTHS)          # .. 2026-12
BUDGET_MONTHS = _month_range(f"{BUDGET_YEAR}-01", 12)
ALL_MONTHS = HISTORY + BUDGET_MONTHS


# --------------------------------------------------------------------------
# Driver price paths — where the story hooks live
# --------------------------------------------------------------------------

def _build_driver_prices(rng: np.random.Generator) -> dict[str, dict[str, float]]:
    """{driver_id: {month: price in the driver's quote currency}} for HISTORY."""
    series: dict[str, dict[str, float]] = {}
    n = len(HISTORY)

    for (did, _name, _cat, _unit, _ccy, base, *_rest) in DRIVERS:
        drift, season, noise = _shape(did)
        path = {}
        for i, month in enumerate(HISTORY):
            t = i / max(1, n - 1)
            seasonal = 1.0 + season * math.sin(2 * math.pi * (i % 12) / 12.0)
            wobble = 1.0 + float(rng.normal(0.0, noise))
            path[month] = base * (1.0 + drift * t) * seasonal * wobble
        series[did] = path

    _apply_story_hooks(series)
    return series


def _shape(driver_id: str) -> tuple[float, float, float]:
    """(total drift over the whole history, seasonal amplitude, monthly noise)."""
    return {
        "chicken_meal":   (0.06, 0.020, 0.012),
        "wheat":          (0.04, 0.045, 0.014),
        "soybean_meal":   (0.02, 0.030, 0.011),
        "maize":          (0.03, 0.040, 0.013),
        "rapeseed_meal":  (0.05, 0.035, 0.013),
        "fish_meal":      (0.12, 0.025, 0.016),
        "vitamin_premix": (0.08, 0.010, 0.008),
        "sea_freight":    (0.05, 0.060, 0.030),
        "road_freight":   (0.09, 0.015, 0.010),
        "energy_gas":     (0.02, 0.180, 0.035),
        "energy_power":   (0.04, 0.120, 0.030),
        "packaging_film": (0.07, 0.020, 0.010),
        "eur_usd":        (0.00, 0.008, 0.006),
        "wage_inflation": (0.15, 0.000, 0.010),
    }.get(driver_id, (0.03, 0.02, 0.012))


def _ramp(series: dict[str, float], driver_id: str, start_month: str,
          total_pct: float) -> None:
    """Ramp a driver by total_pct between start_month and the anchor, smoothly,
    scaling every later month so the shape of the noise survives."""
    path = series[driver_id]
    span = _months_between(start_month, ANCHOR)
    if span <= 0:
        return
    for month, value in list(path.items()):
        step = _months_between(start_month, month)
        if step <= 0:
            continue
        frac = min(1.0, step / span)
        path[month] = value * (1.0 + total_pct * frac)


def _apply_story_hooks(series: dict[str, dict[str, float]]) -> None:
    # Chicken meal +28% (USD) over the last six months — the headline drift.
    _ramp(series, "chicken_meal", "2026-06", 0.28)
    # Sea freight +40% over the last two months, and it is only 15% hedged.
    _ramp(series, "sea_freight", "2026-10", 0.40)
    # Fish meal +14% since the lock — a second driver over the drift threshold.
    _ramp(series, "fish_meal", LOCK_MONTH, 0.14)
    # Wheat -8% — the partial offset, so the news is not uniformly bad.
    _ramp(series, "wheat", "2026-06", -0.08)
    # EUR weakening vs USD: 1.12 -> ~1.035. Soybean meal is flat in USD, so its
    # EUR cost rises purely on FX — arithmetic the model must never do itself.
    _ramp(series, "eur_usd", "2026-03", -0.075)


# --------------------------------------------------------------------------
# Cost model
# --------------------------------------------------------------------------

def _eur(driver_id: str, month: str, series: dict, catalog: dict) -> float:
    """A driver's price converted into EUR for a given month."""
    price = series[driver_id][month]
    if catalog[driver_id]["quote_currency"] != "USD" or driver_id == "eur_usd":
        return price
    return price / series["eur_usd"][month]


def _unit_cost(line: str, month: str, series: dict, catalog: dict) -> float:
    """EUR cost per tonne of finished feed: the bill of materials plus conversion."""
    total = CONVERSION_COST_PER_TONNE[line]
    for did, qty in BOM[line].items():
        total += qty * _eur(did, month, series, catalog)
    return total


def _price_index(month: str) -> float:
    """Selling-price multiplier for a month.

    Two components: a slow ~2% baseline drift across the whole history, plus a
    ~3% list-price increase pushed through over the last six months. The second
    is the point — it is only a partial pass-through of a ~9% cost move, which
    is what produces the negative price effect the variance decomposition is
    meant to surface.
    """
    i = ALL_MONTHS.index(month) if month in ALL_MONTHS else 0
    baseline = 1.0 + 0.020 * (i / max(1, len(ALL_MONTHS) - 1))
    step = _months_between("2026-06", month)
    span = _months_between("2026-06", ANCHOR)
    passthrough = 1.0 + 0.030 * min(1.0, max(0.0, step / span))
    return baseline * passthrough


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def generate(profile: dict | None = None) -> dict:
    """Write every dataset. `profile` (optional) lets the confirmed company
    profile rename the product lines; the shape of the data is unchanged."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(RNG_SEED)

    catalog = {d[0]: {"driver_id": d[0], "name": d[1], "category": d[2], "unit": d[3],
                      "quote_currency": d[4], "baseline": d[5], "hedge_coverage": d[6],
                      "adverse_direction": d[7], "stale_after_days": d[8],
                      "search_hint": d[9]} for d in DRIVERS}
    series = _build_driver_prices(rng)
    list_prices = _derive_list_prices(series, catalog)

    _write(pd.DataFrame([
        {"product_line": n, "segment": s, "pack_format": p,
         "list_price_eur_per_tonne": list_prices[n], "target_margin_pct": tm}
        for (n, s, p, tm, _v) in PRODUCT_LINES
    ]), "product_lines")

    _write(pd.DataFrame(list(catalog.values())), "drivers")

    _write(pd.DataFrame([
        {"product_line": line, "driver_id": did, "qty_per_tonne": qty,
         "unit": catalog[did]["unit"]}
        for line, items in BOM.items() for did, qty in items.items()
    ]), "bill_of_materials")

    _write(_driver_prices_frame(series, catalog), "driver_prices")
    _write(_budget_vs_actuals_frame(rng, series, catalog, list_prices), "budget_vs_actuals")
    _write(_opex_frame(rng, series), "opex_plan")

    refresh_overview()
    _seed_locked_assumptions(series, catalog)
    _seed_baseline_scenario(series, catalog)
    return {"months": len(HISTORY), "drivers": len(DRIVERS),
            "product_lines": len(PRODUCT_LINES), "data_dir": str(DATA_DIR)}


def _seed_locked_assumptions(series: dict, catalog: dict) -> None:
    """Freeze the September positions the 2027 budget was built on.

    Without this the drift rule has no reference point and the whole premise of
    the app — "your assumption has moved since you locked it" — has nothing to
    compare against on a fresh install. Values are in each driver's OWN quote
    currency, matching driver_prices.price, so the comparison is unit-consistent.
    """
    from . import drivers as drivers_mod

    assumptions = {
        did: {
            "value": round(float(series[did][LOCK_MONTH]), 4),
            "unit": catalog[did]["unit"],
            "source_url": "",
            "rationale": f"Locked for the {BUDGET_YEAR} budget at the {LOCK_MONTH} market level.",
        }
        for did in series
    }
    drivers_mod.lock_assumptions(assumptions, scenario_id=None,
                                 note=f"{BUDGET_YEAR} budget lock ({LOCK_MONTH})")


def _seed_baseline_scenario(series: dict, catalog: dict) -> None:
    """The as-locked 2027 budget, stored as the active scenario.

    Assumptions are all-zero: this IS the budget, so the scenario engine must
    reproduce it exactly (the first property test_scenario_engine asserts).
    """
    from . import budget as budget_mod
    from . import scenarios as scenarios_mod

    bva = _read("budget_vs_actuals")
    plan = bva[bva["month"].str.startswith(str(BUDGET_YEAR))]
    baseline_rows = [
        {"month": r["month"], "product_line": r["product_line"],
         "volume_tonnes": float(r["volume_tonnes_budget"] or 0.0),
         "revenue_eur": float(r["revenue_budget_eur"] or 0.0),
         "cogs_eur": float(r["cogs_budget_eur"] or 0.0),
         "opex_eur": float(r["opex_budget_eur"] or 0.0)}
        for _, r in plan.iterrows() if pd.notna(r["revenue_budget_eur"])
    ]
    bom_rows = [{"product_line": line, "driver_id": did, "qty_per_tonne": qty}
                for line, items in BOM.items() for did, qty in items.items()]
    lock_prices = {did: _eur(did, LOCK_MONTH, series, catalog) for did in series}

    projection = budget_mod.project_pnl(baseline_rows, bom_rows, lock_prices, {})
    scenarios_mod.save_scenario({
        "name": f"{BUDGET_YEAR} budget (as locked)",
        "note": f"The plan as frozen in {LOCK_MONTH}, before any driver drift.",
        "baseline": f"budget_{BUDGET_YEAR}",
        "assumptions": {},
        "price_pass_through": 0.35,
        "totals": projection["totals"],
        "by_month": projection["by_month"],
        "by_product_line": projection["by_product_line"],
        "driver_prices_used": lock_prices,
        "active": True,
    })


def _budget_price_month(month: str) -> str:
    """A year's budget prices are frozen at the previous September's lock."""
    return f"{int(month[:4]) - 1:04d}-09"


def _derive_list_prices(series: dict, catalog: dict) -> dict[str, float]:
    """Price each line to its target margin at the lock month."""
    out = {}
    for (name, _seg, _pack, target, _vol) in PRODUCT_LINES:
        cost = _unit_cost(name, LOCK_MONTH, series, catalog)
        out[name] = round(cost / (1.0 - target / 100.0), 2)
    return out


def _driver_prices_frame(series: dict, catalog: dict) -> pd.DataFrame:
    """Append-only observation log. `revision` is 0 for every seeded row; the
    agent's writes append revision 1, 2, … and never mutate this history.

    recorded_at is set relative to *now* rather than to the data anchor, so the
    freshness view is meaningful whenever the demo is run. Two drivers are
    deliberately left stale.
    """
    now = datetime.now(timezone.utc)
    stale_drivers = {"vitamin_premix", "packaging_film"}
    rows = []
    for did, path in series.items():
        meta = catalog[did]
        for month, price in path.items():
            is_latest = month == ANCHOR
            if is_latest:
                age = timedelta(days=31 if did in stale_drivers else 0, hours=6)
            else:
                age = timedelta(days=30 * (_months_between(month, ANCHOR) + 1))
            rows.append({
                "month": month,
                "driver_id": did,
                "price": round(float(price), 4),
                "currency": meta["quote_currency"],
                "source": "seed_history",
                "source_url": "",
                "revision": 0,
                "recorded_at": (now - age).isoformat(timespec="seconds"),
                "note": "",
            })
    return pd.DataFrame(rows).sort_values(["driver_id", "month"]).reset_index(drop=True)


def _budget_vs_actuals_frame(rng, series: dict, catalog: dict,
                             list_prices: dict) -> pd.DataFrame:
    """month x product line x region, actuals and budget.

    Budget unit costs are frozen at the LOCK_MONTH driver prices — that is what
    makes the drift meaningful. 2027 rows carry budget only (no actuals yet):
    it is the year being planned.
    """
    rows = []
    lock_costs = {line: _unit_cost(line, LOCK_MONTH, series, catalog) for line in BOM}

    for (line, _seg, _pack, _tm, base_vol) in PRODUCT_LINES:
        list_price = list_prices[line]
        for (region, _ccy, demand, price_factor, opened) in REGIONS:
            for month in ALL_MONTHS:
                if opened and _months_between(opened, month) < 0:
                    continue
                is_budget_year = month in BUDGET_MONTHS
                i = ALL_MONTHS.index(month)
                seasonal = 1.0 + 0.07 * math.sin(2 * math.pi * ((i % 12) - 2) / 12.0)
                growth = 1.0 + 0.035 * (i / 12.0)

                budget_vol = base_vol * demand * seasonal * growth
                # Premium Pet outperforms its plan — the mix-effect hook.
                out_perf = 1.12 if line == "Premium Pet Nutrition" else 1.0
                actual_vol = (budget_vol * out_perf
                              * (1.0 + float(rng.normal(0.0, 0.028))))

                cost_month = month if not is_budget_year else ANCHOR
                unit_cost_actual = _unit_cost(line, cost_month, series, catalog)
                unit_cost_budget = lock_costs[line]

                # Selling price passes through only part of the cost move:
                # ~3% actual price against ~9% actual cost on Poultry Feed.
                # The BUDGET price is frozen at the prior autumn's lock, because
                # a budget set in September cannot know about a price increase
                # pushed through the following June. Revenue variance is then a
                # volume/mix story and COGS variance a price story — which is
                # exactly what makes the decomposition worth showing.
                unit_price_budget = (list_price * price_factor
                                     * _price_index(_budget_price_month(month)))
                unit_price_actual = (list_price * price_factor * _price_index(month)
                                     * (1.0 + float(rng.normal(0.0, 0.006))))

                opex_rate = 0.052
                row = {
                    "month": month, "product_line": line, "region": region,
                    "volume_tonnes_budget": round(budget_vol, 1),
                    "revenue_budget_eur": round(budget_vol * unit_price_budget, 2),
                    "cogs_budget_eur": round(budget_vol * unit_cost_budget, 2),
                    "opex_budget_eur": round(budget_vol * unit_price_budget * opex_rate, 2),
                }
                if is_budget_year:
                    row |= {"volume_tonnes_actual": np.nan, "revenue_actual_eur": np.nan,
                            "cogs_actual_eur": np.nan, "opex_actual_eur": np.nan}
                else:
                    row |= {
                        "volume_tonnes_actual": round(actual_vol, 1),
                        "revenue_actual_eur": round(actual_vol * unit_price_actual, 2),
                        "cogs_actual_eur": round(actual_vol * unit_cost_actual, 2),
                        "opex_actual_eur": round(actual_vol * unit_price_actual * opex_rate, 2),
                    }
                # Adriatic opened after the budget was set: it has actuals from
                # 2026-07 but no 2026 budget at all. "The data isn't there."
                if region == "Adriatic" and not is_budget_year:
                    row |= {"volume_tonnes_budget": np.nan, "revenue_budget_eur": np.nan,
                            "cogs_budget_eur": np.nan, "opex_budget_eur": np.nan}
                rows.append(row)

    cols = ["month", "product_line", "region",
            "volume_tonnes_actual", "volume_tonnes_budget",
            "revenue_actual_eur", "revenue_budget_eur",
            "cogs_actual_eur", "cogs_budget_eur",
            "opex_actual_eur", "opex_budget_eur"]
    return pd.DataFrame(rows)[cols].sort_values(
        ["month", "product_line", "region"]).reset_index(drop=True)


def _opex_frame(rng, series: dict) -> pd.DataFrame:
    """Cost centre plan. Carries headcount, but there is deliberately NO payroll
    dataset anywhere — wage-rate questions must hit "that data isn't available"."""
    rows = []
    for month in ALL_MONTHS:
        i = ALL_MONTHS.index(month)
        for (centre, base, heads, driver_id) in COST_CENTRES:
            wage_idx = 1.0 + 0.034 * (i / 12.0)
            amount = base * wage_idx * (1.0 + float(rng.normal(0.0, 0.012)))
            rows.append({
                "month": month, "cost_centre": centre,
                "amount_eur": round(amount, 2),
                "amount_budget_eur": round(base * wage_idx, 2),
                "headcount": int(round(heads * (1.0 + 0.02 * (i / 12.0)))),
                "driver_id": driver_id,
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# The overview KPI file (CSV on purpose — exercises both readers)
# --------------------------------------------------------------------------

def refresh_overview(jitter: float = 0.0) -> dict:
    """Recompute budget_overview.csv from the stable tables plus the latest
    driver observations. Pure pandas, no LLM — this is what the built-in
    data_refresh task runs every hour."""
    bva = _read("budget_vs_actuals")
    closed = bva[bva["revenue_actual_eur"].notna()]
    latest = closed["month"].max() if len(closed) else ANCHOR
    cur = closed[closed["month"] == latest]

    rev_a = float(cur["revenue_actual_eur"].sum()) * (1.0 + jitter)
    rev_b = float(cur["revenue_budget_eur"].sum(min_count=1) or 0.0)
    cogs_a = float(cur["cogs_actual_eur"].sum()) * (1.0 + jitter)
    opex_a = float(cur["opex_actual_eur"].sum())
    vol_a = float(cur["volume_tonnes_actual"].sum())

    gross = rev_a - cogs_a
    ebitda = gross - opex_a

    ytd = closed[closed["month"].str.startswith(latest[:4])]
    ytd_rev = float(ytd["revenue_actual_eur"].sum()) * (1.0 + jitter)

    rows = [
        ("latest_closed_month", latest, "", ""),
        ("group_revenue_eur", round(rev_a, 2), "EUR", latest),
        ("group_revenue_budget_eur", round(rev_b, 2), "EUR", latest),
        ("revenue_vs_budget_pct",
         round((rev_a - rev_b) / rev_b * 100.0, 2) if rev_b else "", "%", latest),
        ("group_cogs_eur", round(cogs_a, 2), "EUR", latest),
        ("gross_margin_eur", round(gross, 2), "EUR", latest),
        ("gross_margin_pct", round(gross / rev_a * 100.0, 2) if rev_a else "", "%", latest),
        ("ebitda_eur", round(ebitda, 2), "EUR", latest),
        ("ebitda_margin_pct", round(ebitda / rev_a * 100.0, 2) if rev_a else "", "%", latest),
        ("volume_tonnes", round(vol_a, 1), "t", latest),
        ("blended_unit_cost_eur_per_tonne",
         round(cogs_a / vol_a, 2) if vol_a else "", "EUR/t", latest),
        ("ytd_revenue_eur", round(ytd_rev, 2), "EUR", f"{latest[:4]} YTD"),
        ("budget_year", BUDGET_YEAR, "", ""),
    ]
    refreshed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    df = pd.DataFrame(rows, columns=["kpi", "value", "unit", "period"])
    df["last_refreshed_utc"] = refreshed

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / "budget_overview.csv.tmp"
    df.to_csv(tmp, index=False)
    tmp.replace(DATA_DIR / "budget_overview.csv")
    return {"refreshed_utc": refreshed, "latest_closed_month": latest,
            "rows": len(df), "source_file": "budget_overview.csv"}


# --------------------------------------------------------------------------

def _write(df: pd.DataFrame, name: str) -> None:
    """Both formats for every table: tools prefer Parquet, tests read CSV."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(DATA_DIR / f"{name}.csv", index=False)
    df.to_parquet(DATA_DIR / f"{name}.parquet", index=False)


def _read(name: str) -> pd.DataFrame:
    pq = DATA_DIR / f"{name}.parquet"
    if pq.exists():
        return pd.read_parquet(pq)
    return pd.read_csv(DATA_DIR / f"{name}.csv")


if __name__ == "__main__":
    info = generate()
    print(f"Wrote datasets to {info['data_dir']}: "
          f"{info['months']} months, {info['drivers']} drivers, "
          f"{info['product_lines']} product lines.")
