# The Budget tab — one engine, two input modes

Governs `app/budgetplan.py`, `static/budget.js`, `static/budget.css` and the `#budget-view` /
`#budget-config-view` shells in `static/index.html`. Tested by `tests/test_budget_plan.py` and
`tests/test_budget_comparison.py`.

`app/budgetplan.py` + `static/budget.js` +
`static/budget.css` + `#budget-view` / `#budget-config-view`.

**This section reverses what it said before Phase 3, deliberately.** The old rationale — reads no
dataset, own chat loop, "not a fourth `KIND_META` entry" — held while the feature was a standalone
config-driven read, and it shipped a second budget for a second company alongside the feed business.
There is one budget now. What survives is the *engine*, not the separateness:

- **`derive()` is still pure and still does the whole page** — ranking, deltas, totals, margin — and
  the modes differ only in where `variables[]` comes from. That is what makes it one model
  rather than several behind one tab, and it is why the whole of `tests/test_budget_plan.py` above the
  BOM section needed no changes.
- **`derive()` takes amounts as well as percentages, and that is a pure extension.** A variable may
  carry `next_amount` and the baseline `revenue_next`; absent, the arithmetic is byte-for-byte what it
  always was, which
  `tests/test_budget_comparison.py` asserts **first**, exactly as
  `test_forward_curve.py` asserts the flat-curve no-op first. It exists because a cost line the
  baseline does not carry has `current_amount == 0`, and `next = current × (1 + pct/100)` cannot
  express a non-zero next amount from a zero current one — so the comparison below would silently
  report zero for it. Patching rows after `derive()` returns, or copying its arithmetic into a second
  function, would give the page two definitions of "delta"; this keeps one.
  `expected_change_pct` is then **`None`, never 0, when the baseline side has no such line** — the same
  rule as `driver_status`'s `forward_12m` and `cash.cash_conversion_pct`, and the reason `_pct_text`
  and the frontend's `pct()` render "new" rather than "+0.0%" next to a delta of millions. Note
  `1000 → 0` stays a defined **−100%**: the line existed and was removed. Only `0 → X` is undefined.
- **BOM mode** (`bom_available()`: `bill_of_materials` + `budget_vs_actuals` both exist and are
  non-empty) materialises every line from the datasets via `materialise_from_datasets()`, each
  carrying the **`driver_id`** it was priced from. That id is the whole migration: a line that names
  a driver inherits its locked value, its provenance, its place in a frozen version and its row in
  the export — none of which needed new machinery. **Simple mode** is the old behaviour, and a
  `driver_id` can still be typed in by hand on `#/budget/config`.
- **The materialised cost base reconciles to the P&L exactly**, and three cuts make that true:
  driver lines valued through `budget.driver_exposure`, a **conversion residual** (COGS minus those
  materials, floored at zero) and opex cost centres sized by their **share** of the opex plan applied
  to the P&L's opex level. That last one is the two-cuts problem again — `opex_plan` is cut by cost
  centre, `budget_vs_actuals` by product line — solved the same scale-free way `opex_bridge` solves
  it. Do not substitute the opex plan's own total; the margin on the hero would stop being the real
  margin, and nothing below it would be defensible.
- **Any two budgets, side by side, as a read-time overlay.** Two selects above the hero
  (`#bo-compare`) pick a baseline and a comparison, and **three selection kinds** reduce to one
  comparable *snapshot* which becomes the `variables[]` of a throwaway plan `derive()` then does the
  arithmetic on: `budget:YYYY` (as booked in `budget_vs_actuals`), `actual:YYYY` (as outturned) and
  `scenario:<id>`. Five things there are load-bearing:
  - **Nothing is persisted.** `budget_plan.json`, `/api/budgetplan` and `tools.budget_outlook()` keep
    reading the CFO's own configured budget, so a comparison cannot corrupt what the agent sees — and
    `validate()`'s `budget_year = current_year + 1` therefore cannot truncate a 2024-vs-2027 pair,
    because the overlay never goes near it. There is a test asserting it never does.
  - **`actual:YYYY` is what keeps the page's own default view expressible.** `actual:<closed year>`
    against `budget:<plan year>` is exactly the pair `materialise_from_datasets` freezes, which makes
    "the comparison reproduces the persisted plan" a real regression test rather than an aspiration —
    it is what stops the two orchestrations over the same leaves drifting apart. It is also the only
    pairing where driver **prices** genuinely move, because a booked budget has no lock of its own
    (see `references/demo-data.md`).
  - **A scenario's driver spend is exact, not re-derived**: `budget.driver_exposure(…, driver_prices_used)
    + driver_impact_eur[did]`. `project_pnl` builds its per-driver delta as
    `(effective − locked) × qty × post-volume tonnage × unhedged` on top of `locked × qty × tonnage`, so
    that sum carries the forward curve, the CFO's percentage and the hedge coverage without this module
    knowing about any of them. Both sides then reconcile to their own COGS + opex exactly, which is the
    property that keeps the margin on the hero the real margin.
  - **The union of cost-line ids, and the gap is quantified rather than merely named.** A line on one
    side only contributes 0 on the other and carries `coverage`, so the page says "not in the 2026
    budget" instead of a −100% collapse. And `_gap_note` states what a missing *region* costs the
    reader: in the seeded data 49% of the default pair's revenue step and 67% of its volume step is
    Adriatic, a market with no 2026 budget at all. Naming it is not enough — a page that reports
    "+13.3%" without that share is not wrong about the arithmetic and badly wrong about the business.
  - **The options travel in every response, including the failures.** Same reason
    `tools.scenario_options` exists next to `build_budget_scenario`'s teachable errors: two derivations
    of "the budgets you may pick" is how a dropdown offers a value the resolver then rejects. It also
    lets a select self-heal after a scenario is deleted in another tab. `compare()` returns
    `available: false` plus a reason for every state that is a state, never a 4xx.
- **Its gate is still its own** (`plan["configured"]`), independent of `setup_complete`, and the
  `/api/budgetplan*` routes are still **ungated** — with one exception, `GET
  /api/budgetplan/comparison`, which is **gated** for the reason `/api/cash` on this page is: it reads
  the datasets and the scenario store. A 409 leaves the selectors unrendered and the page falls back to
  the persisted plan, which is exactly what it rendered before the feature existed. In simple mode
  there is nothing for a profile to gate and nothing to compare, so the bar never draws — the mode
  follows the data, not the gate.
- **Numbers are computed, prose is not.** The model writes only the 2–4 sentence read (cached on
  `fingerprint()`, falling back to `templated_narrative()` when the API is unreachable). A
  **comparison's read is deterministic and ships inline** with the payload — `comparison_narrative()`,
  no network, no model call, no cache to thrash, which is what makes trying a dozen pairs free. It is a
  separate function from `templated_narrative` because that one's sentences are built around a year
  ("the 2027 cost base") and a scenario name produces "the Pet cost breach — forward curve cost base";
  a comparison leads with the difference instead, because that is what the two dropdowns ask. The
  `computed` tag next to "The read" says which one is on screen, and `#bo-regen` hides when there is
  nothing to rewrite.
- **The cash strip follows the RIGHT-hand selection** and is not part of `derive()` at all: it reads
  `/api/cash?scenario_id=` into `#bo-cash`, showing free cash flow, the low
  point against the CFO's floor, the cash conversion cycle with its `basis`, twelve bars of closing
  cash with the floor drawn across them, and the four bridge steps. Two rules in there: the **body and
  the form render into separate hosts** (`#bo-cash-body` / `#bo-cash-form-host`) so toggling "defer
  the proposed capex" cannot wipe half-typed values out of the form — the problem `renderLockPanel`
  solves with `lockFormOpen`, solved here by never re-rendering the form; and **the bars and the floor
  line share one positioning box** (`.bo-cash-bars`), because `bottom: N%` and `height: N%` measured
  against different boxes put a covenant line a few pixels off where the arithmetic put it. An empty
  cash form field means *not stated*, and its placeholder carries the measured figure so the field
  says what leaving it blank does. When the right-hand selection is a **dataset year** there is no
  monthly shape to project from, so the strip stays visible and says so — `cash.scenario_id` is `None`
  with a `reason`, written into `#bo-cash-body` alone so the form's half-typed values survive. An empty
  section would have been the one outcome the CFO could not act on.
- **There is no chat loop here any more.** `stream_chat` and `/api/budgetplan/chat*` are gone. A page
  whose cost lines name real drivers has no business answering questions about them without the
  tools, so every "ask" affordance hands off through `askFromBudget()` in app.js — the same
  `pendingHomeMessage` + `goHome()` path an alert, a driver card and a scenario card already use. The
  agent reads the page back through the **`budget_outlook`** tool, which is the only tool that sees
  cost lines the bill of materials does not cover. `budget_outlook` reports the **configured** budget,
  so when a comparison is on screen `ask()`'s prefix **names both sides** and the tool's own `note`
  says which pair it is describing — the divergence is stated rather than left for the agent to answer
  about a comparison nobody is looking at.
- **The repaint hosts.** `renderOverview()` writes the shell once and `renderBody()` repaints
  everything the dropdowns move. `#bo-compare` and the composer are **siblings** of
  `#bo-overview-body`, never children, so a pair change cannot reach the two selects — the third
  instance of the rule `#bo-cash-form-host` and `#scenario-edit` already follow. Two consequences that
  fail silently if missed: `wireOverview()` runs **once**, so the document-level Escape handler and the
  composer submit are never double-bound; and the `.bo-mix-seg` `title` assignment lives in
  `renderHero()`, not `wireOverview()`, or every cost-mix tooltip disappears after the first dropdown
  use. Mid-switch `#bo-overview-body` gets `.is-stale` and the previous figures **dim rather than
  disappear** — a page that blanks on every dropdown change reads as broken even when it is working.

Dataset reads in `budgetplan.py` are **lazy imports inside the functions that need them**, so
everything above `derive()` stays importable with no pandas and no data directory — which is what
keeps most of `tests/test_budget_plan.py` and the whole engine half of
`tests/test_budget_comparison.py` fixture-free.

One **pre-existing** limitation is worth knowing before trusting the stored plan's plan-year total: a
persisted variable is "this year's amount × a percentage", so a cost line that starts in the plan year
has `current_amount == 0`, `_pct_change(0, X)` is 0, and its money vanishes from `cost_next`. It never
fires on the seeded data (every cost centre runs in every year, every driver keeps its BOM position),
and `test_the_configured_pair_reproduces_the_persisted_plan` pins the divergence explicitly rather than
hiding it. `derive()`'s `next_amount` is the mechanism for the fix; carrying it through
`_clean_variable`/`validate`/`fingerprint` and the config table is the work.

## See also

- `references/scenario-engine.md` — `project_pnl`, `driver_exposure` and `opex_bridge`, the leaves
  the comparison orchestrates over.
- `references/cash.md` — what the cash strip is reading.
- `references/frontend.md` — the `.hidden`-per-component rule, the bare `select { max-width: 190px }`
  trap and `askFromBudget()`'s `goHome()` path.
- `references/demo-data.md` — the two seeded properties the comparison depends on.
- `references/api-and-gates.md` — why `/api/budgetplan*` is ungated but its comparison route is not.
