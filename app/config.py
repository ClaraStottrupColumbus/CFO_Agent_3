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

from .agent import (AVAILABLE_MODELS, DEFAULT_EFFORT, DEFAULT_MODEL, EFFORT_LEVELS,
                    clamp_effort)

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "data" / "settings.json"

DEFAULT_SETTINGS: dict = {
    "model": DEFAULT_MODEL,
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


def _normalise(cfg: dict) -> dict:
    if cfg.get("model") not in AVAILABLE_MODELS:
        cfg["model"] = DEFAULT_MODEL

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
    return _normalise(_merge(DEFAULT_SETTINGS, stored))


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
    """
    cfg = get_settings()
    return {
        "model": cfg["model"],
        "reasoning": cfg["reasoning"]["enabled"],
        "effort": cfg["reasoning"]["effort"],
        "research": cfg["research"],
    }
