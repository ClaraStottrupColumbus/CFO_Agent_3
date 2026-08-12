# tools.py — The local tools exposed to Claude, plus their Anthropic schemas.
#
# Conventions carried over from the reference implementation, all load-bearing:
#   * Errors are teachable {"error": "…"} dicts that ENUMERATE the valid options,
#     so the model corrects itself inside one turn instead of guessing.
#   * Every result carries source_file, except presentation-only tools — that
#     absence is what keeps render_chart out of the citation list.
#   * All arithmetic happens here or in budget.py. The model never does it.
#   * _load prefers Parquet and falls back to CSV, so tests can use CSV fixtures.
#   * render_chart smuggles its validated spec out under the private _chart_spec
#     key: the UI gets the full spec, the model only a compact ack.
#
# One signature change from the reference: execute_tool takes a `ctx` carrying
# {fetched_urls, session_id, profile}. record_driver_observation needs it to
# verify that a cited URL was actually visited this turn — the trust boundary
# described in §4.

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import budget, drivers, ingest, scenarios, uploads

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

CHART_MAX_SERIES = 4
CHART_MAX_POINTS = 36
WATERFALL_MAX_POINTS = 9

# One read_document call returns at most this much markdown (~10k tokens), with
# an offset to page through the rest. Ingest caps a CONVERTED document at
# MAX_EXTRACT_CHARS (200k), but markdown added through add_markdown/save_research
# never passes through that cap — and whatever a read returns is re-sent on every
# remaining round of the turn, so an unbounded read can dominate a turn's spend.
# Generous on purpose: a board memo or a contract should still arrive in one call.
DOCUMENT_WINDOW_CHARS = 40_000

DATASETS = {
    "budget_vs_actuals": {
        "description": "Monthly volumes, revenue, COGS and opex per product line and region — "
                       "actual and budget (EUR). 2024-2026 are closed; 2027 carries budget only.",
        "format": "parquet",
    },
    "product_lines": {
        "description": "One row per product line: segment, pack format, list price per tonne "
                       "and target margin.",
        "format": "parquet",
    },
    "bill_of_materials": {
        "description": "Quantity of each input (driver) consumed per tonne of finished feed, "
                       "per product line. This is what drives sensitivity.",
        "format": "parquet",
    },
    "drivers": {
        "description": "The driver watchlist: category, unit, quote currency, baseline, "
                       "hedge coverage, adverse direction and staleness tolerance.",
        "format": "parquet",
    },
    "driver_prices": {
        "description": "Append-only observation log: month x driver price in the driver's own "
                       "quote currency, with source URL and revision.",
        "format": "parquet",
    },
    "driver_forwards": {
        "description": "Append-only forward curves: one row per curve_date x driver x "
                       "quote_month, priced in the driver's own quote currency with the source "
                       "URL it was read from. This is what a budget on basis 'forward' is "
                       "built from — query it to see the shape of a curve month by month.",
        "format": "parquet",
    },
    "opex_plan": {
        "description": "Monthly operating cost per cost centre: amount, budget, headcount and "
                       "the driver it is linked to. Note: there is NO payroll or salary dataset.",
        "format": "parquet",
    },
    "working_capital": {
        "description": "Monthly balance-sheet working capital: receivables, inventory, payables "
                       "and the cash balance, with the revenue and COGS of the same month. This "
                       "is what DSO, DIO and DPO are MEASURED from — they are never typed in.",
        "format": "parquet",
    },
    "capex_plan": {
        "description": "Capital expenditure by month: project, category, amount, budget and "
                       "whether it is committed or still proposed. A proposed project is a cash "
                       "lever the CFO can defer; a committed one is not.",
        "format": "parquet",
    },
    "budget_overview": {
        "description": "Curated KPI overview for the latest closed month, kept fresh by the "
                       "scheduled data refresh: revenue vs budget, margins, EBITDA, unit cost.",
        "format": "csv",
    },
}

NUMERIC_COLUMNS = {
    "budget_vs_actuals": ["volume_tonnes_actual", "volume_tonnes_budget",
                          "revenue_actual_eur", "revenue_budget_eur",
                          "cogs_actual_eur", "cogs_budget_eur",
                          "opex_actual_eur", "opex_budget_eur"],
    "opex_plan": ["amount_eur", "amount_budget_eur", "headcount"],
    "driver_prices": ["price", "revision"],
    "driver_forwards": ["price", "revision"],
    "working_capital": ["revenue_eur", "cogs_eur", "receivables_eur", "inventory_eur",
                        "payables_eur", "cash_eur"],
    "capex_plan": ["amount_eur", "amount_budget_eur"],
}

# What a scenario prices its drivers at. The percentages in `assumptions` always
# mean "versus the locked value" — the basis decides what the price they apply to
# is, and every delta is still measured back to the locked baseline the budget
# was built on, so the three are directly comparable.
SCENARIO_BASES = {
    "locked": "The values frozen into the budget. Percentages are the only movement.",
    "spot": "Today's observed market prices, with your percentages on top — "
            "'what does the budget look like if the market stays exactly here'.",
    "forward": "The recorded forward curve month by month, with your percentages on "
               "top. Monthly cost then carries the curve's own shape instead of a "
               "flat annual assumption.",
}

BASIS_COLUMNS = {
    "actual": {"volume": "volume_tonnes_actual", "revenue": "revenue_actual_eur",
               "cogs": "cogs_actual_eur", "opex": "opex_actual_eur"},
    "budget": {"volume": "volume_tonnes_budget", "revenue": "revenue_budget_eur",
               "cogs": "cogs_budget_eur", "opex": "opex_budget_eur"},
}

# There is deliberately no extrapolation tool here. `project_series` — linear
# trend plus monthly seasonality over a driver's own history — was removed once
# `driver_forwards` landed: a fitted trend is a claim about the future with no
# source behind it, and this app's whole position is that an assumption without
# provenance cannot be defended. Forward prices now come from the curve the
# market publishes (record_driver_forward → basis="forward"), and forward P&L
# from build_budget_scenario. If neither has data, the answer is "we have not
# looked", not a regression line.


# --------------------------------------------------------------------------
# Tool schemas
# --------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "list_datasets",
        "description": "Discover what is available: the internal datasets (with columns, row "
                       "counts and covered period), any datasets and documents the CFO has "
                       "added on the Data ingestion page, the driver watchlist, the saved "
                       "budget scenarios and the currently locked assumptions. Datasets and "
                       "documents can be added or removed at any time, so call this first "
                       "whenever you are unsure what data exists.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_document",
        "description": "Read a document the CFO added on the Data ingestion page (PDF, Word, "
                       "PowerPoint, text) as markdown. Call list_datasets first to see which "
                       "documents exist. Cite the document by its file name in your answer. "
                       "Long documents come back one window at a time: if the result has "
                       "`truncated: true`, call again with `offset` set to its `next_offset` "
                       "to continue. Only page on if the part you have does not answer the "
                       "question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Document name as reported by list_datasets."},
                "offset": {"type": "integer",
                           "description": "Character offset to read from. Omit for the start; "
                                          "otherwise pass the previous result's next_offset."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_budget_data",
        "description": "Escape hatch for questions the purpose-built tools do not cover. "
                       "Filter, group, aggregate, sort and limit any internal dataset. "
                       "Returns `columns` (names, in order) and `rows` (each row a list of "
                       "values positionally matching `columns`), plus `total_matching_rows`, "
                       "a `truncated` flag and the source file. Prefer `columns`/`group_by`/"
                       "`filters` to narrow the query over pulling wide rows and reading them "
                       "yourself. Do NOT use it to compute variances, sensitivities or "
                       "projections — the dedicated tools do that correctly.",
        "input_schema": {
            "type": "object",
            "properties": {
                # No enum. TOOL_DEFINITIONS is built once at import, but the CFO can add
                # or remove datasets at runtime on the Data ingestion page, so an enum
                # here would freeze the set at server start and silently exclude every
                # upload. The name is validated on execution instead, and an unknown one
                # comes back as a teachable error listing what does exist.
                "dataset": {"type": "string",
                            "description": "Which dataset, by name from list_datasets."},
                "columns": {"type": "array", "items": {"type": "string"},
                            "description": "Columns to return (default: all)."},
                "filters": {"type": "array", "description": "Row filters, ANDed together.",
                            "items": {"type": "object", "properties": {
                                "column": {"type": "string"},
                                "op": {"type": "string",
                                       "enum": ["==", "!=", ">", ">=", "<", "<=", "in", "contains"]},
                                "value": {}}, "required": ["column", "op", "value"]}},
                "group_by": {"type": "array", "items": {"type": "string"}},
                "aggregate": {"type": "object",
                              "description": "{column: 'sum'|'mean'|'min'|'max'|'count'}"},
                "sort": {"type": "object", "properties": {
                    "column": {"type": "string"}, "desc": {"type": "boolean"}}},
                "limit": {"type": "integer", "description": "Max rows (default 100, cap 500)."},
            },
            "required": ["dataset"],
        },
    },
    {
        "name": "variance_analysis",
        "description": "Explain a revenue or COGS gap by decomposing it into price, volume, mix "
                       "and joint effects, which sum exactly to the total. Use this for EVERY "
                       "actual-vs-budget, vs-prior-year or vs-scenario comparison — never derive "
                       "a variance yourself from query_budget_data rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["revenue", "cogs"],
                           "description": "Which line to decompose."},
                "period": {"type": "string",
                           "description": "Month 'YYYY-MM', year 'YYYY', or 'latest'."},
                "compare_to": {"type": "string", "enum": ["budget", "prior_year", "prior_month"],
                               "description": "The baseline to compare against."},
                "group_by": {"type": "string", "enum": ["product_line", "region"],
                             "description": "What the effects are attributed to (default product_line)."},
                "product_line": {"type": "string", "description": "Optional: restrict to one line."},
                "region": {"type": "string", "description": "Optional: restrict to one region."},
            },
            "required": ["metric", "period", "compare_to"],
        },
    },
    {
        "name": "cost_buildup",
        "description": "The EUR-per-tonne cost bridge for a product line: every ingredient, "
                       "packaging, freight, energy and conversion cost, with each input's share "
                       "and the driver behind it. Use this to explain WHY a unit cost moved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_line": {"type": "string", "description": "Which product line."},
                "month": {"type": "string", "description": "Month 'YYYY-MM' (default: latest)."},
                "compare_month": {"type": "string",
                                  "description": "Optional second month to bridge against."},
            },
            "required": ["product_line"],
        },
    },
    {
        "name": "driver_status",
        "description": "Per driver: the value locked into the budget, the latest observation, "
                       "the drift between them, how stale it is, and whether the move is adverse "
                       "or favourable. This is the watchlist the CFO trusts the budget on.",
        "input_schema": {
            "type": "object",
            "properties": {
                "driver_id": {"type": "string", "description": "Optional: just one driver."},
                "category": {"type": "string", "description": "Optional: one category."},
                "only_stale": {"type": "boolean", "description": "Only drivers past their staleness limit."},
                "only_drifted": {"type": "boolean", "description": "Only drivers that moved >10% since lock."},
            },
        },
    },
    {
        "name": "record_driver_observation",
        "description": "Record a driver price you researched on the web, so the analysis tools "
                       "can compute on it. You MUST have fetched the page you cite with "
                       "web_fetch in THIS turn — a URL you did not visit is refused. Record the "
                       "price in the driver's own quote currency and do not convert it yourself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "driver_id": {"type": "string", "description": "Must exist in the drivers dataset."},
                "price": {"type": "number", "description": "In the driver's quote currency."},
                "month": {"type": "string", "description": "Month the price refers to, 'YYYY-MM'."},
                "source_url": {"type": "string",
                               "description": "The exact URL you fetched this figure from."},
                "note": {"type": "string", "description": "Short context, e.g. what moved and why."},
                "override_sanity_check": {
                    "type": "boolean",
                    "description": "Set true ONLY to record a genuine large move that the "
                                   "0.2x-5.0x sanity band rejected, and explain it in your answer."},
            },
            "required": ["driver_id", "price", "month", "source_url"],
        },
    },
    {
        "name": "record_driver_forward",
        "description": "Record a FORWARD PRICE CURVE you researched on the web — the prices the "
                       "market is quoting for future months, which is what a budget should be "
                       "built on rather than an extrapolation of the past. You MUST have fetched "
                       "the page you cite with web_fetch in THIS turn; a URL you did not visit is "
                       "refused. Record every month the page quotes in one call, in the driver's "
                       "own quote currency, and do not convert or interpolate figures yourself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "driver_id": {"type": "string", "description": "Must exist in the drivers dataset."},
                "points": {
                    "type": "array",
                    "description": "The curve, one entry per quoted month, as read off the page.",
                    "items": {"type": "object", "properties": {
                        "quote_month": {"type": "string", "description": "The month the price is "
                                                                        "quoted FOR, 'YYYY-MM'."},
                        "price": {"type": "number", "description": "In the driver's quote currency."},
                    }, "required": ["quote_month", "price"]},
                },
                "source_url": {"type": "string",
                               "description": "The exact URL you fetched this curve from."},
                "curve_date": {"type": "string",
                               "description": "The date the curve was published, 'YYYY-MM-DD' "
                                              "(default: today)."},
                "note": {"type": "string", "description": "Short context, e.g. contango or "
                                                          "backwardation and why."},
                "override_sanity_check": {
                    "type": "boolean",
                    "description": "Set true ONLY to record a genuine large move that the "
                                   "0.2x-5.0x band against the latest spot rejected, and explain "
                                   "it in your answer."},
            },
            "required": ["driver_id", "points", "source_url"],
        },
    },
    {
        "name": "driver_sensitivity",
        "description": "What a +/-X% move in one or more drivers does to COGS, EBITDA and margin, "
                       "computed from the bill of materials and hedge coverage — plus the "
                       "breakeven shock that would push EBITDA margin to the floor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "driver_ids": {"type": "array", "items": {"type": "string"},
                               "description": "Drivers to shock (default: all watchlist drivers)."},
                "shock_pct": {"type": "number", "description": "Percentage move to apply (default 10)."},
                "year": {"type": "string", "description": "Volume basis year (default: budget year)."},
                "floor_margin_pct": {"type": "number",
                                     "description": "EBITDA margin floor for breakeven (default 8)."},
                "price_pass_through": {"type": "number",
                                       "description": "0-1 fraction of cost recovered in price (default 0.35)."},
            },
        },
    },
    {
        "name": "build_budget_scenario",
        "description": "Apply an assumption set to the budget baseline and get a full monthly P&L "
                       "projection back. This is the ONLY place volume, price, driver and opex "
                       "assumptions may be turned into numbers — never state a projected revenue, "
                       "COGS, opex or EBITDA figure that did not come out of this tool. The "
                       "scenario is PERSISTED so later revisions can compare against it. Every "
                       "percentage is a change versus the budget baseline (drivers: versus their "
                       "locked value).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short scenario name, e.g. 'Freight stays high'."},
                "assumptions": {"type": "object",
                                "description": "Input-cost shocks: {driver_id: pct change vs locked "
                                               "value}, e.g. {\"chicken_meal\": 15, \"wheat\": -8}."},
                "volume": {"type": "object",
                           "description": "Volume decisions: {product_line: pct change vs budget "
                                          "volume}. Use \"*\" for every line. Revenue and COGS scale "
                                          "with volume; opex does not."},
                "price": {"type": "object",
                          "description": "Selling-price decisions: {product_line: pct change}. Use "
                                         "\"*\" for every line. Setting a price on a line REPLACES "
                                         "price_pass_through there — the two are the same lever."},
                "opex": {"type": "object",
                         "description": "Opex growth: {cost_centre or driver_id: pct}. A cost centre "
                                        "not named here moves by its linked driver's percentage if "
                                        "it has one, else by opex_inflation_pct."},
                "note": {"type": "string", "description": "One line on what this scenario represents."},
                "basis": {"type": "string", "enum": list(SCENARIO_BASES),
                          "description": "What the drivers are priced at before your percentages "
                                         "are applied (default 'locked'). "
                                         + " ".join(f"'{k}': {v}" for k, v in SCENARIO_BASES.items())},
                "price_pass_through": {"type": "number",
                                       "description": "0-1 automatic cost recovery, applied only to "
                                                      "lines with no explicit price (default 0.35)."},
                "opex_inflation_pct": {"type": "number",
                                       "description": "Default growth for unmapped cost centres "
                                                      "(default 0)."},
                "make_active": {"type": "boolean",
                                "description": "Make this the scenario the budget rests on."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "cash_flow_projection",
        "description": "Turn a saved scenario's monthly EBITDA into a monthly CASH profile: the "
                       "working-capital swing its trading pattern implies, capex, optional cash "
                       "tax, the cash balance month by month and the month cash troughs. Use it "
                       "whenever cash, liquidity, funding, working capital, DSO/DPO/DIO or capex "
                       "come up — EBITDA is not cash, and a budget can add EBITDA while "
                       "consuming cash because the receivables and inventory behind the extra "
                       "revenue have to be funded first. The days are MEASURED off the "
                       "working_capital dataset unless the CFO has stated them; the returned "
                       "`days.basis` says which, and you must say so too. This is free cash flow "
                       "before financing — depreciation, interest and debt are not modelled and "
                       "`caveats` says so.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario_id": {"type": "string",
                                "description": "Which scenario (default: the active one)."},
                "dso_days": {"type": "number",
                             "description": "Override the measured days sales outstanding — a "
                                            "collection DECISION, not an observation."},
                "dio_days": {"type": "number",
                             "description": "Override the measured days inventory outstanding."},
                "dpo_days": {"type": "number",
                             "description": "Override the measured days payable outstanding."},
                "opening_cash_eur": {"type": "number",
                                     "description": "Opening cash (default: the latest closed "
                                                    "month's balance in working_capital)."},
                "tax_rate_pct": {"type": "number",
                                 "description": "Cash tax rate on positive EBITDA. Omit unless "
                                                "the CFO has stated one — no tax is deducted "
                                                "without it, and the result says so."},
                "min_cash_eur": {"type": "number",
                                 "description": "Covenant or comfort floor; every month below it "
                                                "is named back."},
                "include_proposed_capex": {"type": "boolean",
                                           "description": "Default true. False defers the capex "
                                                          "projects still marked proposed — the "
                                                          "one real cash lever here."},
                "measure_months": {"type": "integer",
                                   "description": "Closed months to measure the days over "
                                                  "(default 12)."},
            },
        },
    },
    {
        "name": "budget_outlook",
        "description": "The Budget page as the CFO sees it: next year's cost lines ranked by "
                       "materiality, each with its amount, expected change and the driver it "
                       "is tracked against, plus revenue, cost base and operating margin. Use "
                       "it when asked about 'the budget' as a whole, or about a cost line that "
                       "is not one of the watchlist drivers — it is the only tool that sees "
                       "lines the bill of materials does not.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "lock_assumptions",
        "description": "Freeze an agreed assumption set as the budget's official position, with "
                       "provenance. Use ONLY when the CFO explicitly agrees to lock — routine "
                       "research must never overwrite a locked assumption.",
        "input_schema": {
            "type": "object",
            "properties": {
                "assumptions": {"type": "object",
                                "description": "{driver_id: {value, unit, source_url, rationale}}"},
                "scenario_id": {"type": "string", "description": "Scenario this lock corresponds to."},
                "note": {"type": "string"},
            },
            "required": ["assumptions"],
        },
    },
    {
        "name": "render_chart",
        "description": "Draw a chart in the answer. 'waterfall' is the budget-to-actual bridge: "
                       "one series whose points each carry kind 'absolute' (a total) or 'delta' "
                       "(a step). Use only figures returned by the other tools, never invented "
                       "points, and still state the key numbers in your text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["line", "bar", "waterfall"]},
                "title": {"type": "string"},
                "unit": {"type": "string", "description": "e.g. 'EUR', 'EUR/t', '%'."},
                "series": {
                    "type": "array",
                    "description": "Series to plot. A waterfall takes exactly one.",
                    "items": {"type": "object", "properties": {
                        "name": {"type": "string"},
                        "points": {"type": "array", "items": {"type": "object", "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "number"},
                            "kind": {"type": "string", "enum": ["absolute", "delta"],
                                     "description": "Waterfall only."},
                        }, "required": ["label", "value"]}},
                    }, "required": ["name", "points"]},
                },
            },
            "required": ["chart_type", "title", "series"],
        },
    },
]


# --------------------------------------------------------------------------
# Loading helpers
# --------------------------------------------------------------------------

def dataset_meta(dataset: str) -> dict | None:
    """Resolve a dataset name to its metadata — built-in or CFO-uploaded.

    THIS IS THE ONLY PLACE THAT DISTINCTION EXISTS. Uploaded datasets (uploads.py)
    live in data/uploads/ and are otherwise identical: the same CSV+Parquet pair,
    so _load below needs no special case, and neither does the model — it never
    knows or cares where a dataset came from. Keeping that seam to one function is
    what stops "add your own data" doubling the surface area of every tool that
    touches data.

    Returns None for an unknown name; callers turn that into a teachable error.
    """
    if dataset in DATASETS:
        return {**DATASETS[dataset], "dir": DATA_DIR, "builtin": True}
    record = uploads.get_by_name(dataset)
    if record and record.get("kind") == "dataset":
        return {"description": record["description"], "format": record["format"],
                "dir": uploads.UPLOAD_DIR, "builtin": False,
                "original_filename": record.get("original_filename")}
    return None


def dataset_names() -> list[str]:
    return list(DATASETS) + uploads.dataset_names()


def _source_label(path: Path) -> str:
    """The citation string for a loaded file: its path relative to data/, with
    forward slashes on every platform — 'budget_overview.csv' for a built-in
    (unchanged, since a top-level file's relative path is its bare name) and
    'uploads/board_pack.csv' for an upload.

    Falls back to the bare filename when the file is not under DATA_DIR at all.
    uploads.UPLOAD_DIR is derived from its OWN module constant, so the two roots
    can legitimately diverge (a test patching one and not the other), and
    relative_to would raise ValueError — which none of _load's callers catch,
    since they guard FileNotFoundError/OSError. citations.dataset_record labels
    off rsplit("/") anyway, so the bare name degrades cleanly.
    """
    try:
        return str(path.relative_to(DATA_DIR)).replace("\\", "/")
    except ValueError:
        return path.name


def _load(dataset: str) -> tuple[pd.DataFrame, str]:
    """Load a dataset, returning the frame and the file it came from.

    Prefers Parquet; falls back to CSV so tests can use CSV fixtures. Built-in
    and uploaded resolve through dataset_meta and are read by identical code —
    the one seam is meta["dir"].
    """
    meta = dataset_meta(dataset)
    if meta is None:
        raise KeyError(dataset)
    directory = meta["dir"]
    if meta["format"] == "parquet":
        path = directory / f"{dataset}.parquet"
        if path.exists():
            return pd.read_parquet(path), _source_label(path)
    path = directory / f"{dataset}.csv"
    return pd.read_csv(path), _source_label(path)


def _unknown_dataset(dataset: str) -> dict:
    return {"error": f"Unknown dataset '{dataset}'. Available: {', '.join(dataset_names())}. "
                     "Call list_datasets to see what exists."}


def _unknown(kind: str, value, options) -> dict:
    opts = ", ".join(str(o) for o in options)
    return {"error": f"Unknown {kind} '{value}'. Valid options: {opts}."}


def _closed(bva: pd.DataFrame) -> pd.DataFrame:
    """Rows with actuals — 2027 is budget-only."""
    return bva[bva["revenue_actual_eur"].notna()]


def _latest_closed_month(bva: pd.DataFrame) -> str | None:
    closed = _closed(bva)
    return None if closed.empty else str(closed["month"].max())


def _driver_eur_prices(month: str | None = None) -> tuple[dict, dict]:
    """({driver_id: EUR price}, {driver_id: quote-currency price}) for a month.

    USD-quoted drivers are converted with the eur_usd driver's own observation
    for that month, so FX is real arithmetic rather than a label. This is the
    conversion the model must never do itself.
    """
    prices = drivers.load_prices()
    catalog = drivers.load_catalog()
    if prices.empty or catalog.empty:
        return {}, {}
    if month:
        prices = prices[prices["month"] <= month]
    latest = (prices.sort_values(["month", "revision"], kind="stable")
              .groupby("driver_id").tail(1).set_index("driver_id"))
    quote = {str(k): float(v) for k, v in latest["price"].items()}
    fx = quote.get("eur_usd") or 1.0
    ccy = dict(zip(catalog["driver_id"].astype(str), catalog["quote_currency"].astype(str)))
    eur = {d: (p / fx if ccy.get(d) == "USD" and d != "eur_usd" else p)
           for d, p in quote.items()}
    return eur, quote


# --------------------------------------------------------------------------
# 1. list_datasets
# --------------------------------------------------------------------------

def _dataset_info(name: str, meta: dict) -> dict:
    """One list_datasets entry — or an error entry if the file has gone missing.
    Built-in and uploaded differ only in the hint, never in the shape."""
    try:
        df, source = _load(name)
    except (FileNotFoundError, OSError):
        hint = ("data file missing — run app/generate_data.py" if meta.get("builtin")
                else "uploaded file is missing — remove it on the Data ingestion page "
                     "and add it again")
        return {"name": name, "error": hint}
    info = {"name": name, "description": meta["description"], "source_file": source,
            "format": meta["format"], "columns": list(df.columns), "rows": len(df),
            "uploaded": not meta.get("builtin")}
    if "month" in df.columns and len(df):
        info["period"] = f"{df['month'].min()} to {df['month'].max()}"
    if "last_refreshed_utc" in df.columns and len(df):
        info["last_refreshed_utc"] = str(df["last_refreshed_utc"].iloc[0])
    return info


def list_datasets() -> dict:
    out = [_dataset_info(name, {**meta, "builtin": True}) for name, meta in DATASETS.items()]
    for record in uploads.list_uploads("dataset"):
        meta = dataset_meta(record["name"])
        if meta:
            # upload_id is what lets the Data ingestion page offer a Remove button;
            # the built-ins have none, which is what makes them undeletable in the
            # UI with no extra logic.
            out.append({**_dataset_info(record["name"], meta), "upload_id": record["id"]})

    documents = [{
        "name": d["name"],
        "file_name": d["original_filename"],
        "format": d["format"],
        "description": d["description"],
        "upload_id": d["id"],
        # "research" for an answer saved from a chat, "upload" for a file.
        "origin": d.get("origin", "upload"),
    } for d in uploads.list_uploads("document")]

    status = drivers.driver_status()
    locked = drivers.load_assumptions()
    saved = [scenarios.summary(s) for s in scenarios.list_scenarios()]

    return {
        "datasets": out,
        "documents": documents,
        "watchlist_drivers": [{"driver_id": d["driver_id"], "name": d["name"],
                               "category": d["category"], "unit": d["unit"],
                               "verify_status": d["verify_status"]} for d in status],
        "locked_assumptions": {"locked_at": locked.get("locked_at"),
                               "count": len(locked.get("assumptions") or {}),
                               "drivers": sorted(locked.get("assumptions") or {})},
        "scenarios": saved,
        "notes": ["There is no payroll, salary or per-employee dataset. Headcount by cost "
                  "centre is in opex_plan, but wage rates are not available — say so rather "
                  "than estimating.",
                  "Unit price and unit cost are not stored; they are derived as "
                  "revenue/volume and cogs/volume."],
        "source_file": "budget_overview.csv",
    }


# --------------------------------------------------------------------------
# 1b. read_document
# --------------------------------------------------------------------------

def read_document(name: str, offset: int = 0) -> dict:
    """Return a CFO-added document as markdown, converted at upload time.

    A missing cache entry is a teachable error rather than an exception, the same
    as every other failure path here — the model relays it and the CFO knows to
    re-add the file.

    Windowed rather than whole-file, for the same reason `query_budget_data` caps
    at 500 rows. Ingest bounds a converted document at MAX_EXTRACT_CHARS (200k,
    ~55k tokens) and markdown added via `add_markdown`/`save_research` is not
    bounded at all — and whatever comes back here is re-sent on every remaining
    round of the turn. One unlucky read could dominate a whole turn's spend.

    The window is stated in the result, in the same shape the other bulk tool
    uses (`truncated` + a total), so the model can ask for the next one rather
    than silently reasoning over a document it only half saw.
    """
    record = uploads.get_by_name(name)
    if not record or record.get("kind") != "document":
        available = [d["name"] for d in uploads.list_uploads("document")]
        return {"error": f"Unknown document '{name}'." +
                         (f" Available: {', '.join(available)}." if available else
                          " No documents have been added on the Data ingestion page.")}
    markdown = ingest.cached_markdown(record.get("cache_key") or "")
    if markdown is None:
        return {"error": f"The converted text for '{name}' is missing. Remove the document on "
                         "the Data ingestion page and add it again."}

    total = len(markdown)
    start = max(0, int(offset or 0))
    if start >= total and total:
        return {"error": f"offset {start} is past the end of '{name}', which is "
                         f"{total} characters. Use an offset below that."}
    window = markdown[start:start + DOCUMENT_WINDOW_CHARS]
    end = start + len(window)
    result = {
        "source_file": record["original_filename"],
        "document": name,
        "format": record["format"],
        "content": window,
        "char_offset": start,
        "total_chars": total,
        "truncated": end < total,
    }
    if end < total:
        result["next_offset"] = end
    return result


# --------------------------------------------------------------------------
# 2. query_budget_data
# --------------------------------------------------------------------------

def query_budget_data(dataset: str, columns=None, filters=None, group_by=None,
                      aggregate=None, sort=None, limit=None) -> dict:
    if dataset_meta(dataset) is None:
        return _unknown_dataset(dataset)
    try:
        df, source = _load(dataset)
    except (FileNotFoundError, OSError):
        return {"error": f"Dataset '{dataset}' has no data file — run app/generate_data.py, "
                         "or re-add it on the Data ingestion page if you uploaded it."}

    for f in filters or []:
        col, op, val = f.get("column"), f.get("op"), f.get("value")
        if col not in df.columns:
            return _unknown("column", col, list(df.columns))
        try:
            if op == "==":
                df = df[df[col] == val]
            elif op == "!=":
                df = df[df[col] != val]
            elif op == ">":
                df = df[df[col] > val]
            elif op == ">=":
                df = df[df[col] >= val]
            elif op == "<":
                df = df[df[col] < val]
            elif op == "<=":
                df = df[df[col] <= val]
            elif op == "in":
                df = df[df[col].isin(val if isinstance(val, list) else [val])]
            elif op == "contains":
                df = df[df[col].astype(str).str.contains(str(val), case=False, na=False)]
            else:
                return _unknown("operator", op, ["==", "!=", ">", ">=", "<", "<=", "in", "contains"])
        except TypeError as exc:
            return {"error": f"Filter on '{col}' failed: {exc}."}

    if group_by:
        missing = [c for c in group_by if c not in df.columns]
        if missing:
            return _unknown("group_by column", missing[0], list(df.columns))
        if not aggregate:
            # Curated datasets declare their summable columns above. An upload has
            # nothing to declare, so infer them off the frame — a deliberate
            # simplification: good enough for an ad-hoc file, not curated.
            default = NUMERIC_COLUMNS.get(dataset)
            if default is None:
                default = [c for c in df.columns
                           if c not in group_by and pd.api.types.is_numeric_dtype(df[c])]
            aggregate = {c: "sum" for c in default if c in df.columns}
        if not aggregate:
            return {"error": "group_by needs an aggregate, e.g. "
                             "{\"revenue_actual_eur\": \"sum\"}."}
        bad = [c for c in aggregate if c not in df.columns]
        if bad:
            return _unknown("aggregate column", bad[0], list(df.columns))
        try:
            df = df.groupby(group_by, as_index=False).agg(aggregate)
        except (TypeError, ValueError) as exc:
            return {"error": f"Aggregation failed: {exc}."}
    elif columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            return _unknown("column", missing[0], list(df.columns))
        df = df[columns]

    if sort:
        col = sort.get("column")
        if col not in df.columns:
            return _unknown("sort column", col, list(df.columns))
        df = df.sort_values(col, ascending=not sort.get("desc", False))

    total = len(df)
    cap = min(int(limit or 100), 500)
    df = df.head(cap)
    columns, rows = _columnar(df)
    return {"columns": columns, "rows": rows,
            "row_count": len(df), "total_matching_rows": total,
            "truncated": total > len(df), "source_file": source}


def _records(df: pd.DataFrame) -> list[dict]:
    """JSON-safe records: NaN becomes None, numpy scalars become Python scalars."""
    return df.replace({np.nan: None}).to_dict(orient="records")


def _columnar(df: pd.DataFrame) -> tuple[list[str], list[list]]:
    """The same rows as `_records`, as a header plus positional rows.

    This is the ONE result shape in this module that is not a list of dicts, and
    the reason is size rather than taste. `query_budget_data` is the only tool
    that returns bulk rows — up to 500 — and a list of dicts repeats every column
    name on every row. On `budget_vs_actuals` at the 500 cap that repetition was
    most of a ~160k-char result, which then rode along in the conversation for
    every remaining round of the turn.

    Nothing is lost: `columns` carries the same names, in order, once. Keep the
    small structured results elsewhere as dicts — there the key on each value is
    what makes the result readable, and there are a handful of rows, not 500.
    """
    clean = df.replace({np.nan: None})
    columns = [str(c) for c in clean.columns]
    return columns, clean.to_numpy(dtype=object).tolist()


def rows_as_records(result: dict) -> dict:
    """`query_budget_data`'s payload with `columns`/`rows` folded back into the
    list-of-dicts shape, for HTTP callers.

    The columnar shape exists to keep bulk rows out of the MODEL's context. An
    HTTP response is not under that pressure, and a JSON API should not change
    shape underneath its callers because of an unrelated token optimisation —
    so the saving is applied where it pays and reversed where it does not.
    """
    if "columns" not in result or "rows" not in result:
        return result
    folded = {k: v for k, v in result.items() if k != "columns"}
    folded["rows"] = [dict(zip(result["columns"], row)) for row in result["rows"]]
    return folded


# --------------------------------------------------------------------------
# 3. variance_analysis
# --------------------------------------------------------------------------

def variance_analysis(metric: str, period: str, compare_to: str,
                      group_by: str = "product_line", product_line=None, region=None) -> dict:
    if metric not in ("revenue", "cogs"):
        return _unknown("metric", metric, ["revenue", "cogs"])
    if compare_to not in ("budget", "prior_year", "prior_month"):
        return _unknown("compare_to", compare_to, ["budget", "prior_year", "prior_month"])
    if group_by not in ("product_line", "region"):
        return _unknown("group_by", group_by, ["product_line", "region"])

    bva, source = _load("budget_vs_actuals")
    if product_line:
        if product_line not in set(bva["product_line"]):
            return _unknown("product_line", product_line, sorted(set(bva["product_line"])))
        bva = bva[bva["product_line"] == product_line]
    if region:
        if region not in set(bva["region"]):
            return _unknown("region", region, sorted(set(bva["region"])))
        bva = bva[bva["region"] == region]

    months = _resolve_period(bva, period)
    if isinstance(months, dict):
        return months

    cur = bva[bva["month"].isin(months)]
    if compare_to == "budget":
        base_rows, base_basis, base_label = cur, "budget", f"budget {_label(months)}"
        cur_rows = cur
    else:
        shift = 12 if compare_to == "prior_year" else 1
        base_months = _shift_months(months, -shift)
        base_rows = bva[bva["month"].isin(base_months)]
        base_basis, base_label = "actual", f"actual {_label(base_months)}"
        cur_rows = cur
        if base_rows.empty:
            return {"error": f"No data for the comparison period {_label(base_months)}. "
                             f"Available months: {bva['month'].min()} to {bva['month'].max()}."}

    base = _price_volume_rows(base_rows, metric, base_basis, group_by)
    current = _price_volume_rows(cur_rows, metric, "actual", group_by)
    if not current:
        return {"error": f"No actuals for {_label(months)} — 2027 is the budget year and has "
                         f"no actuals yet. The latest closed month is "
                         f"{_latest_closed_month(bva)}."}
    if not base:
        entity = product_line or region or "this selection"
        return {"error": f"No {base_label} figures exist for {entity}. This happens where a "
                         f"region opened after the budget was set — state that the comparison "
                         f"is not available rather than estimating it."}

    result = budget.variance_decomposition(base, current, key=group_by,
                                           price_key="price", volume_key="volume")
    if "error" in result:
        return result

    lines = sorted(result["lines"],
                   key=lambda r: abs(r["price_effect"] + r["joint_effect"]), reverse=True)
    return {
        "metric": metric, "period": _label(months), "compare_to": compare_to,
        "baseline": base_label, "group_by": group_by,
        "base_value_eur": round(result["base_value"], 2),
        "current_value_eur": round(result["current_value"], 2),
        "total_delta_eur": round(result["total_delta"], 2),
        "total_delta_pct": (round(result["total_delta"] / result["base_value"] * 100.0, 2)
                            if result["base_value"] else None),
        "effects_eur": _rounded_effects(result),
        "base_unit_price_eur": round(result["base_avg_price"], 2),
        "base_volume_tonnes": round(result["base_volume"], 1),
        "current_volume_tonnes": round(result["current_volume"], 1),
        "by_entity": [{group_by: r[group_by],
                       "base_unit_value_eur": round(r["base_price"], 2),
                       "current_unit_value_eur": round(r["current_price"], 2),
                       "base_volume_tonnes": round(r["base_volume"], 1),
                       "current_volume_tonnes": round(r["current_volume"], 1),
                       "price_effect_eur": round(r["price_effect"], 2),
                       "joint_effect_eur": round(r["joint_effect"], 2)} for r in lines],
        "note": "price + volume + mix + joint sum exactly to the total delta. "
                "For COGS a positive price effect means costs rose.",
        "source_file": source,
    }


def _rounded_effects(result: dict) -> dict:
    """Round for display while preserving additivity: joint absorbs the residual,
    which is what it already is mathematically."""
    price = round(result["price_effect"], 2)
    volume = round(result["volume_effect"], 2)
    mix = round(result["mix_effect"], 2)
    total = round(result["total_delta"], 2)
    return {"price": price, "volume": volume, "mix": mix,
            "joint": round(total - price - volume - mix, 2), "total": total}


def _price_volume_rows(df: pd.DataFrame, metric: str, basis: str, group_by: str) -> list[dict]:
    """Aggregate to {entity, price, volume}, where price is the DERIVED unit value
    (value / volume) — never a stored column, so there is one source of truth."""
    cols = BASIS_COLUMNS[basis]
    value_col, vol_col = cols[metric], cols["volume"]
    if value_col not in df.columns:
        return []
    sub = df[df[value_col].notna() & df[vol_col].notna()]
    if sub.empty:
        return []
    grouped = sub.groupby(group_by, as_index=False)[[value_col, vol_col]].sum()
    out = []
    for _, r in grouped.iterrows():
        volume = float(r[vol_col])
        if volume <= 0:
            continue
        out.append({group_by: str(r[group_by]), "price": float(r[value_col]) / volume,
                    "volume": volume})
    return out


def _resolve_period(bva: pd.DataFrame, period: str):
    available = sorted(set(bva["month"].astype(str)))
    closed = sorted(set(_closed(bva)["month"].astype(str)))
    text = str(period or "").strip().lower()
    if text in ("latest", "", "current"):
        return [closed[-1]] if closed else {"error": "No closed months in the data."}
    if len(text) == 7 and text[4] == "-":
        if text not in available:
            return {"error": f"Month '{period}' is not in the data. Available: "
                             f"{available[0]} to {available[-1]}."}
        return [text]
    if len(text) == 4 and text.isdigit():
        months = [m for m in available if m.startswith(text)]
        if not months:
            years = sorted({m[:4] for m in available})
            return _unknown("year", period, years)
        return months
    return {"error": f"Could not parse period '{period}'. Use 'YYYY-MM', 'YYYY' or 'latest'."}


def _shift_months(months: list[str], by: int) -> list[str]:
    out = []
    for m in months:
        y, mo = int(m[:4]), int(m[5:7])
        total = y * 12 + (mo - 1) + by
        out.append(f"{total // 12:04d}-{total % 12 + 1:02d}")
    return out


def _label(months: list[str]) -> str:
    if not months:
        return "(none)"
    if len(months) == 1:
        return months[0]
    return f"{min(months)}..{max(months)}"


# --------------------------------------------------------------------------
# 4. cost_buildup
# --------------------------------------------------------------------------

def cost_buildup(product_line: str, month: str | None = None,
                 compare_month: str | None = None) -> dict:
    bom, source = _load("bill_of_materials")
    lines = sorted(set(bom["product_line"]))
    if product_line not in lines:
        return _unknown("product_line", product_line, lines)

    bva, _ = _load("budget_vs_actuals")
    month = month or _latest_closed_month(bva)
    prices, quotes = _driver_eur_prices(month)
    if not prices:
        return {"error": "No driver prices available — run app/generate_data.py."}

    catalog = drivers.load_catalog().set_index("driver_id")
    rows = bom[bom["product_line"] == product_line]

    def buildup(at_month: str) -> tuple[list[dict], float]:
        eur, _q = _driver_eur_prices(at_month)
        items, total = [], 0.0
        for _, r in rows.iterrows():
            did, qty = str(r["driver_id"]), float(r["qty_per_tonne"])
            price = eur.get(did)
            if price is None:
                continue
            cost = qty * price
            total += cost
            meta = catalog.loc[did] if did in catalog.index else {}
            items.append({"driver_id": did,
                          "name": str(meta.get("name", did)) if len(meta) else did,
                          "category": str(meta.get("category", "")) if len(meta) else "",
                          "qty_per_tonne": qty, "unit": str(r.get("unit", "")),
                          "price_eur": round(price, 4),
                          "cost_eur_per_tonne": round(cost, 2)})
        return items, total

    items, materials_total = buildup(month)
    actual_unit_cost = _actual_unit_cost(bva, product_line, month)
    conversion = (actual_unit_cost - materials_total) if actual_unit_cost else None

    for it in items:
        it["share_pct"] = (round(it["cost_eur_per_tonne"] / actual_unit_cost * 100.0, 1)
                           if actual_unit_cost else None)
    items.sort(key=lambda i: i["cost_eur_per_tonne"], reverse=True)

    out = {
        "product_line": product_line, "month": month,
        "inputs": items,
        "materials_cost_eur_per_tonne": round(materials_total, 2),
        "conversion_and_overhead_eur_per_tonne": (round(conversion, 2)
                                                  if conversion is not None else None),
        "total_unit_cost_eur_per_tonne": (round(actual_unit_cost, 2)
                                          if actual_unit_cost else round(materials_total, 2)),
        "note": "Unit cost is derived as cogs/volume from budget_vs_actuals; the input costs "
                "come from the bill of materials priced at that month's driver observations, "
                "with USD-quoted inputs converted at that month's EUR/USD rate.",
        "source_file": source,
    }

    if compare_month:
        prev_items, prev_total = buildup(compare_month)
        prev_by_id = {i["driver_id"]: i for i in prev_items}
        bridge = []
        for it in items:
            prev = prev_by_id.get(it["driver_id"])
            if not prev:
                continue
            delta = it["cost_eur_per_tonne"] - prev["cost_eur_per_tonne"]
            if abs(delta) < 0.005:
                continue
            bridge.append({"driver_id": it["driver_id"], "name": it["name"],
                           "from_eur_per_tonne": prev["cost_eur_per_tonne"],
                           "to_eur_per_tonne": it["cost_eur_per_tonne"],
                           "delta_eur_per_tonne": round(delta, 2)})
        bridge.sort(key=lambda b: abs(b["delta_eur_per_tonne"]), reverse=True)
        prev_unit = _actual_unit_cost(bva, product_line, compare_month)
        out["bridge"] = {
            "from_month": compare_month, "to_month": month,
            "from_unit_cost_eur_per_tonne": round(prev_unit, 2) if prev_unit else round(prev_total, 2),
            "to_unit_cost_eur_per_tonne": round(actual_unit_cost, 2) if actual_unit_cost else round(materials_total, 2),
            "materials_delta_eur_per_tonne": round(materials_total - prev_total, 2),
            "by_input": bridge,
        }
    return out


def _actual_unit_cost(bva: pd.DataFrame, product_line: str, month: str) -> float | None:
    sub = bva[(bva["product_line"] == product_line) & (bva["month"] == month)]
    sub = sub[sub["cogs_actual_eur"].notna()]
    if sub.empty:
        return None
    volume = float(sub["volume_tonnes_actual"].sum())
    return float(sub["cogs_actual_eur"].sum()) / volume if volume > 0 else None


# --------------------------------------------------------------------------
# 5-6. driver_status / record_driver_observation
# --------------------------------------------------------------------------

def driver_status_tool(driver_id=None, category=None, only_stale=False,
                       only_drifted=False) -> dict:
    status = drivers.driver_status()
    if not status:
        return {"error": "No drivers configured yet. Complete setup, or run "
                         "app/generate_data.py to seed the demo watchlist."}
    if driver_id:
        ids = [d["driver_id"] for d in status]
        if driver_id not in ids:
            return _unknown("driver_id", driver_id, ids)
        status = [d for d in status if d["driver_id"] == driver_id]
    if category:
        cats = sorted({d["category"] for d in status if d["category"]})
        if category not in cats:
            return _unknown("category", category, cats)
        status = [d for d in status if d["category"] == category]
    if only_stale:
        status = [d for d in status if d["verify_status"] != "fresh"]
    if only_drifted:
        status = [d for d in status if d["drift_pct"] is not None and abs(d["drift_pct"]) > 10.0]

    for d in status:
        for field in ("drift_pct", "forward_vs_lock_pct", "forward_12m"):
            if d.get(field) is not None:
                d[field] = round(d[field], 2)
    return {"drivers": status, "count": len(status),
            "locked_at": drivers.load_assumptions().get("locked_at"),
            "note": "drift_pct compares the latest observation to the value locked into the "
                    "budget, both in the driver's own quote currency. 'adverse' already "
                    "accounts for direction — a falling EUR/USD rate is adverse. forward_12m "
                    "is the mean of the next 12 quoted months on the latest recorded forward "
                    "curve, and forward_vs_lock_pct is that against the locked value: drift "
                    "is what the market has already done, forward is what it says comes next. "
                    "Both are null where no curve has been recorded — query driver_forwards "
                    "for the month-by-month shape.",
            "source_file": "driver_prices.parquet"}


def record_driver_observation(driver_id: str, price, month: str, source_url: str,
                              note=None, override_sanity_check=False, *, ctx=None) -> dict:
    ctx = ctx or {}
    return drivers.append_observation(
        driver_id, price, month, source_url=source_url,
        fetched_urls=ctx.get("fetched_urls") or set(),
        source="agent_research", note=note,
        override_sanity_check=bool(override_sanity_check))


def record_driver_forward(driver_id: str, points, source_url: str, curve_date=None,
                          note=None, override_sanity_check=False, *, ctx=None) -> dict:
    ctx = ctx or {}
    return drivers.append_forward(
        driver_id, points, source_url=source_url, curve_date=curve_date,
        fetched_urls=ctx.get("fetched_urls") or set(),
        source="agent_research", note=note,
        override_sanity_check=bool(override_sanity_check))


# --------------------------------------------------------------------------
# 7. driver_sensitivity
# --------------------------------------------------------------------------

def driver_sensitivity(driver_ids=None, shock_pct=10.0, year=None,
                       floor_margin_pct=8.0, price_pass_through=0.35) -> dict:
    bom, source = _load("bill_of_materials")
    bva, _ = _load("budget_vs_actuals")
    catalog = drivers.load_catalog()
    if catalog.empty:
        return {"error": "No drivers configured yet."}

    known = [str(d) for d in catalog["driver_id"]]
    if driver_ids:
        bad = [d for d in driver_ids if d not in known]
        if bad:
            return _unknown("driver_id", bad[0], known)
    else:
        driver_ids = known

    year = str(year or _budget_year(bva))
    rows = bva[bva["month"].str.startswith(year)]
    if rows.empty:
        years = sorted({m[:4] for m in bva["month"]})
        return _unknown("year", year, years)

    basis = "budget" if rows["volume_tonnes_budget"].notna().any() else "actual"
    vol_col = BASIS_COLUMNS[basis]["volume"]
    volumes = rows.groupby("product_line")[vol_col].sum().to_dict()
    volumes = {str(k): float(v) for k, v in volumes.items() if pd.notna(v)}

    baseline = {
        "revenue_eur": float(rows[BASIS_COLUMNS[basis]["revenue"]].sum()),
        "cogs_eur": float(rows[BASIS_COLUMNS[basis]["cogs"]].sum()),
        "opex_eur": float(rows[BASIS_COLUMNS[basis]["opex"]].sum()),
    }
    prices, _q = _driver_eur_prices()
    hedges = dict(zip(catalog["driver_id"].astype(str),
                      catalog["hedge_coverage"].astype(float)))
    names = dict(zip(catalog["driver_id"].astype(str), catalog["name"].astype(str)))
    bom_rows = [{"product_line": str(r["product_line"]), "driver_id": str(r["driver_id"]),
                 "qty_per_tonne": float(r["qty_per_tonne"])} for _, r in bom.iterrows()]

    results = []
    for did in driver_ids:
        price = prices.get(did)
        if price is None:
            continue
        exposure = budget.driver_exposure(bom_rows, volumes, price, did)
        if exposure <= 0:
            continue
        hedge = float(hedges.get(did, 0.0))
        s = budget.sensitivity(exposure, float(shock_pct), hedge_coverage=hedge,
                               price_pass_through=float(price_pass_through),
                               baseline=baseline)
        be = budget.breakeven_shock(exposure, baseline, float(floor_margin_pct),
                                    hedge_coverage=hedge,
                                    price_pass_through=float(price_pass_through))
        results.append({
            "driver_id": did, "name": names.get(did, did),
            "exposure_eur": round(exposure, 2),
            "exposure_pct_of_cogs": (round(exposure / baseline["cogs_eur"] * 100.0, 1)
                                     if baseline["cogs_eur"] else None),
            "hedge_coverage": hedge,
            "delta_cogs_eur": round(s["delta_cogs_eur"], 2),
            "delta_ebitda_eur": round(s["delta_ebitda_eur"], 2),
            "margin_delta_pp": round(s.get("margin_delta_pp", 0.0), 3),
            "breakeven_shock_pct": None if be is None else round(be, 1),
        })
    results.sort(key=lambda r: abs(r["delta_ebitda_eur"]), reverse=True)

    return {
        "shock_pct": float(shock_pct), "year": year, "volume_basis": basis,
        "price_pass_through": float(price_pass_through),
        "floor_margin_pct": float(floor_margin_pct),
        "baseline": {k: round(v, 2) for k, v in baseline.items()},
        "baseline_ebitda_margin_pct": round(
            (baseline["revenue_eur"] - baseline["cogs_eur"] - baseline["opex_eur"])
            / baseline["revenue_eur"] * 100.0, 2) if baseline["revenue_eur"] else None,
        "drivers": results,
        "note": "Exposure is read from the bill of materials (qty per tonne x volume x price), "
                "not assumed. breakeven_shock_pct is the move that would put EBITDA margin "
                "exactly on the floor.",
        "source_file": source,
    }


def _budget_year(bva: pd.DataFrame) -> str:
    """The latest year that has budget rows but no actuals — the year being planned."""
    by_year = {}
    for m in sorted(set(bva["month"].astype(str))):
        by_year.setdefault(m[:4], []).append(m)
    for year in sorted(by_year, reverse=True):
        rows = bva[bva["month"].str.startswith(year)]
        if rows["revenue_actual_eur"].isna().all():
            return year
    return sorted(by_year)[-1]


def _scenario_inputs() -> dict:
    """Everything a scenario is built from, and every key it may legally name.

    One function, because the options enumerated in build_budget_scenario's
    teachable errors and the options the CFO's edit form renders have to come off
    the same read. Two derivations of "the valid product lines" is how a form
    offers a value the validator then rejects.

    Returns the same `{"error": …}` dicts as before for the two states that make
    a scenario impossible at all, so both callers report one reason.
    """
    catalog = drivers.load_catalog()
    if catalog.empty:
        return {"error": "No drivers configured yet."}
    bom, source = _load("bill_of_materials")
    bva, _ = _load("budget_vs_actuals")
    year = _budget_year(bva)
    plan = bva[bva["month"].str.startswith(year)]
    if plan.empty:
        return {"error": f"No budget rows for {year}."}
    opex_rows = _opex_plan_rows(year)
    return {
        "catalog": catalog, "known": [str(d) for d in catalog["driver_id"]],
        "bom": bom, "plan": plan, "year": year,
        "lines": sorted(set(plan["product_line"].astype(str))),
        "opex_rows": opex_rows,
        "centres": sorted({r["cost_centre"] for r in opex_rows}),
        "source": source,
    }


def scenario_options() -> dict:
    """The keys a scenario may name and the defaults it starts from — what the
    CFO's edit form fills its selects with.

    Not a model tool: the model already receives these lists inside the teachable
    errors `build_budget_scenario` returns, which is the door it can act on. This
    exists so the form and the validator cannot disagree about what a valid
    driver, product line, cost centre or basis is.
    """
    ctx = _scenario_inputs()
    if "error" in ctx:
        return ctx
    cat = ctx["catalog"]
    ids = cat["driver_id"].astype(str)
    names = dict(zip(ids, cat["name"].astype(str))) if "name" in cat.columns else {}
    units = dict(zip(ids, cat["unit"].astype(str))) if "unit" in cat.columns else {}
    return {
        "year": ctx["year"],
        "drivers": [{"driver_id": d, "name": names.get(d, d), "unit": units.get(d)}
                    for d in ctx["known"]],
        "product_lines": ctx["lines"],
        "all_lines": budget.ALL_LINES,
        "cost_centres": ctx["centres"],
        "bases": [{"basis": k, "description": v} for k, v in SCENARIO_BASES.items()],
        "blocks": list(budget.ASSUMPTION_BLOCKS),
        "defaults": {"basis": "locked", "price_pass_through": 0.35,
                     "opex_inflation_pct": 0.0},
        "source_file": ctx["source"],
    }


# --------------------------------------------------------------------------
# 8-9. build_budget_scenario / lock_assumptions
#
# Creation stays conversational — turning "freight holds all year" into an
# assumption set is what the model is for. CORRECTION does not: the CFO's
# `replace_scenario_id` recomputes a stored scenario in place through this same
# function, so there is one implementation of "what these assumptions are worth"
# and the figure on the card cannot disagree with the figure in an answer.
# --------------------------------------------------------------------------

def build_budget_scenario(name: str, assumptions=None, note=None,
                          price_pass_through=0.35, opex_inflation_pct=0.0,
                          make_active=False, volume=None, price=None,
                          opex=None, basis="locked", *,
                          replace_scenario_id: str | None = None) -> dict:
    """Apply an assumption set to the budget baseline, project the P&L once and
    persist the result. The only path from assumptions to numbers.

    `replace_scenario_id` recomputes an EXISTING scenario in place: same id, same
    `active` flag, the stored projection overwritten, `updated_at` bumped. It is
    keyword-only and deliberately absent from the model's tool schema — see
    `build_budget_scenario_tool`, which is the door the model gets.
    """
    basis = str(basis or "locked")
    if basis not in SCENARIO_BASES:
        return _unknown("basis", basis, list(SCENARIO_BASES))

    previous = None
    if replace_scenario_id:
        # scenarios.save_scenario UPSERTS on id, so a stale id would mint a ghost
        # record rather than fail. This check is the whole difference between
        # "recompute in place" and "create with a chosen id".
        previous = scenarios.get_scenario(replace_scenario_id)
        if previous is None:
            return {"error": f"No scenario with id '{replace_scenario_id}' to recompute — it may "
                             "have been deleted in another tab. Reload the scenarios list."}

    ctx = _scenario_inputs()
    if "error" in ctx:
        return ctx
    catalog, known, lines = ctx["catalog"], ctx["known"], ctx["lines"]
    bom, plan, year = ctx["bom"], ctx["plan"], ctx["year"]
    opex_rows, centres, source = ctx["opex_rows"], ctx["centres"], ctx["source"]

    # An edit must not silently re-base a scenario onto a different plan. Once
    # actuals start landing, _budget_year moves on; a recompute would then keep
    # the name and the id while measuring against a year nobody asked about.
    if previous is not None:
        was = str(previous.get("baseline") or "")
        if was and was != f"budget_{year}":
            return {"error": f"This scenario was built against {was}, but the budget year is now "
                             f"{year}. Recomputing it would silently change what it measures "
                             f"against. Build a new scenario for {year} instead."}

    spec = _assumption_blocks(assumptions, volume, price, opex)
    if "error" in spec:
        return spec
    # A CREATION guard, not an edit one: a scenario the model saved by accident is
    # worse than a corrected tool call, but a stored scenario already exists —
    # several of them legitimately carry no assumptions at all ("2027 budget (as
    # locked)"), and renaming one or moving its basis is a real edit.
    if not any(spec.values()) and previous is None:
        return {"error": "A scenario needs at least one assumption. Use 'assumptions' for driver "
                         f"price shocks ({', '.join(known)}), 'volume' or 'price' for product "
                         f"lines ({', '.join(lines)}), or 'opex' for cost centres."}

    # Teachable validation, one enumerated option list per block.
    for bad in [d for d in spec["drivers"] if d not in known]:
        return _unknown("driver_id", bad, known)
    for block in ("volume", "price"):
        for bad in [ln for ln in spec[block] if ln != budget.ALL_LINES and ln not in lines]:
            return _unknown(f"{block} product_line", bad, lines + [budget.ALL_LINES])
    for bad in [k for k in spec["opex"] if k not in centres and k not in known]:
        return _unknown("opex cost_centre or driver_id", bad, centres + known)

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

    # Assumptions are percentage moves versus the LOCKED value, so the baseline
    # price each percentage applies to is the locked one, converted to EUR.
    locked = drivers.load_assumptions().get("assumptions", {})
    eur_now, quote_now = _driver_eur_prices()
    fx_lock = (locked.get("eur_usd") or {}).get("value") or quote_now.get("eur_usd") or 1.0
    ccy = dict(zip(catalog["driver_id"].astype(str), catalog["quote_currency"].astype(str)))
    lock_prices = {}
    for did in known:
        entry = locked.get(did) or {}
        value = entry.get("value")
        if value is None:
            lock_prices[did] = eur_now.get(did, 0.0)
            continue
        lock_prices[did] = (float(value) / fx_lock
                            if ccy.get(did) == "USD" and did != "eur_usd" else float(value))

    hedges = dict(zip(catalog["driver_id"].astype(str),
                      catalog["hedge_coverage"].astype(float)))

    months = sorted({str(r["month"]) for r in baseline_rows})
    by_month_prices, basis_note = _basis_prices(basis, months, known, ccy,
                                                eur_now, fx_lock)
    if isinstance(by_month_prices, dict) and "error" in by_month_prices:
        return by_month_prices

    projection = budget.project_pnl(
        baseline_rows, bom_rows, lock_prices, spec, hedges=hedges,
        price_pass_through=float(price_pass_through),
        opex_inflation_pct=float(opex_inflation_pct), opex_rows=opex_rows,
        driver_prices_by_month=by_month_prices)

    fields = {
        "name": name, "note": note, "baseline": f"budget_{year}",
        "assumptions": spec, "price_pass_through": float(price_pass_through),
        "opex_inflation_pct": float(opex_inflation_pct), "basis": basis,
        "totals": projection["totals"], "by_month": projection["by_month"],
        "by_product_line": projection["by_product_line"],
        "driver_impact_eur": projection["driver_impact_eur"],
        "opex_bridge": projection["opex_bridge"],
        "ebitda_bridge": projection["ebitda_bridge"],
        "driver_prices_used": lock_prices,
    }
    if previous is not None:
        fields["id"] = replace_scenario_id
        # This tool has never written source_records, so a replace that omitted it
        # would silently strip provenance off any record that carries one.
        fields["source_records"] = previous.get("source_records") or []
    # `active` is ASSERTED here, never DENIED. save_scenario inherits the stored
    # flag when the key is absent, so recomputing the active scenario leaves the
    # budget resting on it; passing active=False would quietly take the budget off
    # a scenario the CFO was only editing. On a create, absent and False are
    # identical to save_scenario, so that path is byte-for-byte what it was.
    if make_active:
        fields["active"] = True
    stored = scenarios.save_scenario(fields)

    active = scenarios.get_active() or {}
    baseline_totals = budget.project_pnl(baseline_rows, bom_rows, lock_prices, {})["totals"]
    totals = projection["totals"]
    return {
        "scenario_id": stored["id"], "name": stored["name"], "year": year,
        "active": stored["active"], "assumptions": spec, "basis": basis,
        "basis_note": basis_note,
        "replaced": previous is not None,
        "created_at": stored["created_at"], "updated_at": stored["updated_at"],
        # A recompute re-reads today's locked assumptions, so a scenario's EBITDA
        # can move with not one percentage changed. Say which drivers did it,
        # rather than handing back a number nobody typed a reason for.
        "repriced_drivers": (budget.repriced_drivers(previous.get("driver_prices_used") or {},
                                                     lock_prices)
                             if previous is not None else []),
        "totals": {k: (round(v, 2) if isinstance(v, (int, float)) and v is not None else v)
                   for k, v in totals.items()},
        "vs_baseline": {
            "revenue_delta_eur": round(totals["revenue_eur"] - baseline_totals["revenue_eur"], 2),
            "cogs_delta_eur": round(totals["cogs_eur"] - baseline_totals["cogs_eur"], 2),
            "ebitda_delta_eur": round(totals["ebitda_eur"] - baseline_totals["ebitda_eur"], 2),
            "volume_delta_tonnes": round(projection["volume_delta_tonnes"], 2),
            "margin_delta_pp": round((totals["ebitda_margin_pct"] or 0.0)
                                     - (baseline_totals["ebitda_margin_pct"] or 0.0), 3),
            # Waterfall-shaped: hand it straight to render_chart as the single
            # series of a 'waterfall'. Every step sums to ebitda_delta_eur.
            "bridge": _ebitda_bridge_points(projection["ebitda_bridge"]),
        },
        "driver_impact_eur": {k: round(v, 2) for k, v in projection["driver_impact_eur"].items()},
        "opex_bridge": [{"cost_centre": r["cost_centre"], "driver_id": r["driver_id"],
                         "basis": r["basis"], "pct": round(r["pct"], 3),
                         "delta_eur": round(r["delta_eur"], 2)}
                        for r in projection["opex_bridge"]["by_cost_centre"]],
        "opex_growth_pct": round(projection["opex_bridge"]["weighted_pct"], 3),
        "by_month": [{"month": m["month"], "revenue_eur": round(m["revenue_eur"], 2),
                      "cogs_eur": round(m["cogs_eur"], 2),
                      "ebitda_eur": round(m["ebitda_eur"], 2)}
                     for m in projection["by_month"]],
        "currently_active_scenario": active.get("name"),
        "note": ("Scenario recomputed in place: same id, same active flag, the previous "
                 "projection overwritten. A budget version already frozen from it keeps its "
                 "own copy, so nothing that was approved changed. "
                 if previous is not None else "Scenario saved. ")
                + "Every delta is measured against the LOCKED budget, applied "
                "through the bill of materials and each driver's hedge coverage; volume and "
                "price are moves versus the budget. Pass-through was suppressed on any line "
                "with an explicit price, so recovery is never counted twice. " + basis_note,
        "source_file": source,
    }


def _basis_prices(basis: str, months: list[str], known: list[str], ccy: dict,
                  spot_eur: dict, fx_fallback: float):
    """({month: {driver_id: EUR price to apply}} | None, a sentence for the model).

    The baseline the deltas are measured from never changes — it is the locked
    price the budget was built on. What the basis changes is the price the
    CFO's percentages sit on top of, which is why 'spot' with no assumptions at
    all is a real answer ("what the market has already done to the budget")
    rather than a no-op.

    Returns an {"error": …} dict in the first slot when the chosen basis has no
    data behind it, enumerating the bases that do — the same teachable shape as
    every other validation here.
    """
    if basis == "locked":
        return None, "Drivers are priced at their locked values."

    if basis == "spot":
        priced = {d: p for d, p in spot_eur.items() if d in known}
        if not priced:
            return ({"error": "No driver observations exist, so basis 'spot' has nothing to "
                              "price against. Use basis 'locked', or research the prices "
                              "first with record_driver_observation."}, "")
        return ({m: dict(priced) for m in months},
                f"Drivers were re-priced at the latest observed market level "
                f"({len(priced)} drivers) before your percentages were applied, so the "
                f"move versus the locked budget includes what the market has already done.")

    wanted = set(months)
    quote_by_month: dict[str, dict[str, float]] = {}
    if wanted:
        for did in known:
            for point in drivers.forward_curve(did, months=len(wanted) + 24,
                                               start_month=min(wanted)):
                if point["quote_month"] in wanted:
                    quote_by_month.setdefault(point["quote_month"], {})[did] = point["price"]

    if not quote_by_month:
        return ({"error": "No forward curves cover the budget year, so basis 'forward' has "
                          "nothing to build on. Research the curve, record it with "
                          "record_driver_forward citing the page you fetched, then rebuild — "
                          "or use basis 'locked' or 'spot'."}, "")

    # A USD-quoted forward is converted at the FX FORWARD for that month where
    # one has been recorded, so an FX curve and a commodity curve compose. The
    # locked rate is the fallback, matching how lock_prices are converted.
    eur_by_month: dict[str, dict[str, float]] = {}
    for month, prices in quote_by_month.items():
        fx = float(prices.get("eur_usd") or fx_fallback or 1.0)
        eur_by_month[month] = {
            d: (p / fx if ccy.get(d) == "USD" and d != "eur_usd" else p)
            for d, p in prices.items()}

    covered = sorted({d for prices in quote_by_month.values() for d in prices})
    return (eur_by_month,
            f"Drivers were priced off the recorded forward curve, month by month, for "
            f"{len(covered)} driver(s) across {len(eur_by_month)} of {len(months)} budget "
            f"months ({', '.join(covered)}); anything the curve does not cover stayed at its "
            f"locked value. Monthly cost therefore carries the curve's own shape.")


def _assumption_blocks(assumptions, volume, price, opex) -> dict:
    """Merge the accepted calling styles into the four blocks, or an {"error"}.

    `assumptions` may be flat (`{driver_id: pct}`) or already blocked; `volume`,
    `price` and `opex` merge on top. Both styles are accepted because the flat
    one is what every stored scenario and every prior prompt example uses.

    Unlike `budget.normalise_assumptions`, which drops junk because it runs on
    read, this raises a teachable error — the model is here to be corrected.
    """
    raw: dict[str, dict] = {b: {} for b in budget.ASSUMPTION_BLOCKS}
    if assumptions is not None:
        if not isinstance(assumptions, dict):
            return {"error": "assumptions must be an object of {driver_id: pct_change}."}
        if any(k in budget.ASSUMPTION_BLOCKS and isinstance(v, dict)
               for k, v in assumptions.items()):
            for block in budget.ASSUMPTION_BLOCKS:
                nested = assumptions.get(block)
                if nested is None:
                    continue
                if not isinstance(nested, dict):
                    return {"error": f"assumptions['{block}'] must be an object of {{key: pct}}."}
                raw[block].update(nested)
        else:
            raw["drivers"].update(assumptions)

    for block, extra in (("volume", volume), ("price", price), ("opex", opex)):
        if extra is None:
            continue
        if not isinstance(extra, dict):
            key = "cost_centre or driver_id" if block == "opex" else "product_line"
            return {"error": f"'{block}' must be an object of {{{key}: pct_change}}."}
        raw[block].update(extra)

    spec: dict[str, dict] = {}
    for block, entries in raw.items():
        clean = {}
        for key, value in entries.items():
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return {"error": f"The '{block}' assumption for '{key}' must be a number — a "
                                 f"percentage change — got {value!r}."}
            try:
                clean[str(key)] = float(value)
            except (TypeError, ValueError):
                return {"error": f"The '{block}' assumption for '{key}' must be a number — a "
                                 f"percentage change — got {value!r}."}
        spec[block] = clean
    return spec


def _opex_plan_rows(year: str) -> list[dict]:
    """Budget-basis opex_plan rows for one year, shaped for `budget.opex_bridge`.

    Returns [] rather than raising when the dataset is missing — a company with
    no opex plan still gets a scenario, just on the blanket inflation rate.
    """
    try:
        plan, _ = _load("opex_plan")
    except (FileNotFoundError, OSError, KeyError):
        return []
    rows = plan[plan["month"].astype(str).str.startswith(str(year))]
    if rows.empty:
        return []
    amount_col = ("amount_budget_eur" if "amount_budget_eur" in rows.columns else "amount_eur")
    out = []
    for _, r in rows.iterrows():
        did = r.get("driver_id")
        out.append({"cost_centre": str(r["cost_centre"]),
                    "driver_id": None if pd.isna(did) else str(did),
                    "amount_eur": float(r[amount_col] or 0.0)})
    return out


# At most this many steps in the returned waterfall, matching render_chart's
# WATERFALL_MAX_POINTS — the smallest drivers fold into one "Other drivers" step
# so the bridge stays additive AND directly renderable.
BRIDGE_MAX_POINTS = WATERFALL_MAX_POINTS


def _ebitda_bridge_points(bridge: dict) -> list[dict]:
    def kept(pairs):
        return [(label, value) for label, value in pairs if abs(value) > 0.005]

    head = kept([("Volume", bridge["volume_eur"]), ("Price", bridge["price_eur"])])
    tail = kept([("Opex", bridge["opex_eur"])])
    by_driver = kept(sorted(bridge["drivers_eur"].items(), key=lambda kv: -abs(kv[1])))

    # Two absolutes bookend the deltas; only drivers fold, so Volume, Price and
    # Opex always keep their own step and never hide inside "Other drivers".
    room = BRIDGE_MAX_POINTS - 2 - len(head) - len(tail)
    if len(by_driver) > room:
        by_driver = by_driver[:room - 1] + [
            ("Other drivers", sum(v for _, v in by_driver[room - 1:]))]
    steps = head + by_driver + tail

    points = [{"label": "Baseline EBITDA", "value": round(bridge["baseline_ebitda_eur"], 2),
               "kind": "absolute"}]
    points += [{"label": label, "value": round(value, 2), "kind": "delta"}
               for label, value in steps]
    points.append({"label": "Scenario EBITDA", "value": round(bridge["projected_ebitda_eur"], 2),
                   "kind": "absolute"})
    return points


# --------------------------------------------------------------------------
# 10. cash_flow_projection — the scenario's EBITDA turned into cash
# --------------------------------------------------------------------------

# How many closed months the days are measured over by default. A year cancels
# seasonality in the balances; a single month is mostly noise about when the
# invoices went out.
CASH_MEASURE_MONTHS = 12


def cash_flow_projection(scenario_id=None, dso_days=None, dio_days=None,
                         dpo_days=None, opening_cash_eur=None, tax_rate_pct=None,
                         min_cash_eur=None, include_proposed_capex=True,
                         measure_months=CASH_MEASURE_MONTHS) -> dict:
    """A stored scenario's monthly EBITDA, turned into a monthly cash profile.

    Everything this needs is either read off a dataset or stated by the CFO, and
    the result says which for every input:

      * **Days** — measured from `working_capital` over the last `measure_months`
        CLOSED months. An argument here or a figure on the company profile
        overrides the measurement, and `days.stated` names which ones were
        overridden. Precedence is argument → profile → measurement, so the model
        can answer "what if we collected in 30 days" without writing anything.
      * **Opening cash and balances** — the latest closed month of
        `working_capital`. Without that dataset the balances are implied by the
        first projected month and the projection says so.
      * **Capex** — `capex_plan`, budget-basis, for the scenario's own year.
        `include_proposed_capex=False` defers the projects still marked proposed,
        which is the one genuine cash lever in here: committed capex is not a
        lever and this tool does not pretend it is.

    The arithmetic is all in `cash.py`. This function is the wiring, and the
    errors it returns enumerate the fix, like every other tool here.
    """
    from . import cash, profile as profile_mod

    scenario = (scenarios.get_scenario(scenario_id) if scenario_id
                else scenarios.get_active())
    if not scenario:
        names = [f"{s.get('name')} ({s.get('id')})" for s in scenarios.list_scenarios()]
        return {"error": ("No scenario found to project cash from. "
                          + (f"Existing scenarios: {'; '.join(names)}."
                             if names else "Build one with build_budget_scenario first."))}

    by_month = [r for r in (scenario.get("by_month") or []) if r.get("month")]
    if not by_month:
        return {"error": f"Scenario '{scenario.get('name')}' carries no monthly P&L, so there "
                         f"is nothing to convert into cash. Re-run build_budget_scenario to "
                         f"regenerate it."}
    months = sorted({str(r["month"]) for r in by_month})
    year = months[0][:4]

    # ---- Measure the days off the closed months, if the dataset is there ----
    history, wc_source = _working_capital_rows(before=months[0])
    measured = cash.working_capital_days(history, months=measure_months) if history else {}
    if "error" in measured:
        measured = {}

    stated = profile_mod.working_capital()
    overrides = {"dso_days": dso_days, "dio_days": dio_days, "dpo_days": dpo_days,
                 "opening_cash_eur": opening_cash_eur, "tax_rate_pct": tax_rate_pct,
                 "min_cash_eur": min_cash_eur}

    def resolve(field):
        """argument → profile → measurement → None, with the second element
        saying whether the winner was STATED rather than measured.

        A candidate that will not parse falls through to the next rather than
        failing the field: the alternative is a "neither measured nor stated"
        error on a company whose balance sheet was measured perfectly well.
        """
        for candidate in (overrides.get(field), stated.get(field)):
            if candidate is None:
                continue
            try:
                return float(candidate), True
            except (TypeError, ValueError):
                continue
        value = measured.get(field)
        try:
            return (None if value is None else float(value)), False
        except (TypeError, ValueError):
            return None, False

    days: dict[str, float] = {}
    was_stated: list[str] = []
    for field in ("dso_days", "dio_days", "dpo_days"):
        value, is_stated = resolve(field)
        if value is None:
            return {"error": f"{field} is neither measured nor stated, so the cash profile "
                             f"cannot be built. Either populate the working_capital dataset "
                             f"(monthly receivables, inventory and payables balances, which is "
                             f"what dso_days, dio_days and dpo_days are measured from), or pass "
                             f"dso_days, dio_days and dpo_days explicitly, or set them on the "
                             f"Budget page."}
        days[field] = value
        if is_stated:
            was_stated.append(field)

    opening_cash, cash_stated = resolve("opening_cash_eur")
    if opening_cash is None:
        opening_cash = measured.get("closing_cash_eur") or 0.0
    tax_rate, _ = resolve("tax_rate_pct")
    floor, _ = resolve("min_cash_eur")

    opening_balances = measured.get("closing_balances") or None

    # ---- Capex for the scenario's own year ----
    capex_rows, capex_source = _capex_rows(year)
    capex_by_month: dict[str, float] = {}
    deferred: list[dict] = []
    for row in capex_rows:
        if not include_proposed_capex and row["status"] == "proposed":
            deferred.append(row)
            continue
        capex_by_month[row["month"]] = capex_by_month.get(row["month"], 0.0) + row["amount_eur"]

    result = cash.project_cashflow(
        by_month, dso_days=days["dso_days"], dio_days=days["dio_days"],
        dpo_days=days["dpo_days"], opening_cash_eur=opening_cash,
        opening_balances=opening_balances, capex_by_month=capex_by_month,
        tax_rate_pct=tax_rate, min_cash_eur=floor)
    if "error" in result:
        return result

    basis = ("stated" if len(was_stated) == 3
             else "measured" if not was_stated else "measured + stated")
    caveats = list(result["caveats"])
    if not capex_rows:
        caveats.append(f"No capex is planned for {year} in capex_plan, so this is an "
                       f"operating-cash view only.")
    if deferred:
        caveats.append(f"{len(deferred)} proposed capex project(s) worth "
                       f"€{sum(r['amount_eur'] for r in deferred):,.0f} were EXCLUDED at your "
                       f"request; they are still in the plan.")

    return {
        "scenario_id": scenario.get("id"), "scenario": scenario.get("name"),
        "basis": scenario.get("basis") or "locked", "year": year,
        "currency": "EUR",
        "days": {**{k: round(v, 1) for k, v in result["days"].items()},
                 "basis": basis, "stated": was_stated,
                 "measured_from": (f"{measured.get('from_month')}..{measured.get('to_month')}"
                                   if measured else None)},
        "opening_balances": {k: round(v, 2) for k, v in result["opening_balances"].items()},
        "opening_balances_implied": result["opening_balances_implied"],
        "opening_cash_source": ("stated" if cash_stated
                                else "working_capital" if measured else "assumed zero"),
        "totals": {k: (round(v, 2) if isinstance(v, (int, float)) and v is not None else v)
                   for k, v in result["totals"].items()},
        "months": [{"month": r["month"], "ebitda_eur": round(r["ebitda_eur"], 2),
                    "working_capital_change_eur": round(r["working_capital_change_eur"], 2),
                    "tax_eur": round(r["tax_eur"], 2), "capex_eur": round(r["capex_eur"], 2),
                    "free_cash_flow_eur": round(r["free_cash_flow_eur"], 2),
                    "closing_cash_eur": round(r["closing_cash_eur"], 2)}
                   for r in result["months"]],
        "trough": result["trough"],
        "min_cash_eur": result["min_cash_eur"],
        "breaches_minimum": result["breaches_minimum"],
        "months_below_minimum": result["months_below_minimum"],
        "tax_rate_pct": result["tax_rate_pct"],
        "capex": {
            "total_eur": round(sum(capex_by_month.values()), 2),
            "committed_eur": round(sum(r["amount_eur"] for r in capex_rows
                                       if r["status"] == "committed"), 2),
            "proposed_eur": round(sum(r["amount_eur"] for r in capex_rows
                                      if r["status"] == "proposed"), 2),
            "proposed_included": bool(include_proposed_capex),
            "projects": [{"month": r["month"], "project": r["project"],
                          "category": r["category"], "status": r["status"],
                          "amount_eur": round(r["amount_eur"], 2)} for r in capex_rows],
        },
        # Waterfall-shaped: hand it straight to render_chart as the single series
        # of a 'waterfall'. Every step sums to free cash flow.
        "bridge": cash.cash_bridge(result),
        "caveats": caveats,
        "datasets_used": [d for d in (wc_source, capex_source, "scenarios.json") if d],
        "note": "Free cash flow before financing: EBITDA less the working-capital swing, capex "
                "and cash tax. The days are stated in `days` with `basis` saying whether each "
                "was measured off the balance sheet or decided by the CFO — say which when you "
                "quote them. Read `caveats` before presenting any of this as a cash balance.",
        # One citation, and it is the load-bearing one: the file the days came
        # from. The others are listed in `datasets_used` for the narrative.
        "source_file": wc_source or "scenarios.json",
    }


def _working_capital_rows(before: str | None = None) -> tuple[list[dict], str | None]:
    """Closed-month working-capital rows, shaped for `cash.working_capital_days`.

    Returns ([], None) rather than raising when the dataset is absent — a company
    with no balance-sheet history can still plan cash by stating its terms, so a
    missing dataset narrows the answer instead of removing it.
    """
    try:
        frame, source = _load("working_capital")
    except (FileNotFoundError, OSError, KeyError):
        return [], None
    if frame.empty:
        return [], source
    rows = frame.copy()
    rows["month"] = rows["month"].astype(str)
    if before:
        rows = rows[rows["month"] < str(before)]
    out = []
    for _, r in rows.sort_values("month").iterrows():
        entry = {"month": str(r["month"])}
        for column in ("revenue_eur", "cogs_eur", "receivables_eur", "inventory_eur",
                       "payables_eur", "cash_eur"):
            value = r.get(column)
            entry[column] = None if value is None or pd.isna(value) else float(value)
        out.append(entry)
    return out, source


CAPEX_STATUSES = ("committed", "proposed")


def _capex_rows(year: str) -> tuple[list[dict], str | None]:
    """Budget-basis capex rows for one year. Prefers `amount_budget_eur` — the
    plan — over `amount_eur`, which is what was actually spent."""
    try:
        frame, source = _load("capex_plan")
    except (FileNotFoundError, OSError, KeyError):
        return [], None
    if frame.empty:
        return [], source
    rows = frame[frame["month"].astype(str).str.startswith(str(year))]
    if rows.empty:
        return [], source
    amount_col = ("amount_budget_eur" if "amount_budget_eur" in rows.columns
                  else "amount_eur")
    out = []
    for _, r in rows.sort_values("month").iterrows():
        status = str(r.get("status") or "committed").strip().lower()
        out.append({
            "month": str(r["month"]),
            "project": str(r.get("project") or "Capex"),
            "category": str(r.get("category") or "other"),
            "status": status if status in CAPEX_STATUSES else "committed",
            "amount_eur": float(r[amount_col] or 0.0),
        })
    return out, source


def budget_outlook() -> dict:
    """The Budget page's own numbers, for the agent.

    Imported lazily: budgetplan reads no dataset of ours and importing it at
    module scope would put an `agent` import (and therefore the SDK) behind
    every tools.py import, including the ones the pure tests make.
    """
    from . import budgetplan

    plan = budgetplan.get_plan()
    if not plan.get("configured"):
        return {"error": "The budget has not been configured yet. Open the Budget page and "
                         "either build it from the datasets or enter the cost lines, then ask "
                         "again. Until then use build_budget_scenario for forward P&L."}
    derived = budgetplan.derive(plan)
    t = derived["totals"]
    return {
        "company": (plan.get("company") or {}).get("name"),
        "current_year": t["current_year"], "budget_year": t["budget_year"],
        "currency": t["currency"],
        "mode": "bill_of_materials" if any(r.get("driver_id") for r in derived["ranked"])
                else "manual",
        "totals": {k: (round(v, 2) if isinstance(v, (int, float)) else v)
                   for k, v in t.items()},
        "variables": [{
            "rank": r["rank"], "id": r["id"], "label": r["label"], "category": r["category"],
            "driver_id": r.get("driver_id"),
            "current_amount": round(r["current_amount"], 2),
            "next_amount": round(r["next_amount"], 2),
            "delta": round(r["delta"], 2),
            # None, never 0, when the baseline carries no such line at all —
            # "this is new" and "this is flat" are different answers.
            "expected_change_pct": (None if r["expected_change_pct"] is None
                                    else round(r["expected_change_pct"], 3)),
            "share_of_cost_pct": round(r["share_of_cost_pct"], 2),
            "share_of_movement_pct": round(r["impact_share"] * 100.0, 2),
            "assumption": r["assumption"] or r["default_note"],
        } for r in derived["ranked"]],
        "note": "Ranked by materiality — share of the cost base times expected change, so a "
                "large line moving a little outranks a small one moving a lot. A line with a "
                "driver_id is priced off the watchlist and its locked value and source are in "
                "driver_status; a line without one was entered by hand. These are the CFO's "
                "own planning figures, not a forecast. This is the CONFIGURED budget — the "
                "closed year against the plan year. The Budget page can also compare any two "
                "budgets or scenarios side by side, and when it does the question names both, "
                "so read the question's own framing rather than assuming this pair.",
        "source_file": "budget_plan.json",
    }


def lock_assumptions_tool(assumptions: dict, scenario_id=None, note=None) -> dict:
    result = drivers.lock_assumptions(assumptions, scenario_id=scenario_id, note=note)
    return result


def build_budget_scenario_tool(name: str, assumptions=None, note=None,
                               price_pass_through=0.35, opex_inflation_pct=0.0,
                               make_active=False, volume=None, price=None,
                               opex=None, basis="locked") -> dict:
    """The model's door: exactly the schema's arguments and no more.

    `replace_scenario_id` is deliberately not here, and leaving it out of the
    input_schema would not have been enough — the dispatcher splats model input
    with `**i` and no schema sets additionalProperties: false, so a hallucinated
    argument would be honoured. This wrapper makes it unrepresentable instead:
    the model gets execute_tool's teachable "Invalid arguments" rather than
    silently overwriting a stored projection with no undo. Same reasoning as
    record_driver_observation's `verify_provenance`, which only main.py passes.
    """
    return build_budget_scenario(
        name, assumptions=assumptions, note=note,
        price_pass_through=price_pass_through, opex_inflation_pct=opex_inflation_pct,
        make_active=make_active, volume=volume, price=price, opex=opex, basis=basis)


def propose_watchlist(ctx: dict | None = None, **proposal) -> dict:
    """Setup only. The tool's input_schema IS the proposal schema — structured
    outputs are incompatible with citations (a 400), and the proposal must carry
    web citations, so it arrives as a tool call instead."""
    from . import profile as profile_mod
    ctx = ctx or {}
    return profile_mod.save_proposal(proposal, model=ctx.get("model"),
                                     source_records=ctx.get("source_records"))


# --------------------------------------------------------------------------
# 11. render_chart (presentation only — returns NO source_file)
# --------------------------------------------------------------------------

def render_chart(chart_type: str, title: str, series: list, unit: str | None = None) -> dict:
    if chart_type not in ("line", "bar", "waterfall"):
        return _unknown("chart_type", chart_type, ["line", "bar", "waterfall"])
    title = " ".join(str(title or "").split())
    if not title:
        return {"error": "A chart needs a title."}
    if not isinstance(series, list) or not series:
        return {"error": "series must be a non-empty list of {name, points}."}

    is_waterfall = chart_type == "waterfall"
    if is_waterfall and len(series) != 1:
        return {"error": "A waterfall takes exactly one series — the bridge from one total to "
                         "another. Put each step in that series' points."}
    if len(series) > CHART_MAX_SERIES:
        return {"error": f"At most {CHART_MAX_SERIES} series per chart."}
    max_points = WATERFALL_MAX_POINTS if is_waterfall else CHART_MAX_POINTS

    clean_series = []
    for s in series:
        if not isinstance(s, dict):
            return {"error": "Each series must be an object with name and points."}
        points = s.get("points")
        if not isinstance(points, list) or not points:
            return {"error": "Each series needs a non-empty points list."}
        if len(points) > max_points:
            return {"error": f"At most {max_points} points per "
                             f"{'waterfall' if is_waterfall else 'series'} — every bar must stay "
                             f"labelled to be readable."}
        clean_points = []
        for p in points:
            if not isinstance(p, dict) or "label" not in p or "value" not in p:
                return {"error": "Each point must be an object with label and value."}
            try:
                value = float(p["value"])
            except (TypeError, ValueError):
                return {"error": f"Non-numeric value for label '{p.get('label')}'."}
            if not math.isfinite(value):
                return {"error": f"Value for label '{p.get('label')}' must be finite."}
            point = {"label": str(p["label"]), "value": value}
            if is_waterfall:
                kind = str(p.get("kind") or "delta")
                if kind not in ("absolute", "delta"):
                    return _unknown("point kind", kind, ["absolute", "delta"])
                point["kind"] = kind
            clean_points.append(point)
        clean_series.append({"name": " ".join(str(s.get("name") or "").split()) or title,
                             "points": clean_points})

    if is_waterfall and not any(p["kind"] == "absolute" for p in clean_series[0]["points"]):
        return {"error": "A waterfall needs at least one point with kind 'absolute' — usually "
                         "the opening total, and normally the closing total too."}

    spec = {"chart_type": chart_type, "title": title,
            "unit": (str(unit).strip() or None) if unit else None, "series": clean_series}
    # No source_file: presentation only, so charts never pollute the source list.
    return {"rendered": True, "title": title,
            "points": sum(len(s["points"]) for s in clean_series),
            "_chart_spec": spec}


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

_TOOLS = {
    "list_datasets": lambda i, ctx: list_datasets(),
    "read_document": lambda i, ctx: read_document(**i),
    "query_budget_data": lambda i, ctx: query_budget_data(**i),
    "variance_analysis": lambda i, ctx: variance_analysis(**i),
    "cost_buildup": lambda i, ctx: cost_buildup(**i),
    "driver_status": lambda i, ctx: driver_status_tool(**i),
    "record_driver_observation": lambda i, ctx: record_driver_observation(**i, ctx=ctx),
    "record_driver_forward": lambda i, ctx: record_driver_forward(**i, ctx=ctx),
    "driver_sensitivity": lambda i, ctx: driver_sensitivity(**i),
    "build_budget_scenario": lambda i, ctx: build_budget_scenario_tool(**i),
    "cash_flow_projection": lambda i, ctx: cash_flow_projection(**i),
    "budget_outlook": lambda i, ctx: budget_outlook(),
    "lock_assumptions": lambda i, ctx: lock_assumptions_tool(**i),
    "render_chart": lambda i, ctx: render_chart(**i),
    "propose_watchlist": lambda i, ctx: propose_watchlist(ctx=ctx, **i),
}


def execute_tool(name: str, tool_input: dict, ctx: dict | None = None) -> dict:
    """Dispatch a local tool call. `ctx` carries {fetched_urls, session_id, profile};
    only record_driver_observation needs it, but it is threaded through uniformly."""
    fn = _TOOLS.get(name)
    if fn is None:
        return {"error": f"Unknown tool '{name}'. Available: {', '.join(_TOOLS)}."}
    try:
        return fn(tool_input or {}, ctx or {})
    except TypeError as exc:      # unexpected kwargs from the model → teachable, not a crash
        return {"error": f"Invalid arguments for {name}: {exc}"}
    except FileNotFoundError:
        return {"error": f"{name} needs a dataset that is missing — run app/generate_data.py."}
    except Exception as exc:      # never let a tool bug kill the stream
        return {"error": f"{name} failed: {type(exc).__name__}: {exc}"}
