# The API surface and the setup gate

Governs the routes in `app/main.py`, the `require_setup` dependency and the two second-door
conventions every route follows.

## Setup gate

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

## What is gated, at a glance

`Gated = [Depends(require_setup)]` (`app/main.py:123`) is applied per route. The **ungated** set is
small and closed — everything not listed here carries `dependencies=Gated`:

| Ungated route | Why |
|---|---|
| `GET`/`POST /api/profile`, `/api/profile/propose`, `/api/profile/demo`, `/api/profile/reset` | the setup wizard itself; gating them would deadlock the gate |
| `POST /api/profile/working-capital` | the cash inputs change mid-round — see `references/cash.md` |
| `GET`/`POST /api/settings` | the settings panel must work while gated |
| `GET /api/budgetplan`, `/api/budgetplan/defaults`, `POST /api/budgetplan/{rebuild,config,reset,narrative}` | the Budget tab has its own gate, `plan["configured"]` — see `references/budget-tab.md` |

And the two deliberate exceptions *inside* an otherwise-ungated area, both gated because they read
the datasets and the scenario store: `GET /api/budgetplan/comparison` and `GET /api/cash`.

The `/api/uploads*` routes are gated, consistent with `/api/datasets`, and use this repo's
`HTTPException(detail={"error": …})` envelope — see `references/data-ingestion.md`.

## The second-door rule

A CFO-facing route calls the **model's existing function**, never a second implementation of it. Two
derivations of the same figure is how a page and an answer come to disagree; two derivations of "the
valid product lines" is how a form offers a value the validator then rejects. The four instances:

| Route | Calls |
|---|---|
| `POST /api/assumptions/lock` | `drivers.lock_assumptions` |
| `PUT /api/scenarios/{id}` | `tools.build_budget_scenario_tool` |
| `GET /api/scenario-options` | `tools.scenario_options` |
| `GET /api/cash` | `tools.cash_flow_projection` |

The prose behind the first three is in `references/versions-and-export.md`; behind the fourth, in
`references/cash.md`. Note `/api/scenario-options` is a **flat** path, not `/api/scenarios/options`,
which `/api/scenarios/{scenario_id}` would swallow.

## 409 vs 400

A well-formed request that the object's *state* forbids is a **409**, not a 400 — `PUT
/api/scenarios/{id}` on a scenario frozen by an approved version, and `POST
/api/budget/versions/{id}/revoke` on a version that is not approved. `setup_incomplete` is the same
distinction applied to the whole app.

## The joins `main.py` owns

`main.py` is the only module that joins the store layer to the dataset layer.
`_drivers_snapshot()` reads `drivers.driver_status()` and hands the result to
`budgetversions.create_version` and `export.scenario_pack`; `budgetversions.approved_freeze()` is
joined in as `frozen` in the `GET /api/scenarios` envelope rather than inside `scenarios.summary`.
Keep that direction — a store or an exporter that reaches back into the dataset layer loses its
tests. See the layering diagram in `SKILL.md`.

## See also

- `references/sessions.md` — the session routes and what streams through them.
- `references/scheduler-and-alerts.md` — `get_run_params` is injected into the scheduler from
  `main.py` so background modules never import it.
