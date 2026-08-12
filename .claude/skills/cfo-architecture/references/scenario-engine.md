# The scenario engine

Governs `app/budget.py` (pure maths), `app/scenarios.py` (the store), and the
`build_budget_scenario` / `scenario_options` tools in `app/tools.py`. Tested by
`tests/test_scenario_engine.py`, `tests/test_scenario_store.py`, `tests/test_opex_bridge.py`,
`tests/test_forward_curve.py`, `tests/test_sensitivity.py` and
`tests/test_variance_decomposition.py`.

## It speaks in four assumption blocks

`build_budget_scenario` / `budget.project_pnl` take `{"drivers": {driver_id: pct}, "volume":
{product_line: pct}, "price": {product_line: pct}, "opex": {cost_centre | driver_id: pct}}` —
volume and price are the first two conversations in any budget round, so they are first-class
rather than reachable only as a consequence of cost. `"*"` is the wildcard in `volume`/`price`.

Five things there are load-bearing:

- **`budget.normalise_assumptions` runs on read as well as on write** — in `scenarios.list_scenarios`
  and `scenarios.summary`, not just in the tool. Every scenario in `data/scenarios.json` predating
  the blocks is a flat `{driver_id: pct}` dict; without the read-side lift they parse as an empty
  drivers block and silently project the baseline, which looks like a re-run that worked. The two
  shapes are told apart by the **value** being a dict, never by the key, so a driver named `price`
  cannot flip the detection.
- **Pass-through τ applies only to lines with no explicit `price` entry.** τ and a stated price are
  the same lever — "we recover cost in price" — and applying both double-counts recovery and
  overstates EBITDA. An explicit `0` counts as deliberate (a decision to hold price).
- **Application order** is volume → driver cost on the *post-volume* tonnage → explicit price → τ →
  opex. Opex never scales with volume; it is period cost.
- **`opex_bridge` returns a weighted rate, not a substitute total.** `opex_plan` is cut by cost
  centre, `project_pnl`'s baseline opex by product line, and the two need not reconcile — only the
  scale-free rate crosses over, with per-centre detail returned alongside for the narrative. Do not
  "improve" this into a direct substitution without solving the two-cuts problem first. It is what
  finally makes `opex_plan.csv`'s `driver_id` column mean something: wage inflation now reaches the
  budget through the same machinery as chicken meal.
- **`replace_scenario_id` recomputes in place, and `active` is asserted rather than denied.** It is
  the keyword-only argument behind `PUT /api/scenarios/{id}` — same id, same `active` flag, stored
  projection overwritten, `updated_at` bumped — and three things make it safe. It checks the scenario
  **exists** first, because `save_scenario` upserts and a stale id would otherwise mint a ghost
  record. It passes `active` **only when `make_active` is true**, because `save_scenario` inherits the
  stored flag from an absent key, and passing `False` would quietly take the budget off a scenario the
  CFO was only correcting. And the "needs at least one assumption" refusal became a **creation**
  guard (`and previous is None`): several stored scenarios legitimately carry none — the seeded
  "2027 budget (as locked)" among them — so renaming one or moving its basis is a real edit. The
  argument is **absent from the model's tool schema**, and absence is not enough on its own:
  `execute_tool` splats input with `**i` and no schema sets `additionalProperties: false`, so
  `build_budget_scenario_tool` exists as the narrow door and the dispatcher points at *it*. A
  recompute also re-reads today's locked prices, so `budget.repriced_drivers` names any driver whose
  locked value moved — otherwise EBITDA changes with no percentage touched and nothing says why. Its
  threshold is **relative** (`REPRICE_MIN_PCT`), because a spot fallback re-read as a rounded locked
  value differs in the fifth decimal and every USD driver inherits that from FX: reported literally,
  an edit that changed nothing names fourteen drivers at "+0.0%".

`ebitda_bridge` attributes the EBITDA move across volume / price / each driver / opex and carries a
`check_residual` for the same reason `variance_decomposition` does — a bridge that does not close is
one nobody can defend to a board. Each driver's entry is **net of the τ recovery it triggers**, which
is why the four components sum exactly.

## `basis` — what the percentages sit on top of

`build_budget_scenario` takes `basis: "locked" | "spot" | "forward"` (default `locked`), and it is
the one field that changes what a percentage *means*, so it is stored on the scenario, frozen onto
the version and printed in both exports. A scenario whose basis is not stated cannot be defended:
the same assumption set on a forward curve and on locked values are two different budgets.

The mechanism is one optional argument, `project_pnl(…, driver_prices_by_month={month: {driver_id:
price}})`, and the design rule is that it is a pure **extension**:

- **`driver_prices` stays the baseline the deltas are measured from** — the locked price the budget
  was built on. `driver_prices_by_month` supplies only the price to *apply*. The per-driver delta is
  `(applied × (1 + pct/100) − locked) × qty × volume' × (1 − hedge)`, so a curve move, a stated
  percentage and both together are one subtraction.
- **A month or driver the mapping omits falls back to `driver_prices`**, i.e. no implied move. Omit
  the argument entirely and the arithmetic is byte-for-byte what it was — that property is what lets
  `basis` be a switch rather than a fork, and `tests/test_forward_curve.py` asserts it first.
- `tools._basis_prices` builds the mapping: `locked` → `None`; `spot` → today's EUR observations
  repeated across every budget month (so an all-zero `spot` scenario is a real answer — "what the
  market has already done to the budget" — not a no-op); `forward` → the recorded curve, with
  USD-quoted drivers converted at that month's **eur_usd forward** where one exists, so an FX curve
  and a commodity curve compose. A basis with no data behind it returns a teachable error naming the
  bases that do.

An **edit re-validates the basis**, because it runs the whole pipeline again. A scenario stored on
`forward` whose curve no longer covers the budget year therefore cannot be saved — not even renamed —
until the CFO moves it to `locked` or `spot`, and the form renders that refusal inline next to the
basis select rather than as "could not save". That is the teachable error doing its job on a human;
do not soften it into a silent fallback, or a scenario would quietly change what it measures.

## See also

- `references/drivers-trust-boundary.md` — where the forward curve behind `basis: "forward"` comes
  from, and the guards that let it be trusted.
- `references/versions-and-export.md` — a scenario is explored; a version freezes one, and an
  approved version is the only thing that makes a scenario read-only.
- `references/cash.md` — `project_cashflow` turns a scenario's `by_month` into a cash profile, which
  is why a forward-basis budget produces a genuinely different one.
- `references/api-and-gates.md` — `PUT /api/scenarios/{id}` and `GET /api/scenario-options` are
  second doors onto these same functions.
