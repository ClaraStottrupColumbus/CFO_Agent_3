# Which of the two configured models runs a given turn.
#
# The decision is split across three pure pieces on purpose — config resolves
# the two slots off disk, reporting picks between them off the session, tasks
# lets a scheduled job override both — and each piece fails silently rather than
# loudly if it drifts: a report quietly answered by the cheap model looks like a
# worse report, not like a bug.

import json

import pytest

from app import config, reporting, tasks
from app.agent import (AVAILABLE_MODELS, DEFAULT_MODEL_GENERAL, DEFAULT_MODEL_HEAVY)


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Point the module-level settings path at a tmp file, per the convention in
    test_rules.py. config reads and writes it under a lock on every call."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    return path


# ---------- config: two slots, resolved independently ----------

def test_defaults_put_the_expensive_model_on_reports(settings_file):
    params = config.get_run_params()
    assert params["model_heavy"] == DEFAULT_MODEL_HEAVY
    assert params["model"] == DEFAULT_MODEL_GENERAL


def test_model_is_the_general_slot_not_the_heavy_one(settings_file):
    """Load-bearing naming. Every pre-split reader of params["model"] — the
    budget-page narrative, the alert narrative, driver verification, the
    assumption refresh — is a surface that should be general, so they all got
    the cheap model with no edit. Flip this and four surfaces silently move onto
    the expensive model."""
    config.save_settings({"models": {"heavy": "claude-opus-5", "general": "claude-sonnet-5"}})
    assert config.get_run_params()["model"] == "claude-sonnet-5"


def test_a_legacy_single_model_file_migrates_into_the_heavy_slot(settings_file):
    # Every settings.json written before the split is a flat string. _merge
    # would have left it in place, dead, while the stored choice was replaced
    # by the default.
    settings_file.write_text(json.dumps({"model": "claude-opus-4-8", "show_debug": True}))
    cfg = config.get_settings()
    assert cfg["models"]["heavy"] == "claude-opus-4-8"
    assert cfg["models"]["general"] == DEFAULT_MODEL_GENERAL
    assert "model" not in cfg          # the dead key does not survive the read
    assert cfg["show_debug"] is True   # and nothing else about the file is lost


def test_a_real_two_slot_payload_beats_the_legacy_key(settings_file):
    settings_file.write_text(json.dumps(
        {"model": "claude-opus-4-8", "models": {"heavy": "claude-opus-5"}}))
    assert config.get_settings()["models"]["heavy"] == "claude-opus-5"


def test_a_legacy_key_naming_a_retired_model_still_lands_on_a_default(settings_file):
    settings_file.write_text(json.dumps({"model": "claude-2.1"}))
    assert config.get_settings()["models"]["heavy"] == DEFAULT_MODEL_HEAVY


def test_one_bad_slot_does_not_reset_the_other(settings_file):
    settings_file.write_text(json.dumps(
        {"models": {"heavy": "claude-sonnet-5", "general": "gpt-9"}}))
    models = config.get_settings()["models"]
    assert models["heavy"] == "claude-sonnet-5"          # the CFO's choice survives
    assert models["general"] == DEFAULT_MODEL_GENERAL    # only the bad one is coerced


def test_saving_one_slot_preserves_the_sibling(settings_file):
    """The settings panel posts one slot at a time, relying on _merge's
    key-by-key nested merge. A shallow replace here would blank whichever select
    the CFO did not touch."""
    config.save_settings({"models": {"heavy": "claude-opus-4-8"}})
    config.save_settings({"models": {"general": "claude-opus-5"}})
    models = config.get_settings()["models"]
    assert models == {"heavy": "claude-opus-4-8", "general": "claude-opus-5"}


def test_reading_settings_never_mutates_the_module_defaults(settings_file):
    """_merge is a SHALLOW copy of the base, so cfg["models"] can be the very
    same object as DEFAULT_SETTINGS["models"] when the file carries no `models`
    key — which is exactly the legacy case. Writing into that slot instead of
    replacing it would rewrite the defaults for the life of the process, and no
    later fix to the file would undo it."""
    settings_file.write_text(json.dumps({"model": "claude-opus-4-8"}))
    config.get_settings()
    assert config.DEFAULT_SETTINGS["models"] == {"heavy": DEFAULT_MODEL_HEAVY,
                                                 "general": DEFAULT_MODEL_GENERAL}


def test_every_offered_model_is_accepted_in_either_slot(settings_file):
    for model in AVAILABLE_MODELS:
        config.save_settings({"models": {"heavy": model, "general": model}})
        assert config.get_settings()["models"] == {"heavy": model, "general": model}


# ---------- reporting: the tier is read off the session ----------

PARAMS = {"model": "general-model", "model_heavy": "heavy-model"}


@pytest.mark.parametrize("kind", ["weekly", "monthly"])
def test_a_report_runs_on_the_heavy_model(kind):
    assert reporting.model_for_session({"kind": kind}, PARAMS) == "heavy-model"


def test_chat_runs_on_the_general_model():
    assert reporting.model_for_session({"kind": "chat"}, PARAMS) == "general-model"


@pytest.mark.parametrize("kind", ["weekly", "monthly"])
def test_a_follow_up_under_a_report_runs_on_the_general_model(kind):
    """The half of the rule that is easy to get wrong, and expensive if you do.

    A follow-up thread is NOT kind "chat" — startThread and
    resolveTargetSession both create it with the report's own kind plus a
    parent_id. Keying on kind alone therefore puts every question anyone asks
    about a report onto the expensive model, silently, forever."""
    child = {"kind": kind, "parent_id": "abc123"}
    assert reporting.model_for_session(child, PARAMS) == "general-model"


def test_a_session_with_no_kind_runs_on_the_general_model():
    assert reporting.model_for_session({}, PARAMS) == "general-model"


def test_a_pre_split_params_dict_still_runs():
    # run_session_turn is callable from tests and from background modules that
    # may hand it a params dict built before the split; a missing heavy slot
    # must degrade to the one model present, not to None.
    assert reporting.model_for_session({"kind": "weekly"}, {"model": "only-model"}) == "only-model"


def test_heavy_kinds_are_exactly_the_report_kinds():
    from app import store
    assert set(reporting.HEAVY_KINDS) < set(store.KINDS)
    assert "chat" not in reporting.HEAVY_KINDS


# ---------- tasks: an override wins both slots ----------

def _task(**extra):
    return {"id": "t1", "name": "n", "type": "driver_scan", "model": None,
            "reasoning": False, **extra}


def test_a_task_without_an_override_inherits_both_tiers():
    out = tasks._params_for(_task(), dict(PARAMS))
    assert out["model"] == "general-model"
    assert out["model_heavy"] == "heavy-model"


def test_a_task_override_wins_both_slots():
    """A report task resolves through the heavy slot, so overriding only `model`
    would leave a pinned weekly scan on the configured heavy model and read as
    the override being ignored."""
    out = tasks._params_for(_task(model="claude-sonnet-5"), dict(PARAMS))
    assert out["model"] == "claude-sonnet-5"
    assert out["model_heavy"] == "claude-sonnet-5"


def test_an_override_naming_a_retired_model_falls_back_to_the_configured_tiers():
    out = tasks._params_for(_task(model="claude-2.1"), dict(PARAMS))
    assert out["model"] == "general-model"
    assert out["model_heavy"] == "heavy-model"


def test_params_for_does_not_mutate_the_shared_settings_dict():
    # The scheduler builds params once and runs several tasks off it.
    shared = dict(PARAMS)
    tasks._params_for(_task(model="claude-opus-5"), shared)
    assert shared == PARAMS
