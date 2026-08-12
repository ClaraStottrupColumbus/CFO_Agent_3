# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CFO **budgeting** agent: it builds, defends and revises next year's budget by holding cost-driver
assumptions (ingredient prices, freight, FX, energy) as sourced, dated, first-class objects, and
saying when one has moved far enough to change the budget. The same rule extends to cash: the
working-capital days behind the cash plan are measured off the balance sheet, not typed in.

FastAPI + uvicorn in one process, vanilla-JS SPA, JSON/Parquet files on disk. No database, no message
broker, no frontend build step, no bundler.

## Booting the agent

One command, from the repo root — the whole app (API + SPA) is a single uvicorn process:

```bash
.venv/bin/uvicorn app.main:app --port 8323 --reload
```

Then open **http://127.0.0.1:8323/** — `/` serves the SPA, which lands on Home: a centred chat that
keeps its conversation in place. Ready when the log says `Application startup complete.` Port **8323**
is the convention (Agent 1 = 8321, Agent 2 = 8322). Run it backgrounded with output to a log file;
`--reload` picks up `app/` edits, but **not** `static/` cache-busting — see the `?v=N` rule below.

Preconditions, in the order they bite:

1. **`.venv` exists** — not committed, and `requirements.txt` is the only dependency source:
   `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
2. **`.env` has `ANTHROPIC_API_KEY`** — the only required env var, read at import time by
   `app.main`, and never overriding a value already exported in the shell. If missing:
   `cp .env.example .env` and fill it in. The server still starts without it; the agent turns fail.
3. **`data/` is populated** — if datasets are missing, `.venv/bin/python -m app.generate_data`
   (seeded and deterministic, `RNG_SEED = 42`).

Smoke-check without a browser:

```bash
curl -s http://127.0.0.1:8323/api/profile          # {"setup_complete": true, ...}
curl -s "http://127.0.0.1:8323/api/sessions?kind=chat"
```

If `setup_complete` is `false`, every gated route 409s with `{"error": "setup_incomplete"}` and the
SPA lands on `#/setup` — that is the setup gate working, not a bug.

Tests — the whole suite, one file, one case:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_driver_guards.py -q
.venv/bin/python -m pytest tests/test_variance_decomposition.py -k additivity -q
```

**Editing `static/*` requires bumping the `?v=N` query string** on all **five** asset links in
[static/index.html](static/index.html) (`columbus-tokens.css`, `style.css`, `budget.css`,
`budget.js`, `app.js`) — there is no no-cache middleware in this app, so browsers serve a stale
`app.js` otherwise. Bump them together; a half-bumped set is worse than none.

## Other files worth knowing about

- [Agent_upgrade.md](Agent_upgrade.md) — the live rewrite plan on the `agent_rewrite` branch, in
  phases, each marked complete as it lands. Where it contradicts this file it is deliberate and says
  so; read the relevant phase before changing a subsystem it touches.
- Module docstrings throughout `app/` say **"the reference"** — they mean CFO_Agent_2, the sibling
  app this one was cloned from. Its frontend behaviour (routing, markdown renderer, streaming
  reveal, SVG charts, polling) still holds almost verbatim here; its backend module table, tool
  names and task types do not.
- `skills/` holds two vendored generic skills (`python-pro`, `code-reviewer`); nothing here depends
  on them.

## Architecture

### Layering and import discipline

```
static/ (vanilla JS SPA, hash-routed)  ──fetch JSON + SSE──►  app/main.py (FastAPI, one process)
                                                                │
   reporting.py  run_session_turn  ── agent.py (streaming loop) ── tools.py ── budget.py (pure maths)
                                                                            └───── cash.py (pure maths)
   scheduler.py (daemon thread) ── tasks.py ── rules.py (pure pandas) ── alerts.py
   store.py / profile.py / config.py / scenarios.py / drivers.py / budgetversions.py = JSON + Parquet
   export.py (pure leaf: xlsx + markdown out of plain dicts)
   budgetplan.py (own store, own gate; lazy tools/drivers reads for BOM mode) ── agent.get_client
```

Two rules hold the graph acyclic and are load-bearing:

- `reporting.py` exists **only** so `tasks.py` and `alerts.py` can run agent turns without importing
  `main.py`. Never import `main` from a background module.
- `config.py` is injected into the scheduler as the zero-arg callable `get_run_params`
  ([main.py:51](app/main.py:51)) for the same reason; `agent.run_agent` takes its settings as
  arguments so it stays callable from tests with no settings file on disk.

`budget.py` and `citations.py` are pure leaves — no I/O, no app imports. That is what makes the test
suite possible without touching disk or the SDK; keep new arithmetic and new block/URL handling
there rather than inline in `tools.py`/`agent.py`. `export.py` is the third: it imports only
`budget.py`, and takes driver provenance and company details as *arguments* rather than reading
`drivers.py`, which is why `tests/test_export.py` needs no fixtures at all. `cash.py` is the fourth
and imports nothing at all. `budgetversions.py` is a store, but its `diff()` is pure for the same
reason.

`main.py` is the only module that joins those two halves: `_drivers_snapshot()` reads
`drivers.driver_status()` and hands the result to `budgetversions.create_version` and
`export.scenario_pack`. Keep that direction — a store or an exporter that reaches back into the
dataset layer loses its tests.

### A session is the only user-visible object

Chat, weekly market scan and monthly budget revision are all **sessions** — one JSON file under
`data/history/{kind}/{id}.json`, `KINDS = ("chat", "weekly", "monthly")`. A report is just a session
whose first user message is a preset prompt (`WEEKLY_PROMPT` / `MONTHLY_PROMPT`), so report
generation, report follow-ups, scheduled prompts, the setup proposal stream and ordinary chat all run
through one function, `reporting.run_session_turn`. Child chats under a report carry `parent_id`
**and the report's own kind** — `startThread` and `resolveTargetSession` both call
`apiCreateSession(view.kind, reportId)`, so a follow-up question about a market scan is itself a
`weekly` session. Kind alone therefore does *not* separate a report from a conversation about it;
`parent_id` is the discriminator, which is why `is_conversation` and `model_for_session` both test it
and why the sidebar lists filter on `!s.parent_id`.

**Which of the two configured models runs a turn is decided from that pair**, in the pure
`reporting.model_for_session`: a `weekly`/`monthly` session with no parent is the heavy model
(default `claude-opus-5`), everything else is the general one (default `claude-sonnet-5`). The
decision lives next to the session rather than at the four call sites because the session is the only
place both facts are available — and because `tasks.execute_task` receives an already-resolved params
dict and cannot import `config`, which is the whole reason `get_run_params` is *injected* into the
scheduler. `get_run_params()` stays zero-arg and ships both models; its `model` key is the **general**
one, so every pre-split reader of `params["model"]` — the budget-page narrative, the alert narrative,
driver verification, the assumption refresh — is on the cheap model with no edit, and only the report
path opts up. A per-task override in `tasks.py` sets both slots, or a pinned scan would silently keep
running on the configured heavy model.

Neither the `monthly` nor the `weekly` kind has **a tab of its own**. Both are the same merge, done
twice, and neither changed anything server-side:

- A budget revision exists to produce a scenario, so both live under `#/scenarios`: "+ New scenario"
  creates a `monthly` session, streams one turn seeded with a client-composed build prompt, and the
  agent's `build_budget_scenario` tool persists the result. Past revisions — including everything the
  scheduled `budget_revision` task writes — list in the Revisions panel. Each card also carries
  **Edit assumptions**, which recomputes that scenario in place through the same tool — except the one
  an approved version was frozen from, which shows its frozen state instead.
- A market scan exists to say which drivers moved, so it lives under `#/drivers` as the **"This week"
  strip**: the latest scan's headline, the drifted drivers, and a button into the full transcript.
  `WEEKLY_PROMPT` asks for a headline that stands alone precisely because the strip shows that line
  and nothing else; `scanHeadline()` takes the first line of the first assistant turn and strips
  markdown rather than rendering it. The strip loads **once per visit**, not on the 2 s verify poll —
  and `renderWeekStrip` bails while a scan is running, the same guard `renderLockPanel` uses.

`#/monthly` and `#/weekly` with no id `location.replace` to `#/scenarios` and `#/drivers`; an
individual transcript still opens at `#/{kind}/{id}` in the ordinary thread view, whose sidebar lists
every past one. `NAV_ALIAS = {monthly: "scenarios", weekly: "drivers"}` points each kind at the pill
it now lives under.

UI-only message fields (`attachments`, `text`, `charts`, `reasoning`, per-message `sources`) are
persisted but stripped before the API call.

Every store module (`store`, `profile`, `config`, `tasks`, `alerts`, `rules`, `scenarios`, `drivers`)
writes to `.tmp` then `Path.replace()` under a `threading.Lock`. Keep that pattern — a scheduler
worker and an interactive stream do write concurrently.

### The agent loop is the novel part

[app/agent.py](app/agent.py) — the reference's loop assumed every tool runs locally; this one mixes
local pandas tools with Anthropic's **server-side** `web_search` / `web_fetch`. Three things there are
correctness-critical and commented as such:

- `stop_reason == "pause_turn"` must **continue** (re-issue with no user turn), not break — treating
  it as "finished" silently truncates a research turn with no error. Bounded by `MAX_CONTINUATIONS`.
- A round with **no local tool uses** must break before appending a user turn, or it POSTs
  `content: []` → 400. `MAX_TOOL_TURNS` counts only rounds that ran ≥1 local tool.
- `MODEL_CAPS` derives tool version, thinking mode, effort and **`fallbacks`** support per model, and
  **`model_request_fields` is the one place a model-dependent request field is written**.
  `AVAILABLE_MODELS` is a subset of the registry — the models whose caps clear what the loop needs of
  every model: the two web tools (this app's premise is cited research), adaptive thinking, effort.
  `fallbacks` is deliberately **not** in that derivation any more. It was, and it had to be, back
  when the loop sent `betas` + `fallbacks` unconditionally — that was a real 400 on every turn for
  anyone who picked sonnet, and excluding the model was the fix available at the time. Gating the two
  lines on the cap is the better fix, and once it exists the requirement genuinely shrinks and sonnet
  becomes offerable. The rule is unchanged and is the thing to preserve: a field only some models
  accept gets gated in `model_request_fields` and dropped from the `AVAILABLE_MODELS` derivation,
  **in that order**. Never widen the list on its own.
  `web_search` and `web_fetch` carry separate version keys deliberately (pre-4.6 dates differ).

Also: `build_system` splits the system prompt into a **cached** block (static prompt + stable company
profile) and an **uncached** one (`volatile_context`: today's date, staleness counts). Putting the
date in the cached block means zero cache reads forever with nothing erroring to tell you. Tool order
in `build_tools` is part of the cached prefix — reordering silently invalidates the cache.

Event vocabulary yielded by `run_agent` (the backend↔browser contract, documented in its docstring):
`text`, `reasoning`, `research`, `tool_call`, `tool_result`, `chart`, `citation`, `web_error`,
`notice`, `sources`, `done`, `error`. `error` is **terminal** in the frontend's contract — a failed
web tool must yield `web_error` instead, and every error arm yields `done` after it so the frontend's
streaming state clears.

### The trust boundary: `record_driver_observation` and `record_driver_forward`

Server-side web tools return results into the model's *context*, not to disk, so nothing downstream
can compute on them. Those two tools are the only path from web research to pandas, which makes
`driver_prices` and `driver_forwards` the two **model-writable** datasets. Two pure guards in
[app/drivers.py](app/drivers.py) stand in front of both writes and are the highest-consequence code
in the repo ([tests/test_driver_guards.py](tests/test_driver_guards.py),
[tests/test_forward_curve.py](tests/test_forward_curve.py)):

- `verify_source_url` — the cited page must actually have been fetched **this turn**. Both sides go
  through `citations.normalise_url`; if the two sides ever diverge the guard fails closed, refuses
  every legitimate observation, and leaves nothing in the logs pointing at the comparison.
- `check_sanity_band` — 0.2×–5.0× the last known value, escapable via `override_sanity_check`. For a
  forward the band is measured against the latest **spot**, which is what a forward is a claim about.

The boundary widened to a second dataset without a second implementation, and that is the property
to protect: `append_forward` calls the same two functions. It differs from `append_observation` only
in taking a **list** of `{quote_month, price}` points, because a curve is read off one page in one
go — twelve calls would mean twelve provenance checks against the same URL and eleven chances to
drift onto another page mid-curve. Validation is all-or-nothing for the same reason: what lands on
disk is always a curve somebody could have read off the cited page.

`forward_curve()` resolves the latest curve per quote month (newest `curve_date`, then `revision`),
so a re-read supersedes without deleting. `driver_status` reports `forward_12m` and
`forward_vs_lock_pct` **as `None`, never 0, when no curve exists** — "we have not looked" and "the
market says flat" are different states and a UI that renders 0% for the first is lying.

`lock_assumptions` is split from `append_observation` on purpose: routine research must not be able
to overwrite the CFO's locked position.

### Conventions in `tools.py`

- Errors are teachable `{"error": "…"}` dicts that **enumerate** valid options, never exceptions —
  the model corrects itself inside one turn.
- Every result carries `source_file`, except presentation-only tools; that absence is what keeps
  `render_chart` out of the citation list.
- All arithmetic lives here or in `budget.py`. The system prompt forbids the model deriving variances
  from `query_budget_data` rows. `budget_outlook` is the one tool that reads `budget_plan.json`; it
  imports `budgetplan` lazily so the SDK does not end up behind every `tools.py` import, including
  the pure tests'.
- **There is deliberately no extrapolation tool.** `project_series` — linear trend plus seasonality
  over a driver's own history — was deleted once `driver_forwards` landed, and prompt rule 3 is now
  the prohibition rather than the restriction: a fitted line is a claim about the future with no
  source behind it, which is the one thing this app exists not to ship. Forward prices come from the
  published curve (`record_driver_forward` → `basis: "forward"`), forward P&L from
  `build_budget_scenario`. If neither has data, "we have not looked" is the answer. Do not re-add a
  projection tool; add a curve.
- `_load` prefers Parquet, falls back to CSV — which is what lets tests use CSV fixtures.
- `render_chart` returns its validated spec under the private `_chart_spec` key; the loop pops it,
  streams the full spec to the UI and hands the model a compact ack.

### The scenario engine speaks in four assumption blocks

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

### `basis` — what the percentages sit on top of

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

### A scenario is explored; a version is committed

[app/budgetversions.py](app/budgetversions.py) — `data/budget_versions.json`, the same store pattern
as everything else. A scenario is live: re-runnable against today's prices, **editable by hand**,
deletable, and it moves when the market does. A version is a **freeze** of one, and the difference is
the whole point of the module:

- **It snapshots the driver provenance, not a reference to it.** `create_version` takes
  `drivers_snapshot` (value, `source_url`, `retrieved_at`, `locked_at`, rationale per driver) as an
  argument — `main._drivers_snapshot()` builds it. A later re-lock therefore cannot rewrite what was
  approved, and the export can print a source next to every assumption months afterwards.
- **`diff(a, b)` is pure and reports three cuts**, because "what changed" gets asked three ways: the
  assumptions the CFO stated, the **locked values underneath them** (a percentage can hold still
  while the price it applies to moves — that cut is what answers "the €2.4M swing came from
  re-locking chicken meal on 12 Nov"), and the EBITDA those add up to. Both sides attribute against
  the same baseline plan, so component-wise subtraction is exact; a re-cut baseline becomes its own
  step and `check_residual` stays ~0. A version stored without a bridge still closes, via an
  `Unattributed` step — a diff that silently drops a move is worse than an ugly one.
- **At most one `approved` version**; approving another supersedes it, and an approved version
  cannot be deleted or re-submitted. `approved_by` is required and free-text: this app has **no
  authentication**, so it is an *attestation*, not a signature, and both the API error and the UI
  copy say so rather than implying otherwise.
- **`revoke_approval` is the undo, and it is a withdrawal rather than an erasure.** Approving the
  wrong version used to be a one-way door: `approve` refused a second approval, `submit` refused an
  approved version, `delete_version` refused it too, and the frozen scenario stayed read-only — so the
  only way out was hand-editing `budget_versions.json`. That was a gap, not a decision. Four
  properties make the undo safe, and [tests/test_budget_versions.py](tests/test_budget_versions.py)
  pins each: `approved_by`/`approved_at` move to **`revoked_by`/`revoked_at`** rather than being
  cleared, because "was approved and taken back" is a different fact from "was never approved"; the
  version returns to **`submitted` if it had been submitted and `draft` otherwise**, since revoking an
  approval is not undoing the whole review; a **superseded predecessor is not silently promoted**,
  because "the budget is v1 again" is a second decision this function was not asked to make, so
  revoking leaves *nothing* approved; and `by` is **optional** here where it is required for
  approving — a revocation with no name is still better than a budget frozen on a mis-click. Deletion
  stays a separate, deliberate second step, and `delete_version`'s refusal now **enumerates both ways
  out** (revoke, or supersede), because a refusal that does not say what to do instead is exactly why
  someone reaches for the JSON file. The route is `POST /api/budget/versions/{id}/revoke` and it 409s
  rather than 400s on a non-approved version — the request is well formed and the object's state
  forbids it, the same distinction `PUT /api/scenarios/{id}` draws for a frozen scenario.
- **It snapshots the cash profile too** (`cash_snapshot`), for the same reason it snapshots the
  driver provenance: the working-capital days were *measured* off the balance sheet on the day it was
  approved, so next quarter's measurement must not silently rewrite the funding case the board signed
  off. Optional — a version created on a company with no working-capital history carries `None`, and
  the Cash flow sheet says which inputs are missing rather than printing zeros.
- **The approved version is the one thing that makes a scenario read-only.** `approved_freeze()` —
  which scenario the approved version was frozen from — lives in *this* module, because a version
  knows what it froze while a scenario knows nothing about versions, and `scenarios.py` must not learn
  otherwise. `main.py` joins the halves twice: as `frozen` in the `GET /api/scenarios` envelope (not
  inside `scenarios.summary`, whose shape belongs to that module) and as the **409** on
  `PUT /api/scenarios/{id}`. A *draft or submitted* version deliberately freezes nothing — a budget
  under review is still being explored — and superseding hands the older scenario back, so at most one
  approved version means at most one frozen scenario, which is why the shape is one record or `None`
  rather than a map. A frozen scenario is still **deletable**: the version holds a full copy, which is
  the entire reason the freeze is a copy.
- `POST /api/assumptions/lock` is a thin route onto the **existing** `drivers.lock_assumptions` — the
  CFO gets a second door to the model's function, never a second implementation. Locking replaces
  the whole set, which is why the form on `#/drivers` posts every driver. `PUT /api/scenarios/{id}` →
  `tools.build_budget_scenario` and `GET /api/scenario-options` → `tools.scenario_options` are the same
  rule applied twice more; the second exists so the edit form's selects and the tool's teachable
  validation come off **one** dataset read, because two derivations of "the valid product lines" is how
  a form offers a value the validator then rejects. Its path is flat, not `/api/scenarios/options`,
  which `/api/scenarios/{scenario_id}` would swallow.

[app/export.py](app/export.py) turns either record into a workbook (**Summary · Monthly P&L · By
product line · Cash flow · Assumptions · Driver bridge · Version diff**) or a board-pack markdown.
The **Cash flow** sheet takes its content from a `cash=` *argument* like everything else here, so
the module still reads no dataset, and it prints every `caveat` next to the figures rather than
leaving them to a covering email — it is the sheet a board is most likely to misread as a bank
balance, and it is not one. Three more things
there are deliberate: the **Assumptions sheet carries `source_url` / `retrieved_at` / `locked_at` per
driver** and lists the drivers the scenario *didn't* shock — that provenance is the reason to export
from this tool rather than from a spreadsheet, so do not tidy those columns away. `openpyxl` is
imported **inside** `workbook()`, so a checkout that predates the requirements bump loses one route
rather than failing to import `main.py`. And `export.bridge_rows` deliberately does *not* fold small
drivers into "Other drivers" the way `tools._ebitda_bridge_points` does for the chart: a spreadsheet
has no point budget, and a board pack that hides a line is worse than a long one.

### EBITDA is not cash

[app/cash.py](app/cash.py) is the fourth pure leaf and imports **nothing** — not even `budget.py`.
It exists because a budget can be entirely right about EBITDA and still miss the thing that kills
companies: the receivables and inventory behind the extra revenue have to be funded before the
margin arrives. Two functions, and the split matters:

- **`working_capital_days` MEASURES DSO/DIO/DPO** off `data/working_capital.parquet` (monthly
  receivables, inventory, payables and cash balances) — average balance × Σdays ÷ Σflow over a
  trailing window. This is the same discipline as `driver_exposure` reading qty-per-tonne off the
  bill of materials rather than guessing an elasticity: **a working-capital day nobody measured is an
  assumption with no source.** The CFO can still override one, and then it is labelled a *decision*
  everywhere it appears (`days.basis` → `measured` | `stated` | `measured + stated`). DPO is measured
  against COGS because there is no purchases dataset, and that approximation is named in `caveats`
  rather than buried.
- **`project_cashflow` turns a scenario's `by_month` into a monthly cash profile.** Balances are
  `flow/days × the day count`, so they carry the scenario's own revenue and cost *shape* — which is
  what makes a forward-basis budget produce a genuinely different cash profile from a flat one. Then
  `free_cash_flow = ebitda − working_capital_swing − tax − capex`, with the swing signed off
  `WC_COMPONENTS` (receivables and inventory rising consume cash; payables rising release it) so
  there is one place that convention lives.

Three properties are load-bearing and [tests/test_cash_flow.py](tests/test_cash_flow.py) pins them:

- **It closes.** Σ free cash flow == closing cash − opening cash (`check_residual`, the same
  discipline as the variance and EBITDA bridges), *and* the year's working-capital swing equals the
  closing balances less the opening ones. A cash plan that cannot be pointed at a balance-sheet
  movement is one nobody can defend.
- **Nothing is invented, and the gaps are stated.** `tax_rate_pct=None` means no tax is deducted and
  a caveat says so — a guessed tax rate moves a cash trough further than most of the assumptions this
  app guards. `min_cash_eur=None` means no floor is tested rather than a floor at zero. Omitted
  opening balances are implied from month one (so the first month shows no artificial step) and the
  result declares it. **There is no depreciation, interest, debt or dividend dataset**, so what comes
  out is free cash flow *before financing* — never net income, never a bank balance — and every
  caller prints that. Same rule as the missing payroll dataset.
- **`cash_conversion_pct` is `None`, never 0, when EBITDA is nil** — "the ratio is undefined" and
  "none of it converts" are different answers, exactly as with `driver_status`'s `forward_12m`.

`tools.cash_flow_projection` is the wiring: it resolves each input **argument → profile → measured**,
reads `capex_plan` for the scenario's own year, and offers one genuine lever —
`include_proposed_capex: false` defers the projects marked `proposed`, and committed capex is not a
lever so it does not pretend otherwise. Neither `working_capital` nor `capex_plan` is model-writable;
they are read-only datasets, unlike the two behind the trust boundary above.

`GET /api/cash` is a thin route onto that same tool — the second-door rule again, so the figure on the
Budget page and the figure in an answer cannot disagree — and it returns `available: false` with a
reason instead of a 4xx, because "we have not measured your working capital yet" is a state the page
renders as a prompt, not a failure. The cash inputs live on the **company profile**
(`working_capital`: the three day counts, opening cash, cash tax rate, minimum cash, note), where
every field defaults to `None` = *not stated*, and `POST /api/profile/working-capital` is ungated with
the rest of `/api/profile*` because those figures change mid-round and re-opening the setup wizard to
edit one would be absurd.

### Setup gate

Almost every route depends on `require_setup` and 409s with `{"error": "setup_incomplete"}` until the
CFO confirms a company profile. `/api/settings`, `/api/profile*` (including
`/api/profile/working-capital`) and `/api/budgetplan*` stay **ungated** so the settings panel, the
setup wizard and the Budget tab work while gated. `/api/cash` and `/api/budgetplan/comparison` are the
two exceptions on that page and are **gated**, because both read the scenario engine and the datasets;
the cash strip and the two budget selectors therefore just do not render until setup is done, the same
way BOM mode does not offer itself. On the
frontend, `profile` gates every route except `#/setup` and `#/budget`. Startup report generation is
gated on both the API key and `setup_complete()`.

The setup research turn runs as a real session so it inherits streaming, citations and a readable
transcript; the proposal comes back through the `propose_watchlist` **tool call** rather than
structured outputs, because `output_config.format` is incompatible with citations (400).

### The Budget tab — one engine, two input modes

[app/budgetplan.py](app/budgetplan.py) + [static/budget.js](static/budget.js) +
[static/budget.css](static/budget.css) + `#budget-view` / `#budget-config-view`.

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
  [tests/test_budget_comparison.py](tests/test_budget_comparison.py) asserts **first**, exactly as
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
    (see Demo data).
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

### Autonomy

`scheduler.py` is one daemon thread; each due task runs on its own worker, a per-task guard prevents
self-overlap, failures land in `last_status`/`last_error`. Schedule math (`tasks.compute_next_run`) is
a pure function — that is why it is tested. `TASK_TYPES = (data_refresh, driver_scan,
budget_revision, assumption_refresh, drift_scan, custom_prompt)`; `TASK_KIND` maps task type to
session kind explicitly (never `ttype.split("_")[0]`).

`rules.py` detects deterministically in pandas and **never calls the API**; only the narrative in
`alerts.py` does, and it falls back to the finding title when the network is down. The finding shape
`{rule_id, entity, period, metric_value, threshold, severity, title}` is `.format(**finding)`-ed into
`URGENCY_ANALYSIS_PROMPT` — renaming a key breaks prompt formatting at runtime, not at import. Alert
dedup is **windowed with magnitude bucketing**, not permanent-per-period, so a re-breach at a higher
magnitude re-fires.

There are **three** rules, and there were five. `driver_stale` and `scenario_ebitda_floor` are gone
because both fired on every evaluation and trained the CFO to mute the other three. The distinction
worth keeping is **state vs event**: nothing *happens* when a driver crosses its staleness limit, and
a scenario under the EBITDA floor stays under it until someone acts — so both re-fired the same
finding every window. Neither is lost as information: staleness is a badge, a summary count and a
card rule on `#/drivers`, and the floor's arithmetic (`rules._rerun_active_scenario`, still tested)
now feeds `main.budget_state` and the home screen headline. An alert should be something that
happened; anything that is merely true belongs on a screen.

Background agent runs take `reporting.AGENT_SEMAPHORE` (bounded at 2); interactive chat never
acquires it, so user requests can't queue behind an 03:00 market scan.

### Frontend

[static/app.js](static/app.js) — one file, hash-routed, `showOnly(viewEl)` over `ALL_VIEWS`. Every
view ships in [static/index.html](static/index.html) as a sibling `<main>`; nothing is templated.
Rendering is `innerHTML` from template strings and then attach listeners; a changed list is
re-rendered wholesale. **Three escapes, by position, and they are not interchangeable:** text nodes
through `escapeHtml`, anything inside a quoted attribute through **`escapeAttr`** (`escapeHtml`
leaves quotes alone, so a model-supplied name containing `" onfocus="…` closes the value and opens a
handler without ever needing a `<`), and URLs through `safeHttpUrl` — no amount of escaping stops a
`javascript:` URL, so links are built imperatively with a protocol allowlist. Charts are hand-built SVG strings
(`CHART_COLORS` is validated for lightness/chroma/CVD/contrast — do not reorder). Liveness comes from
polling timers, not websockets; `route()` is the single place navigation clears them.

A bare `#/` is **Home** (`renderHome`) — and so is any unrecognised kind. Home is *not* a second view:
it is `#feature-view` wearing `.is-home`, which hides the sidebar and the header and centres the
composer. That matters, because `sendMessage`, `resolveTargetSession`, `createStreamRenderer` and the
attachment code all close over the module-level `#composer` / `#input` / `#messages` singletons; a
separate home view would mean parameterising every one of them. Only two things differ at runtime:
`view.isHome`, and the two lines at the top of `sendMessage` that drop the `.home-hero` and add
`.has-thread` on the first question. Nothing navigates — `resolveTargetSession`'s `replaceState`
fires no `hashchange`, so the conversation continues on Home while staying recoverable on reload.
`renderFeature` must strip `is-home`/`has-thread`, or the thread view renders with no sidebar.

`#/chat` is **Ask**, the conversation archive (`#ask-view`, `renderAsk`) — a searchable grid of past
chats, not a fresh one. `#/chat/{id}` opens one in the ordinary thread view. Anything that means "ask
a new question" (`+ New question`, an alert investigation, a driver or scenario hand-off) goes through
`goHome()`, which exists because `location.hash = "#/"` fires no event when the hash is already `#/`.

`#topic-switcher` is the app's one navigation surface, and **`route()` is the only place that decides
nav chrome** — one `updateNav(kind || "home")` call after the profile gate sets both visibility and the
active tab. Render functions must not touch the switcher themselves; that is exactly how `#/drivers`
and `#/scenarios` used to inherit whichever pill was highlighted last. `updateNav` hides the nav only
where its links would be traps: `#/setup` (everything else 409s until the profile is confirmed) and
Budget Outlook's first run, read through the synchronous `BudgetPlan.isConfigured()` — safe because
`boot()` awaits `BudgetPlan.load()` before the first `route()`.

`.card` (the surface under every driver, scenario, version and archived conversation) and `.panel`
(the cream-50 module container they group into) live in `style.css`. Three surfaces separate by
lightness alone — cream canvas, cream-50 panel, white card — so section boundaries need no rules
drawn between them. The lock form and the "This week" strip on `#/drivers` and the version rail on
`#/scenarios` add no fourth surface for the same reason; the approved version carries the same 4 px accent rule as the active
scenario, because it means the same thing — *this is the one you are running on*.

`renderLockPanel` returns early while `lockFormOpen`: `refreshDrivers` re-paints every 2 s during a
verify run, and re-rendering the form would wipe what the CFO is typing into it. The scenario edit form
solves the same problem **structurally instead of with a flag**: `#scenario-edit` is a *sibling* of
`#scenarios-body` and `#versions-body`, so a finished build, an activate or a delete repaints the lists
and cannot reach inside the form — the `#bo-cash-form-host` rule again. `editingScenarioId` therefore
lives outside the form too, letting a repaint mark the card being edited without reading an input.
Navigating in closes the form (`renderScenarios` → `closeScenarioEdit`); a repaint never does.

**`.hidden` is scoped per component in both stylesheets, and there is no global rule** — so a component
that toggles the class without declaring it has a collapse that silently does nothing. That was the
History button on `#/drivers` for as long as `.driver-history.hidden` was missing: the handler toggled,
the table stayed, and the button's label never changed either. Declare the rule next to the element,
and give the control a visible state (`History` / `Hide history` plus `aria-expanded`) so a dead toggle
is visible immediately rather than a year later. `.error-text.hidden` and `#scenario-edit-form.hidden`
were missing for the same reason, and `.bo-compare.hidden`, `.bo-cmp-error.hidden`, `.bo-read-tag.hidden`
and `.bo-regen.hidden` are declared next to their elements for the same reason.

One more trap in the same family, in the other direction: **`style.css` ships a bare
`select { max-width: 190px }`**, so any new `<select>` outside `.bo-field` silently inherits it — and
`width: 100%` does not beat a `max-width`. `.bo-compare select` and the settings panel's two model
selects lift it explicitly, with `max-width: none`. That second example used to be `#model-select`,
which set `width: 100%` **and nothing else** — so the one rule this file cited as the good example
was itself clipped to 190px in a 340px panel for as long as it existed. A scenario name clipped to
190px is a stub nobody can tell apart from the next one.

`createStreamRenderer(container, bubble, opts)` is the extracted streaming reveal loop, shared by
chat, the setup wizard and the reasoning disclosure. Its 130 ms cadence, `max(14, backlog/5)` batch
size, `.fade-new` span boundary (via the `\\u0001` sentinel inserted before markdown parsing), the
120 px near-bottom scroll rule and the `document.hidden` flush path are byte-for-byte from the
reference. A regression in any of them surfaces later as a *citations* or *reasoning* bug and gets
debugged in the wrong file. Citation `[n]` markers are rewritten to superscript links in
`finalRender` only — never on a reveal tick, where a fade span can split a marker in half.

Styling: [static/columbus-tokens.css](static/columbus-tokens.css) is pure design tokens;
[static/style.css](static/style.css) maps them to app-local semantic names and then styles by
section. Brand ratio ~70% cream canvas / 25% navy / **5% orange reserved for primary CTAs only**.

## Testing

Pure functions get tests; `main.py` routes, the SSE layer, the streaming shell of `run_agent` and
anything touching the network deliberately do not. `tests/` covers the variance/sensitivity/scenario
maths, the opex bridge, the provenance guards, block classification, rule boundaries, alert dedup,
schedule math, the model-capability registry, the version diff and approval state machine, both
export formats, the forward-curve guards and per-month pricing, the BOM materialisation's
reconciliation to the P&L, the cash projection's additivity, sign conventions and measured days, and —
behind the CFO's edit form — the scenario store's replace-by-id semantics (overwrite in place,
`created_at` preserved, `active` inherited from an absent key) and which scenario an *approved* version
freezes. [tests/test_budget_comparison.py](tests/test_budget_comparison.py) splits the same way the
module does: an engine half with no fixtures at all (`derive()`'s explicit amounts, `_pct_or_none`'s
undefined case, the same-selection zero) and an orchestration half whose fixture deliberately carries a
region with no budget and a cost centre absent from one year — the two shapes the seeded data never
produces, and the only way to exercise a line that exists on one side only.

Tests that need data `monkeypatch.setattr` the module-level path constants (`tools.DATA_DIR`,
`drivers.PRICES_FILE`, `scenarios.SCENARIOS_FILE`, …) at a `tmp_path` and write **CSV** fixtures —
see the `data` fixture in [tests/test_rules.py](tests/test_rules.py:14). New modules that read or
write files should keep their paths as module-level constants so this stays possible.

## Demo data

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
