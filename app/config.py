# config.py — Runtime settings (model, reasoning, research, notifications).
# Code defaults merged over data/settings.json, written atomically under a lock —
# the pattern ported from CFO_Agent_2's config.py.
#
# Rule thresholds deliberately do NOT live here: they stay in data/rules.json
# behind /api/rules. Two homes for one number is how thresholds drift.

from __future__ import annotations

import json
import threading
from pathlib import Path

from .agent import (AVAILABLE_MODELS, DEFAULT_EFFORT, DEFAULT_MODEL_GENERAL,
                    DEFAULT_MODEL_HEAVY, EFFORT_LEVELS, clamp_effort)

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "data" / "settings.json"

# Model slots, not a single model. `heavy` runs the two reports — the weekly
# market scan and the monthly budget revision — because those are the turns
# whose reasoning has to survive a board asking why. `general` runs everything
# else: chat, the setup proposal, alert narratives, driver verification and the
# Budget page's read. Which surface gets which is decided in reporting.py, off
# the session, and never here.
MODEL_SLOTS = ("heavy", "general")
DEFAULT_MODEL_FOR_SLOT = {"heavy": DEFAULT_MODEL_HEAVY, "general": DEFAULT_MODEL_GENERAL}

DEFAULT_SETTINGS: dict = {
    "models": dict(DEFAULT_MODEL_FOR_SLOT),
    # The reasoning toggle and the effort selector are coupled: disabling
    # thinking is rejected above `high` effort. The coupling is expressed here
    # and in the payload rather than discovered as an API error.
    "reasoning": {"enabled": True, "effort": DEFAULT_EFFORT},
    "research": {"enabled": True, "max_searches": 8, "max_fetches": 8},
    "notifications": {"browser": False},
    "show_debug": False,
}

_lock = threading.Lock()


def _merge(base: dict, override: dict) -> dict:
    """Shallow-deep merge: nested dicts merge key-by-key, everything else replaces."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate(stored: dict) -> dict:
    """Lift a pre-split settings file onto the two-slot schema.

    Runs on the RAW stored dict, before the defaults are merged in — which is
    the whole trick. Done inside _normalise it would be dead code: _merge has
    already populated `models` from DEFAULT_SETTINGS by then, so "the slot is
    unset" is never true and the legacy value could never win. Here, an absent
    `models.heavy` genuinely means the file predates the split.

    It seeds `heavy` because that is where a stored single model belongs — it
    was chosen for the whole app, reports included — and the general slot takes
    its own default. The flat key is dropped rather than left to sit in the file
    forever, dead but still readable, which is how two homes for one setting
    start to disagree.
    """
    stored = dict(stored)
    legacy = stored.pop("model", None)
    if isinstance(legacy, str):
        models = dict(stored.get("models") or {})
        models.setdefault("heavy", legacy)   # a real two-slot payload still wins
        stored["models"] = models
    return stored


def _normalise(cfg: dict) -> dict:
    # Coerced per slot, independently: one bad value must not reset the other.
    models = cfg.get("models") or {}
    cfg["models"] = {slot: (models.get(slot) if models.get(slot) in AVAILABLE_MODELS
                            else DEFAULT_MODEL_FOR_SLOT[slot])
                     for slot in MODEL_SLOTS}
    cfg.pop("model", None)   # belt and braces: nothing downstream reads it

    reasoning = cfg.get("reasoning") or {}
    enabled = bool(reasoning.get("enabled", True))
    effort = reasoning.get("effort")
    if effort not in EFFORT_LEVELS:
        effort = DEFAULT_EFFORT
    # Enforce the coupling in the stored payload, so the UI and the loop agree
    # and the invalid pair can never reach the API.
    cfg["reasoning"] = {"enabled": enabled, "effort": clamp_effort(effort, enabled)}

    research = cfg.get("research") or {}
    cfg["research"] = {
        "enabled": bool(research.get("enabled", True)),
        "max_searches": max(1, min(20, int(research.get("max_searches") or 8))),
        "max_fetches": max(1, min(20, int(research.get("max_fetches") or 8))),
    }
    cfg["notifications"] = {"browser": bool((cfg.get("notifications") or {}).get("browser"))}
    cfg["show_debug"] = bool(cfg.get("show_debug"))
    return cfg


def get_settings() -> dict:
    with _lock:
        stored = {}
        if SETTINGS_FILE.exists():
            try:
                stored = json.loads(SETTINGS_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                stored = {}
    return _normalise(_merge(DEFAULT_SETTINGS, _migrate(stored)))


def save_settings(update: dict) -> dict:
    merged = _normalise(_merge(get_settings(), update or {}))
    with _lock:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, indent=2))
        tmp.replace(SETTINGS_FILE)
    return merged


def get_run_params() -> dict:
    """The zero-arg callable the scheduler is injected with.

    Widened from the reference's `get_model` so background runs carry the same
    reasoning/research configuration as interactive ones. Injected rather than
    imported specifically to avoid the circular import with main.py.

    STAYS ZERO-ARG across the model split, deliberately. Taking a `kind` here
    would have been the obvious shape, but tasks.py receives an already-resolved
    params dict and cannot import config — that circular import is the entire
    reason this callable is injected — so scheduled reports could not have
    resolved their own tier without widening the injection contract. Both models
    travel instead, and reporting.py picks off the session.

    `model` is the GENERAL one, and that is load-bearing: every existing reader
    of params["model"] — the budget-page narrative, the alert narrative, driver
    verification, the assumption refresh — is a surface that should be general,
    so they all get the cheap model with no edit. Only the report path reads
    `model_heavy`, and it opts in explicitly.
    """
    cfg = get_settings()
    return {
        "model": cfg["models"]["general"],
        "model_heavy": cfg["models"]["heavy"],
        "reasoning": cfg["reasoning"]["enabled"],
        "effort": cfg["reasoning"]["effort"],
        "research": cfg["research"],
    }
