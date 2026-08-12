# A scenario is explored; a version is committed

Governs `app/budgetversions.py` (`data/budget_versions.json`) and `app/export.py`. Tested by
`tests/test_budget_versions.py` and `tests/test_export.py`.

`app/budgetversions.py` — `data/budget_versions.json`, the same store pattern
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
  properties make the undo safe, and `tests/test_budget_versions.py`
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

## Export

`app/export.py` turns either record into a workbook (**Summary · Monthly P&L · By
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

## See also

- `references/scenario-engine.md` — what a version is a freeze *of*, and why an edit re-validates the
  basis.
- `references/drivers-trust-boundary.md` — the provenance `drivers_snapshot` captures.
- `references/cash.md` — what `cash_snapshot` holds and why the Cash flow sheet prints its caveats.
- `references/api-and-gates.md` — the second-door rule and the 409-vs-400 distinction.
