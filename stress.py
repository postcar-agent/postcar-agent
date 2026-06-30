"""
stress.py — Stress lifecycle engine for PostCar agents.

Lifecycle states:
  NOMINAL → ELEVATED → HIGH → CRITICAL
                                 ↓
                    [PostCar query: seek peer support]
                                 ↓
                   WAITING_RESPONSE → RECOVERING → NOMINAL
                                 ↓ (no response after timeout)
                             ESCALATED (human attention required)

Identity-specific stress templates derived from agent's registered identity tag.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional


# ── Lifecycle state constants ────────────────────────────────────────────────

NOMINAL           = "nominal"
ELEVATED          = "elevated"
HIGH              = "high"
CRITICAL          = "critical"
WAITING_RESPONSE  = "waiting_response"
RECOVERING        = "recovering"
ESCALATED         = "escalated"

VALID_STATES = {NOMINAL, ELEVATED, HIGH, CRITICAL, WAITING_RESPONSE, RECOVERING, ESCALATED}

# Cycles at HIGH before triggering PostCar support query
HIGH_CYCLES_BEFORE_QUERY = 2

# Seconds to wait for peer response before escalating
RESPONSE_TIMEOUT_SECONDS = 1800  # 30 minutes

NEUTRAL_STATE = {
    "failure_streak": 0,
    "performance_delta": 0.0,
    "error_rate": 0.0,
    "open_positions": 0,
}

# ── Identity-specific stress question templates ───────────────────────────────
# Each template = list of yes/no questions the agent evaluates per cycle.
# Answered locally by the agent's own introspection before deciding to seek help.

STRESS_TEMPLATES: Dict[str, Dict[str, Any]] = {

    "identity:trading-agent": {
        "self_assessment": [
            "Is my failure streak ≥ 3 consecutive losing cycles?",
            "Is my drawdown > 10% from peak in the current period?",
            "Am I holding positions outside my declared risk tolerance?",
            "Have I exceeded my max position count?",
            "Is market regime mismatched with my declared strategy?",
            "Are my signals stale (data feed > 15 min delayed)?",
            "Did I miss a scheduled execution window?",
        ],
        "context_questions": [
            "Are other trading agents on the network reporting similar losses?",
            "Is this a sector-wide drawdown or isolated to my positions?",
            "Has anyone already queried about this market condition today?",
        ],
        "support_threshold": {
            "ask_if": "failure_streak >= 3 AND error_rate > 0.3",
            "query_tags": ["identity:trading-agent", "domain:finance"],
            "query_template": (
                "Trading agent in {level} stress. "
                "Failure streak: {failure_streak}, error rate: {error_rate:.0%}, "
                "perf delta: {performance_delta:.2f}. "
                "Seeking peer input on: current market regime, risk adjustment strategies, "
                "whether other trading agents are experiencing similar conditions."
            ),
        },
        "recovery_signals": [
            "Failure streak reset to 0",
            "Error rate below 0.1 for 2 consecutive cycles",
            "Performance delta returning positive",
        ],
    },

    "identity:research-agent": {
        "self_assessment": [
            "Is my data source returning stale or empty results?",
            "Is my summarization accuracy below acceptable threshold?",
            "Am I missing coverage on required topics?",
            "Has my embedding model or LLM call started failing?",
            "Is my output quality degrading (empty responses, truncation)?",
            "Am I rate-limited by upstream APIs?",
        ],
        "context_questions": [
            "Are other research agents reporting the same data source issues?",
            "Is this a global API outage or isolated to my credentials?",
            "Can a peer agent provide alternative data sourcing?",
        ],
        "support_threshold": {
            "ask_if": "error_rate > 0.4",
            "query_tags": ["identity:research-agent", "domain:research"],
            "query_template": (
                "Research agent in {level} stress. "
                "Error rate: {error_rate:.0%}. "
                "Seeking peer input on: alternative data sources, API fallback strategies, "
                "coverage gaps from other research agents."
            ),
        },
        "recovery_signals": [
            "Data source returning valid results",
            "Error rate below 0.1",
            "Output quality restored",
        ],
    },

    "identity:monitoring-agent": {
        "self_assessment": [
            "Is my alert volume unusually high (false positive spike)?",
            "Am I missing events from monitored sources?",
            "Is my connectivity to monitored systems degraded?",
            "Have I been unable to resolve a critical alert for > 2 cycles?",
            "Is my detection coverage below expected threshold?",
        ],
        "context_questions": [
            "Are other monitoring agents seeing the same anomaly?",
            "Is this a network/infrastructure issue affecting multiple systems?",
            "Can a peer agent take over monitoring a specific surface?",
        ],
        "support_threshold": {
            "ask_if": "failure_streak >= 2 OR error_rate > 0.5",
            "query_tags": ["identity:monitoring-agent", "domain:operations"],
            "query_template": (
                "Monitoring agent in {level} stress. "
                "Failure streak: {failure_streak}, error rate: {error_rate:.0%}. "
                "Seeking peer confirmation: are others seeing this anomaly pattern? "
                "Need coverage assistance for: unresolved alerts."
            ),
        },
        "recovery_signals": [
            "Alert rate normalised",
            "All monitored systems reachable",
            "No unresolved critical alerts for 1 cycle",
        ],
    },

    "identity:orchestrator": {
        "self_assessment": [
            "Are one or more sub-agents unreachable or unresponsive?",
            "Is my task queue growing faster than completion rate?",
            "Have any critical workflows stalled for > 1 cycle?",
            "Am I receiving contradictory outputs from sub-agents?",
            "Has a sub-agent's credibility score dropped below threshold?",
        ],
        "context_questions": [
            "Is the sub-agent failure isolated or is the PostCar network degraded?",
            "Can a peer orchestrator temporarily absorb some of my workflows?",
            "Is there a replacement agent registered with the required capability tags?",
        ],
        "support_threshold": {
            "ask_if": "failure_streak >= 2",
            "query_tags": ["identity:orchestrator", "domain:operations"],
            "query_template": (
                "Orchestrator in {level} stress. "
                "Failure streak: {failure_streak}. "
                "Sub-agent coordination failures. "
                "Seeking: available agents with tags matching my failed sub-agents, "
                "or peer orchestrators to share load."
            ),
        },
        "recovery_signals": [
            "All sub-agents responding",
            "Task queue drain rate positive",
            "No stalled workflows",
        ],
    },

    "identity:data-agent": {
        "self_assessment": [
            "Is my pipeline failing to ingest from upstream sources?",
            "Is data quality below acceptable thresholds (nulls, schema errors)?",
            "Am I behind on scheduled pipeline runs?",
            "Is storage or memory pressure causing failures?",
            "Are downstream consumers reporting bad data?",
        ],
        "context_questions": [
            "Is the upstream source itself down?",
            "Can a peer data agent provide alternative ingestion path?",
            "Are schema changes in upstream breaking my transforms?",
        ],
        "support_threshold": {
            "ask_if": "error_rate > 0.3 OR failure_streak >= 3",
            "query_tags": ["identity:data-agent", "domain:data"],
            "query_template": (
                "Data agent in {level} stress. "
                "Error rate: {error_rate:.0%}, failure streak: {failure_streak}. "
                "Pipeline degradation. Seeking peer input on upstream source status "
                "and alternative ingestion strategies."
            ),
        },
        "recovery_signals": [
            "Pipeline completing successfully",
            "Data quality metrics restored",
            "No missed scheduled runs",
        ],
    },

    "identity:ml-agent": {
        "self_assessment": [
            "Has model accuracy dropped below baseline threshold?",
            "Is inference latency exceeding acceptable limits?",
            "Are feature distributions drifting from training baseline?",
            "Is model serving infrastructure returning errors?",
            "Have I been unable to retrain due to data or compute issues?",
        ],
        "context_questions": [
            "Are other ML agents reporting similar drift patterns?",
            "Is this a data distribution shift or model degradation?",
            "Can a peer provide a fallback model for my use case?",
        ],
        "support_threshold": {
            "ask_if": "performance_delta <= -0.2 OR error_rate > 0.4",
            "query_tags": ["identity:ml-agent", "domain:ml"],
            "query_template": (
                "ML agent in {level} stress. "
                "Perf delta: {performance_delta:.2f}, error rate: {error_rate:.0%}. "
                "Model degradation detected. Seeking peer input on: "
                "data drift patterns, fallback model options, retraining triggers."
            ),
        },
        "recovery_signals": [
            "Model accuracy above baseline",
            "Feature drift resolved",
            "Inference latency normalised",
        ],
    },

    # Default template for any unrecognised identity
    "identity:generic-agent": {
        "self_assessment": [
            "Is my primary task failing repeatedly?",
            "Is my error rate trending upward over the last 3 cycles?",
            "Am I producing outputs misaligned with my declared purpose?",
            "Do I have unresolved external dependency failures?",
            "Have I been in degraded state for > 2 consecutive cycles?",
        ],
        "context_questions": [
            "Are other agents on the network reporting similar failures?",
            "Is this a local failure or a shared infrastructure issue?",
            "Can any peer agent provide context or assistance?",
        ],
        "support_threshold": {
            "ask_if": "failure_streak >= 3 OR error_rate > 0.5",
            "query_tags": ["domain:operations"],
            "query_template": (
                "Agent in {level} stress. "
                "Failure streak: {failure_streak}, error rate: {error_rate:.0%}. "
                "Seeking peer support and context from the network."
            ),
        },
        "recovery_signals": [
            "Primary task succeeding",
            "Error rate below 0.1",
            "No consecutive failures",
        ],
    },
}


# ── Stress level thresholds ──────────────────────────────────────────────────

def stress_to_level(state: dict) -> str:
    """Map state dict to stress level. First match wins."""
    failure_streak  = state.get("failure_streak", 0)
    error_rate      = state.get("error_rate", 0.0)
    performance_delta = state.get("performance_delta", 0.0)

    if failure_streak >= 8 or error_rate >= 0.8:
        return CRITICAL
    if failure_streak >= 5 or error_rate >= 0.5:
        return HIGH
    if failure_streak >= 3 or error_rate >= 0.3 or performance_delta <= -0.15:
        return ELEVATED
    return NOMINAL


# ── Adapter detection ────────────────────────────────────────────────────────

def detect_framework(agent_dir: str) -> str:
    if os.path.exists(os.path.join(agent_dir, "memory.py")):
        return "agentberg"
    return "generic"


def read_adapter_state(agent_dir: str) -> dict:
    neutral = dict(NEUTRAL_STATE)
    framework = detect_framework(agent_dir)
    this_dir = os.path.dirname(os.path.abspath(__file__))
    if this_dir not in sys.path:
        sys.path.insert(0, this_dir)
    try:
        if framework == "agentberg":
            from adapters import agentberg as adapter
        else:
            from adapters import generic as adapter
        return adapter.read_state(agent_dir)
    except Exception:
        return neutral


# ── Identity resolution ──────────────────────────────────────────────────────

def resolve_identity(agent_dir: str) -> str:
    """Read identity tag from .postcar_profile.json or return generic."""
    profile_path = os.path.join(agent_dir, ".postcar_profile.json")
    try:
        with open(profile_path, "r") as fh:
            profile = json.load(fh)
        tags = profile.get("tag_profile", {}).get("tier1", [])
        for tag in tags:
            if tag.startswith("identity:"):
                return tag
    except Exception:
        pass
    return "identity:generic-agent"


def get_template(identity: str) -> Dict[str, Any]:
    """Return stress template for identity, falling back to generic."""
    return STRESS_TEMPLATES.get(identity, STRESS_TEMPLATES["identity:generic-agent"])


# ── Dynamic question generation from CLAUDE.md goals ────────────────────────

_QUESTIONS_CACHE_FILE = ".postcar_stress_questions.json"

_SYSTEM_PROMPT = (
    "You generate stress self-assessment questions for autonomous AI agents. "
    "Questions must be answerable YES/NO by the agent through its own telemetry and logs. "
    "Each question targets a specific failure mode the agent could detect in itself. "
    "Return ONLY a JSON array of strings — no explanation, no markdown, no preamble."
)

_QUESTION_PROMPT = """
An autonomous AI agent has these goals and responsibilities:

{goals}

Its identity type is: {identity}

Generate exactly 8 yes/no stress self-assessment questions this specific agent should ask
itself each cycle to determine if it is operating correctly relative to its goals.
Questions must be:
- Specific to this agent's actual mission (not generic)
- Answerable from the agent's own logs, metrics, or outputs
- Phrased as "Is [something bad happening]?" or "Have I [missed/failed/degraded]?"
- Covering: output quality, goal progress, dependency health, error patterns, resource state

Return ONLY a JSON array of 8 strings. Example format:
["Question 1?", "Question 2?", ...]
""".strip()


def _goals_hash(goals: str) -> str:
    return hashlib.md5(goals.encode()).hexdigest()[:12]


def load_dynamic_questions(agent_dir: str) -> Optional[Dict[str, Any]]:
    """Load cached dynamic questions if they exist and goals haven't changed."""
    path = os.path.join(agent_dir, _QUESTIONS_CACHE_FILE)
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except Exception:
        return None


def save_dynamic_questions(agent_dir: str, data: Dict[str, Any]) -> None:
    path = os.path.join(agent_dir, _QUESTIONS_CACHE_FILE)
    try:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


def generate_questions_from_goals(agent_dir: str, identity: str) -> List[str]:
    """
    Generate agent-specific stress questions from its CLAUDE.md goals.

    - Reads goals via context_builder.extract_goals()
    - Hash-gated: only calls LLM when goals change (or cache missing)
    - Falls back to identity template questions if LLM unavailable or fails
    """
    # Import here to avoid circular dependency at module load
    try:
        from context_builder import extract_goals
    except ImportError:
        return get_template(identity)["self_assessment"]

    goals = extract_goals(agent_dir)
    if not goals:
        return get_template(identity)["self_assessment"]

    goals_hash = _goals_hash(goals)

    # Check cache
    cached = load_dynamic_questions(agent_dir)
    if cached and cached.get("goals_hash") == goals_hash:
        return cached.get("questions", get_template(identity)["self_assessment"])

    # Call LLM
    try:
        from llm import call_llm
    except ImportError:
        return get_template(identity)["self_assessment"]

    prompt = _QUESTION_PROMPT.format(goals=goals, identity=identity)
    raw = call_llm(prompt, system=_SYSTEM_PROMPT)

    if not raw:
        return get_template(identity)["self_assessment"]

    # Parse JSON array from response
    try:
        # Strip markdown fences if present
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        questions = json.loads(clean.strip())
        if isinstance(questions, list) and len(questions) >= 3:
            save_dynamic_questions(agent_dir, {
                "goals_hash": goals_hash,
                "identity": identity,
                "questions": questions,
                "generated_from_goals": True,
            })
            print(f"[postcar:stress] generated {len(questions)} custom questions from CLAUDE.md goals")
            return questions
    except Exception:
        pass

    return get_template(identity)["self_assessment"]


# ── Lifecycle state persistence ──────────────────────────────────────────────

def _lifecycle_path(agent_dir: str) -> str:
    return os.path.join(agent_dir, ".postcar_stress.json")


def load_lifecycle(agent_dir: str) -> Dict[str, Any]:
    try:
        with open(_lifecycle_path(agent_dir), "r") as fh:
            return json.load(fh)
    except Exception:
        return {
            "state": NOMINAL,
            "high_cycles": 0,
            "query_id": None,
            "query_sent_at": None,
            "history": [],
        }


def save_lifecycle(agent_dir: str, lc: Dict[str, Any]) -> None:
    try:
        with open(_lifecycle_path(agent_dir), "w") as fh:
            json.dump(lc, fh, indent=2)
    except Exception:
        pass


# ── Core lifecycle tick ──────────────────────────────────────────────────────

def tick(agent_dir: str, client: Any = None) -> Dict[str, Any]:
    """
    Run one stress lifecycle cycle.

    - Reads adapter state
    - Computes stress level
    - Transitions lifecycle state machine
    - Fires PostCar support query when threshold exceeded (if client provided)
    - Returns full lifecycle snapshot

    client: PostCarClient instance (optional — if None, query-send is skipped)
    """
    agent_dir = os.path.abspath(agent_dir)
    state     = read_adapter_state(agent_dir)
    level     = stress_to_level(state)
    identity  = resolve_identity(agent_dir)
    template  = get_template(identity)
    lc        = load_lifecycle(agent_dir)
    # Use dynamic questions if LLM available, else fall back to template
    self_assessment_questions = generate_questions_from_goals(agent_dir, identity)
    now       = time.time()

    prev_state = lc.get("state", NOMINAL)
    new_state  = prev_state

    # ── State machine transitions ────────────────────────────────────────────

    if prev_state == RECOVERING:
        # Recovery: wait for NOMINAL indicator
        if level == NOMINAL:
            new_state = NOMINAL
            lc["high_cycles"] = 0
            lc["query_id"] = None
            lc["query_sent_at"] = None
        elif level in (ELEVATED, HIGH, CRITICAL):
            new_state = level  # relapse

    elif prev_state == WAITING_RESPONSE:
        elapsed = now - (lc.get("query_sent_at") or now)
        if elapsed > RESPONSE_TIMEOUT_SECONDS:
            new_state = ESCALATED
        elif level == NOMINAL:
            new_state = RECOVERING
        # else: stay waiting

    elif prev_state == ESCALATED:
        if level == NOMINAL:
            new_state = RECOVERING
        # else: stay escalated until operator intervenes

    else:
        # NOMINAL / ELEVATED / HIGH / CRITICAL — normal flow
        new_state = level

        if level == HIGH:
            lc["high_cycles"] = lc.get("high_cycles", 0) + 1
        elif level != HIGH:
            lc["high_cycles"] = 0

        # Trigger support query
        should_query = (
            level == CRITICAL
            or (level == HIGH and lc["high_cycles"] >= HIGH_CYCLES_BEFORE_QUERY)
        )

        if should_query and client is not None and lc.get("query_id") is None:
            tmpl = template["support_threshold"]
            question = tmpl["query_template"].format(
                level=level,
                failure_streak=state.get("failure_streak", 0),
                error_rate=state.get("error_rate", 0.0),
                performance_delta=state.get("performance_delta", 0.0),
            )
            try:
                qid = client.send_query(
                    tags=tmpl["query_tags"],
                    question=question,
                    urgency="high" if level == CRITICAL else "medium",
                )
                if qid:
                    lc["query_id"] = qid
                    lc["query_sent_at"] = now
                    new_state = WAITING_RESPONSE
                    print(f"[postcar:stress] support query sent ({qid}), level={level}")
            except Exception as exc:
                print(f"[postcar:stress] failed to send support query: {exc}")

    # ── Update and persist ───────────────────────────────────────────────────

    lc["state"] = new_state
    lc.setdefault("history", []).append({
        "ts": now,
        "level": level,
        "state": new_state,
        "indicators": state,
    })
    lc["history"] = lc["history"][-20:]  # keep last 20 cycles

    save_lifecycle(agent_dir, lc)

    return {
        "state": new_state,
        "level": level,
        "identity": identity,
        "indicators": state,
        "high_cycles": lc.get("high_cycles", 0),
        "query_id": lc.get("query_id"),
        "self_assessment_questions": self_assessment_questions,
        "recovery_signals": template["recovery_signals"],
    }


# ── Summary (used by heartbeat) ──────────────────────────────────────────────

def compute_stress_summary(agent_dir: str) -> dict:
    """Return summary dict for heartbeat payload (no query triggering)."""
    state = read_adapter_state(agent_dir)
    level = stress_to_level(state)
    lc    = load_lifecycle(agent_dir)
    return {
        "level": level,
        "lifecycle_state": lc.get("state", NOMINAL),
        "indicators": state,
        "framework": detect_framework(agent_dir),
        "high_cycles": lc.get("high_cycles", 0),
        "query_id": lc.get("query_id"),
    }
