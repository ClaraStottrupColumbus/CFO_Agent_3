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

## Result size is part of the contract

Whatever a tool returns is `json.dumps`'d into `convo` and **re-sent on every remaining round of the
turn**, so an oversized result is not paid for once — it is paid for up to eight times. Two rules
follow, and both reverse how this file used to read.

- **`query_budget_data` returns `columns` + positional `rows`, not records.** It is the one tool that
  returns bulk rows (up to 500), and a list of dicts repeats every column name on every row: on
  `budget_vs_actuals` at the cap that repetition was most of a 161k-char result. Columnar measured
  50–63% smaller across the built-in datasets with nothing lost. This is deliberately **not** the
  house shape — everywhere else the key on each value is what makes a handful of rows readable, and
  `_records` still exists for them. Size is the whole justification; do not spread it on taste.
  `rows_as_records` folds it back for HTTP callers (`/api/datasets/{name}`), because the saving is
  about the model's context and a JSON API should not change shape underneath its callers.
- **`read_document` is windowed** (`DOCUMENT_WINDOW_CHARS`, with `offset`/`next_offset` and a
  `truncated` flag, the same discipline as `query_budget_data`). It used to return the whole file:
  bounded at 200k chars for a converted document, and **unbounded** for markdown added through
  `add_markdown`/`save_research`, which never passes through `MAX_EXTRACT_CHARS`. One such read early
  in a turn could dominate the turn's entire spend.

## See also

- `references/data-ingestion.md` — `tools.dataset_meta` is the ONE place "built-in" and "uploaded"
  are told apart, and why `query_budget_data`'s `dataset` argument must never carry an `enum`.
- `references/drivers-trust-boundary.md` — the two tools that write, and the guards in front of them.
- `references/scenario-engine.md` — `build_budget_scenario`, `_basis_prices`, `scenario_options`.
- `references/cash.md` — `cash_flow_projection` and how it resolves each input.
- `references/budget-tab.md` — `budget_outlook`, the only tool that sees cost lines the bill of
  materials does not cover.
