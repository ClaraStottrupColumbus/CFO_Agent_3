# tools.dataset_meta is the ONE place "built-in" and "uploaded" are told apart.
# Everything above and below it — the query engine, the citation layer, the
# preview route, the model itself — is written against a uniform shape.
#
# The first test asserts the NO-OP: that adding uploads changed nothing about how
# a curated dataset loads or cites. Same discipline as test_forward_curve.py
# asserting the flat-curve no-op before anything else.

import pytest

from app import citations, ingest, tools, uploads

CSV = b"month,revenue_eur,region\n2026-01,100,Nordics\n2026-02,120,Baltics\n"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOADS_FILE", tmp_path / "user_uploads.json")
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "uploads" / "cache")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def builtin(tmp_path, monkeypatch):
    """A curated dataset on disk as a CSV fixture, the house pattern."""
    monkeypatch.setattr(tools, "DATA_DIR", tmp_path)
    (tmp_path / "budget_overview.csv").write_bytes(CSV)
    return tmp_path


# ---------- The no-op: built-ins are untouched ----------

def test_a_builtin_still_cites_its_bare_filename(builtin):
    """A top-level file's path relative to data/ IS its bare name, so making
    _load return a relative path changed no built-in citation."""
    df, source = tools._load("budget_overview")
    assert source == "budget_overview.csv"
    assert len(df) == 2


def test_every_builtin_source_file_is_a_bare_name_not_a_path():
    for name in tools.DATASETS:
        try:
            _, source = tools._load(name)
        except (FileNotFoundError, OSError):
            continue          # dataset not generated in this checkout
        assert "/" not in source, f"{name} started citing a path"


def test_dataset_meta_marks_builtins_and_points_them_at_data_dir():
    meta = tools.dataset_meta("budget_vs_actuals")
    assert meta["builtin"] is True
    assert meta["dir"] == tools.DATA_DIR


# ---------- An upload resolves through the identical path ----------

def test_an_uploaded_dataset_resolves_and_loads(store, builtin):
    uploads.add_dataset("Board plan.csv", CSV)
    meta = tools.dataset_meta("board_plan")
    assert meta["builtin"] is False
    assert meta["dir"] == uploads.UPLOAD_DIR

    df, source = tools._load("board_plan")
    assert list(df.columns) == ["month", "revenue_eur", "region"]
    # Relative to data/, with forward slashes on every platform.
    assert source == "uploads/board_plan.parquet"


def test_an_uploaded_dataset_cites_with_the_bare_filename_as_its_label(store, builtin):
    """The citation layer needed no change: dataset_record already rsplits."""
    uploads.add_dataset("Board plan.csv", CSV)
    _, source = tools._load("board_plan")
    record = citations.dataset_record(source, tool="query_budget_data")
    assert record["kind"] == "dataset"
    assert record["id"] == "uploads/board_plan.parquet"
    assert record["label"] == "board_plan.parquet"


def test_an_unknown_name_resolves_to_none(store):
    assert tools.dataset_meta("no_such_thing") is None


def test_the_teachable_error_enumerates_builtins_and_uploads(store, builtin):
    uploads.add_dataset("Board plan.csv", CSV)
    message = tools._unknown_dataset("nope")["error"]
    assert "budget_vs_actuals" in message
    assert "board_plan" in message
    assert "Call list_datasets" in message


# ---------- The model reaches an upload through the ordinary query tool ----------

def test_query_budget_data_reads_an_upload(store, builtin):
    uploads.add_dataset("Board plan.csv", CSV)
    result = tools.query_budget_data("board_plan")
    assert result["row_count"] == 2
    assert result["source_file"] == "uploads/board_plan.parquet"
    assert result["rows"][0]["region"] == "Nordics"


def test_filters_and_sorting_work_on_an_upload(store, builtin):
    uploads.add_dataset("Board plan.csv", CSV)
    result = tools.query_budget_data(
        "board_plan", filters=[{"column": "region", "op": "==", "value": "Baltics"}])
    assert result["row_count"] == 1 and result["rows"][0]["revenue_eur"] == 120


def test_group_by_infers_the_summable_columns_for_an_upload(store, builtin):
    """NUMERIC_COLUMNS has no entry for an upload, so they come off the frame."""
    uploads.add_dataset("Board plan.csv", CSV)
    result = tools.query_budget_data("board_plan", group_by=["region"])
    assert "error" not in result
    assert {r["region"] for r in result["rows"]} == {"Nordics", "Baltics"}
    assert result["rows"][0]["revenue_eur"] in (100, 120)


def test_an_unknown_column_on_an_upload_is_still_teachable(store, builtin):
    uploads.add_dataset("Board plan.csv", CSV)
    result = tools.query_budget_data(
        "board_plan", filters=[{"column": "nope", "op": "==", "value": 1}])
    assert "revenue_eur" in result["error"]


def test_the_query_tool_schema_carries_no_dataset_enum():
    """An enum would freeze the set at import and silently exclude every upload."""
    schema = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "query_budget_data")
    assert "enum" not in schema["input_schema"]["properties"]["dataset"]


# ---------- Discovery ----------

def test_list_datasets_reports_uploads_and_documents(store, builtin):
    uploads.add_dataset("Board plan.csv", CSV)
    uploads.add_markdown("Market scan", "Sea freight is up 40%.")

    listed = tools.list_datasets()
    uploaded = [d for d in listed["datasets"] if d.get("uploaded")]
    assert [d["name"] for d in uploaded] == ["board_plan"]
    assert uploaded[0]["upload_id"]              # the Remove button needs it
    assert uploaded[0]["rows"] == 2

    builtins = [d for d in listed["datasets"] if not d.get("uploaded")]
    assert all("upload_id" not in d for d in builtins)   # built-ins are undeletable

    assert [d["name"] for d in listed["documents"]] == ["market_scan"]
    assert listed["documents"][0]["origin"] == "research"


def test_list_datasets_keeps_every_pre_existing_key(store, builtin):
    """The uploads addition is purely additive — nothing downstream moves."""
    listed = tools.list_datasets()
    for key in ("datasets", "watchlist_drivers", "locked_assumptions",
                "scenarios", "notes", "source_file"):
        assert key in listed


def test_load_never_raises_value_error_when_the_roots_diverge(store, builtin, monkeypatch):
    """uploads.UPLOAD_DIR comes from its own module constant, so it can sit
    outside tools.DATA_DIR. relative_to would raise ValueError, which none of
    _load's callers catch — they guard FileNotFoundError/OSError."""
    uploads.add_dataset("Board plan.csv", CSV)
    monkeypatch.setattr(tools, "DATA_DIR", builtin / "somewhere-else")
    df, source = tools._load("board_plan")
    assert len(df) == 2
    assert source == "board_plan.parquet"        # degrades to the bare filename
    assert citations.dataset_record(source)["label"] == "board_plan.parquet"


def test_a_missing_upload_file_says_how_to_fix_it(store, builtin):
    record = uploads.add_dataset("Board plan.csv", CSV)
    (store / "uploads" / "board_plan.csv").unlink()
    (store / "uploads" / "board_plan.parquet").unlink()
    entry = next(d for d in tools.list_datasets()["datasets"]
                 if d["name"] == record["name"])
    assert "Data ingestion page" in entry["error"]


# ---------- read_document ----------

def test_read_document_returns_the_markdown(store):
    uploads.add_markdown("Market scan", "Sea freight is up 40%.")
    result = tools.read_document("market_scan")
    assert result["content"] == "Sea freight is up 40%."
    assert result["source_file"] == "Market scan.md"
    assert result["format"] == "md"


def test_read_document_enumerates_what_exists(store):
    uploads.add_markdown("Market scan", "body text")
    assert "market_scan" in tools.read_document("nope")["error"]


def test_read_document_says_so_when_nothing_has_been_added(store):
    assert "No documents have been added" in tools.read_document("nope")["error"]


def test_read_document_refuses_a_dataset(store, builtin):
    uploads.add_dataset("Board plan.csv", CSV)
    assert "Unknown document" in tools.read_document("board_plan")["error"]


def test_a_missing_cache_entry_is_teachable_not_an_exception(store):
    record = uploads.add_markdown("Market scan", "body text")
    (ingest.CACHE_DIR / f"{record['cache_key']}.md").unlink()
    assert "add it again" in tools.read_document("market_scan")["error"]


def test_read_document_is_reachable_through_execute_tool(store):
    uploads.add_markdown("Market scan", "Sea freight is up 40%.")
    assert tools.execute_tool("read_document", {"name": "market_scan"})["content"] \
        == "Sea freight is up 40%."
