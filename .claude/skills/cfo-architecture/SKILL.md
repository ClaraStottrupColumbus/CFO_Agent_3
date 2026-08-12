---
name: cfo-architecture
description: Architecture, invariants and load-bearing design decisions of this CFO budgeting agent — the agent loop, the driver trust boundary, data ingestion, the scenario engine, cash, the Budget tab, the scheduler and the SPA. Read the relevant reference file BEFORE changing anything under app/, static/ or tests/, and update that file in the same change.
metadata:
  domain: architecture
  role: reference
  scope: this repo only
---

# CFO agent architecture

This skill is the repo's architecture manual, split so you read the subsystem you are touching and
nothing else. `CLAUDE.md` holds only purpose, boot/test commands and the routing table below.

**Read the reference file before you change the subsystem, and update it in the same change.** Every
paragraph in these files exists because something failed silently without it — they document *why*,
which is the part the code cannot tell you.

## What this is

A CFO **budgeting** agent: it builds, defends and revises next year's budget by holding cost-driver
assumptions (ingredient prices, freight, FX, energy) as sourced, dated, first-class objects, and
saying when one has moved far enough to change the budget. The same rule extends to cash: the
working-capital days behind the cash plan are measured off the balance sheet, not typed in.

FastAPI + uvicorn in one process, vanilla-JS SPA, JSON/Parquet files on disk. No database, no message
broker, no frontend build step, no bundler.

## Layering and import discipline

```
static/ (vanilla JS SPA, hash-routed)  ──fetch JSON + SSE──►  app/main.py (FastAPI, one process)
                                                                │
   reporting.py  run_session_turn  ── agent.py (streaming loop) ── tools.py ── budget.py (pure maths)
                                                                            └───── cash.py (pure maths)
   scheduler.py (daemon thread) ── tasks.py ── rules.py (pure pandas) ── alerts.py
   store.py / profile.py / config.py / scenarios.py / drivers.py / budgetversions.py = JSON + Parquet
   export.py (pure leaf: xlsx + markdown out of plain dicts)
   budgetplan.py (own store, own gate; lazy tools/drivers reads for BOM mode) ── agent.get_client
   tools.py ── uploads.py (registry) ── ingest.py (leaf; lazy agent.get_client)
```

Two rules hold the graph acyclic and are load-bearing:

- `reporting.py` exists **only** so `tasks.py` and `alerts.py` can run agent turns without importing
  `main.py`. Never import `main` from a background module.
- `config.py` is injected into the scheduler as the zero-arg callable `get_run_params`
  (`app/main.py:51`) for the same reason; `agent.run_agent` takes its settings as
  arguments so it stays callable from tests with no settings file on disk.

`budget.py` and `citations.py` are pure leaves — no I/O, no app imports. That is what makes the test
suite possible without touching disk or the SDK; keep new arithmetic and new block/URL handling
there rather than inline in `tools.py`/`agent.py`. `export.py` is the third: it imports only
`budget.py`, and takes driver provenance and company details as *arguments* rather than reading
`drivers.py`, which is why `tests/test_export.py` needs no fixtures at all. `cash.py` is the fourth
and imports nothing at all. `budgetversions.py` is a store, but its `diff()` is pure for the same
reason. `ingest.py` is the fifth leaf, and its one app dependency — `agent.get_client` inside
`convert_document` — is a **local** import for two reasons at once: it keeps the module importable
with no API key and no SDK on the path, and it breaks a genuine cycle
(`agent → tools → uploads → ingest`). Per-format readers (`pypdf`, `docx`, `pptx`) are imported
inside their own extractor for the same reason `export.py` imports `openpyxl` inside `workbook()` —
a stale venv loses one file format, not the whole app.

`main.py` is the only module that joins those two halves: `_drivers_snapshot()` reads
`drivers.driver_status()` and hands the result to `budgetversions.create_version` and
`export.scenario_pack`. Keep that direction — a store or an exporter that reaches back into the
dataset layer loses its tests.

## Cross-cutting invariants

These recur across five or six subsystems, so they are the rules most easily broken by someone who
read only one reference file:

- **`None`, never `0`, for "we have not measured this."** `driver_status.forward_12m`,
  `cash.cash_conversion_pct`, `budgetplan.expected_change_pct`. "We have not looked" and "the answer
  is zero" are different facts and a UI that renders the first as 0 is lying.
- **Teachable `{"error": …}` dicts that enumerate the valid options, never exceptions.** The model
  corrects itself inside one turn. The same stance is applied to humans — an inline refusal next to
  the field, not "could not save".
- **Every store writes `.tmp` then `Path.replace()` under a `threading.Lock`.** A scheduler worker
  and an interactive stream do write concurrently.
- **Pure leaves take their inputs as arguments and read no dataset** — `budget.py`, `citations.py`,
  `export.py`, `cash.py`, `ingest.py`. That is what keeps their tests fixture-free; a leaf that
  reaches back into the dataset layer loses them.
- **The second-door rule.** A CFO-facing route calls the model's existing function, never a second
  implementation. Two derivations of one figure is how a page and an answer come to disagree.
- **Bridges close.** `variance_decomposition`, `ebitda_bridge`, `budgetversions.diff` and
  `cash.project_cashflow` each carry a `check_residual`. A bridge that does not close is one nobody
  can defend to a board.
- **Extend, don't fork: assert the no-op first.** A new optional argument must leave the old
  arithmetic byte-for-byte unchanged when omitted, and the test file must say so before it asserts
  anything new.
- **Module-level path constants**, so tests can `monkeypatch.setattr` them at a `tmp_path`.

## Which file to read

| Touching | Read first |
|---|---|
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

A change that spans two subsystems reads both — the seams (`main.py`'s joins, the two-cuts problem,
the `_chart_spec` hand-off) are where the invariants actually break.

## Keeping this documentation true

- Change a subsystem → **update its reference file in the same change**. A rationale that lags the
  code is worse than none, because the next agent trusts it.
- Reverse a decision → say in the file that it reversed and why. `references/budget-tab.md` opens
  with "this section reverses what it said before Phase 3, deliberately" — that is the house style,
  and it is how a future agent knows the change was deliberate rather than drift.
- New subsystem → new `references/*.md` **and** a row in both routing tables (this file and
  `CLAUDE.md`).
- Do not grow `CLAUDE.md` back. If what you are adding is more than one line, it belongs in a
  reference file.
- Write down the *why*, not just the rule.
