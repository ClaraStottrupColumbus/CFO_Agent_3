# reporting.py — Shared session-turn machinery.
#
# The single code path for running an agent turn against a stored session, used
# by the chat SSE endpoint, the setup proposal stream, startup report
# generation and the scheduler. It exists outside main.py purely so tasks.py and
# alerts.py can run agent turns without importing main.py — the import
# discipline that keeps the circular dependency from forming.
#
# Extended from the reference in two ways, both from §3: sources are persisted
# PER MESSAGE as well as at session level, and summarized reasoning is persisted
# as a UI-only field alongside charts.

from __future__ import annotations

import threading
from datetime import datetime, timezone

from . import store
from .agent import REPORT_PROMPTS, run_agent

# Cap on concurrent BACKGROUND agent runs. Interactive chat never acquires this —
# user requests must not queue behind an 03:00 market scan.
AGENT_SEMAPHORE = threading.BoundedSemaphore(2)

# Summarized reasoning is frequently longer than the answer; cap it so session
# JSON doesn't balloon.
MAX_REASONING_CHARS = 20_000

# The two session kinds that run on the heavy model. A report is the one thing
# this app produces that somebody has to defend line by line, and it is written
# once per period rather than once per question — so it is worth the expensive
# answer, and chat is not.
HEAVY_KINDS = ("weekly", "monthly")


def model_for_session(session: dict, params: dict) -> str | None:
    """Which of the two configured models this session's turns run on.

    The decision lives HERE, next to the session, rather than at each call site.
    run_session_turn already holds the session, so the tier cannot be forgotten
    by a caller — and there are four callers, one of which is the scheduler,
    which could not have resolved it itself (see config.get_run_params).

    The `parent_id` clause is not defensive, it is half the rule. A chat thread
    under a report is created with the REPORT'S OWN KIND — `startThread` and
    `resolveTargetSession` both call apiCreateSession(view.kind, reportId) — so
    a follow-up question about a market scan is a `weekly` session too, and kind
    alone would put every one of them on the expensive model. Only the report
    itself, which has no parent, is heavy. `is_conversation` two functions down
    tells the same two records apart the same way.

    Falls back to params["model"] when no heavy model is configured, so a params
    dict built before the split — or by a test — still runs.
    """
    if session.get("kind") in HEAVY_KINDS and not session.get("parent_id"):
        return params.get("model_heavy") or params.get("model")
    return params.get("model")


def period_key(kind: str) -> str:
    """Current period identifier: ISO week for weekly, year-month for monthly.
    Needs no change from the reference — these are exactly the right grains for
    a market scan and a budget revision."""
    now = datetime.now(timezone.utc)
    if kind == "weekly":
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return now.strftime("%Y-%m")


def derive_title(text: str) -> str:
    t = " ".join((text or "").split())
    if len(t) > 52:
        t = t[:52].rstrip() + "…"
    return t or "New chat"


def build_user_content(message: str, attachments: list | None):
    """Return (api_content, display_attachments)."""
    if not attachments:
        return message, []
    blocks = []
    if message:
        blocks.append({"type": "text", "text": message})
    display = []
    for att in attachments:
        att = att.model_dump() if hasattr(att, "model_dump") else dict(att)
        name, kind = att.get("name", "file"), att.get("kind")
        if kind == "image":
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": att.get("media_type") or "image/png",
                "data": att.get("data", "")}})
        elif kind == "document":
            blocks.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": att.get("data", "")}})
        elif kind == "text":
            blocks.append({"type": "text",
                           "text": f"Attached file: {name}\n```\n{att.get('data', '')}\n```"})
        display.append({"name": name, "kind": kind})
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks, display


def run_session_turn(session: dict, message: str, params: dict,
                     attachments: list | None = None, *, setup: bool = False,
                     profile: dict | None = None, volatile: str | None = None):
    """Append a user message, run the agent, yield events, persist the reply.

    `params` is the dict from config.get_run_params(): {model, model_heavy,
    reasoning, effort, research}. Passed through rather than read here, so this
    module never imports config and stays callable from tests — the one field
    it does interpret is the model, via model_for_session, because picking
    between the two tiers needs the session and config does not have it.
    """
    is_first_user = not any(m.get("role") == "user" for m in session["messages"])
    content, display = build_user_content(message, attachments)
    user_msg = {"role": "user", "content": content}
    if display:
        user_msg["attachments"] = display   # UI-only, stripped before the API call
        user_msg["text"] = message
    session["messages"].append(user_msg)

    is_conversation = session.get("kind") == "chat" or session.get("parent_id")
    if is_conversation and is_first_user and session.get("title") in (None, "New chat"):
        basis = message or (display[0]["name"] if display else "")
        session["title"] = derive_title(basis)

    # A chat thread under a report gets the report's generation turn as context
    # without storing it on the thread.
    effective = session["messages"]
    if session.get("parent_id"):
        parent = store.get_session(session["parent_id"])
        if parent and parent.get("messages"):
            effective = parent["messages"][:2] + session["messages"]

    assistant_text = ""
    reasoning_text = ""
    turn_sources: list[str] = []
    turn_records: list[dict] = []
    turn_charts: list[dict] = []

    for event in run_agent(effective,
                           model=model_for_session(session, params),
                           reasoning=bool(params.get("reasoning", True)),
                           effort=params.get("effort"),
                           research=params.get("research"),
                           profile=profile, setup=setup, volatile=volatile):
        etype = event.get("type")
        if etype == "text":
            assistant_text += event["text"]
        elif etype == "reasoning":
            if len(reasoning_text) < MAX_REASONING_CHARS:
                reasoning_text += event.get("text") or ""
        elif etype == "chart":
            if event.get("spec"):
                turn_charts.append(event["spec"])
        elif etype == "sources":
            turn_sources = event.get("sources", [])
            turn_records = event.get("records", [])
        elif etype == "done":
            if assistant_text:
                msg = {"role": "assistant", "content": assistant_text}
                # UI-only metadata, stripped before the next API call. Citations
                # belong to the turn that made them: a session-level array turns
                # into a thirty-chip soup pinned to whichever assistant message
                # happens to be last.
                if turn_charts:
                    msg["charts"] = turn_charts
                if turn_records:
                    msg["sources"] = turn_records
                if reasoning_text.strip():
                    msg["reasoning"] = reasoning_text[:MAX_REASONING_CHARS]
                session["messages"].append(msg)
            # Still union into the session for backward compatibility.
            for s in turn_sources:
                if s not in session["sources"]:
                    session["sources"].append(s)
            existing = {(r.get("kind"), r.get("id"))
                        for r in session.get("source_records") or []}
            records = list(session.get("source_records") or [])
            for r in turn_records:
                if (r.get("kind"), r.get("id")) not in existing:
                    existing.add((r.get("kind"), r.get("id")))
                    records.append(r)
            session["source_records"] = records
            store.save_session(session)
        yield event


def create_and_generate_report(kind: str, params: dict, *, profile: dict | None = None,
                               volatile: str | None = None) -> dict:
    """Create this period's report session and generate it. One report per
    period; users start chat threads under it rather than duplicating it."""
    period = period_key(kind)
    title = f"{_kind_noun(kind)} · {period}"
    session = store.create_session(kind, title=title, period=period)
    for _ in run_session_turn(session, REPORT_PROMPTS[kind], params,
                              profile=profile, volatile=volatile):
        pass
    return store.get_session(session["id"]) or session


def _kind_noun(kind: str) -> str:
    return {"weekly": "Market scan", "monthly": "Budget revision"}.get(kind, kind.capitalize())


def ensure_report(kind: str, params: dict, *, profile: dict | None = None,
                  volatile: str | None = None) -> None:
    """Startup: ensure a report exists for the current period, reusing an
    existing one so a restart within the same week doesn't burn tokens."""
    if store.find_by_period(kind, period_key(kind)):
        return
    create_and_generate_report(kind, params, profile=profile, volatile=volatile)


def run_agent_to_text(messages: list[dict], params: dict, *,
                      profile: dict | None = None) -> tuple[str, str | None]:
    """Drain run_agent without streaming: returns (assistant_text, error_or_None).

    Used by background callers that need only the final text — the urgency
    narrative and the assumption refresh, neither of which creates a session.

    No session means no tier to pick, so these run on params["model"], the
    general one. That is the right answer rather than a limitation: all three
    callers are short single-shot turns, and two of them already fall back to
    deterministic text when the API is unreachable.
    """
    text, error = "", None
    for event in run_agent(messages,
                           model=params.get("model"),
                           reasoning=bool(params.get("reasoning", False)),
                           effort=params.get("effort"),
                           research=params.get("research"),
                           profile=profile):
        etype = event.get("type")
        if etype == "text":
            text += event["text"]
        elif etype == "error" and error is None:
            error = event.get("message") or "Unknown agent error"
    return text, error
