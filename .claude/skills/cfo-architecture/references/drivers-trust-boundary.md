# The trust boundary: `record_driver_observation` and `record_driver_forward`

Governs `app/drivers.py` and the two model-writable datasets, `driver_prices` and `driver_forwards`.
This is the highest-consequence code in the repo. Tested by `tests/test_driver_guards.py` and
`tests/test_forward_curve.py`.

Server-side web tools return results into the model's *context*, not to disk, so nothing downstream
can compute on them. Those two tools are the only path from web research to pandas, which makes
`driver_prices` and `driver_forwards` the two **model-writable** datasets. Two pure guards in
`app/drivers.py` stand in front of both writes and are the highest-consequence code
in the repo (`tests/test_driver_guards.py`, `tests/test_forward_curve.py`):

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

## See also

- `references/api-and-gates.md` — `POST /api/assumptions/lock` is a thin second door onto
  `drivers.lock_assumptions`, never a second implementation.
- `references/scenario-engine.md` — `basis: "forward"` is what consumes `forward_curve()`.
- `references/versions-and-export.md` — `main._drivers_snapshot()` freezes this provenance onto a
  budget version so a later re-lock cannot rewrite what was approved.
