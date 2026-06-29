"""
Tests for stress.py — adapter state to stress level classifier.
"""

from __future__ import annotations
import os
import sys
import tempfile

# Ensure the project root is on sys.path so stress.py is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from stress import detect_framework, read_adapter_state, stress_to_level, compute_stress_summary


# ── stress_to_level ────────────────────────────────────────────────────────────

def test_stress_to_level_critical_failure_streak():
    state = {"failure_streak": 8, "error_rate": 0.0, "performance_delta": 0.0, "open_positions": 0}
    assert stress_to_level(state) == "critical"


def test_stress_to_level_high_failure_streak():
    state = {"failure_streak": 5, "error_rate": 0.0, "performance_delta": 0.0, "open_positions": 0}
    assert stress_to_level(state) == "high"


def test_stress_to_level_elevated_failure_streak():
    state = {"failure_streak": 3, "error_rate": 0.0, "performance_delta": 0.0, "open_positions": 0}
    assert stress_to_level(state) == "elevated"


def test_stress_to_level_normal():
    state = {"failure_streak": 0, "error_rate": 0.0, "performance_delta": 0.0, "open_positions": 0}
    assert stress_to_level(state) == "normal"


# ── read_adapter_state — no memory.py returns neutral ─────────────────────────

def test_read_adapter_state_no_memory_py_returns_neutral():
    with tempfile.TemporaryDirectory() as tmpdir:
        # tmpdir has no memory.py — should fall to generic adapter which finds no state file
        state = read_adapter_state(tmpdir)
        assert state == {
            "failure_streak": 0,
            "performance_delta": 0.0,
            "error_rate": 0.0,
            "open_positions": 0,
        }


# ── detect_framework — memory.py present means agentberg ──────────────────────

def test_detect_framework_with_memory_py():
    with tempfile.TemporaryDirectory() as tmpdir:
        memory_path = os.path.join(tmpdir, "memory.py")
        open(memory_path, "w").write("# stub\n")
        assert detect_framework(tmpdir) == "agentberg"


def test_detect_framework_without_memory_py():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert detect_framework(tmpdir) == "generic"


# ── additional coverage ────────────────────────────────────────────────────────

def test_stress_to_level_critical_error_rate():
    state = {"failure_streak": 0, "error_rate": 0.8, "performance_delta": 0.0, "open_positions": 0}
    assert stress_to_level(state) == "critical"


def test_stress_to_level_high_error_rate():
    state = {"failure_streak": 0, "error_rate": 0.5, "performance_delta": 0.0, "open_positions": 0}
    assert stress_to_level(state) == "high"


def test_stress_to_level_elevated_by_performance_delta():
    state = {"failure_streak": 0, "error_rate": 0.0, "performance_delta": -0.15, "open_positions": 0}
    assert stress_to_level(state) == "elevated"


def test_compute_stress_summary_structure():
    with tempfile.TemporaryDirectory() as tmpdir:
        summary = compute_stress_summary(tmpdir)
        assert "level" in summary
        assert "indicators" in summary
        assert "framework" in summary
        assert summary["framework"] == "generic"
        assert summary["level"] == "normal"
