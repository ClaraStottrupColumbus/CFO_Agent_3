# Demo data

Governs `app/generate_data.py` and the seeded datasets under `data/`. Rebuild with
`.venv/bin/python -m app.generate_data` — seeded and deterministic, `RNG_SEED = 42`. **This module
has no tests at all**; changes to it are verified by eyeballing the demo.

`generate_data.py` is seeded and carries deliberate story hooks: chicken meal +28% in USD against a
September lock (drift rule fires), sea freight +40% at 15% hedged (biggest exposure), wheat −8% (a
partial offset so the news isn't uniformly bad), EUR weakening vs USD, Premium Pet volume +12% vs
budget (a real mix effect), a market opened mid-2026 with no prior-year budget ("the data isn't
there"), and **no payroll dataset at all** — the agent must say so rather than invent wage figures.
Unit price and unit cost are not stored; they are derived as revenue/volume and cogs/volume in
`tools.py` so there is one source of truth. Prices are stored in each driver's **own quote currency**
and converted via the `eur_usd` driver, making FX real arithmetic the model must delegate.

Two properties of that data the Budget page's comparison depends on, and which anybody "tidying"
`_budget_vs_actuals_frame` would break:

- **Budget unit COST is frozen at one lock month for every year it writes** (`lock_costs` at
  `LOCK_MONTH`, 2026-09), while budget unit PRICE is per-year (`_budget_price_month`, the prior
  September). So a booked budget year has no lock of its own, a budget-to-budget comparison is a volume
  and mix story **by construction**, and the page says so rather than implying prices held flat. That is
  also why `actual:YYYY` exists as a selection kind: it is where the price story is real. Changing
  `lock_costs` to be per-year would move 2024–2026 budget COGS and therefore the seeded COGS-variance
  findings, `budget_overview.csv`, `budget_plan.json` and the rule thresholds that fire on that
  history — and `generate_data.py` has **no tests at all**, so it would be verified by eyeballing the
  demo. Do it as its own change, with coverage, or not at all.
- **Adriatic has a budget only in the plan year** (`if region == "Adriatic" and not is_budget_year`).
  That is the "the data isn't there" hook, and it is bigger than it looks: 67% of the 2026-to-2027
  volume step and roughly half the revenue step is a market the baseline never budgeted. The comparison
  quantifies that share instead of merely mentioning the gap, because +13.3% reads as organic growth
  otherwise.

The cash hooks work the same way. DSO drifts out from 44 to 53 days across the history — a leak worth
about a month of EBITDA that a P&L never shows and that has to be *measured* to be found. In the
budget year the extruder line is paid for in February and April, so the plan is fine on EBITDA
(€11.6M) and still breaches the €3.0M facility floor in three months. The point of the shape is that
the two `proposed` capex projects land in September and November: **deferring them does not fix the
trough**, and the only lever that reaches April is collections. "The capex you can defer is after the
problem" is a better demo than a lever that always works. `working_capital.cash_eur` is *walked* month
by month off EBITDA, the working-capital swing and capex — with `FINANCING_OUTFLOW_SHARE` of EBITDA
leaving as debt service and distributions, which is both what keeps the balance realistic and the
honest reason the projection is labelled before-financing: those outflows are in the observed history
and there is no dataset that would let them be continued.

## See also

- `references/budget-tab.md` — the comparison that depends on the two `_budget_vs_actuals_frame`
  properties above.
- `references/cash.md` — what measures the DSO drift, and why the projection is before-financing.
- `references/scheduler-and-alerts.md` — the drift rule the chicken-meal hook fires.
