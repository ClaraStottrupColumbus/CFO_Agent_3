# main.py — FastAPI app: routes, SSE, setup gating, static files, lifespan.
#
# Single uvicorn process on port 8323 (Agent 1 = 8321, Agent 2 = 8322). JSON
# files on disk, threads, no database, no build step.
#
# The setup gate is the structural difference from the reference: almost every
# route 409s until the CFO has confirmed a company profile, because a market
# scan run before the agent knows which markets matter burns tokens producing a
# report nobody can use.

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (alerts, budgetplan, config, drivers, generate_data,
               profile as profile_mod, reporting, rules, scenarios, scheduler,
               store, tasks, tools)
from .agent import AVAILABLE_MODELS, EFFORT_LEVELS, volatile_context

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"


def _load_dotenv() -> None:
    """Minimal .env reader so ANTHROPIC_API_KEY can live in a file. Never
    overrides a variable already set in the environment."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

_scheduler = scheduler.TaskScheduler(config.get_run_params,
                                     lambda: profile_mod.get_profile())


def _volatile() -> str:
    """The uncached system block — recomputed per turn, never cached."""
    try:
        status = drivers.driver_status()
        stale = sum(1 for d in status if d["verify_status"] != "fresh")
        drifted = sum(1 for d in status
                      if d.get("drift_pct") is not None and abs(d["drift_pct"]) > 10)
        return volatile_context(stale, drifted)
    except Exception:
        return volatile_context()


def _startup_generate_reports() -> None:
    """Gate on the API key AND on setup_complete. The reference gates only on
    the key; here a weekly scan before the profile exists is wasted tokens."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[startup] no ANTHROPIC_API_KEY — skipping report generation")
        return
    if not profile_mod.setup_complete():
        print("[startup] setup not complete — skipping report generation")
        return
    params = config.get_run_params()
    prof = profile_mod.get_profile()
    for kind in ("weekly", "monthly"):
        try:
            reporting.ensure_report(kind, params, profile=prof, volatile=_volatile())
        except Exception as exc:
            print(f"[startup] {kind} report failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not (DATA_DIR / "budget_vs_actuals.parquet").exists():
        print("[startup] generating demo datasets…")
        generate_data.generate()
    _scheduler.start()
    if profile_mod.setup_complete():
        import threading
        threading.Thread(target=_startup_generate_reports,
                         name="startup-reports", daemon=True).start()
    yield
    _scheduler.stop()


app = FastAPI(title="CFO Budgeting Agent", lifespan=lifespan)


# --------------------------------------------------------------------------
# The setup gate
# --------------------------------------------------------------------------

def require_setup() -> None:
    """409 until the CFO has confirmed a profile.

    /api/settings and /api/profile* stay open so the settings panel and the
    wizard keep working while the app is gated.
    """
    if not profile_mod.setup_complete():
        raise HTTPException(status_code=409, detail={"error": "setup_incomplete"})


Gated = [Depends(require_setup)]


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class Attachment(BaseModel):
    name: str
    kind: str | None = None
    media_type: str | None = None
    data: str | None = None


class ChatRequest(BaseModel):
    message: str = ""
    attachments: list[Attachment] | None = None


class SessionRequest(BaseModel):
    kind: str = "chat"
    title: str | None = None
    parent_id: str | None = None


class ProposeRequest(BaseModel):
    description: str
    currency: str | None = "EUR"
    budget_year: int | None = None
    fiscal_year_start: int | None = None


class SettingsRequest(BaseModel):
    model: str | None = None
    reasoning: dict | None = None
    research: dict | None = None
    notifications: dict | None = None
    show_debug: bool | None = None


class TaskRequest(BaseModel):
    name: str | None = None
    type: str | None = None
    prompt: str | None = None
    schedule: dict | None = None
    enabled: bool | None = None
    model: str | None = None
    reasoning: bool | None = None


class ObservationRequest(BaseModel):
    price: float
    month: str
    source_url: str | None = ""
    note: str | None = None


class BudgetPlanRequest(BaseModel):
    company: dict | None = None
    baseline: dict | None = None
    variables: list[dict] | None = None


class BudgetChatRequest(BaseModel):
    message: str = ""


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------

def _sse(events) -> StreamingResponse:
    def gen():
        for event in events:
            yield f"data: {json.dumps(event, default=str)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------
# Profile / setup  (UNGATED — the wizard has to work before setup exists)
# --------------------------------------------------------------------------

@app.get("/api/profile")
def get_profile() -> dict:
    view = profile_mod.public_view()
    view["has_api_key"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
    view["has_demo_profile"] = profile_mod.DEMO_PROFILE_FILE.exists()
    return view


@app.post("/api/profile")
def post_profile(payload: dict) -> dict:
    """Confirm the CFO-edited profile, then run the side effects in order:
    seed drivers → generate datasets → create the default tasks → wake."""
    try:
        saved = profile_mod.confirm_profile(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    try:
        generate_data.apply_profile(saved)
    except Exception as exc:
        print(f"[setup] dataset seeding failed: {exc}")
    tasks.seed_default_tasks()
    _scheduler.wake()
    view = profile_mod.public_view(saved)
    view["has_api_key"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return view


@app.post("/api/profile/propose")
def propose_profile(payload: ProposeRequest):
    """Stream the setup research turn as a real session, so it inherits
    streaming, citations and a readable transcript for free."""
    profile_mod.set_description(payload.description)
    company: dict[str, Any] = {"reporting_currency": (payload.currency or "EUR").upper()}
    if payload.budget_year:
        company["budget_year"] = payload.budget_year
    if payload.fiscal_year_start:
        company["fiscal_year_start_month"] = payload.fiscal_year_start

    session = store.create_session("chat", title="Setup · watchlist proposal")
    params = config.get_run_params()
    prompt = (f"The CFO describes their business as follows:\n\n{payload.description}\n\n"
              f"Reporting currency: {company['reporting_currency']}. "
              f"Budget year: {company.get('budget_year', 'next year')}.")
    from .agent import SETUP_PROMPT

    def events():
        for event in reporting.run_session_turn(
                session, f"{SETUP_PROMPT}\n\n---\n\n{prompt}", params,
                setup=True, volatile=_volatile()):
            yield event
        # Terminal event so the wizard can populate the edit form.
        view = profile_mod.public_view()
        yield {"type": "proposal", "profile": view, "session_id": session["id"]}

    return _sse(events())


@app.post("/api/profile/demo")
def use_demo_profile() -> dict:
    """The offline path: load the bundled animal-feed persona."""
    demo = profile_mod.load_demo_profile()
    if not demo:
        raise HTTPException(status_code=404, detail={"error": "no demo profile bundled"})
    return post_profile(demo)


@app.post("/api/profile/reset")
def reset_profile() -> dict:
    return profile_mod.public_view(profile_mod.reset_setup())


# --------------------------------------------------------------------------
# Settings  (UNGATED — the panel must work while the app is gated)
# --------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict:
    """Must keep returning `scheduler` and `alerts`: the 10s frontend poll
    drives the refresh badge, the alert bell and browser notifications off this
    one payload, so dropping either silently kills three features. The `drivers`
    block buys a second header badge with no new timer."""
    cfg = config.get_settings()
    cfg["available_models"] = AVAILABLE_MODELS
    cfg["effort_levels"] = list(EFFORT_LEVELS)
    cfg["scheduler"] = _scheduler.refresh_compat_status()
    cfg["alerts"] = alerts.summary()
    cfg["setup_complete"] = profile_mod.setup_complete()
    try:
        status = drivers.driver_status()
        cfg["drivers"] = {
            "total": len(status),
            "stale": sum(1 for d in status if d["verify_status"] == "stale"),
            "failed": sum(1 for d in status if d["verify_status"] == "never_verified"),
            "drifted": sum(1 for d in status if d.get("drift_pct") is not None
                           and abs(d["drift_pct"]) > 10),
        }
    except Exception:
        cfg["drivers"] = {"total": 0, "stale": 0, "failed": 0, "drifted": 0}
    return cfg


@app.post("/api/settings")
def post_settings(payload: SettingsRequest) -> dict:
    update = {k: v for k, v in payload.model_dump().items() if v is not None}
    config.save_settings(update)
    _scheduler.wake()
    return get_settings()


# --------------------------------------------------------------------------
# Budget Outlook  (UNGATED — deliberately, and this is the one place it matters)
# --------------------------------------------------------------------------
#
# The omission of `dependencies=Gated` on every route below is intentional, not
# an oversight. Budget Outlook is config-driven: it reads no dataset and needs no
# company profile, and it carries its OWN first-run gate (`plan["configured"]`).
# Putting it behind require_setup would force a CFO through the driver-research
# wizard for a feature that never touches a driver.

@app.get("/api/budgetplan")
def get_budget_plan() -> dict:
    plan = budgetplan.get_plan()
    return {
        "plan": plan,
        "derived": budgetplan.derive(plan) if plan.get("configured") else None,
        "industries": budgetplan.industry_options(),
        "sizes": [{"id": s, "label": budgetplan.SIZE_LABELS[s]} for s in budgetplan.SIZES],
        "has_api_key": budgetplan.has_api_key(),
    }


@app.get("/api/budgetplan/defaults")
def get_budget_defaults(industry: str = Query(budgetplan.DEFAULT_INDUSTRY),
                        revenue: float = Query(0.0)) -> dict:
    """Repopulates the config screen's variable table when the industry changes."""
    return {"variables": budgetplan.defaults_for(industry, revenue)}


@app.post("/api/budgetplan/config")
def post_budget_plan(payload: BudgetPlanRequest) -> dict:
    result = budgetplan.save_config(payload.model_dump())
    if isinstance(result, str):
        raise HTTPException(status_code=400, detail={"error": result})
    return {"plan": result, "derived": budgetplan.derive(result)}


@app.post("/api/budgetplan/reset")
def reset_budget_plan() -> dict:
    return {"plan": budgetplan.reset_plan()}


@app.post("/api/budgetplan/narrative")
def post_budget_narrative(force: bool = Query(False)) -> dict:
    plan = budgetplan.get_plan()
    if not plan.get("configured"):
        raise HTTPException(status_code=409, detail={"error": "budget_plan_not_configured"})
    model = config.get_run_params()["model"]
    return {"narrative": budgetplan.ensure_narrative(plan, model, force=force)}


@app.post("/api/budgetplan/chat")
def post_budget_chat(payload: BudgetChatRequest) -> StreamingResponse:
    plan = budgetplan.get_plan()
    if not plan.get("configured"):
        raise HTTPException(status_code=409, detail={"error": "budget_plan_not_configured"})
    model = config.get_run_params()["model"]
    return _sse(budgetplan.stream_chat(plan, payload.message, model))


@app.post("/api/budgetplan/chat/clear")
def clear_budget_chat() -> dict:
    return {"plan": budgetplan.clear_chat()}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

@app.get("/api/sessions", dependencies=Gated)
def list_sessions(kind: str = Query("chat")) -> dict:
    if kind not in store.KINDS:
        raise HTTPException(status_code=400, detail={"error": f"unknown kind '{kind}'"})
    return {"sessions": store.list_sessions(kind)}


@app.post("/api/sessions", dependencies=Gated)
def create_session(payload: SessionRequest) -> dict:
    if payload.kind not in store.KINDS:
        raise HTTPException(status_code=400, detail={"error": f"unknown kind '{payload.kind}'"})
    return store.create_session(payload.kind, title=payload.title,
                                parent_id=payload.parent_id)


@app.get("/api/sessions/{session_id}", dependencies=Gated)
def get_session(session_id: str) -> dict:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail={"error": "session not found"})
    return session


@app.delete("/api/sessions/{session_id}", dependencies=Gated)
def delete_session(session_id: str) -> dict:
    if not store.delete_session(session_id):
        raise HTTPException(status_code=404, detail={"error": "session not found"})
    return {"deleted": True}


@app.post("/api/sessions/{session_id}/chat", dependencies=Gated)
def chat(session_id: str, payload: ChatRequest):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail={"error": "session not found"})
    params = config.get_run_params()
    return _sse(reporting.run_session_turn(
        session, payload.message, params, payload.attachments,
        profile=profile_mod.get_profile(), volatile=_volatile()))


@app.post("/api/reports/{kind}", dependencies=Gated)
def generate_report(kind: str) -> dict:
    if kind not in ("weekly", "monthly"):
        raise HTTPException(status_code=400, detail={"error": "kind must be weekly or monthly"})
    session = reporting.create_and_generate_report(
        kind, config.get_run_params(), profile=profile_mod.get_profile(),
        volatile=_volatile())
    return session


# --------------------------------------------------------------------------
# Datasets
# --------------------------------------------------------------------------

@app.get("/api/datasets", dependencies=Gated)
def list_datasets() -> dict:
    return tools.list_datasets()


@app.get("/api/datasets/{name}", dependencies=Gated)
def preview_dataset(name: str, limit: int = Query(25)) -> dict:
    result = tools.query_budget_data(name, limit=min(limit, 200))
    if "error" in result:
        raise HTTPException(status_code=404, detail=result)
    return result


@app.post("/api/refresh", dependencies=Gated)
def refresh_now() -> dict:
    return _scheduler.run_refresh_now()


# --------------------------------------------------------------------------
# Drivers
# --------------------------------------------------------------------------

_verifying: set[str] = set()


@app.get("/api/drivers", dependencies=Gated)
def list_drivers() -> dict:
    status = drivers.driver_status()
    for d in status:
        d["verifying"] = d["driver_id"] in _verifying
    return {
        "drivers": status,
        "summary": {
            "total": len(status),
            "fresh": sum(1 for d in status if d["verify_status"] == "fresh"),
            "stale": sum(1 for d in status if d["verify_status"] == "stale"),
            "never_verified": sum(1 for d in status if d["verify_status"] == "never_verified"),
            "drifted": sum(1 for d in status if d.get("drift_pct") is not None
                           and abs(d["drift_pct"]) > 10),
        },
        "locked_at": drivers.load_assumptions().get("locked_at"),
        "running": bool(_verifying),
    }


@app.get("/api/drivers/{driver_id}/history", dependencies=Gated)
def driver_history(driver_id: str) -> dict:
    prices = drivers.load_prices()
    if prices.empty or driver_id not in set(prices["driver_id"].astype(str)):
        raise HTTPException(status_code=404, detail={"error": f"unknown driver '{driver_id}'"})
    rows = prices[prices["driver_id"] == driver_id].sort_values(["month", "revision"])
    return {"driver_id": driver_id, "observations": tools._records(rows.tail(60))}


@app.post("/api/drivers/{driver_id}/verify", dependencies=Gated)
def verify_driver(driver_id: str) -> dict:
    """Fire-and-forget: an agent turn is slow, so the card shows a spinner and
    the view polls while any driver is running."""
    driver = drivers.get_driver(driver_id)
    if driver is None:
        raise HTTPException(status_code=404, detail={"error": f"unknown driver '{driver_id}'"})
    if driver_id in _verifying:
        return {"started": False, "reason": "already running"}

    _verifying.add(driver_id)
    params = config.get_run_params()
    prof = profile_mod.get_profile()
    hint = driver.get("search_hint") or driver.get("name") or driver_id
    prompt = (f"Find the current market price for '{driver.get('name')}' "
              f"(driver_id {driver_id}, quoted in {driver.get('quote_currency')}, "
              f"unit {driver.get('unit')}). Search for: {hint}. Fetch the page that "
              f"carries the figure, then record it with record_driver_observation citing "
              f"that exact URL. Reply with one sentence on what you found and the source.")

    def run():
        try:
            with reporting.AGENT_SEMAPHORE:
                reporting.run_agent_to_text([{"role": "user", "content": prompt}],
                                            params, profile=prof)
        except Exception as exc:
            print(f"[drivers] verify {driver_id} failed: {exc}")
        finally:
            _verifying.discard(driver_id)

    import threading
    threading.Thread(target=run, name=f"verify-{driver_id}", daemon=True).start()
    return {"started": True}


@app.post("/api/drivers/{driver_id}/observation", dependencies=Gated)
def add_observation(driver_id: str, payload: ObservationRequest) -> dict:
    """The manual path: a value the CFO typed. Provenance verification is
    skipped because there is a human behind it rather than a citation."""
    result = drivers.append_observation(
        driver_id, payload.price, payload.month,
        source_url=payload.source_url or "manual entry",
        source="cfo_manual", note=payload.note, verify_provenance=False)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    return result


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

@app.get("/api/scenarios", dependencies=Gated)
def list_scenarios() -> dict:
    items = scenarios.list_scenarios()
    return {"scenarios": [scenarios.summary(s) for s in items]}


@app.get("/api/scenarios/{scenario_id}", dependencies=Gated)
def get_scenario(scenario_id: str) -> dict:
    s = scenarios.get_scenario(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail={"error": "scenario not found"})
    return s


@app.post("/api/scenarios/{scenario_id}/activate", dependencies=Gated)
def activate_scenario(scenario_id: str) -> dict:
    s = scenarios.set_active(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail={"error": "scenario not found"})
    return scenarios.summary(s)


@app.delete("/api/scenarios/{scenario_id}", dependencies=Gated)
def delete_scenario(scenario_id: str) -> dict:
    if not scenarios.delete_scenario(scenario_id):
        raise HTTPException(status_code=404, detail={"error": "scenario not found"})
    return {"deleted": True}


# --------------------------------------------------------------------------
# Budget state (the home screen headline)
# --------------------------------------------------------------------------

@app.get("/api/budget-state", dependencies=Gated)
def budget_state() -> dict:
    """The cumulative budget effect of everything that has drifted since lock —
    the highest-value number on the home screen. Returns available: false rather
    than guessing, so the frontend can hide the whole block."""
    try:
        projected = rules._rerun_active_scenario()
        status = drivers.driver_status()
    except Exception:
        return {"available": False}
    if not projected:
        return {"available": False}
    prof = profile_mod.get_profile()
    return {
        "available": True,
        "budget_year": (prof.get("company") or {}).get("budget_year"),
        "scenario": projected["scenario_name"],
        "net_delta_ebitda_eur": projected["delta_ebitda_eur"],
        "margin_pct": projected["margin_pct"],
        "baseline_margin_pct": projected["baseline_margin_pct"],
        "margin_delta_pp": (projected["margin_pct"]
                            - (projected["baseline_margin_pct"] or 0.0)),
        "drivers": len(status),
        "drifted": sum(1 for d in status if d.get("drift_pct") is not None
                       and abs(d["drift_pct"]) > 10),
        "stale": sum(1 for d in status if d["verify_status"] != "fresh"),
        "locked_at": drivers.load_assumptions().get("locked_at"),
    }


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

@app.get("/api/tasks", dependencies=Gated)
def list_tasks() -> dict:
    start = _scheduler.session_start
    return {"tasks": [{**tasks.with_next_run(t, start),
                       "running": _scheduler.is_running(t["id"])}
                      for t in tasks.list_tasks()],
            "task_types": list(tasks.TASK_TYPES)}


@app.post("/api/tasks", dependencies=Gated)
def create_task(payload: TaskRequest) -> dict:
    try:
        task = tasks.create_task(payload.model_dump(exclude_none=True))
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    _scheduler.wake()
    return task


@app.patch("/api/tasks/{task_id}", dependencies=Gated)
def update_task(task_id: str, payload: TaskRequest) -> dict:
    try:
        task = tasks.update_task(task_id, payload.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    _scheduler.wake()
    return task


@app.delete("/api/tasks/{task_id}", dependencies=Gated)
def delete_task(task_id: str) -> dict:
    try:
        if not tasks.delete_task(task_id):
            raise HTTPException(status_code=404, detail={"error": "task not found"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    _scheduler.wake()
    return {"deleted": True}


@app.post("/api/tasks/{task_id}/run", dependencies=Gated)
def run_task(task_id: str) -> dict:
    if not _scheduler.run_task_now(task_id):
        raise HTTPException(status_code=409,
                            detail={"error": "task not found or already running"})
    return {"started": True}


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

@app.get("/api/alerts", dependencies=Gated)
def list_alerts() -> dict:
    return {"alerts": alerts.list_alerts(), "summary": alerts.summary()}


@app.post("/api/alerts/read-all", dependencies=Gated)
def read_all_alerts() -> dict:
    return {"updated": alerts.mark_all_read()}


@app.post("/api/alerts/{alert_id}/read", dependencies=Gated)
def read_alert(alert_id: str) -> dict:
    alert = alerts.mark_read(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail={"error": "alert not found"})
    return alert


@app.delete("/api/alerts/{alert_id}", dependencies=Gated)
def delete_alert(alert_id: str) -> dict:
    if not alerts.delete_alert(alert_id):
        raise HTTPException(status_code=404, detail={"error": "alert not found"})
    return {"deleted": True}


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

@app.get("/api/rules", dependencies=Gated)
def get_rules() -> dict:
    return {"rules": rules.get_rules()}


@app.post("/api/rules", dependencies=Gated)
def post_rules(payload: dict) -> dict:
    try:
        return {"rules": rules.save_rules(payload.get("rules") or [])}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})


# --------------------------------------------------------------------------
# Static
# --------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
