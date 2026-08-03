# tasks.py — Scheduled tasks: the autonomous side of the agent.
#
# The schedule math (compute_next_run, _validate_schedule) is reused UNCHANGED
# from the reference — it is what gives the CFO interval/daily/weekly/monthly
# cadence out of the box, and tests/test_schedule_math.py passes against it
# unmodified.
#
# Two concrete breakages from the reference are fixed here:
#   * execute_task derived the report kind via ttype.split("_")[0], which no
#     longer works now the task types are driver_scan / budget_revision.
#     Replaced with an explicit TASK_KIND map.
#   * A per-task `model` override is added so assumption_refresh can run cheaply
#     on claude-sonnet-5 while driver_scan runs on claude-opus-5. It is VALIDATED
#     against MODEL_CAPS: this is the one place a model id enters the system
#     without passing through the settings picker, so it is the one place a
#     hand-edited tasks.json could reintroduce an unusable model.

from __future__ import annotations

import calendar
import json
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import alerts, drivers, generate_data, reporting, store
from .agent import MODEL_CAPS, ASSUMPTION_REFRESH_PROMPT

TASKS_FILE = Path(__file__).resolve().parent.parent / "data" / "tasks.json"

TASK_TYPES = ("data_refresh", "driver_scan", "budget_revision", "assumption_refresh",
              "drift_scan", "custom_prompt")
SCHEDULE_MODES = ("interval", "daily", "weekly", "monthly")

# Explicit, because ttype.split("_")[0] would yield "driver" and "budget".
TASK_KIND = {"driver_scan": "weekly", "budget_revision": "monthly"}

BUILTIN_REFRESH_ID = "builtin_data_refresh"

_lock = threading.Lock()


# ---------- Persistence ----------

def _builtin_refresh_task() -> dict:
    now = time.time()
    return {
        "id": BUILTIN_REFRESH_ID,
        "name": "Data refresh",
        "type": "data_refresh",
        "prompt": None,
        # Hourly, not the reference's 5 minutes: 30s and 1min are demo pacing
        # and nonsense for commodity quotes.
        "schedule": {"mode": "interval", "interval_seconds": 3600},
        "enabled": True,
        "builtin": True,
        "model": None,
        "reasoning": False,
        "created_at": now,
        "updated_at": now,
        "last_run_utc": None,
        "last_status": None,
        "last_error": None,
        "last_detail": None,
    }


def default_tasks() -> list[dict]:
    """The task set created when setup completes."""
    now = time.time()

    def task(name, ttype, schedule, **extra):
        return {"id": uuid.uuid4().hex, "name": name, "type": ttype, "prompt": None,
                "schedule": schedule, "enabled": True, "builtin": False,
                "model": None, "reasoning": False, "created_at": now, "updated_at": now,
                "last_run_utc": None, "last_status": None, "last_error": None,
                "last_detail": None, **extra}

    return [
        _builtin_refresh_task(),
        # The weekly scan is the one background run whose reasoning trail is
        # genuinely evidence, so it opts in.
        task("Weekly market scan", "driver_scan",
             {"mode": "weekly", "day_of_week": 0, "time": "07:00"}, reasoning=True),
        task("Monthly budget revision", "budget_revision",
             {"mode": "monthly", "day_of_month": 3, "time": "07:00"}),
        # Decouples "keep the data fresh" from "write a report", so the refresh
        # runs daily and cheaply while the narrative runs weekly.
        task("Assumption refresh", "assumption_refresh",
             {"mode": "daily", "time": "06:00"}, model="claude-sonnet-5"),
        task("Drift scan", "drift_scan", {"mode": "interval", "interval_seconds": 3600}),
    ]


def _load_all() -> list[dict]:
    if not TASKS_FILE.exists():
        return [_builtin_refresh_task()]
    try:
        return json.loads(TASKS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return [_builtin_refresh_task()]


def _save_all(items: list[dict]) -> None:
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(TASKS_FILE)


def list_tasks() -> list[dict]:
    with _lock:
        items = _load_all()
        if not TASKS_FILE.exists():
            _save_all(items)
        return items


def get_task(task_id: str) -> dict | None:
    return next((t for t in list_tasks() if t["id"] == task_id), None)


def seed_default_tasks() -> list[dict]:
    """Called once when setup completes. Never clobbers existing tasks."""
    with _lock:
        if TASKS_FILE.exists():
            items = _load_all()
            if len(items) > 1:
                return items
        items = default_tasks()
        _save_all(items)
        return items


def _validate_schedule(schedule: dict) -> dict:
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object.")
    mode = schedule.get("mode")
    if mode not in SCHEDULE_MODES:
        raise ValueError(f"schedule.mode must be one of {SCHEDULE_MODES}.")
    clean: dict = {"mode": mode}
    if mode == "interval":
        try:
            clean["interval_seconds"] = max(10, int(schedule.get("interval_seconds", 3600)))
        except (TypeError, ValueError):
            raise ValueError("schedule.interval_seconds must be a number.")
        return clean
    t = str(schedule.get("time", "07:00"))
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
        raise ValueError("schedule.time must be HH:MM (24h).")
    clean["time"] = t
    if mode == "weekly":
        dow = int(schedule.get("day_of_week", 0))
        if not 0 <= dow <= 6:
            raise ValueError("schedule.day_of_week must be 0 (Monday) … 6 (Sunday).")
        clean["day_of_week"] = dow
    elif mode == "monthly":
        dom = int(schedule.get("day_of_month", 1))
        if not 1 <= dom <= 31:
            raise ValueError("schedule.day_of_month must be 1…31.")
        clean["day_of_month"] = dom
    return clean


def _validate_model(value) -> str | None:
    """A per-task model override must be a model the picker would offer.

    This is the one place a model id enters the system without passing through
    the settings picker, so it is the one place a hand-edited tasks.json could
    reintroduce a model that cannot research or that errors on effort.
    """
    if value in (None, "", "default"):
        return None
    if value not in MODEL_CAPS:
        raise ValueError(f"Unknown model '{value}'. Valid: {', '.join(MODEL_CAPS)}.")
    return value


def create_task(fields: dict) -> dict:
    ttype = fields.get("type")
    if ttype not in TASK_TYPES:
        raise ValueError(f"type must be one of {TASK_TYPES}.")
    prompt = (fields.get("prompt") or "").strip() or None
    if ttype == "custom_prompt" and not prompt:
        raise ValueError("A custom prompt task needs a prompt.")
    name = " ".join(str(fields.get("name") or "").split())
    if not name:
        raise ValueError("Task name is required.")
    now = time.time()
    task = {
        "id": uuid.uuid4().hex,
        "name": name[:80],
        "type": ttype,
        "prompt": prompt if ttype == "custom_prompt" else None,
        "schedule": _validate_schedule(fields.get("schedule") or {}),
        "enabled": bool(fields.get("enabled", True)),
        "builtin": False,
        "model": _validate_model(fields.get("model")),
        "reasoning": bool(fields.get("reasoning", False)),
        "created_at": now,
        "updated_at": now,
        "last_run_utc": None,
        "last_status": None,
        "last_error": None,
        "last_detail": None,
    }
    with _lock:
        items = _load_all()
        items.append(task)
        _save_all(items)
    return task


def update_task(task_id: str, fields: dict) -> dict:
    with _lock:
        items = _load_all()
        task = next((t for t in items if t["id"] == task_id), None)
        if not task:
            raise KeyError("Task not found.")
        if task.get("builtin"):
            allowed = {k: v for k, v in fields.items() if k in ("schedule", "enabled")}
            if "schedule" in allowed and allowed["schedule"].get("mode") != "interval":
                raise ValueError("The built-in data refresh runs on an interval.")
            fields = allowed
        if "name" in fields and fields["name"] is not None:
            name = " ".join(str(fields["name"]).split())
            if not name:
                raise ValueError("Task name is required.")
            task["name"] = name[:80]
        if "prompt" in fields and task["type"] == "custom_prompt":
            prompt = (fields["prompt"] or "").strip()
            if not prompt:
                raise ValueError("A custom prompt task needs a prompt.")
            task["prompt"] = prompt
        if "schedule" in fields and fields["schedule"] is not None:
            task["schedule"] = _validate_schedule(fields["schedule"])
        if "enabled" in fields and fields["enabled"] is not None:
            task["enabled"] = bool(fields["enabled"])
        if "model" in fields:
            task["model"] = _validate_model(fields["model"])
        if "reasoning" in fields and fields["reasoning"] is not None:
            task["reasoning"] = bool(fields["reasoning"])
        task["updated_at"] = time.time()
        _save_all(items)
        return task


def delete_task(task_id: str) -> bool:
    with _lock:
        items = _load_all()
        task = next((t for t in items if t["id"] == task_id), None)
        if not task:
            return False
        if task.get("builtin"):
            raise ValueError("The built-in data refresh task can't be deleted.")
        items.remove(task)
        _save_all(items)
        return True


def mark_running(task_id: str) -> None:
    _patch(task_id, last_status="running",
           last_run_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def set_run_result(task_id: str, status: str, error: str | None = None,
                   detail: str | None = None) -> None:
    _patch(task_id, last_status=status, last_error=error, last_detail=detail)


def _patch(task_id: str, **updates) -> None:
    with _lock:
        items = _load_all()
        task = next((t for t in items if t["id"] == task_id), None)
        if not task:
            return
        task.update(updates)
        _save_all(items)


# ---------- Schedule math (pure — unit-tested, unchanged from the reference) ----------

def _parse_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compute_next_run(task: dict, now: datetime | None = None,
                     session_start: datetime | None = None,
                     tz=None) -> datetime | None:
    """Next due time as an aware UTC datetime, or None if the task is disabled.

    Interval tasks count from the last run, floored at `session_start` so a
    restart never replays an overdue interval. Wall-clock tasks return the next
    future occurrence; missed occurrences are never caught up. day_of_month
    clamps to the month.
    """
    if not task.get("enabled"):
        return None
    sched = task.get("schedule") or {}
    mode = sched.get("mode")
    now = now or datetime.now(timezone.utc)

    if mode == "interval":
        interval = max(10, int(sched.get("interval_seconds", 300)))
        base = _parse_utc(task.get("last_run_utc"))
        floor = session_start
        if task.get("created_at"):
            created = datetime.fromtimestamp(task["created_at"], tz=timezone.utc)
            floor = max(floor, created) if floor else created
        if floor and (base is None or base < floor):
            base = floor
        if base is None:
            base = now
        return base + timedelta(seconds=interval)

    tz = tz or datetime.now().astimezone().tzinfo
    local_now = now.astimezone(tz)
    hh, mm = (int(p) for p in str(sched.get("time", "07:00")).split(":"))

    if mode == "daily":
        cand = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= local_now:
            cand += timedelta(days=1)
        return cand.astimezone(timezone.utc)

    if mode == "weekly":
        cand = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        cand += timedelta(days=(int(sched.get("day_of_week", 0)) - cand.weekday()) % 7)
        if cand <= local_now:
            cand += timedelta(days=7)
        return cand.astimezone(timezone.utc)

    if mode == "monthly":
        dom = int(sched.get("day_of_month", 1))

        def occurrence(year: int, month: int) -> datetime:
            day = min(dom, calendar.monthrange(year, month)[1])
            return local_now.replace(year=year, month=month, day=day,
                                     hour=hh, minute=mm, second=0, microsecond=0)

        cand = occurrence(local_now.year, local_now.month)
        if cand <= local_now:
            y, m = ((local_now.year + 1, 1) if local_now.month == 12
                    else (local_now.year, local_now.month + 1))
            cand = occurrence(y, m)
        return cand.astimezone(timezone.utc)

    return None


def with_next_run(task: dict, session_start: datetime | None = None) -> dict:
    nr = compute_next_run(task, session_start=session_start)
    return {**task, "next_run_utc": nr.isoformat(timespec="seconds") if nr else None}


# ---------- Execution ----------

def _params_for(task: dict, params: dict) -> dict:
    """Run params for a task: the configured settings, with the validated
    per-task overrides applied. An invalid stored model falls back rather than
    failing the run."""
    out = dict(params)
    override = task.get("model")
    if override and override in MODEL_CAPS:
        out["model"] = override
    out["reasoning"] = bool(task.get("reasoning", False))
    return out


def execute_task(task: dict, params: dict, profile: dict | None = None) -> str:
    """Run one task to completion; returns a short human detail string.
    Raises on failure — the scheduler worker records the error on the task."""
    ttype = task["type"]
    run_params = _params_for(task, params)

    if ttype == "data_refresh":
        # Small jitter simulates fresh numbers arriving from source systems.
        result = generate_data.refresh_overview(jitter=random.uniform(-0.004, 0.004))
        scan = alerts.run_drift_scan(run_params, profile)
        return f"refreshed {result['refreshed_utc']} · {scan}"

    if ttype in TASK_KIND:
        kind = TASK_KIND[ttype]
        period = reporting.period_key(kind)
        if store.find_by_period(kind, period):
            return f"report already exists for {period}"
        with reporting.AGENT_SEMAPHORE:
            reporting.create_and_generate_report(kind, run_params, profile=profile)
        return f"generated {kind} report · {period}"

    if ttype == "assumption_refresh":
        # Cheap, narrative-free: records observations for stale drivers and
        # creates no session.
        before = _stale_ids()
        if not before:
            return "no stale drivers"
        with reporting.AGENT_SEMAPHORE:
            text, error = reporting.run_agent_to_text(
                [{"role": "user", "content": ASSUMPTION_REFRESH_PROMPT}],
                run_params, profile=profile)
        if error:
            raise RuntimeError(error)
        after = _stale_ids()
        refreshed = len(before - after)
        return (f"{refreshed}/{len(before)} stale driver(s) refreshed"
                + (f" · {text.strip()[:120]}" if text.strip() else ""))

    if ttype == "drift_scan":
        return alerts.run_drift_scan(run_params, profile)

    if ttype == "custom_prompt":
        title = f"[Task] {task['name']} · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        session = store.create_session("chat", title=title)
        error = None
        with reporting.AGENT_SEMAPHORE:
            for event in reporting.run_session_turn(session, task["prompt"] or "",
                                                    run_params, profile=profile):
                if event.get("type") == "error" and error is None:
                    error = event.get("message")
        if error:
            raise RuntimeError(error)
        return f"ran prompt → chat “{title}”"

    raise ValueError(f"Unknown task type '{ttype}'.")


def _stale_ids() -> set[str]:
    return {d["driver_id"] for d in drivers.driver_status()
            if d.get("verify_status") != "fresh"}
