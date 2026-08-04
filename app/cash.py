# cash.py — Cash and working capital. Pure functions, no I/O, no app imports.
#
# The fourth pure leaf, alongside budget.py, citations.py and export.py, and for
# the same reason: this is arithmetic the model must never do itself, so it lives
# where it can be tested without disk, network or the SDK.
#
# Why cash is a separate module rather than a fifth section of budget.py: it
# speaks a different vocabulary. budget.py turns assumptions into a P&L; this
# turns a P&L into cash, and the inputs it needs — days, balances, capex,
# opening cash — are not assumptions about next year's trading at all. Two
# things live here:
#
#   1. working_capital_days — DSO/DIO/DPO MEASURED from the company's own
#      balances, not typed in. This is the same discipline as driver_exposure
#      reading qty-per-tonne off the bill of materials rather than guessing an
#      elasticity: a working-capital day the CFO invented is an assumption with
#      no source, and this app's whole position is that those cannot be
#      defended. The CFO can still override a measured day deliberately — that
#      is a decision ("we are going to collect faster"), and it is recorded as
#      one.
#   2. project_cashflow — a scenario's monthly EBITDA turned into a monthly cash
#      profile: the working-capital swing the trading pattern implies, capex,
#      optional cash tax, and the cash balance month by month.
#
# **What this deliberately does NOT model, because the data is not there.**
# There is no depreciation, interest, debt-schedule, dividend or deferred-tax
# dataset in this app, so there is no net income here and no attempt at one.
# What comes out is EBITDA less the working-capital swing, less capex, less an
# optional cash-tax rate the CFO states — a *free cash flow before financing*.
# Every projection carries that limitation in `caveats` and the callers print
# it. The same rule as the missing payroll dataset: say what is not there rather
# than filling the gap with something that looks like an answer.

from __future__ import annotations

import calendar
from collections import OrderedDict

# Below this, a denominator is treated as zero rather than divided by. Same
# constant and same reason as budget.EPS.
EPS = 1e-9

# A month whose label will not parse is billed 30 days rather than crashing the
# page. The days only scale a balance, so a wrong one is visibly wrong in the
# row it appears in; a traceback takes the whole cash profile down.
FALLBACK_DAYS = 30

# The three working-capital components, in reading order: the balance, the days
# metric that sizes it, the flow it is measured against, and `sign` — the effect
# on the working-capital swing of that balance RISING. More receivables and more
# inventory consume cash; more payables release it. Every sign convention in
# this module reads off this table, so there is one place it lives.
WC_COMPONENTS = (
    ("receivables", "dso_days", "revenue", +1,
     "DSO — days of revenue held as receivables"),
    ("inventory", "dio_days", "cogs", +1,
     "DIO — days of COGS held as inventory"),
    ("payables", "dpo_days", "cogs", -1,
     "DPO — days of COGS owed to suppliers"),
)


def days_in_month(month) -> int:
    """Calendar days in a "YYYY-MM" label. Real days, not a 30-day convention —
    a February that bills 28 days is the difference between a receivables
    balance that reconciles to the ledger and one that is 7% out."""
    try:
        year, mon = (int(part) for part in str(month).split("-")[:2])
        return calendar.monthrange(year, mon)[1]
    except (TypeError, ValueError, calendar.IllegalMonthError):
        return FALLBACK_DAYS


# --------------------------------------------------------------------------
# 1. Measuring the days
# --------------------------------------------------------------------------

def working_capital_days(rows: list[dict], *, months: int | None = None) -> dict:
    """DSO, DIO and DPO measured off the company's own monthly balances.

    `rows` — [{month, revenue_eur, cogs_eur, receivables_eur, inventory_eur,
               payables_eur, cash_eur?}, …]; only closed months belong here.
    `months` — measure over the most recent N months only (default: all of
               them). A trailing window is what a CFO quotes, because a single
               month's balance divided by a single month's revenue is mostly
               noise about when the invoices went out.

    Each metric is an AVERAGE balance over the window divided by the average
    daily flow across the same window:

        DSO = mean(receivables) · Σdays / Σrevenue
        DIO = mean(inventory)   · Σdays / Σcogs
        DPO = mean(payables)    · Σdays / Σcogs

    Averaging the balance and summing the flow is deliberate: a balance is a
    stock at a point in time and a flow accumulates, so mixing a period sum of
    one with a period sum of the other would count the balance twelve times.

    **DPO is measured against COGS, not purchases**, because there is no
    purchases dataset. On a business that holds roughly stable inventory the two
    are close, and the approximation is named in `caveats` rather than buried —
    it is the one figure here a controller would query first.

    Returns an {"error": …} dict, never raises, when there is nothing to measure
    from: no rows, or no revenue/COGS in the window.
    """
    clean = _months_sorted(rows)
    if not clean:
        return {"error": "No working-capital history supplied, so DSO, DIO and DPO cannot be "
                         "measured. Populate working_capital (monthly receivables, inventory "
                         "and payables balances), or state the days explicitly."}
    if months is not None:
        try:
            window = max(1, int(months))
        except (TypeError, ValueError):
            window = len(clean)
        clean = clean[-window:]

    revenue = sum(_f(r.get("revenue_eur")) for r in clean)
    cogs = sum(_f(r.get("cogs_eur")) for r in clean)
    total_days = sum(days_in_month(r.get("month")) for r in clean)

    if abs(revenue) < EPS or abs(cogs) < EPS:
        return {"error": f"The working-capital history for "
                         f"{clean[0].get('month')}..{clean[-1].get('month')} carries no "
                         f"revenue or no COGS, so the days cannot be measured. Pick a window "
                         f"with trading in it, or state the days explicitly."}

    flows: dict[str, float] = {"revenue": revenue, "cogs": cogs}
    out: dict = {
        "months_used": len(clean),
        "from_month": clean[0].get("month"),
        "to_month": clean[-1].get("month"),
        "days_in_window": total_days,
        "revenue_eur": revenue,
        "cogs_eur": cogs,
        "basis": "measured",
        "caveats": ["DPO is measured against COGS because there is no purchases dataset; on a "
                    "business holding roughly stable inventory the two are close, but a "
                    "controller reconciling to the ledger should expect a small difference."],
    }
    for name, days_key, flow_key, _sign, _label in WC_COMPONENTS:
        balances = [_f(r.get(f"{name}_eur")) for r in clean]
        average = sum(balances) / len(balances)
        out[f"{name}_avg_eur"] = average
        out[f"{name}_closing_eur"] = balances[-1]
        out[days_key] = average * total_days / flows[flow_key]

    out["cash_conversion_cycle_days"] = (out["dso_days"] + out["dio_days"]
                                         - out["dpo_days"])
    last = clean[-1]
    out["closing_month"] = last.get("month")
    out["closing_cash_eur"] = _f(last.get("cash_eur")) if last.get("cash_eur") is not None else None
    out["closing_balances"] = {name: out[f"{name}_closing_eur"]
                               for name, _dk, _fk, _s, _l in WC_COMPONENTS}
    return out


# --------------------------------------------------------------------------
# 2. Projecting the cash
# --------------------------------------------------------------------------

def project_cashflow(rows: list[dict], *, dso_days: float, dio_days: float,
                     dpo_days: float, opening_cash_eur: float = 0.0,
                     opening_balances: dict | None = None,
                     capex_by_month: dict | None = None,
                     tax_rate_pct: float | None = None,
                     min_cash_eur: float | None = None) -> dict:
    """A scenario's monthly P&L turned into a monthly cash profile.

    `rows` — the scenario's `by_month`: [{month, revenue_eur, cogs_eur,
             opex_eur, ebitda_eur?}, …]. EBITDA is taken from the row when it is
             there and derived as revenue − cogs − opex when it is not, so this
             accepts `budget.project_pnl`'s output unchanged.
    `opening_balances` — {receivables, inventory, payables} at the start, i.e.
             the latest closed month's actual balance sheet. When omitted they
             are IMPLIED from the first projected month, which makes the first
             month's working-capital swing exactly zero: that is the right
             default for reading the shape of a year in isolation, and the wrong
             one for asking "what does cash do from where we actually are". The
             callers pass the measured balances; the fallback exists so the
             function is usable with a P&L alone.
    `capex_by_month` — {month: EUR}. Absent months spend nothing.
    `tax_rate_pct` — a CASH tax rate applied to positive EBITDA, or None. None
             means "not stated", and then no tax is deducted and a caveat says
             so. It is never guessed: a made-up tax rate moves the cash trough
             by more than most of the assumptions this app guards.
    `min_cash_eur` — the covenant or comfort floor to test the trough against.

    Per month, in this order:

    1. **Balances the trading pattern implies.** receivables = revenue/days ×
       DSO, inventory = COGS/days × DIO, payables = COGS/days × DPO. Balances
       therefore carry the revenue and cost SHAPE of the scenario — which is
       what makes a forward-basis budget (whose monthly COGS follows the curve)
       produce a genuinely different cash profile from a flat one.
    2. **The working-capital swing**, versus the previous month's balances:
       `Δreceivables + Δinventory − Δpayables`. Positive means cash absorbed.
       Signs are taken from WC_COMPONENTS rather than written out per line, so
       there is one place the convention lives.
    3. **Cash tax** on positive EBITDA only — a loss-making month does not
       generate a refund here, because carry-back rules are not modelled.
    4. **Capex** from the plan.
    5. `free_cash_flow = ebitda − wc_change − tax − capex`, and the closing cash
       balance rolls forward.

    Two invariants hold and are asserted by tests/test_cash_flow.py: Σ free cash
    flow equals closing cash − opening cash (`check_residual`, the same
    discipline as the variance and EBITDA bridges), and the total
    working-capital swing equals the closing balances minus the opening ones —
    so the year's cash absorption can always be pointed at a balance sheet
    movement rather than at a modelling artefact.

    Returns an {"error": …} dict, never raises, when there are no monthly rows.
    """
    clean = _months_sorted(rows)
    if not clean:
        return {"error": "No monthly P&L rows supplied, so there is no cash profile to build. "
                         "Build or pick a scenario first — its by_month is the input here."}

    days = {"dso_days": _f(dso_days), "dio_days": _f(dio_days),
            "dpo_days": _f(dpo_days)}
    capex_plan = {str(k): _f(v) for k, v in (capex_by_month or {}).items()}
    rate = None if tax_rate_pct is None else max(0.0, _f(tax_rate_pct))
    opening_cash = _f(opening_cash_eur)

    def balances_for(row) -> dict:
        d = days_in_month(row.get("month")) or FALLBACK_DAYS
        flows = {"revenue": _f(row.get("revenue_eur")), "cogs": _f(row.get("cogs_eur"))}
        return {name: flows[flow_key] / d * days[days_key]
                for name, days_key, flow_key, _s, _l in WC_COMPONENTS}

    # An omitted opening balance sheet implies itself from month one, so the
    # first month shows no artificial step. See the argument's docstring.
    implied_opening = balances_for(clean[0])
    prev = {name: (_f((opening_balances or {}).get(name), implied_opening[name])
                   if opening_balances else implied_opening[name])
            for name, _dk, _fk, _s, _l in WC_COMPONENTS}
    opening_snapshot = dict(prev)

    months_out: list[dict] = []
    cash = opening_cash
    trough: dict | None = None
    below: list[str] = []
    acc = {"ebitda_eur": 0.0, "working_capital_change_eur": 0.0, "tax_eur": 0.0,
           "capex_eur": 0.0, "free_cash_flow_eur": 0.0}
    acc |= {f"d_{name}_eur": 0.0 for name, _dk, _fk, _s, _l in WC_COMPONENTS}

    for row in clean:
        month = str(row.get("month"))
        revenue, cogs = _f(row.get("revenue_eur")), _f(row.get("cogs_eur"))
        opex = _f(row.get("opex_eur"))
        ebitda = (_f(row.get("ebitda_eur")) if row.get("ebitda_eur") is not None
                  else revenue - cogs - opex)

        balances = balances_for(row)
        wc_change = 0.0
        deltas = {}
        for name, _days_key, _flow_key, sign, _label in WC_COMPONENTS:
            delta = balances[name] - prev[name]
            deltas[name] = delta
            wc_change += sign * delta
            acc[f"d_{name}_eur"] += delta
        prev = balances

        tax = 0.0 if rate is None else max(0.0, ebitda) * rate / 100.0
        capex = capex_plan.get(month, 0.0)
        fcf = ebitda - wc_change - tax - capex

        month_opening, cash = cash, cash + fcf
        entry = {
            "month": month,
            "days": days_in_month(month),
            "revenue_eur": revenue,
            "cogs_eur": cogs,
            "opex_eur": opex,
            "ebitda_eur": ebitda,
            "working_capital_eur": (balances["receivables"] + balances["inventory"]
                                    - balances["payables"]),
            "working_capital_change_eur": wc_change,
            "tax_eur": tax,
            "capex_eur": capex,
            "free_cash_flow_eur": fcf,
            "opening_cash_eur": month_opening,
            "closing_cash_eur": cash,
        }
        for name, _days_key, _flow_key, _sign, _label in WC_COMPONENTS:
            entry[f"{name}_eur"] = balances[name]
            entry[f"d_{name}_eur"] = deltas[name]
        months_out.append(entry)

        acc["ebitda_eur"] += ebitda
        acc["working_capital_change_eur"] += wc_change
        acc["tax_eur"] += tax
        acc["capex_eur"] += capex
        acc["free_cash_flow_eur"] += fcf

        if trough is None or cash < trough["closing_cash_eur"]:
            trough = {"month": month, "closing_cash_eur": cash}
        if min_cash_eur is not None and cash < _f(min_cash_eur):
            below.append(month)

    ebitda_total = acc["ebitda_eur"]
    totals = dict(acc)
    totals |= {
        "opening_cash_eur": opening_cash,
        "closing_cash_eur": cash,
        "cash_delta_eur": cash - opening_cash,
        # EBITDA that survives to cash. None, never 0, when EBITDA is nil: "the
        # ratio is undefined" and "none of it converts" are different answers.
        "cash_conversion_pct": (acc["free_cash_flow_eur"] / ebitda_total * 100.0
                               if abs(ebitda_total) > EPS else None),
        # Always ~0 (float noise only), for the same reason the variance and
        # EBITDA bridges carry one: a change that breaks additivity fails loudly
        # instead of drifting silently.
        "check_residual": (cash - opening_cash) - acc["free_cash_flow_eur"],
    }

    caveats = [
        "This is free cash flow before financing: EBITDA less the working-capital swing, "
        "capex and cash tax. Depreciation, interest, debt repayment and dividends are NOT "
        "modelled — there is no dataset for any of them — so this is not net income and not "
        "a closing bank balance you can reconcile.",
        "Working-capital balances are implied by the days, not scheduled invoice by invoice: "
        "they carry the scenario's monthly revenue and cost shape, which is the point, but a "
        "single month's collection timing can differ.",
    ]
    if rate is None:
        caveats.append("No cash tax rate has been stated, so no tax is deducted. The figures "
                       "below are before tax — set a rate on the Budget page to include it.")
    if opening_balances is None:
        caveats.append("No opening balance sheet was supplied, so the opening receivables, "
                       "inventory and payables are implied by the first projected month. The "
                       "first month therefore shows no working-capital step; the shape across "
                       "the year is unaffected.")

    return {
        "months": months_out,
        "totals": totals,
        "days": dict(days) | {"cash_conversion_cycle_days": (
            days["dso_days"] + days["dio_days"] - days["dpo_days"])},
        "opening_balances": opening_snapshot,
        "closing_balances": dict(prev),
        "opening_balances_implied": opening_balances is None,
        "tax_rate_pct": rate,
        "min_cash_eur": None if min_cash_eur is None else _f(min_cash_eur),
        "trough": trough,
        "months_below_minimum": below,
        "breaches_minimum": bool(below),
        "caveats": caveats,
    }


# --------------------------------------------------------------------------
# 3. The bridge
# --------------------------------------------------------------------------

def cash_bridge(result: dict) -> list[dict]:
    """EBITDA → free cash flow as waterfall points, ready for `render_chart`.

    Deliberately shaped like `tools._ebitda_bridge_points`: two absolutes
    bookending signed deltas, every step summing to the difference. The three
    working-capital components are separate steps because "€1.4M of it is
    inventory" is the sentence a CFO acts on, and folding them into one
    "working capital" bar would hide exactly that.

    Values are the effect ON CASH, so a rising receivables balance appears as a
    negative step even though the balance went up.
    """
    totals = (result or {}).get("totals") or {}
    if not totals:
        return []
    steps = [(f"{name.capitalize()} ({days_key.removesuffix('_days').upper()})",
              -sign * _f(totals.get(f"d_{name}_eur")))
             for name, days_key, _flow, sign, _label in WC_COMPONENTS]
    steps.append(("Cash tax", -_f(totals.get("tax_eur"))))
    steps.append(("Capex", -_f(totals.get("capex_eur"))))

    points = [{"label": "EBITDA", "value": round(_f(totals.get("ebitda_eur")), 2),
               "kind": "absolute"}]
    points += [{"label": label, "value": round(value, 2), "kind": "delta"}
               for label, value in steps if abs(value) > 0.005]
    points.append({"label": "Free cash flow",
                   "value": round(_f(totals.get("free_cash_flow_eur")), 2),
                   "kind": "absolute"})
    return points


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _f(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out != out else out          # NaN → default


def _months_sorted(rows) -> list[dict]:
    """Rows with a usable month, one per month, in month order.

    Duplicates collapse by summing the flows and taking the LAST balance —
    balances are stocks, so summing two readings of the same month's
    receivables would double it. Callers pass one row per month; this exists so
    an un-aggregated frame does not silently double the working capital.
    """
    merged: OrderedDict[str, dict] = OrderedDict()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        month = row.get("month")
        if month in (None, ""):
            continue
        key = str(month)
        if key not in merged:
            merged[key] = dict(row) | {"month": key}
            continue
        slot = merged[key]
        for field in ("revenue_eur", "cogs_eur", "opex_eur", "ebitda_eur"):
            if row.get(field) is not None:
                slot[field] = _f(slot.get(field)) + _f(row.get(field))
        for name, _dk, _fk, _sign, _label in WC_COMPONENTS:
            field = f"{name}_eur"
            if row.get(field) is not None:
                slot[field] = _f(row.get(field))
        if row.get("cash_eur") is not None:
            slot["cash_eur"] = _f(row.get("cash_eur"))
    return [merged[k] for k in sorted(merged)]
