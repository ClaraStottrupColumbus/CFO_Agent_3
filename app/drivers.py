# drivers.py — The driver catalog, the observation log, and the trust boundary.
#
# Server-side web tools return results into the model's CONTEXT, not to disk, so
# nothing downstream can compute on them. record_driver_observation closes that
# loop — it is the only path from web research to pandas — which also makes
# driver_prices the one model-writable dataset in the system. Two pure guards
# stand in front of that write:
#
#   verify_source_url  — the cited page must actually have been visited this turn
#   check_sanity_band  — the price must be within 0.2x-5.0x the last known value
#
# Both are pure predicates, factored out of the I/O path deliberately: they are
# the highest-consequence code here, and inline they would be verifiable only by
# hand in a browser. tests/test_driver_guards.py covers them.
#
# The I/O around them is append-only with a revision column — seeded history is
# never mutated — using the same atomic .tmp + replace() under a lock as
# store.py, because a scheduler worker and an interactive chat can write
# concurrently.

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .citations import normalise_url

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DRIVERS_FILE = DATA_DIR / "drivers.parquet"
PRICES_FILE = DATA_DIR / "driver_prices.parquet"
PRICES_CSV = DATA_DIR / "driver_prices.csv"
ASSUMPTIONS_FILE = DATA_DIR / "locked_assumptions.json"

# A new observation must land within this multiple of the last known price.
# Wide enough that real commodity moves pass; narrow enough that a
# units-confusion (EUR/t read as EUR/kg) or a hallucinated figure is caught.
SANITY_MIN_FACTOR = 0.2
SANITY_MAX_FACTOR = 5.0

_lock = threading.Lock()


# --------------------------------------------------------------------------
# The two pure guards
# --------------------------------------------------------------------------

def verify_source_url(url: str | None, fetched_urls) -> bool:
    """Was `url` actually visited this turn?

    Both sides go through normalise_url, so a URL that differs from the visited
    one only by a trailing slash, host casing, a `www.` prefix, a fragment or a
    `utm_*` parameter still verifies. That symmetry is the whole point: comparing
    raw URLs here while citations.py stored normalised ones would refuse every
    legitimate observation, and the agent would dutifully report that it cannot
    verify its own sources with nothing in the logs pointing at the comparison.

    An empty visited set refuses everything — a turn that did no web research
    has no business recording a web-sourced price.
    """
    key = normalise_url(url)
    if not key:
        return False
    return key in {normalise_url(u) for u in (fetched_urls or set())}


def check_sanity_band(price, previous, *, override: bool = False) -> dict | None:
    """None when the price is acceptable; a teachable error dict when it is not.

    The error names the previous value so the model can either correct a
    units mistake or justify a genuine spike by re-calling with
    override_sanity_check — it must be able to record a real 3x move, just not
    silently.

    A first observation (no previous value) is always accepted: there is nothing
    to compare against, and refusing would make a new driver un-seedable.
    """
    try:
        p = float(price)
    except (TypeError, ValueError):
        return {"error": f"price must be a number, got {price!r}."}
    if p != p or p in (float("inf"), float("-inf")):
        return {"error": "price must be a finite number."}
    if p <= 0:
        return {"error": f"price must be greater than zero, got {p}."}

    if previous is None:
        return None
    try:
        prev = float(previous)
    except (TypeError, ValueError):
        return None
    if prev <= 0:
        return None
    if override:
        return None

    low, high = prev * SANITY_MIN_FACTOR, prev * SANITY_MAX_FACTOR
    if low <= p <= high:
        return None
    return {
        "error": (f"Price {p:g} is outside the sanity band {low:g}–{high:g} "
                  f"(0.2x–5.0x the last known value of {prev:g}). If this move is "
                  f"real, re-call with override_sanity_check: true and say in your "
                  f"answer why the price moved this far. If it is not, check the "
                  f"units and the currency of the figure you read."),
        "previous_value": prev,
        "sanity_min": low,
        "sanity_max": high,
        "rejected_value": p,
    }


def parse_month(value) -> str | None:
    """Accept 'YYYY-MM', 'YYYY-MM-DD' or a date; return 'YYYY-MM' or None."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m")
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text[:7]
    return None


# --------------------------------------------------------------------------
# Catalog + observation I/O (the tested guards sit inside these)
# --------------------------------------------------------------------------

def _read(path: Path, csv_fallback: Path | None = None) -> pd.DataFrame:
    """Prefer Parquet, fall back to CSV so tests can use CSV fixtures."""
    if path.exists():
        return pd.read_parquet(path)
    csv = csv_fallback or path.with_suffix(".csv")
    if csv.exists():
        return pd.read_csv(csv)
    return pd.DataFrame()


def load_catalog() -> pd.DataFrame:
    return _read(DRIVERS_FILE)


def load_prices() -> pd.DataFrame:
    df = _read(PRICES_FILE, PRICES_CSV)
    if df.empty:
        return df
    if "revision" not in df.columns:
        df["revision"] = 0
    return df


def get_driver(driver_id: str) -> dict | None:
    cat = load_catalog()
    if cat.empty or "driver_id" not in cat.columns:
        return None
    hit = cat[cat["driver_id"] == driver_id]
    return None if hit.empty else hit.iloc[0].to_dict()


def known_driver_ids() -> list[str]:
    cat = load_catalog()
    if cat.empty or "driver_id" not in cat.columns:
        return []
    return [str(d) for d in cat["driver_id"].tolist()]


def latest_observation(driver_id: str, prices: pd.DataFrame | None = None) -> dict | None:
    """The most recent observation for a driver — newest month, then newest revision."""
    df = load_prices() if prices is None else prices
    if df.empty or "driver_id" not in df.columns:
        return None
    rows = df[df["driver_id"] == driver_id]
    if rows.empty:
        return None
    rows = rows.sort_values(["month", "revision"], kind="stable")
    return rows.iloc[-1].to_dict()


def _write_prices(df: pd.DataFrame) -> None:
    """Atomic write of both representations, under the module lock."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_pq = PRICES_FILE.with_suffix(".parquet.tmp")
    df.to_parquet(tmp_pq, index=False)
    tmp_pq.replace(PRICES_FILE)
    tmp_csv = PRICES_CSV.with_suffix(".csv.tmp")
    df.to_csv(tmp_csv, index=False)
    tmp_csv.replace(PRICES_CSV)


def append_observation(driver_id: str, price, month, *, source_url: str,
                       fetched_urls=None, currency: str | None = None,
                       source: str | None = None, note: str | None = None,
                       override_sanity_check: bool = False,
                       verify_provenance: bool = True) -> dict:
    """Append one observation after both guards pass. Returns a result or an
    {"error": …} dict — teachable, never an exception, so the model can correct
    itself inside the same turn.

    `verify_provenance=False` is for the CFO-initiated manual path (a value typed
    into the drivers page), which has a human behind it rather than a citation.
    """
    driver = get_driver(driver_id)
    if driver is None:
        known = known_driver_ids()
        return {"error": f"Unknown driver_id '{driver_id}'. Valid ids: "
                         f"{', '.join(known) if known else '(none — run setup first)'}."}

    parsed_month = parse_month(month)
    if not parsed_month:
        return {"error": f"Could not parse month {month!r}. Use 'YYYY-MM'."}

    if verify_provenance and not verify_source_url(source_url, fetched_urls):
        return {"error": (f"source_url {source_url!r} was not fetched in this turn, so the "
                          f"observation was NOT recorded. Every recorded price must come "
                          f"from a page you actually visited: search for the figure, fetch "
                          f"the page with web_fetch, then record it citing that exact URL.")}

    with _lock:
        prices = load_prices()
        prev_row = latest_observation(driver_id, prices)
        previous = prev_row.get("price") if prev_row else None

        rejection = check_sanity_band(price, previous, override=override_sanity_check)
        if rejection:
            return rejection

        existing = pd.DataFrame() if prices.empty else prices[
            (prices["driver_id"] == driver_id) & (prices["month"] == parsed_month)]
        revision = 0 if existing.empty else int(existing["revision"].max()) + 1

        row = {
            "month": parsed_month,
            "driver_id": driver_id,
            "price": float(price),
            "currency": currency or driver.get("quote_currency") or "EUR",
            "source": source or "agent_research",
            "source_url": str(source_url or ""),
            "revision": revision,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": note or "",
        }
        updated = pd.concat([prices, pd.DataFrame([row])], ignore_index=True)
        _write_prices(updated)

    return {
        "recorded": True, "driver_id": driver_id, "month": parsed_month,
        "price": float(price), "currency": row["currency"], "revision": revision,
        "previous_value": previous,
        "change_pct": (None if not previous or float(previous) == 0
                       else (float(price) - float(previous)) / float(previous) * 100.0),
        "source_url": row["source_url"],
        "source_file": PRICES_FILE.name,
    }


# --------------------------------------------------------------------------
# Locked assumptions — the CFO's frozen positions
# --------------------------------------------------------------------------

def load_assumptions() -> dict:
    if not ASSUMPTIONS_FILE.exists():
        return {"locked_at": None, "scenario_id": None, "assumptions": {}}
    try:
        return json.loads(ASSUMPTIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"locked_at": None, "scenario_id": None, "assumptions": {}}


def lock_assumptions(assumptions: dict, *, scenario_id: str | None = None,
                     note: str | None = None) -> dict:
    """Freeze an assumption set with its provenance.

    Split out from append_observation on purpose: routine research must not be
    able to overwrite the CFO's locked position while recording a market price.
    """
    if not isinstance(assumptions, dict) or not assumptions:
        return {"error": "assumptions must be a non-empty object of "
                         "{driver_id: {value, source_url, …}}."}

    known = set(known_driver_ids())
    clean: dict = {}
    for driver_id, entry in assumptions.items():
        if known and driver_id not in known:
            return {"error": f"Unknown driver_id '{driver_id}'. Valid ids: "
                             f"{', '.join(sorted(known))}."}
        entry = entry if isinstance(entry, dict) else {"value": entry}
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            return {"error": f"Driver '{driver_id}': value must be a number."}
        clean[driver_id] = {
            "value": value,
            "unit": entry.get("unit"),
            "source_url": entry.get("source_url"),
            "rationale": entry.get("rationale"),
        }

    payload = {
        "locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scenario_id": scenario_id,
        "note": note,
        "assumptions": clean,
    }
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ASSUMPTIONS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(ASSUMPTIONS_FILE)
    return {"locked": True, "count": len(clean), "locked_at": payload["locked_at"],
            "source_file": ASSUMPTIONS_FILE.name}


# --------------------------------------------------------------------------
# Status — what both the monthly report and two rules need
# --------------------------------------------------------------------------

def driver_status(now: datetime | None = None) -> list[dict]:
    """Locked assumption vs latest observation vs staleness, per driver.

    `adverse` is derived from the driver's own adverse_direction rather than the
    sign of the move — a rising ingredient price is bad, a rising sales price is
    good, and colouring by sign is the single easiest way to make a budgeting UI
    lie.
    """
    now = now or datetime.now(timezone.utc)
    catalog = load_catalog()
    prices = load_prices()
    locked = load_assumptions().get("assumptions", {})

    out = []
    if catalog.empty:
        return out

    for _, row in catalog.iterrows():
        driver_id = str(row["driver_id"])
        obs = latest_observation(driver_id, prices)
        lock = locked.get(driver_id) or {}
        locked_value = lock.get("value")
        latest_value = float(obs["price"]) if obs else None

        drift_pct = None
        if locked_value not in (None, 0) and latest_value is not None:
            drift_pct = (latest_value - float(locked_value)) / float(locked_value) * 100.0

        age_days, verify_status = None, "never_verified"
        if obs and obs.get("recorded_at"):
            try:
                seen = datetime.fromisoformat(str(obs["recorded_at"]))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                age_days = (now - seen).total_seconds() / 86400.0
            except ValueError:
                age_days = None
        stale_after = float(row.get("stale_after_days") or 7)
        if age_days is not None:
            verify_status = "stale" if age_days > stale_after else "fresh"

        adverse_direction = str(row.get("adverse_direction") or "up")
        adverse = None
        if drift_pct is not None and abs(drift_pct) > 1e-9:
            moving_up = drift_pct > 0
            adverse = moving_up if adverse_direction == "up" else not moving_up

        out.append({
            "driver_id": driver_id,
            "name": row.get("name") or driver_id,
            "category": row.get("category"),
            "unit": row.get("unit"),
            "quote_currency": row.get("quote_currency"),
            "hedge_coverage": float(row.get("hedge_coverage") or 0.0),
            "adverse_direction": adverse_direction,
            "stale_after_days": stale_after,
            "latest_value": latest_value,
            "latest_month": obs.get("month") if obs else None,
            "latest_source_url": obs.get("source_url") if obs else None,
            "last_verified_utc": obs.get("recorded_at") if obs else None,
            "age_days": None if age_days is None else round(age_days, 2),
            "verify_status": verify_status,
            "locked_value": locked_value,
            "drift_pct": drift_pct,
            "adverse": adverse,
        })
    return out
