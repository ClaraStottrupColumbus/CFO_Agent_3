# Data ingestion — the CFO's own files

Governs `app/ingest.py`, `app/uploads.py`, the `/api/uploads*` routes, `#/data` and the chat
attachment path in `app/reporting.py`. Tested by `tests/test_ingest.py`, `tests/test_uploads.py` and
`tests/test_dataset_resolver.py`.

`app/ingest.py` + `app/uploads.py` + the `/api/uploads*` routes +
`#/data`. A file dropped on the Data ingestion page becomes one of exactly two things, and the split
is the feature: **tabular** (csv/tsv/xlsx/xls/parquet) becomes a **dataset**, which is *queried*;
everything else readable (pdf/docx/pptx/txt/md/…) becomes a **document**, which is *read* whole
through `read_document`. Images are refused with "attach it in a chat instead", because that is
where the model can actually see them. The page renders the two as separate sections for the same
reason — one list would misrepresent what the agent can do with each.

Six properties are load-bearing:

- **`tools.dataset_meta` is the ONE place "built-in" and "uploaded" are told apart**, and everything
  above and below it is written against a uniform shape. `_load` reads `meta["dir"]`, the query
  engine, the preview route, the citation layer and the model itself never learn the difference.
  This is the property to protect: it is what keeps "add your own data" from doubling the surface
  area of every tool that touches data. `tests/test_dataset_resolver.py` opens by asserting the
  **no-op** — that every curated dataset still cites its bare filename — before it asserts anything
  new, the same discipline as `test_forward_curve.py`'s flat curve.
- **`source_file` is now relative to `data/`** (`budget_overview.csv`, `uploads/board_plan.parquet`),
  which needed no citation change at all: `citations.dataset_record` already labels off
  `rsplit("/", 1)[-1]`. That is the whole reason an uploaded figure is citable — the plumbing was
  already there and the seam was chosen to fit it.
- **No `enum` in the tool schema for a runtime-mutable set.** `TOOL_DEFINITIONS` is built once at
  import; uploads happen at runtime. `query_budget_data`'s `dataset` argument used to carry
  `"enum": list(DATASETS)`, which would freeze the set at server start and silently exclude every
  upload. It is validated on execution instead, and `_unknown_dataset` enumerates built-ins *and*
  uploads and tells the model to call `list_datasets`. Do not re-add the enum.
- **The registry is authoritative; filenames on disk are always derived from it.** `_slugify`
  (lowercase, `[a-z0-9_]`, must start with a letter, capped at 48, `_2`/`_3` on collision) is the
  path-traversal defence — untrusted input reaches the filesystem exactly once, and only after it.
  `RESERVED_NAMES` stops an upload shadowing a built-in, and is a **literal** rather than
  `list(tools.DATASETS)` because `tools` imports `uploads`; a test asserts the two cannot drift.
  Delete is **registry-first**, so a crash mid-delete leaves an orphaned file rather than a record
  pointing at nothing.
- **Convert at upload time, keyed by content hash, on a cheap model.** `ingest.convert_cached` runs
  the extraction through a Haiku reformatter whose system prompt says it is "a reformatter, not an
  analyst" and must never touch a figure — the only framing under which this is safe in an app whose
  premise is sourced numbers. The cache key is the **SHA-256 of the bytes**, so the conversion
  happens once per distinct file content ever, and no answer stalls on it mid-turn.
  `uploads.add_markdown` and `update_document` key on the **record id** instead, because two
  identical saved answers must not share one file — `cached_markdown` treats the key as opaque and
  needs no special case. Editing a document takes ownership of its markdown and drops the
  content-hash entry: once a person corrects a bad extraction, their version *is* the document, and
  the fix reaches every future answer that cites it.
- **Deterministic fallback over hard failure, everywhere.** No API key or a failed converter call
  returns the raw text; a Parquet serialisation failure keeps the CSV; a failed cache write is
  ignored; a scanned PDF returns the `SCANNED_PDF` sentinel so the caller sends the pages as a native
  document block rather than shipping an empty extraction. Same stance as `alerts.py`'s narrative.

Two smaller deliberate choices. The routes are **gated**, consistent with `/api/datasets`, and use
this repo's `HTTPException(detail={"error": …})` envelope rather than the bare `{"error": …}` body
the sibling app returns — the frontend's `apiError()` unwraps all three shapes. And the converter is
pinned to the *same* Haiku id as `agent.REASONING_SUMMARY_MODEL`, because two hardcoded model ids in
one app is one that silently goes stale.

**Chat attachments run on the same primitives but stay ephemeral.** `reporting._file_block`
dispatches on `ingest.classify`: a tabular attachment inlines through `summarise_table` (truncation
*stated*, pointing at the Data page to query all of it), a document through `convert_cached`. Nothing
is written to the registry. Three consequences worth knowing: `.xlsx`/`.docx`/`.pptx` work in a chat
at all now; what lands in the persisted session JSON is the converted **text**, not the base64
payload, so a thread stops re-sending megabytes on every subsequent turn; and the conversion is
content-hash shared with the Data page, so attaching a file already added there costs nothing.
`POST /api/uploads/research` is the one bridge between the two worlds — a "Save to Data ingestion"
action on an assistant answer — and it is opt-in, because a market scan is worth re-reading months
later and a transcript is not.

## See also

- `references/tools-and-datasets.md` — `_load`, `source_file` and the teachable-error convention the
  `_unknown_dataset` message follows.
- `references/testing.md` — why `tests/test_ingest.py` is fixture-free and `tests/test_uploads.py` is
  not.
- `references/api-and-gates.md` — the `/api/uploads*` routes sit behind `require_setup`.
