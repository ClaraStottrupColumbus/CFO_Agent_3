# EBITDA is not cash

Governs `app/cash.py` (the fourth pure leaf), `tools.cash_flow_projection`, `GET /api/cash` and the
`working_capital` block on the company profile. Tested by `tests/test_cash_flow.py`.

`app/cash.py` is the fourth pure leaf and imports **nothing** — not even `budget.py`.
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

Three properties are load-bearing and `tests/test_cash_flow.py` pins them:

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
  "none of it converts" are different answers, exactly as with `driver_status`'s `forward_12m` (see
  `references/drivers-trust-boundary.md`).

`tools.cash_flow_projection` is the wiring: it resolves each input **argument → profile → measured**,
reads `capex_plan` for the scenario's own year, and offers one genuine lever —
`include_proposed_capex: false` defers the projects marked `proposed`, and committed capex is not a
lever so it does not pretend otherwise. Neither `working_capital` nor `capex_plan` is model-writable;
they are read-only datasets, unlike the two behind the trust boundary
(`references/drivers-trust-boundary.md`).

`GET /api/cash` is a thin route onto that same tool — the second-door rule again, so the figure on the
Budget page and the figure in an answer cannot disagree — and it returns `available: false` with a
reason instead of a 4xx, because "we have not measured your working capital yet" is a state the page
renders as a prompt, not a failure. The cash inputs live on the **company profile**
(`working_capital`: the three day counts, opening cash, cash tax rate, minimum cash, note), where
every field defaults to `None` = *not stated*, and `POST /api/profile/working-capital` is ungated with
the rest of `/api/profile*` because those figures change mid-round and re-opening the setup wizard to
edit one would be absurd.

## See also

- `references/scenario-engine.md` — the `by_month` shape `project_cashflow` consumes.
- `references/budget-tab.md` — the cash strip follows the right-hand selection and renders body and
  form into separate hosts.
- `references/versions-and-export.md` — `cash_snapshot` on a version, and the Cash flow sheet.
- `references/demo-data.md` — the seeded DSO drift and the April trough the deferrable capex cannot
  reach.
