# citations.py — Block classification and source records. Pure, no I/O, no app imports.
#
# Data transparency is the requirement that routed web ingestion through
# Anthropic's server-side tools, and this module is the testable core of that.
# It answers three questions about one assistant turn's content blocks:
#
#   1. Which blocks are LOCAL tool calls the loop must execute? (server_tool_use,
#      web_search_tool_result and web_fetch_tool_result never are — dispatching
#      one locally is the bug this filter exists to make impossible.)
#   2. What sources did the turn use, as one deduped, ordered list?
#   3. Which URLs were actually visited? That set is the input to the provenance
#      guard in drivers.py, which is why it is returned from here rather than
#      accumulated inline in the loop: both sides then go through the SAME
#      normalise_url, and a bug in it fails a test instead of a demo.
#
# Everything accepts either plain dicts (tests) or SDK objects (the loop), since
# the loop compares block.type as a string and never depends on SDK typing.

from __future__ import annotations

from collections import OrderedDict
from datetime import date, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking parameters that identify a *visit*, not a *page*. Left in, a page
# that is both searched and fetched yields two chips for one source — and, worse,
# the provenance guard stops recognising its own visited URLs.
TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = {"ref", "ref_src", "referrer", "fbclid", "gclid", "mc_cid", "mc_eid",
                   "igshid", "s_kwcid", "_hsenc", "_hsmi"}

# Block types that arrive from the server-side web tools. Never dispatched locally.
SERVER_BLOCK_TYPES = ("server_tool_use", "web_search_tool_result", "web_fetch_tool_result")


def _get(obj, key, default=None):
    """Read a field from either a dict fixture or an SDK response object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def normalise_url(url: str | None) -> str:
    """Canonical form of a URL for dedup and provenance comparison.

    Lowercases scheme and host, drops the fragment, strips a trailing slash and
    removes tracking parameters. Deliberately conservative: it never touches the
    path's casing (many servers are case-sensitive) and never drops meaningful
    query parameters.
    """
    if not url:
        return ""
    raw = str(url).strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()

    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and not ((scheme == "http" and parts.port == 80)
                           or (scheme == "https" and parts.port == 443)):
        host = f"{host}:{parts.port}"

    path = parts.path or ""
    while path.endswith("/") and len(path) > 1:
        path = path[:-1]
    if path == "/":
        path = ""

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS
            and not any(k.lower().startswith(p) for p in TRACKING_PREFIXES)]
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, host, path, query, ""))   # fragment always dropped


# ---------- Source records ----------

def dataset_record(source_file: str, tool: str | None = None) -> dict:
    """A source record for an internal dataset file."""
    return {"kind": "dataset", "id": str(source_file),
            "label": str(source_file).rsplit("/", 1)[-1], "tool": tool}


def web_record(url: str, *, title: str | None = None, accessed: str | None = None,
               via: str = "web_search", snippet: str | None = None) -> dict:
    """A source record for a web page. `id` is the normalised URL — the dedup key.

    `encrypted_content` from a search result is never stored: it is opaque, large,
    and useless to every consumer of this record.
    """
    return {"kind": "web", "id": normalise_url(url), "url": str(url or ""),
            "title": title or None, "accessed": accessed or _today(),
            "via": via, "snippet": snippet}


def _today() -> str:
    return date.today().isoformat()


def record_key(record: dict) -> tuple:
    return (record.get("kind"), record.get("id"))


def merge_records(records: list[dict]) -> list[dict]:
    """Dedup on (kind, id), preserving first-appearance order.

    When the same URL arrives twice — searched, then fetched — the records are
    merged rather than the second dropped: `web_fetch` wins as the `via` (we
    actually read that page), and any title, accessed date or snippet missing
    from the first is filled from the second.
    """
    out: OrderedDict[tuple, dict] = OrderedDict()
    for rec in records or []:
        key = record_key(rec)
        if key not in out:
            out[key] = dict(rec)
            continue
        existing = out[key]
        if rec.get("via") == "web_fetch":
            existing["via"] = "web_fetch"
            if rec.get("accessed"):
                existing["accessed"] = rec["accessed"]
        for field in ("title", "snippet", "url", "tool"):
            if not existing.get(field) and rec.get(field):
                existing[field] = rec[field]
    return list(out.values())


def display_label(record: dict) -> str:
    """The short string persisted in session["sources"] for backward compatibility."""
    if record.get("kind") == "dataset":
        return record.get("label") or record.get("id") or ""
    return record.get("title") or record.get("url") or record.get("id") or ""


# ---------- Block classification ----------

def classify_blocks(content_blocks) -> dict:
    """Classify one assistant turn's content blocks.

    Returns:
      local_tool_uses — blocks the loop must execute locally (type == "tool_use").
                        Server tool blocks are NEVER included; that is the whole
                        point of filtering positively rather than guarding
                        negatively on stop_reason.
      source_records  — deduped, ordered web source records from this turn.
      fetched_urls    — set of NORMALISED URLs actually visited this turn. The
                        provenance guard's input.
      web_errors      — [{tool, error_code}] from failed server tool calls.
      research        — [{tool, query|url}] from server_tool_use blocks.
      citations       — [{url, title, cited_text, …}] from cited text blocks.
    """
    local_tool_uses = []
    records: list[dict] = []
    fetched: set[str] = set()
    web_errors: list[dict] = []
    research: list[dict] = []
    citations: list[dict] = []

    for block in content_blocks or []:
        btype = _get(block, "type")

        if btype == "tool_use":
            local_tool_uses.append(block)
            continue

        if btype == "server_tool_use":
            inp = _get(block, "input") or {}
            research.append({
                "tool": _get(block, "name"),
                "query": _get(inp, "query"),
                "url": _get(inp, "url"),
            })
            continue

        if btype == "web_search_tool_result":
            recs, err = _search_results(_get(block, "content"))
            if err:
                web_errors.append({"tool": "web_search", "error_code": err})
            for rec in recs:
                records.append(rec)
                if rec["id"]:
                    fetched.add(rec["id"])
            continue

        if btype == "web_fetch_tool_result":
            rec, err = _fetch_result(_get(block, "content"))
            if err:
                web_errors.append({"tool": "web_fetch", "error_code": err})
            if rec:
                records.append(rec)
                if rec["id"]:
                    fetched.add(rec["id"])
            continue

        if btype == "text":
            for cit in _get(block, "citations") or []:
                url = _get(cit, "url")
                if not url:
                    continue
                citations.append({
                    "url": str(url),
                    "id": normalise_url(url),
                    "title": _get(cit, "title"),
                    "cited_text": _get(cit, "cited_text"),
                })

    return {
        "local_tool_uses": local_tool_uses,
        "source_records": merge_records(records),
        "fetched_urls": fetched,
        "web_errors": web_errors,
        "research": research,
        "citations": citations,
    }


def _error_code(content) -> str | None:
    """A server tool result carries its error as a single OBJECT with an
    error_code, where success carries a LIST of results. Branching on that
    before indexing is what keeps an HTTP-200 tool failure from crashing the loop.
    """
    if content is None or isinstance(content, list):
        return None
    code = _get(content, "error_code")
    if code:
        return str(code)
    ctype = _get(content, "type") or ""
    return str(ctype) if str(ctype).endswith("_error") else None


def _search_results(content) -> tuple[list[dict], str | None]:
    err = _error_code(content)
    if err:
        return [], err
    if not isinstance(content, list):
        return [], None
    out = []
    for item in content:
        url = _get(item, "url")
        if not url:
            continue
        out.append(web_record(url, title=_get(item, "title"), via="web_search"))
    return out, None


def _fetch_result(content) -> tuple[dict | None, str | None]:
    err = _error_code(content)
    if err:
        return None, err
    if content is None:
        return None, None
    url = _get(content, "url")
    if not url:
        return None, None
    return web_record(url, title=_fetch_title(content), via="web_fetch",
                      accessed=_normalise_accessed(_get(content, "retrieved_at"))), None


def _fetch_title(content) -> str | None:
    doc = _get(content, "content")
    return _get(doc, "title") if doc is not None else None


def _normalise_accessed(retrieved_at) -> str | None:
    """web_fetch reports a full timestamp; the UI only ever shows the date."""
    if not retrieved_at:
        return None
    text = str(retrieved_at)
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return text[:10] or None


def records_from_tool_result(name: str, result) -> list[dict]:
    """Source records contributed by one LOCAL tool result — zero or one.

    A tool that returns no `source_file` contributes nothing. That is what keeps
    presentation-only tools like render_chart out of the source list, and it
    must hold for every future presentation-only tool too, so the rule lives
    here rather than as a name check at the call site.
    """
    if not isinstance(result, dict):
        return []
    source_file = result.get("source_file")
    if not source_file:
        return []
    return [dataset_record(source_file, tool=name)]


def upgrade_session_sources(session: dict) -> list[dict]:
    """Source records for a stored session, tolerating pre-upgrade sessions.

    Sessions written before source_records existed carry only `sources`, a list
    of display strings. Those are dataset file names, so they rebuild as dataset
    records and render byte-identically; nothing needs migrating on disk.
    """
    records = (session or {}).get("source_records")
    if records:
        return [dict(r) for r in records]
    return [dataset_record(s) for s in (session or {}).get("sources") or [] if s]


def attach_snippets(records: list[dict], citations: list[dict]) -> list[dict]:
    """Attach each citation's `cited_text` as the snippet on its matching record.

    Done as a post-stream pass over the final message rather than during
    streaming, because cited text arrives split across text blocks and stitching
    it back together mid-stream is awkward for no benefit.
    """
    by_id = {r["id"]: r for r in records if r.get("kind") == "web"}
    for cit in citations or []:
        rec = by_id.get(cit.get("id"))
        if rec is None:
            continue
        if not rec.get("snippet") and cit.get("cited_text"):
            rec["snippet"] = str(cit["cited_text"])[:400]
        if not rec.get("title") and cit.get("title"):
            rec["title"] = cit["title"]
    return records
