# Agent upgrade plan — CFO budgeting agent rewrite

Branch: `agent_rewrite`

## Context

A CEO-level review of this app concluded: the **governance** layer is strong and should be
protected, while the **budgeting** layer is half-built. Today the app is an excellent
cost-driver watchtower being presented as a budgeting agent.

What the review found genuinely good, and what this rewrite must not damage:

- **Provenance guards.** [verify_source_url](app/drivers.py#L52) requires the cited page to have
  been fetched in the same turn; [check_sanity_band](app/drivers.py#L71) bounds the value at
  0.2×–5.0×. The model cannot assert a price into the dataset. This is the differentiator.
- **`lock_assumptions` split from `append_observation`** ([drivers.py:276](app/drivers.py#L276)) —
  routine research cannot overwrite the CFO's frozen position.
- **Exposure computed, not assumed.** [driver_sensitivity](app/tools.py#L805) reads
  qty-per-tonne × volume × price off the bill of materials and returns a breakeven shock %.
- **Deterministic detection, narrative-only AI.** [rules.py](app/rules.py) never calls the API;
  [alerts.py](app/alerts.py) degrades to the finding title offline.
- **Variance split into price/volume/mix/joint** that sums exactly
  ([budget.py:29](app/budget.py#L29)).
- The agent loop's correctness work — `pause_turn` continuation, the empty-tool-round break,
  `MODEL_CAPS`/`fallbacks` derivation, the cached/uncached system split. Untouched by this plan.

What the review found bad, and what this rewrite fixes:

| # | Finding | Fix |
|---|---|---|
| 1 | Scenarios hold **volume fixed** and move revenue only as cost pass-through — no volume, price or mix assumption is expressible | Phase 1 ✅ |
| 2 | Opex is a blanket %; `opex_plan.csv`'s `driver_id` column drives nothing | Phase 1 ✅ |
| 3 | **No export** of any kind — the budget cannot reach a board pack or an ERP | Phase 2 ✅ |
| 4 | **Two budget models for two different companies** (Budget Outlook = "Northwind Interiors"; the rest = the feed business) | Phase 3 ✅ |
| 5 | No approval state, no version diff, no audit of what a re-lock was worth | Phase 2 ✅ |
| 6 | The CFO **cannot lock from the UI** — `lock_assumptions` is a model-only tool | Phase 2 ✅ |
| 7 | Budgets are built on **today's spot**; `project_series` is extrapolation dressed as foresight | Phase 3 ✅ (tool deleted in Phase 4 ✅) |
| 8 | No cash / working capital | Phase 5 ✅ |

**Correction to the review:** it also asked for the tool-call debug blocks to be put behind a
flag. They already are — [app.js:199](static/app.js#L199) returns early unless
`settings.show_debug`, which defaults to `false` in [config.py:27](app/config.py#L27). No work
required.

**Originally deferred, now built:** cash, working capital and capex. The deferral reason was that
DSO/DPO/DIO and capex inputs had no home in the current data model — Phase 5 gives them one, and the
answer to "where do the days come from" turned out to be a dataset to measure them off rather than
the profile fields the deferral assumed. See Phase 5.

**Delivery:** five phases, each ending with a green `.venv/bin/python -m pytest -q`.

---

## Phase 1 — Make the scenario engine a budget engine ✅ COMPLETE

**Status: done.** All four blocks land, `normalise_assumptions` lifts on read and on write, the opex
bridge drives off `opex_plan`'s `driver_id`, and the EBITDA bridge closes to the cent. 305 tests
green (`tests/test_scenario_engine.py` +21 cases, new `tests/test_opex_bridge.py` +16).
`rules._rerun_active_scenario` now carries the stored volume/price/opex blocks and replaces only the
drivers block with what the market did. `CLAUDE.md` gained a section on the four blocks, the τ rule,
the application order and the weighted-rate design note. Deviation from plan: none of substance —
`build_budget_scenario` accepts the blocks *either* nested inside `assumptions` or as the three
sibling arguments, since the flat form is what every stored scenario and prompt example uses.

The core defect: [project_pnl](app/budget.py#L274) carries `volume_tonnes` through unchanged and
sets `d_revenue = tau * d_cogs`, so revenue can only move as a consequence of cost.
[build_budget_scenario](app/tools.py#L904) then rejects any assumption key that is not a
`driver_id`. Volume and price — the first two conversations in any budget round — are
unrepresentable.

### 1.1 A structured assumption set

Assumptions become four blocks instead of one flat dict:

```python
{
  "drivers": {driver_id:     pct},   # existing behaviour
  "volume":  {product_line:  pct},   # NEW — "*" applies to all lines
  "price":   {product_line:  pct},   # NEW — the CFO's pricing decision
  "opex":    {driver_id | cost_centre: pct},   # NEW — replaces the blanket %
}
```

Add `budget.normalise_assumptions(raw)` — a pure function that lifts a legacy flat
`{driver_id: pct}` dict into `{"drivers": …}`. Every stored scenario in `data/scenarios.json`
keeps working with no migration, and the tool schema accepts both shapes.

### 1.2 Application order in `project_pnl` — documented and tested

Per baseline row, in this order:

1. **Volume.** `volume' = volume × (1 + v/100)`. Revenue and COGS scale with it (both variable
   in tonnes). Opex does **not** scale with volume — it is period cost.
2. **Driver cost** deltas computed through the BOM on `volume'` (not the original volume),
   × `(1 − hedge_coverage)`. Unchanged in spirit from today; only the volume base moves.
3. **Explicit price.** `revenue' += revenue_after_volume × (p/100)`.
4. **Pass-through τ applies only to lines with no explicit price assumption.** This is the one
   subtle decision in the phase: τ and an explicit price are two ways of saying the same thing,
   and applying both double-counts recovery. τ becomes "automatic recovery where the CFO has not
   priced deliberately". A `"*"` price entry counts as explicit for every line.
5. **Opex** via the bridge below.

`ebitda = revenue − cogs − opex` stays a per-row invariant, and the all-zero assumption set must
still reproduce the baseline exactly — the property
[tests/test_scenario_engine.py](tests/test_scenario_engine.py) asserts first.

### 1.3 The opex bridge — fixing the dead `driver_id` column

New pure function in [app/budget.py](app/budget.py):

```python
def opex_bridge(opex_rows, driver_assumptions, opex_overrides, default_pct) -> dict
```

Each `opex_plan` cost centre moves by its explicit override, else by its linked driver's
percentage, else by `default_pct`. Returns per-cost-centre deltas **and** a single weighted
growth rate.

**Design note worth keeping:** `opex_plan.csv` is cut by cost centre, while `project_pnl`'s
baseline opex comes from `budget_vs_actuals` cut by product line. These two totals need not
reconcile. So the bridge is used **only to derive the weighted rate**, which is then applied to
the `budget_vs_actuals` opex level. That sidesteps a reconciliation that would otherwise have to
be invented, and keeps the baseline identity exact. The per-cost-centre detail is still returned
so the agent can say "Production opex +€412k, all of it wage inflation."

Wage inflation is on the watchlist today and reaches the budget as a hand-typed number; after
this it flows through the same driver machinery as chicken meal.

### 1.4 Tool surface

[tools.py::build_budget_scenario](app/tools.py#L904) gains `volume`, `price` and `opex` objects
in its `input_schema`. Validation reuses the existing teachable-error helpers: `_unknown()`
against the driver catalogue for `drivers`/`opex`, and against `product_lines` for
`volume`/`price`. `opex_inflation_pct` is retained as the default for unmapped centres.

Returned `vs_baseline` gains `volume_delta_tonnes` and a `bridge` list attributing the EBITDA
move across volume / price / each driver / opex — the shape `render_chart`'s waterfall already
consumes.

### Files
`app/budget.py`, `app/tools.py`, `app/scenarios.py` (store the four blocks),
`tests/test_scenario_engine.py`, new `tests/test_opex_bridge.py`.

### Tests
All-zero reproduces baseline · volume-only +10% scales revenue and COGS but not opex and leaves
gross-margin % unchanged · price-only +4% moves revenue only · explicit price suppresses τ on
that line · opex bridge moves only the centres mapped to the shocked driver · the EBITDA bridge
sums to the total EBITDA delta (the same additivity discipline as `check_residual`).

---

## Phase 2 — Versioning, CFO-facing lock and approve, export ✅ COMPLETE

**Status: done.** `app/budgetversions.py` and `app/export.py` land, both with the store/pure-leaf
discipline the plan asked for; the lock, submit, approve, diff and export routes are all gated; the
UI carries a lock form, a version rail and export buttons. 340 tests green (`tests/test_budget_versions.py`
+18, `tests/test_export.py` +17). `CLAUDE.md` gained "A scenario is explored; a version is committed"
and the three-escapes rule. Asset links bumped to `?v=21`.

Deviations from plan, all deliberate:

1. **The lock and approve UI is not on `#/budget`.** `#/budget` is still Budget Outlook, which reads
   no dataset and carries its own gate — properties Phase 3 supersedes and Phase 2 does not. Putting
   a driver-lock form there now would break them a phase early. Instead: **Lock assumptions** is a
   panel on `#/drivers`, where the locked values, the drift and the provenance already live, and the
   **version rail with Submit / Approve / What changed / Export** is on `#/scenarios`, next to the
   scenarios versions are made out of. Phase 3 can move both under the unified Budget tab.
2. **`diff` gained a third cut, `locked_values`.** Per-driver assumption deltas and per-line P&L
   deltas answer "what did we change"; neither answers "what was the re-lock worth", because a
   percentage can hold still while the locked price under it moves. That cut is the one that
   attributes the €2.4M swing to chicken meal on 12 Nov, which is what 2.1 said the diff was for.
3. **An approved version cannot be deleted**, and at most one is approved at a time (approving
   another supersedes it). History that can be deleted is not an audit trail.
4. **Adjacent security fix.** `escapeHtml` does not escape quotes, and eight existing sites
   interpolated model-supplied text straight into `value="…"` attributes — enough for
   `" onfocus="…` without ever needing a `<`. Added `escapeAttr` and moved every attribute site onto
   it, new and pre-existing. Documented in CLAUDE.md as three escapes by position.

### 2.1 Budget versions

New `app/budgetversions.py`, following the existing store pattern exactly — module-level
`VERSIONS_FILE` constant (so tests can `monkeypatch.setattr` it at a `tmp_path`), `.tmp` write
then `Path.replace()` under a `threading.Lock`.

Record: `{id, version_no, label, scenario_id, assumptions_snapshot, totals, status, note,
parent_version_id, created_at, created_by, approved_by, approved_at}` with
`status ∈ draft | submitted | approved | superseded`.

`diff(version_a, version_b)` is a **pure** function returning per-driver assumption deltas,
per-line P&L deltas and the EBITDA bridge between versions. This is what answers "the €2.4M
swing came from re-locking chicken meal on 12 Nov". Pure, therefore tested.

### 2.2 A lock and approve the CFO can actually press

- `POST /api/assumptions/lock` — a thin, gated route that calls the **existing**
  [drivers.lock_assumptions](app/drivers.py#L276). The model-side path and its guard are
  unchanged; the CFO simply gets a second door to the same function.
- `POST /api/budget/versions/{id}/submit` and `/approve`.
- Frontend: a **Lock assumptions** form and an **Approve** button behind a confirm, on `#/budget`.

**Honest limitation to state in the UI copy:** this app has no authentication. `approved_by` is
a required free-text field seeded from the company profile. It is an *attestation*, not an
authenticated signature, and the plan should not pretend otherwise.

### 2.3 Export

New `app/export.py`:

- `workbook(record) -> bytes` via **openpyxl** (add to `requirements.txt`). Sheets: **Summary** ·
  **Monthly P&L** · **By product line** · **Assumptions** · **Driver bridge** · **Version diff**.
- `board_pack_markdown(record) -> str`, reusing the stored narrative.

The **Assumptions** sheet carries `value, unit, source_url, retrieved_at, locked_at, rationale`
per driver. This is nearly free — the data already carries `source_url` and `recorded_at` — and
it is the reason to export from this tool rather than from a spreadsheet: the assumptions arrive
in the board pack with their provenance attached.

Routes: `GET /api/scenarios/{id}/export.xlsx`, `GET /api/scenarios/{id}/export.md`, and the same
two under `/api/budget/versions/{id}/`. All `dependencies=Gated`. Export buttons on each scenario
card and on the version rail.

### Files
New `app/budgetversions.py`, `app/export.py`; `app/main.py` (routes), `requirements.txt`,
`static/app.js`, `static/style.css`, `static/index.html`; new `tests/test_budget_versions.py`,
`tests/test_export.py`.

---

## Phase 3 — One budget model, and forward curves instead of spot ✅ COMPLETE

**Status: done.** `driver_forwards` lands behind the existing guards, `basis` reaches
`build_budget_scenario`, and the Budget tab now reads the feed business through the bill of
materials instead of shipping a second company. 369 tests green (new
`tests/test_forward_curve.py` +26, `tests/test_budget_plan.py` +12 and −6 for the deleted chat
layer). `CLAUDE.md`'s "Budget Outlook — the one feature that stands apart" is rewritten as "The
Budget tab — one engine, two input modes" and says outright that it reverses itself. Asset links
bumped to `?v=22`.

Deviations from plan, all deliberate:

1. **`record_driver_forward` takes a LIST of points, not one figure.** The plan said "mirroring
   `record_driver_observation`'s schema"; a curve is read off one page in one go, and twelve calls
   would mean twelve provenance checks against the same URL and eleven chances for the model to
   drift onto a different page mid-curve. The *guards* are mirrored exactly — same two functions,
   applied per point — and validation is all-or-nothing so a curve with one bad month writes
   nothing.
2. **`spot` basis is measured from the locked price, not repriced onto itself.** The obvious reading
   of "basis" — swap the baseline price — makes an all-zero `spot` scenario a no-op, which is
   exactly wrong: "what has the market already done to the budget" is the most useful thing that
   basis can say. So `driver_prices` stays the locked baseline every delta is measured from, and the
   basis only chooses the price the percentages *apply* to. That also makes the three bases directly
   comparable, and makes `driver_prices_by_month` a pure extension — omit it and the arithmetic is
   unchanged, which `test_forward_curve.py` asserts first.
3. **BOM mode materialises `variables[]` rather than running a second engine.** "One engine in two
   input modes" is implemented as: the datasets produce variables in exactly the shape simple mode
   produces them, and `derive()` is untouched. The cost base reconciles to COGS + opex through a
   **conversion residual**, and opex cost centres are sized by their *share* of the opex plan applied
   to the P&L's opex level — the same two-cuts problem Phase 1 solved the same scale-free way.
4. **The Budget page's chat became a hand-off, not a re-implementation.** The plan said chat goes
   through `reporting.run_session_turn`; rather than build a second thread view on `#/budget`, every
   "ask" affordance now routes through `askFromBudget()` into Ask — the path alerts, drivers and
   scenarios already use — and the agent reads the page back through a new **`budget_outlook`** tool.
   That is also what keeps the page working before setup: the sessions API is gated, so the rail says
   so instead of offering a button that 409s.
5. **Prompt rules 11–13 added now, not deferred to Phase 4.** Two new tools and a new `basis`
   argument with no guidance would have shipped a capability the model never reaches for. Phase 4
   still owns deleting `project_series`, rewriting rule 3 and updating the report prompts.
6. **`basis` is stored on the scenario, frozen onto the version and printed in both exports.** A
   scenario whose basis is not stated cannot be defended — the same percentages on a forward curve
   and on locked values are two different budgets.

Not done here, and left to Phase 4 (where all four landed): `project_series` still exists, the Market
scan pill is still in the nav, `driver_stale`/`scenario_ebitda_floor` still fire, and
`WEEKLY_PROMPT`/`MONTHLY_PROMPT` are unchanged.

### 3.1 Forward curves replace extrapolation

- New `data/driver_forwards.parquet` — append-only and revisioned, the same shape discipline as
  `driver_prices`: `{driver_id, quote_month, price, curve_date, source_url, recorded_at, revision}`.
- `drivers.append_forward()` reuses **the existing guards unchanged** — `verify_source_url`
  against this turn's fetched URLs, `check_sanity_band` against the latest spot. The trust
  boundary widens to a second dataset without a second implementation.
- New tool `record_driver_forward`, mirroring `record_driver_observation`'s schema and its
  "you must have fetched this page in this turn" description.
- `build_budget_scenario` gains **`basis: "locked" | "spot" | "forward"`** (default `locked`).
  On `forward`, `project_pnl` takes an optional `driver_prices_by_month` and the budget acquires
  genuine per-month price shape — seasonality falls out of the curve instead of being assumed.
- [driver_status](app/drivers.py#L324) gains `forward_12m` and `forward_vs_lock_pct`.

This is the substantive answer to "it doesn't predict the market": it stops trying to, and reads
the curve the market publishes.

### 3.2 Collapse to one budget model

Budget Outlook's engine is good — `derive()`, `CATALOGUE`, the validation and
`templated_narrative()` all stay. What goes is its **separateness**.

- `#/budget` becomes the single Budget tab, with one engine in two input modes: **BOM mode**
  when `bill_of_materials` + `budget_vs_actuals` exist, **simple mode** otherwise.
- Each entry in `budget_plan.json`'s `variables[]` gains an optional **`driver_id`**. The moment
  a watchlist exists, simple-mode cost lines link to drivers and inherit locking, provenance,
  versioning and export. This is the migration path, and it is what makes the two one model
  rather than two models behind one tab.
- **Delete `budgetplan.stream_chat` and `/api/budgetplan/chat*`.** Chat goes through
  `reporting.run_session_turn` like everything else.
- Seed `data/budget_plan.json` from [generate_data.py](app/generate_data.py) using the feed
  business, so the demo stops shipping two unrelated companies.

**Note for the implementer:** [CLAUDE.md](CLAUDE.md) currently documents Budget Outlook's
independence — its own gate, its own chat loop, "not a fourth `KIND_META` entry" — as
load-bearing. That rationale is *deliberately superseded* here: it held while the feature was
tool-less and citation-less, and stops holding once simple mode needs locking, export and driver
links. Update CLAUDE.md in the same commit rather than leaving the two in contradiction.

### Files
`app/drivers.py`, `app/tools.py`, `app/budget.py`, `app/budgetplan.py`, `app/main.py`,
`app/generate_data.py`, `static/budget.js`, `static/app.js`, `CLAUDE.md`; new
`tests/test_forward_curve.py`.

---

## Phase 4 — Removals, prompts, navigation ✅ COMPLETE

**Status: done.** `project_series` is gone, Market scan has lost its pill and reads as the "This
week" strip on Drivers, the two noise rules are removed, and the prompts cover forward curves, the
assumption blocks and the version diff. 366 tests green (`tests/test_rules.py` −7 for the deleted
rules, +4 for the re-run and an assertion that the removals stay removed). `CLAUDE.md` gained the
no-extrapolation rule under "Conventions in `tools.py`", the state-vs-event rationale under
"Autonomy", and the weekly merge alongside the monthly one. Demo data regenerated. Asset links
bumped to `?v=23`.

Deviations from plan, all deliberate:

1. **Prompt rule 3 is a prohibition, not a restriction.** The plan said "rewrite rule 3 around
   forward curves". With the tool deleted, the rule that mattered was not "use the curve instead" but
   "you have no projection tool and must not become one in prose" — no run-rates, no "at this rate",
   no annualising three months. Deleting the tool without that rule would have moved extrapolation
   from a tool call, where it was at least labelled, into unlabelled prose.
2. **A new rule 12 for the assumption blocks, pushing `basis` to 13 and `budget_outlook` to 14.**
   Renumbering invalidates the cached prompt prefix once, which is expected on any prompt edit, and
   reading order is worth more than a one-off cache miss: the blocks and the basis are the same
   conversation.
3. **The "This week" strip carries a Run-a-scan button.** With `#/weekly` redirecting, the "Generate
   this week's report" button inside `renderFeature`'s placeholder became unreachable, and the only
   remaining way to get a scan would have been the scheduler. The strip's empty state and its action
   row both call the same `apiGenerateReport("weekly")` the placeholder did.
4. **The strip loads once per visit, and pauses while a scan runs.** `refreshDrivers` repaints every
   2 s during a verify run; polling the scan on that tick would be two wasted requests a second for a
   headline written at 03:00, and repainting mid-scan would put an idle label back on a running
   button. Same guard as `renderLockPanel`'s `lockFormOpen`.
5. **`_rerun_active_scenario` survives its rule.** `scenario_ebitda_floor` was the noise; the
   arithmetic under it is what `main.budget_state` puts on the home screen. It keeps its tests, now
   testing the function directly rather than the deleted rule — a home screen quoting a wrong number
   is worse than a noisy alert.

### The work

1. **Delete `project_series`** — the tool, its schema, `PROJECTABLE`
   ([tools.py:91](app/tools.py#L91)) and system-prompt rule 3. Superseded by 3.1.
2. **Market scan loses its nav pill.** Mirror the proven `monthly → scenarios` merge: add
   `weekly: "drivers"` to `NAV_ALIAS` ([app.js:1432](static/app.js#L1432)). The `weekly` session
   kind, the scheduled task and `#/weekly/{id}` transcripts are all untouched — only the pill
   goes. Drivers gains a "This week" strip showing the latest scan's headline and the drifted
   drivers, linking through. The report becomes the alert instead of a second artifact.
3. **Two alert rules removed** from `DEFAULT_RULES` and `DEDUP` in
   [rules.py:28](app/rules.py#L28): `driver_stale` and `scenario_ebitda_floor`. Both fire
   constantly and train the CFO to mute the other three. Staleness remains fully visible as a
   *state* on the Drivers page — it is a UI condition, not an event. Update
   [tests/test_rules.py](tests/test_rules.py).
4. **Prompts** in [agent.py](app/agent.py#L95): rewrite rule 3 around forward curves; add a rule
   that volume, price and opex assumptions belong in `build_budget_scenario` and must never be
   asserted in prose; update `WEEKLY_PROMPT` and `MONTHLY_PROMPT` to cover the version diff and
   the forward basis. Rules 1, 2 and 6 (traceability, never do arithmetic, no payroll dataset)
   stay verbatim — they are why the output is trustworthy.
5. **Regenerate demo data** — `.venv/bin/python -m app.generate_data` now also writes
   `driver_forwards` and the seeded `budget_plan.json`.
6. **Bump `?v=N` on all five asset links** in [static/index.html](static/index.html)
   (`columbus-tokens.css`, `style.css`, `budget.css`, `budget.js`, `app.js`) — together, per the
   repo rule. A half-bumped set is worse than none.

---

## Phase 5 — Cash ✅ COMPLETE

**Status: done**, and no longer deferred. `app/cash.py` lands as a fourth pure leaf,
`working_capital` and `capex_plan` as two new read-only datasets, `working_capital` as a new profile
block, `cash_flow_projection` as a new tool with a `GET /api/cash` route onto it, and a cash strip on
the Budget page. 406 tests green (new `tests/test_cash_flow.py` +33, `tests/test_export.py` +7).
`CLAUDE.md` gained "EBITDA is not cash" plus the cash hooks under Demo data. Asset links bumped to
`?v=26`.

What the phase was scoped as, and all four landed: **working-capital days (DSO/DPO/DIO)**, **capex**,
**a monthly cash-flow projection off the scenario's EBITDA**, and **a cash line on the budget page**,
on a new dataset and new profile inputs.

Deviations from the one-paragraph plan, all deliberate:

1. **The days are MEASURED, not entered.** The plan said "needs DSO/DPO/DIO inputs", which reads as
   three profile fields. Three profile fields would have been an assumption with no source — the
   exact thing the rest of this app exists to refuse. So `working_capital.parquet` carries monthly
   receivables, inventory, payables and cash *balances*, `cash.working_capital_days` measures the days
   off them (average balance × Σdays ÷ Σflow over a trailing window), and the profile fields are
   *overrides* that get labelled as decisions wherever they appear (`days.basis`). The same move as
   `driver_exposure` reading qty-per-tonne off the bill of materials instead of guessing an
   elasticity. A company with no balance-sheet history can still plan cash by stating its terms — the
   dataset makes the days measured, it does not gate them.
2. **`cash.py` rather than a fifth section of `budget.py`.** CLAUDE.md says to keep new arithmetic in
   `budget.py`, and this respects the intent (pure, no I/O, no app imports — it imports nothing at
   all) while keeping a separate vocabulary separate: days, balances, capex and opening cash are not
   assumptions about next year's trading, and `budget.py`'s docstring enumerates exactly four things.
3. **No cash alert rule.** Phase 4's state-vs-event rule applies directly: a cash trough below the
   floor *stays* below it until someone acts, so a rule would re-fire the same finding every window
   and train the CFO to mute the other three. The breach is a badge, a metric card and a `⚠` line in
   the board pack — a screen, not an alert.
4. **Tax, interest and depreciation are not modelled, and the output says so in three places.** There
   is no dataset for any of them, so what comes out is free cash flow *before financing* — never net
   income, never a bank balance. `caveats` carries it, the workbook prints it next to the figures, and
   prompt rule 15 forbids presenting it otherwise. A cash tax rate is applied only if the CFO states
   one; `None` deducts nothing and says so, because a guessed rate moves a trough further than most of
   the assumptions this app guards.
5. **Capex `proposed` vs `committed` is the one lever, and the demo makes it fail honestly.** The
   seeded plan puts the committed extruder line in February and April and the two proposed projects
   in September and November, so `include_proposed_capex: false` does NOT fix the April trough —
   collections do. A lever that always works teaches nothing; "the capex you can defer is after the
   problem" is the answer a CFO can act on.
6. **Scope reached slightly past the plan, in two places that would have been half-features
   otherwise.** The workbook gained a **Cash flow** sheet and the board pack a **Cash** section (a
   cash plan nobody can export is not a cash plan), and a version now freezes `cash_snapshot`
   alongside its driver provenance — the days were measured on the day the board approved it, so next
   quarter's measurement must not rewrite the funding case. Both are optional and backwards
   compatible: an older version carries `None` and the sheet says which inputs are missing.

### The work, as built

- **`app/cash.py`** — `working_capital_days` (measured DSO/DIO/DPO + closing balances),
  `project_cashflow` (monthly balances → working-capital swing → tax → capex → free cash flow →
  closing cash, with the trough and any floor breach), `cash_bridge` (waterfall points for
  `render_chart`). Both invariants are asserted: Σ FCF == closing − opening, and Σ working-capital
  swing == closing balances − opening balances.
- **Datasets** — `working_capital` (closed months only: a balance sheet is something that happened)
  and `capex_plan` (project, category, status, budget vs actual), both in `DATASETS` so
  `query_budget_data` reaches them, neither model-writable.
- **Profile** — `working_capital` block, every field defaulting to `None` = *not stated*, validated
  and clamped in `profile._clean_working_capital`; `POST /api/profile/working-capital`, ungated.
- **Tool** — `cash_flow_projection`, resolving each input argument → profile → measured, plus system
  prompt rule 15 and a cash paragraph in `MONTHLY_PROMPT`.
- **Route** — `GET /api/cash`, gated, thin onto the tool, returning `available: false` with a reason
  rather than a 4xx.
- **UI** — `#bo-cash` on `#/budget`: free cash flow, the low point against the floor, the cash
  conversion cycle with its basis, twelve bars of closing cash with the floor drawn across them, the
  four bridge steps, a defer-proposed-capex toggle, an ask hand-off, and the cash-assumptions form.

### Still not modelled

Interest, debt schedules, depreciation, dividends and deferred tax. Each needs a dataset this app
does not have, and the projection names the gap instead of filling it. That is the honest boundary,
not an oversight: the next phase to do cash properly would start with a financing dataset.

---

## Phase 6 — The CFO can correct a scenario ✅ COMPLETE

**The gap:** every scenario had to be built, and rebuilt, through the agent. Nudging one percentage
meant a conversation, and the only way to "edit" a scenario was to describe a new one — which left the
old one behind and moved the numbers for reasons buried in a transcript. `app/scenarios.py`'s header
said this was deliberate ("there is deliberately no scenario-authoring form anywhere in the app"), and
it was right about **creation** and wrong about **correction**.

**Status: done.** 425 tests green (new `tests/test_scenario_store.py` +8, `test_budget_versions.py` +4,
`test_scenario_engine.py` +8). Asset links bumped to `?v=29`.

What landed, and the one rule it all follows — *a second door onto one implementation, never a second
implementation*:

- **`tools.build_budget_scenario` gained keyword-only `replace_scenario_id`.** No new arithmetic: the
  same validation, the same `_basis_prices`, the same single `project_pnl` call, and
  `scenarios.save_scenario`'s latent replace-by-id path finally load-bearing. Three guards make it
  safe — an existence check (`save_scenario` upserts, so a stale id would mint a ghost record), `active`
  asserted-never-denied (an absent key inherits the stored flag), and the "needs at least one
  assumption" refusal demoted to a *creation* guard, because the seeded "2027 budget (as locked)"
  legitimately has none and renaming it is a real edit.
- **`build_budget_scenario_tool` is the model's narrow door.** Leaving the argument out of the schema
  was not enough: the dispatcher splats input with `**i` and no schema sets
  `additionalProperties: false`, so a hallucinated `replace_scenario_id` would have been honoured. Now
  it is unrepresentable and gets a teachable "Invalid arguments" instead.
- **`budget.repriced_drivers`** — a recompute re-reads today's locked prices, so EBITDA can move with
  no percentage touched. This names the drivers responsible, with a **relative** threshold, because a
  spot fallback re-read as a rounded locked value differs in the fifth decimal and every USD driver
  inherits that through FX: reported literally, an edit that changed nothing named fourteen drivers at
  "+0.0%".
- **`budgetversions.approved_freeze` / `approved_freeze_of`** — which scenario the approved version was
  frozen from, computed where the knowledge lives. Draft and submitted versions freeze nothing;
  superseding hands the older scenario back; at most one approved version means at most one frozen
  scenario, so the shape is one record or `None`.
- **Routes** — `PUT /api/scenarios/{id}` (404 → 409 if frozen → the tool), `GET /api/scenario-options`
  (flat path, because `/api/scenarios/{scenario_id}` would swallow `/api/scenarios/options`), and
  `frozen` added to the `GET /api/scenarios` envelope rather than to `scenarios.summary`, whose shape
  belongs to that module.
- **UI** — `#scenario-edit`, a *sibling* of the two list hosts so a repaint cannot wipe a half-typed
  form, with add/remove rows per block, duplicate keys made unrepresentable by rebuilding the sibling
  selects, a blank percentage refused inline (never coerced to `0`, which means "hold this
  deliberately"), and a stored key the datasets no longer carry kept and marked rather than silently
  dropped. Frozen cards show `Frozen — approved as vN` with a muted rule, never the 4 px accent.

Three latent CSS bugs fell out of it and were fixed in the same pass, all the same bug: this project
scopes `.hidden` per component and ships no global rule, so `.driver-history.hidden` (**the History
button never collapsed** — the reported symptom), `.error-text.hidden` (a cleared error stayed on
screen) and `#scenario-edit-form.hidden` had no rule behind the class the JS was toggling.

**Manual checklist:**

13. On `#/drivers`, the button reads **Edit assumptions**; History expands, and clicking it again
    collapses it with the label flipping to "Hide history".
14. Edit an alternative scenario's chicken-meal percentage and save — EBITDA moves on the card, the
    scenario keeps its place, and `data/scenarios.json` shows the same id and `created_at`.
15. Edit the **active** scenario and confirm it is still active afterwards.
16. Freeze it as a version and approve it: the card becomes `Frozen — approved as v1` with no Edit
    button, and a `PUT` to it returns 409. Approve a version from another scenario and the first
    becomes editable again.

---

## Phase 7 — Two model tiers ✅ COMPLETE

**The gap:** every turn in the app ran on `claude-opus-5`, and nothing else was selectable.
`AVAILABLE_MODELS` derived the picker from `caps["fallbacks"]`, which excluded `claude-sonnet-5`
because the loop once sent `betas` + `fallbacks` unconditionally and a model that cannot accept them
400s on the first turn. By the time of this phase the request builder already gated those two lines
on the cap — the exact condition the comment above the derivation named before widening the list —
so the exclusion had outlived its reason. Meanwhile every alert narrative, Budget-page read, driver
check and chat turn paid Opus rates for work that does not need Opus depth.

**What landed.** `AVAILABLE_MODELS` now derives from what the loop needs of *every* model (the two
web tools, adaptive thinking, effort), and the model-dependent request fields moved into one pure
`agent.model_request_fields`, which is what makes that derivation safe rather than lucky. Settings
carries two slots — `models.heavy` (default `claude-opus-5`) and `models.general` (default
`claude-sonnet-5`) — with a read-side migration off the flat `model` key. `reporting.model_for_session`
picks between them off the session: a `weekly`/`monthly` session **with no `parent_id`** is heavy,
everything else general. The `parent_id` half is load-bearing and was a real bug caught in review —
report follow-ups are created with the *report's* kind, so keying on kind alone put every question
anyone asked about a report onto the expensive model.

**Status: done.** 496 tests green (new `tests/test_model_tiers.py` +21, `test_agent_config.py` net +3,
and the cached-prefix floor raised 512 → 1024 because Sonnet 5's minimum cacheable prefix is twice
Opus 5's). Asset links bumped to `?v=36`.

---

## Verification

**Automated**

```bash
.venv/bin/python -m pytest -q                                    # all files green
.venv/bin/python -m pytest tests/test_scenario_engine.py -q      # Phase 1
.venv/bin/python -m pytest tests/test_driver_guards.py -q        # guards still pass, unchanged
.venv/bin/python -m pytest tests/test_cash_flow.py -q            # Phase 5
.venv/bin/python -m pytest tests/test_scenario_store.py -q       # Phase 6
```

**End to end**

```bash
.venv/bin/python -m app.generate_data
.venv/bin/uvicorn app.main:app --port 8323 --reload
curl -s http://127.0.0.1:8323/api/profile
curl -s http://127.0.0.1:8323/api/budget/versions
curl -sI http://127.0.0.1:8323/api/scenarios/<id>/export.xlsx    # 200 + xlsx content-type
```

**Manual checklist** — at http://127.0.0.1:8323/

1. Ask: *"what if Aqua Feed volume drops 15% and we take list price up 4%?"* — a scenario builds
   with both effects, and the EBITDA bridge separates volume, price and driver contributions.
2. Ask for a wage-inflation shock — the opex bridge names the affected cost centres, and no
   figure appears for wage *rates* (there is still no payroll dataset; the model must say so).
3. Export the active scenario — the workbook opens and the Assumptions sheet carries a source URL
   and a retrieval date on every driver.
4. Lock assumptions from the UI, approve a version, then diff it against the prior version — the
   EBITDA delta is attributed to the assumptions that changed.
5. Build a scenario on `basis: "forward"` — monthly COGS varies with the curve, not flat.
6. Confirm `project_series` is gone and the model reaches for the forward curve instead.
7. Confirm the Market scan pill is gone, Drivers shows the "This week" strip, and an existing
   `#/weekly/{id}` transcript still opens.
8. Confirm the Budget tab shows one budget for one company, and Budget Outlook chat still works
   through the main agent loop.
9. On `#/budget`, the cash strip names the month cash is lowest and whether it clears the floor, and
   the cash conversion cycle says whether its days were measured or stated. Open **Cash assumptions**,
   type a DSO, save — the low point moves and the basis flips to "measured, with 1 you stated". Clear
   the field again and it returns to measured.
10. Tick **Defer the capex still marked proposed** — free cash flow improves and the April trough does
    not, because the proposed projects are in September and November. That is the point.
11. Ask: *"cash bottoms out in April — what fixes it?"* — the agent runs `cash_flow_projection`, says
    whether the days were measured or stated, and does not present the closing balance as a bank
    balance.
12. Export the active scenario — the workbook has a **Cash flow** sheet carrying the days, their
    basis, the capital plan project by project and the before-financing caveat.

---

## Risks

- **The τ / explicit-price interaction (1.2 step 4)** is the one place a plausible implementation
  double-counts price recovery and quietly overstates EBITDA. It gets a dedicated test.
- **Assumption-shape compatibility.** `normalise_assumptions` must be applied on read in
  `scenarios.py` as well as in the tool, or existing saved scenarios read as empty and silently
  project the baseline.
- **Two opex cuts.** The weighted-rate approach (1.3) is chosen precisely to avoid an invented
  reconciliation; do not "improve" it into a direct substitution without handling the mismatch.
- **CLAUDE.md contradiction.** Phase 3 overturns documented rationale. Update the doc in the same
  commit.
- **Cash read as a bank balance.** The highest-consequence risk in Phase 5, and it is a
  *communication* risk rather than an arithmetic one: the projection is before financing, and a board
  reading a closing balance will not assume that unless told. Hence the caveat in three independent
  places — `caveats`, the workbook, and prompt rule 15 — rather than one.
- **A stated day count masquerading as a measured one.** `days.basis` and `days.stated` exist so the
  two can never be confused; any new surface that prints a day count must print its basis too.
