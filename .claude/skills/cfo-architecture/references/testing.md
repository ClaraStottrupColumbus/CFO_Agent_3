# Testing

Governs `tests/`. Run the suite with `.venv/bin/python -m pytest -q` — see `CLAUDE.md` for the
single-file and single-case invocations.

Pure functions get tests; `main.py` routes, the SSE layer, the streaming shell of `run_agent` and
anything touching the network deliberately do not. `tests/` covers the variance/sensitivity/scenario
maths, the opex bridge, the provenance guards, block classification, rule boundaries, alert dedup,
schedule math, the model-capability registry, the version diff and approval state machine, both
export formats, the forward-curve guards and per-month pricing, the BOM materialisation's
reconciliation to the P&L, the cash projection's additivity, sign conventions and measured days, and —
behind the CFO's edit form — the scenario store's replace-by-id semantics (overwrite in place,
`created_at` preserved, `active` inherited from an absent key) and which scenario an *approved* version
freezes. `tests/test_budget_comparison.py` splits the same way the
module does: an engine half with no fixtures at all (`derive()`'s explicit amounts, `_pct_or_none`'s
undefined case, the same-selection zero) and an orchestration half whose fixture deliberately carries a
region with no budget and a cost centre absent from one year — the two shapes the seeded data never
produces, and the only way to exercise a line that exists on one side only.

The three ingestion files split the same way. `tests/test_ingest.py` is
**fixture-free** like `test_export.py`, because `ingest.py` is a leaf: classification, the
`YYYY-MM` date convention, the stated truncations, the content-hash cache and the
no-key/failed-call/failed-write fallbacks. `tests/test_uploads.py` pins the
registry's safety properties — `_slugify` asserted as a *property* (`[a-z][a-z0-9_]*`, one
component) for the backslash cases, since `Path.stem` splits differently on Windows and POSIX —
plus reserved names, registry-first delete and the cache-key ownership move on edit.
`tests/test_dataset_resolver.py` is the seam, and it **opens with
the no-op**: every curated dataset still cites its bare filename.

Tests that need data `monkeypatch.setattr` the module-level path constants (`tools.DATA_DIR`,
`drivers.PRICES_FILE`, `scenarios.SCENARIOS_FILE`, …) at a `tmp_path` and write **CSV** fixtures —
see the `data` fixture in `tests/test_rules.py:14`. New modules that read or
write files should keep their paths as module-level constants so this stays possible.

## The no-op-first discipline

Three test files open by asserting that an extension changed *nothing*, before asserting anything it
added. Follow the pattern when extending a pure function:

- `tests/test_forward_curve.py` — omit `driver_prices_by_month` and `project_pnl` is byte-for-byte
  what it was (`references/scenario-engine.md`).
- `tests/test_budget_comparison.py` — omit `next_amount` and `derive()` is unchanged
  (`references/budget-tab.md`).
- `tests/test_dataset_resolver.py` — every curated dataset still cites its bare filename
  (`references/data-ingestion.md`).

## See also

Each reference file names the tests that pin its invariants. `references/demo-data.md` is the
exception worth knowing: `app/generate_data.py` has **no tests at all**.
