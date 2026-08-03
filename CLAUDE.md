# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CFO **budgeting** agent: it builds, defends and revises next year's budget by holding cost-driver
assumptions (ingredient prices, freight, FX, energy) as sourced, dated, first-class objects, and
saying when one has moved far enough to change the budget.

FastAPI + uvicorn in one process, vanilla-JS SPA, JSON/Parquet files on disk. No database, no message
broker, no frontend build step, no bundler.

## Commands

Setup (no `.venv` is committed — create one; `requirements.txt` is the only dependency source):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Run the server (port **8323** by convention — Agent 1 = 8321, Agent 2 = 8322):

```bash
.venv/bin/uvicorn app.main:app --port 8323 --reload
```

Tests:

```bash
.venv/bin/python -m pytest -q
```

A single file / single case:

```bash
.venv/bin/python -m pytest tests/test_driver_guards.py -q
```

```bash
.venv/bin/python -m pytest tests/test_variance_decomposition.py -k additivity -q
```

Rebuild the demo datasets (deterministic, `RNG_SEED = 42`):

```bash
.venv/bin/python -m app.generate_data
```

`ANTHROPIC_API_KEY` is the only required env var. `app.main` reads `.env` at import time and never
overrides a value already exported in the shell (`cp .env.example .env`).

**Editing `static/*` requires bumping the `?v=N` query string** on all three asset links in
[static/index.html](static/index.html) — there is no no-cache middleware in this app, so browsers
serve a stale `app.js` otherwise.

## Documentation already in the repo

- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — **this** app's spec: design rationale per
  module, the citation model, the tool surface, the test table (§10), build order (§11) and a
  10-step manual verification checklist (§12). Read the relevant section before changing a
  subsystem; most non-obvious code has a "why" recorded there.
- [ARCHITECTURE.md](ARCHITECTURE.md) — describes the **reference/sibling app** (CFO_Agent_2) this
  one was cloned from, not this codebase. Its frontend chapters (§8: routing, markdown renderer,
  streaming reveal, SVG charts, polling) still apply almost verbatim; its backend module table,
  tool names, task types and the `.claude/launch.json` reference do **not**. Module docstrings
  throughout `app/` say "the reference" — they mean that app.
- `skills/` holds two vendored generic skills (`python-pro`, `code-reviewer`); nothing here depends
  on them.

## Architecture

### Layering and import discipline

```
static/ (vanilla JS SPA, hash-routed)  ──fetch JSON + SSE──►  app/main.py (FastAPI, one process)
                                                                │
   reporting.py  run_session_turn  ── agent.py (streaming loop) ── tools.py ── budget.py (pure maths)
   scheduler.py (daemon thread) ── tasks.py ── rules.py (pure pandas) ── alerts.py
   store.py / profile.py / config.py / scenarios.py / drivers.py   =  JSON + Parquet on disk
```

Two rules hold the graph acyclic and are load-bearing:

- `reporting.py` exists **only** so `tasks.py` and `alerts.py` can run agent turns without importing
  `main.py`. Never import `main` from a background module.
- `config.py` is injected into the scheduler as the zero-arg callable `get_run_params`
  ([main.py:51](app/main.py:51)) for the same reason; `agent.run_agent` takes its settings as
  arguments so it stays callable from tests with no settings file on disk.

`budget.py` and `citations.py` are pure leaves — no I/O, no app imports. That is what makes the test
suite possible without touching disk or the SDK; keep new arithmetic and new block/URL handling
there rather than inline in `tools.py`/`agent.py`.

### A session is the only user-visible object

Chat, weekly market scan and monthly budget revision are all **sessions** — one JSON file under
`data/history/{kind}/{id}.json`, `KINDS = ("chat", "weekly", "monthly")`. A report is just a session
whose first user message is a preset prompt (`WEEKLY_PROMPT` / `MONTHLY_PROMPT`), so report
generation, report follow-ups, scheduled prompts, the setup proposal stream and ordinary chat all run
through one function, `reporting.run_session_turn`. Child chats under a report carry `parent_id`.

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
- `MODEL_CAPS` derives tool version, thinking mode and effort support **per model**, and
  `AVAILABLE_MODELS` is derived from it, so a model cannot reach the picker without capability data.
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

### The trust boundary: `record_driver_observation`

Server-side web tools return results into the model's *context*, not to disk, so nothing downstream
can compute on them. `record_driver_observation` is the only path from web research to pandas, which
makes `driver_prices` the one **model-writable** dataset. Two pure guards in
[app/drivers.py](app/drivers.py) stand in front of that write and are the highest-consequence code in
the repo ([tests/test_driver_guards.py](tests/test_driver_guards.py)):

- `verify_source_url` — the cited page must actually have been fetched **this turn**. Both sides go
  through `citations.normalise_url`; if the two sides ever diverge the guard fails closed, refuses
  every legitimate observation, and leaves nothing in the logs pointing at the comparison.
- `check_sanity_band` — 0.2×–5.0× the last known value, escapable via `override_sanity_check`.

`lock_assumptions` is split from `append_observation` on purpose: routine research must not be able
to overwrite the CFO's locked position.

### Conventions in `tools.py`

- Errors are teachable `{"error": "…"}` dicts that **enumerate** valid options, never exceptions —
  the model corrects itself inside one turn.
- Every result carries `source_file`, except presentation-only tools; that absence is what keeps
  `render_chart` out of the citation list.
- All arithmetic lives here or in `budget.py`. The system prompt forbids the model deriving variances
  from `query_budget_data` rows, and `project_series` is restricted to volumes and driver price
  series (forward P&L goes through `build_budget_scenario`).
- `_load` prefers Parquet, falls back to CSV — which is what lets tests use CSV fixtures.
- `render_chart` returns its validated spec under the private `_chart_spec` key; the loop pops it,
  streams the full spec to the UI and hands the model a compact ack.

### Setup gate

Almost every route depends on `require_setup` and 409s with `{"error": "setup_incomplete"}` until the
CFO confirms a company profile. `/api/settings` and `/api/profile*` stay **ungated** so the settings
panel and the setup wizard work while gated. On the frontend, `profile` gates every route. Startup
report generation is gated on both the API key and `setup_complete()`.

The setup research turn runs as a real session so it inherits streaming, citations and a readable
transcript; the proposal comes back through the `propose_watchlist` **tool call** rather than
structured outputs, because `output_config.format` is incompatible with citations (400).

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
Rendering is `innerHTML` from template strings (every interpolated value through `escapeHtml`) and
then attach listeners; a changed list is re-rendered wholesale. Charts are hand-built SVG strings
(`CHART_COLORS` is validated for lightness/chroma/CVD/contrast — do not reorder). Liveness comes from
polling timers, not websockets; `route()` is the single place navigation clears them.

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
anything touching the network deliberately do not. Nine files cover the variance/sensitivity/scenario
maths, the provenance guards, block classification, rule boundaries, alert dedup, schedule math and
the model-capability registry.

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
