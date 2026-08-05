# budgetplan.py — "the Budget": ONE config-driven read on next year's budget,
# in two input modes.
#
#   * BOM mode — `bill_of_materials` and `budget_vs_actuals` exist, so the
#     variables are MATERIALISED from them: every ingredient priced through the
#     bill of materials, every cost centre off the opex plan, each carrying the
#     `driver_id` it came from. Provenance, locking, versioning and export all
#     reach these lines through that id.
#   * Simple mode — no datasets, so the user types the cost lines themselves,
#     seeded from an industry default set. This is what lets the page work on a
#     fresh install, and a `driver_id` can still be attached by hand, which is
#     the migration path from one mode to the other.
#
# The engine is the same either way: `derive()` is pure, takes no I/O, and does
# the whole page — ranking, deltas, totals, margin. Only the source of
# `variables[]` differs. That is what "one budget model" means here; before
# Phase 3 this module read no dataset at all and the app shipped two budgets for
# two different companies.
#
# It has its OWN `configured` flag and is NOT behind main.require_setup, so the
# page is reachable while the rest of the app is still gated on #/setup — in
# simple mode there is nothing for a profile to gate.
#
# There is deliberately NO chat loop in here any more. It had its own small
# streaming turn while it was tool-less and citation-less; now that its lines
# carry driver ids, a question about one is a question about the real
# watchlist, so the page hands off to `reporting.run_session_turn` like
# everything else in the app. `budget_outlook` in tools.py is how the agent
# reads this page.
#
# Dataset reads are imported lazily, inside the two functions that need them.
# Everything above `derive()` stays importable with no pandas and no data
# directory, which is what keeps tests/test_budget_plan.py fixture-free.

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path

from .agent import get_client

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLAN_FILE = DATA_DIR / "budget_plan.json"

_lock = threading.Lock()

# A variable whose expected change is inside this band reads as "flat" rather
# than as a rounding-noise "up". Expressed on the percentage the user typed, not
# on the derived delta, so the label matches the field they can see.
FLAT_EPS_PCT = 0.05

# The overview shows this many before "show all". The brief says 6-8; 8 gives
# the long-tail industries a fuller first screen without becoming a wall.
TOP_N = 8

MAX_PCT = 100.0
NARRATIVE_MAX_TOKENS = 400

CATEGORIES = ("people", "materials", "logistics", "facilities",
              "technology", "commercial", "professional", "other")

SIZES = ("micro", "small", "mid", "large", "enterprise")

SIZE_LABELS = {
    "micro": "Micro (under 10 people)",
    "small": "Small (10–49)",
    "mid": "Mid-market (50–249)",
    "large": "Large (250–999)",
    "enterprise": "Enterprise (1000+)",
}

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")


# --------------------------------------------------------------------------
# The shared variable catalogue
# --------------------------------------------------------------------------
#
# One definition per cost line, reused across industries, so "Energy & utilities"
# means the same thing and carries the same explanation whichever industry the
# user picks. An industry then only chooses WHICH lines apply and how big they
# are — the wording never forks.

CATALOGUE: dict[str, dict] = {
    "salaries": {
        "label": "Salaries & wages",
        "category": "people",
        "description": "Gross pay for permanent employees, before employer taxes and benefits.",
    },
    "payroll_oncosts": {
        "label": "Employer taxes & benefits",
        "category": "people",
        "description": "Employer social contributions, pension, healthcare and other on-costs "
                       "that move with the salary base.",
    },
    "contractors": {
        "label": "Contractors & temporary staff",
        "category": "people",
        "description": "Day-rate and agency labour used to flex capacity without adding headcount.",
    },
    "recruitment_training": {
        "label": "Recruitment & training",
        "category": "people",
        "description": "Hiring fees, onboarding and the learning budget.",
    },
    "raw_materials": {
        "label": "Raw materials",
        "category": "materials",
        "description": "The input commodities and components consumed in what you sell.",
    },
    "goods_for_resale": {
        "label": "Goods for resale",
        "category": "materials",
        "description": "Landed cost of finished stock bought in and sold on.",
    },
    "packaging": {
        "label": "Packaging",
        "category": "materials",
        "description": "Primary and secondary packaging, including film, board and labels.",
    },
    "inventory_shrinkage": {
        "label": "Shrinkage & write-offs",
        "category": "materials",
        "description": "Stock lost to damage, theft, expiry or obsolescence.",
    },
    "freight_inbound": {
        "label": "Inbound freight",
        "category": "logistics",
        "description": "Moving materials and stock from suppliers to you.",
    },
    "freight_outbound": {
        "label": "Outbound freight & delivery",
        "category": "logistics",
        "description": "Getting finished goods to customers, including last-mile carriage.",
    },
    "warehousing": {
        "label": "Warehousing & storage",
        "category": "logistics",
        "description": "Third-party storage, handling and pick-pack fees.",
    },
    "fuel_fleet": {
        "label": "Fuel",
        "category": "logistics",
        "description": "Diesel, petrol and electricity consumed by owned or leased vehicles.",
    },
    "vehicle_costs": {
        "label": "Fleet leasing & maintenance",
        "category": "logistics",
        "description": "Lease payments, servicing, tyres and roadside cover for the fleet.",
    },
    "rent": {
        "label": "Rent & property costs",
        "category": "facilities",
        "description": "Lease payments, service charges and business rates on premises.",
    },
    "energy": {
        "label": "Energy & utilities",
        "category": "facilities",
        "description": "Electricity, gas, water and waste — the lines most exposed to wholesale "
                       "market moves.",
    },
    "maintenance": {
        "label": "Maintenance & repairs",
        "category": "facilities",
        "description": "Keeping plant, equipment and premises in working order.",
    },
    "equipment": {
        "label": "Equipment & tooling",
        "category": "facilities",
        "description": "Consumable tooling plus small equipment expensed rather than capitalised.",
    },
    "cloud_hosting": {
        "label": "Cloud & hosting",
        "category": "technology",
        "description": "Compute, storage, bandwidth and managed services from cloud providers.",
    },
    "software_licences": {
        "label": "Software & licences",
        "category": "technology",
        "description": "SaaS subscriptions and per-seat licences across the business.",
    },
    "it_support": {
        "label": "IT hardware & support",
        "category": "technology",
        "description": "Endpoints, networking and the managed-service contracts behind them.",
    },
    "marketing": {
        "label": "Marketing & advertising",
        "category": "commercial",
        "description": "Paid media, content, events and brand spend.",
    },
    "sales_commission": {
        "label": "Sales commission",
        "category": "commercial",
        "description": "Variable compensation that scales with booked revenue.",
    },
    "merchant_fees": {
        "label": "Payment & merchant fees",
        "category": "commercial",
        "description": "Card processing, gateway and marketplace commission on each transaction.",
    },
    "travel": {
        "label": "Travel & entertainment",
        "category": "commercial",
        "description": "Flights, accommodation, mileage and client entertainment.",
    },
    "professional_fees": {
        "label": "Professional fees",
        "category": "professional",
        "description": "Audit, legal, tax and advisory retainers.",
    },
    "insurance": {
        "label": "Insurance",
        "category": "professional",
        "description": "Property, liability, cyber and credit cover.",
    },
    "compliance": {
        "label": "Compliance & certification",
        "category": "professional",
        "description": "Regulatory filings, audits, certifications and the work behind them.",
    },
    "subcontractors": {
        "label": "Subcontractors",
        "category": "professional",
        "description": "Specialist trades and delivery partners engaged per project.",
    },
    "clinical_supplies": {
        "label": "Clinical & medical supplies",
        "category": "materials",
        "description": "Consumables, devices and pharmacy stock used in care delivery.",
    },
    "other_overheads": {
        "label": "Other overheads",
        "category": "other",
        "description": "The remaining general and administrative spend not itemised elsewhere.",
    },
}


def _v(var_id: str, share: float, change: float, note: str) -> dict:
    """One industry default: which catalogue line, how big, how it's expected to move."""
    return {"id": var_id, "share_of_revenue_pct": share,
            "expected_change_pct": change, "default_note": note}


# Shares are percent OF REVENUE, so they sum to less than 100 — the remainder is
# the operating margin. They are starting points the user is expected to correct,
# which is why every one of them arrives in an editable field rather than being
# computed behind the scenes.

INDUSTRIES: dict[str, dict] = {
    "manufacturing": {
        "label": "Manufacturing & industrial",
        "defaults": [
            _v("raw_materials", 38.0, 3.0, "Assumes input commodities track general producer-price inflation with no contract renegotiation."),
            _v("salaries", 18.0, 3.5, "Assumes a cost-of-living settlement at the going market rate, headcount flat."),
            _v("payroll_oncosts", 4.5, 3.5, "Moves with the salary base; no change to statutory rates assumed."),
            _v("energy", 5.0, 6.0, "Assumes wholesale energy stays above the pre-2022 baseline; no new hedge."),
            _v("freight_inbound", 3.5, 4.0, "Assumes container and road rates firm modestly on tight capacity."),
            _v("freight_outbound", 3.0, 4.0, "Assumes carrier rate cards rise with fuel and driver pay."),
            _v("maintenance", 2.5, 3.0, "Planned maintenance schedule unchanged; parts inflation only."),
            _v("packaging", 2.5, 2.5, "Board and film prices assumed broadly stable."),
            _v("rent", 2.0, 3.0, "Assumes indexed lease uplifts on existing sites, no relocation."),
            _v("equipment", 1.5, 2.5, "Consumable tooling only; capital projects excluded."),
            _v("insurance", 1.0, 5.0, "Assumes continued hardening in property and liability cover."),
            _v("professional_fees", 0.8, 3.5, "Audit and advisory retainers renewed on similar scope."),
            _v("other_overheads", 3.0, 3.0, "General inflation on the residual G&A base."),
        ],
    },
    "retail_ecommerce": {
        "label": "Retail & e-commerce",
        "defaults": [
            _v("goods_for_resale", 52.0, 2.5, "Assumes supplier cost prices rise with inflation and the buying mix is unchanged."),
            _v("salaries", 12.0, 4.0, "Assumes statutory minimum-wage uplift flows through the store and warehouse base."),
            _v("payroll_oncosts", 3.0, 4.0, "Moves with the salary base."),
            _v("rent", 6.0, 3.0, "Assumes indexed lease uplifts across the estate, no openings or closures."),
            _v("marketing", 5.0, 5.0, "Assumes paid-media CPMs keep climbing at current pace to hold share of voice."),
            _v("freight_outbound", 4.0, 4.5, "Assumes carrier surcharges persist and parcel mix is unchanged."),
            _v("merchant_fees", 1.8, 2.0, "Scheme fees assumed flat; growth is volume-driven."),
            _v("warehousing", 2.5, 4.0, "Third-party storage rates assumed to follow industrial property indices."),
            _v("energy", 1.5, 6.0, "Store and warehouse power on renewed contracts above the old baseline."),
            _v("inventory_shrinkage", 1.2, 6.0, "Assumes shrink stays at the elevated post-2023 run rate."),
            _v("software_licences", 1.0, 7.0, "Assumes vendor list-price rises at renewal."),
            _v("other_overheads", 2.5, 3.0, "General inflation on the residual G&A base."),
        ],
    },
    "professional_services": {
        "label": "Professional services",
        "defaults": [
            _v("salaries", 48.0, 4.5, "Assumes competitive market pay reviews to hold attrition at plan."),
            _v("payroll_oncosts", 11.0, 4.5, "Moves with the salary base."),
            _v("contractors", 8.0, 5.0, "Assumes associate day rates rise faster than employed pay."),
            _v("rent", 5.0, 2.5, "Assumes a smaller indexed office footprint under hybrid working."),
            _v("software_licences", 3.5, 7.0, "Assumes per-seat SaaS list prices rise at renewal."),
            _v("travel", 3.0, 4.0, "Assumes client travel returns to plan with airfare inflation."),
            _v("marketing", 2.5, 3.0, "Business development and thought-leadership spend held broadly flat in real terms."),
            _v("recruitment_training", 2.5, 4.0, "Assumes hiring at replacement level plus the CPD budget."),
            _v("insurance", 1.5, 6.0, "Professional indemnity and cyber cover assumed to keep hardening."),
            _v("professional_fees", 1.2, 3.5, "Audit, legal and tax retainers on similar scope."),
            _v("it_support", 1.5, 3.0, "Endpoint refresh cycle unchanged."),
            _v("other_overheads", 2.5, 3.0, "General inflation on the residual G&A base."),
        ],
    },
    "software_saas": {
        "label": "Software & SaaS",
        "defaults": [
            _v("salaries", 42.0, 4.5, "Assumes engineering and go-to-market pay reviews at market rate."),
            _v("payroll_oncosts", 9.0, 4.5, "Moves with the salary base."),
            _v("cloud_hosting", 12.0, 8.0, "Assumes usage grows with customers and no new committed-spend discount lands."),
            _v("marketing", 9.0, 6.0, "Assumes paid-acquisition costs keep rising to hold pipeline coverage."),
            _v("sales_commission", 6.0, 5.0, "Scales with booked revenue at unchanged plan rates."),
            _v("software_licences", 4.0, 8.0, "Assumes internal tooling and AI vendor pricing rises at renewal."),
            _v("contractors", 3.0, 4.0, "Specialist contract engineering at current day rates."),
            _v("rent", 2.0, 2.0, "Assumes a reduced indexed office footprint."),
            _v("professional_fees", 1.5, 3.5, "Audit, legal and tax on similar scope."),
            _v("compliance", 1.2, 6.0, "Assumes an expanding security and data-protection certification scope."),
            _v("travel", 1.5, 4.0, "Field sales and conference travel at plan."),
            _v("other_overheads", 2.0, 3.0, "General inflation on the residual G&A base."),
        ],
    },
    "food_beverage": {
        "label": "Food & beverage",
        "defaults": [
            _v("raw_materials", 44.0, 4.0, "Assumes agricultural inputs stay volatile and above the five-year average."),
            _v("salaries", 14.0, 4.0, "Assumes statutory wage uplift flows through the production base."),
            _v("payroll_oncosts", 3.5, 4.0, "Moves with the salary base."),
            _v("packaging", 5.5, 3.0, "Board, glass and film assumed to rise with energy-linked converter costs."),
            _v("energy", 5.0, 7.0, "Chilled and process energy on renewed contracts; no new hedge assumed."),
            _v("freight_outbound", 4.0, 4.5, "Temperature-controlled distribution rates assumed to firm."),
            _v("freight_inbound", 2.5, 4.0, "Assumes ingredient haulage tracks road-freight indices."),
            _v("inventory_shrinkage", 2.0, 3.0, "Waste and expiry held at the current run rate."),
            _v("maintenance", 2.5, 3.5, "Planned line maintenance unchanged; parts inflation only."),
            _v("compliance", 1.5, 5.0, "Assumes food-safety audit and certification scope grows."),
            _v("rent", 2.0, 3.0, "Indexed lease uplifts on existing sites."),
            _v("other_overheads", 2.5, 3.0, "General inflation on the residual G&A base."),
        ],
    },
    "logistics_transport": {
        "label": "Logistics & transport",
        "defaults": [
            _v("salaries", 34.0, 5.0, "Assumes driver and warehouse pay keeps outpacing general inflation on scarcity."),
            _v("payroll_oncosts", 8.0, 5.0, "Moves with the salary base."),
            _v("fuel_fleet", 18.0, 4.0, "Assumes diesel stays near the current forward curve; no new hedge."),
            _v("vehicle_costs", 9.0, 4.5, "Lease renewals at higher list prices plus parts and tyre inflation."),
            _v("subcontractors", 7.0, 5.0, "Assumes partner haulier rates rise with their own driver costs."),
            _v("warehousing", 5.0, 4.0, "Industrial property and handling rates assumed to keep firming."),
            _v("insurance", 3.0, 7.0, "Motor and cargo cover assumed to keep hardening on claims experience."),
            _v("maintenance", 3.0, 4.0, "Workshop parts and labour inflation on an unchanged fleet."),
            _v("it_support", 1.5, 4.0, "Telematics and routing platform renewals."),
            _v("compliance", 1.0, 4.0, "Operator licensing, tachograph and emissions-zone compliance."),
            _v("other_overheads", 2.5, 3.0, "General inflation on the residual G&A base."),
        ],
    },
    "construction": {
        "label": "Construction & engineering",
        "defaults": [
            _v("subcontractors", 34.0, 4.5, "Assumes trade rates rise with skilled-labour scarcity on live packages."),
            _v("raw_materials", 24.0, 3.5, "Assumes steel, cement and timber track producer-price inflation."),
            _v("salaries", 14.0, 4.0, "Site and commercial staff pay reviews at market rate."),
            _v("payroll_oncosts", 3.5, 4.0, "Moves with the salary base."),
            _v("equipment", 6.0, 4.0, "Plant hire rates assumed to rise with utilisation and finance costs."),
            _v("fuel_fleet", 3.0, 4.0, "Site plant and van fuel at the current forward curve."),
            _v("insurance", 2.5, 6.0, "Contract works and liability cover assumed to keep hardening."),
            _v("freight_inbound", 2.0, 4.0, "Material deliveries to site tracking road-freight indices."),
            _v("compliance", 1.5, 5.0, "Building-safety and certification obligations assumed to widen."),
            _v("professional_fees", 1.5, 3.5, "Design, legal and quantity-surveying retainers."),
            _v("other_overheads", 2.5, 3.0, "General inflation on the residual G&A base."),
        ],
    },
    "healthcare": {
        "label": "Healthcare & life sciences",
        "defaults": [
            _v("salaries", 46.0, 4.5, "Assumes clinical pay settlements at the national framework rate."),
            _v("payroll_oncosts", 11.0, 4.5, "Moves with the salary base."),
            _v("clinical_supplies", 14.0, 4.0, "Assumes consumables and devices rise with medical-inflation indices."),
            _v("contractors", 5.0, 6.0, "Assumes continued reliance on agency clinical cover at premium rates."),
            _v("rent", 4.0, 3.0, "Indexed lease uplifts on clinical premises."),
            _v("energy", 2.5, 6.0, "Assumes continuous-run building services on renewed contracts."),
            _v("maintenance", 2.5, 4.0, "Medical equipment service contracts and premises upkeep."),
            _v("compliance", 2.0, 5.0, "Regulatory inspection, accreditation and clinical-governance scope."),
            _v("insurance", 2.0, 7.0, "Clinical indemnity assumed to keep hardening."),
            _v("software_licences", 1.5, 7.0, "Clinical systems and records platform renewals."),
            _v("other_overheads", 2.5, 3.0, "General inflation on the residual G&A base."),
        ],
    },
    "other": {
        "label": "Other / general business",
        "defaults": [
            _v("salaries", 32.0, 4.0, "Assumes a cost-of-living settlement at the going market rate, headcount flat."),
            _v("payroll_oncosts", 7.5, 4.0, "Moves with the salary base."),
            _v("rent", 6.0, 3.0, "Indexed lease uplifts on existing premises."),
            _v("software_licences", 4.0, 7.0, "Assumes vendor list prices rise at renewal."),
            _v("marketing", 4.0, 4.0, "Held broadly flat in real terms."),
            _v("professional_fees", 2.5, 3.5, "Audit, legal and tax retainers on similar scope."),
            _v("energy", 2.5, 6.0, "Renewed supply contracts above the old baseline."),
            _v("travel", 2.0, 4.0, "Travel at plan with airfare and accommodation inflation."),
            _v("insurance", 1.5, 5.0, "Assumes continued hardening across the programme."),
            _v("it_support", 2.0, 3.0, "Endpoint refresh cycle unchanged."),
            _v("other_overheads", 6.0, 3.0, "General inflation on the residual G&A base."),
        ],
    },
}

DEFAULT_INDUSTRY = "other"


def industry_options() -> list[dict]:
    """For the config screen's picker."""
    return [{"id": key, "label": meta["label"]} for key, meta in INDUSTRIES.items()]


def defaults_for(industry: str, revenue: float) -> list[dict]:
    """Materialise one industry's default variable set, sized against `revenue`.

    An unknown industry falls back to `other` rather than raising: the picker is
    the only writer today, but a hand-edited budget_plan.json must not be able to
    take the page down.
    """
    meta = INDUSTRIES.get(industry) or INDUSTRIES[DEFAULT_INDUSTRY]
    try:
        rev = max(0.0, float(revenue))
    except (TypeError, ValueError):
        rev = 0.0
    out = []
    for d in meta["defaults"]:
        spec = CATALOGUE[d["id"]]
        out.append({
            "id": d["id"],
            "label": spec["label"],
            "category": spec["category"],
            "description": spec["description"],
            "current_amount": round(rev * d["share_of_revenue_pct"] / 100.0, 2),
            "expected_change_pct": d["expected_change_pct"],
            "assumption": "",
            "default_note": d["default_note"],
            "driver_id": None,
            "include": True,
        })
    return out


# --------------------------------------------------------------------------
# The maths — pure, no I/O, no app imports beyond this module's constants
# --------------------------------------------------------------------------

def _f(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(out) else out


def _pct_or_none(current: float, nxt: float) -> float | None:
    """The percentage step between two amounts, or None when there is no step to
    take a percentage OF.

    "This cost line does not exist in the baseline budget" and "this cost line is
    flat" are different claims, and 0% asserts the second. Same rule as
    `driver_status`'s `forward_12m` and `cash.cash_conversion_pct`: None, never 0,
    when the figure is undefined rather than nil.

    Note 1000 -> 0 is a defined -100%: the line existed and was removed. Only
    0 -> X is undefined.
    """
    if abs(current) > 1e-9:
        return _pct_change(current, nxt)
    return None if abs(nxt) > 1e-9 else 0.0


def _pct_text(pct: float | None, digits: int = 1) -> str:
    """A percentage for prose. The float branch's output is byte-for-byte what the
    inline f-strings produced before `None` became possible, which is what keeps
    the brief and the templated read stable."""
    if pct is None:
        return "new"
    return f"{pct:+.{digits}f}%"


def derive(plan: dict) -> dict:
    """Everything the overview and the drawer display, computed from the config.

    Materiality is literally the brief's formula — share of the cost base times
    expected change — which reduces to |delta| / current cost base. Ranking on
    the derived delta rather than on the percentage is the whole point: a 40%
    rise on a line worth 0.5% of spend must not outrank a 3% rise on the payroll.
    """
    baseline = plan.get("baseline") or {}
    company = plan.get("company") or {}

    revenue = max(0.0, _f(baseline.get("revenue")))
    # `revenue_next` is the second amount stated outright, the baseline's twin of
    # a variable's `next_amount` below. Worth having even where the percentage
    # would do: percentage -> multiply -> back is lossy, and the headline revenue
    # on a comparison should be the other budget's own sum, not a round-trip of it.
    if baseline.get("revenue_next") is None:
        revenue_pct = _f(baseline.get("revenue_change_pct"))
        revenue_next = revenue * (1.0 + revenue_pct / 100.0)
    else:
        revenue_next = max(0.0, _f(baseline.get("revenue_next")))
        revenue_pct = _f(_pct_or_none(revenue, revenue_next))

    included = [v for v in (plan.get("variables") or []) if v.get("include", True)]
    excluded_count = len(plan.get("variables") or []) - len(included)

    cost_current = math.fsum(max(0.0, _f(v.get("current_amount"))) for v in included)

    rows = []
    for v in included:
        current = max(0.0, _f(v.get("current_amount")))
        pct = _f(v.get("expected_change_pct"))
        # `next_amount` is the SECOND AMOUNT stated outright, and it exists for the
        # comparison view below: a cost line that only the right-hand budget
        # carries has current == 0, and next = current x (1 + pct/100) can never
        # express a non-zero next amount from a zero current one. The percentage
        # is then derived rather than read, so `delta`, `direction`, `materiality`
        # and the ranking all keep coming out of this one function — the
        # alternative is a second definition of "delta" living somewhere else.
        # Absent (the stored-config path — validate never emits it) the
        # arithmetic below is byte-for-byte what it always was.
        explicit = v.get("next_amount")
        if explicit is None:
            nxt = current * (1.0 + pct / 100.0)
        else:
            nxt = max(0.0, _f(explicit))
            # None when the baseline carries no such line — see _pct_or_none.
            pct = _pct_or_none(current, nxt)
        delta = nxt - current
        # FLAT_EPS_PCT is measured on the percentage BECAUSE that is the field the
        # user typed, so the label matches what they can see. An undefined
        # percentage has no field to match, so that case keys off the delta — a
        # line worth millions on one side only must not read as "flat".
        if pct is None:
            direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
        elif abs(pct) < FLAT_EPS_PCT:
            direction = "flat"
        else:
            direction = "up" if delta > 0 else "down"
        spec = CATALOGUE.get(v.get("id")) or {}
        rows.append({
            "id": v.get("id"),
            "label": v.get("label") or spec.get("label") or v.get("id"),
            "category": v.get("category") or spec.get("category") or "other",
            "description": v.get("description") or spec.get("description") or "",
            "assumption": (v.get("assumption") or "").strip(),
            "default_note": v.get("default_note") or "",
            "driver_id": v.get("driver_id") or None,
            # "both" on every stored-config row; the comparison sets it so the
            # page can say "not in the 2026 budget" rather than render a -100%
            # collapse or an undefined percentage with no explanation next to it.
            "coverage": v.get("coverage") or "both",
            "current_amount": current,
            "expected_change_pct": pct,
            "next_amount": nxt,
            "delta": delta,
            "direction": direction,
            # Share of TODAY's cost base — how much of the budget this line is.
            "share_of_cost_pct": (current / cost_current * 100.0) if cost_current else 0.0,
            "materiality": (abs(delta) / cost_current) if cost_current else 0.0,
        })

    total_abs_delta = math.fsum(abs(r["delta"]) for r in rows)
    for r in rows:
        # Share of the total movement, which is what the bar length encodes.
        r["impact_share"] = (abs(r["delta"]) / total_abs_delta) if total_abs_delta else 0.0

    # Ties broken by size then id so the order is stable across reloads — an
    # overview that reshuffles on refresh reads as data changing when it hasn't.
    rows.sort(key=lambda r: (-r["materiality"], -r["current_amount"], str(r["id"])))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    cost_next = math.fsum(r["next_amount"] for r in rows)
    cost_delta = math.fsum(r["delta"] for r in rows)

    margin_current = revenue - cost_current
    margin_next = revenue_next - cost_next
    margin_pct_current = (margin_current / revenue * 100.0) if revenue else 0.0
    margin_pct_next = (margin_next / revenue_next * 100.0) if revenue_next else 0.0

    totals = {
        "currency": company.get("currency") or "EUR",
        "current_year": baseline.get("current_year"),
        "budget_year": baseline.get("budget_year"),
        "revenue_current": revenue,
        "revenue_next": revenue_next,
        "revenue_delta": revenue_next - revenue,
        "revenue_change_pct": revenue_pct,
        "cost_current": cost_current,
        "cost_next": cost_next,
        "cost_delta": cost_delta,
        "cost_change_pct": (cost_delta / cost_current * 100.0) if cost_current else 0.0,
        "margin_current": margin_current,
        "margin_next": margin_next,
        "margin_delta": margin_next - margin_current,
        "margin_pct_current": margin_pct_current,
        "margin_pct_next": margin_pct_next,
        "margin_delta_pp": margin_pct_next - margin_pct_current,
        "included_count": len(rows),
        "excluded_count": excluded_count,
    }

    return {"ranked": rows, "top": rows[:TOP_N], "rest": rows[TOP_N:], "totals": totals}


def fingerprint(plan: dict) -> str:
    """A hash over only what changes the numbers.

    Editing an assumption note or toggling an excluded variable's percentage must
    not burn a model call, so neither is in here — but the note IS shown to the
    model, so it is included when the variable is included.
    """
    baseline = plan.get("baseline") or {}
    company = plan.get("company") or {}
    payload = {
        "company": [company.get("name"), company.get("industry"), company.get("size"),
                    company.get("currency"), company.get("fiscal_year_start_month")],
        "baseline": [baseline.get("current_year"), baseline.get("budget_year"),
                     round(_f(baseline.get("revenue")), 2),
                     round(_f(baseline.get("revenue_change_pct")), 4)],
        "variables": sorted(
            [v.get("id"), v.get("label"),
             round(_f(v.get("current_amount")), 2),
             round(_f(v.get("expected_change_pct")), 4),
             (v.get("assumption") or "").strip(),
             v.get("driver_id") or ""]
            for v in (plan.get("variables") or []) if v.get("include", True)
        ),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# Validation — teachable error strings, never exceptions
# --------------------------------------------------------------------------

def _clean_variable(raw: dict, seen: set[str]) -> dict | str:
    if not isinstance(raw, dict):
        return "Each variable must be an object."
    var_id = str(raw.get("id") or "").strip().lower()
    if not _ID_RE.match(var_id):
        return (f"Variable id '{var_id or '(empty)'}' is not valid. Use lowercase letters, "
                f"digits and underscores, starting with a letter (e.g. 'raw_materials').")
    if var_id in seen:
        return f"Duplicate variable id '{var_id}'. Each variable needs its own id."
    seen.add(var_id)

    spec = CATALOGUE.get(var_id) or {}
    label = str(raw.get("label") or spec.get("label") or "").strip()
    if not label:
        return f"Variable '{var_id}' needs a name."

    category = str(raw.get("category") or spec.get("category") or "other").strip().lower()
    if category not in CATEGORIES:
        category = "other"

    amount = _f(raw.get("current_amount"))
    if amount < 0:
        return f"'{label}' has a negative current-year amount. Amounts must be 0 or more."

    pct = _f(raw.get("expected_change_pct"))
    if not -MAX_PCT <= pct <= MAX_PCT:
        return (f"'{label}' has an expected change of {pct:g}%. "
                f"Expected change must be between -{MAX_PCT:g}% and {MAX_PCT:g}%.")

    # The link that makes this one budget model rather than two. A variable
    # carrying a driver_id inherits that driver's locked value, its provenance,
    # its place in a frozen version and its row in the export — none of which
    # needed new machinery, only the id. Validated as a slug, not against the
    # catalogue: in simple mode there may be no catalogue yet, and refusing an
    # id the CFO is about to create would make the migration path unusable.
    driver_id = str(raw.get("driver_id") or "").strip().lower()
    if driver_id and not _ID_RE.match(driver_id):
        return (f"'{label}' has an invalid driver link '{driver_id}'. Use lowercase "
                f"letters, digits and underscores, or leave it empty.")

    return {
        "id": var_id,
        "label": label[:80],
        "category": category,
        "description": str(raw.get("description") or spec.get("description") or "")[:400],
        "current_amount": round(amount, 2),
        "expected_change_pct": round(pct, 4),
        "assumption": str(raw.get("assumption") or "").strip()[:600],
        "default_note": str(raw.get("default_note") or "")[:400],
        "driver_id": driver_id or None,
        "include": bool(raw.get("include", True)),
    }


def validate(payload: dict) -> dict | str:
    """Return a cleaned config fragment, or a message the UI can show verbatim."""
    if not isinstance(payload, dict):
        return "Configuration must be an object."

    raw_company = payload.get("company") or {}
    name = str(raw_company.get("name") or "").strip()
    if not name:
        return "Company name is required."

    industry = str(raw_company.get("industry") or DEFAULT_INDUSTRY).strip().lower()
    if industry not in INDUSTRIES:
        return (f"Unknown industry '{industry}'. Valid options: "
                f"{', '.join(INDUSTRIES)}.")

    size = str(raw_company.get("size") or "mid").strip().lower()
    if size not in SIZES:
        return f"Unknown company size '{size}'. Valid options: {', '.join(SIZES)}."

    currency = str(raw_company.get("currency") or "EUR").strip().upper()
    if not re.match(r"^[A-Z]{3}$", currency):
        return f"Currency '{currency}' is not a 3-letter code (e.g. EUR, USD, DKK)."

    try:
        fy_month = int(raw_company.get("fiscal_year_start_month") or 1)
    except (TypeError, ValueError):
        return "Fiscal year start month must be a number between 1 and 12."
    if not 1 <= fy_month <= 12:
        return "Fiscal year start month must be between 1 and 12."

    raw_baseline = payload.get("baseline") or {}
    try:
        current_year = int(raw_baseline.get("current_year") or 0)
    except (TypeError, ValueError):
        return "Current year must be a four-digit year."
    if not 2000 <= current_year <= 2100:
        return "Current year must be between 2000 and 2100."

    revenue = _f(raw_baseline.get("revenue"))
    if revenue <= 0:
        return "Current-year revenue must be greater than 0."

    revenue_pct = _f(raw_baseline.get("revenue_change_pct"))
    if not -MAX_PCT <= revenue_pct <= MAX_PCT:
        return (f"Expected revenue change of {revenue_pct:g}% is out of range. "
                f"It must be between -{MAX_PCT:g}% and {MAX_PCT:g}%.")

    raw_vars = payload.get("variables")
    if not isinstance(raw_vars, list) or not raw_vars:
        return "Add at least one budgeting variable before saving."

    seen: set[str] = set()
    cleaned_vars = []
    for raw in raw_vars:
        result = _clean_variable(raw, seen)
        if isinstance(result, str):
            return result
        cleaned_vars.append(result)

    if not any(v["include"] for v in cleaned_vars):
        return "At least one variable must be included in the budget."

    return {
        "company": {"name": name[:120], "industry": industry, "size": size,
                    "currency": currency, "fiscal_year_start_month": fy_month},
        "baseline": {"current_year": current_year, "budget_year": current_year + 1,
                     "revenue": round(revenue, 2),
                     "revenue_change_pct": round(revenue_pct, 4)},
        "variables": cleaned_vars,
    }


# --------------------------------------------------------------------------
# Persistence — the repo's atomic .tmp + Path.replace() under a module lock
# --------------------------------------------------------------------------

def default_plan() -> dict:
    return {
        "version": 1,
        "configured": False,
        "created_at": 0,
        "updated_at": 0,
        "company": {"name": "", "industry": DEFAULT_INDUSTRY, "size": "mid",
                    "currency": "EUR", "fiscal_year_start_month": 1},
        "baseline": {"current_year": 0, "budget_year": 0,
                     "revenue": 0.0, "revenue_change_pct": 0.0},
        "variables": [],
        "narrative": {"text": "", "generated_at": 0, "fingerprint": "", "model": ""},
    }


def _read() -> dict:
    base = default_plan()
    if not PLAN_FILE.exists():
        return base
    try:
        stored = json.loads(PLAN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return base
    if not isinstance(stored, dict):
        return base
    base.update({k: v for k, v in stored.items() if k in base})
    return base


def _write(plan: dict) -> dict:
    plan["updated_at"] = time.time()
    if not plan.get("created_at"):
        plan["created_at"] = plan["updated_at"]
    PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plan, indent=2))
    tmp.replace(PLAN_FILE)
    return plan


def get_plan() -> dict:
    with _lock:
        return _read()


def save_config(payload: dict) -> dict | str:
    cleaned = validate(payload)
    if isinstance(cleaned, str):
        return cleaned
    with _lock:
        plan = _read()
        plan.update(cleaned)
        plan["configured"] = True
        # Drop a narrative written against the old numbers rather than showing
        # prose that contradicts the table underneath it.
        if plan["narrative"].get("fingerprint") != fingerprint(plan):
            plan["narrative"] = {"text": "", "generated_at": 0, "fingerprint": "", "model": ""}
        return _write(plan)


def reset_plan() -> dict:
    """Back to first-run: configuration and narrative both cleared."""
    with _lock:
        return _write(default_plan())


# --------------------------------------------------------------------------
# BOM mode — the same variables, read out of the real datasets
# --------------------------------------------------------------------------
#
# Everything below imports pandas-backed modules LAZILY. The engine above must
# stay importable, and testable, with no data directory at all.

# driver category (drivers.parquet) -> this module's variable category, so a
# materialised line lands in the same taxonomy a hand-typed one does.
_DRIVER_CATEGORIES = {
    "ingredient": "materials", "packaging": "materials", "logistics": "logistics",
    "energy": "facilities", "labour": "people", "fx": "other",
}

CONVERSION_ID = "conversion_overhead"


def _slug(text: str, prefix: str = "") -> str:
    """A stable, _ID_RE-safe id from a free-text label (a cost-centre name)."""
    body = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_") or "x"
    out = f"{prefix}{body}"
    if not out[:1].isalpha():
        out = f"c_{out}"
    return out[:49]


def bom_available() -> bool:
    """Is there a bill of materials and a budget to price it against?

    This, not the company profile, is what picks the mode: a company that has
    uploaded neither still gets a working budget page in simple mode.
    """
    try:
        from . import tools
    except Exception:
        return False
    for name in ("bill_of_materials", "budget_vs_actuals"):
        try:
            df, _ = tools._load(name)
        except (FileNotFoundError, OSError, KeyError):
            return False
        if df.empty:
            return False
    return True


def materialise_from_datasets(company: dict | None = None) -> dict | str:
    """Build the whole configuration out of the datasets. A payload for
    `save_config`, or a message the UI can show verbatim.

    The two years are the closed year and the plan year, and every line is
    computed for BOTH of them — the "expected change" the page ranks on is then
    the budget's own step, not a percentage anybody typed. Three cuts make up
    the cost base and together they reconcile to COGS + opex exactly:

      * one variable per driver with a bill-of-materials position, valued as
        qty-per-tonne × volume × price (the same `driver_exposure` the
        sensitivity tool uses), so the number is read rather than assumed;
      * the conversion residual — COGS minus those materials — which is what
        keeps the total honest instead of quietly understating the cost base;
      * one variable per opex cost centre, sized by its SHARE of the opex plan
        applied to the P&L's opex level. `opex_plan` is cut by cost centre and
        `budget_vs_actuals` by product line, and the two totals need not
        reconcile; only the scale-free share crosses over, exactly as
        `budget.opex_bridge` moves only a weighted rate across the same seam.
    """
    from . import budget as budget_mod
    from . import drivers as drivers_mod
    from . import tools

    try:
        bva, _ = tools._load("budget_vs_actuals")
        bom, _ = tools._load("bill_of_materials")
    except (FileNotFoundError, OSError, KeyError):
        return ("No bill of materials or budget dataset is available, so the budget "
                "cannot be built from your data. Configure the cost lines by hand instead.")
    if bva.empty or bom.empty:
        return ("The bill of materials or the budget dataset is empty, so there is nothing "
                "to price. Configure the cost lines by hand instead.")

    plan_year = tools._budget_year(bva)
    closed = bva[bva["revenue_actual_eur"].notna()]
    if closed.empty:
        return (f"There are no closed months to compare {plan_year} against. "
                f"Configure the cost lines by hand instead.")
    base_year = str(closed["month"].max())[:4]
    if base_year == plan_year:
        return (f"{plan_year} is both the latest closed year and the plan year, so there is "
                f"no year-on-year step to show. Configure the cost lines by hand instead.")

    cur = closed[closed["month"].str.startswith(base_year)]
    plan = bva[bva["month"].str.startswith(plan_year)]

    def _sum(df, column):
        return float(df[column].sum(min_count=1) or 0.0) if column in df.columns else 0.0

    revenue_cur = _sum(cur, "revenue_actual_eur")
    revenue_next = _sum(plan, "revenue_budget_eur")
    cogs_cur, cogs_next = _sum(cur, "cogs_actual_eur"), _sum(plan, "cogs_budget_eur")
    opex_cur, opex_next = _sum(cur, "opex_actual_eur"), _sum(plan, "opex_budget_eur")
    if revenue_cur <= 0:
        return (f"{base_year} has no actual revenue, so there is no baseline to budget "
                f"from. Configure the cost lines by hand instead.")

    bom_rows = [{"product_line": str(r["product_line"]), "driver_id": str(r["driver_id"]),
                 "qty_per_tonne": float(r["qty_per_tonne"])} for _, r in bom.iterrows()]
    vol_cur = _volumes(cur, "volume_tonnes_actual")
    vol_next = _volumes(plan, "volume_tonnes_budget")

    # Current-year prices are the mean EUR level actually paid across that year;
    # plan-year prices are the LOCKED values the budget was frozen at. That is
    # the honest pairing — it is precisely the step the budget took.
    price_cur = _mean_eur_prices(base_year, drivers_mod, tools)
    price_next = _locked_eur_prices(drivers_mod, tools)
    status = {d["driver_id"]: d for d in drivers_mod.driver_status()}

    variables = []
    used_cur = used_next = 0.0
    for did in sorted({r["driver_id"] for r in bom_rows}):
        meta = status.get(did) or {}
        a = budget_mod.driver_exposure(bom_rows, vol_cur, price_cur.get(did, 0.0), did)
        b = budget_mod.driver_exposure(bom_rows, vol_next, price_next.get(did, 0.0), did)
        if a <= 0 and b <= 0:
            continue
        used_cur += a
        used_next += b
        variables.append({
            "id": did,
            "label": meta.get("name") or did,
            "category": _DRIVER_CATEGORIES.get(str(meta.get("category")), "materials"),
            "description": (f"Priced through the bill of materials: quantity per tonne × "
                            f"budgeted volume × the {plan_year} locked price, in "
                            f"{meta.get('unit') or 'the driver unit'}."),
            "current_amount": a,
            "expected_change_pct": _pct_change(a, b),
            "assumption": _driver_assumption(meta, plan_year),
            "driver_id": did,
            "include": True,
        })

    # The residual keeps the cost base equal to COGS. Floored at zero: a BOM
    # that over-explains COGS is a data problem, not a negative cost line.
    conv_cur, conv_next = max(0.0, cogs_cur - used_cur), max(0.0, cogs_next - used_next)
    if conv_cur > 0 or conv_next > 0:
        variables.append({
            "id": CONVERSION_ID,
            "label": "Conversion & plant overhead",
            "category": "facilities",
            "description": "What COGS carries beyond the bill of materials — labour on the "
                           "line, maintenance and plant overhead. Derived as COGS minus the "
                           "priced material inputs, so the cost base ties to the P&L.",
            "current_amount": conv_cur,
            "expected_change_pct": _pct_change(conv_cur, conv_next),
            "assumption": "Derived, not assumed: the part of COGS the bill of materials "
                          "does not explain.",
            "driver_id": None,
            "include": True,
        })

    variables.extend(_opex_variables(tools, status, base_year, plan_year,
                                     opex_cur, opex_next))

    prof = company or {}
    return {
        "company": {
            "name": prof.get("name") or "",
            "industry": prof.get("industry") or DEFAULT_INDUSTRY,
            "size": prof.get("size") or "mid",
            "currency": prof.get("currency") or "EUR",
            "fiscal_year_start_month": prof.get("fiscal_year_start_month") or 1,
        },
        "baseline": {
            "current_year": int(base_year),
            "revenue": revenue_cur,
            "revenue_change_pct": _pct_change(revenue_cur, revenue_next),
        },
        "variables": variables,
    }


def _volumes(df, column) -> dict:
    if column not in df.columns:
        return {}
    grouped = df.groupby("product_line")[column].sum()
    return {str(k): float(v) for k, v in grouped.items() if v == v}


def _pct_change(current: float, nxt: float) -> float:
    return ((nxt - current) / current * 100.0) if abs(current) > 1e-9 else 0.0


def _mean_eur_prices(year: str, drivers_mod, tools) -> dict:
    """Each driver's mean EUR price across one year — what was actually paid.

    Converted month by month through that month's EUR/USD, so a year in which
    the euro moved is priced correctly rather than at a year-end snapshot.
    """
    prices = drivers_mod.load_prices()
    if prices.empty:
        return {}
    months = sorted({m for m in prices["month"].astype(str) if m.startswith(str(year))})
    totals: dict[str, list] = {}
    for month in months:
        eur, _quote = tools._driver_eur_prices(month)
        for did, value in eur.items():
            slot = totals.setdefault(did, [0.0, 0])
            slot[0] += float(value)
            slot[1] += 1
    return {d: total / count for d, (total, count) in totals.items() if count}


def _locked_eur_prices(drivers_mod, tools) -> dict:
    """The locked assumption set in EUR — the prices the plan year was frozen at."""
    locked = drivers_mod.load_assumptions().get("assumptions") or {}
    eur_now, quote_now = tools._driver_eur_prices()
    fx = (locked.get("eur_usd") or {}).get("value") or quote_now.get("eur_usd") or 1.0
    catalog = drivers_mod.load_catalog()
    ccy = ({} if catalog.empty else
           dict(zip(catalog["driver_id"].astype(str), catalog["quote_currency"].astype(str))))
    out = dict(eur_now)
    for did, entry in locked.items():
        value = (entry or {}).get("value")
        if value is None:
            continue
        out[str(did)] = (float(value) / fx
                         if ccy.get(str(did)) == "USD" and did != "eur_usd" else float(value))
    return out


def _driver_assumption(meta: dict, plan_year: str) -> str:
    """The sentence under a materialised cost line: what it is locked at, where
    that came from, and — the point of the whole app — what the market has said
    since. The forward is quoted as a CHALLENGE to the locked figure, never
    silently substituted for it: the budget is what the CFO froze."""
    if not meta:
        return ""
    parts = []
    locked = meta.get("locked_value")
    if locked is not None:
        parts.append(f"Locked into the {plan_year} budget at "
                     f"{locked:,.2f} {meta.get('unit') or ''}".rstrip())
    drift = meta.get("drift_pct")
    if drift is not None and abs(drift) >= 0.05:
        parts.append(f"the latest observation is {drift:+.1f}% against that lock")
    forward = meta.get("forward_vs_lock_pct")
    if forward is not None and abs(forward) >= 0.05:
        parts.append(f"the forward curve for the next 12 months averages "
                     f"{forward:+.1f}% against it")
    if meta.get("verify_status") == "never_verified":
        parts.append("no price has been verified yet")
    return ("; ".join(parts) + ".") if parts else ""


def _opex_shares(tools, year: str) -> tuple[dict, dict]:
    """({cost_centre: that year's planned amount}, {cost_centre: driver_id | None}).

    The single read of `opex_plan` behind both the BOM materialisation and the
    comparison snapshots below, so "how big is the Logistics cost centre" has one
    answer rather than two that can drift apart. Only the SHARE of the returned
    total ever crosses into the P&L — see the caller's docstring for why.
    """
    shares: dict[str, float] = {}
    linked: dict[str, str | None] = {}
    try:
        plan, _ = tools._load("opex_plan")
    except (FileNotFoundError, OSError, KeyError):
        return shares, linked
    if plan is None or plan.empty:
        return shares, linked
    column = "amount_budget_eur" if "amount_budget_eur" in plan.columns else "amount_eur"
    for _, r in plan[plan["month"].astype(str).str.startswith(str(year))].iterrows():
        centre = str(r["cost_centre"])
        shares[centre] = shares.get(centre, 0.0) + float(r[column] or 0.0)
        did = r.get("driver_id")
        # `did != did` is the NaN test: pandas hands back a float NaN for an
        # empty cell, and NaN is the one value not equal to itself.
        linked.setdefault(centre, None if did != did or did in (None, "") else str(did))
    return shares, linked


def _opex_variables(tools, status: dict, base_year: str, plan_year: str,
                    opex_cur: float, opex_next: float) -> list[dict]:
    """One variable per cost centre, sized by SHARE of the opex plan.

    See the docstring above for why only the share crosses the seam. With no
    opex plan at all the whole opex level lands on a single line, which is
    honest — that is exactly how much is known about it.
    """
    shares_cur, linked_cur = _opex_shares(tools, base_year)
    shares_next, linked_next = _opex_shares(tools, plan_year)
    # The base year wins where both name a driver, which is the order the single
    # combined loop this replaced established with setdefault.
    linked: dict[str, str | None] = {**linked_next, **linked_cur}

    if not shares_cur and not shares_next:
        if opex_cur <= 0 and opex_next <= 0:
            return []
        return [{
            "id": "operating_expenses", "label": "Operating expenses", "category": "other",
            "description": "Period cost from the P&L. No cost-centre plan is available to "
                           "break it down.",
            "current_amount": opex_cur, "expected_change_pct": _pct_change(opex_cur, opex_next),
            "assumption": "", "driver_id": None, "include": True,
        }]

    total_cur = sum(shares_cur.values()) or 1.0
    total_next = sum(shares_next.values()) or 1.0
    out = []
    for centre in sorted(set(shares_cur) | set(shares_next)):
        a = opex_cur * shares_cur.get(centre, 0.0) / total_cur
        b = opex_next * shares_next.get(centre, 0.0) / total_next
        if a <= 0 and b <= 0:
            continue
        did = linked.get(centre)
        meta = status.get(did) or {}
        out.append({
            "id": _slug(centre, "opex_"),
            "label": f"{centre} (opex)",
            "category": "people" if str(meta.get("category")) == "labour" else "other",
            "description": (f"The {centre} cost centre's share of operating expenses, taken "
                            f"from the opex plan and applied to the P&L's opex level."),
            "current_amount": a,
            "expected_change_pct": _pct_change(a, b),
            "assumption": (_driver_assumption(meta, plan_year) if meta else ""),
            "driver_id": did,
            "include": True,
        })
    return out


def rebuild_from_datasets(company: dict | None = None) -> dict | str:
    """Materialise and save in one step — what the Budget page's rebuild does."""
    payload = materialise_from_datasets(company)
    if isinstance(payload, str):
        return payload
    return save_config(payload)


# --------------------------------------------------------------------------
# Comparison mode — any two budgets, side by side
# --------------------------------------------------------------------------
#
# `derive()` was always a two-budget engine: every row carries a current and a
# next amount, every total is _current / _next / _delta / _change_pct. What was
# hardcoded was the CHOICE of the two sides — `materialise_from_datasets` picks
# the latest closed year's actuals and the plan year's locked budget and freezes
# the answer into budget_plan.json. This section makes the choice the CFO's, and
# adds no second definition of "delta": both sides are reduced to one comparable
# SNAPSHOT, the snapshots become the `variables[]` of a throwaway plan, and
# `derive()` does the arithmetic exactly as it does for the stored one.
#
# Nothing here is persisted. `get_plan()`, `/api/budgetplan` and
# `tools.budget_outlook()` keep reading the CFO's own configured budget, which is
# why a comparison cannot corrupt what the agent sees — and why `validate()`'s
# "budget_year = current_year + 1" does not stop a 2024-vs-2027 pair.
#
# Three selection kinds, and the asymmetry between them is the point:
#
#   * `budget:YYYY` — a budget as BOOKED in `budget_vs_actuals`: volumes and P&L
#     off the `*_budget_eur` columns, cost lines valued at the locked set.
#   * `actual:YYYY` — a year as OUTTURNED: the `*_actual_eur` columns, cost lines
#     valued at the mean EUR price actually paid across that year. This kind is
#     what keeps the page's own default view expressible — `actual:2026` against
#     `budget:2027` is exactly the pair `materialise_from_datasets` freezes — and
#     it is the only pairing in this dataset where the driver PRICES genuinely
#     move, because a budget year's cost is frozen at one lock month (below).
#   * `scenario:<id>` — a budget as PROJECTED: a stored scenario, carrying its own
#     basis (locked / spot / forward), its own assumptions and its own monthly
#     shape.
#
# All three are dataset reads, so every import in here is lazy, and all three are
# gated at the route — the same reasoning /api/cash on this page is gated.

SELECTION_KINDS = ("budget", "actual", "scenario")

# A dataset budget year prices its cost lines at the locked assumption set. In
# this dataset that is not a shortcut but the literal truth: generate_data
# freezes `unit_cost_budget` at ONE lock month for every year it writes, so a
# budget year has no lock of its own to price against. The consequence — a
# budget-to-budget driver move is volume, not price — is stated on the snapshot
# rather than left for the reader to discover, and it is why `actual:YYYY` exists.
BUDGET_PRICE_NOTE = ("Booked budgets are valued at the locked assumption set, because a budget "
                     "year's own lock is not recorded. Differences on the driver lines between "
                     "two booked budgets are volume and mix; the price story is in the actuals.")

ACTUAL_PRICE_NOTE = ("Cost lines are valued at the mean EUR price actually paid across the year, "
                     "converted month by month, so this side carries what the business really "
                     "spent rather than what it planned to.")

# Which columns each dataset-year kind reads, and what to call it. The two kinds
# differ ONLY in this table and in how their driver prices are resolved, which is
# what keeps them one snapshot builder rather than two.
_YEAR_KINDS = {
    "budget": {"suffix": "budget", "label": "{year} budget (as booked)",
               "note": BUDGET_PRICE_NOTE, "group": "Budget years"},
    "actual": {"suffix": "actual", "label": "{year} actuals (as outturned)",
               "note": ACTUAL_PRICE_NOTE, "group": "Actuals"},
}


def parse_selection(key: str) -> tuple[str, str] | None:
    """'budget:2026' -> ('budget', '2026'); 'scenario:abc' -> ('scenario', 'abc').

    None for anything else, so a caller always has one thing to check. Split on
    the FIRST colon only: a scenario id is opaque and must survive intact.
    """
    text = str(key or "").strip()
    if ":" not in text:
        return None
    kind, _, ident = text.partition(":")
    kind, ident = kind.strip().lower(), ident.strip()
    if kind not in SELECTION_KINDS or not ident:
        return None
    return kind, ident


def _comparison_context() -> dict | str:
    """Everything both snapshot builders read, read once.

    One context per comparison, so a pair costs one pass over the datasets rather
    than two — and so the two sides cannot be priced off different reads of the
    same locked assumption set.
    """
    from . import drivers as drivers_mod
    from . import tools

    try:
        bva, source = tools._load("budget_vs_actuals")
        bom, _ = tools._load("bill_of_materials")
    except (FileNotFoundError, OSError, KeyError):
        return ("No budget or bill-of-materials dataset is available, so there is nothing to "
                "compare. Build the budget from your data first.")
    if bva.empty or bom.empty:
        return ("The budget or bill-of-materials dataset is empty, so there is nothing to "
                "compare.")

    bom_rows = [{"product_line": str(r["product_line"]), "driver_id": str(r["driver_id"]),
                 "qty_per_tonne": float(r["qty_per_tonne"])} for _, r in bom.iterrows()]
    try:
        status = {d["driver_id"]: d for d in drivers_mod.driver_status()}
    except Exception:
        status = {}
    return {
        "bva": bva, "bom_rows": bom_rows, "source": source,
        "driver_ids": sorted({r["driver_id"] for r in bom_rows}),
        "locked": _locked_eur_prices(drivers_mod, tools),
        "status": status,
        "plan_year": tools._budget_year(bva),
        "budget_years": _years_with("budget", bva),
        "actual_years": _years_with("actual", bva),
        "drivers_mod": drivers_mod,
        "tools": tools,
        # Mean-EUR prices are twelve dataset reads per year, so they are resolved
        # once per year and memoised for the life of one comparison.
        "_mean_cache": {},
    }


def _years_with(kind: str, bva) -> list[str]:
    """Every year carrying at least one row of that kind, newest first.

    A year whose `*_actual_eur` columns are entirely empty is not an outturn, and
    a year whose `*_budget_eur` columns are entirely empty is not a budget. The
    plan year is the first case and the plan year alone.
    """
    column = f"revenue_{_YEAR_KINDS[kind]['suffix']}_eur"
    if column not in bva.columns:
        return []
    months = bva["month"].astype(str)
    return [year for year in sorted({m[:4] for m in months}, reverse=True)
            if bva[months.str.startswith(year)][column].notna().any()]


def _year_prices(kind: str, year: str, ctx: dict) -> dict:
    """The EUR price each driver is valued at on one dataset-year side.

    A booked budget has no lock of its own (see BUDGET_PRICE_NOTE), so it is
    valued at the one locked set. An outturn is valued at what was actually paid.
    This one branch is the whole difference between the two kinds.
    """
    if kind == "budget":
        return ctx["locked"]
    cached = ctx["_mean_cache"].get(year)
    if cached is None:
        cached = _mean_eur_prices(year, ctx["drivers_mod"], ctx["tools"])
        ctx["_mean_cache"][year] = cached
    return cached


def _cost_line(line_id: str, label: str, category: str, description: str,
               amount: float, driver_id: str | None, assumption: str = "") -> dict:
    """One entry in a snapshot's `lines`. Same field names a variable uses, so
    `compare` can hand them to `derive()` with no translation step."""
    return {"id": line_id, "label": label, "category": category,
            "description": description, "assumption": assumption,
            "driver_id": driver_id, "amount": max(0.0, _f(amount))}


def _sum_column(df, column: str) -> float:
    return float(df[column].sum(min_count=1) or 0.0) if column in df.columns else 0.0


def _year_snapshot(kind: str, year: str, ctx: dict) -> dict | str:
    """One dataset year — booked budget or outturn — reduced to a snapshot.

    The three cuts are `materialise_from_datasets`'s, for the same reason: the
    priced material inputs, the conversion residual that keeps the cost base
    equal to COGS, and the opex cost centres sized by their share of the plan.
    Only the column family and the driver prices differ between the two kinds,
    which is why both come through one function.
    """
    from . import budget as budget_mod

    spec = _YEAR_KINDS[kind]
    sfx = spec["suffix"]
    available = ctx[f"{kind}_years"]
    bva = ctx["bva"]
    rows = bva[bva["month"].astype(str).str.startswith(str(year))]
    if rows.empty:
        return (f"There are no rows for {year}. Available {kind} years: "
                f"{', '.join(available) or 'none'}.")
    priced = (rows[rows[f"revenue_{sfx}_eur"].notna()]
              if f"revenue_{sfx}_eur" in rows.columns else rows.iloc[0:0])
    if priced.empty:
        return (f"{year} carries no {kind} rows. Available {kind} years: "
                f"{', '.join(available) or 'none'}.")

    revenue = _sum_column(priced, f"revenue_{sfx}_eur")
    cogs = _sum_column(priced, f"cogs_{sfx}_eur")
    opex = _sum_column(priced, f"opex_{sfx}_eur")
    volumes = _volumes(priced, f"volume_tonnes_{sfx}")
    prices = _year_prices(kind, str(year), ctx)
    priced_at = ("the locked price" if kind == "budget"
                 else f"the mean EUR price paid across {year}")

    lines: dict[str, dict] = {}
    used = 0.0
    for did in ctx["driver_ids"]:
        spend = budget_mod.driver_exposure(ctx["bom_rows"], volumes,
                                          prices.get(did, 0.0), did)
        if spend <= 0:
            continue
        used += spend
        meta = ctx["status"].get(did) or {}
        lines[did] = _cost_line(
            did, meta.get("name") or did,
            _DRIVER_CATEGORIES.get(str(meta.get("category")), "materials"),
            (f"Priced through the bill of materials: quantity per tonne × the {year} "
             f"{kind} volume × {priced_at}, in "
             f"{meta.get('unit') or 'the driver unit'}."),
            spend, did, _driver_assumption(meta, ctx["plan_year"]))

    residual = max(0.0, cogs - used)
    if residual > 0:
        lines[CONVERSION_ID] = _cost_line(
            CONVERSION_ID, "Conversion & plant overhead", "facilities",
            "What COGS carries beyond the bill of materials — labour on the line, "
            "maintenance and plant overhead. Derived as COGS minus the priced material "
            "inputs, so the cost base ties to the P&L.",
            residual, None,
            "Derived, not assumed: the part of COGS the bill of materials does not explain.")

    for line in _opex_lines(ctx, str(year), opex):
        lines[line["id"]] = line

    return {
        "key": f"{kind}:{year}", "kind": kind, "year": str(year),
        "label": spec["label"].format(year=year),
        "short_label": f"{year} {'budget' if kind == 'budget' else 'actuals'}",
        "basis": None, "group": spec["group"],
        "revenue": revenue, "cogs": cogs, "opex": opex,
        "volume": math.fsum(volumes.values()),
        "lines": lines,
        "months": sorted({str(m) for m in priced["month"].astype(str)}),
        "product_lines": sorted(volumes),
        "regions": sorted(set(priced["region"].astype(str))) if "region" in priced.columns else [],
        "notes": [spec["note"]] + _year_coverage(kind, rows, priced, year),
    }


def _year_coverage(kind: str, rows, priced, year: str) -> list[str]:
    """What this side does not cover, said out loud.

    A region that opened after the budget was set has actuals and no budget. That
    is a real state — the whole reason `budget_vs_actuals` uses NaN rather than a
    zero — and a comparison that silently summed over it would understate this
    side against whatever it is being compared to. How MUCH it understates by is
    `_gap_note`'s job, because only the pair knows that.
    """
    missing = len(rows) - len(priced)
    if missing <= 0:
        return []
    note = f"{missing} of {len(rows)} {year} rows carry no {kind} figures and are excluded"
    if "region" in rows.columns:
        # Callers reach here only with a non-empty `priced`, so this is the set of
        # regions present in the year with nothing of this kind recorded.
        gone = sorted(set(rows["region"].astype(str)) - set(priced["region"].astype(str)))
        if gone:
            note += f" ({', '.join(gone)}: no {year} {kind} was set)"
    return [note + "."]


def _opex_lines(ctx: dict, year: str, opex_total: float,
                rates: dict | None = None) -> list[dict]:
    """One cost line per opex cost centre, sized by SHARE of the opex plan.

    Same seam as `_opex_variables` and the same rule: `opex_plan` is cut by cost
    centre and the P&L by product line, the two totals need not reconcile, and
    only the scale-free share crosses over. `rates` optionally weights the split
    by each centre's own movement — a scenario's `opex_bridge` supplies them — and
    is renormalised so the lines still sum to `opex_total` exactly. Omit it and
    this is the plain share, which is what makes the weighting a pure extension.
    """
    if opex_total <= 0:
        return []
    shares, linked = _opex_shares(ctx["tools"], year)
    if not shares:
        return [_cost_line("operating_expenses", "Operating expenses", "other",
                           "Period cost from the P&L. No cost-centre plan is available to "
                           "break it down.", opex_total, None)]

    rates = rates or {}
    weights = {c: (amount / (sum(shares.values()) or 1.0))
                  * (1.0 + _f(rates.get(c)) / 100.0)
               for c, amount in shares.items()}
    total_weight = math.fsum(weights.values())
    if total_weight <= 0:
        return []

    out = []
    for centre in sorted(shares):
        amount = opex_total * weights[centre] / total_weight
        if amount <= 0:
            continue
        did = linked.get(centre)
        meta = ctx["status"].get(did) or {}
        out.append(_cost_line(
            _slug(centre, "opex_"), f"{centre} (opex)",
            "people" if str(meta.get("category")) == "labour" else "other",
            f"The {centre} cost centre's share of operating expenses, taken from the opex "
            f"plan and applied to the P&L's opex level.",
            amount, did, _driver_assumption(meta, ctx["plan_year"]) if meta else ""))
    return out


def _scenario_year(scenario: dict) -> str:
    """A scenario carries no year field — it is in the months it projected, and
    in `baseline` ('budget_2027') as a fallback. Both, in that order."""
    months = scenario.get("by_month") or []
    if months and months[0].get("month"):
        return str(months[0]["month"])[:4]
    tail = str(scenario.get("baseline") or "").rpartition("_")[2]
    return tail if tail.isdigit() else ""


def _scenario_snapshot(scenario: dict, ctx: dict) -> dict | str:
    """A stored scenario, reduced to the same snapshot shape.

    Driver spend is EXACT rather than re-derived. `project_pnl` builds each
    driver's delta as (effective − locked) × qty × post-volume tonnage × unhedged
    on top of a baseline of locked × qty × tonnage, so the applied spend is
    `driver_exposure(…, locked) + driver_impact_eur[did]` — which carries the
    forward curve, the CFO's percentage and the hedge coverage without this
    module knowing anything about any of them.
    """
    from . import budget as budget_mod

    totals = scenario.get("totals") or {}
    revenue = _f(totals.get("revenue_eur"))
    cogs = _f(totals.get("cogs_eur"))
    opex = _f(totals.get("opex_eur"))
    if revenue <= 0 and cogs <= 0:
        return (f"Scenario '{scenario.get('name')}' has no stored projection to compare. "
                "Open it on the Scenarios page and recompute it first.")

    volumes = {str(r.get("product_line")): _f(r.get("volume_tonnes"))
               for r in (scenario.get("by_product_line") or [])}
    locked_used = scenario.get("driver_prices_used") or {}
    impact = scenario.get("driver_impact_eur") or {}
    basis = str(scenario.get("basis") or "locked")
    year = _scenario_year(scenario)

    lines: dict[str, dict] = {}
    used = 0.0
    for did in ctx["driver_ids"]:
        base_price = _f(locked_used.get(did, ctx["locked"].get(did, 0.0)))
        qty_spend = budget_mod.driver_exposure(ctx["bom_rows"], volumes, base_price, did)
        spend = qty_spend + _f(impact.get(did))
        if spend <= 0:
            continue
        used += spend
        meta = ctx["status"].get(did) or {}
        lines[did] = _cost_line(
            did, meta.get("name") or did,
            _DRIVER_CATEGORIES.get(str(meta.get("category")), "materials"),
            (f"Priced through the bill of materials at this scenario's own volumes, on the "
             f"{basis} basis, in {meta.get('unit') or 'the driver unit'}."),
            spend, did,
            _scenario_line_assumption(meta, ctx["plan_year"], basis, base_price,
                                      qty_spend, spend))

    residual = max(0.0, cogs - used)
    if residual > 0:
        lines[CONVERSION_ID] = _cost_line(
            CONVERSION_ID, "Conversion & plant overhead", "facilities",
            "What COGS carries beyond the bill of materials. Derived as this scenario's COGS "
            "minus its priced material inputs, so the cost base ties to its own P&L.",
            residual, None,
            "Derived, not assumed. A price basis moves only the bill-of-materials lines, so "
            "this figure is the same on every basis.")

    rates = {str(r.get("cost_centre")): _f(r.get("pct"))
             for r in ((scenario.get("opex_bridge") or {}).get("by_cost_centre") or [])}
    for line in _opex_lines(ctx, year or ctx["plan_year"], opex, rates):
        lines[line["id"]] = line

    name = str(scenario.get("name") or "Untitled scenario")
    return {
        "key": f"scenario:{scenario.get('id')}", "kind": "scenario", "year": year,
        "label": name, "short_label": name, "basis": basis, "group": "Scenarios",
        "revenue": revenue, "cogs": cogs, "opex": opex,
        "volume": _f(totals.get("volume_tonnes")),
        "lines": lines,
        "months": [str(r.get("month")) for r in (scenario.get("by_month") or [])],
        "product_lines": sorted(volumes),
        # A scenario is cut by product line, never by region: it is projected off
        # the plan year's budget rows, so its regional coverage is that year's.
        # `_gap_note` reads the year rather than guessing.
        "regions": [],
        "notes": [SCENARIO_BASIS_NOTES.get(basis, "")] if basis in SCENARIO_BASIS_NOTES else [],
        "scenario_id": scenario.get("id"),
        "active": bool(scenario.get("active")),
    }


SCENARIO_BASIS_NOTES = {
    "locked": "Drivers are priced at their locked values — the budget as frozen.",
    "spot": "Drivers were re-priced at the latest observed market level before this "
            "scenario's percentages were applied.",
    "forward": "Drivers were priced off the published forward curve, month by month, before "
               "this scenario's percentages were applied.",
}


def _scenario_line_assumption(meta: dict, plan_year: str, basis: str, locked_price: float,
                              at_locked: float, applied: float) -> str:
    """The sentence under a scenario's cost line.

    The applied price is back-solved from the spend rather than looked up, which
    is the only way to state it: a forward-basis line is priced month by month
    and there is no single number in the store to read.
    """
    parts = []
    if basis != "locked" and at_locked > 0 and locked_price > 0:
        effective = locked_price * applied / at_locked
        unit = meta.get("unit") or ""
        parts.append(f"Applied at an average {effective:,.2f} {unit}".rstrip()
                     + f" on the {basis} basis")
    tail = _driver_assumption(meta, plan_year)
    if tail:
        parts.append(tail[:-1] if tail.endswith(".") else tail)
    return ("; ".join(parts) + ".") if parts else ""


def budget_snapshot(key: str, ctx: dict | None = None) -> dict | str:
    """Resolve one selection key to a snapshot, or to a teachable sentence.

    Teachable in the tools.py sense: every refusal ENUMERATES the valid options,
    so a stale key from another tab tells the caller what to pick instead rather
    than just failing.
    """
    from . import scenarios as scenarios_mod

    if ctx is None:
        ctx = _comparison_context()
    if isinstance(ctx, str):
        return ctx

    parsed = parse_selection(key)
    if parsed is None:
        return (f"'{key}' is not a budget selection. Use 'budget:YYYY' for a booked budget, "
                f"'actual:YYYY' for an outturn, or 'scenario:<id>' for a stored scenario.")
    kind, ident = parsed

    if kind in _YEAR_KINDS:
        available = ctx[f"{kind}_years"]
        if ident not in available:
            return (f"There is no {ident} {kind}. Available {kind} years: "
                    f"{', '.join(available) or 'none'}.")
        return _year_snapshot(kind, ident, ctx)

    scenario = scenarios_mod.get_scenario(ident)
    if scenario is None:
        names = [f"{s.get('name')} ({s.get('id')})" for s in scenarios_mod.list_scenarios()]
        return (f"No scenario with id '{ident}' — it may have been deleted in another tab. "
                f"Stored scenarios: {'; '.join(names) or 'none'}.")
    return _scenario_snapshot(scenario, ctx)


def _gap_note(left: dict, right: dict, ctx: dict) -> str:
    """How much of the difference is a region the baseline never covered.

    `_year_coverage` says a region is missing. This says what that COSTS the
    reader, and it is the single most important sentence on the page: in the
    seeded data 55% of the default pair's revenue step and 67% of its volume step
    is Adriatic, a market that opened mid-2026 and has no 2026 budget at all. A
    page that reports "+13.3%" without saying so is not wrong about the
    arithmetic and is badly wrong about the business.

    Measured off the right side's own YEAR in `budget_vs_actuals`, which is why a
    scenario works too — a scenario is projected from that year's budget rows, so
    the regions it covers are that year's.
    """
    if not left.get("regions") or not right.get("year"):
        return ""
    bva = ctx["bva"]
    rows = bva[bva["month"].astype(str).str.startswith(str(right["year"]))]
    if rows.empty or "region" not in rows.columns:
        return ""
    # The right side's year is a plan year for a scenario and its own year for a
    # dataset side; either way the budget columns are the ones that describe it.
    extra = sorted(set(rows["region"].astype(str)) - set(left["regions"]))
    if not extra:
        return ""
    added = rows[rows["region"].astype(str).isin(extra)]
    rev = _sum_column(added, "revenue_budget_eur")
    vol = _sum_column(added, "volume_tonnes_budget")
    rev_step = right["revenue"] - left["revenue"]
    vol_step = _f(right.get("volume")) - _f(left.get("volume"))

    shares = []
    if rev > 0 and rev_step > 0:
        shares.append(f"{rev / rev_step * 100:.0f}% of the revenue step")
    if vol > 0 and vol_step > 0:
        shares.append(f"{vol / vol_step * 100:.0f}% of the volume step")
    if not shares:
        return ""
    return (f"{', '.join(extra)} appears on the {right['short_label']} side only — "
            f"{left['short_label']} has no figures for it — so {' and '.join(shares)} "
            f"is a market the baseline never covered, not growth in the rest of the business.")


def comparison_options(ctx: dict | None = None) -> dict | str:
    """The budgets both dropdowns offer, and which pair the page opens on.

    The defaults are DERIVED, never hardcoded: the baseline is the booked budget
    for the year before the one being planned, and the comparison is whatever
    scenario the budget currently rests on. `scenarios.get_active()` already falls
    back to the most recently updated scenario, so only an empty store reaches the
    stated fallback below — and it is stated rather than silently substituted,
    because "no scenario is active" and "this scenario is active" are different
    answers.
    """
    from . import scenarios as scenarios_mod

    if ctx is None:
        ctx = _comparison_context()
    if isinstance(ctx, str):
        return ctx

    years = ctx["budget_years"]
    budgets = [{"key": f"{kind}:{y}", "kind": kind,
                "label": _YEAR_KINDS[kind]["label"].format(year=y),
                "year": y, "basis": None, "active": False,
                "group": _YEAR_KINDS[kind]["group"]}
               for kind in ("budget", "actual") for y in ctx[f"{kind}_years"]]

    for s in scenarios_mod.list_scenarios():
        budgets.append({
            "key": f"scenario:{s.get('id')}", "kind": "scenario",
            "label": str(s.get("name") or "Untitled scenario"),
            "year": _scenario_year(s), "basis": s.get("basis") or "locked",
            # The record's OWN flag, not get_active()'s answer. get_active() falls
            # back to the most recently updated scenario, and a dropdown that
            # labelled that one "active" would be asserting something false.
            "active": bool(s.get("active")),
            # Two seeded scenarios share a name, so the label alone cannot
            # identify one in a dropdown. The basis and the date are what tell
            # them apart, and both belong to the option rather than the name.
            "updated_at": s.get("updated_at"),
            "group": "Scenarios",
        })

    if len(budgets) < 2:
        return ("There is only one budget to look at, so there is nothing to compare yet. "
                "Build a scenario on the Scenarios page, or add another budget year to your "
                "data.")

    plan_year = str(ctx["plan_year"])
    prior = str(int(plan_year) - 1) if plan_year.isdigit() else ""
    keys = {b["key"] for b in budgets}
    left = next((k for k in (f"budget:{prior}",
                             f"budget:{years[0]}" if years else "",
                             budgets[0]["key"]) if k in keys), budgets[0]["key"])

    fallback = None
    active = scenarios_mod.get_active()
    if active is not None and active.get("active"):
        right = f"scenario:{active.get('id')}"
    elif active is not None:
        right = f"scenario:{active.get('id')}"
        fallback = {"field": "right", "reason": (
            "No scenario is flagged active on the Scenarios page, so the comparison side "
            f"falls back to the most recently updated one, \"{active.get('name')}\".")}
    elif f"budget:{plan_year}" in keys:
        right = f"budget:{plan_year}"
        fallback = {"field": "right", "reason": (
            "There are no scenarios yet, so the comparison side falls back to the "
            f"{plan_year} budget as booked.")}
    else:
        right = next((b["key"] for b in budgets if b["key"] != left), budgets[0]["key"])
        fallback = {"field": "right", "reason": (
            "There are no scenarios yet, so the comparison side falls back to the next "
            "budget in the list.")}
    if right == left and len(budgets) > 1:
        right = next(b["key"] for b in budgets if b["key"] != left)

    return {"budgets": budgets, "defaults": {"left": left, "right": right},
            "fallback": fallback, "plan_year": plan_year,
            "source_file": ctx["source"]}


def compare(left_key: str | None = None, right_key: str | None = None) -> dict:
    """Two budget selections, one `derive()` output measuring the second against
    the first, and the options both dropdowns need.

    The union of cost-line ids is what makes the missing-line case honest: a line
    the baseline never carried contributes 0 on that side and is NAMED, rather
    than arriving as a −100% that reads like a cut somebody made.

    The OPTIONS travel in every response, including the failures — the same reason
    `tools.scenario_options` exists next to `build_budget_scenario`'s teachable
    errors: two derivations of "the budgets you may pick" is how a dropdown offers
    a value the resolver then rejects. It also means a select self-heals after a
    scenario is deleted in another tab, instead of holding a dead key.

    Returns a dict either way — `available: false` plus a reason for every state
    that is a state rather than a failure, exactly as `/api/cash` does.
    """
    ctx = _comparison_context()
    if isinstance(ctx, str):
        return {"available": False, "error": ctx, "options": [], "defaults": None}

    options = comparison_options(ctx)
    if isinstance(options, str):
        return {"available": False, "error": options, "options": [], "defaults": None}
    envelope = {"options": options["budgets"], "defaults": options["defaults"],
                "fallback": options["fallback"], "plan_year": options["plan_year"],
                "source_file": ctx["source"]}

    left_key = left_key or options["defaults"]["left"]
    right_key = right_key or options["defaults"]["right"]
    left = budget_snapshot(left_key, ctx)
    if isinstance(left, str):
        return {"available": False, "error": left, "field": "left", **envelope}
    right = budget_snapshot(right_key, ctx)
    if isinstance(right, str):
        return {"available": False, "error": right, "field": "right", **envelope}

    # Left's order first, then whatever only the right side carries — so the
    # baseline's own shape leads and additions are visible as additions.
    ids = list(left["lines"]) + [i for i in right["lines"] if i not in left["lines"]]
    only_in = {}
    variables = []
    for line_id in ids:
        a, b = left["lines"].get(line_id), right["lines"].get(line_id)
        # Wording resolves RIGHT-first: the right side is the budget being
        # proposed, and its description is the one the CFO is deciding about.
        spec = b or a
        coverage = "both"
        if a is None:
            coverage = only_in[line_id] = "right"
        elif b is None:
            coverage = only_in[line_id] = "left"
        variables.append({
            "id": line_id, "label": spec["label"], "category": spec["category"],
            "description": spec["description"], "assumption": spec["assumption"],
            "default_note": "", "driver_id": spec["driver_id"],
            "current_amount": a["amount"] if a else 0.0,
            "next_amount": b["amount"] if b else 0.0,
            "coverage": coverage, "include": True,
        })

    # `current_year` / `budget_year` carry the two LABELS, not years. derive()
    # copies them through untouched, so the page and the read both name the budget
    # rather than a year — which matters when both sides are scenarios for the
    # same year. The real years travel in left/right below. validate() is never
    # involved: nothing here is persisted, which is also why a non-consecutive
    # pair is expressible at all.
    plan = {
        "company": get_plan().get("company") or {},
        "baseline": {"current_year": left["label"], "budget_year": right["label"],
                     "revenue": left["revenue"], "revenue_next": right["revenue"]},
        "variables": variables,
    }
    derived = derive(plan)

    same = left["key"] == right["key"]
    gap = "" if same else _gap_note(left, right, ctx)
    notes = [n for n in (left.get("notes") or []) if n]
    notes += [n for n in (right.get("notes") or []) if n and n not in notes]
    if same:
        notes.insert(0, "Both sides are the same budget, so every difference is zero.")
    if gap:
        notes.insert(0, gap)
    period = _period_note(left, right)
    if period:
        notes.append(period)

    return {
        "available": True,
        "left": _side(left), "right": _side(right),
        "derived": derived,
        "only_in": only_in,
        "same": same,
        "notes": notes,
        "narrative": comparison_narrative(derived, left, right, same, gap),
        # The cash strip projects from a scenario's own monthly shape, so a dataset
        # year has nothing for it to read. None is the honest answer and the page
        # renders it as a reason, not as an empty section.
        "cash": ({"scenario_id": right.get("scenario_id")} if right["kind"] == "scenario"
                 else {"scenario_id": None, "reason": (
                     f"A cash profile is projected from a scenario's own monthly shape. "
                     f"\"{right['label']}\" is a dataset {right['kind']}, so there is nothing "
                     f"to project from — pick a scenario on the right to see cash.")}),
        **envelope,
    }


def comparison_narrative(derived: dict, left: dict, right: dict, same: bool,
                         gap: str = "") -> str:
    """The read on a pair. Deterministic, computed, and never a model call.

    `templated_narrative` is the read on ONE budget and its sentences are built
    around a year ("the 2027 cost base"); fed a scenario name it produces "the Pet
    cost breach — forward curve cost base". A comparison is a different sentence —
    it leads with the difference, because that is the question the two dropdowns
    ask — so it gets its own, and shares `_money` and the derived figures rather
    than any arithmetic.
    """
    t = derived["totals"]
    ccy = t["currency"]
    ranked = derived["ranked"]
    if same:
        return (f"Both sides are {right['label']}, so every difference on this page is zero. "
                f"Pick a different budget on either side to see a comparison.")
    if not ranked:
        return (f"Neither {left['label']} nor {right['label']} carries any cost lines, so "
                f"there is nothing to compare.")

    verb = ("carries" if t["cost_delta"] > 0 else "saves") if t["cost_delta"] else "matches"
    if t["cost_delta"]:
        head = (f"Against {left['label']}, {right['label']} {verb} "
                f"{_money(abs(t['cost_delta']), ccy)} "
                f"{'more' if t['cost_delta'] > 0 else 'less'} cost, "
                f"{t['cost_change_pct']:+.1f}%.")
    else:
        head = (f"{right['label']} {verb} {left['label']} on cost at "
                f"{_money(t['cost_next'], ccy)}.")

    first = ranked[0]
    parts = [head,
             f"{first['label']} is the biggest mover at "
             f"{_pct_text(first['expected_change_pct'])} "
             f"({_money(first['delta'], ccy)}), {first['impact_share'] * 100:.0f}% of the "
             f"total change."]
    if len(ranked) > 1:
        second = ranked[1]
        parts.append(f"{second['label']} follows at {_pct_text(second['expected_change_pct'])} "
                     f"({_money(second['delta'], ccy)}).")
    # Stated in the read, not only in the caveat list. In the seeded data 55% of
    # the default pair's revenue step is a region the baseline never budgeted, and
    # a paragraph that explains "+13.3%" without saying so is confidently wrong.
    if gap:
        parts.append(gap)
    parts.append(f"Revenue is {_money(t['revenue_delta'], ccy)} "
                 f"({t['revenue_change_pct']:+.1f}%) and operating margin lands "
                 f"{abs(t['margin_delta_pp']):.1f} points "
                 f"{'lower' if t['margin_delta_pp'] < 0 else 'higher'} at "
                 f"{t['margin_pct_next']:.1f}%.")
    return " ".join(parts)


def _side(snapshot: dict) -> dict:
    """A snapshot without its `lines` — what the header and the selects render.
    The lines already travel inside `derived`, and sending them twice would let
    the two copies disagree."""
    return {k: v for k, v in snapshot.items() if k != "lines"}


def _period_note(left: dict, right: dict) -> str | None:
    """Whether the two sides cover the same number of periods.

    Compared on COUNT, not on the month strings: two budgets for different years
    never share a month, and saying so every time would be noise. Twelve months
    against nine is the real asymmetry, and annual totals are the only honest
    comparison when it holds.
    """
    a, b = len(left.get("months") or []), len(right.get("months") or [])
    if a == b or not a or not b:
        return None
    thin, thick = (left, right) if a < b else (right, left)
    return (f"{thin['label']} covers {min(a, b)} months against {max(a, b)} for "
            f"{thick['label']}. The totals compared here are each budget's own full period.")


# --------------------------------------------------------------------------
# The model layer
# --------------------------------------------------------------------------

def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _money(value: float, currency: str) -> str:
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{currency} {value / 1e9:,.2f}bn"
    if magnitude >= 1e6:
        return f"{currency} {value / 1e6:,.2f}m"
    if magnitude >= 1e3:
        return f"{currency} {value / 1e3:,.1f}k"
    return f"{currency} {value:,.0f}"


def brief(plan: dict, derived: dict | None = None) -> str:
    """A compact plain-text rendering of the whole configuration.

    This is the ONLY thing the model ever sees. It carries the derived figures
    alongside the raw inputs so the model never has to do arithmetic — the same
    discipline the main agent's system prompt enforces with its tools.
    """
    derived = derived or derive(plan)
    t = derived["totals"]
    ccy = t["currency"]
    company = plan.get("company") or {}

    lines = [
        f"Company: {company.get('name')}",
        f"Industry: {(INDUSTRIES.get(company.get('industry')) or {}).get('label', company.get('industry'))}",
        f"Size: {SIZE_LABELS.get(company.get('size'), company.get('size'))}",
        f"Reporting currency: {ccy}",
        f"Fiscal year starts: month {company.get('fiscal_year_start_month')}",
        f"Current year: {t['current_year']}    Budget year: {t['budget_year']}",
        "",
        f"Revenue {t['current_year']}: {_money(t['revenue_current'], ccy)}",
        f"Revenue {t['budget_year']}: {_money(t['revenue_next'], ccy)} "
        f"({t['revenue_change_pct']:+.1f}%)",
        f"Cost base {t['current_year']}: {_money(t['cost_current'], ccy)}",
        f"Cost base {t['budget_year']}: {_money(t['cost_next'], ccy)} "
        f"({t['cost_change_pct']:+.1f}%, {_money(t['cost_delta'], ccy)})",
        f"Operating margin: {_money(t['margin_current'], ccy)} "
        f"({t['margin_pct_current']:.1f}%) -> {_money(t['margin_next'], ccy)} "
        f"({t['margin_pct_next']:.1f}%), {t['margin_delta_pp']:+.1f}pp",
        "",
        "Budgeting variables, ranked by materiality "
        "(share of cost base x expected change):",
    ]
    for r in derived["ranked"]:
        line = (f"{r['rank']}. {r['label']} [{r['category']}] — "
                f"{t['current_year']}: {_money(r['current_amount'], ccy)}, "
                f"{t['budget_year']}: {_money(r['next_amount'], ccy)}, "
                f"change {_pct_text(r['expected_change_pct'])} "
                f"({_money(r['delta'], ccy)}), "
                f"{r['share_of_cost_pct']:.1f}% of the cost base, "
                f"{r['impact_share'] * 100:.0f}% of total movement")
        lines.append(line)
        note = r["assumption"] or r["default_note"]
        if note:
            lines.append(f"     assumption: {note}")
        # Named so the reader can go and look at the locked value and its
        # source, rather than treating the line as a number somebody typed.
        if r.get("driver_id"):
            lines.append(f"     tracked driver: {r['driver_id']}")

    excluded = [v for v in (plan.get("variables") or []) if not v.get("include", True)]
    if excluded:
        lines.append("")
        lines.append("Explicitly EXCLUDED from this budget (do not include them in totals): "
                     + ", ".join(str(v.get("label") or v.get("id")) for v in excluded))
    return "\n".join(lines)


def templated_narrative(plan: dict, derived: dict | None = None) -> str:
    """The offline read. Deterministic, and the fallback whenever the API is not
    reachable — the same degrade-to-computed-text posture as alerts.py."""
    derived = derived or derive(plan)
    t = derived["totals"]
    ccy = t["currency"]
    ranked = derived["ranked"]
    company = (plan.get("company") or {}).get("name") or "The business"

    if not ranked:
        return (f"No budgeting variables are switched on yet, so there is nothing to read "
                f"for {t['budget_year']}. Add at least one cost line in configuration.")

    direction = "rise" if t["cost_delta"] > 0 else "fall" if t["cost_delta"] < 0 else "hold flat"
    first = ranked[0]
    parts = [
        f"{company}'s {t['budget_year']} cost base is set to {direction} to "
        f"{_money(t['cost_next'], ccy)}, {t['cost_change_pct']:+.1f}% on {t['current_year']} "
        f"({_money(t['cost_delta'], ccy)}).",
        f"{first['label']} is the single biggest mover at "
        f"{_pct_text(first['expected_change_pct'])} "
        f"({_money(first['delta'], ccy)}), {first['impact_share'] * 100:.0f}% of the total change.",
    ]
    if len(ranked) > 1:
        second = ranked[1]
        parts.append(f"{second['label']} follows at {_pct_text(second['expected_change_pct'])} "
                     f"({_money(second['delta'], ccy)}).")
    parts.append(
        f"With revenue at {_money(t['revenue_next'], ccy)} ({t['revenue_change_pct']:+.1f}%), "
        f"operating margin moves {t['margin_delta_pp']:+.1f} points to "
        f"{t['margin_pct_next']:.1f}%."
    )
    return " ".join(parts)


def explain_api_failure(exc: Exception) -> str:
    """Turn an SDK exception into something a CFO can act on.

    The raw repr of an auth error is a JSON blob with a request id in it —
    accurate, and useless to the person reading it. The cause is named, and the
    reassurance that the page's own figures are unaffected is part of the
    message because it is the first thing anyone will wonder.
    """
    name = type(exc).__name__
    detail = str(exc)
    if "authentication" in detail.lower() or name == "AuthenticationError":
        reason = ("The ANTHROPIC_API_KEY was rejected. Check the key in .env — the "
                  "written read cannot be generated until it is valid.")
    elif name in ("APIConnectionError", "APITimeoutError") or "connection" in detail.lower():
        reason = "Could not reach the API. Check your connection and try again."
    elif name == "RateLimitError":
        reason = "Rate limited by the API. Wait a moment and try again."
    else:
        reason = f"The request failed ({name})."
    return f"{reason} The budget figures on this page are computed locally and are unaffected."


NARRATIVE_SYSTEM = """You write the opening read on a company's draft budget for next year.

Write 2-4 sentences of plain, direct prose for a CFO. No headings, no bullets, no
markdown, no preamble, no sign-off — just the paragraph.

Rules:
- Use ONLY the figures given to you. Never introduce a number that is not in the
  brief, and never re-derive one; every total you need is already computed.
- Lead with where the cost base and margin land, then name the one or two
  variables actually driving it.
- Say what it means, not what the table already shows. If the margin compresses,
  say so plainly. If one line dominates the movement, say that it does.
- Neutral and specific. No hedging filler ("it is important to note"), no
  cheerleading, no recommendations unless the numbers make one obvious.
- These are the user's own planning assumptions, not a forecast. Do not present
  them as certainty."""


def ensure_narrative(plan: dict, model: str, *, force: bool = False) -> dict:
    """Return the cached narrative, or generate and cache a fresh one.

    Cached on the config fingerprint, so re-opening the page is free and only a
    real change to the numbers costs a call.
    """
    derived = derive(plan)
    fp = fingerprint(plan)
    cached = plan.get("narrative") or {}
    if not force and cached.get("text") and cached.get("fingerprint") == fp:
        return cached

    text = ""
    source = "template"
    if has_api_key():
        try:
            client = get_client()
            response = client.messages.create(
                model=model,
                max_tokens=NARRATIVE_MAX_TOKENS,
                system=NARRATIVE_SYSTEM,
                messages=[{"role": "user", "content": brief(plan, derived)}],
            )
            text = "".join(b.text for b in response.content
                           if getattr(b, "type", None) == "text").strip()
            if text:
                source = model
        except Exception:
            # Never let a network blip blank the page — fall through to the
            # deterministic read, exactly as alerts.py falls back to the title.
            text = ""

    if not text:
        text = templated_narrative(plan, derived)

    narrative = {"text": text, "generated_at": time.time(),
                 "fingerprint": fp, "model": source}
    with _lock:
        stored = _read()
        stored["narrative"] = narrative
        _write(stored)
    return narrative

