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

## Architecture — read before you change anything

The architecture lives in the **`cfo-architecture` skill**, not in this file: one reference file per
subsystem, each carrying the rationale behind invariants that fail *silently* when broken. Open the
one that covers what you are touching before you touch it. Paths are relative to
`.claude/skills/cfo-architecture/`.

| Touching | Read first |
|---|---|
| Anything, if you are new to the repo | `SKILL.md` — layering, import discipline, cross-cutting invariants |
| `app/agent.py` | `references/agent-loop.md` |
| `app/reporting.py`, `app/store.py`, session JSON under `data/history/` | `references/sessions.md` |
| `app/drivers.py`, `record_driver_observation`, `record_driver_forward` | `references/drivers-trust-boundary.md` |
| `app/tools.py`, anything reading `data/*.parquet` | `references/tools-and-datasets.md` |
| `app/ingest.py`, `app/uploads.py`, `#/data`, chat attachments | `references/data-ingestion.md` |
| `app/budget.py`, `app/scenarios.py`, assumptions or `basis` | `references/scenario-engine.md` |
| `app/budgetversions.py`, `app/export.py` | `references/versions-and-export.md` |
| `app/cash.py`, working capital, capex | `references/cash.md` |
| `app/budgetplan.py`, `static/budget.js`, `static/budget.css` | `references/budget-tab.md` |
| `app/scheduler.py`, `app/tasks.py`, `app/rules.py`, `app/alerts.py` | `references/scheduler-and-alerts.md` |
| `static/app.js`, `static/style.css`, `static/index.html` | `references/frontend.md` |
| `app/main.py` routes, the setup gate | `references/api-and-gates.md` |
| `tests/` | `references/testing.md` |
| `app/generate_data.py`, seeded `data/` | `references/demo-data.md` |

## Keeping this documentation true

The reference files are part of the code, not commentary on it:

- Change a subsystem → **update its reference file in the same change**. A rationale that lags the
  code is worse than none, because the next agent trusts it.
- Reverse a decision → say in the file that it reversed and why. `references/budget-tab.md` opens
  with "this section reverses what it said before Phase 3, deliberately" — that is the house style.
- New subsystem → new `references/*.md` **and** a row in both routing tables (this one and the one
  in `SKILL.md`).
- **Do not grow this file back.** If what you are adding is more than one line, it belongs in a
  reference file. `CLAUDE.md` holds purpose, boot, routing and this contract — nothing else.
- Write down the *why*, not just the rule. Every paragraph in those files exists because something
  failed silently without it.

## Other files worth knowing about

- [Agent_upgrade.md](Agent_upgrade.md) — the live rewrite plan on the `agent_rewrite` branch, in
  phases, each marked complete as it lands. Where it contradicts the reference files it is deliberate
  and says so; read the relevant phase before changing a subsystem it touches.
- [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) + [Dockerfile](Dockerfile) — container build and the
  Azure deployment path. Nothing in `app/` depends on either; local development never needs them.
- Module docstrings throughout `app/` say **"the reference"** — they mean CFO_Agent_2, the sibling
  app this one was cloned from. Its frontend behaviour (routing, markdown renderer, streaming
  reveal, SVG charts, polling) still holds almost verbatim here; its backend module table, tool
  names and task types do not.
- `.claude/skills/` holds two vendored generic skills (`python-pro`, `code-reviewer`) that nothing
  here depends on, plus `cfo-tester`, which is repo-specific: it boots the app and hands it to a CFO
  persona for a relevance review.
