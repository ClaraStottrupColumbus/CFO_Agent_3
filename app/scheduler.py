# scheduler.py — The background task scheduler.
# A daemon thread that runs the user-defined tasks in data/tasks.json
# (tasks.py): data refresh, report generation, custom agent prompts, urgency
# scans. Each due task executes on its own worker thread so a slow agent run
# never delays the next data refresh; a per-task guard prevents a task from
# overlapping itself, and failures are recorded on the task (last_status /
# last_error) instead of dying silently. The old RefreshScheduler behaviour
# lives on as the built-in data-refresh task, and refresh_compat_status()
# keeps the original /api/settings scheduler payload shape working.

from __future__ import annotations

import random
import threading
from datetime import datetime, timezone

from . import alerts, generate_data
from . import tasks as tasks_mod

# Wait cap: the loop re-reads tasks.json at least this often, so schedule
# edits, wall-clock tasks and external file changes stay accurate.
MAX_WAIT_SECONDS = 30.0


class TaskScheduler:
    def __init__(self, get_model):
        """`get_model` is a zero-arg callable returning the current model id —
        passed in (rather than importing main.py) to avoid a circular import."""
        self._get_model = get_model
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_ids: set[str] = set()
        self._running_lock = threading.Lock()
        self._session_start: datetime | None = None

    # ---------- Lifecycle ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Interval tasks count from here — a restart starts a fresh interval
        # instead of replaying an overdue one (tasks.compute_next_run).
        self._session_start = datetime.now(timezone.utc)
        self._thread = threading.Thread(target=self._loop, name="task-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Re-evaluate schedules now (called after any task create/update/delete)."""
        self._wake.set()

    @property
    def session_start(self) -> datetime | None:
        return self._session_start

    # ---------- Dispatch ----------

    def run_task_now(self, task_id: str) -> bool:
        """Run a task immediately on a worker thread. Returns False if the task
        doesn't exist or is already running."""
        task = tasks_mod.get_task(task_id)
        if not task:
            return False
        return self._dispatch(task)

    def _dispatch(self, task: dict) -> bool:
        with self._running_lock:
            if task["id"] in self._running_ids:
                return False
            self._running_ids.add(task["id"])
        tasks_mod.mark_running(task["id"])
        threading.Thread(target=self._worker, args=(task,),
                         name=f"task-{task['name'][:24]}", daemon=True).start()
        return True

    def _worker(self, task: dict) -> None:
        try:
            detail = tasks_mod.execute_task(task, self._get_model())
            tasks_mod.set_run_result(task["id"], "ok", error=None, detail=detail)
        except Exception as exc:  # surface the failure in the UI, keep the demo alive
            print(f"[scheduler] task '{task['name']}' failed: {exc}")
            tasks_mod.set_run_result(task["id"], "error", error=str(exc))
        finally:
            with self._running_lock:
                self._running_ids.discard(task["id"])
            self._wake.set()   # recompute the next due time now that last_run moved

    # ---------- Manual refresh (Data page's "Refresh now") ----------

    def run_refresh_now(self) -> dict:
        """Refresh the curated overview synchronously — the Data page re-renders
        on return and must see the new timestamp — then run the urgency scan in
        the background. Skips the scan if the built-in task is mid-run."""
        result = generate_data.refresh_overview(jitter=random.uniform(-0.004, 0.004))
        with self._running_lock:
            scan_wanted = tasks_mod.BUILTIN_REFRESH_ID not in self._running_ids
            if scan_wanted:
                self._running_ids.add(tasks_mod.BUILTIN_REFRESH_ID)
        if scan_wanted:
            tasks_mod.mark_running(tasks_mod.BUILTIN_REFRESH_ID)

            def scan() -> None:
                try:
                    detail = alerts.run_urgency_scan(self._get_model())
                    tasks_mod.set_run_result(tasks_mod.BUILTIN_REFRESH_ID, "ok",
                                             detail=f"manual refresh · {detail}")
                except Exception as exc:
                    tasks_mod.set_run_result(tasks_mod.BUILTIN_REFRESH_ID, "error", error=str(exc))
                finally:
                    with self._running_lock:
                        self._running_ids.discard(tasks_mod.BUILTIN_REFRESH_ID)
                    self._wake.set()

            threading.Thread(target=scan, name="task-manual-refresh", daemon=True).start()
        return result

    # ---------- Status ----------

    def is_running(self, task_id: str) -> bool:
        with self._running_lock:
            return task_id in self._running_ids

    def refresh_compat_status(self) -> dict:
        """The original RefreshScheduler.status() payload, derived from the
        built-in refresh task, so the existing settings panel and header badge
        keep working unchanged."""
        task = tasks_mod.get_task(tasks_mod.BUILTIN_REFRESH_ID) or tasks_mod._builtin_refresh_task()
        now = datetime.now(timezone.utc)
        next_run = tasks_mod.compute_next_run(task, now=now, session_start=self._session_start)
        return {
            "enabled": bool(task.get("enabled")),
            "interval_seconds": int(task["schedule"].get("interval_seconds", 300)),
            "last_refresh_utc": task.get("last_run_utc"),
            "next_refresh_in_seconds": (max(0, int((next_run - now).total_seconds()))
                                        if next_run else None),
            "server_time_utc": now.isoformat(timespec="seconds"),
        }

    # ---------- Loop ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            timeout = MAX_WAIT_SECONDS
            try:
                now = datetime.now(timezone.utc)
                soonest: datetime | None = None
                for task in tasks_mod.list_tasks():
                    if self.is_running(task["id"]):
                        continue   # still mid-run — its next slot counts from completion
                    next_run = tasks_mod.compute_next_run(task, now=now,
                                                          session_start=self._session_start)
                    if next_run is None:
                        continue
                    if next_run <= now:
                        self._dispatch(task)
                    elif soonest is None or next_run < soonest:
                        soonest = next_run
                if soonest is not None:
                    timeout = min(MAX_WAIT_SECONDS,
                                  max(0.5, (soonest - now).total_seconds()))
            except Exception as exc:  # never let the scheduler thread die
                print(f"[scheduler] loop error: {exc}")
            self._wake.wait(timeout=timeout)
            self._wake.clear()
