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
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (alerts, budgetplan, budgetversions, config, drivers, export,
               generate_data, profile as profile_mod, reporting, rules,
               scenarios, scheduler, store, tasks, tools)
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


class LockRequest(BaseModel):
    assumptions: dict
    scenario_id: str | None = None
    note: str | None = None


class ScenarioEditRequest(BaseModel):
    """The editable half of a scenario record.

    Every field is STATED, not patched: an edit replaces the assumption set
    whole, the same semantics as POST /api/assumptions/lock replacing the whole
    locked position. A key the CFO removed has to be *absent*, and a partial patch
    cannot express a removal. An omitted scalar falls back to the stored value.

    Validation lives in tools._assumption_blocks and budget.py, never here, so
    this form and the model's tool cannot disagree about what a valid basis or a
    valid product line is.
    """
    name: str
    note: str | None = None
    basis: str | None = None
    price_pass_through: float | None = None
    opex_inflation_pct: float | None = None
    assumptions: dict | None = None      # the four blocks, or a flat drivers dict


class VersionRequest(BaseModel):
    scenario_id: str | None = None
    label: str | None = None
    note: str | None = None
    created_by: str | None = None


class ApprovalRequest(BaseModel):
    by: str | None = None
    note: str | None = None


class WorkingCapitalRequest(BaseModel):
    """Every field optional and nullable: None means "not stated", which is a
    real value here and not the same as zero. Validation and clamping live in
    profile._clean_working_capital, so the model's tool and this form cannot
    disagree about what a valid DSO is."""
    dso_days: float | None = None
    dio_days: float | None = None
    dpo_days: float | None = None
    opening_cash_eur: float | None = None
    tax_rate_pct: float | None = None
    min_cash_eur: float | None = None
    note: str | None = None


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


@app.post("/api/profile/working-capital")
def post_working_capital(payload: WorkingCapitalRequest) -> dict:
    """The cash inputs, on their own.

    Ungated with the rest of /api/profile*, and separate from POST /api/profile
    on purpose: DSO, the opening cash position and a facility floor change during
    a budget round, and re-opening the setup wizard to change one of them would
    be absurd. `exclude_unset` matters — a field the form did not send must keep
    its stored value, while a field sent as null is the CFO deliberately clearing
    it back to "not stated".
    """
    saved = profile_mod.set_working_capital(payload.model_dump(exclude_unset=True))
    return {"working_capital": profile_mod.working_capital(saved)}


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
# The Budget  (UNGATED — deliberately, and this is the one place it matters)
# --------------------------------------------------------------------------
#
# The omission of `dependencies=Gated` on every route below is intentional, not
# an oversight. In simple mode the Budget page is config-driven: it reads no
# dataset and needs no company profile, and it carries its OWN first-run gate
# (`plan["configured"]`). Putting it behind require_setup would force a CFO
# through the driver-research wizard to reach a page that, until they have
# datasets, never touches a driver.
#
# BOM mode reads datasets that only exist once setup has run, which is why
# `bom_available` is reported rather than assumed: the mode follows the data,
# not the gate.

@app.get("/api/budgetplan")
def get_budget_plan() -> dict:
    plan = budgetplan.get_plan()
    payload = {
        "plan": plan,
        "derived": budgetplan.derive(plan) if plan.get("configured") else None,
        "industries": budgetplan.industry_options(),
        "sizes": [{"id": s, "label": budgetplan.SIZE_LABELS[s]} for s in budgetplan.SIZES],
        "has_api_key": budgetplan.has_api_key(),
        "bom_available": budgetplan.bom_available(),
        "setup_complete": profile_mod.setup_complete(),
        "drivers": {},
    }
    # Provenance for the driver-linked lines: the locked value, where it was
    # read and what has happened since. Keyed by driver_id so the drawer can
    # look one up without a second request, and empty (never absent) when there
    # is no watchlist, so the frontend has one shape to render.
    try:
        payload["drivers"] = {d["driver_id"]: d for d in drivers.driver_status()}
    except Exception:
        pass
    return payload


@app.post("/api/budgetplan/rebuild")
def rebuild_budget_plan() -> dict:
    """Rebuild every cost line from the datasets — BOM mode's one write.

    Ungated with the rest of the page, but it fails cleanly rather than
    dangerously: with no datasets `materialise_from_datasets` returns a sentence
    saying so and nothing is saved.
    """
    company = (profile_mod.get_profile().get("company") or {})
    result = budgetplan.rebuild_from_datasets({
        "name": company.get("name") or (budgetplan.get_plan().get("company") or {}).get("name"),
        "currency": company.get("reporting_currency"),
        "industry": (budgetplan.get_plan().get("company") or {}).get("industry"),
        "size": (budgetplan.get_plan().get("company") or {}).get("size"),
        "fiscal_year_start_month": company.get("fiscal_year_start_month"),
    })
    if isinstance(result, str):
        raise HTTPException(status_code=400, detail={"error": result})
    return {"plan": result, "derived": budgetplan.derive(result)}


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


# The Budget page has no chat route of its own. It had one while the feature was
# tool-less and citation-less; now its lines carry driver ids, so a question
# about one is a question about the real watchlist and goes through
# reporting.run_session_turn like every other conversation in the app. The page
# hands off to Ask, and `budget_outlook` is how the agent reads it.


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
    # `frozen` travels in the ENVELOPE, not inside summary(): that shape belongs to
    # scenarios.py, and that module must not learn that versions exist. The
    # precedent is GET /api/budget/versions returning approved_id and
    # active_scenario alongside its summaries. At most one version is approved, so
    # this is one record or None.
    return {"scenarios": [scenarios.summary(s) for s in items],
            "frozen": budgetversions.approved_freeze()}


@app.get("/api/scenario-options", dependencies=Gated)
def scenario_options() -> dict:
    """What a scenario may name — drivers, product lines, cost centres, bases.

    Thin onto tools.scenario_options, which reads what the tool's own validation
    reads, so the edit form cannot offer a key the validator then rejects. 200
    with available: false rather than a 4xx, like /api/cash: "there is no budget
    plan yet" is a state the form renders as a reason.

    A flat path deliberately — /api/scenarios/options would be swallowed by the
    /api/scenarios/{scenario_id} route below.
    """
    result = tools.scenario_options()
    if "error" in result:
        return {"available": False, **result}
    return {"available": True, **result}


@app.get("/api/scenarios/{scenario_id}", dependencies=Gated)
def get_scenario(scenario_id: str) -> dict:
    s = scenarios.get_scenario(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail={"error": "scenario not found"})
    return s


@app.put("/api/scenarios/{scenario_id}", dependencies=Gated)
def update_scenario(scenario_id: str, payload: ScenarioEditRequest) -> dict:
    """Recompute one scenario in place from an edited assumption set.

    Thin, and onto the SAME tools.build_budget_scenario the model calls — the rule
    POST /api/assumptions/lock follows. One implementation of "what these
    assumptions are worth", so the figure on the card and the figure in an answer
    cannot disagree.

    The frozen check is HERE because this is the only module allowed to join the
    two halves: tools.py imports no budgetversions and scenarios.py must not.
    """
    existing = scenarios.get_scenario(scenario_id)
    if not existing:
        raise HTTPException(status_code=404, detail={"error": "scenario not found"})

    frozen = budgetversions.approved_freeze_of(scenario_id)
    if frozen:
        # 409, not 400: the request is well formed, the object's state forbids it.
        by = f" by {frozen['approved_by']}" if frozen.get("approved_by") else ""
        raise HTTPException(status_code=409, detail={
            "error": f"v{frozen['version_no']} was approved{by} from this scenario, so it is "
                     "frozen. The approved budget has to keep saying what was approved. Build a "
                     "new scenario, or approve a later version first.",
            "version_id": frozen["version_id"], "version_no": frozen["version_no"]})

    result = tools.build_budget_scenario(
        payload.name, assumptions=payload.assumptions, note=payload.note,
        basis=payload.basis or existing.get("basis") or "locked",
        price_pass_through=(float(existing.get("price_pass_through") or 0.0)
                            if payload.price_pass_through is None
                            else payload.price_pass_through),
        opex_inflation_pct=(float(existing.get("opex_inflation_pct") or 0.0)
                            if payload.opex_inflation_pct is None
                            else payload.opex_inflation_pct),
        # make_active is deliberately NOT passed: activation stays POST /activate.
        # One lever per door, and an edit that silently moved the budget onto a
        # scenario would be a trap.
        replace_scenario_id=scenario_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    return result


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
# Cash
# --------------------------------------------------------------------------

def _cash_for(scenario_id: str | None = None, *,
              include_proposed_capex: bool = True) -> dict:
    """The cash profile, or an {"error"} dict — never an exception.

    Thin, and deliberately onto the SAME `tools.cash_flow_projection` the model
    calls, for the reason POST /api/assumptions/lock is thin onto
    drivers.lock_assumptions: the CFO gets a second door to one implementation,
    never a second implementation. It is also what makes the Budget page's cash
    line and the agent's answer about cash the same numbers.
    """
    try:
        return tools.cash_flow_projection(
            scenario_id=scenario_id, include_proposed_capex=include_proposed_capex)
    except Exception as exc:                                  # pragma: no cover
        return {"error": f"Could not build the cash profile: {type(exc).__name__}: {exc}"}


@app.get("/api/cash", dependencies=Gated)
def cash_profile(scenario_id: str | None = Query(None),
                 include_proposed_capex: bool = Query(True)) -> dict:
    result = _cash_for(scenario_id, include_proposed_capex=include_proposed_capex)
    if "error" in result:
        # 200 with the reason, not a 4xx: "we have not measured your working
        # capital yet" is a state the Budget page renders as a prompt to fix it,
        # not a failure. Same shape as /api/budget-state's available: false.
        return {"available": False, **result,
                "working_capital": profile_mod.working_capital()}
    return {"available": True, **result,
            "working_capital": profile_mod.working_capital()}


# --------------------------------------------------------------------------
# Locked assumptions — the CFO's door to the same function the model uses
# --------------------------------------------------------------------------

@app.post("/api/assumptions/lock", dependencies=Gated)
def lock_assumptions(payload: LockRequest) -> dict:
    """Freeze the assumption set from the UI.

    Deliberately thin: it calls the SAME drivers.lock_assumptions the model's
    tool calls, so there is one implementation of the frozen position and one
    place its validation lives. Locking replaces the whole set — that is the
    existing semantics, and the form posts every driver for exactly that reason.
    """
    result = drivers.lock_assumptions(payload.assumptions,
                                      scenario_id=payload.scenario_id,
                                      note=payload.note)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result)
    return result


# --------------------------------------------------------------------------
# Budget versions
# --------------------------------------------------------------------------

def _drivers_snapshot() -> tuple[list[dict], str | None]:
    """The driver provenance a version freezes: locked value, the URL it was read
    from, and when. Copied into the version rather than referenced, so a later
    re-lock cannot rewrite what was approved."""
    payload = drivers.load_assumptions()
    locked, locked_at = payload.get("assumptions") or {}, payload.get("locked_at")
    rows = []
    for d in drivers.driver_status():
        entry = locked.get(d["driver_id"]) or {}
        value = entry.get("value")
        rows.append({
            "driver_id": d["driver_id"],
            "name": d.get("name"),
            "unit": entry.get("unit") or d.get("unit"),
            "value": value if value is not None else d.get("latest_value"),
            "basis": "locked" if value is not None else "latest observation",
            "latest_value": d.get("latest_value"),
            "source_url": entry.get("source_url") or d.get("latest_source_url"),
            "retrieved_at": d.get("last_verified_utc"),
            "locked_at": locked_at,
            "rationale": entry.get("rationale"),
        })
    return rows, locked_at


def _company_name() -> str:
    return ((profile_mod.get_profile().get("company") or {}).get("name") or "").strip()


def _version_or_404(version_id: str) -> dict:
    version = budgetversions.get_version(version_id)
    if not version:
        raise HTTPException(status_code=404, detail={"error": "version not found"})
    return version


def _comparison_for(version: dict) -> dict | None:
    """The version this one is naturally read against: its parent, else the
    highest version below it. None for the first version ever created."""
    parent_id = version.get("parent_version_id")
    if parent_id:
        parent = budgetversions.get_version(parent_id)
        if parent:
            return parent
    below = [v for v in budgetversions.list_versions()
             if (v.get("version_no") or 0) < (version.get("version_no") or 0)]
    return below[0] if below else None


@app.get("/api/budget/versions", dependencies=Gated)
def list_budget_versions() -> dict:
    items = budgetversions.list_versions()
    active = scenarios.get_active()
    return {
        "versions": [budgetversions.summary(v) for v in items],
        "approved_id": (budgetversions.approved_version() or {}).get("id"),
        "active_scenario": scenarios.summary(active) if active else None,
        # Seeds the approval attestation field. There is no authentication here,
        # so this is a default name, never an identity.
        "default_approver": _company_name(),
    }


@app.post("/api/budget/versions", dependencies=Gated)
def create_budget_version(payload: VersionRequest) -> dict:
    scenario = (scenarios.get_scenario(payload.scenario_id) if payload.scenario_id
                else scenarios.get_active())
    if not scenario:
        raise HTTPException(status_code=404, detail={
            "error": "No scenario to version. Build one on the Scenarios page first."})
    snapshot, locked_at = _drivers_snapshot()
    # Frozen with the driver provenance, and for the same reason: the days were
    # measured off the balance sheet as it stood today, so next quarter's
    # measurement must not rewrite the funding case that was approved. A company
    # with no working-capital history freezes None and the export says so.
    cash = _cash_for(scenario.get("id"))
    try:
        version = budgetversions.create_version(
            scenario, label=payload.label, note=payload.note,
            created_by=payload.created_by, drivers_snapshot=snapshot,
            locked_at=locked_at,
            cash_snapshot=None if "error" in cash else cash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    return budgetversions.summary(version)


@app.get("/api/budget/versions/{version_id}", dependencies=Gated)
def get_budget_version(version_id: str) -> dict:
    return _version_or_404(version_id)


@app.post("/api/budget/versions/{version_id}/submit", dependencies=Gated)
def submit_budget_version(version_id: str, payload: ApprovalRequest) -> dict:
    try:
        return budgetversions.summary(
            budgetversions.submit(version_id, payload.by, payload.note))
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": "version not found"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})


@app.post("/api/budget/versions/{version_id}/approve", dependencies=Gated)
def approve_budget_version(version_id: str, payload: ApprovalRequest) -> dict:
    try:
        return budgetversions.summary(
            budgetversions.approve(version_id, payload.by or "", payload.note))
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": "version not found"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})


@app.delete("/api/budget/versions/{version_id}", dependencies=Gated)
def delete_budget_version(version_id: str) -> dict:
    try:
        deleted = budgetversions.delete_version(version_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "version not found"})
    return {"deleted": True}


@app.get("/api/budget/versions/{version_id}/diff", dependencies=Gated)
def diff_budget_version(version_id: str, against: str | None = Query(None)) -> dict:
    version = _version_or_404(version_id)
    other = (budgetversions.get_version(against) if against
             else _comparison_for(version))
    if not other:
        raise HTTPException(status_code=404, detail={
            "error": "no earlier version to compare against"})
    return budgetversions.diff(other, version)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def _xlsx(pack: dict) -> Response:
    try:
        content = export.workbook(pack)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)})
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{export.filename(pack, "xlsx")}"'})


def _markdown(pack: dict) -> Response:
    return Response(
        content=export.board_pack_markdown(pack), media_type="text/markdown",
        headers={"Content-Disposition":
                 f'attachment; filename="{export.filename(pack, "md")}"'})


def _scenario_pack(scenario_id: str) -> dict:
    scenario = scenarios.get_scenario(scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail={"error": "scenario not found"})
    snapshot, locked_at = _drivers_snapshot()
    cash = _cash_for(scenario_id)
    return export.scenario_pack(scenario, drivers_snapshot=snapshot,
                                locked_at=locked_at, company=_company_name(),
                                cash=None if "error" in cash else cash)


def _version_export_pack(version_id: str) -> dict:
    version = _version_or_404(version_id)
    other = _comparison_for(version)
    return export.version_pack(
        version, company=_company_name(),
        diff=budgetversions.diff(other, version) if other else None)


@app.get("/api/scenarios/{scenario_id}/export.xlsx", dependencies=Gated)
def export_scenario_xlsx(scenario_id: str) -> Response:
    return _xlsx(_scenario_pack(scenario_id))


@app.get("/api/scenarios/{scenario_id}/export.md", dependencies=Gated)
def export_scenario_md(scenario_id: str) -> Response:
    return _markdown(_scenario_pack(scenario_id))


@app.get("/api/budget/versions/{version_id}/export.xlsx", dependencies=Gated)
def export_version_xlsx(version_id: str) -> Response:
    return _xlsx(_version_export_pack(version_id))


@app.get("/api/budget/versions/{version_id}/export.md", dependencies=Gated)
def export_version_md(version_id: str) -> Response:
    return _markdown(_version_export_pack(version_id))


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
