# uploads.py — Registry for files the CFO adds on the Data ingestion page.
# Two kinds of upload, both listed here: a "dataset" (a tabular file, stored as
# CSV + Parquet in data/uploads/ so the query tools read it exactly like a
# built-in) and a "document" (pdf/docx/pptx/..., stored as the original bytes
# plus the converted markdown from ingest.py, which the agent reads via the
# read_document tool).
#
# Same shape as store.py / tasks.py: a JSON list written to .tmp then
# Path.replace() under a module lock, tolerant of a missing or corrupt file. A
# scheduler worker and an interactive upload really can write concurrently.
#
# The registry is the source of truth for what exists; file names on disk are
# always DERIVED from it, never taken from user input. That is the whole
# path-traversal defence — see _slugify.

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

import pandas as pd

from . import ingest

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UPLOADS_FILE = DATA_DIR / "user_uploads.json"
UPLOAD_DIR = DATA_DIR / "uploads"

KINDS = ("dataset", "document")

# An upload must never shadow a built-in dataset name. These ten are hardcoded
# in rules.py, budget.py, budgetplan.py, generate_data.py and the report prompts,
# so silently aliasing one would corrupt all of them.
#
# Deliberately a literal rather than list(tools.DATASETS): tools imports uploads,
# so reading it back here would cycle. tests/test_uploads.py asserts the two
# cannot drift apart.
RESERVED_NAMES = (
    "budget_vs_actuals", "product_lines", "bill_of_materials", "drivers",
    "driver_prices", "driver_forwards", "opex_plan", "working_capital",
    "capex_plan", "budget_overview",
)

_lock = threading.Lock()


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _load_all() -> list[dict]:
    if not UPLOADS_FILE.exists():
        return []
    try:
        return json.loads(UPLOADS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_all(items: list[dict]) -> None:
    UPLOADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = UPLOADS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(UPLOADS_FILE)


def list_uploads(kind: str | None = None) -> list[dict]:
    with _lock:
        items = _load_all()
    if kind:
        items = [u for u in items if u.get("kind") == kind]
    return sorted(items, key=lambda u: u.get("uploaded_at") or 0, reverse=True)


def get_by_name(name: str) -> dict | None:
    with _lock:
        return next((u for u in _load_all() if u.get("name") == name), None)


def get_by_id(upload_id: str) -> dict | None:
    with _lock:
        return next((u for u in _load_all() if u.get("id") == upload_id), None)


def dataset_names() -> list[str]:
    return [u["name"] for u in list_uploads("dataset")]


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

def _slugify(filename: str) -> str:
    """File name -> a safe dataset/document name: lowercase, [a-z0-9_] only.

    The result is used as a dataset name the model passes to the query tools AND
    as a path component under data/uploads/, so it must be typeable and must
    never contain a path separator, a dot or a leading dash. Untrusted input
    reaches the filesystem exactly once, here, and only after this.
    """
    stem = Path(filename).stem.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug or not re.match(r"^[a-z]", slug):
        slug = f"upload_{slug}" if slug else "upload"
    return slug[:48]


def _unique_name(base: str, existing: list[str]) -> str:
    taken = set(existing) | set(RESERVED_NAMES)
    if base not in taken:
        return base
    for n in range(2, 100):
        candidate = f"{base}_{n}"
        if candidate not in taken:
            return candidate
    return f"{base}_{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------
# Creating
# --------------------------------------------------------------------------

def add_dataset(filename: str, raw: bytes) -> dict:
    """Register a tabular upload as a queryable dataset.

    Written as both CSV and Parquet — the same pair generate_data.py produces —
    so tools._load's parquet-preferred/CSV-fallback path needs no special case.
    Raises ValueError with a user-facing message if the file can't be parsed.
    """
    df = ingest.read_table(filename, raw)
    sheets = ingest.sheet_names(filename, raw)
    with _lock:
        items = _load_all()
        name = _unique_name(_slugify(filename), [u["name"] for u in items])
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = UPLOAD_DIR / f"{name}.csv"
        parquet_path = UPLOAD_DIR / f"{name}.parquet"
        _atomic_frame_write(df, csv_path, parquet_path)
        record = {
            "id": uuid.uuid4().hex,
            "name": name,
            "kind": "dataset",
            "original_filename": filename,
            "stored_file": csv_path.name,
            "format": "parquet" if parquet_path.exists() else "csv",
            "description": ingest.describe_table(df, filename, sheets),
            "columns": [str(c) for c in df.columns],
            "rows": int(len(df)),
            "sheets": sheets,
            "cache_key": None,
            "uploaded_at": time.time(),
        }
        items.append(record)
        _save_all(items)
    return record


def add_document(filename: str, raw: bytes, model: str | None = None) -> dict:
    """Register a document upload: store the bytes, convert once, register.

    The conversion (ingest.convert_cached) happens HERE, at upload time, so the
    agent's read_document call is always fast and no answer stalls on it.
    """
    kwargs = {"model": model} if model else {}
    markdown = ingest.convert_cached(filename, raw, **kwargs)
    if markdown == ingest.SCANNED_PDF:
        raise ValueError(
            f"'{filename}' looks like a scanned PDF with no extractable text. "
            "Attach it directly in a chat instead — the agent can read the pages as images."
        )
    ext = ingest.extension(filename)
    with _lock:
        items = _load_all()
        name = _unique_name(_slugify(filename), [u["name"] for u in items])
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        stored = UPLOAD_DIR / f"{name}.{ext}" if ext else UPLOAD_DIR / name
        tmp = stored.with_name(stored.name + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(stored)
        record = {
            "id": uuid.uuid4().hex,
            "name": name,
            "kind": "document",
            "original_filename": filename,
            "stored_file": stored.name,
            "format": ext or "txt",
            "description": f"Uploaded document {filename}, "
                           f"{len(markdown.splitlines())} lines of converted text.",
            "columns": [],
            "rows": 0,
            "sheets": [],
            "cache_key": ingest.cache_key(raw),
            "origin": "upload",
            "title": filename,
            "uploaded_at": time.time(),
        }
        items.append(record)
        _save_all(items)
    return record


def add_markdown(title: str, markdown: str, origin: str = "research") -> dict:
    """Register markdown the user owns — an answer saved from a chat, or a note
    written here — as a document, with no conversion step.

    An uploaded document's markdown is addressed by the content hash of its
    source bytes. Markdown created here has no source bytes, and hashing the text
    itself would make two documents with identical content share one file (so
    deleting either would break the other). The record's own id is unique and
    already permanent, so it doubles as the key: ingest.cached_markdown,
    tools.read_document and _remove_files all treat the key as an opaque file
    name, and need no special case for these.
    """
    markdown = (markdown or "").strip()
    if not markdown:
        raise ValueError("The document is empty.")
    title = " ".join((title or "").split()) or "Saved answer"
    with _lock:
        items = _load_all()
        name = _unique_name(_slugify(title), [u["name"] for u in items])
        upload_id = uuid.uuid4().hex
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        stored = UPLOAD_DIR / f"{name}.md"
        tmp = stored.with_name(stored.name + ".tmp")
        tmp.write_text(markdown, encoding="utf-8")
        tmp.replace(stored)
        _write_markdown(upload_id, markdown)
        record = {
            "id": upload_id,
            "name": name,
            "kind": "document",
            "original_filename": f"{title}.md",
            "stored_file": stored.name,
            "format": "md",
            "description": _markdown_description(title, markdown, origin),
            "columns": [],
            "rows": 0,
            "sheets": [],
            "cache_key": upload_id,
            "origin": origin,
            "title": title,
            "uploaded_at": time.time(),
        }
        items.append(record)
        _save_all(items)
    return record


def update_document(upload_id: str, markdown: str | None = None,
                    title: str | None = None) -> dict:
    """Rewrite an existing document's markdown and/or its title.

    Editing is destructive to the conversion cache on purpose: once a person has
    corrected the text, their version IS the document, so the entry keyed by the
    original file's bytes is dropped and the record takes ownership of its
    markdown (keyed by record id, as in add_markdown). The original file stays on
    disk untouched — it is the provenance, not the content.

    Correcting a bad extraction here fixes every future answer that cites it,
    with no re-upload.
    """
    with _lock:
        items = _load_all()
        record = next((u for u in items if u["id"] == upload_id), None)
        if not record:
            raise KeyError("Upload not found.")
        if record.get("kind") != "document":
            raise ValueError("Only documents can be edited.")
        if markdown is not None:
            text = markdown.strip()
            if not text:
                raise ValueError("The document can't be empty.")
            old_key = record.get("cache_key")
            _write_markdown(upload_id, text)
            record["cache_key"] = upload_id
            # Drop the entry keyed by the original bytes — but only if no OTHER
            # record still points at it. The same file added twice shares one
            # cache file, and editing one copy must not blank the other. Same
            # check as _remove_files; see its docstring.
            if old_key and old_key != upload_id and old_key not in _cache_keys(items):
                (ingest.CACHE_DIR / f"{old_key}.md").unlink(missing_ok=True)
            # A document created from markdown keeps its own file in sync too.
            if record.get("format") == "md" and record.get("stored_file"):
                stored = UPLOAD_DIR / record["stored_file"]
                if stored.exists():
                    tmp = stored.with_name(stored.name + ".tmp")
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(stored)
            record["description"] = _markdown_description(
                title or record.get("title") or record["name"], text,
                record.get("origin", "upload"))
        if title is not None:
            clean = " ".join(title.split())
            if not clean:
                raise ValueError("Title can't be empty.")
            record["title"] = clean
        record["updated_at"] = time.time()
        _save_all(items)
    return record


def _write_markdown(upload_id: str, markdown: str) -> None:
    """Write a record-owned markdown body to its cache path (tmp + replace)."""
    ingest.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = ingest.CACHE_DIR / f"{upload_id}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(markdown, encoding="utf-8")
    tmp.replace(path)


def _markdown_description(title: str, markdown: str, origin: str) -> str:
    lead = "Saved answer" if origin == "research" else "Document"
    return f"{lead} “{title}”, {len(markdown.splitlines())} lines of markdown."


def _atomic_frame_write(df: pd.DataFrame, csv_path: Path, parquet_path: Path) -> None:
    """Write a frame as CSV + Parquet via tmp+replace, so a reader never sees a
    half-written file. Parquet is best-effort: a frame with mixed-type columns
    can fail to serialise, and tools._load's CSV fallback covers that — one lost
    format beats a failed upload."""
    csv_tmp = csv_path.with_suffix(".csv.tmp")
    df.to_csv(csv_tmp, index=False)
    csv_tmp.replace(csv_path)
    try:
        parquet_tmp = parquet_path.with_suffix(".parquet.tmp")
        df.to_parquet(parquet_tmp, index=False)
        parquet_tmp.replace(parquet_path)
    except Exception:
        parquet_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Removing
# --------------------------------------------------------------------------

def _remove_files(record: dict, keep_keys: set[str] | None = None) -> None:
    """Delete one record's files. `keep_keys` is the set of cache keys STILL
    referenced by surviving records, and it is not optional in spirit.

    A converted upload's cache file is keyed by the hash of its source bytes, so
    adding the same file twice — under any two names — gives both records the
    same key and one shared markdown file. Unlinking it on the first delete would
    silently break the second, which is the exact hazard add_markdown's docstring
    describes and avoids for record-owned text. Datasets and stored source files
    are named from the unique slug and cannot be shared, so only the cache needs
    the check.
    """
    name = record.get("name", "")
    stored = record.get("stored_file")
    if stored:
        (UPLOAD_DIR / stored).unlink(missing_ok=True)
    if record.get("kind") == "dataset" and name:
        (UPLOAD_DIR / f"{name}.csv").unlink(missing_ok=True)
        (UPLOAD_DIR / f"{name}.parquet").unlink(missing_ok=True)
    key = record.get("cache_key")
    if key and key not in (keep_keys or set()):
        (ingest.CACHE_DIR / f"{key}.md").unlink(missing_ok=True)


def _cache_keys(items: list[dict]) -> set[str]:
    return {u["cache_key"] for u in items if u.get("cache_key")}


def delete_upload(upload_id: str) -> bool:
    """Registry first, then the files — never the reverse. A crash mid-delete
    then leaves an orphaned file rather than a record pointing at nothing."""
    with _lock:
        items = _load_all()
        record = next((u for u in items if u["id"] == upload_id), None)
        if not record:
            return False
        items.remove(record)
        _save_all(items)
        survivors = _cache_keys(items)
    _remove_files(record, survivors)
    return True


def delete_all() -> int:
    """Remove every upload and its files — the factory-reset path."""
    with _lock:
        items = _load_all()
        _save_all([])
    for record in items:
        _remove_files(record, set())      # nothing survives, so nothing is kept
    if UPLOAD_DIR.exists():
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    return len(items)
