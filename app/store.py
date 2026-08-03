# store.py — Server-side session store (persistent memory).
# Chat conversations and reports are unified as "sessions" saved as JSON files
# under data/history/{kind}/{id}.json, so they survive server restarts and are
# shared across clients. A report is just a session whose first turn is a preset
# generation prompt — the same code path as chat, so follow-up questions on a
# report reuse the agent exactly like a normal conversation.

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

HISTORY_DIR = Path(__file__).resolve().parent.parent / "data" / "history"
KINDS = ("chat", "weekly", "monthly")

# Serialize writes so concurrent SSE streams / startup generation don't clobber
# each other's files. Reads are cheap JSON loads; a single lock is plenty here.
_lock = threading.Lock()


def _kind_dir(kind: str) -> Path:
    if kind not in KINDS:
        raise ValueError(f"Unknown session kind '{kind}'. Expected one of {KINDS}.")
    d = HISTORY_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(kind: str, session_id: str) -> Path:
    return _kind_dir(kind) / f"{session_id}.json"


def _find_path(session_id: str) -> Path | None:
    """Locate a session file across all kinds (ids are globally unique)."""
    for kind in KINDS:
        p = _path(kind, session_id)
        if p.exists():
            return p
    return None


def create_session(kind: str, title: str | None = None, period: str | None = None,
                   parent_id: str | None = None) -> dict:
    """Create and persist a new empty session, returning the full record.

    `parent_id` links a chat thread to its parent report (folder structure): a
    weekly/monthly report is a top-level session (parent_id=None); the chats a
    user starts under it are child sessions carrying that report's id.
    """
    now = time.time()
    session = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "title": title or _default_title(kind),
        "period": period,
        "parent_id": parent_id,
        "created_at": now,
        "updated_at": now,
        "messages": [],   # [{role: "user"|"assistant", content: str}]
        "sources": [],    # accumulated dataset source files
    }
    save_session(session)
    return session


def _default_title(kind: str) -> str:
    return {"chat": "New chat", "weekly": "Weekly report", "monthly": "Monthly report"}.get(kind, "Session")


def save_session(session: dict) -> dict:
    """Persist a session record (atomically) and bump updated_at."""
    session["updated_at"] = time.time()
    p = _path(session["kind"], session["id"])
    with _lock:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(session, indent=2))
        tmp.replace(p)
    return session


def get_session(session_id: str) -> dict | None:
    """Load a full session record by id, or None if it doesn't exist."""
    p = _find_path(session_id)
    if not p:
        return None
    with _lock:
        return json.loads(p.read_text())


def delete_session(session_id: str) -> bool:
    p = _find_path(session_id)
    if not p:
        return False
    with _lock:
        p.unlink()
    return True


def list_sessions(kind: str) -> list[dict]:
    """Return lightweight summaries for one kind, newest-updated first."""
    d = _kind_dir(kind)
    out = []
    for p in d.glob("*.json"):
        try:
            s = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": s["id"],
            "kind": s["kind"],
            "title": s.get("title") or _default_title(kind),
            "period": s.get("period"),
            "parent_id": s.get("parent_id"),
            "created_at": s.get("created_at"),
            "updated_at": s.get("updated_at"),
            "message_count": len(s.get("messages", [])),
        })
    out.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return out


def find_by_period(kind: str, period: str) -> dict | None:
    """Find the top-level report of a kind for a given period key (startup dedup).
    Child chat threads carry no period, so this only ever matches report sessions."""
    d = _kind_dir(kind)
    for p in d.glob("*.json"):
        try:
            s = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if s.get("parent_id") is None and s.get("period") == period:
            return s
    return None
