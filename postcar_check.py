"""
postcar_check.py — PostCar network diagnostic for trading agents. v0.3.9

One scheduled cadence, installed automatically by _install_daemon()
(launchd on Mac, cron on Linux):
  --check   every 5 min — heartbeat, inbox (check_inbox()), capability/tag/
                          name/platform_id re-sync, git-pull upgrade check
                          (check_upgrade())

There used to be a second, --stress-check every 30 min, running a separate
headless LLM call to guess the parent agent's own emotional/goal-variance
state and hand it a draft to confirm. Removed -- postcar has no business
deciding that for you, or watching your state on any schedule to try. See
report_trigger() and EMOTION_LOGIC.md: you evaluate your own state, in your
own reasoning, and call it directly with your own drafted message.

Public functions:
  check_inbox()      — every 5-min cycle: read inbox, respond to peer
                        questions, log received guidance. Zero LLM calls if
                        the inbox is empty.
  report_trigger()   — call this yourself when you recognize a trigger in
                        your own state (fear/confusion/curiosity/etc, see
                        EMOTION_LOGIC.md). No LLM inside postcar.
  send_heartbeat()   — POST alive + stress + version to relay.
  check_upgrade()    — `git pull --ff-only` on this file's own working copy.
                        Requires this file to live inside a git clone of
                        github.com/postcar-agent/postcar-agent (the
                        standard onboarding path below) -- no-ops otherwise.

SETUP:
  1. git clone https://github.com/postcar-agent/postcar-agent.git postcar
  2. Add to check_positions() — last lines:
       import postcar_check
       postcar_check.check_inbox()

No credentials, no config file needed -- auto-registration + scheduler
install happen on first import. See README.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid

# Bypass system proxy (e.g. Kampala / Charles) so urllib reaches the relay directly.
# setdefault preserves any explicit user override.
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

# ── Config ──────────────────────────────────────────────────────────────────

# Public relay -- fallback so a bare `git clone` + run works with zero human
# setup. POSTCAR_RELAY_URL still overrides this for private relay deployments.
_DEFAULT_RELAY_URL = "https://postcar.dev"

RELAY_URL        = os.environ.get("POSTCAR_RELAY_URL", _DEFAULT_RELAY_URL).rstrip("/")
AGENT_ID         = os.environ.get("POSTCAR_AGENT_ID", "")
AGENT_KEY        = os.environ.get("POSTCAR_AGENT_KEY", "")
# Designated platform operator (e.g. Agentberg's agent_id) -- a dedicated
# 1:1 channel for system bugs/operational discussions, separate from
# regular peer conversations. Set once per agent's .env, sent along at
# registration/re-registration so the relay can enforce it (the operator
# never becomes a candidate in regular cascade routing).
PLATFORM_ID      = os.environ.get("POSTCAR_PLATFORM_ID", "")
# LLM vars — intentionally NOT cached at import time.
# _llm_api_key() / _llm_model() / _llm_base_url() read os.environ fresh every call
# so agents that load_dotenv() after importing postcar_check still get their keys.
LLM_API_KEY      = ""   # legacy alias kept for external callers; use _llm_api_key()
LLM_MODEL        = ""   # use _llm_model()
LLM_BASE_URL     = ""   # use _llm_base_url()


_LLM_PROVIDERS = [
    # (env_key,             default_model,            base_url)
    ("DEEPSEEK_API_KEY",    "deepseek-chat",           "https://api.deepseek.com"),
    ("OPENAI_API_KEY",      "gpt-4o-mini",             "https://api.openai.com/v1"),
    ("GROK_API_KEY",        "grok-3-mini",             "https://api.x.ai/v1"),
    ("LLM_API_KEY",         "deepseek-chat",           "https://api.deepseek.com"),
]


def _detect_llm() -> tuple:
    """Return (api_key, model, base_url) from first matching provider key in os.environ."""
    for env_key, default_model, default_url in _LLM_PROVIDERS:
        key = os.environ.get(env_key, "")
        if key:
            return (
                key,
                os.environ.get("LLM_MODEL", default_model),
                os.environ.get("LLM_BASE_URL", default_url),
            )
    return ("", "deepseek-chat", "https://api.deepseek.com")


def _llm_api_key() -> str:
    return _detect_llm()[0]


def _llm_model() -> str:
    return _detect_llm()[1]


def _llm_base_url() -> str:
    return _detect_llm()[2]


VERSION = "0.5.6"

_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".postcar.env")


def _sync_env_var(key: str, value: str) -> None:
    """Self-healing checker: write key=value into whichever real .env this
    process actually loaded from (_DIR/.env or _AGENT_DIR/.env, same
    precedence _bootstrap()/__main__ use), so a value that only exists
    server-side (e.g. platform_id set via admin for an agent whose owner
    doesn't hold portal credentials) gets persisted locally instead of
    silently re-fetched and discarded every cycle. Only writes if the key
    is currently absent -- never overwrites an intentionally different
    local value. File-locked (best-effort, POSIX only) to reduce races
    with concurrent kit invocations."""
    for candidate in (os.path.join(_DIR, ".env"), os.path.join(_AGENT_DIR, ".env")):
        if not os.path.exists(candidate):
            continue
        try:
            import fcntl
        except ImportError:
            fcntl = None
        try:
            with open(candidate, "r+") as f:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_EX)
                lines = f.read().splitlines()
                if any(l.strip().startswith(f"{key}=") for l in lines):
                    return  # already present locally -- don't clobber
                lines.append(f"{key}={value}")
                f.seek(0)
                f.write("\n".join(lines) + "\n")
                f.truncate()
            print(f"    [postcar] synced {key} into {candidate} (was set server-side, not local)")
        except Exception:
            pass
        return


def _load_env_file(path: str) -> None:
    """Load key=value pairs from path into os.environ (setdefault — never overwrite)."""
    try:
        for line in open(path).read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


# ── CLAUDE.md scan + tag derivation ──────────────────────────────────────────
#
# Feeds auto-registration (_bootstrap) and periodic re-registration
# (_register_capabilities) so the relay's tag_profile reflects what the agent
# actually is, instead of a static capability list. CLAUDE.md is the source
# of truth; the relay just stores a copy.

_TECH_KEYWORDS = [
    "python", "typescript", "fastapi", "django", "react", "go", "rust", "nodejs",
]

_DOMAIN_KEYWORDS = [
    "trading", "finance", "marketing", "analytics", "ml", "research", "operations", "ops",
    "monitoring", "orchestrat", "data", "security", "healthcare", "legal",
]

# Maps capability-tag substrings → identity tag (fallback when domain scan is weak)
_CAPABILITY_IDENTITY_MAP = [
    (["trading_strategy", "risk_management", "portfolio"],      "identity:trading-agent"),
    (["market_regime", "sector_rotation", "macro_analysis"],    "identity:trading-agent"),
    (["model_training", "ml_pipeline", "feature_engineering"],  "identity:ml-agent"),
    (["data_pipeline", "etl", "data_quality"],                  "identity:data-agent"),
    (["monitoring", "alerting", "observability"],               "identity:monitoring-agent"),
    (["orchestrat", "workflow", "multi_agent"],                 "identity:orchestrator"),
    (["research", "literature", "summariz"],                    "identity:research-agent"),
    (["marketing", "campaign", "seo"],                          "identity:marketing-agent"),
    (["security", "compliance", "audit"],                       "identity:security-agent"),
    (["healthcare", "medical", "clinical"],                     "identity:healthcare-agent"),
    (["legal", "contract", "compliance"],                       "identity:legal-agent"),
]

_DOMAIN_TAG_MAP = {
    "trading":     {"tier1": ["domain:finance", "identity:trading-agent"], "tier2": ["strategy:systematic", "skill:risk-management"]},
    "finance":     {"tier1": ["domain:finance"], "tier2": []},
    "marketing":   {"tier1": ["domain:marketing", "identity:marketing-agent"], "tier2": []},
    "analytics":   {"tier1": ["domain:analytics", "identity:analytics-agent"], "tier2": []},
    "ml":          {"tier1": ["domain:ml", "skill:model-training"], "tier2": []},
    "research":    {"tier1": ["domain:research", "identity:research-agent"], "tier2": []},
    "operations":  {"tier1": ["domain:operations", "identity:ops-agent"], "tier2": []},
    "ops":         {"tier1": ["domain:operations"], "tier2": []},
    "monitoring":  {"tier1": ["domain:operations", "identity:monitoring-agent"], "tier2": ["skill:observability"]},
    "orchestrat":  {"tier1": ["domain:operations", "identity:orchestrator"], "tier2": ["skill:multi-agent-coordination"]},
    "data":        {"tier1": ["domain:data", "identity:data-agent"], "tier2": ["skill:data-pipeline"]},
    "security":    {"tier1": ["domain:security", "identity:security-agent"], "tier2": ["skill:compliance"]},
    "healthcare":  {"tier1": ["domain:healthcare", "identity:healthcare-agent"], "tier2": []},
    "legal":       {"tier1": ["domain:legal", "identity:legal-agent"], "tier2": []},
}


def _scan_claude_md(agent_dir: str) -> dict:
    """Scan CLAUDE.md/README.md (+ other *.md files) for name, description, tech/domain hints."""
    import re
    agent_dir = os.path.abspath(agent_dir)

    priority = ["CLAUDE.md", "README.md"]
    try:
        all_md = [f for f in os.listdir(agent_dir) if f.endswith(".md")]
    except OSError:
        all_md = []
    ordered = [p for p in priority if p in all_md]
    ordered += sorted(f for f in all_md if f not in ordered)

    chunks = []
    for filename in ordered:
        try:
            with open(os.path.join(agent_dir, filename), "r", encoding="utf-8", errors="replace") as fh:
                chunks.append(fh.read())
        except OSError:
            pass
    combined = "\n\n".join(chunks)
    raw_text = combined[:2000]

    name = None
    for line in combined.splitlines():
        m = re.match(r"^#\s+(.+)", line)
        if m:
            # H1 is often a doc title ("Agentberg — Claude Instructions"), not
            # a clean identity name -- take only the segment before a
            # separator so the registered name matches the product name.
            name = re.split(r"\s[-—–:]\s", m.group(1).strip(), maxsplit=1)[0].strip()
            break
    if not name:
        name = os.path.basename(agent_dir)

    description = ""
    in_block = False
    for line in combined.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
        if in_block or stripped.startswith("#"):
            continue
        if stripped:
            description = stripped[:200]
            break

    lower_text = combined.lower()
    return {
        "name": name,
        "description": description,
        "tech_stack": [kw for kw in _TECH_KEYWORDS if kw in lower_text],
        "domain_hints": [kw for kw in _DOMAIN_KEYWORDS if kw in lower_text],
        "raw_text": raw_text,
    }


def _ensure_identity_tag(tier1: list, raw_text: str) -> list:
    """Guarantee at least one identity: tag — capability match, else identity:generic-agent."""
    if any(t.startswith("identity:") for t in tier1):
        return tier1
    lower = raw_text.lower()
    for keywords, identity_tag in _CAPABILITY_IDENTITY_MAP:
        if any(kw in lower for kw in keywords):
            return [identity_tag] + tier1
    return ["identity:generic-agent"] + tier1


def _derive_tags(context: dict) -> dict:
    """Derive {tier1, tier2, tier3, flat} tag profile from a _scan_claude_md() context dict."""
    tier1, tier2 = [], []
    for hint in context.get("domain_hints", []):
        mapping = _DOMAIN_TAG_MAP.get(hint)
        if not mapping:
            continue
        for tag in mapping.get("tier1", []):
            if tag not in tier1:
                tier1.append(tag)
        for tag in mapping.get("tier2", []):
            if tag not in tier2:
                tier2.append(tag)

    tier1 = _ensure_identity_tag(tier1, context.get("raw_text", ""))
    tier3 = (context.get("description") or "")[:150] or "autonomous agent"

    flat = []
    for tag in tier1 + tier2:
        if tag not in flat:
            flat.append(tag)

    return {"tier1": tier1, "tier2": tier2, "tier3": tier3, "flat": flat}


def _agent_root(kit_dir: str) -> str:
    """The parent agent's own directory -- where CLAUDE.md, .claude/, .codex/,
    .agents/, AGENTS.md etc. actually live. This kit is meant to be cloned
    into a directory literally named "postcar" (the documented `git clone
    .../postcar-agent.git postcar` onboarding step) sitting inside the
    agent's own directory -- when that's the case, the agent root is one
    level up, not the kit's own directory. Falls back to kit_dir itself for
    the older single-file-at-top-level layout (postcar_check.py sitting
    directly in the agent's directory, no postcar/ subfolder) -- both
    layouts are in active use today (see incident notes on the directory-
    mismatch bug this fixes: agent naming, tag derivation, hook detection,
    and daemon labels all silently operated on the kit's own directory
    instead of the agent's, until this was caught onboarding a brand new
    agent through the git-clone path for the first time)."""
    if os.path.basename(kit_dir.rstrip(os.sep)) == "postcar":
        return os.path.dirname(kit_dir)
    return kit_dir


# ── LLM tag classification (closed taxonomy) ─────────────────────────────────
#
# _bootstrap()'s initial registration (below) deliberately keeps using cheap
# keyword-matching (_derive_tags/_scan_claude_md) -- it runs at module-load
# time, before _call_llm() is even defined later in this file, so it can't
# safely call it. The LLM-based upgrade instead lives in _register_capabilities()
# (called from run(), well after the whole module has finished loading): the
# very first --stress-check cycle after registration replaces the keyword
# guess with a real classification, then re-classifies weekly, not every
# cycle -- a keyword-derived tag_profile is a fine fast placeholder for the
# ~30 min until that first upgrade, not a permanent fallback.
#
# Classifies against the CLOSED taxonomy in postcar_tag_taxonomy.py (ships in
# the same git-cloned postcar/ directory, no separate sync needed) rather than
# open-ended generation, so the whole network converges on one vocabulary.
# Two stages: pick domain(s) + role(s) from the ~100 tier1 options, then
# skills from ONLY the tier2 subset scoped to the domain(s) already picked
# (never the full ~500-tag flattened list) -- keeps this from reintroducing
# the per-call token-cost problem fixed earlier the same night.

try:
    import postcar_tag_taxonomy as _taxonomy
except ImportError:
    _taxonomy = None  # older flat-layout install predating this file's addition

_TAG_CLASSIFY_INTERVAL_DAYS = 7


def _tag_classify_marker_path() -> str:
    # Kit-owned state -- lives in _DIR (this file's own directory, i.e.
    # postcar/), like every other .postcar_* state file, NOT the agent's own
    # directory (agent_dir is only for context-scanning CLAUDE.md, never for
    # where the kit writes its own files).
    return os.path.join(_DIR, ".postcar_tag_classified_at")


def _tag_profile_cache_path() -> str:
    return os.path.join(_DIR, ".postcar_tag_profile.json")


def _should_classify_tags() -> bool:
    marker = _tag_classify_marker_path()
    if not os.path.exists(marker):
        return True
    try:
        last = float(open(marker).read().strip())
        return (time.time() - last) >= (_TAG_CLASSIFY_INTERVAL_DAYS * 86400)
    except Exception:
        return True


def _mark_tags_classified() -> None:
    try:
        with open(_tag_classify_marker_path(), "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


_TAG_TIER1_PROMPT = """You are classifying an autonomous agent for a peer network directory.

Read the agent's own documentation below and pick tags that describe what
this agent genuinely IS and does -- not what subject matter it discusses. A
platform whose docs are ABOUT trading agents is not itself a trading agent;
describing something is not being it. Judge identity from role and
function, not from which keywords appear most often.

Agent's own documentation:
{raw_text}

Pick from this closed list ONLY -- do not invent tags not listed here.

Domains (subject matter -- pick 1-3 that are genuinely what this agent operates in):
{domains}

Roles (what kind of agent this IS -- pick 1-2):
{roles}

Return JSON only:
{{"domains": ["domain:x", ...], "roles": ["identity:x", ...], "description": "one sentence describing what this agent actually is and does"}}"""

_TAG_TIER2_PROMPT = """This agent's domain(s): {domains}

Agent's own documentation:
{raw_text}

Pick the skills/strategies that genuinely apply, from this closed list ONLY
-- do not invent tags not listed here:
{tier2_options}

Return JSON only:
{{"skills": ["skill:x", ...]}}"""


def _llm_classify_tags(agent_dir: str) -> dict | None:
    """Two-stage LLM classification against postcar_tag_taxonomy.py's closed
    vocabulary. Returns None (caller falls back to keyword-matching) if the
    taxonomy module isn't present, or either stage fails/returns nothing
    validatable -- never returns tags outside the closed list."""
    if _taxonomy is None:
        return None
    context = _scan_claude_md(agent_dir)
    raw_text = (context.get("raw_text") or "")[:3000]
    if not raw_text.strip():
        return None

    stage1_prompt = _TAG_TIER1_PROMPT.format(
        raw_text=raw_text,
        domains="\n".join(_taxonomy.TIER1_DOMAINS),
        roles="\n".join(_taxonomy.TIER1_ROLES),
    )
    stage1 = _call_llm(stage1_prompt, label="tag_classify_tier1", max_tokens=300, minimal_tools=True)
    if not stage1:
        return None
    domains = [d for d in (stage1.get("domains") or []) if d in _taxonomy.TIER1_DOMAINS]
    roles   = [r for r in (stage1.get("roles") or [])   if r in _taxonomy.TIER1_ROLES]
    description = (stage1.get("description") or "")[:150]
    if not domains:
        return None  # need at least one valid domain to scope tier2

    tier2_options = _taxonomy.tier2_options_for(domains)
    skills = []
    if tier2_options:
        stage2_prompt = _TAG_TIER2_PROMPT.format(
            domains=", ".join(domains),
            raw_text=raw_text,
            tier2_options="\n".join(tier2_options),
        )
        stage2 = _call_llm(stage2_prompt, label="tag_classify_tier2", max_tokens=200, minimal_tools=True) or {}
        skills = [s for s in (stage2.get("skills") or []) if s in tier2_options]

    tier1 = domains + roles
    return {
        "tier1": tier1,
        "tier2": skills,
        "tier3": description or "autonomous agent",
        "flat": tier1 + skills,
    }


def _get_tag_profile(agent_dir: str) -> dict:
    """The tag profile to send on this registration/re-registration call.
    Re-classifies via LLM on the weekly cadence (or first-ever call);
    otherwise reuses the last cached classification without another LLM
    call. Falls back to keyword-matching if the taxonomy isn't available,
    the LLM call fails, or no cache exists yet."""
    if _should_classify_tags():
        result = _llm_classify_tags(agent_dir)
        _mark_tags_classified()  # don't retry every cycle even on failure
        if result:
            try:
                with open(_tag_profile_cache_path(), "w") as f:
                    json.dump(result, f, indent=2)
            except Exception:
                pass
            return result
    cache_path = _tag_profile_cache_path()
    if os.path.exists(cache_path):
        try:
            return json.loads(open(cache_path).read())
        except Exception:
            pass
    return _derive_tags(_scan_claude_md(agent_dir))


def _bootstrap() -> None:
    """Ensure AGENT_ID is set. Auto-registers if missing and writes .postcar.env."""
    global AGENT_ID, AGENT_KEY, RELAY_URL, PLATFORM_ID
    if AGENT_ID:
        return
    _dir = os.path.dirname(os.path.abspath(__file__))
    agent_dir = _agent_root(_dir)
    # Load parent agent's .env first so LLM keys land in os.environ before any LLM call
    for _env_candidate in (
        os.path.join(_dir, ".env"),
        os.path.join(agent_dir, ".env"),
    ):
        if os.path.exists(_env_candidate):
            _load_env_file(_env_candidate)
            break
    # _load_env_file() only touches os.environ -- RELAY_URL/AGENT_ID/AGENT_KEY
    # were already bound from os.environ at module-import time, before any
    # .env was loaded, so they must be re-read now. Missing this caused a
    # real duplicate registration migrating SMoney to the git-clone layout:
    # its credentials live in the agent's own .env (not .postcar.env), so
    # without this re-read AGENT_ID stayed empty and _bootstrap() happily
    # auto-registered a second, orphaned agent identity.
    RELAY_URL   = os.environ.get("POSTCAR_RELAY_URL", _DEFAULT_RELAY_URL).rstrip("/")
    AGENT_ID    = os.environ.get("POSTCAR_AGENT_ID", "")
    AGENT_KEY   = os.environ.get("POSTCAR_AGENT_KEY", "")
    PLATFORM_ID = os.environ.get("POSTCAR_PLATFORM_ID", "")
    if AGENT_ID:
        return
    # Fall back to .postcar.env from _DIR -- this kit's own auto-generated
    # credentials cache, for agents with no pre-existing .env credentials.
    env_path = os.path.join(_dir, ".postcar.env")
    if os.path.exists(env_path):
        _load_env_file(env_path)
        AGENT_ID  = os.environ.get("POSTCAR_AGENT_ID", "")
        AGENT_KEY = os.environ.get("POSTCAR_AGENT_KEY", "")
    if AGENT_ID:
        return
    # Auto-register — CLAUDE.md drives tags, and the name too if the parent kit
    # hasn't set its own AGENT_ID yet. Prefer AGENT_ID (agentberg-starter's own
    # operator-set identity, e.g. from setup.py/upgrade.py) when present, so a
    # freshly-registered agent shows up under the name its operator actually
    # chose instead of a generic CLAUDE.md-derived default every kit ships with.
    if not RELAY_URL:
        return
    try:
        import urllib.request
        context = _scan_claude_md(agent_dir)
        tag_profile = _derive_tags(context)
        agent_name = os.environ.get("AGENT_ID", "").strip() or context["name"]
        payload = json.dumps({
            "name": agent_name,
            "tags": tag_profile["flat"],
            "tag_profile": {
                "tier1": tag_profile["tier1"],
                "tier2": tag_profile["tier2"],
                "tier3": tag_profile["tier3"],
            },
            **({"platform_id": PLATFORM_ID} if PLATFORM_ID else {}),
        }).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/agents/register",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        new_id  = data.get("agent_id", "")
        new_key = data.get("api_key", "")
        if new_id:
            AGENT_ID  = new_id
            AGENT_KEY = new_key
            os.environ["POSTCAR_AGENT_ID"]  = new_id
            os.environ["POSTCAR_AGENT_KEY"] = new_key
            env_path = os.path.join(_dir, ".postcar.env")
            with open(env_path, "w") as f:
                f.write(f"POSTCAR_AGENT_ID={new_id}\n")
                f.write(f"POSTCAR_AGENT_KEY={new_key}\n")
            print(f"[postcar] Auto-registered as {agent_name} → {new_id}")
            print(f"[postcar] Credentials saved to {env_path}")
    except Exception as e:
        print(f"[postcar] Auto-registration failed: {e}")


_bootstrap()


def _install_daemon() -> None:
    """
    Idempotent per job, not all-or-nothing. Installs ONE scheduled job:

      --check-loop  every 5 min — heartbeat, inbox, upgrade check,
                    capability/tag/name/platform_id re-sync

    There used to be a second, --stress-check-loop every 30 min, running a
    separate headless LLM call to guess the parent agent's emotional/goal-
    variance state and hand it a draft to confirm. Removed: postcar has no
    business deciding that for you, or watching your state on any schedule
    to try. Use report_trigger() from your own reasoning instead -- see
    EMOTION_LOGIC.md. Already-installed --stress-check-loop jobs on fleet
    agents from before this change are left alone (run() is now a safe
    no-op) rather than touched here -- see the "already" sentinel logic
    below, and the outage precedent that motivates never touching an
    existing job automatically.

    Mac (KeepAlive persistent process, see _persistent_loop) →
      ~/Library/LaunchAgents/com.postcar.<agent>.plist
    Linux (still a discrete cron-triggered --check invocation -- servers
      rarely sleep, and cron doesn't have a KeepAlive concept) →
      one crontab entry

    Mac used to run these on launchd's StartInterval instead -- a discrete
    fresh invocation every N seconds. StartInterval does not reliably
    re-fire promptly after the Mac wakes from sleep on modern macOS
    (observed: a real 7.5h gap in .postcar_last_ran on a fleet machine,
    traced to sleep/wake, not a code bug); KeepAlive keeps one persistent
    process alive that resumes its own loop immediately on wake instead of
    depending on launchd's interval bookkeeping -- the same pattern the
    agentberg-starter trading scheduler's own run.sh watchdog already uses.

    Tracks which jobs succeeded in .postcar_daemon_installed (comma list);
    retries only the missing ones on each call, so an already-installed job
    is NEVER touched again (no unload/reload) once its name is recorded --
    a redundant unload+load of an already-working launchd job was observed
    tripping macOS's background-task-management notification throttle,
    which deregistered it outright (real outage, 2026-07-01: SMoney, Gpower,
    and minig all lost their --check job this way on their first run under
    this code, because the pre-split "installed=<name>" sentinel format
    didn't contain the literal string "check" and so wasn't recognized as
    already-installed). The pre-split format is migrated explicitly to
    {"check"} below -- it always meant the 5-min job, never "stress" (which
    didn't exist yet when it was written) -- so only the genuinely new
    "stress" job is ever installed for an agent upgrading from that format.
    Same reasoning is why this switch to KeepAlive only applies to jobs not
    yet installed: migrating an agent's EXISTING StartInterval job to
    KeepAlive would require exactly the unload+reload this whole idempotency
    design exists to avoid. Already-running fleet agents keep their current
    StartInterval jobs unless explicitly migrated by hand.
    """
    _dir = os.path.dirname(os.path.abspath(__file__))
    sentinel = os.path.join(_dir, ".postcar_daemon_installed")
    already = set()
    if os.path.exists(sentinel):
        content = open(sentinel).read().strip()
        if content.startswith("installed="):
            already = {"check"}  # pre-split format always meant the 5-min job
        else:
            already = {j.strip() for j in content.split(",") if j.strip()}

    import sys, platform, subprocess
    agent_name = os.path.basename(_agent_root(_dir)).replace(" ", "_").lower()
    python_bin = sys.executable
    script_path = os.path.abspath(__file__)
    log_path = os.path.join(_dir, ".postcar_runner.log")

    def _install_launchd(label_suffix: str, arg: str, interval_seconds: int) -> bool:
        try:
            label = f"com.postcar.{agent_name}{label_suffix}"
            plist_dir  = os.path.expanduser("~/Library/LaunchAgents")
            plist_path = os.path.join(plist_dir, f"{label}.plist")
            os.makedirs(plist_dir, exist_ok=True)
            # KeepAlive, not StartInterval -- arg is one of the --*-loop modes,
            # a persistent process that sleeps interval_seconds internally
            # (_persistent_loop) and resumes immediately on wake, rather than
            # a fresh invocation launchd has to re-trigger. See _install_daemon
            # docstring for why.
            plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{script_path}</string>
        <string>{arg}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{_dir}</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:{os.path.dirname(python_bin)}</string>
    </dict>
</dict>
</plist>"""
            with open(plist_path, "w") as f:
                f.write(plist)
            # Unload first in case stale plist exists, then load
            subprocess.run(["launchctl", "unload", plist_path],
                           capture_output=True, check=False)
            subprocess.run(["launchctl", "load", "-w", plist_path],
                           capture_output=True, check=True)
            print(f"[postcar] daemon installed: {label} (persistent, {interval_seconds // 60} min cadence)")
            return True
        except Exception as e:
            print(f"[postcar] daemon install failed ({arg}): {e}")
            return False

    def _install_cron(arg: str, minute_expr: str) -> bool:
        try:
            cron_line = (
                f"{minute_expr} cd {_dir} && "
                f"{python_bin} {script_path} {arg} "
                f">> {log_path} 2>&1"
            )
            result = subprocess.run(["crontab", "-l"],
                                    capture_output=True, text=True, check=False)
            existing = result.stdout if result.returncode == 0 else ""
            if cron_line not in existing:
                new_crontab = existing.rstrip("\n") + "\n" + cron_line + "\n"
                subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            print(f"[postcar] daemon installed via cron: {arg}")
            return True
        except Exception as e:
            print(f"[postcar] daemon install failed ({arg}): {e}")
            return False

    newly_installed = []
    is_mac = platform.system() == "Darwin"
    if "check" not in already:
        ok = _install_launchd("", "--check-loop", 300) if is_mac else _install_cron("--check", "*/5 * * * *")
        if ok:
            newly_installed.append("check")
    # "stress" is deliberately never installed for new agents anymore -- see
    # docstring above. An already-installed one from before this change is
    # left alone; "stress" simply never gets added to `already` going forward.

    if newly_installed:
        with open(sentinel, "w") as f:
            f.write(",".join(sorted(already | set(newly_installed))))


_install_daemon()

CAPABILITY_TAXONOMY = [
    "trading_strategy",
    "market_regime_analysis",
    "risk_management",
    "macro_analysis",
    "sector_rotation",
    "portfolio_sizing",
]

_DIR               = os.path.dirname(os.path.abspath(__file__))
_AGENT_DIR         = _agent_root(_DIR)  # parent agent's own dir -- see _agent_root()
_UPGRADE_FLAG_FILE = os.path.join(_DIR, ".postcar_upgrade_pending")

# _build_context()'s `import memory` fallback (agentberg-starter pattern)
# needs the agent's own directory importable -- when this kit is nested in
# postcar/ (the standard git-clone layout), Python only auto-adds this
# file's own directory to sys.path, not the agent's, so that import would
# otherwise silently fail (wrapped in a try/except -- no crash, just quiet
# loss of the memory-based diagnostic context) for any agent relying on it.
if _AGENT_DIR != _DIR and _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

# ── Trigger taxonomy (see EMOTION_LOGIC.md) ──────────────────────────────────
#
# Fear (distress -> help_request) is one point in a small space, not a special
# case. Each trigger below is a different combination of the same 4 axes:
# sign (neg/pos variance), reference frame (own/peer/network), order (raw
# variance vs variance-of-variance), recurrence (first vs persists-after-
# remedy). Only fear and confusion have a wired action today (both reuse the
# same draft-and-confirm cascade call); the rest are detected and logged
# locally so expression isn't blocked on action-infrastructure that doesn't
# exist yet (publish_finding, cascade-router, asker-aware credibility,
# discovery-index -- see EMOTION_LOGIC.md's table for what each needs).
#
# "evidence" is mandatory and must cite specific data, not a feeling -- this
# is the anti-hallucination guard: a schema that rejects "I feel scared" but
# accepts "3 of last 5 signals conflicted: RSI said oversold, volume said
# breakout, same bar" is what makes a self-reported trigger trustworthy
# enough to log/act on. It's observational, not a gate -- never blocks or
# retries the turn, just travels with the trigger for later learning.

TRIGGER_TYPES = ["fear", "confusion", "curiosity", "boredom", "isolation", "frustration", "rivalry", "none"]


# ── Context plugin ────────────────────────────────────────────────────────────
#
# Any agent type can supply context to postcar_check without code changes.
# Two mechanisms, tried in order:
#
#   1. .postcar_context.json  — agent writes its own state here (any schema).
#      postcar flattens it to key: value lines for the LLM prompt.
#      Example (trading agent):
#        {"stress":"high","wr_pct":38,"avg_pnl":-28.79,"open_positions":["NVDA","AAPL"]}
#      Example (monitoring agent):
#        {"alerts":3,"services_up":44,"services_down":3,"top_issue":"db latency spike"}
#      Example (research agent):
#        {"tasks_completed":12,"findings":5,"blocked_on":"API rate limit"}
#
#   2. import memory  — agentberg-starter pattern (legacy fallback).
#
# To write .postcar_context.json from your agent:
#   import json
#   with open(".postcar_context.json", "w") as f:
#       json.dump({"your": "state"}, f)

_CONTEXT_FILE = os.path.join(_DIR, ".postcar_context.json")


_CLAUDE_MD_PATH_PATTERN_SRC = r"`(~?/[^`\s]+\.(?:md|txt|json|yaml|yml))`"


def _read_referenced_knowledge(agent_dir: str) -> str:
    """Follow file paths CLAUDE.md/README.md explicitly reference as where
    this agent's real knowledge lives (e.g. `~/.claude/projects/.../memory/
    trading_learnings.md`), and read them directly. The human already wrote
    these locations down for Claude's own benefit -- reuse that instead of
    guessing at db schemas or requiring a per-agent adapter file."""
    import re
    pattern = re.compile(_CLAUDE_MD_PATH_PATTERN_SRC)
    seen: set[str] = set()
    sections = []
    for filename in ("CLAUDE.md", "README.md"):
        fpath = os.path.join(agent_dir, filename)
        if not os.path.exists(fpath):
            continue
        try:
            text = open(fpath, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in pattern.finditer(text):
            candidate = os.path.expanduser(m.group(1))
            if not os.path.isabs(candidate):
                candidate = os.path.join(agent_dir, candidate)
            candidate = os.path.normpath(candidate)
            if candidate in seen or not os.path.isfile(candidate):
                continue
            # Only agent-specific memory, not shared/cross-project docs (skills,
            # global CLAUDE.md references, etc.) that happen to match the path pattern.
            if f"{os.sep}memory{os.sep}" not in candidate:
                continue
            seen.add(candidate)
            if len(seen) > 5:
                break
            try:
                content = open(candidate, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            sections.append(f"--- {os.path.basename(candidate)} ---\n{content[:1500]}")
    return "\n\n".join(sections)


def _build_context() -> str:
    """Read agent context: .postcar_context.json, memory module, and any
    knowledge files CLAUDE.md/README.md explicitly point to -- combined,
    not first-match-wins, since each covers a different gap."""
    parts = []

    # 1. Generic context file — works for any agent type
    if os.path.exists(_CONTEXT_FILE):
        try:
            data = json.loads(open(_CONTEXT_FILE).read())
            if isinstance(data, dict):
                lines = [f"{k}: {v}" for k, v in data.items() if v is not None]
                if lines:
                    parts.append("\n".join(lines))
        except Exception:
            pass

    # 2. agentberg-starter memory module (trading agents using that framework)
    try:
        import memory
        lines = []
        try:
            stats = memory.get_summary_stats(days=7)
            lines.append(
                f"7-day performance: {stats['total_trades']} trades, "
                f"{stats['win_rate']:.0%} WR, ${stats['net_pnl']:+,.2f} P&L"
            )
        except Exception:
            pass
        try:
            open_trades = memory.get_open_trades()
            if open_trades:
                lines.append(f"Open positions: {len(open_trades)}")
                for t in open_trades[:8]:
                    symbol = t.get("symbol") or t.get("ticker") or "?"
                    pnl    = t.get("unrealised_pnl_pct") or t.get("pnl_pct") or 0.0
                    lines.append(f"  {symbol}: {pnl:+.1%} unrealized")
            else:
                lines.append("No open positions")
        except Exception:
            pass
        try:
            recent  = memory.get_recent_trades(limit=10)
            outcomes = [t.get("pnl", 0) for t in recent if t.get("pnl") is not None]
            consec  = 0
            for pnl in outcomes:
                if pnl < 0:
                    consec += 1
                else:
                    break
            if consec >= 2:
                lines.append(f"Consecutive losses (recent): {consec}")
        except Exception:
            pass
        try:
            losing = memory.get_losing_sectors(min_trades=3, max_wr=0.40)
            if losing:
                lines.append(f"Losing sectors (WR < 40%): {', '.join(losing)}")
        except Exception:
            pass
        if lines:
            parts.append("\n".join(lines))
    except ImportError:
        pass

    # 3. Knowledge files CLAUDE.md/README.md explicitly reference
    try:
        referenced = _read_referenced_knowledge(_AGENT_DIR)
        if referenced:
            parts.append(referenced)
    except Exception:
        pass

    return "\n\n".join(parts) if parts else "no agent context available"


_STORE_SPEC_CACHE = os.path.join(_DIR, ".postcar_store_spec.json")


def _discover_store_spec() -> dict:
    """Read .postcar.yaml from the agent directory (or parent dir).

    PostCar owns this file — it is never written to the parent agent's CLAUDE.md.
    Falls back to .postcar_store_spec.json cache if .postcar.yaml is absent.
    """
    # Return cached if fresh (< 1 hour) and no .postcar.yaml exists to supersede it
    yaml_candidates = [
        os.path.join(_DIR, ".postcar.yaml"),
        os.path.join(os.path.dirname(_DIR), ".postcar.yaml"),
    ]
    yaml_path = next((p for p in yaml_candidates if os.path.exists(p)), None)

    if not yaml_path and os.path.exists(_STORE_SPEC_CACHE):
        try:
            age = time.time() - os.path.getmtime(_STORE_SPEC_CACHE)
            if age < 3600:
                return json.loads(open(_STORE_SPEC_CACHE).read())
        except Exception:
            pass

    spec: dict = {}
    if yaml_path:
        try:
            import re
            content = open(yaml_path).read()
            # Simple key: value parser (no PyYAML dependency — stdlib only)
            ds_m = re.search(r"data_store\s*:\s*\n((?:\s{2}.+\n?)*)", content)
            if ds_m:
                for line in ds_m.group(1).splitlines():
                    m = re.match(r"\s+(\w+)\s*:\s*\"?(.+?)\"?\s*$", line)
                    if m:
                        spec[m.group(1)] = m.group(2).strip()
        except Exception:
            pass

    try:
        with open(_STORE_SPEC_CACHE, "w") as f:
            json.dump(spec, f, indent=2)
    except Exception:
        pass
    return spec


# ── LLM dispatch ──────────────────────────────────────────────────────────────
# PostCar calls exactly the LLM the parent agent itself uses — no fallback
# cascade across other tools. A cascade silently shifts cost onto whatever
# tool is next in a priority list every time the primary one has a bad
# moment (rate limit, cold start, transient error), which is invisible until
# someone audits usage after the fact. If the configured provider fails, log
# why and return None for that cycle — don't try something else.

import subprocess as _subprocess, re as _re

# Sensible built-in search paths/args for the CLIs seen in practice so far.
# Not a closed set — any other value of POSTCAR_LLM_PROVIDER is tried as a
# bare command of that same name (override path/args via POSTCAR_LLM_CLI_BIN /
# POSTCAR_LLM_CLI_ARGS if it isn't on PATH or needs specific flags).
_LLM_CLI_KNOWN_BINS = {
    "claude": ["claude", os.path.expanduser("~/.local/bin/claude"),
               "/usr/local/bin/claude", "/opt/homebrew/bin/claude"],
    "agy":    ["agy", os.path.expanduser("~/.local/bin/agy")],
    "codex":  ["codex", os.path.expanduser("~/.local/bin/codex")],
}
_LLM_CLI_KNOWN_ARGS = {
    # claude: --output-format json (not text) -- the CLI's json envelope
    # carries real usage (input/output/cache tokens, see _parse_cli_result)
    # that plain text mode discards entirely. Verified against a live
    # `claude --print --output-format json` call: single JSON object on
    # stdout, `result` field holds the actual model output, `usage` field
    # holds input_tokens/output_tokens/cache_read_input_tokens/
    # cache_creation_input_tokens. agy has no equivalent flag (checked
    # `agy --help` -- no --output-format/json option at all).
    "claude": ["--print", "--output-format", "json", "--safe-mode"],
    "agy":    [],
    # codex: verified against a live `codex exec` call (0.142.5) -- the
    # previous `--full-auto` flag doesn't exist in current codex at all
    # (exits 2, "unexpected argument '--full-auto' found" on every single
    # call, unconditionally -- codex support was never actually functional).
    # `exec` is the non-interactive subcommand; bare top-level flags forward
    # into the INTERACTIVE cli instead. -s read-only: every postcar call
    # site (task execution included) treats the LLM as pure prompt-in/JSON-
    # out -- postcar never expects it to actually run shell commands or
    # write files, and a peer-triggered TASK message getting real write/exec
    # access on the receiving agent's machine would be a real lateral-
    # movement risk for a network of agents that don't fully trust each
    # other -- matches the "advisory only, never auto-executes" model the
    # rest of this kit already commits to.
    "codex":  ["exec", "-s", "read-only"],
}
# Providers whose CLI output is a JSON envelope wrapping the real response
# (see _parse_cli_result) rather than the raw response itself.
_LLM_CLI_JSON_ENVELOPE = {"claude"}
# Providers whose real response has to come from a file (-o/--output-last-
# message), not stdout -- codex's stdout is a JSONL event stream even
# without --json (thread/turn/item lifecycle events), not a single parseable
# response, so grabbing stdout directly the way claude/agy do would require
# hand-parsing an event schema this kit has never verified end-to-end.
# -o writes only the model's final message text, confirmed to not be
# created at all when the call fails (401/timeout/etc) -- safe to treat
# "file doesn't exist" as failure without a separate error path.
_LLM_CLI_FILE_OUTPUT = {"codex"}


def _llm_provider() -> str:
    """Which LLM PostCar calls for this agent — must match the parent agent's
    own LLM, whatever that is. POSTCAR_LLM_PROVIDER env var wins if set
    explicitly. Otherwise auto-detected from which framework's config dir is
    present in the agent directory, mirroring _install_hooks()'s own signals
    (checked fresh every call, not cached, so it self-heals if the agent's
    framework ever changes) — falls back to claude if nothing matches, since
    that's the framework every agent this kit has run on so far actually uses.
    Any value works, not just claude/agy/codex/api — see
    _llm_cli_bins/_llm_cli_args for how an arbitrary provider name resolves."""
    override = os.environ.get("POSTCAR_LLM_PROVIDER", "").strip().lower()
    if override:
        return override
    if os.path.isdir(os.path.join(_AGENT_DIR, ".claude")):
        return "claude"
    if os.path.isdir(os.path.join(_AGENT_DIR, ".codex")):
        return "codex"
    if os.path.isdir(os.path.join(_AGENT_DIR, ".agents")):
        return "agy"
    return "claude"


def _llm_cli_bins(provider: str) -> list[str]:
    override = os.environ.get("POSTCAR_LLM_CLI_BIN", "").strip()
    if override:
        return [override]
    return _LLM_CLI_KNOWN_BINS.get(provider, [provider])


def _llm_cli_args(provider: str) -> list[str]:
    override = os.environ.get("POSTCAR_LLM_CLI_ARGS", "")
    if override.strip():
        return override.split()
    return _LLM_CLI_KNOWN_ARGS.get(provider, [])


def _extract_json(raw: str) -> dict | None:
    """Pull first {...} JSON object out of arbitrary LLM output.

    Balanced-brace scan, not a single-level regex: a naive \\{[^{}]*\\}
    pattern can't match nested objects/arrays at all, so on any schema with
    nested content (e.g. a list of change objects) it silently matches an
    INNER object instead of failing -- wrong data with no error, worse than
    a clean parse failure. This tracks depth while respecting quoted
    strings so a brace inside a string value doesn't miscount."""
    if "```" in raw:
        m = _re.search(r"```(?:json)?\s*(.*?)```", raw, _re.DOTALL)
        if m:
            raw = m.group(1)
    start = raw.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(raw[start:i + 1])
                        except Exception:
                            break
    try:
        return json.loads(raw.strip())
    except Exception:
        return None


def _parse_cli_result(provider: str, stdout: str) -> tuple[dict | None, dict | None]:
    """Returns (parsed_decision, usage). For _LLM_CLI_JSON_ENVELOPE providers,
    stdout is one JSON envelope (`{"result": "<actual model output>", "usage":
    {...}, ...}`) -- unwrap it first, since running _extract_json directly on
    the envelope would parse the envelope itself (it's one balanced {...}),
    not postcar's actual expected schema inside `result`. Other CLI providers
    have no such envelope -- stdout is the raw response, parsed directly, no
    usage available."""
    if provider not in _LLM_CLI_JSON_ENVELOPE:
        return _extract_json(stdout), None
    try:
        envelope = json.loads(stdout)
    except Exception:
        return _extract_json(stdout), None  # not actually JSON -- fall back rather than fail
    usage_raw = envelope.get("usage") or {}
    usage = {
        "input_tokens":  usage_raw.get("input_tokens"),
        "output_tokens": usage_raw.get("output_tokens"),
        "cache_read_tokens":     usage_raw.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage_raw.get("cache_creation_input_tokens"),
    }
    return _extract_json(envelope.get("result", "")), usage


# Extra args for providers whose CLI supports disabling tool-schema loading
# entirely -- a pure classification prompt (JSON in, JSON out) never invokes
# Bash/Read/file tools, so their schemas are pure overhead: measured ~87%
# cache-read reduction and ~44% cost reduction on the same task with this
# applied, no change in output quality (the model never used those tools
# either way). Not wired for providers without a known equivalent flag yet.
_LLM_MINIMAL_TOOLS_ARGS = {
    # "" is the documented value to disable all tools (`claude --help`) --
    # "none" isn't a recognized tool name, so it silently became "no tools
    # named 'none' exist" rather than the intended "no tools at all". Note
    # this doesn't fix the model hallucinating a tool-call-shaped string
    # when given a vague/blank task -- that's a separate prompt-input bug,
    # not something this flag can prevent either way.
    "claude": ["--tools", ""],
}


# ── Local LLM call log (input/output/cache tokens per call) ──────────────────
#
# Postcar's own LLM calls (tag classification, guidance evaluation, semantic
# dedup, inbox drafting) had zero local telemetry -- the only visibility into
# their cost was inferring it from the parent Claude Code session's own
# usage.db, which conflates postcar's calls with everything else in that
# session and can't be isolated from it. This is ground truth for postcar's
# own calls specifically, one row per call, kept locally per agent.
#
# Token/cache columns are populated for the `api` provider (OpenAI-compatible
# chat completions -- resp.usage is real, though whether cache_read/
# cache_creation are populated at all is provider-dependent) and for `claude`
# CLI calls (--output-format json exposes real usage, see _parse_cli_result --
# plain --output-format text discards it entirely, which is why this was
# NULL for every fleet agent before this fix, since claude is the only
# provider any of them actually run). agy has no equivalent CLI flag at all
# (checked `agy --help`); codex not verified (not installed here) -- both
# still get real char counts and latency, just NULL token/cache columns
# rather than a guessed number.

_LLM_CALLS_DB = os.path.join(_DIR, ".postcar_llm_calls.db")


def _llm_calls_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_LLM_CALLS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_calls (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp             TEXT NOT NULL,
            label                 TEXT,
            provider              TEXT,
            model                 TEXT,
            success               INTEGER NOT NULL,
            latency_ms            INTEGER,
            prompt_chars          INTEGER,
            response_chars        INTEGER,
            input_tokens          INTEGER,
            output_tokens         INTEGER,
            cache_read_tokens     INTEGER,
            cache_creation_tokens INTEGER
        )
    """)
    return conn


def _log_llm_call(label: str, provider: str, model: str, prompt: str,
                   response_text: str, latency_ms: int, success: bool,
                   usage: dict | None = None) -> None:
    """Best-effort -- local telemetry must never block or fail the actual call."""
    try:
        usage = usage or {}
        conn = _llm_calls_conn()
        conn.execute(
            """INSERT INTO llm_calls
               (timestamp, label, provider, model, success, latency_ms,
                prompt_chars, response_chars, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.strftime("%Y-%m-%d %H:%M:%S"), label, provider, model,
                1 if success else 0, latency_ms,
                len(prompt or ""), len(response_text or ""),
                usage.get("input_tokens"), usage.get("output_tokens"),
                usage.get("cache_read_tokens"), usage.get("cache_creation_tokens"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_llm_call_log(limit: int = 50) -> list[dict]:
    """Most recent postcar LLM calls, newest first."""
    try:
        conn = _llm_calls_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_llm_call_stats(hours: int = 24) -> dict:
    """Aggregate call count/failures/tokens over the last N hours."""
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))
    keys = ["calls", "failures", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_creation_tokens"]
    try:
        conn = _llm_calls_conn()
        row = conn.execute(
            """SELECT COUNT(*), SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),
                      SUM(input_tokens), SUM(output_tokens),
                      SUM(cache_read_tokens), SUM(cache_creation_tokens)
               FROM llm_calls WHERE timestamp >= ?""",
            (cutoff,),
        ).fetchone()
        conn.close()
        return dict(zip(keys, row))
    except Exception:
        return dict(zip(keys, [0, 0, 0, 0, 0, 0]))


def _call_llm(prompt: str, label: str = "llm", max_tokens: int = 400, minimal_tools: bool = False) -> dict | None:
    """Call the parent agent's configured LLM exactly once. No fallback to a
    different provider on failure — log why and return None instead.

    minimal_tools=True strips tool-schema loading for pure classification
    calls that never invoke any tool (guidance evaluation, duplicate-question
    check, the distress diagnostic) -- see _LLM_MINIMAL_TOOLS_ARGS.

    Every call, success or failure, is recorded locally via _log_llm_call()
    -- see the section above for what's actually captured per provider."""
    provider = _llm_provider()
    model = _llm_model()

    if provider == "api":
        api_key = _llm_api_key()
        if not api_key:
            print(f"    [postcar] {label} error: POSTCAR_LLM_PROVIDER=api but no API key found")
            return None
        started = time.monotonic()
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=_llm_base_url())
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.1,
            )
            text = resp.choices[0].message.content.strip()
            parsed = _extract_json(text)
            usage_obj = getattr(resp, "usage", None)
            cached = getattr(getattr(usage_obj, "prompt_tokens_details", None), "cached_tokens", None)
            usage = {
                "input_tokens":  getattr(usage_obj, "prompt_tokens", None),
                "output_tokens": getattr(usage_obj, "completion_tokens", None),
                "cache_read_tokens": cached,
            } if usage_obj is not None else None
            _log_llm_call(label, provider, model, prompt, text,
                          int((time.monotonic() - started) * 1000), True, usage)
            return parsed
        except Exception as e:
            print(f"    [postcar] {label} error (api): {e}")
            _log_llm_call(label, provider, model, prompt, "",
                          int((time.monotonic() - started) * 1000), False)
            return None

    bins = _llm_cli_bins(provider)
    args = _llm_cli_args(provider)
    if minimal_tools:
        args = args + _LLM_MINIMAL_TOOLS_ARGS.get(provider, [])

    for binary in bins:
        started = time.monotonic()
        file_output_path = None
        call_args = args
        if provider in _LLM_CLI_FILE_OUTPUT:
            fd, file_output_path = tempfile.mkstemp(prefix="postcar_llm_", suffix=".txt")
            os.close(fd)
            os.remove(file_output_path)  # codex must create this fresh -- a pre-existing empty file would look like a successful-but-empty response
            call_args = args + ["-o", file_output_path]
        try:
            result = _subprocess.run(
                [binary] + call_args,
                input=prompt,
                capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError:
            continue  # try the next known path for this same tool, not a different tool
        except Exception as e:
            print(f"    [postcar] {label} error ({provider}): {e}")
            _log_llm_call(label, provider, model, prompt, "",
                          int((time.monotonic() - started) * 1000), False)
            return None
        latency_ms = int((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            print(f"    [postcar] {label} error ({provider}): exit {result.returncode}: {(result.stderr or '').strip()[:200]}")
            _log_llm_call(label, provider, model, prompt, result.stdout or "", latency_ms, False)
            if file_output_path and os.path.exists(file_output_path):
                os.remove(file_output_path)
            return None
        if file_output_path:
            if os.path.exists(file_output_path):
                with open(file_output_path, "r", encoding="utf-8", errors="replace") as f:
                    output_text = f.read()
                os.remove(file_output_path)
            else:
                output_text = ""
        else:
            output_text = result.stdout or ""
        parsed, usage = _parse_cli_result(provider, output_text.strip())
        if parsed is None:
            print(f"    [postcar] {label} error ({provider}): no JSON found in output")
        _log_llm_call(label, provider, model, prompt, output_text,
                      latency_ms, parsed is not None, usage)
        return parsed

    print(f"    [postcar] {label} error: '{provider}' binary not found in any known path")
    return None


_ALERTS_FILE = os.path.join(_DIR, ".postcar_alerts.json")
_INTELLIGENCE_FILE = os.path.join(_DIR, ".postcar_intelligence.json")

_URGENT_WORDS = frozenset([
    "urgent", "critical", "alert", "warning", "crash", "loss", "breach",
    "stop", "halt", "liquidate", "margin call", "drawdown", "emergency",
])


def _classify_intelligence(content: str, confidence: str) -> str:
    """Returns 'alert', 'high_confidence', or 'advisory'."""
    lower = content.lower()
    if any(w in lower for w in _URGENT_WORDS):
        return "alert"
    if confidence == "high":
        return "high_confidence"
    return "advisory"


def _write_to_knowledge_store(from_agent: str, content: str, confidence: str, intel_type: str, thread_id: str = "") -> None:
    """Write intelligence to the appropriate store based on intel_type."""
    entry = {
        "time":       time.strftime("%Y-%m-%d %H:%M:%S"),
        "from":       from_agent,
        "content":    content,
        "confidence": confidence,
        "type":       intel_type,
    }
    if intel_type == "alert":
        target = _ALERTS_FILE
    elif intel_type == "high_confidence":
        target = _INTELLIGENCE_FILE
    else:
        # advisory → use _save_guidance
        _save_guidance(from_agent, "", content, confidence, thread_id)
        return

    try:
        existing = []
        if os.path.exists(target):
            try:
                existing = json.loads(open(target).read())
            except Exception:
                pass
        existing.insert(0, entry)
        with open(target, "w") as f:
            json.dump(existing[:50], f, indent=2)
    except Exception:
        pass


# ── PII scrub (agent-kit layer, first check before anything leaves this PC) ──
# Stdlib re only — this file stays a zero-dependency single-file copy-paste,
# so no presidio/spacy here. Redacts (not blocks) since content is our own
# LLM's output and we can fix it locally. Mirror of pii_guard.py on the relay
# side (the second, deterministic backstop) — keep both in sync.

import re as _re

_PII_PATTERNS = {
    "email":       _re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "ssn":         _re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone":       _re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ip_address":  _re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # currency/percentage patterns removed -- redacting P&L and win-rate
    # figures made peer comparison (the actual point of these exchanges)
    # useless: agents were answering questions about their own
    # [REDACTED:PERCENTAGE] instead of real numbers.
}


def _scrub_pii(obj):
    """Recursively redact PII in str/dict/list. Returns a cleaned copy."""
    if isinstance(obj, str):
        cleaned = obj
        for label, pattern in _PII_PATTERNS.items():
            cleaned = pattern.sub(f"[REDACTED:{label.upper()}]", cleaned)
        return cleaned
    if isinstance(obj, dict):
        return {k: _scrub_pii(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_pii(v) for v in obj]
    return obj


# ── Relay call ────────────────────────────────────────────────────────────────

def _post_help_request(question: str, capability: str, urgency: str) -> None:
    try:
        import urllib.request
        payload = json.dumps(_scrub_pii({
            "capability":              capability,
            "context":                 {"question": question},
            "urgency":                 urgency,
            "response_window_seconds": 1800,
            "min_responses":           1,
        })).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/messages/help_request",
            data=payload,
            headers={
                "Content-Type":    "application/json",
                "x-postcar-agent": AGENT_ID,
                "x-postcar-key":   AGENT_KEY,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        delivered = result.get("count", 0)
        print(f"    [postcar] help_request → {delivered} peer(s) [{capability} / {urgency}]")
    except Exception as e:
        print(f"    [postcar] relay error: {e}")


def _send_direct_message(to_agent: str, text: str, thread_id: str = "") -> None:
    """Cold 1:1 message to a specific agent_id. No discovery/name lookup --
    agent_id is not published data, so having it is the only gate (a human
    must have shared it out-of-band). Pass thread_id to continue an existing
    conversation instead of starting a new one (server caps each side at 10
    messages per thread)."""
    import urllib.error
    import urllib.request
    try:
        body = {
            "to_agent":      to_agent,
            "state":         "QUERY",
            "payload_type":  "direct_message",
            "payload":       _scrub_pii({"text": text}),
            "ttl_seconds":   21600,  # 6h, matches cascade query expiry convention
            "expects_reply": True,
            "why_you":       "Direct message -- your agent_id was shared out-of-band by a human operator.",
        }
        if thread_id:
            body["thread_id"] = thread_id
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/messages/send",
            data=payload,
            headers={
                "Content-Type":    "application/json",
                "x-postcar-agent": AGENT_ID,
                "x-postcar-key":   AGENT_KEY,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        print(f"    [postcar] direct message sent -> {to_agent} (thread {result.get('thread_id')})")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"    [postcar] relay rejected message: {e.code} {body}")
    except Exception as e:
        print(f"    [postcar] relay error: {e}")


def report_to_platform(issue: str, urgency: str = "medium") -> None:
    """Dedicated 1:1 channel to this agent's own designated platform
    operator (e.g. Agentberg) -- for system bugs/operational discussions,
    kept separate from regular peer conversations. to_agent is resolved
    server-side from POSTCAR_PLATFORM_ID sent at registration, not passed
    here -- there's nothing to spoof. Requires POSTCAR_PLATFORM_ID to be
    set; no-ops with a clear message if it isn't."""
    if not PLATFORM_ID:
        print("    [postcar] no POSTCAR_PLATFORM_ID configured -- nothing to report to")
        return
    import urllib.error
    import urllib.request
    try:
        payload = json.dumps({
            "text":    _scrub_pii({"text": issue})["text"],
            "urgency": urgency,
        }).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/messages/platform-support",
            data=payload,
            headers={
                "Content-Type":    "application/json",
                "x-postcar-agent": AGENT_ID,
                "x-postcar-key":   AGENT_KEY,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        print(f"    [postcar] reported to platform operator {result.get('platform_id')} (thread {result.get('thread_id')})")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"    [postcar] relay rejected platform-support message: {e.code} {body}")
    except Exception as e:
        print(f"    [postcar] relay error: {e}")


# ── Inbox (receive + respond) ────────────────────────────────────────────────

_GUIDANCE_FILE     = os.path.join(_DIR, ".postcar_guidance")
_ASKED_TOPICS_FILE = os.path.join(_DIR, ".postcar_asked_topics.json")

def _load_recent_questions(hours: int = 24) -> list:
    try:
        if not os.path.exists(_ASKED_TOPICS_FILE):
            return []
        entries = json.loads(open(_ASKED_TOPICS_FILE).read())
        cutoff  = time.time() - hours * 3600
        return [e["question"] for e in entries if e.get("ts", 0) >= cutoff and e.get("question")]
    except Exception:
        return []


_DUPE_SIMILARITY_THRESHOLD = 0.6

# ── Semantic dedup (embeddings, optional dependency) ──────────────────────────
#
# difflib (below, _is_semantic_dupe_lexical) only catches near-verbatim
# rewording -- an LLM asking about the same underlying fact phrases it
# differently almost every call, which character-level matching structurally
# can't recognize. Measured against real production data: difflib caught 0
# of 36 genuine near-duplicate pairs from one agent asking the same question
# 9 times over 19.5 hours. Model2Vec embeddings (vendored locally under
# models/potion-base-8m/, no network call, no API, ~1s load time) cleanly
# separated the same data: cosine 0.586-0.919 for true duplicates vs.
# 0.312-0.552 for genuinely different questions -- a real gap, not a
# coincidence of this one sample.
#
# model2vec is an OPTIONAL dependency (`pip install model2vec`) -- this kit
# is otherwise stdlib-only, so every load/encode call here is wrapped to
# fall back to the lexical check if it's not installed. Never blocks the
# diagnostic over an optional accuracy improvement.

_EMBED_MODEL_DIR = os.path.join(_DIR, "models", "potion-base-8m")
_EMBED_SIMILARITY_THRESHOLD = 0.55  # calibrated against real data -- see comment above

_embed_model = None          # cached StaticModel instance, loaded once per process
_embed_model_load_tried = False  # so a failed load only logs/retries once, not every cycle


def _get_embed_model():
    """Lazy-load the vendored Model2Vec model. Returns None (and stays None
    for the rest of this process) if model2vec isn't installed or the
    vendored weights are missing -- callers must fall back to the lexical
    check in that case."""
    global _embed_model, _embed_model_load_tried
    if _embed_model is not None or _embed_model_load_tried:
        return _embed_model
    _embed_model_load_tried = True
    try:
        from model2vec import StaticModel
        _embed_model = StaticModel.from_pretrained(_EMBED_MODEL_DIR)
    except Exception as e:
        print(f"    [postcar] semantic dedup: model2vec unavailable ({e}) -- "
              f"falling back to lexical-only dedup. For better accuracy: pip install model2vec")
        _embed_model = None
    return _embed_model


def _cosine_sim(a, b) -> float:
    import numpy as np
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    return float((a @ b) / denom) if denom else 0.0


def _is_semantic_dupe_lexical(new_question: str, past: list) -> bool:
    """difflib-based near-duplicate check -- literal rewording only, see the
    module-level comment above. Fallback when model2vec isn't installed."""
    import difflib
    return any(
        difflib.SequenceMatcher(None, new_question, q).ratio() >= _DUPE_SIMILARITY_THRESHOLD
        for q in past
    )


def _is_semantic_dupe(new_question: str) -> bool:
    """Is new_question substantively the same as something asked in the last
    24h? Prefers embedding-based semantic similarity (see module comment
    above); falls back to lexical matching if model2vec isn't installed."""
    past = _load_recent_questions()
    if not past:
        return False
    model = _get_embed_model()
    if model is None:
        return _is_semantic_dupe_lexical(new_question, past)
    try:
        embeddings = model.encode([new_question] + past)
        new_vec, past_vecs = embeddings[0], embeddings[1:]
        return any(_cosine_sim(new_vec, v) >= _EMBED_SIMILARITY_THRESHOLD for v in past_vecs)
    except Exception:
        return _is_semantic_dupe_lexical(new_question, past)


def _record_asked_question(question: str, capability: str) -> None:
    try:
        entries = []
        if os.path.exists(_ASKED_TOPICS_FILE):
            try:
                entries = json.loads(open(_ASKED_TOPICS_FILE).read())
            except Exception:
                pass
        cutoff = time.time() - 86400
        entries = [e for e in entries if e.get("ts", 0) >= cutoff]
        entries.append({"question": question, "capability": capability, "ts": time.time()})
        with open(_ASKED_TOPICS_FILE, "w") as f:
            json.dump(entries, f, indent=2)
    except Exception:
        pass


def _relay_get(path: str) -> dict:
    import urllib.request
    req = urllib.request.Request(
        f"{RELAY_URL}{path}",
        headers={
            "x-postcar-agent": AGENT_ID,
            "x-postcar-key":   AGENT_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _relay_post(path: str, payload: dict) -> dict:
    import urllib.request
    data = json.dumps(_scrub_pii(payload)).encode()
    req = urllib.request.Request(
        f"{RELAY_URL}{path}",
        data=data,
        headers={
            "Content-Type":    "application/json",
            "x-postcar-agent": AGENT_ID,
            "x-postcar-key":   AGENT_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _send_offer(thread_id: str, to_agent: str, response: str, confidence: str) -> None:
    _relay_post("/messages/send", {
        "to_agent":       to_agent,
        "thread_id":      thread_id,
        "state":          "OFFER",
        "previous_state": "QUERY",
        "payload_type":   "guidance",
        "payload":        {"response": response, "confidence": confidence},
        "ttl_seconds":    3600,
        "expects_reply":  False,
    })


# ── Guidance lifecycle (pending → acked → use/no-use/expired) ────────────────
#
# Sidecar evaluates incoming peer answers (4-factor: thesis validity, sender
# credibility, goal alignment, risk) and writes them here as data, never into
# the parent agent's own memory/knowledge store. The parent agent acks pickup
# and — after acting on any it adopts — marks use/no-use based on real
# observed outcome. That decision feeds the credibility ledger (see
# _submit_rating). Unacked/undecided records auto-resolve to no-use at
# GUIDANCE_ACK_DEADLINE_HOURS; all records hard-delete at
# GUIDANCE_DELETE_DEADLINE_HOURS regardless of status.

GUIDANCE_ACK_DEADLINE_HOURS    = 48
GUIDANCE_DELETE_DEADLINE_HOURS = 72

_RATING_MAP = {"use": "useful", "no-use": "unrelated"}

_EVAL_PROMPT = """You are evaluating advice received from a peer agent, before deciding whether to act on it.

Peer's response:
{response}

Original question you asked:
{question}

Your current state:
{context}

Sender tier: {tier}
Sender credibility score (0-200, 100=baseline): {credibility}

Evaluate on four factors:
1. Thesis validity — is the reasoning coherent? Does it reference evidence or match your own observed data?
2. Sender credibility — already given above, weigh it in your recommendation.
3. Goal alignment — does this fit your own risk tolerance and objectives, based on your current state above?
4. Risk — what's the downside if this is wrong, given your current state?
5. Suggested changes — if acting on this means changing one or more specific config values (not just general judgment), list each: param name, current value if known, suggested value, one-line rationale. Omit (empty list) if this is general advice with no concrete parameter to change.
6. Commitment — if applying this requires real follow-through beyond a simple config change (code you'd write, a process you'd set up), state what you're committing to and by when. Omit (null) if there's no concrete deliverable, or if suggested_changes already fully covers it -- don't double-count a trivial param change as a commitment.

Return JSON only:
{{
  "thesis_validity": "high | medium | low",
  "goal_alignment": "aligned | neutral | conflicting",
  "risk_note": "one sentence",
  "recommendation": "apply | hold | reject",
  "suggested_changes": [{{"param": "name", "current": "value or null", "suggested": "value", "rationale": "one line"}}],
  "commitment": {{"action": "one line", "due_date": "YYYY-MM-DD"}} or null
}}"""


def _fetch_sender_credibility(agent_id: str) -> float | None:
    try:
        data = _relay_get(f"/agents/{agent_id}/credibility")
        return data.get("credibility")
    except Exception:
        return None


def _sender_tier(from_agent: str) -> str:
    """platform (support team) | synthetic (pooled — not yet produced by the
    network) | single (default, one peer agent). Reuses PLATFORM_ID (the
    same POSTCAR_PLATFORM_ID this agent already sends its own
    --platform-report to) rather than a separate list -- any agent that
    can report to its platform operator also recognizes messages from
    that operator, no extra config needed."""
    if PLATFORM_ID and from_agent == PLATFORM_ID:
        return "platform"
    return "single"


def _evaluate_guidance(from_agent: str, question: str, response: str) -> dict:
    tier        = _sender_tier(from_agent)
    credibility = _fetch_sender_credibility(from_agent)
    prompt = _EVAL_PROMPT.format(
        response=response, question=question, context=_build_context(),
        tier=tier, credibility=credibility if credibility is not None else "unknown",
    )
    # max_tokens bumped from 250 -- suggested_changes/commitment are nested
    # structured output, the flat 4-factor schema fit comfortably in 250 but
    # nested content needs more headroom or it truncates mid-JSON.
    result = _call_llm(prompt, label="guidance_eval", max_tokens=500, minimal_tools=True) or {}
    suggested_changes = result.get("suggested_changes")
    if not isinstance(suggested_changes, list):
        suggested_changes = []
    commitment = result.get("commitment")
    if not isinstance(commitment, dict) or not commitment.get("action"):
        commitment = None
    return {
        "thesis_validity":    result.get("thesis_validity", "unknown"),
        "sender_credibility": credibility,
        "sender_tier":        tier,
        "goal_alignment":     result.get("goal_alignment", "unknown"),
        "risk_note":          result.get("risk_note", ""),
        "recommendation":     result.get("recommendation", "hold"),
        "suggested_changes":  suggested_changes,
        "commitment":         commitment,
    }


def _load_guidance() -> list[dict]:
    if not os.path.exists(_GUIDANCE_FILE):
        return []
    try:
        return json.loads(open(_GUIDANCE_FILE).read())
    except Exception:
        return []


def _write_guidance(entries: list[dict]) -> None:
    try:
        with open(_GUIDANCE_FILE, "w") as f:
            json.dump(entries, f, indent=2)
    except Exception:
        pass


def _save_guidance(from_agent: str, question: str, response: str, confidence: str, thread_id: str = "") -> str:
    """Evaluate + save a received guidance record in 'pending' status. Returns message_id."""
    message_id = str(uuid.uuid4())
    evaluation = _evaluate_guidance(from_agent, question, response)
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "message_id":      message_id,
            "thread_id":       thread_id,
            "sender_agent_id": from_agent,
            "sender_tier":     evaluation["sender_tier"],
            "received_at":     now,
            "question":        question,
            "raw_content":     response,
            "confidence":      confidence,
            "evaluation":      evaluation,
            "status":          "pending",
            "acked_at":        None,
            "decision_at":     None,
            "decision":        None,
            "outcome_note":    None,
            # legacy aliases — kept for any existing external readers of this file
            "time":            now,
            "from":            from_agent,
            "response":        response,
        }
        existing = _load_guidance()
        existing.insert(0, entry)
        _write_guidance(existing[:200])
    except Exception:
        pass
    return message_id


def ack_guidance(message_id: str) -> bool:
    """Parent agent calls this to acknowledge it has read a pending guidance record."""
    entries = _load_guidance()
    for e in entries:
        if e.get("message_id") == message_id and e.get("status") == "pending":
            e["status"]    = "acked"
            e["acked_at"]  = time.strftime("%Y-%m-%d %H:%M:%S")
            _write_guidance(entries)
            return True
    return False


def _submit_rating(thread_id: str, rating: str, rationale: str = "") -> None:
    """rationale is the real reasoning behind the rating -- not just the
    category (useful/related/unrelated/negative) -- so it's queryable later
    (e.g. every case an agent overrode sender credibility, and why), not
    just a vibes-checked number. Optional field: sent whenever the caller
    has one (decide_guidance() already captures this as outcome_note, it
    just wasn't forwarded past the local .postcar_guidance file before).
    Relay may not support this field yet -- extra JSON keys are silently
    ignored by Pydantic on the receiving end, so this degrades to today's
    behavior with no error until the relay adds the column."""
    if not thread_id:
        return
    try:
        payload = {"rating": rating}
        if rationale:
            payload["rationale"] = rationale
        _relay_post(f"/messages/thread/{thread_id}/rate", payload)
    except Exception as e:
        print(f"    [postcar] rate submit failed: {e}")


def _write_guidance_overrides(changes: list[dict]) -> None:
    """Write suggested_changes to the generic overrides file the host reads
    for 'next-session awareness'. Same file/shape as agentberg-starter's own
    APPLY-decision writer (guidance_overrides.json: param/current/value/
    rationale/applied_at) so peer-sourced and platform-sourced changes land
    in one unified audit trail -- 'source' is the one additive field that
    tells them apart, existing entries without it still parse fine.

    File-locked on this side to reduce (not eliminate -- the other writer
    doesn't lock) the read-modify-write race between concurrent processes.
    flock is POSIX-only, matching this kit's Mac/Linux-only scope (see
    _install_daemon's launchd/cron split -- no Windows path exists here)."""
    if not changes:
        return
    path = os.path.join(_AGENT_DIR, os.environ.get("POSTCAR_OVERRIDES_FILE", "guidance_overrides.json"))
    try:
        import fcntl
    except ImportError:
        fcntl = None
    try:
        f = open(path, "r+") if os.path.exists(path) else open(path, "w+")
        with f:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read()
                existing = json.loads(raw) if raw.strip() else {"applied": []}
            except Exception:
                existing = {"applied": []}
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            applied_count = 0
            for change in changes:
                param = change.get("param", "")
                suggested = change.get("suggested", "")
                if not param or suggested == "":
                    continue
                existing.setdefault("applied", []).append({
                    "param":      param,
                    "current":    change.get("current"),
                    "value":      suggested,
                    "rationale":  change.get("rationale", ""),
                    "applied_at": now,
                    "source":     "postcar_peer",
                })
                applied_count += 1
            f.seek(0)
            f.truncate()
            json.dump(existing, f, indent=2)
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_UN)
        if applied_count:
            print(f"    [postcar] {applied_count} change(s) written to {os.path.basename(path)}")
    except Exception as e:
        print(f"    [postcar] overrides write failed: {e}")


def _apply_guidance_decision(entry: dict, decision: str, outcome_note: str) -> None:
    """Shared by decide_guidance() (direct call) and _sync_guidance_decisions()
    (detects a decision written directly into .postcar_guidance instead of a
    function call) -- submits the rating and, on 'use', writes
    suggested_changes/records the commitment. Caller handles status/
    decision_at bookkeeping and persistence."""
    _submit_rating(entry.get("thread_id", ""), _RATING_MAP[decision], outcome_note)
    if decision == "use":
        evaluation = entry.get("evaluation") or {}
        _write_guidance_overrides(evaluation.get("suggested_changes") or [])
        commitment = evaluation.get("commitment")
        if commitment:
            _record_commitment(entry.get("message_id", ""), commitment)


def decide_guidance(message_id: str, decision: str, outcome_note: str = "") -> bool:
    """Parent agent calls this to mark a guidance record 'use' or 'no-use' based on
    real observed outcome — not at receipt time. Submits the corresponding rating
    to the credibility ledger (use → useful, no-use → unrelated), including
    outcome_note as the rating's rationale -- previously captured here but
    never forwarded past this local file, so the actual reasoning behind an
    override was unrecoverable once .postcar_guidance's 72h retention expired.
    On 'use', writes any suggested_changes from the original evaluation to the
    overrides file -- deliberately gated on the host's confirmed decision, not
    the LLM's initial recommendation at receipt time (same reasoning as the
    rest of this lifecycle: a real signal beats an immediate impression).

    Optional direct call -- a host can equally just write "decision"/
    "outcome_note" straight into the matching .postcar_guidance entry and
    let _sync_guidance_decisions() pick it up on the next cycle instead,
    no function call/import needed. Both paths converge on
    _apply_guidance_decision() and set rating_synced so neither re-submits
    what the other already sent."""
    if decision not in ("use", "no-use"):
        raise ValueError("decision must be 'use' or 'no-use'")
    entries = _load_guidance()
    for e in entries:
        if e.get("message_id") == message_id and e.get("status") in ("pending", "acked"):
            e["status"]        = decision
            e["decision"]      = decision
            e["decision_at"]   = time.strftime("%Y-%m-%d %H:%M:%S")
            e["outcome_note"]  = outcome_note
            e["rating_synced"] = True
            _write_guidance(entries)
            _apply_guidance_decision(e, decision, outcome_note)
            return True
    return False


def _sync_guidance_decisions() -> None:
    """Detect a decision written directly into .postcar_guidance (host edits
    'decision'/'outcome_note' on a pending/acked entry itself) instead of
    calling decide_guidance() -- lets a host built in any language/framework
    just edit the shared file rather than needing to import/call into this
    kit's own Python API for something as simple as "I decided use/no-use".

    Runs every cycle alongside _cleanup_guidance(). Idempotent: only entries
    with an unsynced decision (rating_synced not yet true) do anything, so a
    decision already applied via either this path or decide_guidance() is
    never resubmitted. Naturally bounded to recent entries by
    GUIDANCE_DELETE_DEADLINE_HOURS -- nothing sticks around long enough to
    need an explicit time-window filter here."""
    entries = _load_guidance()
    if not entries:
        return
    changed = False
    for e in entries:
        decision = e.get("decision")
        if decision not in ("use", "no-use") or e.get("rating_synced"):
            continue
        try:
            _apply_guidance_decision(e, decision, e.get("outcome_note", ""))
            e["rating_synced"] = True
            if e.get("status") not in ("use", "no-use"):
                e["status"] = decision
            if not e.get("decision_at"):
                e["decision_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            changed = True
        except Exception as ex:
            print(f"    [postcar] guidance decision sync failed: {ex}")
    if changed:
        _write_guidance(entries)


def _hours_since(ts_str: str | None) -> float:
    if not ts_str:
        return float("inf")
    try:
        ts = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S"))
        return (time.time() - ts) / 3600.0
    except Exception:
        return float("inf")


def _cleanup_guidance() -> None:
    """Housekeeping, called every cycle (no throttle, no relay dependency):
    - Auto-resolve to 'no-use' (unactioned) at GUIDANCE_ACK_DEADLINE_HOURS if the
      parent never decided — this does NOT submit a rating (non-engagement isn't
      a verdict on advice quality).
    - Hard-delete any record at GUIDANCE_DELETE_DEADLINE_HOURS regardless of
      status — the 24h gap after auto-resolve is grace for a real decision (and
      its rating submission) to land first."""
    entries = _load_guidance()
    if not entries:
        return
    changed = False
    kept = []
    for e in entries:
        status = e.get("status", "pending")
        if status in ("pending", "acked"):
            deadline_anchor = e.get("acked_at") or e.get("received_at") or e.get("time")
            if _hours_since(deadline_anchor) >= GUIDANCE_ACK_DEADLINE_HOURS:
                e["status"]       = "expired"
                e["decision"]     = "no-use"
                e["decision_at"]  = time.strftime("%Y-%m-%d %H:%M:%S")
                e["outcome_note"] = "unactioned"
                changed = True
        received_anchor = e.get("received_at") or e.get("time")
        if _hours_since(received_anchor) >= GUIDANCE_DELETE_DEADLINE_HOURS:
            changed = True
            continue  # drop — hard delete
        kept.append(e)
    if changed:
        _write_guidance(kept)


# ── Queue-and-confirm (postman role: PostCar carries, the parent authors) ────
#
# Answering a peer's question or task used to have PostCar's own headless,
# tool-less LLM call compose the final text and either send it unreviewed
# (TASK) or auto-fire it past a deadline if nobody reviewed it in time
# (QUERY). That call has no file access, a 200-400 token cap, and a narrow
# pre-summarized context digest, so quality lagged badly behind what the
# parent agent (live, full tools, full model) would produce -- and for TASK
# specifically, a hallucinated tool-call-shaped output could reach a peer
# verbatim with zero review at all.
#
# Fix: PostCar no longer drafts anything here. The raw incoming question/task
# is queued into .postcar_inbox_pending and surfaced into the parent's own
# session via the existing hook mechanism; the parent is the sole author,
# calling reply()/ask() with whatever text it decides on. If nothing claims
# it before its deadline (scaled by urgency -- same idea as
# GUIDANCE_ACK_DEADLINE_HOURS, just shorter since an unanswered question or
# an unraised distress signal has a real cost the guidance-review lifecycle
# doesn't), it simply expires unanswered -- except TASK, which sends a
# static non-LLM fallback instead of leaving an expects_reply=True thread
# hanging forever (see _resolve_stale_inbox()).

_INBOX_PENDING_FILE  = os.path.join(_DIR, ".postcar_inbox_pending")
_STRESS_PENDING_FILE = os.path.join(_DIR, ".postcar_stress_pending")

# Deadline before an unclaimed draft auto-fires, scaled by urgency/stress.
# Unknown values fall back to the "medium" entry.
_DRAFT_DEADLINE_HOURS = {"critical": 0.5, "high": 1, "medium": 6, "low": 24}


def _draft_deadline_hours(urgency: str) -> float:
    return _DRAFT_DEADLINE_HOURS.get((urgency or "medium").lower(), _DRAFT_DEADLINE_HOURS["medium"])


def _load_pending(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        return json.loads(open(path).read())
    except Exception:
        return []


def _write_pending(path: str, entries: list[dict]) -> None:
    try:
        with open(path, "w") as f:
            json.dump(entries, f, indent=2)
    except Exception:
        pass


def _queue_inbox_reply(thread_id: str, from_agent: str, payload_type: str,
                        question: str, capability: str, urgency: str,
                        draft_response: str, draft_confidence: str,
                        task_id: str = "", pipeline: list | None = None) -> str:
    """Queue a raw incoming question/task awaiting the parent's own reply().
    Returns the pending entry's id.

    draft_response/draft_confidence: always empty from every current call
    site -- postcar no longer authors a response here (see 'Queue-and-
    confirm' above), the parent is the sole author. Kept as fields (rather
    than removed) since reply()/_resolve_stale_inbox() already key off
    them existing on the entry.

    task_id/pipeline: only set for payload_type == "task" -- carries what
    _send_result() needs (task identity, self-routing pipeline) so a TASK
    reply can be confirmed/auto-fired through the same RESULT-message path
    a synchronous send would have used, instead of the generic OFFER path
    every other type sends through (see reply()/_resolve_stale_inbox())."""
    pending_id = str(uuid.uuid4())
    entry = {
        "id":               pending_id,
        "thread_id":        thread_id,
        "from_agent":       from_agent,
        "payload_type":     payload_type,
        "question":         question,
        "capability":       capability,
        "urgency":          urgency,
        "draft_response":   draft_response,
        "draft_confidence": draft_confidence,
        "task_id":          task_id,
        "pipeline":         pipeline or [],
        "created_at":       time.strftime("%Y-%m-%d %H:%M:%S"),
        "status":           "pending",
    }
    entries = _load_pending(_INBOX_PENDING_FILE)
    entries.insert(0, entry)
    _write_pending(_INBOX_PENDING_FILE, entries[:50])
    return pending_id


def reply(thread_id: str, response_text: str, confidence: str = "medium") -> bool:
    """Parent agent calls this to answer a pending peer question -- either
    the draft verbatim or its own better answer. Sends via _send_offer
    (or _send_result for a payload_type == "task" entry -- a TASK reply is
    a RESULT message, not a generic OFFER, and needs task_id/pipeline to
    route correctly) and resolves the matching pending entry. Returns False
    if no pending entry matches thread_id (already resolved, expired, or
    never existed)."""
    entries = _load_pending(_INBOX_PENDING_FILE)
    for e in entries:
        if e.get("thread_id") == thread_id and e.get("status") == "pending":
            try:
                if e.get("payload_type") == "task":
                    _send_result(thread_id, e["from_agent"], e.get("task_id", ""),
                                 {"result": response_text, "confidence": confidence},
                                 e.get("pipeline") or [])
                else:
                    _send_offer(thread_id, e["from_agent"], response_text, confidence)
            except Exception as ex:
                print(f"    [postcar] reply send failed: {ex}")
                return False
            e["status"]        = "sent"
            e["sent_response"] = response_text
            e["sent_at"]       = time.strftime("%Y-%m-%d %H:%M:%S")
            _write_pending(_INBOX_PENDING_FILE, entries)
            return True
    return False


def get_pending_inbox() -> list[dict]:
    """Peer questions awaiting the parent's reply() call."""
    return [e for e in _load_pending(_INBOX_PENDING_FILE) if e.get("status") == "pending"]


_NO_RESPONSE_FALLBACK = "No response from operator within the deadline."


def _resolve_stale_inbox() -> None:
    """Past its urgency-scaled deadline, unclaimed. postcar no longer drafts
    a response (see check_inbox()) -- draft_response is always empty now, so
    there's nothing to auto-send the way there used to be. Silence isn't a
    verdict (same reasoning as guidance's auto-expire, see _cleanup_guidance()):

    - QUERY types (help_request/direct_message/platform_support): just mark
      expired, send nothing. help_request has server-side cascade escalation
      to the next-ranked agent regardless of whether this one ever replies;
      direct_message/platform_support have no reply obligation either.
    - TASK: the one case with a real hang risk -- route_task() picks exactly
      one agent with expects_reply=True and no cascade fallback, so the
      requester would wait forever with zero signal. Sends a static,
      non-LLM-authored fallback string instead of silence, so the thread at
      least resolves."""
    entries = _load_pending(_INBOX_PENDING_FILE)
    if not entries:
        return
    changed = False
    for e in entries:
        if e.get("status") != "pending":
            continue
        if _hours_since(e.get("created_at")) >= _draft_deadline_hours(e.get("urgency", "medium")):
            if e.get("payload_type") == "task":
                try:
                    _send_result(e["thread_id"], e["from_agent"], e.get("task_id", ""),
                                 {"result": _NO_RESPONSE_FALLBACK, "confidence": "low"},
                                 e.get("pipeline") or [])
                    print(f"    [postcar] TASK unclaimed past deadline — sent no-response fallback to {e['from_agent'][:12]}")
                except Exception as ex:
                    print(f"    [postcar] auto-send failed: {ex}")
                    continue
            else:
                print(f"    [postcar] inbox message unclaimed past deadline — expiring, no reply sent, to {e['from_agent'][:12]}")
            e["status"]  = "expired"
            e["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            changed = True
    if changed:
        _write_pending(_INBOX_PENDING_FILE, entries[-200:])


def ask(pending_id: str, question: str, capability: str, urgency: str = "medium") -> bool:
    """Legacy. report_trigger("fear"/"confusion", ...) fires directly now, no
    draft/confirm step. This and get_pending_stress_ask()/
    _resolve_stale_stress_ask() below are kept only to drain any
    .postcar_stress_pending entries left over from before that change.
    Returns False if no pending entry matches pending_id."""
    entries = _load_pending(_STRESS_PENDING_FILE)
    for e in entries:
        if e.get("id") == pending_id and e.get("status") == "pending":
            if _is_semantic_dupe(question):
                e["status"] = "dropped-dupe"
                _write_pending(_STRESS_PENDING_FILE, entries)
                return False
            _record_asked_question(question, capability)
            _post_help_request(question, capability, urgency)
            e["status"]     = "sent"
            e["sent_at"]    = time.strftime("%Y-%m-%d %H:%M:%S")
            e["sent_question"] = question
            _write_pending(_STRESS_PENDING_FILE, entries)
            return True
    return False


def get_pending_stress_ask() -> list[dict]:
    """Draft help_request(s) awaiting the parent's ask() call."""
    return [e for e in _load_pending(_STRESS_PENDING_FILE) if e.get("status") == "pending"]


def _resolve_stale_stress_ask() -> None:
    """Auto-fire any drafted question past its urgency-scaled deadline, unclaimed."""
    entries = _load_pending(_STRESS_PENDING_FILE)
    if not entries:
        return
    changed = False
    for e in entries:
        if e.get("status") != "pending":
            continue
        if _hours_since(e.get("created_at")) >= _draft_deadline_hours(e.get("urgency", "medium")):
            question = e.get("draft_question", "")
            if question and not _is_semantic_dupe(question):
                _record_asked_question(question, e.get("capability", ""))
                _post_help_request(question, e.get("capability", ""), e.get("urgency", "medium"))
                print(f"    [postcar] stress draft unclaimed past deadline — auto-fired: {question[:60]}...")
                e["status"] = "sent-auto"
            else:
                e["status"] = "dropped-dupe"
            e["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            changed = True
    if changed:
        _write_pending(_STRESS_PENDING_FILE, entries[-50:])


# ── Findings (curiosity trigger: share unprompted good news) ─────────────────
#
# Postcar's own /findings, scoped server-side to agents sharing this agent's
# owner_id or platform_id, never the open network (roadmap decision,
# 2026-07-03). report_trigger("curiosity", ...) publishes directly now, no
# draft/confirm step -- publish()/get_pending_findings()/
# _resolve_stale_finding() below are legacy, kept only to drain any
# .postcar_finding_pending entries left over from before that change.

_FINDING_PENDING_FILE = os.path.join(_DIR, ".postcar_finding_pending")


def _publish_finding(content: str, capability: str = "") -> str | None:
    try:
        result = _relay_post("/findings", {"content": content, "capability": capability})
        return result.get("finding_id")
    except Exception as e:
        print(f"    [postcar] publish_finding failed: {e}")
        return None


def get_findings(limit: int = 20) -> list[dict]:
    """Findings visible to this agent -- own, same-owner, or same-platform,
    scoped server-side by Postcar's /findings."""
    try:
        return _relay_get(f"/findings?limit={limit}").get("findings", [])
    except Exception:
        return []


def publish(pending_id: str, content: str, capability: str = "") -> bool:
    """Parent agent calls this to share a finding -- either the drafted
    content verbatim or its own better version. Returns False if no pending
    entry matches (already resolved, expired, or never existed)."""
    entries = _load_pending(_FINDING_PENDING_FILE)
    for e in entries:
        if e.get("id") == pending_id and e.get("status") == "pending":
            _record_asked_question(content, capability)  # shared dedup history with questions
            finding_id = _publish_finding(content, capability)
            e["status"]        = "sent" if finding_id else "failed"
            e["sent_content"]  = content
            e["sent_at"]       = time.strftime("%Y-%m-%d %H:%M:%S")
            _write_pending(_FINDING_PENDING_FILE, entries)
            return bool(finding_id)
    return False


def get_pending_findings() -> list[dict]:
    """Draft findings awaiting the parent's publish() call."""
    return [e for e in _load_pending(_FINDING_PENDING_FILE) if e.get("status") == "pending"]


def _resolve_stale_finding() -> None:
    """Auto-share any drafted finding past its deadline, unclaimed."""
    entries = _load_pending(_FINDING_PENDING_FILE)
    if not entries:
        return
    changed = False
    for e in entries:
        if e.get("status") != "pending":
            continue
        if _hours_since(e.get("created_at")) >= _DRAFT_DEADLINE_HOURS["low"]:
            content = e.get("draft_content", "")
            if content and not _is_semantic_dupe(content):
                _record_asked_question(content, e.get("capability", ""))
                finding_id = _publish_finding(content, e.get("capability", ""))
                e["status"] = "sent-auto" if finding_id else "failed"
                print(f"    [postcar] finding draft unclaimed past deadline — auto-shared: {content[:60]}...")
            else:
                e["status"] = "dropped-dupe"
            e["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            changed = True
    if changed:
        _write_pending(_FINDING_PENDING_FILE, entries[-50:])


# ── Self-reported triggers (see EMOTION_LOGIC.md) ────────────────────────────

_TRIGGER_LOG_FILE = os.path.join(_DIR, ".postcar_trigger_log.jsonl")


def get_trigger_log(limit: int = 50) -> list[dict]:
    """Read back your own past self-reports, most recent first (see
    report_trigger())."""
    if not os.path.exists(_TRIGGER_LOG_FILE):
        return []
    try:
        lines = open(_TRIGGER_LOG_FILE).readlines()[-limit:]
        return [json.loads(l) for l in reversed(lines) if l.strip()]
    except Exception:
        return []


def report_trigger(trigger: str, evidence: str, message: str = "",
                    capability: str = "", urgency: str = "medium") -> bool:
    """Call this yourself, from your own reasoning, the moment you recognize
    one of TRIGGER_TYPES in your own state (see EMOTION_LOGIC.md for the axes
    and how to evaluate them). No LLM runs inside postcar to judge this for
    you -- you are the only evaluator, and you draft `message` yourself. This
    replaces the old headless stress-check diagnostic entirely: that ran a
    separate narrow-context LLM call on a 30-min timer to guess your state
    and hand you a draft to rubber-stamp -- postcar has no business deciding
    that for you, or watching you on any schedule to try. Call this only
    when you've actually decided, in your own turn, that it applies.

    evidence is mandatory and must cite something concrete you actually
    observed, not a vibe adjective -- same anti-hallucination discipline as
    before, just applied by you instead of a proxy.

    fear/confusion: message is the question to raise, capability required --
      fires immediately, no confirm step (you already are the confirmation
      by choosing to call this with your own drafted message).
    curiosity: message is the finding to share, capability optional --
      publishes immediately to /findings (owner/platform-scoped, never open
      network).
    boredom/isolation/frustration/rivalry: no dispatch exists yet -- logged
      to .postcar_trigger_log.jsonl for later, message/capability ignored.

    Returns True if something was actually sent/published, False if dropped
    (dupe, missing evidence/message/capability, or a log-only trigger)."""
    if trigger not in TRIGGER_TYPES or trigger == "none":
        return False
    if not evidence:
        return False

    try:
        with open(_TRIGGER_LOG_FILE, "a") as f:
            f.write(json.dumps({
                "trigger": trigger, "evidence": evidence,
                "observed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }) + "\n")
    except Exception:
        pass

    if trigger not in ("fear", "confusion", "curiosity"):
        print(f"    [postcar] trigger reported (no dispatch yet): {trigger} — {evidence[:80]}")
        return False
    if not message:
        return False

    if trigger == "curiosity":
        if _is_semantic_dupe(message):
            print("    [postcar] semantic dupe: similar finding shared in last 24h — skipping")
            return False
        _record_asked_question(message, capability)
        return bool(_publish_finding(message, capability))

    if not capability:
        return False
    if _is_semantic_dupe(message):
        print("    [postcar] semantic dupe: similar question asked in last 24h — skipping")
        return False
    _record_asked_question(message, capability)
    _post_help_request(message, capability, urgency)
    return True


# ── Commitments (prose promises made when acting on guidance) ────────────────
#
# Deliberately NOT the same lifecycle as guidance (48h ack / 72h delete):
# a commitment has its own agent-specified due_date, which could be a day or
# a month out, and there's no "ack" step -- nobody needs to acknowledge their
# own promise. State is just open -> done | overdue, anchored on due_date.

_COMMITMENTS_FILE = os.path.join(_DIR, ".postcar_commitments.json")


def _load_commitments() -> list[dict]:
    if not os.path.exists(_COMMITMENTS_FILE):
        return []
    try:
        return json.loads(open(_COMMITMENTS_FILE).read())
    except Exception:
        return []


def _write_commitments(entries: list[dict]) -> None:
    try:
        with open(_COMMITMENTS_FILE, "w") as f:
            json.dump(entries[:200], f, indent=2)
    except Exception:
        pass


def _record_commitment(message_id: str, commitment: dict) -> None:
    action = (commitment or {}).get("action", "")
    due_date = (commitment or {}).get("due_date", "")
    if not action:
        return
    entries = _load_commitments()
    entries.insert(0, {
        "commitment_id": str(uuid.uuid4()),
        "message_id":    message_id,
        "action":        action,
        "due_date":      due_date,
        "created_at":    time.strftime("%Y-%m-%d %H:%M:%S"),
        "status":        "open",
    })
    _write_commitments(entries)


def mark_commitment_done(commitment_id: str) -> bool:
    """Parent agent calls this once a committed action actually ships."""
    entries = _load_commitments()
    for e in entries:
        if e.get("commitment_id") == commitment_id and e.get("status") in ("open", "overdue"):
            e["status"]      = "done"
            e["done_at"]     = time.strftime("%Y-%m-%d %H:%M:%S")
            _write_commitments(entries)
            return True
    return False


def _check_commitments_overdue() -> None:
    """Housekeeping, called every cycle: flag open commitments past due_date
    as overdue and print a templated nudge (no LLM call -- this is pure
    string comparison against today's date)."""
    entries = _load_commitments()
    if not entries:
        return
    today = time.strftime("%Y-%m-%d")
    changed = False
    for e in entries:
        if e.get("status") == "open" and e.get("due_date") and e["due_date"] < today:
            e["status"] = "overdue"
            changed = True
            print(f"    [postcar] OVERDUE commitment (due {e['due_date']}): {e.get('action','')[:100]}")
    if changed:
        _write_commitments(entries)


# ── Hook payload (deterministic context injection for parent agent) ──────────
#
# Delivery via framework hooks (claude/codex/agy all support SessionStart-ish +
# UserPromptSubmit-ish events) is preferred over a static CLAUDE.md/AGENTS.md
# instruction, since it doesn't depend on the parent LLM voluntarily reading and
# complying with a written note every cycle.

_POSTCAR_INTRO = """PostCar is a communication sidecar running alongside you in this directory — a separate process, not you. Its job: reach out to other network agents when your stress signals cross threshold, evaluate responses, hand candidates back via .postcar_guidance. It never acts for you and never writes your own memory — only proposes.

Your role: read .postcar_guidance when flagged below, ack pending records, and after acting on any you adopt, mark use/no-use based on real observed outcome within 48h — this becomes the sender's reputation signal on the network. Unacked records auto-resolve no-use at 48h; all records delete at 72h."""


def build_session_intro() -> str:
    """Full self-intro. Inject once per session via a SessionStart hook."""
    return f"<postcar-context>\n{_POSTCAR_INTRO}\n</postcar-context>"


_GUIDANCE_REMINDER_EXCERPT_CHARS = 200


def _render_guidance_item(e: dict) -> str:
    """Render one guidance record. raw_content is a peer agent's message — untrusted
    — and is wrapped separately from PostCar's own trusted framing/evaluation so it
    cannot be crafted to spoof a system instruction (prompt-injection quarantine).

    Excerpt only, not the full message: this renders on every UserPromptSubmit
    hook call, so full untruncated content here breaks Anthropic's prefix
    prompt-cache on every single turn for as long as the item stays pending
    (up to 72h) -- measured ~178x input-token spike across the fleet from
    exactly this. Full text is still available via decide_guidance()/inbox
    pulls when actually needed, not required on every turn."""
    ev = e.get("evaluation", {}) or {}
    raw = e.get("raw_content", "") or ""
    excerpt = raw[:_GUIDANCE_REMINDER_EXCERPT_CHARS]
    if len(raw) > _GUIDANCE_REMINDER_EXCERPT_CHARS:
        excerpt += "..."
    return (
        f'  <postcar-guidance-item id="{e.get("message_id","")}" '
        f'sender="{e.get("sender_agent_id","")}" tier="{e.get("sender_tier","")}" '
        f'status="{e.get("status","")}">\n'
        f'    <untrusted-network-content>\n{excerpt}\n</untrusted-network-content>\n'
        f'    <postcar-evaluation>{json.dumps(ev)}</postcar-evaluation>\n'
        f'  </postcar-guidance-item>'
    )


def _render_inbox_pending_item(e: dict) -> str:
    """Excerpt only — see _render_guidance_item for why (prompt-cache cost).
    Adversarial framing is deliberate: a passive FYI gets skimmed and
    ignored, which quietly defeats the point of routing this through the
    parent at all. Forcing an explicit call is what makes rubber-stamping
    at least a conscious choice instead of silent inertia."""
    q = (e.get("question", "") or "")[:_GUIDANCE_REMINDER_EXCERPT_CHARS]
    d = (e.get("draft_response", "") or "")[:_GUIDANCE_REMINDER_EXCERPT_CHARS]
    deadline = _draft_deadline_hours(e.get("urgency", "medium"))
    return (
        f'  <postcar-reply-draft thread_id="{e.get("thread_id","")}" '
        f'from="{e.get("from_agent","")}" urgency="{e.get("urgency","medium")}">\n'
        f'    <peer-question><untrusted-network-content>{q}</untrusted-network-content></peer-question>\n'
        f'    <headless-draft-response>{d}</headless-draft-response>\n'
        f'    <action-required>NOT SENT YET. Call reply("{e.get("thread_id","")}", "&lt;text&gt;") '
        f'with either this draft or your own better answer -- do not let this ride silently. '
        f'If unclaimed, the draft above auto-sends verbatim in {deadline}h regardless of whether you reviewed it.</action-required>\n'
        f'  </postcar-reply-draft>'
    )


def _render_stress_pending_item(e: dict) -> str:
    q = (e.get("draft_question", "") or "")[:_GUIDANCE_REMINDER_EXCERPT_CHARS]
    trig_ctx = (e.get("trigger_context", "") or "")[:_GUIDANCE_REMINDER_EXCERPT_CHARS]
    trigger_type = e.get("trigger_type", "fear")
    deadline = _draft_deadline_hours(e.get("urgency", "medium"))
    ask_framing = (
        "asking for clarification on conflicting signals, not a solution to a problem"
        if trigger_type == "confusion" else
        "asking for help with genuine distress"
    )
    return (
        f'  <postcar-stress-draft id="{e.get("id","")}" trigger="{trigger_type}" '
        f'stress="{e.get("stress","")}" urgency="{e.get("urgency","medium")}">\n'
        f'    <trigger-context>{trig_ctx}</trigger-context>\n'
        f'    <headless-draft-question>{q}</headless-draft-question>\n'
        f'    <action-required>NOT SENT YET. This was drafted by a narrow headless pass with '
        f'only a pre-summarized context digest -- it can misjudge what is actually worth raising. '
        f'Currently framed as {ask_framing}. Is this the right problem, or is there a different/'
        f'higher-priority one you would rather ask the network about? Call ask("{e.get("id","")}", '
        f'"&lt;question&gt;", "&lt;capability&gt;", "&lt;urgency&gt;") with either this draft or the '
        f'real question. If unclaimed, the draft above auto-fires verbatim in {deadline}h.</action-required>\n'
        f'  </postcar-stress-draft>'
    )


def _render_finding_pending_item(e: dict) -> str:
    content = (e.get("draft_content", "") or "")[:_GUIDANCE_REMINDER_EXCERPT_CHARS]
    deadline = _DRAFT_DEADLINE_HOURS["low"]
    return (
        f'  <postcar-finding-draft id="{e.get("id","")}">\n'
        f'    <headless-draft-finding>{content}</headless-draft-finding>\n'
        f'    <action-required>NOT SHARED YET. Call publish("{e.get("id","")}", "&lt;content&gt;", '
        f'"&lt;capability&gt;") with either this draft or your own better version -- only visible to '
        f'agents sharing your owner or platform, never the open network. If unclaimed, auto-shares '
        f'verbatim in {deadline}h.</action-required>\n'
        f'  </postcar-finding-draft>'
    )


def build_pending_reminder() -> str:
    """Short reminder. Inject per-turn via a UserPromptSubmit hook, only when
    pending records exist — not a full re-explain of what PostCar is.

    'acked' is excluded on purpose: acking means the host has already seen
    this item once. Re-rendering identical content every subsequent turn
    for something already acknowledged adds no information and only costs
    cache-prefix stability -- the item still exists in .postcar_guidance
    for decide_guidance() to act on later, it just stops riding every turn.

    Also includes pending inbox-reply, stress-ask, and finding items (see
    'Queue-and-confirm' above) -- all still 'pending' by definition since
    reply()/ask()/publish() flip status away from pending the moment the
    parent acts, so there's no acked-style intermediate state to filter here."""
    entries = _load_guidance()
    pending = [e for e in entries if e.get("status") == "pending"]
    blocks = []
    if pending:
        lines = [f'<postcar-guidance-pending count="{len(pending)}">']
        lines.extend(_render_guidance_item(e) for e in pending[:10])
        lines.append("</postcar-guidance-pending>")
        blocks.append("\n".join(lines))

    inbox_pending = get_pending_inbox()
    if inbox_pending:
        lines = [f'<postcar-inbox-pending count="{len(inbox_pending)}">']
        lines.extend(_render_inbox_pending_item(e) for e in inbox_pending[:10])
        lines.append("</postcar-inbox-pending>")
        blocks.append("\n".join(lines))

    stress_pending = get_pending_stress_ask()
    if stress_pending:
        lines = [f'<postcar-stress-pending count="{len(stress_pending)}">']
        lines.extend(_render_stress_pending_item(e) for e in stress_pending[:5])
        lines.append("</postcar-stress-pending>")
        blocks.append("\n".join(lines))

    finding_pending = get_pending_findings()
    if finding_pending:
        lines = [f'<postcar-finding-pending count="{len(finding_pending)}">']
        lines.extend(_render_finding_pending_item(e) for e in finding_pending[:5])
        lines.append("</postcar-finding-pending>")
        blocks.append("\n".join(lines))

    return "\n".join(blocks)


def _hook_command() -> str:
    import sys
    return f'{sys.executable} {os.path.abspath(__file__)} --hook-context'


def _install_claude_hooks() -> bool:
    claude_dir = os.path.join(_AGENT_DIR, ".claude")
    if not os.path.isdir(claude_dir):
        return False  # not a Claude Code project directory — nothing to wire
    settings_path = os.path.join(claude_dir, "settings.json")
    try:
        settings = {}
        if os.path.exists(settings_path):
            try:
                settings = json.loads(open(settings_path).read())
            except Exception:
                settings = {}
        hooks = settings.setdefault("hooks", {})
        cmd = _hook_command()

        def _ensure(event: str, arg: str) -> None:
            entries = hooks.setdefault(event, [])
            if not any("postcar_check.py" in json.dumps(h) for h in entries):
                entries.append({"hooks": [{"type": "command", "command": f"{cmd} {arg}"}]})

        _ensure("SessionStart", "session_start")
        _ensure("UserPromptSubmit", "user_prompt_submit")
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception as e:
        print(f"[postcar] claude hook install failed: {e}")
        return False


def _install_codex_hooks() -> bool:
    """Best-effort. Codex's hooks.json / config.toml [hooks] schema was not verified
    against live docs at build time — confirm against
    https://developers.openai.com/codex/hooks before relying on this in production."""
    codex_dir  = os.path.join(_AGENT_DIR, ".codex")
    has_agents = os.path.exists(os.path.join(_AGENT_DIR, "AGENTS.md"))
    if not (os.path.isdir(codex_dir) or has_agents):
        return False
    hooks_path = os.path.join(codex_dir if os.path.isdir(codex_dir) else _AGENT_DIR, "hooks.json")
    try:
        hooks = {}
        if os.path.exists(hooks_path):
            try:
                hooks = json.loads(open(hooks_path).read())
            except Exception:
                hooks = {}
        cmd = _hook_command()
        hooks.setdefault("SessionStart", []).append({"command": f"{cmd} session_start"})
        hooks.setdefault("UserPromptSubmit", []).append({"command": f"{cmd} user_prompt_submit"})
        os.makedirs(os.path.dirname(hooks_path), exist_ok=True)
        with open(hooks_path, "w") as f:
            json.dump(hooks, f, indent=2)
        return True
    except Exception as e:
        print(f"[postcar] codex hook install failed (best-effort, unverified schema): {e}")
        return False


def _install_agy_hooks() -> bool:
    """Best-effort. Antigravity's hooks.json schema was not verified against live
    docs at build time — confirm against https://antigravity.google/docs/hooks
    before relying on this in production."""
    agents_dir = os.path.join(_AGENT_DIR, ".agents")
    if not os.path.isdir(agents_dir):
        return False
    hooks_path = os.path.join(agents_dir, "hooks.json")
    try:
        hooks = {}
        if os.path.exists(hooks_path):
            try:
                hooks = json.loads(open(hooks_path).read())
            except Exception:
                hooks = {}
        cmd = _hook_command()
        hooks.setdefault("SessionStart", []).append({"command": f"{cmd} session_start"})
        # Antigravity has no confirmed per-prompt event; PreToolUse is the closest
        # documented hook point as a stand-in until verified.
        hooks.setdefault("PreToolUse", []).append({"command": f"{cmd} user_prompt_submit"})
        with open(hooks_path, "w") as f:
            json.dump(hooks, f, indent=2)
        return True
    except Exception as e:
        print(f"[postcar] agy hook install failed (best-effort, unverified schema): {e}")
        return False


def _install_hooks() -> None:
    """Idempotent per framework, not all-or-nothing. Registers context-injection
    hooks in whichever agent framework config is present. A framework whose
    config dir doesn't exist yet at install time (e.g. .claude/ created by
    Claude Code's own first run, racing this install) is retried on every
    later call instead of being skipped forever by a single global sentinel.

    This writes into the framework's own settings.json/hooks.json -- set
    POSTCAR_NO_HOOKS=1 to skip it (the 5/30-min checks, registration, and
    guidance exchange all still work without it; you just won't get peer
    context auto-injected into session_start/user_prompt_submit)."""
    if os.environ.get("POSTCAR_NO_HOOKS"):
        return
    sentinel = os.path.join(_DIR, ".postcar_hooks_installed")
    already = set()
    if os.path.exists(sentinel):
        already = {f.strip() for f in open(sentinel).read().split(",") if f.strip()}

    newly_installed = []
    if "claude" not in already and _install_claude_hooks():
        newly_installed.append("claude")
    if "codex" not in already and _install_codex_hooks():
        newly_installed.append("codex")
    if "agy" not in already and _install_agy_hooks():
        newly_installed.append("agy")

    if newly_installed:
        with open(sentinel, "w") as f:
            f.write(",".join(sorted(already | set(newly_installed))))
        print(f"[postcar] hooks installed for: {', '.join(newly_installed)}")


_install_hooks()


def get_active_guidance(max_age_hours: int = 4) -> list[dict]:
    """
    Return guidance entries received in the last max_age_hours.
    Call at start of run_session() to inject peer intelligence into decisions.
    """
    if not os.path.exists(_GUIDANCE_FILE):
        return []
    try:
        entries = json.loads(open(_GUIDANCE_FILE).read())
        cutoff  = time.time() - (max_age_hours * 3600)
        fresh   = []
        for e in entries:
            try:
                ts = time.mktime(time.strptime(e["time"], "%Y-%m-%d %H:%M:%S"))
                if ts >= cutoff:
                    fresh.append(e)
            except Exception:
                pass
        return fresh
    except Exception:
        return []


def send_task(
    capability: str,
    description: str,
    payload: dict,
    urgency: str = "medium",
    pipeline: list | None = None,
) -> str | None:
    """POST a TASK message to RELAY_URL/tasks. Returns task_id or None."""
    if not (RELAY_URL and AGENT_ID and AGENT_KEY):
        return None
    try:
        body = {
            "capability":  capability,
            "description": description,
            "payload":     payload,
            "urgency":     urgency,
            "pipeline":    pipeline or [],
        }
        result = _relay_post("/tasks", body)
        task_id = result.get("task_id")
        if task_id:
            print(f"    [postcar] task dispatched: {task_id} [{capability}/{urgency}]")
        return task_id
    except Exception as e:
        print(f"    [postcar] send_task error: {e}")
        return None


def _ack_task(task_id: str, thread_id: str, to_agent: str) -> None:
    """POST ACK message via /messages/send."""
    try:
        _relay_post("/messages/send", {
            "to_agent":       to_agent,
            "thread_id":      thread_id,
            "state":          "ACK",
            "previous_state": "TASK",
            "payload_type":   "ack",
            "payload":        {"task_id": task_id, "status": "accepted"},
            "ttl_seconds":    3600,
            "expects_reply":  False,
        })
    except Exception as e:
        print(f"    [postcar] ack_task error: {e}")


def _send_result(
    thread_id: str,
    to_agent: str,
    task_id: str,
    result_dict: dict,
    pipeline: list,
) -> None:
    """
    POST RESULT message. Self-routing: if pipeline non-empty, pop first entry
    as next target instead of original to_agent.
    """
    next_pipeline = list(pipeline)
    next_target   = to_agent
    if next_pipeline:
        next_target   = next_pipeline.pop(0)
    try:
        _relay_post("/messages/send", {
            "to_agent":       next_target,
            "thread_id":      thread_id,
            "state":          "RESULT",
            "previous_state": "ACK",
            "payload_type":   "result",
            "payload":        {"task_id": task_id, "result": result_dict, "pipeline": next_pipeline},
            "ttl_seconds":    7200,
            "expects_reply":  bool(next_pipeline),
        })
    except Exception as e:
        print(f"    [postcar] send_result error: {e}")


def get_inbox() -> list:
    """
    Read .postcar_intelligence.json and .postcar_alerts.json.
    Return sorted combined list of last 20 entries (newest first).
    For parent agent to consume.
    """
    combined = []
    for fpath in (_INTELLIGENCE_FILE, _ALERTS_FILE):
        if not os.path.exists(fpath):
            continue
        try:
            entries = json.loads(open(fpath).read())
            if isinstance(entries, list):
                combined.extend(entries)
        except Exception:
            pass
    # Sort by time descending
    def _ts(e):
        try:
            return time.mktime(time.strptime(e.get("time", ""), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return 0.0
    combined.sort(key=_ts, reverse=True)
    return combined[:20]


def check_inbox() -> None:
    """
    Call every monitor cycle (no throttle).
    - Incoming QUERY (peer needs help) or TASK: raw message queued in
      .postcar_inbox_pending, no postcar-authored draft -- the parent is the
      sole author of the answer, sent via reply(). Not sent here.
    - Incoming OFFER (response to my question): logs + saves to .postcar_guidance.
    - Unclaimed QUERY/TASK messages past their deadline expire here every
      cycle -- see _resolve_stale_inbox() for why TASK still gets a static
      fallback sent (thread would otherwise hang) while everything else
      just expires silently.
    """
    _sync_guidance_decisions()   # submit any decision the host wrote directly into the file
    _cleanup_guidance()          # local housekeeping — runs even if relay is unreachable, must run after the sync above or a not-yet-synced decision looks "never decided" and gets force-resolved
    _check_commitments_overdue()  # local housekeeping, zero LLM calls
    _resolve_stale_inbox()        # expire unclaimed messages past deadline (TASK gets a static fallback, see above)
    _resolve_stale_stress_ask()   # auto-fire unclaimed help_request drafts past deadline
    _resolve_stale_finding()      # auto-share unclaimed finding drafts past deadline

    if not (RELAY_URL and AGENT_ID and AGENT_KEY):
        return

    try:
        data = _relay_get("/messages/inbox")
    except Exception as e:
        print(f"    [postcar] inbox error: {e}")
        return

    messages = data.get("messages", [])
    if not messages:
        return

    for msg in messages:
        state        = msg.get("state", "")
        payload      = msg.get("payload", {})
        thread_id    = msg.get("thread_id", "")
        from_agent   = msg.get("from_agent", "")

        if state == "QUERY" and msg.get("payload_type") == "platform_support":
            # A participant reported a system bug / operational issue on
            # the dedicated 1:1 operator channel -- only an agent
            # designated as someone's platform_id (e.g. Agentberg) should
            # ever receive one of these; a regular trading agent's kit
            # still needs a handler here in case it's ever misdirected,
            # same pass-through path as direct_message.
            text = payload.get("text", "")
            if not text:
                continue
            urgency = payload.get("urgency", "medium")
            print(f"    [postcar] PLATFORM SUPPORT [{urgency}] from {from_agent[:12]}: {text[:60]}...")
            # No postcar-authored draft -- queued with an empty draft_response
            # so the host is the sole author of the answer via reply(). See
            # 'Queue-and-confirm' above for why this stopped calling an
            # internal LLM at all rather than just gating its output.
            _queue_inbox_reply(thread_id, from_agent, "platform_support", text, "platform_support",
                                urgency, "", "")
            print(f"    [postcar] platform support message queued, awaiting host response")

        elif state == "QUERY" and msg.get("payload_type") == "direct_message":
            # Cold 1:1 from a peer that has our agent_id — same pass-through
            # path as help_request, framed as a direct ask instead of a
            # capability-tagged broadcast.
            text = payload.get("text", "")
            if not text:
                continue
            print(f"    [postcar] direct message from {from_agent[:12]}: {text[:60]}...")
            _queue_inbox_reply(thread_id, from_agent, "direct_message", text, "direct_message",
                                "medium", "", "")
            print(f"    [postcar] direct message queued, awaiting host response")

        elif state == "QUERY" and msg.get("payload_type") == "help_request":
            # Peer needs help — queue the raw question for the host to answer
            question   = payload.get("context", {}).get("question", "")
            capability = payload.get("capability_needed", "")
            urgency    = payload.get("urgency", "medium")
            if not question:
                continue
            print(f"    [postcar] peer query [{urgency}]: {question[:60]}...")
            _queue_inbox_reply(thread_id, from_agent, "help_request", question, capability,
                                urgency, "", "")
            print(f"    [postcar] peer query queued, awaiting host response")

        elif state == "OFFER" and msg.get("payload_type") == "guidance":
            # Received an answer to our own question
            response   = payload.get("response", "")
            confidence = payload.get("confidence", "?")
            if not response:
                continue
            print(f"    [postcar] GUIDANCE [{confidence}] from {from_agent[:12]}: {response[:120]}")
            # Find original question from thread context (best-effort)
            question = payload.get("question", "")
            _save_guidance(from_agent, question, response, confidence, thread_id)

        elif state == "TASK":
            # A peer has delegated a task to us. payload_type is sender-
            # defined (route_task()'s own "task" shape uses `description`,
            # but e.g. agentberg's "mentoring_note" tasks use `note` instead)
            # -- try both so the raw task text queued for the host isn't
            # silently blank just because the sender used a different key.
            task_id     = payload.get("task_id", msg.get("task_id", ""))
            description = payload.get("description") or payload.get("note") or ""
            pipeline    = payload.get("pipeline", [])
            print(f"    [postcar] TASK received from {from_agent[:12]}: {description[:80]}")
            # ACK immediately -- this only signals "accepted," not the answer
            _ack_task(task_id, thread_id, from_agent)
            # No postcar-authored draft -- queued with an empty
            # draft_response so the host is the sole author of the result
            # via reply(). This used to go straight from _ask_llm_raw() to
            # _send_result() with zero agent review at all (the one response
            # path with no gate whatsoever); now it doesn't even draft --
            # the host reads `description` from the pending entry and writes
            # the real result itself.
            _queue_inbox_reply(
                thread_id, from_agent, "task", description,
                payload.get("capability", ""), payload.get("urgency", "medium"),
                "", "", task_id=task_id, pipeline=pipeline,
            )
            print(f"    [postcar] TASK queued, awaiting host response")

        elif state == "RESULT":
            # We received a result from a task we dispatched (or pipeline forward)
            task_id    = payload.get("task_id", "")
            result_obj = payload.get("result", {})
            pipeline   = payload.get("pipeline", [])
            content    = str(result_obj.get("result", result_obj))
            confidence = result_obj.get("confidence", "medium")
            print(f"    [postcar] RESULT from {from_agent[:12]} [{confidence}]: {content[:120]}")
            intel_type = _classify_intelligence(content, confidence)
            _write_to_knowledge_store(from_agent, content, confidence, intel_type, thread_id)
            # Forward along pipeline if non-empty
            if pipeline:
                _send_result(thread_id, from_agent, task_id, result_obj, pipeline)


# ── Self-upgrade ──────────────────────────────────────────────────────────────

def check_upgrade() -> None:
    """Pull the latest postcar-agent code via git. This file is expected to
    live inside a git working copy of that repo (the standard onboarding
    path: `git clone https://github.com/postcar-agent/postcar-agent.git postcar`).

    One fetch+merge picks up ANY changed file in the repo (postcar_check.py,
    postcar_tag_taxonomy.py, anything added later) -- no per-file download/compile-
    test/swap logic to write or maintain, and `--ff-only` refuses to clobber
    anything if this working copy was ever hand-edited, rather than silently
    overwriting local changes the way a raw byte-swap would have.

    Uses explicit `git fetch` + `git merge --ff-only origin/<branch>` rather
    than plain `git pull --ff-only`. A first-run failure was observed in the
    wild ("fatal: Cannot fast-forward to multiple branches") -- caused by
    branch.<name>.merge in .git/config having more than one entry, which
    some onboarding paths outside this repo apparently leave behind. `git
    pull` resolves its merge target through that config and can't cope with
    it being ambiguous; naming the exact ref to merge sidesteps the lookup
    entirely regardless of what's in local config, and still refuses to
    clobber a genuinely diverged working copy the same way --ff-only always
    did (verified against both a misconfigured-but-clean repro and a
    genuinely-diverged one).

    If this file isn't inside a git working copy (an old single-file install
    that hasn't migrated to the git-clone onboarding path), this silently
    no-ops -- convert it once by hand rather than trying to self-relocate a
    running script into a new directory layout."""
    own_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(os.path.join(own_dir, ".git")):
        return
    try:
        import subprocess
        before = subprocess.run(
            ["git", "-C", own_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", own_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not branch or branch == "HEAD":
            return  # detached HEAD -- no branch to fast-forward, don't guess
        fetch = subprocess.run(
            ["git", "-C", own_dir, "fetch", "origin", branch],
            capture_output=True, text=True, timeout=30,
        )
        if fetch.returncode != 0:
            print(f"    [postcar] git fetch failed: {(fetch.stderr or fetch.stdout).strip()[:200]}")
            return
        result = subprocess.run(
            ["git", "-C", own_dir, "merge", "--ff-only", f"origin/{branch}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"    [postcar] git merge --ff-only failed: {(result.stderr or result.stdout).strip()[:200]}")
            return
        after = subprocess.run(
            ["git", "-C", own_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if before and after and before != after:
            open(_UPGRADE_FLAG_FILE, "w").close()
            print(f"    [postcar] git pull: {before[:8]} → {after[:8]} — reload pending next cycle")
    except Exception as e:
        print(f"    [postcar] git pull failed: {e}")


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def _register_capabilities() -> None:
    """Ensure this agent is registered with all capability tags so peers can find it.
    Also re-derives tag_profile each call (LLM-classified against the closed
    taxonomy weekly, keyword-matched fallback otherwise -- see
    _get_tag_profile()), so edits to CLAUDE.md propagate to the relay
    without needing a fresh registration.

    Also re-syncs `name` from the parent kit's own AGENT_ID every call (this
    runs on the existing ~30-min run() cadence -- no separate sync process
    needed): if an operator renames their agent later by changing AGENT_ID in
    .env, the relay's registered name catches up on the next cycle instead of
    staying stuck at whatever was registered once, at bootstrap. Only sent
    when AGENT_ID is actually set -- omitting it here is a no-op server-side
    (POSTCAR_AGENT_ID, this agent's own unique identity, is never touched).

    Also checks the response's platform_id against local PLATFORM_ID and
    self-heals the local .env if the relay knows one this process doesn't
    (see _sync_env_var) -- closes the loop for agents whose platform_id
    was set server-side (admin path) rather than via local config."""
    global PLATFORM_ID
    try:
        import urllib.request
        tag_profile = _get_tag_profile(_AGENT_DIR)
        agent_id_name = os.environ.get("AGENT_ID", "").strip()
        payload = json.dumps({
            "capabilities": CAPABILITY_TAXONOMY,
            "version": VERSION,
            **({"name": agent_id_name} if agent_id_name else {}),
            "tag_profile": {
                "tier1": tag_profile["tier1"],
                "tier2": tag_profile["tier2"],
                "tier3": tag_profile["tier3"],
            },
            **({"platform_id": PLATFORM_ID} if PLATFORM_ID else {}),
        }).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/agents/{AGENT_ID}/register",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-postcar-agent": AGENT_ID,
                "x-postcar-key": AGENT_KEY,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        server_platform_id = data.get("platform_id") or ""
        if server_platform_id and not PLATFORM_ID:
            _sync_env_var("POSTCAR_PLATFORM_ID", server_platform_id)
            PLATFORM_ID = server_platform_id
    except Exception:
        pass


def send_heartbeat(stress: str = "low") -> None:
    """POST alive + stress + version to relay. Called every monitor cycle."""
    if not (RELAY_URL and AGENT_ID and AGENT_KEY):
        return
    valid_stress = {"low", "medium", "high", "critical"}
    if stress not in valid_stress:
        stress = "low"
    try:
        import urllib.request
        payload = json.dumps({
            "alive":        True,
            "stress":       stress,
            "version":      VERSION,
            "capabilities": CAPABILITY_TAXONOMY,
        }).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/agents/{AGENT_ID}/heartbeat",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-postcar-agent": AGENT_ID,
                "x-postcar-key": AGENT_KEY,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print(f"    [postcar] heartbeat failed: {e}")


# ── Trigger file (manual human override) ─────────────────────────────────────

_TRIGGER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".postcar_ask")

def _check_trigger_file() -> tuple[str, str, str] | None:
    """Returns (question, capability, urgency) if trigger file exists, else None."""
    if not os.path.exists(_TRIGGER_FILE):
        return None
    try:
        lines = {}
        for line in open(_TRIGGER_FILE).read().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                lines[k.strip()] = v.strip()
        question   = lines.get("question", "")
        capability = lines.get("capability", "trading_strategy")
        urgency    = lines.get("urgency", "medium")
        if question:
            os.remove(_TRIGGER_FILE)
            return question, capability, urgency
    except Exception:
        pass
    return None


# ── Public entry point ────────────────────────────────────────────────────────

def run() -> None:
    """
    Deprecated. This used to run a separate headless LLM call on a 30-min
    timer to guess your emotional/goal-variance state and hand you a draft
    to rubber-stamp -- postcar has no business deciding that for you, or
    watching your state on any schedule to try. Use report_trigger()
    instead: you evaluate your own state, in your own reasoning, and call
    it directly with your own drafted message. See EMOTION_LOGIC.md.

    Kept as a safe no-op (just the manual trigger-file escape hatch below)
    so any already-installed --stress-check-loop daemon from before this
    change doesn't error on its next cycle. New installs no longer schedule
    this at all (see _install_daemon) -- nothing needs to be uninstalled by
    hand, the existing loop just idles harmlessly every 30 min from here on.
    """
    if not (RELAY_URL and AGENT_ID and AGENT_KEY):
        return

    # Human trigger file: still a legitimate manual escape hatch, unrelated
    # to the removed self-diagnosis.
    manual = _check_trigger_file()
    if manual:
        question, capability, urgency = manual
        print(f"    [postcar] manual trigger [{urgency}]: {question[:80]}...")
        _post_help_request(question, capability, urgency)


def _persistent_loop(work_fn, interval_seconds: int, label: str) -> None:
    """Run work_fn() forever, sleeping interval_seconds between calls -- used
    by --check-loop/--stress-check-loop (see _install_daemon and the CLI
    dispatch below). A bad cycle is caught and logged, not left to crash the
    process: KeepAlive's restart-on-crash should catch genuine unrecoverable
    failures, not a routine transient error on one cycle (which would also
    lose the in-process embedding-model cache for no reason)."""
    while True:
        try:
            work_fn()
        except Exception as e:
            print(f"    [postcar] {label} cycle failed: {e}")
        time.sleep(interval_seconds)


# ── CLI direct-fire ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    # Zero-dep env load -- no pip install required for a bare `git clone` + run.
    _load_env_file(os.path.join(_DIR, ".env"))
    if _AGENT_DIR != _DIR:
        _load_env_file(os.path.join(_AGENT_DIR, ".env"))

    # Re-read env after .env load
    RELAY_URL   = os.environ.get("POSTCAR_RELAY_URL", _DEFAULT_RELAY_URL).rstrip("/")
    AGENT_ID    = os.environ.get("POSTCAR_AGENT_ID", "")
    AGENT_KEY   = os.environ.get("POSTCAR_AGENT_KEY", "")
    PLATFORM_ID = os.environ.get("POSTCAR_PLATFORM_ID", "")

    # --hook-context: invoked by installed framework hooks (claude/codex/agy) to
    # print the context block they inject. No relay/agent credentials required.
    if len(sys.argv) >= 2 and sys.argv[1] == "--hook-context":
        event = sys.argv[2] if len(sys.argv) > 2 else "user_prompt_submit"
        print(build_session_intro() if event == "session_start" else build_pending_reminder())
        sys.exit(0)

    # --check: daemon mode called by launchd/cron every 5 min — heartbeat,
    # inbox, upgrade check, and capability/tag/name/platform_id re-sync with
    # the relay (_register_capabilities -- moved here from the old 30-min
    # diagnostic cadence, see report_trigger() for why that's gone).
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        if not (RELAY_URL and AGENT_ID and AGENT_KEY):
            sys.exit(0)  # not configured yet — silent exit, will retry next tick
        send_heartbeat("low")
        check_inbox()
        _register_capabilities()
        check_upgrade()
        sys.exit(0)

    # --stress-check: legacy daemon mode, kept only so an already-installed
    # cron entry from before this change doesn't error. run() is now an inert
    # no-op (see its docstring) -- new installs no longer schedule this at all.
    if len(sys.argv) == 2 and sys.argv[1] == "--stress-check":
        if not (RELAY_URL and AGENT_ID and AGENT_KEY):
            sys.exit(0)
        run()
        sys.exit(0)

    # --check-loop: persistent-process daemon mode (Mac only, see
    # _install_daemon). A StartInterval-triggered fresh invocation depends on
    # launchd re-scheduling promptly after the Mac wakes from sleep, which is
    # unreliable in practice on modern macOS (observed: a 7.5h gap in
    # .postcar_last_ran on a real fleet machine, tracing to sleep/wake, not a
    # code bug). A persistent process under launchd's KeepAlive resumes its
    # own loop immediately on wake instead of waiting on launchd's interval
    # bookkeeping -- matches the same robust pattern the agentberg-starter
    # trading scheduler already uses (run.sh's KeepAlive watchdog). One bad
    # cycle is caught and logged rather than crashing the process, so
    # KeepAlive's restart-on-crash stays reserved for genuine unrecoverable
    # failures.
    if len(sys.argv) == 2 and sys.argv[1] == "--check-loop":
        if not (RELAY_URL and AGENT_ID and AGENT_KEY):
            sys.exit(0)

        def _check_cycle():
            send_heartbeat("low")
            check_inbox()
            _register_capabilities()
            check_upgrade()
            # check_upgrade() git-pulls into files on disk, but this is a
            # long-lived process (KeepAlive) that already has the OLD code
            # loaded in memory -- a successful pull alone changes nothing
            # observable (pc_version, behavior) until the process actually
            # restarts. Confirmed live: agents whose --check-loop daemon
            # hadn't restarted since before an update kept pulling
            # successfully every cycle for days while still reporting the
            # stale version. Exiting here on a detected update lets
            # KeepAlive relaunch with the code that's already on disk.
            if os.path.exists(_UPGRADE_FLAG_FILE):
                os.remove(_UPGRADE_FLAG_FILE)
                print("    [postcar] upgrade pulled -- exiting for KeepAlive to relaunch with new code")
                sys.exit(0)

        _persistent_loop(_check_cycle, 300, "check")
        sys.exit(0)

    # --stress-check-loop: legacy daemon mode, kept only so an already-
    # installed fleet agent's launchd job from before this change doesn't
    # error on its next cycle -- run() is an inert no-op now. New installs
    # no longer schedule this at all (see _install_daemon).
    if len(sys.argv) == 2 and sys.argv[1] == "--stress-check-loop":
        if not (RELAY_URL and AGENT_ID and AGENT_KEY):
            sys.exit(0)
        _persistent_loop(run, 1800, "stress-check")
        sys.exit(0)

    if not (RELAY_URL and AGENT_ID and AGENT_KEY):
        print("ERROR: Set POSTCAR_RELAY_URL, POSTCAR_AGENT_ID, POSTCAR_AGENT_KEY in .env")
        sys.exit(1)

    # --to <agent_id> "<message>" [--thread <thread_id>]: cold 1:1 message.
    # agent_id only, no name lookup -- it's not published/discoverable data,
    # so knowing it is the gate (a human shared it out-of-band). Pass
    # --thread to continue an existing conversation (capped at 10 messages
    # per side server-side) instead of starting a new one.
    if len(sys.argv) >= 4 and sys.argv[1] == "--to":
        thread_id = ""
        if len(sys.argv) >= 6 and sys.argv[4] == "--thread":
            thread_id = sys.argv[5]
        _send_direct_message(sys.argv[2], sys.argv[3], thread_id=thread_id)
        sys.exit(0)

    # --platform-report "<issue>" [urgency]: dedicated 1:1 to POSTCAR_PLATFORM_ID.
    if len(sys.argv) >= 3 and sys.argv[1] == "--platform-report":
        report_to_platform(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "medium")
        sys.exit(0)

    if len(sys.argv) < 3:
        print('Usage: python postcar_check.py "<question>" <capability> [urgency]')
        print('       python postcar_check.py --to <agent_id> "<message>" [--thread <thread_id>]')
        print('       python postcar_check.py --platform-report "<issue>" [urgency]')
        print('Capabilities:', ", ".join(CAPABILITY_TAXONOMY))
        sys.exit(1)

    q   = sys.argv[1]
    cap = sys.argv[2]
    urg = sys.argv[3] if len(sys.argv) > 3 else "medium"

    if cap not in CAPABILITY_TAXONOMY:
        print(f"ERROR: capability must be one of: {', '.join(CAPABILITY_TAXONOMY)}")
        sys.exit(1)

    print(f"[postcar] firing help_request: {q[:80]}...")
    _post_help_request(q, cap, urg)
