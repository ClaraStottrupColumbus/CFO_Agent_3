# The upload registry. Path constants are monkeypatched at a tmp_path, the
# house fixture pattern (see tests/test_rules.py) — which is what keeps these
# tests off the real data directory.

import json
import re

import pytest

from app import ingest, tools, uploads

CSV = b"month,revenue_eur,region\n2026-01,100,Nordics\n2026-02,120,Baltics\n"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOADS_FILE", tmp_path / "user_uploads.json")
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "uploads" / "cache")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)   # converter falls back to raw
    return tmp_path


# ---------- Naming: the path-traversal defence ----------

@pytest.mark.parametrize("filename,expected", [
    ("Budget 2027.xlsx", "budget_2027"),
    ("../../etc/passwd", "passwd"),
    ("weird!!name@@.csv", "weird_name"),
    ("2026-plan.csv", "upload_2026_plan"),      # must start with a letter
    (".hidden", "hidden"),
    ("...", "upload"),
])
def test_slugify_never_yields_a_path_component(filename, expected):
    slug = uploads._slugify(filename)
    assert slug == expected
    assert "/" not in slug and "\\" not in slug and ".." not in slug


@pytest.mark.parametrize("filename", [
    "../../etc/passwd", "..\\..\\windows\\system32", "a/b/c.csv", "C:\\Windows\\x.csv",
    "%2e%2e/x.csv", "con.csv", "-rf.csv", "  spaced  .csv",
])
def test_slugify_output_is_always_a_safe_single_component(filename):
    """Asserted as a PROPERTY rather than a fixed string: Path.stem splits on
    backslashes on Windows and not on POSIX, so the exact slug for a
    backslash-separated name is platform-dependent. The safety of it is not."""
    slug = uploads._slugify(filename)
    assert re.fullmatch(r"[a-z][a-z0-9_]*", slug)
    assert len(slug) <= 48


def test_slugify_caps_the_length():
    assert len(uploads._slugify("a" * 200 + ".csv")) == 48


def test_a_reserved_name_is_never_handed_out():
    assert uploads._unique_name("budget_vs_actuals", []) == "budget_vs_actuals_2"


def test_a_taken_name_gets_a_numeric_suffix():
    assert uploads._unique_name("plan", ["plan", "plan_2"]) == "plan_3"


def test_reserved_names_cover_every_builtin_dataset():
    """uploads.py can't read tools.DATASETS back (tools imports uploads), so the
    tuple is a literal — and this is what stops the two drifting apart."""
    assert set(tools.DATASETS) <= set(uploads.RESERVED_NAMES)


# ---------- Datasets ----------

def test_add_dataset_writes_the_csv_parquet_pair(store):
    record = uploads.add_dataset("Budget 2027.xlsx.csv", CSV)
    assert record["name"] == "budget_2027_xlsx"
    assert record["kind"] == "dataset"
    assert record["rows"] == 2
    assert record["columns"] == ["month", "revenue_eur", "region"]
    assert (store / "uploads" / "budget_2027_xlsx.csv").exists()
    assert (store / "uploads" / "budget_2027_xlsx.parquet").exists()
    assert record["format"] == "parquet"


def test_an_upload_cannot_shadow_a_builtin(store):
    record = uploads.add_dataset("budget_vs_actuals.csv", CSV)
    assert record["name"] == "budget_vs_actuals_2"
    assert record["name"] not in tools.DATASETS


def test_two_uploads_of_the_same_name_get_distinct_names(store):
    a = uploads.add_dataset("plan.csv", CSV)
    b = uploads.add_dataset("plan.csv", CSV)
    assert (a["name"], b["name"]) == ("plan", "plan_2")


def test_an_unparseable_table_raises_before_anything_is_registered(store):
    with pytest.raises(ValueError):
        uploads.add_dataset("empty.csv", b"a,b\n")
    assert uploads.list_uploads() == []


def test_the_registry_is_written_atomically_and_reads_back(store):
    uploads.add_dataset("plan.csv", CSV)
    on_disk = json.loads((store / "user_uploads.json").read_text(encoding="utf-8"))
    assert [u["name"] for u in on_disk] == ["plan"]
    assert not list(store.glob("*.tmp"))


def test_a_corrupt_registry_reads_as_empty_rather_than_raising(store):
    (store / "user_uploads.json").write_text("{ not json", encoding="utf-8")
    assert uploads.list_uploads() == []


# ---------- Documents ----------

def test_add_document_converts_once_and_caches_by_content_hash(store):
    record = uploads.add_document("memo.md", b"# Memo\n\nEBITDA 11.6M")
    assert record["kind"] == "document"
    assert record["origin"] == "upload"
    assert record["cache_key"] == ingest.cache_key(b"# Memo\n\nEBITDA 11.6M")
    assert ingest.cached_markdown(record["cache_key"]).startswith("Document: memo.md")


def test_a_scanned_pdf_is_refused_with_a_route_out(store, monkeypatch):
    monkeypatch.setattr(ingest, "convert_cached",
                        lambda *a, **k: ingest.SCANNED_PDF)
    with pytest.raises(ValueError) as exc:
        uploads.add_document("scan.pdf", b"%PDF-fake")
    assert "Attach it directly in a chat instead" in str(exc.value)
    assert uploads.list_uploads() == []


def test_add_markdown_keys_its_cache_on_the_record_id_not_the_content(store):
    """Two saved answers with identical text must not share one cache file, or
    deleting either would break the other."""
    a = uploads.add_markdown("Market scan", "Sea freight is up 40%.")
    b = uploads.add_markdown("Market scan", "Sea freight is up 40%.")
    assert a["cache_key"] == a["id"] and b["cache_key"] == b["id"]
    assert a["cache_key"] != b["cache_key"]
    assert a["origin"] == "research"
    assert ingest.cached_markdown(a["id"]) == "Sea freight is up 40%."


def test_add_markdown_refuses_an_empty_body(store):
    with pytest.raises(ValueError):
        uploads.add_markdown("Nothing", "   \n  ")


# ---------- Editing ----------

def test_editing_moves_ownership_of_the_markdown_to_the_record(store):
    record = uploads.add_document("memo.md", b"# Memo\n\nEBITDA 11.6M")
    old_key = record["cache_key"]

    updated = uploads.update_document(record["id"], markdown="# Memo\n\nEBITDA 11.8M")

    assert updated["cache_key"] == record["id"]
    assert ingest.cached_markdown(record["id"]) == "# Memo\n\nEBITDA 11.8M"
    # the entry keyed by the ORIGINAL bytes is gone: the corrected text is now
    # the document, and the source file stays only as provenance.
    assert ingest.cached_markdown(old_key) is None
    assert (store / "uploads" / "memo.md").exists()


def test_editing_a_title_leaves_the_markdown_alone(store):
    record = uploads.add_markdown("Old title", "body text")
    updated = uploads.update_document(record["id"], title="  New   title  ")
    assert updated["title"] == "New title"
    assert ingest.cached_markdown(record["id"]) == "body text"


def test_editing_refuses_an_empty_body_and_a_dataset(store):
    doc = uploads.add_markdown("Note", "body")
    with pytest.raises(ValueError):
        uploads.update_document(doc["id"], markdown="   ")
    dataset = uploads.add_dataset("plan.csv", CSV)
    with pytest.raises(ValueError):
        uploads.update_document(dataset["id"], markdown="anything")


def test_editing_an_unknown_id_raises_key_error(store):
    with pytest.raises(KeyError):
        uploads.update_document("nope", markdown="x")


# ---------- Deleting ----------

def test_delete_is_registry_first_and_takes_the_files_with_it(store):
    record = uploads.add_dataset("plan.csv", CSV)
    assert uploads.delete_upload(record["id"]) is True
    assert uploads.list_uploads() == []
    assert not (store / "uploads" / "plan.csv").exists()
    assert not (store / "uploads" / "plan.parquet").exists()


def test_deleting_a_document_removes_its_cached_markdown(store):
    record = uploads.add_document("memo.md", b"# Memo\n\nbody text here")
    uploads.delete_upload(record["id"])
    assert ingest.cached_markdown(record["cache_key"]) is None


def test_deleting_one_copy_does_not_break_its_twin(store):
    """The cache is keyed by the SOURCE bytes, so adding the same file twice —
    under any two names — gives both records one shared markdown file. Unlinking
    it on the first delete used to silently break the second."""
    raw = b"# Memo\n\nEBITDA 11.6M and more body text"
    first = uploads.add_document("memo.md", raw)
    second = uploads.add_document("renamed copy.md", raw)
    assert first["cache_key"] == second["cache_key"]     # same bytes, one cache file

    uploads.delete_upload(second["id"])
    assert ingest.cached_markdown(first["cache_key"]) is not None

    # ...and once the last reference goes, so does the file.
    uploads.delete_upload(first["id"])
    assert ingest.cached_markdown(first["cache_key"]) is None


def test_editing_one_copy_leaves_its_twin_readable(store):
    """update_document already re-keys onto the record id, so the twin keeps the
    shared entry — this pins that the unlink of the old key respects it too."""
    raw = b"# Memo\n\nEBITDA 11.6M and more body text"
    first = uploads.add_document("memo.md", raw)
    second = uploads.add_document("renamed copy.md", raw)

    uploads.update_document(second["id"], markdown="# Memo\n\ncorrected")
    assert ingest.cached_markdown(first["cache_key"]) is not None
    assert ingest.cached_markdown(second["id"]) == "# Memo\n\ncorrected"


def test_deleting_survives_a_file_that_is_already_gone(store):
    record = uploads.add_dataset("plan.csv", CSV)
    (store / "uploads" / "plan.csv").unlink()
    (store / "uploads" / "plan.parquet").unlink()
    assert uploads.delete_upload(record["id"]) is True
    assert uploads.list_uploads() == []


def test_deleting_an_unknown_id_is_false_not_an_exception(store):
    assert uploads.delete_upload("nope") is False


def test_delete_all_empties_both_the_registry_and_the_directory(store):
    uploads.add_dataset("plan.csv", CSV)
    uploads.add_markdown("Scan", "body text")
    assert uploads.delete_all() == 2
    assert uploads.list_uploads() == []
    assert not (store / "uploads").exists()


# ---------- Lookup ----------

def test_lookups_and_listing(store):
    dataset = uploads.add_dataset("plan.csv", CSV)
    doc = uploads.add_markdown("Scan", "body text")
    assert uploads.dataset_names() == ["plan"]
    assert uploads.get_by_name("plan")["id"] == dataset["id"]
    assert uploads.get_by_id(doc["id"])["name"] == "scan"
    assert uploads.get_by_name("missing") is None
    assert [u["kind"] for u in uploads.list_uploads("document")] == ["document"]
