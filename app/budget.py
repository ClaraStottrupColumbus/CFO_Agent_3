# budget.py — The budgeting maths. Pure functions, no I/O, no app imports.
#
# This module is deliberately a leaf: it takes plain lists/dicts of numbers and
# returns plain dicts. tools.py does the pandas and the file reading; everything
# here is arithmetic the model must never do itself. That separation is what
# makes the three test files in tests/ possible without touching disk or the SDK.
#
# Four things live here:
#   1. variance_decomposition — splits a revenue/cost delta into price, volume,
#      mix and joint effects that sum EXACTLY to the total change.
#   2. driver_exposure / sensitivity / breakeven_shock — what a ±X% move in an
#      input price does to COGS and EBITDA, driven off the bill of materials
#      rather than a guessed elasticity.
#   3. normalise_assumptions / opex_bridge — the assumption vocabulary: four
#      blocks (drivers, volume, price, opex) and the cost-centre bridge that
#      turns a driver shock into an opex growth rate.
#   4. project_pnl — the scenario engine: apply an assumption set to a baseline
#      and get a full monthly P&L projection back.

from __future__ import annotations

from collections import OrderedDict

# Below this, a denominator is treated as zero rather than divided by.
EPS = 1e-9

# The four assumption blocks a scenario is expressed in. A budget round talks
# about volume and price long before it talks about chicken meal, so all four
# are first-class rather than the drivers block alone.
ASSUMPTION_BLOCKS = ("drivers", "volume", "price", "opex")

# Wildcard key in the volume/price blocks: applies to every product line.
ALL_LINES = "*"


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
# 3. The assumption vocabulary
# --------------------------------------------------------------------------

def normalise_assumptions(raw) -> dict:
    """Lift any accepted assumption shape into the four-block canonical form.

        {"drivers": {driver_id: pct}, "volume": {line: pct},
         "price":   {line: pct},      "opex":   {driver_id | cost_centre: pct}}

    A legacy flat `{driver_id: pct}` dict — every scenario written before the
    blocks existed, and every caller that only shocks drivers — lifts into
    `{"drivers": …}`. That is what lets `data/scenarios.json` keep working with
    no migration step.

    The two shapes are told apart by the *value*, not the key: a block's value
    is a dict, a flat assumption's value is a number. A driver that happened to
    be called `price` therefore still reads as a driver, so the discriminator
    cannot be broken by naming a driver after a block.

    Values that will not parse as numbers are dropped rather than raised on —
    this runs on read, where a half-written file must not take the page down.
    Callers that can teach the model (tools.py) validate strictly first.
    """
    out: dict[str, dict[str, float]] = {b: {} for b in ASSUMPTION_BLOCKS}
    if not isinstance(raw, dict):
        return out
    structured = any(k in ASSUMPTION_BLOCKS and isinstance(v, dict) for k, v in raw.items())
    if structured:
        for block in ASSUMPTION_BLOCKS:
            out[block] = _numeric_map(raw.get(block))
    else:
        out["drivers"] = _numeric_map(raw)
    return out


def _numeric_map(d) -> dict[str, float]:
    if not isinstance(d, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in d.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def repriced_drivers(before: dict, after: dict) -> list[dict]:
    """Drivers whose LOCKED price moved between two computations of one scenario.

    Recomputing a scenario re-reads the locked assumptions, so its EBITDA can
    move with not one percentage changed. That is the same fact
    `budgetversions.diff`'s locked-values cut exists to expose months later; here
    it is exposed at the moment of the edit, because a CFO who changed a name and
    got a different EBITDA back deserves the reason in the same breath.

    The threshold is RELATIVE, not absolute, and it is the reason this is not a
    plain inequality: a price that was stored as a spot fallback and is now read
    off a rounded locked value differs in the fifth decimal, and every USD-quoted
    driver inherits that from the FX rate. Reported literally, an edit that
    changed nothing would print fourteen drivers at "+0.0%" — a sentence that
    tells the CFO to distrust a figure that is in fact correct. Anything that
    rounds to 0.0% at the precision the UI prints is not a re-lock worth naming.

    `pct` is None on a zero baseline — an undefined ratio, never 0, the same
    distinction driver_status draws for a missing forward curve. A price that
    appeared from nothing is always reported, however small.
    """
    rows = []
    for driver_id in after:
        was, now = _num(before.get(driver_id)), _num(after.get(driver_id))
        if was is None or now is None:
            continue
        if was and abs(now - was) / abs(was) < REPRICE_MIN_PCT / 100.0:
            continue
        if not was and now == 0:
            continue
        rows.append({"driver_id": str(driver_id), "from": was, "to": now,
                     "pct": ((now - was) / was * 100.0) if was else None})
    rows.sort(key=lambda r: -abs(r["to"] - r["from"]))
    return rows


# The precision the UI prints a driver move at. Below this a "re-locked" note
# would read as "+0.0%", which is noise wearing the clothes of a finding.
REPRICE_MIN_PCT = 0.05


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def opex_bridge(opex_rows: list[dict], driver_assumptions: dict,
                opex_overrides: dict, default_pct: float) -> dict:
    """Move each opex cost centre by its own rate, and report the weighted total.

    `opex_rows` — [{cost_centre, driver_id, amount_eur}, …]; duplicates (one row
    per month) are summed per centre.

    Per centre, the first of these that exists wins:

      1. an explicit override keyed by cost centre  → basis "override"
      2. an explicit override keyed by its driver   → basis "override"
      3. that driver's percentage from the drivers block → basis "driver"
      4. `default_pct` (the blanket opex inflation) → basis "default"

    Step 3 is the point of the function: `opex_plan.csv` has carried a
    `driver_id` column that drove nothing, so wage inflation reached the budget
    as a hand-typed number while chicken meal went through the bill of
    materials. Now both flow through the same machinery.

    **Why only a weighted rate comes back out.** `opex_plan` is cut by cost
    centre; `project_pnl`'s baseline opex comes from `budget_vs_actuals` cut by
    product line. The two totals need not reconcile, and inventing a
    reconciliation would be worse than not having one. So the weighted rate —
    which is scale-free — is what gets applied to the baseline opex level, and
    the per-centre detail is returned alongside so the narrative can say
    "Production opex +€412k, all of it wage inflation." Do not "improve" this
    into a direct substitution without solving the two-cuts problem first.

    With no rows (or no opex value at all) the weighted rate is `default_pct`,
    so a caller that knows nothing about cost centres gets exactly the old
    blanket-percentage behaviour.
    """
    default_pct = float(default_pct or 0.0)
    drivers_block = _numeric_map(driver_assumptions)
    overrides = _numeric_map(opex_overrides)

    # {cost_centre: [amount, driver_id]} — one row per month collapses to one.
    centres: OrderedDict[str, list] = OrderedDict()
    for row in opex_rows or []:
        centre = row.get("cost_centre")
        if centre is None:
            continue
        try:
            amount = float(row.get("amount_eur") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        did = row.get("driver_id")
        did = None if did is None or str(did) in ("", "nan", "None") else str(did)
        slot = centres.setdefault(str(centre), [0.0, did])
        slot[0] += amount
        if slot[1] is None:
            slot[1] = did

    lines = []
    base_total = delta_total = 0.0
    for centre, (amount, did) in centres.items():
        if centre in overrides:
            pct, basis = overrides[centre], "override"
        elif did is not None and did in overrides:
            pct, basis = overrides[did], "override"
        elif did is not None and did in drivers_block:
            pct, basis = drivers_block[did], "driver"
        else:
            pct, basis = default_pct, "default"
        delta = amount * pct / 100.0
        base_total += amount
        delta_total += delta
        lines.append({"cost_centre": centre, "driver_id": did, "basis": basis,
                      "pct": pct, "base_eur": amount, "delta_eur": delta})

    weighted = (delta_total / base_total * 100.0) if abs(base_total) > EPS else default_pct
    return {
        "by_cost_centre": lines,
        "base_eur": base_total,
        "delta_eur": delta_total,
        "weighted_pct": weighted,
        "default_pct": default_pct,
    }


# --------------------------------------------------------------------------
# 4. The scenario engine
# --------------------------------------------------------------------------

def project_pnl(baseline_rows: list[dict], bom: list[dict], driver_prices: dict,
                assumptions: dict, *, hedges: dict | None = None,
                price_pass_through: float = 0.0,
                opex_inflation_pct: float = 0.0,
                opex_rows: list[dict] | None = None,
                driver_prices_by_month: dict | None = None) -> dict:
    """Apply an assumption set to a baseline and return a full monthly P&L.

    `baseline_rows`  — [{month, product_line, volume_tonnes, revenue_eur,
                         cogs_eur, opex_eur}, …]
    `bom`            — [{product_line, driver_id, qty_per_tonne}, …]
    `driver_prices`  — {driver_id: baseline price}. This is the price the
                       baseline P&L was BUILT on — normally the locked value —
                       and every driver delta is measured from it.
    `assumptions`    — the four blocks, or a legacy flat {driver_id: pct}; see
                       `normalise_assumptions`.
    `hedges`         — {driver_id: 0..1 coverage}; unhedged when absent.
    `opex_rows`      — cost-centre rows for `opex_bridge`; without them opex
                       moves by the blanket `opex_inflation_pct`.
    `driver_prices_by_month` — optional {month: {driver_id: price}}: the price
                       to APPLY in that month, in the same currency as
                       `driver_prices`. This is how a forward curve enters the
                       budget — the shock in month m becomes
                       `curve(d, m) × (1 + pct/100)` against the locked price,
                       so per-month seasonality falls out of the curve instead
                       of being assumed. A month or driver the mapping does not
                       cover falls back to `driver_prices`, i.e. no implied
                       move, which is what makes it a pure extension: omit the
                       argument and the arithmetic is unchanged.

    **Application order, per baseline row.** This sequence is the contract; the
    tests in test_scenario_engine.py pin each step.

    1. **Volume.** `volume' = volume × (1 + v/100)`. Revenue and COGS scale with
       it — both are variable in tonnes — so gross margin *percent* is
       unchanged by volume alone. Opex does **not** scale: it is period cost.
    2. **Driver cost** through the bill of materials, on the *post-volume*
       tonnage and net of hedge coverage. The price applied is the month's
       entry in `driver_prices_by_month` where one exists, otherwise the
       baseline price — so a driver moves on the curve, on the CFO's
       percentage, or on both, and all three are one subtraction.
    3. **Explicit price.** `revenue' += revenue_after_volume × (p/100)`.
    4. **Pass-through τ, only on lines with no explicit price assumption.** τ
       and an explicit price are two ways of saying the same thing — "we
       recover cost in price" — so applying both would double-count recovery
       and quietly overstate EBITDA. τ therefore means "automatic recovery
       where the CFO has not priced deliberately". A `"*"` price entry, or an
       explicit 0 (a decision to hold price), counts as deliberate.
    5. **Opex** at the weighted rate from `opex_bridge`.

    `ebitda = revenue − cogs − opex` holds on every row, and an all-zero
    assumption set reproduces the baseline exactly — the property
    test_scenario_engine asserts first.
    """
    hedges = hedges or {}
    by_month_prices = driver_prices_by_month or {}
    tau = _clamp(price_pass_through, 0.0, 1.0)
    spec = normalise_assumptions(assumptions)
    vol_block, price_block = spec["volume"], spec["price"]

    bridge = opex_bridge(opex_rows or [], spec["drivers"], spec["opex"],
                         float(opex_inflation_pct or 0.0))
    opex_factor = 1.0 + bridge["weighted_pct"] / 100.0

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
    # EBITDA attribution, accumulated across rows. Each driver's entry is net of
    # the τ recovery it triggers, so the four components sum to the total move.
    eb_volume = eb_price = eb_opex = 0.0
    eb_drivers: dict[str, float] = {}
    baseline_ebitda = 0.0

    for row in baseline_rows or []:
        line = str(row.get("product_line"))
        volume = float(row.get("volume_tonnes") or 0.0)
        revenue = float(row.get("revenue_eur") or 0.0)
        cogs = float(row.get("cogs_eur") or 0.0)
        opex = float(row.get("opex_eur") or 0.0)
        baseline_ebitda += revenue - cogs - opex

        # 1. Volume scales tonnes, revenue and COGS together.
        v_pct, _ = _line_pct(vol_block, line)
        scale = 1.0 + v_pct / 100.0
        new_volume = volume * scale
        rev_v, cogs_v = revenue * scale, cogs * scale
        eb_volume += (revenue - cogs) * (scale - 1.0)

        # 2. Driver cost on the post-volume tonnage.
        month_prices = by_month_prices.get(row.get("month")) or {}
        d_cogs = 0.0
        row_drivers: dict[str, float] = {}
        for driver_id, qty in by_line.get(line, {}).items():
            if qty == 0.0:
                continue
            pct = float(spec["drivers"].get(driver_id, 0.0) or 0.0)
            base_price = float(driver_prices.get(driver_id, 0.0) or 0.0)
            # The percentage applies ON TOP of whatever price the month carries,
            # and the delta is always measured back to the baseline price. With
            # no curve the two are the same number and this reduces to
            # base_price × pct/100, exactly as before.
            applied = float(month_prices.get(driver_id, base_price) or 0.0)
            effective = applied * (1.0 + pct / 100.0)
            if effective == base_price:
                continue
            unhedged = 1.0 - _clamp(hedges.get(driver_id, 0.0), 0.0, 1.0)
            delta = (effective - base_price) * qty * new_volume * unhedged
            d_cogs += delta
            row_drivers[driver_id] = row_drivers.get(driver_id, 0.0) + delta
            driver_impact[driver_id] = driver_impact.get(driver_id, 0.0) + delta

        # 3-4. Explicit price, and τ only where price was not set deliberately.
        p_pct, priced = _line_pct(price_block, line)
        d_rev_price = rev_v * p_pct / 100.0
        tau_eff = 0.0 if priced else tau
        d_rev_tau = tau_eff * d_cogs
        eb_price += d_rev_price
        for driver_id, delta in row_drivers.items():
            eb_drivers[driver_id] = eb_drivers.get(driver_id, 0.0) - delta * (1.0 - tau_eff)

        # 5. Opex at the bridged rate — untouched by volume.
        new_revenue = rev_v + d_rev_price + d_rev_tau
        new_cogs = cogs_v + d_cogs
        new_opex = opex * opex_factor
        eb_opex -= new_opex - opex

        rows_out.append({
            "month": row.get("month"),
            "product_line": line,
            "volume_tonnes": new_volume,
            "revenue_eur": new_revenue,
            "cogs_eur": new_cogs,
            "opex_eur": new_opex,
            # Invariant asserted per row by test_scenario_engine.
            "ebitda_eur": new_revenue - new_cogs - new_opex,
            "gross_margin_eur": new_revenue - new_cogs,
            "delta_cogs_eur": new_cogs - cogs,
            "delta_revenue_eur": new_revenue - revenue,
            "delta_volume_tonnes": new_volume - volume,
            "volume_pct": v_pct,
            "price_pct": p_pct,
            "price_is_explicit": priced,
        })

    totals = _sum_rows(rows_out)
    by_month = _group_totals(rows_out, "month")
    by_line_totals = _group_totals(rows_out, "product_line")

    components = eb_volume + eb_price + eb_opex + sum(eb_drivers.values())
    baseline_volume = sum(float(r.get("volume_tonnes") or 0.0) for r in baseline_rows or [])

    return {
        "rows": rows_out,
        "by_month": by_month,
        "by_product_line": by_line_totals,
        "totals": totals,
        "driver_impact_eur": driver_impact,
        "assumptions": spec,
        "price_pass_through": tau,
        "opex_inflation_pct": float(opex_inflation_pct or 0.0),
        "opex_bridge": bridge,
        "volume_delta_tonnes": totals["volume_tonnes"] - baseline_volume,
        "ebitda_bridge": {
            "baseline_ebitda_eur": baseline_ebitda,
            "projected_ebitda_eur": totals["ebitda_eur"],
            "volume_eur": eb_volume,
            "price_eur": eb_price,
            "opex_eur": eb_opex,
            "drivers_eur": eb_drivers,
            "total_eur": components,
            # Always ~0 (float noise only), for the same reason
            # variance_decomposition carries one: a change that breaks
            # additivity fails loudly instead of drifting silently.
            "check_residual": (totals["ebitda_eur"] - baseline_ebitda) - components,
        },
    }


def _line_pct(block: dict, line: str) -> tuple[float, bool]:
    """(percentage, was it stated) for one product line, honouring the wildcard.

    The boolean is presence, not non-zero: an explicit 0 on a price is the CFO
    deciding to hold price, which must still suppress τ on that line.
    """
    if line in block:
        return float(block[line]), True
    if ALL_LINES in block:
        return float(block[ALL_LINES]), True
    return 0.0, False


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
