# Conventions in `tools.py`

Governs `app/tools.py` — the local tool surface the agent loop dispatches — and the dataset reads
underneath it.

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

## See also

- `references/data-ingestion.md` — `tools.dataset_meta` is the ONE place "built-in" and "uploaded"
  are told apart, and why `query_budget_data`'s `dataset` argument must never carry an `enum`.
- `references/drivers-trust-boundary.md` — the two tools that write, and the guards in front of them.
- `references/scenario-engine.md` — `build_budget_scenario`, `_basis_prices`, `scenario_options`.
- `references/cash.md` — `cash_flow_projection` and how it resolves each input.
- `references/budget-tab.md` — `budget_outlook`, the only tool that sees cost lines the bill of
  materials does not cover.
