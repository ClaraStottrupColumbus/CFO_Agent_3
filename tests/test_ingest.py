# Ingest is a leaf: pandas plus the standard library, no app imports at module
# level and no API key needed. So most of this file needs no fixtures at all —
# the same property that makes test_export.py fixture-free.

import pandas as pd
import pytest

from app import agent, ingest


# ---------- Classification ----------

@pytest.mark.parametrize("filename,bucket", [
    ("budget.csv", "tabular"),
    ("budget.TSV", "tabular"),
    ("plan.xlsx", "tabular"),
    ("plan.xls", "tabular"),
    ("history.parquet", "tabular"),
    ("memo.pdf", "document"),
    ("memo.docx", "document"),
    ("deck.pptx", "document"),
    ("notes.md", "document"),
    ("config.yaml", "document"),
    ("chart.png", "image"),
    ("photo.JPEG", "image"),
    ("archive.zip", "unsupported"),
    ("no_extension", "unsupported"),
])
def test_classify_buckets(filename, bucket):
    assert ingest.classify(filename) == bucket


def test_supported_note_lists_every_extension():
    note = ingest.supported_note()
    for ext in ingest.SUPPORTED_EXTS:
        assert f".{ext}" in note


# ---------- The Haiku id is shared, not duplicated ----------

def test_converter_model_matches_the_apps_other_haiku_call():
    """Two hardcoded Haiku ids in one app is one that silently goes stale."""
    assert ingest.CONVERTER_MODEL == agent.REASONING_SUMMARY_MODEL


# ---------- Tabular ----------

def test_read_table_normalises_column_names():
    df = ingest.read_table("x.csv", b" month , revenue \n2026-01,100\n")
    assert list(df.columns) == ["month", "revenue"]


def test_read_table_rejects_an_empty_frame_with_a_user_facing_message():
    with pytest.raises(ValueError) as exc:
        ingest.read_table("empty.csv", b"month,revenue\n")
    assert "no rows or no columns" in str(exc.value)
    assert "empty.csv" in str(exc.value)


def test_read_table_names_the_file_when_it_cannot_be_parsed():
    with pytest.raises(ValueError) as exc:
        ingest.read_table("broken.parquet", b"not a parquet file")
    assert "broken.parquet" in str(exc.value)


def test_first_of_month_datetimes_become_the_YYYY_MM_convention():
    """Every period helper in tools.py, budget.py and cash.py matches on the
    'YYYY-MM' string, and a raw datetime json-serialises to epoch millis."""
    df = pd.DataFrame({"month": pd.to_datetime(["2026-01-01", "2026-02-01"])})
    assert list(ingest._stringify_dates(df)["month"]) == ["2026-01", "2026-02"]


def test_finer_grained_datetimes_keep_their_day():
    df = pd.DataFrame({"booked": pd.to_datetime(["2026-01-04", "2026-02-17"])})
    assert list(ingest._stringify_dates(df)["booked"]) == ["2026-01-04", "2026-02-17"]


def test_non_datetime_columns_are_untouched():
    df = pd.DataFrame({"month": ["2026-01"], "revenue": [100]})
    out = ingest._stringify_dates(df.copy())
    assert list(out["month"]) == ["2026-01"] and list(out["revenue"]) == [100]


def test_describe_table_reports_shape_and_month_coverage():
    df = pd.DataFrame({"month": ["2026-01", "2026-06"], "revenue": [1, 2]})
    text = ingest.describe_table(df, "plan.xlsx")
    assert "2 rows" in text and "2 columns" in text
    assert "covering 2026-01 to 2026-06" in text


def test_describe_table_says_which_sheet_of_how_many_became_the_dataset():
    df = pd.DataFrame({"a": [1]})
    assert "sheet 'Q1' of 3" in ingest.describe_table(df, "p.xlsx", ["Q1", "Q2", "Q3"])


# ---------- summarise_table (the chat-attachment form) ----------

def test_summarise_table_states_the_truncation_rather_than_hiding_it():
    df = pd.DataFrame({"n": range(ingest.MAX_PREVIEW_ROWS + 10)})
    text = ingest.summarise_table(df, "big.csv")
    assert f"First {ingest.MAX_PREVIEW_ROWS} of {len(df)} rows" in text
    assert "Data ingestion page" in text     # says where to go to query all of it
    assert text.count("\n") < len(df)        # the rest genuinely is not in there


def test_summarise_table_says_full_contents_when_nothing_was_dropped():
    df = pd.DataFrame({"n": [1, 2, 3]})
    text = ingest.summarise_table(df, "small.csv")
    assert "Full contents:" in text
    assert "3 rows x 1 columns" in text


# ---------- extract_text ----------

def test_plain_text_is_decoded_as_is():
    assert ingest.extract_text("notes.md", b"# Board memo\n\nEBITDA 11.6M") \
        .startswith("# Board memo")


def test_undecodable_bytes_are_replaced_rather_than_raising():
    assert ingest.extract_text("notes.txt", b"caf\xff caf\xc3\xa9")


def test_empty_text_is_a_user_facing_refusal():
    with pytest.raises(ValueError) as exc:
        ingest.extract_text("blank.txt", b"   \n  ")
    assert "blank.txt" in str(exc.value)


def test_long_text_is_truncated_with_the_marker_never_silently():
    raw = ("x" * (ingest.MAX_EXTRACT_CHARS + 5_000)).encode()
    text = ingest.extract_text("long.txt", raw)
    assert len(text) > ingest.MAX_EXTRACT_CHARS      # marker appended, not cut off
    assert "[truncated" in text
    assert str(ingest.MAX_EXTRACT_CHARS) in text


# ---------- The converter's deterministic fallback ----------

def test_conversion_falls_back_to_raw_text_with_no_api_key(monkeypatch):
    """A degraded document beats a failed upload — the same stance as
    alerts.py's narrative and budgetplan.py's templated_narrative."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    raw = "| a | b |\n1 2"
    assert ingest.convert_document("x.md", raw) == raw


def test_conversion_falls_back_when_the_call_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(agent, "get_client", boom)
    assert ingest.convert_document("x.md", "the raw text") == "the raw text"


# ---------- The content-hash cache ----------

def test_cache_key_is_the_content_hash_not_the_name():
    """Re-adding the same file under a different name must be free."""
    assert ingest.cache_key(b"same bytes") == ingest.cache_key(b"same bytes")
    assert ingest.cache_key(b"same bytes") != ingest.cache_key(b"other bytes")


def test_convert_cached_writes_once_and_reads_back(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    raw = b"# Memo\n\nEBITDA 11.6M"

    first = ingest.convert_cached("memo.md", raw)
    assert first.startswith("Document: memo.md")
    cached = list((tmp_path / "cache").glob("*.md"))
    assert len(cached) == 1

    # A second call under a DIFFERENT name hits the same entry — the key is the
    # bytes — and adds no second file.
    assert ingest.convert_cached("renamed.md", raw) == first
    assert len(list((tmp_path / "cache").glob("*.md"))) == 1


def test_cached_markdown_is_none_for_a_missing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    assert ingest.cached_markdown("nothing-here") is None


def test_a_failed_cache_write_does_not_fail_the_conversion(tmp_path, monkeypatch):
    """The cache is an optimisation, never a hard requirement."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("i am a file")
    monkeypatch.setattr(ingest, "CACHE_DIR", blocked / "cache")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "Memo" in ingest.convert_cached("memo.md", b"# Memo\n\nbody text")
