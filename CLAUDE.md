# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CFO **budgeting** agent: it builds, defends and revises next year's budget by holding cost-driver
assumptions (ingredient prices, freight, FX, energy) as sourced, dated, first-class objects, and
saying when one has moved far enough to change the budget.

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
`drivers.py`, which is why `tests/test_export.py` needs no fixtures at all. `budgetversions.py` is a
store, but its `diff()` is pure for the same reason.

`main.py` is the only module that joins those two halves: `_drivers_snapshot()` reads
`drivers.driver_status()` and hands the result to `budgetversions.create_version` and
`export.scenario_pack`. Keep that direction — a store or an exporter that reaches back into the
dataset layer loses its tests.

### A session is the only user-visible object

Chat, weekly market scan and monthly budget revision are all **sessions** — one JSON file under
`data/history/{kind}/{id}.json`, `KINDS = ("chat", "weekly", "monthly")`. A report is just a session
whose first user message is a preset prompt (`WEEKLY_PROMPT` / `MONTHLY_PROMPT`), so report
generation, report follow-ups, scheduled prompts, the setup proposal stream and ordinary chat all run
through one function, `reporting.run_session_turn`. Child chats under a report carry `parent_id`.

The `monthly` kind has **no tab of its own**. A budget revision exists to produce a scenario, so both
live under `#/scenarios`: "+ New scenario" creates a `monthly` session, streams one turn seeded with a
client-composed build prompt, and the agent's `build_budget_scenario` tool persists the result. Past
revisions — including everything the scheduled `budget_revision` task writes — list in the Revisions
panel and open at `#/monthly/{id}` in the ordinary thread view. `#/monthly` with no id
`location.replace`s to `#/scenarios`, and `NAV_ALIAS` points the kind at the Scenarios pill. Nothing
about the kind changed server-side; this is a navigation merge only.

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
- `MODEL_CAPS` derives tool version, thinking mode, effort and **`fallbacks`** support per model.
  `AVAILABLE_MODELS` is the subset whose `fallbacks` is true — not the whole registry — because the
  loop sends `betas` + `fallbacks` on every request, so a model that cannot accept them cannot be
  offered. That was a real 400 on every turn for anyone who picked sonnet; the fix is that the picker
  is derived from what the request actually sends, so it is unrepresentable rather than merely fixed.
  Any new model-dependent request field must be gated on a cap the same way, or it recurs.
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
  from `query_budget_data` rows, and `project_series` is restricted to volumes and driver price
  series (forward P&L goes through `build_budget_scenario`, on a stated `basis`). `budget_outlook`
  is the one tool that reads `budget_plan.json`; it imports `budgetplan` lazily so the SDK does not
  end up behind every `tools.py` import, including the pure tests'.
- `_load` prefers Parquet, falls back to CSV — which is what lets tests use CSV fixtures.
- `render_chart` returns its validated spec under the private `_chart_spec` key; the loop pops it,
  streams the full spec to the UI and hands the model a compact ack.

### The scenario engine speaks in four assumption blocks

`build_budget_scenario` / `budget.project_pnl` take `{"drivers": {driver_id: pct}, "volume":
{product_line: pct}, "price": {product_line: pct}, "opex": {cost_centre | driver_id: pct}}` —
volume and price are the first two conversations in any budget round, so they are first-class
rather than reachable only as a consequence of cost. `"*"` is the wildcard in `volume`/`price`.

Four things there are load-bearing:

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

### A scenario is explored; a version is committed

[app/budgetversions.py](app/budgetversions.py) — `data/budget_versions.json`, the same store pattern
as everything else. A scenario is live: re-runnable against today's prices, deletable, and it moves
when the market does. A version is a **freeze** of one, and the difference is the whole point of the
module:

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
- `POST /api/assumptions/lock` is a thin route onto the **existing** `drivers.lock_assumptions` — the
  CFO gets a second door to the model's function, never a second implementation. Locking replaces
  the whole set, which is why the form on `#/drivers` posts every driver.

[app/export.py](app/export.py) turns either record into a workbook (**Summary · Monthly P&L · By
product line · Assumptions · Driver bridge · Version diff**) or a board-pack markdown. Three things
there are deliberate: the **Assumptions sheet carries `source_url` / `retrieved_at` / `locked_at` per
driver** and lists the drivers the scenario *didn't* shock — that provenance is the reason to export
from this tool rather than from a spreadsheet, so do not tidy those columns away. `openpyxl` is
imported **inside** `workbook()`, so a checkout that predates the requirements bump loses one route
rather than failing to import `main.py`. And `export.bridge_rows` deliberately does *not* fold small
drivers into "Other drivers" the way `tools._ebitda_bridge_points` does for the chart: a spreadsheet
has no point budget, and a board pack that hides a line is worse than a long one.

### Setup gate

Almost every route depends on `require_setup` and 409s with `{"error": "setup_incomplete"}` until the
CFO confirms a company profile. `/api/settings`, `/api/profile*` and `/api/budgetplan*` stay
**ungated** so the settings panel, the setup wizard and the Budget tab work while gated. On the
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
  the two modes differ only in where `variables[]` comes from. That is what makes it one model
  rather than two behind one tab, and it is why the whole of `tests/test_budget_plan.py` above the
  BOM section needed no changes.
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
- **Its gate is still its own** (`plan["configured"]`), independent of `setup_complete`, and the
  `/api/budgetplan*` routes are still **ungated**: in simple mode there is nothing for a profile to
  gate. BOM mode reads datasets that only exist after setup, so it simply does not offer itself until
  they do — the mode follows the data, not the gate.
- **Numbers are computed, prose is not.** The model writes only the 2–4 sentence read (cached on
  `fingerprint()`, falling back to `templated_narrative()` when the API is unreachable).
- **There is no chat loop here any more.** `stream_chat` and `/api/budgetplan/chat*` are gone. A page
  whose cost lines name real drivers has no business answering questions about them without the
  tools, so every "ask" affordance hands off through `askFromBudget()` in app.js — the same
  `pendingHomeMessage` + `goHome()` path an alert, a driver card and a scenario card already use. The
  agent reads the page back through the **`budget_outlook`** tool, which is the only tool that sees
  cost lines the bill of materials does not cover.

Dataset reads in `budgetplan.py` are **lazy imports inside the two functions that need them**, so
everything above `derive()` stays importable with no pandas and no data directory — which is what
keeps most of `tests/test_budget_plan.py` fixture-free.

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
drawn between them. The lock form on `#/drivers` and the version rail on `#/scenarios` add no fourth
surface for the same reason; the approved version carries the same 4 px accent rule as the active
scenario, because it means the same thing — *this is the one you are running on*.

`renderLockPanel` returns early while `lockFormOpen`: `refreshDrivers` re-paints every 2 s during a
verify run, and re-rendering the form would wipe what the CFO is typing into it.

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
export formats, the forward-curve guards and per-month pricing, and the BOM materialisation's
reconciliation to the P&L.

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
