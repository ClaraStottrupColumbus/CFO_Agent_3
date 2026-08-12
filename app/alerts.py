# alerts.py — Persisted alerts and the scan that produces them.
#
# Detection is deterministic (rules.py); only the narrative needs the API, and
# it falls back to the finding title so alert creation never depends on the
# network.
#
# Dedup is WINDOWED with magnitude bucketing, not permanent-per-period. The
# reference dedups forever on rule:entity:period, which is wrong here: a driver
# legitimately re-breaches — wheat crosses +10% in week 1 and +18% in week 3,
# and the escalation IS the news — so a permanent key would swallow the second
# alert entirely. Per-rule config carries the window and whether the key
# includes the period, so the reference's permanent-per-period behaviour is
# still reproducible through the same code path where it is the right answer.

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from . import reporting, rules
from .agent import URGENCY_ANALYSIS_PROMPT

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ALERTS_FILE = DATA_DIR / "alerts.json"
LOG_FILE = DATA_DIR / "alerts_log.jsonl"

# Cost cap: at most this many agent narratives per scan; the rest are reported
# in the scan detail and picked up next time.
MAX_ANALYSES_PER_SCAN = 3

_lock = threading.Lock()


# ---------- The dedup log ----------

def _read_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    out = []
    for line in LOG_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def recently_alerted(key: str, window_hours: float, now: float | None = None) -> bool:
    """Has this dedup key been alerted within the window? The noise-control guard."""
    now = time.time() if now is None else now
    window = float(window_hours) * 3600.0
    with _lock:
        entries = _read_log()
    return any(e.get("dedup_key") == key and (now - float(e.get("created_at", 0))) < window
               for e in entries)


def append_log(key: str, alert_id: str, created_at: float) -> None:
    with _lock:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as fh:
            fh.write(json.dumps({"dedup_key": key, "alert_id": alert_id,
                                 "created_at": created_at}) + "\n")


def bucket(value: float, step: float) -> int:
    """Magnitude bucket, so an ESCALATION re-fires inside the dedup window while
    the same breach at the same level stays suppressed."""
    try:
        v, s = float(value), float(step)
    except (TypeError, ValueError):
        return 0
    if s <= 0:
        return 0
    return int(v // s)


def dedup_key(finding: dict) -> str:
    cfg = rules.DEDUP.get(finding["rule_id"], {})
    step = float(cfg.get("bucket") or 5.0)
    parts = [finding["rule_id"], str(finding["entity"]),
             f"b{bucket(finding.get('metric_value', 0), step)}"]
    if cfg.get("include_period"):
        parts.insert(2, str(finding.get("period") or ""))
    return ":".join(parts)


def window_hours(finding: dict) -> float:
    return float(rules.DEDUP.get(finding["rule_id"], {}).get("window_hours") or 72)


# ---------- Persistence ----------

def _load_all() -> list[dict]:
    if not ALERTS_FILE.exists():
        return []
    try:
        return json.loads(ALERTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_all(items: list[dict]) -> None:
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ALERTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(ALERTS_FILE)


def list_alerts() -> list[dict]:
    with _lock:
        items = _load_all()
    items.sort(key=lambda a: a.get("created_at") or 0, reverse=True)
    return items


def get_alert(alert_id: str) -> dict | None:
    return next((a for a in list_alerts() if a["id"] == alert_id), None)


def create_alert(finding: dict, narrative: str) -> dict:
    """Persist an alert. `context` is passed through wholesale — the reference
    copies a fixed key set, so richer findings would silently lose their driver
    unit, hedge coverage and product line."""
    now = time.time()
    alert = {
        "id": uuid.uuid4().hex,
        "created_at": now,
        "severity": finding["severity"],
        "rule_id": finding["rule_id"],
        "entity": finding["entity"],
        "period": finding["period"],
        "dedup_key": dedup_key(finding),
        "title": finding["title"],
        "narrative": narrative,
        "metric_value": finding["metric_value"],
        "threshold": finding["threshold"],
        "context": finding.get("context") or {},
        "read": False,
        "chat_session_id": None,
    }
    with _lock:
        items = _load_all()
        items.append(alert)
        _save_all(items)
    append_log(alert["dedup_key"], alert["id"], now)
    return alert


def _patch(alert_id: str, **updates) -> dict | None:
    with _lock:
        items = _load_all()
        alert = next((a for a in items if a["id"] == alert_id), None)
        if not alert:
            return None
        alert.update(updates)
        _save_all(items)
        return alert


def mark_read(alert_id: str) -> dict | None:
    return _patch(alert_id, read=True)


def mark_all_read() -> int:
    with _lock:
        items = _load_all()
        changed = 0
        for a in items:
            if not a.get("read"):
                a["read"] = True
                changed += 1
        if changed:
            _save_all(items)
        return changed


def set_chat_session(alert_id: str, session_id: str) -> dict | None:
    return _patch(alert_id, chat_session_id=session_id)


def delete_alert(alert_id: str) -> bool:
    with _lock:
        items = _load_all()
        alert = next((a for a in items if a["id"] == alert_id), None)
        if not alert:
            return False
        items.remove(alert)
        _save_all(items)
        return True


def summary() -> dict:
    """Unread count + newest timestamp — rides the /api/settings poll so the
    bell badge and browser notifications need no channel of their own."""
    with _lock:
        items = _load_all()
    return {
        "unread": sum(1 for a in items if not a.get("read")),
        "latest_created_at": max((a.get("created_at") or 0 for a in items), default=0),
    }


# ---------- The scan ----------

def run_drift_scan(params: dict, profile: dict | None = None) -> str:
    """Evaluate the rules, alert on anything new, return a short task detail."""
    cfg = rules.get_rules()
    checked = sum(1 for r in cfg if r.get("enabled"))
    findings = rules.evaluate(cfg)
    fresh = [f for f in findings
             if not recently_alerted(dedup_key(f), window_hours(f))]

    created = 0
    for finding in fresh[:MAX_ANALYSES_PER_SCAN]:
        narrative = _analyse(finding, params, profile)
        create_alert(finding, narrative)
        created += 1

    detail = f"{checked} rules checked, {created} new alert{'s' if created != 1 else ''}"
    deferred = len(fresh) - created
    if deferred > 0:
        detail += f" ({deferred} more finding{'s' if deferred != 1 else ''} deferred)"
    return detail


def _narrative_params(params: dict) -> dict:
    """The alert narrative's own run settings, narrowed from the caller's.

    Two deliberate reductions, both about what this turn is FOR. `rules.py` has
    already computed the finding deterministically; the model's job is to check
    the figure against internal data and say why it matters in a few sentences —
    not to research it on the open web. So:

      * web research off. It was on, which let a single alert fan out into up to
        8 searches and 8 fetches of unbounded page content, on a scan that runs
        unattended and can produce three of these at a time. The local tools stay
        enabled, so the figure is still verified against the company's own data —
        which is the verification URGENCY_ANALYSIS_PROMPT actually asks for.
      * medium effort. A ≤6-sentence briefing over a pre-computed finding does
        not need the interactive default.

    The model is already the general slot — run_agent_to_text has no session and
    so no tier to pick.
    """
    narrowed = dict(params)
    narrowed["research"] = {**(params.get("research") or {}), "enabled": False}
    narrowed["effort"] = "medium"
    return narrowed


def _analyse(finding: dict, params: dict, profile: dict | None = None) -> str:
    """Agent-written narrative, with a deterministic fallback so alert creation
    never depends on the API being reachable."""
    prompt = URGENCY_ANALYSIS_PROMPT.format(**finding)
    try:
        with reporting.AGENT_SEMAPHORE:
            text, error = reporting.run_agent_to_text(
                [{"role": "user", "content": prompt}], _narrative_params(params),
                profile=profile)
    except Exception as exc:   # belt and braces — run_agent normally yields errors
        text, error = "", str(exc)
    if text and not error:
        return text
    reason = error or "the agent returned no analysis"
    return f"{finding['title']}.\n\n_Automatic analysis unavailable: {reason}_"
