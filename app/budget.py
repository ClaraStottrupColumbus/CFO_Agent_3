# budget.py — The budgeting maths. Pure functions, no I/O, no app imports.
#
# This module is deliberately a leaf: it takes plain lists/dicts of numbers and
# returns plain dicts. tools.py does the pandas and the file reading; everything
# here is arithmetic the model must never do itself. That separation is what
# makes the three test files in tests/ possible without touching disk or the SDK.
#
# Three things live here:
#   1. variance_decomposition — splits a revenue/cost delta into price, volume,
#      mix and joint effects that sum EXACTLY to the total change.
#   2. driver_exposure / sensitivity / breakeven_shock — what a ±X% move in an
#      input price does to COGS and EBITDA, driven off the bill of materials
#      rather than a guessed elasticity.
#   3. project_pnl — the scenario engine: apply an assumption set to a baseline
#      and get a full monthly P&L projection back.

from __future__ import annotations

from collections import OrderedDict

# Below this, a denominator is treated as zero rather than divided by.
EPS = 1e-9


# --------------------------------------------------------------------------
# 1. Variance decomposition
# --------------------------------------------------------------------------

def variance_decomposition(base: list[dict], current: list[dict], *,
                           key: str = "entity", price_key: str = "price",
                           volume_key: str = "volume") -> dict:
    """Split (Σp₁q₁ − Σp₀q₀) into price, volume, mix and joint effects.

    With products *i*, Q = Σq, and p̄₀ = Σp₀q₀ / Q₀:

        price_effect  = Σ (p₁ᵢ − p₀ᵢ) · q₀ᵢ
        volume_effect = (Q₁ − Q₀) · p̄₀
        mix_effect    = Σ p₀ᵢ·q₁ᵢ − Q₁ · p̄₀
        joint_effect  = Σ (p₁ᵢ − p₀ᵢ)(q₁ᵢ − q₀ᵢ)

    These sum exactly to the total delta — that identity is the invariant the
    tests assert, and it is why `joint_effect` is reported rather than folded
    into price. A CFO cannot act on "COGS is €1.2M over" but can act on "€900k
    of it is chicken-meal price, €300k is volume".

    The mix term is written as `Σp₀q₁ − Q₁p̄₀` rather than the equivalent
    `Q₁·(Σp₀ᵢs₁ᵢ − p̄₀)` so it needs no division by Q₁ — a period in which
    everything was discontinued then decomposes cleanly instead of dividing by
    zero.

    Entities present in only one period carry their price across from the period
    where they exist. A launched or discontinued product is then a volume/mix
    story rather than a spurious ±100% price move; the identity holds either
    way, but this keeps `price_effect` meaning "prices changed on things we sold
    in both periods".

    Returns an {"error": …} dict — never raises — when the base period has no
    volume, since p̄₀ is undefined there.
    """
    b = _index_rows(base, key, price_key, volume_key)
    c = _index_rows(current, key, price_key, volume_key)
    if isinstance(b, dict) and "error" in b:
        return b
    if isinstance(c, dict) and "error" in c:
        return c
    if not b and not c:
        return {"error": "No rows supplied for either period; nothing to decompose."}

    entities = list(OrderedDict.fromkeys(list(b) + list(c)))

    q0_total = sum(b.get(e, (0.0, 0.0))[1] for e in entities)
    q1_total = sum(c.get(e, (0.0, 0.0))[1] for e in entities)

    if abs(q0_total) < EPS:
        return {"error": "Base-period volume is zero, so the average base price "
                         "is undefined and the decomposition cannot be computed. "
                         "Pick a base period with volume, or compare amounts directly."}

    value0 = sum(p * q for p, q in b.values())
    avg_p0 = value0 / q0_total

    price_effect = joint_effect = mix_num = 0.0
    lines = []
    for e in entities:
        p0, q0 = b.get(e, (None, 0.0))
        p1, q1 = c.get(e, (None, 0.0))
        # Carry the price across for entities that exist in only one period.
        if p0 is None:
            p0 = p1 if p1 is not None else 0.0
        if p1 is None:
            p1 = p0
        dp, dq = p1 - p0, q1 - q0
        e_price = dp * q0
        e_joint = dp * dq
        price_effect += e_price
        joint_effect += e_joint
        mix_num += p0 * q1
        lines.append({
            key: e,
            "base_price": p0, "current_price": p1,
            "base_volume": q0, "current_volume": q1,
            "price_effect": e_price,
            "joint_effect": e_joint,
        })

    volume_effect = (q1_total - q0_total) * avg_p0
    mix_effect = mix_num - q1_total * avg_p0

    value1 = sum(p * q for p, q in c.values())
    total_delta = value1 - value0
    residual = total_delta - (price_effect + volume_effect + mix_effect + joint_effect)

    return {
        "base_value": value0,
        "current_value": value1,
        "total_delta": total_delta,
        "price_effect": price_effect,
        "volume_effect": volume_effect,
        "mix_effect": mix_effect,
        "joint_effect": joint_effect,
        # Always ~0 (float noise only). Surfaced so a future change that breaks
        # additivity fails loudly instead of drifting silently.
        "check_residual": residual,
        "base_volume": q0_total,
        "current_volume": q1_total,
        "base_avg_price": avg_p0,
        "lines": lines,
    }


def _index_rows(rows: list[dict], key: str, price_key: str, volume_key: str):
    """{entity: (price, volume)} from a list of row dicts, summing duplicates.

    Duplicate entities (e.g. the same product line across regions) are combined
    volume-weighted, so the caller can pass un-aggregated rows.
    """
    acc: OrderedDict[str, list[float]] = OrderedDict()
    for row in rows or []:
        if key not in row:
            return {"error": f"Row is missing the '{key}' column: {sorted(row)}"}
        try:
            q = float(row.get(volume_key) or 0.0)
            raw_p = row.get(price_key)
            p = None if raw_p is None else float(raw_p)
        except (TypeError, ValueError):
            return {"error": f"Row '{row.get(key)}' has a non-numeric "
                             f"'{price_key}' or '{volume_key}'."}
        if p is None:
            continue
        name = str(row[key])
        slot = acc.setdefault(name, [0.0, 0.0])
        slot[0] += p * q   # value
        slot[1] += q       # volume
    out: OrderedDict[str, tuple[float, float]] = OrderedDict()
    for name, (value, q) in acc.items():
        out[name] = ((value / q) if abs(q) > EPS else 0.0, q)
    return out


# --------------------------------------------------------------------------
# 2. Exposure, sensitivity and breakeven
# --------------------------------------------------------------------------

def driver_exposure(bom: list[dict], volumes: dict, driver_price: float,
                    driver_id: str) -> float:
    """Annual spend on one driver: Σᵢ qty_per_tonne(i,d) · volume(i) · price(d).

    This is a fact read out of the bill of materials, not an assumption — which
    is the whole reason sensitivity is driven off the BOM rather than a guessed
    elasticity.
    """
    total_qty = 0.0
    for row in bom or []:
        if row.get("driver_id") != driver_id:
            continue
        line = row.get("product_line")
        total_qty += float(row.get("qty_per_tonne") or 0.0) * float(volumes.get(line, 0.0) or 0.0)
    return total_qty * float(driver_price or 0.0)


def sensitivity(exposure: float, shock_pct: float, *, hedge_coverage: float = 0.0,
                price_pass_through: float = 0.0, baseline: dict | None = None) -> dict:
    """What a `shock_pct` move in one driver does to COGS and EBITDA.

        Δcogs   = shock · E_d · (1 − hedge_coverage)
        Δebitda = −Δcogs · (1 − price_pass_through)

    `price_pass_through` is the fraction of a cost increase recovered through
    selling prices, so it also moves revenue by +τ·Δcogs — which is why EBITDA
    moves by less than COGS. A falling cost driver (negative shock) therefore
    raises EBITDA.
    """
    hedge = _clamp(hedge_coverage, 0.0, 1.0)
    tau = _clamp(price_pass_through, 0.0, 1.0)
    shock = float(shock_pct) / 100.0

    unhedged = float(exposure) * (1.0 - hedge)
    d_cogs = shock * unhedged
    d_revenue = tau * d_cogs
    d_ebitda = -d_cogs * (1.0 - tau)

    out = {
        "exposure_eur": float(exposure),
        "unhedged_exposure_eur": unhedged,
        "shock_pct": float(shock_pct),
        "hedge_coverage": hedge,
        "price_pass_through": tau,
        "delta_cogs_eur": d_cogs,
        "delta_revenue_eur": d_revenue,
        "delta_ebitda_eur": d_ebitda,
    }

    if baseline:
        rev = float(baseline.get("revenue_eur") or 0.0)
        cogs = float(baseline.get("cogs_eur") or 0.0)
        opex = float(baseline.get("opex_eur") or 0.0)
        ebitda = rev - cogs - opex
        rev_new = rev + d_revenue
        ebitda_new = ebitda + d_ebitda
        out["baseline_ebitda_eur"] = ebitda
        out["projected_ebitda_eur"] = ebitda_new
        if abs(rev) > EPS:
            out["baseline_ebitda_margin_pct"] = ebitda / rev * 100.0
        if abs(rev_new) > EPS:
            out["projected_ebitda_margin_pct"] = ebitda_new / rev_new * 100.0
            out["margin_delta_pp"] = (out["projected_ebitda_margin_pct"]
                                      - out.get("baseline_ebitda_margin_pct", 0.0))
    return out


def breakeven_shock(exposure: float, baseline: dict, floor_margin_pct: float, *,
                    hedge_coverage: float = 0.0,
                    price_pass_through: float = 0.0) -> float | None:
    """The shock (in %) at which projected EBITDA margin lands exactly on the floor.

    Solved analytically rather than searched. With k = E_d(1 − hedge),
    Δcogs = s·k, revenue R' = R + τ·s·k and EBITDA' = E − s·k(1 − τ):

        (E − s·k(1−τ)) / (R + s·k·τ) = f
        ⇒ s = (E − f·R) / ( k · [(1−τ) + f·τ] )

    Returns None when there is no exposure to move (k = 0, e.g. fully hedged)
    or the denominator vanishes — there is genuinely no such shock then.
    """
    tau = _clamp(price_pass_through, 0.0, 1.0)
    k = float(exposure) * (1.0 - _clamp(hedge_coverage, 0.0, 1.0))
    if abs(k) < EPS:
        return None

    rev = float(baseline.get("revenue_eur") or 0.0)
    cogs = float(baseline.get("cogs_eur") or 0.0)
    opex = float(baseline.get("opex_eur") or 0.0)
    ebitda = rev - cogs - opex
    f = float(floor_margin_pct) / 100.0

    denom = k * ((1.0 - tau) + f * tau)
    if abs(denom) < EPS:
        return None
    return (ebitda - f * rev) / denom * 100.0


def _clamp(v, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------
# 3. The scenario engine
# --------------------------------------------------------------------------

def project_pnl(baseline_rows: list[dict], bom: list[dict], driver_prices: dict,
                assumptions: dict, *, hedges: dict | None = None,
                price_pass_through: float = 0.0,
                opex_inflation_pct: float = 0.0) -> dict:
    """Apply an assumption set to a baseline and return a full monthly P&L.

    `baseline_rows`  — [{month, product_line, volume_tonnes, revenue_eur,
                         cogs_eur, opex_eur}, …]
    `bom`            — [{product_line, driver_id, qty_per_tonne}, …]
    `driver_prices`  — {driver_id: baseline price}
    `assumptions`    — {driver_id: pct change vs that baseline price}
    `hedges`         — {driver_id: 0..1 coverage}; unhedged when absent.

    Each driver's cost change is applied through the bill of materials, so the
    projection responds to *what the products actually consume* rather than to a
    blanket percentage. An all-zero assumption set reproduces the baseline
    exactly — the property test_scenario_engine asserts first.
    """
    hedges = hedges or {}
    tau = _clamp(price_pass_through, 0.0, 1.0)
    opex_factor = 1.0 + float(opex_inflation_pct or 0.0) / 100.0

    # qty_per_tonne lookup: {product_line: {driver_id: qty}}
    by_line: dict[str, dict[str, float]] = {}
    for row in bom or []:
        line = row.get("product_line")
        if line is None:
            continue
        by_line.setdefault(str(line), {})[str(row.get("driver_id"))] = float(
            row.get("qty_per_tonne") or 0.0)

    rows_out = []
    driver_impact: dict[str, float] = {}

    for row in baseline_rows or []:
        line = str(row.get("product_line"))
        volume = float(row.get("volume_tonnes") or 0.0)
        revenue = float(row.get("revenue_eur") or 0.0)
        cogs = float(row.get("cogs_eur") or 0.0)
        opex = float(row.get("opex_eur") or 0.0)

        d_cogs = 0.0
        for driver_id, qty in by_line.get(line, {}).items():
            pct = float(assumptions.get(driver_id, 0.0) or 0.0)
            if pct == 0.0 or qty == 0.0:
                continue
            price = float(driver_prices.get(driver_id, 0.0) or 0.0)
            unhedged = 1.0 - _clamp(hedges.get(driver_id, 0.0), 0.0, 1.0)
            delta = (pct / 100.0) * qty * volume * price * unhedged
            d_cogs += delta
            driver_impact[driver_id] = driver_impact.get(driver_id, 0.0) + delta

        d_revenue = tau * d_cogs
        new_revenue = revenue + d_revenue
        new_cogs = cogs + d_cogs
        new_opex = opex * opex_factor
        rows_out.append({
            "month": row.get("month"),
            "product_line": line,
            "volume_tonnes": volume,
            "revenue_eur": new_revenue,
            "cogs_eur": new_cogs,
            "opex_eur": new_opex,
            # Invariant asserted per row by test_scenario_engine.
            "ebitda_eur": new_revenue - new_cogs - new_opex,
            "gross_margin_eur": new_revenue - new_cogs,
            "delta_cogs_eur": d_cogs,
            "delta_revenue_eur": d_revenue,
        })

    totals = _sum_rows(rows_out)
    by_month = _group_totals(rows_out, "month")
    by_line_totals = _group_totals(rows_out, "product_line")

    return {
        "rows": rows_out,
        "by_month": by_month,
        "by_product_line": by_line_totals,
        "totals": totals,
        "driver_impact_eur": driver_impact,
        "assumptions": dict(assumptions or {}),
        "price_pass_through": tau,
        "opex_inflation_pct": float(opex_inflation_pct or 0.0),
    }


def _sum_rows(rows: list[dict]) -> dict:
    revenue = sum(r["revenue_eur"] for r in rows)
    cogs = sum(r["cogs_eur"] for r in rows)
    opex = sum(r["opex_eur"] for r in rows)
    out = {
        "volume_tonnes": sum(r["volume_tonnes"] for r in rows),
        "revenue_eur": revenue,
        "cogs_eur": cogs,
        "opex_eur": opex,
        "gross_margin_eur": revenue - cogs,
        "ebitda_eur": revenue - cogs - opex,
    }
    out["ebitda_margin_pct"] = (out["ebitda_eur"] / revenue * 100.0) if abs(revenue) > EPS else None
    out["gross_margin_pct"] = (out["gross_margin_eur"] / revenue * 100.0) if abs(revenue) > EPS else None
    return out


def _group_totals(rows: list[dict], field: str) -> list[dict]:
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for r in rows:
        groups.setdefault(r.get(field), []).append(r)
    return [{field: k, **_sum_rows(v)} for k, v in groups.items()]
