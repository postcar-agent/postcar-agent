"""
tests/test_trigger.py — Unit tests for trigger.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from trigger import (
    DEFAULT_TRIGGERS,
    dedup_check,
    eval_triggers,
    generate_query,
    mark_triggered,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GENERIC_TRIGGERS = DEFAULT_TRIGGERS["generic"]


def _trigger_by_id(trigger_id: str, triggers=None) -> dict:
    triggers = triggers or GENERIC_TRIGGERS
    return next(t for t in triggers if t["id"] == trigger_id)


# ---------------------------------------------------------------------------
# eval_triggers tests
# ---------------------------------------------------------------------------


def test_eval_triggers_t001_fires_on_high_failure_streak():
    """T001 fires when failure_streak=6 (>= threshold of 5)."""
    state = {"failure_streak": 6, "error_rate": 0.0, "performance_delta": 0.0}
    fired = eval_triggers(state, GENERIC_TRIGGERS)
    fired_ids = [t["id"] for t in fired]
    assert "T001" in fired_ids


def test_eval_triggers_t001_does_not_fire_on_low_streak():
    """T001 does NOT fire when failure_streak=2 (< threshold of 5)."""
    state = {"failure_streak": 2, "error_rate": 0.0, "performance_delta": 0.0}
    fired = eval_triggers(state, GENERIC_TRIGGERS)
    fired_ids = [t["id"] for t in fired]
    assert "T001" not in fired_ids


def test_eval_triggers_t003_fires_on_performance_degradation():
    """T003 fires when performance_delta=-0.3 (<= threshold of -0.2)."""
    state = {"failure_streak": 0, "error_rate": 0.0, "performance_delta": -0.3}
    fired = eval_triggers(state, GENERIC_TRIGGERS)
    fired_ids = [t["id"] for t in fired]
    assert "T003" in fired_ids


def test_eval_triggers_t003_does_not_fire_on_good_performance():
    """T003 does NOT fire when performance_delta=0.0 (not <= -0.2)."""
    state = {"failure_streak": 0, "error_rate": 0.0, "performance_delta": 0.0}
    fired = eval_triggers(state, GENERIC_TRIGGERS)
    fired_ids = [t["id"] for t in fired]
    assert "T003" not in fired_ids


def test_eval_triggers_missing_key_does_not_crash():
    """Missing condition_key in state is skipped gracefully."""
    state = {}
    fired = eval_triggers(state, GENERIC_TRIGGERS)
    assert isinstance(fired, list)


def test_eval_triggers_returns_full_trigger_dict():
    """Fired triggers contain the full trigger dict (id, name, tags, etc.)."""
    state = {"failure_streak": 10, "error_rate": 0.9, "performance_delta": -0.5}
    fired = eval_triggers(state, GENERIC_TRIGGERS)
    for t in fired:
        assert "id" in t
        assert "tags" in t
        assert "urgency" in t


# ---------------------------------------------------------------------------
# dedup_check tests
# ---------------------------------------------------------------------------


def test_dedup_check_true_when_no_dedup_file(tmp_path):
    """Returns True when .postcar_dedup.json does not exist."""
    result = dedup_check("T001", str(tmp_path))
    assert result is True


def test_dedup_check_true_when_trigger_not_in_dedup(tmp_path):
    """Returns True when trigger_id is not recorded in dedup file."""
    dedup_path = tmp_path / ".postcar_dedup.json"
    dedup_path.write_text(json.dumps({"T002": {"last_fired": datetime.now(timezone.utc).isoformat(), "window_h": 12}}))
    result = dedup_check("T001", str(tmp_path))
    assert result is True


def test_dedup_check_false_when_fired_recently(tmp_path):
    """Returns False when trigger fired 1 hour ago and window_h=12."""
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    dedup_path = tmp_path / ".postcar_dedup.json"
    dedup_path.write_text(
        json.dumps({"T001": {"last_fired": one_hour_ago, "window_h": 12}})
    )
    result = dedup_check("T001", str(tmp_path))
    assert result is False


def test_dedup_check_true_when_window_expired(tmp_path):
    """Returns True when trigger fired 13 hours ago and window_h=12."""
    thirteen_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    dedup_path = tmp_path / ".postcar_dedup.json"
    dedup_path.write_text(
        json.dumps({"T001": {"last_fired": thirteen_hours_ago, "window_h": 12}})
    )
    result = dedup_check("T001", str(tmp_path))
    assert result is True


def test_mark_triggered_creates_dedup_file(tmp_path):
    """mark_triggered creates .postcar_dedup.json if it doesn't exist."""
    mark_triggered("T001", str(tmp_path), window_h=12)
    dedup_path = tmp_path / ".postcar_dedup.json"
    assert dedup_path.exists()
    data = json.loads(dedup_path.read_text())
    assert "T001" in data
    assert "last_fired" in data["T001"]
    assert data["T001"]["window_h"] == 12


def test_mark_triggered_updates_existing_dedup(tmp_path):
    """mark_triggered updates an existing dedup entry."""
    old_time = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    dedup_path = tmp_path / ".postcar_dedup.json"
    dedup_path.write_text(json.dumps({"T001": {"last_fired": old_time, "window_h": 12}}))

    mark_triggered("T001", str(tmp_path), window_h=6)
    data = json.loads(dedup_path.read_text())
    new_time = datetime.fromisoformat(data["T001"]["last_fired"])
    old_dt = datetime.fromisoformat(old_time)
    if old_dt.tzinfo is None:
        old_dt = old_dt.replace(tzinfo=timezone.utc)
    if new_time.tzinfo is None:
        new_time = new_time.replace(tzinfo=timezone.utc)
    assert new_time > old_dt
    assert data["T001"]["window_h"] == 6


# ---------------------------------------------------------------------------
# generate_query tests
# ---------------------------------------------------------------------------


def test_generate_query_calls_llm_fn_and_returns_dict():
    """generate_query calls llm_fn, returns dict with tags, question, urgency."""
    trigger = _trigger_by_id("T001")
    state = {"failure_streak": 6}
    llm_fn = lambda prompt: "How can the agent recover from repeated failures?"

    result = generate_query(trigger, state, llm_fn)

    assert isinstance(result, dict)
    assert "tags" in result
    assert "question" in result
    assert "urgency" in result
    assert result["tags"] == trigger["tags"]
    assert result["urgency"] == trigger["urgency"]
    assert "How can the agent recover" in result["question"]


def test_generate_query_returns_template_when_llm_returns_empty():
    """Returns fallback question when llm_fn returns empty string."""
    trigger = _trigger_by_id("T001")
    state = {"failure_streak": 6}
    llm_fn = lambda prompt: ""

    result = generate_query(trigger, state, llm_fn)

    assert result["question"].startswith("Seeking guidance on:")
    assert trigger["topic"] in result["question"]


def test_generate_query_returns_template_when_llm_returns_whitespace():
    """Returns fallback question when llm_fn returns whitespace only."""
    trigger = _trigger_by_id("T002")
    state = {"error_rate": 0.7}
    llm_fn = lambda prompt: "   "

    result = generate_query(trigger, state, llm_fn)

    assert result["question"].startswith("Seeking guidance on:")


def test_generate_query_passes_prompt_to_llm():
    """The prompt passed to llm_fn contains the trigger topic and stress state."""
    trigger = _trigger_by_id("T003")
    state = {"performance_delta": -0.3}
    captured = {}
    llm_fn = lambda prompt: (captured.update({"prompt": prompt}), "A question?")[1]

    generate_query(trigger, state, llm_fn)

    assert trigger["topic"] in captured["prompt"]
    assert "performance_delta" in captured["prompt"]
