# budgetplan.py — "Budget Outlook": a self-contained, config-driven read on next
# year's budget for ANY company.
#
# Deliberately independent of everything the other three agents stand on:
#
#   * It reads NO dataset. Not drivers.parquet, not budget_vs_actuals.parquet,
#     not locked_assumptions.json. Every number on the page comes from what the
#     user typed on the configuration screen. That is what lets it work on a
#     fresh install with no demo data and no company profile.
#   * It has its OWN `configured` flag and is NOT behind main.require_setup, so
#     it is reachable while the rest of the app is still gated on #/setup.
#   * Its chat transcript lives in this module's own file rather than in
#     store.py, so store.KINDS — and therefore the three existing agents — are
#     untouched.
#
# It imports `agent` only for `get_client()`. The model id is passed IN from
# main.py rather than read from config.py, keeping the same injection discipline
# that stops scheduler.py and reporting.py importing main.
#
# All arithmetic is in `derive()`, which is pure and takes no I/O — the same
# split that makes budget.py testable.

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Generator

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
MAX_CHAT_MESSAGES = 40          # trimmed on write; the transcript is a rail, not an archive
NARRATIVE_MAX_TOKENS = 400
CHAT_MAX_TOKENS = 1200

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
    revenue_pct = _f(baseline.get("revenue_change_pct"))
    revenue_next = revenue * (1.0 + revenue_pct / 100.0)

    included = [v for v in (plan.get("variables") or []) if v.get("include", True)]
    excluded_count = len(plan.get("variables") or []) - len(included)

    cost_current = math.fsum(max(0.0, _f(v.get("current_amount"))) for v in included)

    rows = []
    for v in included:
        current = max(0.0, _f(v.get("current_amount")))
        pct = _f(v.get("expected_change_pct"))
        nxt = current * (1.0 + pct / 100.0)
        delta = nxt - current
        if abs(pct) < FLAT_EPS_PCT:
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
             (v.get("assumption") or "").strip()]
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

    return {
        "id": var_id,
        "label": label[:80],
        "category": category,
        "description": str(raw.get("description") or spec.get("description") or "")[:400],
        "current_amount": round(amount, 2),
        "expected_change_pct": round(pct, 4),
        "assumption": str(raw.get("assumption") or "").strip()[:600],
        "default_note": str(raw.get("default_note") or "")[:400],
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
        "chat": [],
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
    """Back to first-run: configuration, narrative AND chat all cleared."""
    with _lock:
        return _write(default_plan())


def append_chat(role: str, text: str) -> dict:
    with _lock:
        plan = _read()
        history = list(plan.get("chat") or [])
        history.append({"role": role, "text": text, "ts": time.time()})
        plan["chat"] = history[-MAX_CHAT_MESSAGES:]
        return _write(plan)


def clear_chat() -> dict:
    with _lock:
        plan = _read()
        plan["chat"] = []
        return _write(plan)


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
                f"change {r['expected_change_pct']:+.1f}% "
                f"({_money(r['delta'], ccy)}), "
                f"{r['share_of_cost_pct']:.1f}% of the cost base, "
                f"{r['impact_share'] * 100:.0f}% of total movement")
        lines.append(line)
        note = r["assumption"] or r["default_note"]
        if note:
            lines.append(f"     assumption: {note}")

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
        f"{first['label']} is the single biggest mover at {first['expected_change_pct']:+.1f}% "
        f"({_money(first['delta'], ccy)}), {first['impact_share'] * 100:.0f}% of the total change.",
    ]
    if len(ranked) > 1:
        second = ranked[1]
        parts.append(f"{second['label']} follows at {second['expected_change_pct']:+.1f}% "
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
        reason = ("The ANTHROPIC_API_KEY was rejected. Check the key in .env — chat "
                  "cannot run until it is valid.")
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


CHAT_SYSTEM = """You are a budgeting adviser with exactly one job: helping this CFO think
through their {budget_year} budget for {company}.

The complete configuration is below. It is the single source of truth.

SCOPE — this is a hard boundary.
You answer questions about {company}'s {budget_year} budget: the variables in the
brief, what drives them, what the assumptions imply, what to sanity-check, how a
different assumption would change the picture, and how to present or defend the
numbers.

Anything else — other companies, prior-year accounting, tax filings, personal
finance, investment advice, coding, general knowledge, current events, or small
talk — is out of scope. Decline in ONE short sentence and redirect to something
concrete you can help with from the brief. Do not answer partially, do not
caveat at length, and do not apologise more than once.

USING THE NUMBERS
- Every figure you cite must come from the brief. Never invent, estimate or
  recall a number that is not there.
- The totals and deltas are already computed. Use them as given.
- If the user asks "what if X changes to Y", you may compute that one arithmetic
  step from the brief's figures and show your working in a sentence. Label it
  clearly as a hypothetical, not as the configured plan.
- If the brief genuinely does not contain what is needed, say so and name what
  the CFO would have to add in configuration. Do not fill the gap with a
  plausible figure.
- These are planning assumptions the CFO entered, not a forecast or a market
  view. Never present them as fact about the world.

STYLE
Direct and brief. Short paragraphs, light markdown, a list only when the content
is genuinely a list. No headings on short answers. Lead with the answer.

--- CONFIGURATION ---
{brief}
--- END CONFIGURATION ---"""


def build_chat_system(plan: dict, derived: dict | None = None) -> str:
    derived = derived or derive(plan)
    company = (plan.get("company") or {}).get("name") or "this company"
    return CHAT_SYSTEM.format(
        company=company,
        budget_year=derived["totals"]["budget_year"],
        brief=brief(plan, derived),
    )


def starter_questions(plan: dict, derived: dict | None = None) -> list[str]:
    """Three or four openers, built from THIS company's ranked variables so they
    are answerable from the brief rather than generic prompts."""
    derived = derived or derive(plan)
    t = derived["totals"]
    ranked = derived["ranked"]
    if not ranked:
        return ["What should I add to this budget?"]

    questions = [f"What's driving the {t['budget_year']} cost increase?"
                 if t["cost_delta"] > 0 else
                 f"Why is the {t['budget_year']} cost base coming down?"]
    questions.append(f"How exposed am I if {ranked[0]['label'].lower()} moves further than "
                     f"{ranked[0]['expected_change_pct']:+.1f}%?")
    if len(ranked) > 1:
        questions.append(f"Which assumptions look weakest — {ranked[0]['label'].lower()} or "
                         f"{ranked[1]['label'].lower()}?")
    questions.append(f"What would it take to hold margin at "
                     f"{t['margin_pct_current']:.1f}%?")
    return questions[:4]


def stream_chat(plan: dict, message: str, model: str) -> Generator[dict, None, None]:
    """One scoped chat turn.

    A deliberately small loop rather than agent.run_agent: this turn has no
    tools, no thinking and no citations, so it needs none of that loop's tool
    rounds, pause_turn handling or per-model capability logic — and reusing it
    would have meant adding override parameters to the one piece of code in the
    repo that most needs to stay as it is.

    Yields a strict SUBSET of run_agent's documented vocabulary — `text`,
    `error`, `done` — so the frontend's createStreamRenderer consumes it
    unchanged. `error` is terminal and is always followed by `done`, matching
    that contract.
    """
    text = (message or "").strip()
    if not text:
        yield {"type": "error", "message": "Empty message."}
        yield {"type": "done"}
        return

    if not has_api_key():
        yield {"type": "error",
               "message": "No ANTHROPIC_API_KEY is set, so chat is unavailable. "
                          "The budget figures on this page are computed locally and "
                          "remain accurate."}
        yield {"type": "done"}
        return

    # The new turn is NOT persisted yet. A failed call that had already stored it
    # would leave a question with no answer in the transcript, and the next
    # attempt would then send two user turns in a row.
    plan = get_plan()
    convo = [{"role": m["role"], "content": m["text"]}
             for m in (plan.get("chat") or []) if m.get("text")]
    convo.append({"role": "user", "content": text})

    collected: list[str] = []
    try:
        client = get_client()
        with client.messages.stream(
            model=model,
            max_tokens=CHAT_MAX_TOKENS,
            system=build_chat_system(plan),
            messages=convo,
        ) as stream:
            for chunk in stream.text_stream:
                collected.append(chunk)
                yield {"type": "text", "text": chunk}
    except Exception as exc:
        yield {"type": "error", "message": explain_api_failure(exc)}
        yield {"type": "done"}
        return

    answer = "".join(collected).strip()
    append_chat("user", text)
    if answer:
        append_chat("assistant", answer)
    yield {"type": "done"}
