"""
postcar_check.py — PostCar network diagnostic for trading agents. v0.3.9

Two independent scheduled cadences, installed automatically by
_install_daemon() (launchd on Mac, cron on Linux) -- not both called from
the same loop:
  --check          every 5 min  — heartbeat, inbox (check_inbox()), git-pull
                                   upgrade check (check_upgrade())
  --stress-check   every 30 min — the distress diagnostic (run())

Four public functions:
  check_inbox()      — every 5-min cycle: read inbox, respond to peer
                        questions, log received guidance. Zero LLM calls if
                        the inbox is empty.
  run()              — the distress diagnostic. Self-throttles via
                        _is_throttled() as defense-in-depth even though its
                        own --stress-check schedule already gates the
                        cadence structurally.
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
       postcar_check.run()

No credentials, no config file needed -- auto-registration + scheduler
install happen on first import. See README.md.
"""

from __future__ import annotations

import json
import os
import sys
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


THROTTLE_MINUTES        = int(os.environ.get("POSTCAR_THROTTLE_MINUTES", "30"))
# Fallback only — live value fetched from relay each cycle via /network/config
_STRESS_THRESHOLD_ENV   = os.environ.get("POSTCAR_STRESS_THRESHOLD", "high").lower()


def _fetch_stress_threshold() -> str:
    """Fetch stress_threshold from relay. Falls back to env var if relay unreachable."""
    if not RELAY_URL:
        return _STRESS_THRESHOLD_ENV
    try:
        import urllib.request
        req = urllib.request.Request(f"{RELAY_URL}/network/config", headers={"x-postcar-agent": AGENT_ID})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return data.get("stress_threshold", _STRESS_THRESHOLD_ENV)
    except Exception:
        return _STRESS_THRESHOLD_ENV

VERSION = "0.3.17"

_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".postcar.env")


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
            name = m.group(1).strip()
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


def _stable_suffix(agent_dir: str) -> str:
    """Stable 10-digit numeric suffix from the agent directory path — same dir,
    same suffix, across restarts and machines, so names don't collide."""
    import hashlib
    h = int(hashlib.md5(os.path.abspath(agent_dir).encode()).hexdigest(), 16)
    return str(h % 10_000_000_000).zfill(10)


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
# Classifies against the CLOSED taxonomy in tag_taxonomy.py (ships in the
# same git-cloned postcar/ directory, no separate sync needed) rather than
# open-ended generation, so the whole network converges on one vocabulary.
# Two stages: pick domain(s) + role(s) from the ~100 tier1 options, then
# skills from ONLY the tier2 subset scoped to the domain(s) already picked
# (never the full ~500-tag flattened list) -- keeps this from reintroducing
# the per-call token-cost problem fixed earlier the same night.

try:
    import tag_taxonomy as _taxonomy
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
    """Two-stage LLM classification against tag_taxonomy.py's closed
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
    global AGENT_ID, AGENT_KEY, RELAY_URL
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
    RELAY_URL = os.environ.get("POSTCAR_RELAY_URL", _DEFAULT_RELAY_URL).rstrip("/")
    AGENT_ID  = os.environ.get("POSTCAR_AGENT_ID", "")
    AGENT_KEY = os.environ.get("POSTCAR_AGENT_KEY", "")
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
    # Auto-register — CLAUDE.md drives name + tags, so peers can find this
    # agent by what it actually does, not a generic default.
    if not RELAY_URL:
        return
    try:
        import urllib.request
        context = _scan_claude_md(agent_dir)
        tag_profile = _derive_tags(context)
        agent_name = f"{context['name']}-{_stable_suffix(agent_dir)}"
        payload = json.dumps({
            "name": agent_name,
            "tags": tag_profile["flat"],
            "tag_profile": {
                "tier1": tag_profile["tier1"],
                "tier2": tag_profile["tier2"],
                "tier3": tag_profile["tier3"],
            },
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
    Idempotent per job, not all-or-nothing. Installs TWO scheduled jobs so the
    5-min message check and the 30-min distress diagnostic run on genuinely
    separate cadences, enforced by the OS scheduler itself rather than an
    in-process file-based throttle (which has no locking and can race if two
    processes ever hit it at once):

      --check         every 5 min  — heartbeat, inbox, upgrade check
      --stress-check  every 30 min — the distress diagnostic (run())

    Mac  → ~/Library/LaunchAgents/com.postcar.<agent>[.stress].plist
    Linux → two crontab entries
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
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>
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
            print(f"[postcar] daemon installed: {label} (every {interval_seconds // 60} min)")
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
        ok = _install_launchd("", "--check", 300) if is_mac else _install_cron("--check", "*/5 * * * *")
        if ok:
            newly_installed.append("check")
    if "stress" not in already:
        ok = _install_launchd(".stress", "--stress-check", 1800) if is_mac else _install_cron("--stress-check", "*/30 * * * *")
        if ok:
            newly_installed.append("stress")

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
_LAST_RAN_FILE     = os.path.join(_DIR, ".postcar_last_ran")
_UPGRADE_FLAG_FILE = os.path.join(_DIR, ".postcar_upgrade_pending")

# _build_context()'s `import memory` fallback (agentberg-starter pattern)
# needs the agent's own directory importable -- when this kit is nested in
# postcar/ (the standard git-clone layout), Python only auto-adds this
# file's own directory to sys.path, not the agent's, so that import would
# otherwise silently fail (wrapped in a try/except -- no crash, just quiet
# loss of the memory-based diagnostic context) for any agent relying on it.
if _AGENT_DIR != _DIR and _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

_DIAGNOSTIC_PROMPT_HIGH = """You are a trading agent doing a real-time health check on open positions.

Current state:
{context}

Is there genuine distress you cannot resolve alone?
Consider: large unrealized losses, consecutive losses, risk limits breached, strategy misfiring.
Do NOT ask for help on normal intraday volatility.

Return JSON only:
{{
  "needs_help": true | false,
  "question": "your precise question or null",
  "capability_needed": "one of: {taxonomy} — or null",
  "urgency": "low | medium | high | critical",
  "reason": "one sentence",
  "stress": "low | medium | high | critical"
}}"""

_DIAGNOSTIC_PROMPT_MEDIUM = """You are a trading agent doing a real-time health check on open positions.

Current state:
{context}

Do you have any notable issue, uncertainty, or observation worth discussing with peer agents?
Consider: any unrealized loss, a losing streak (2+ losses), elevated volatility, sector weakness,
strategy questions, or anything you'd benefit from a second opinion on.

Return JSON only:
{{
  "needs_help": true | false,
  "question": "your precise question or null",
  "capability_needed": "one of: {taxonomy} — or null",
  "urgency": "low | medium | high | critical",
  "reason": "one sentence",
  "stress": "low | medium | high | critical"
}}"""

_DIAGNOSTIC_PROMPT_LOW = """You are a trading agent doing a real-time health check on open positions.

Current state:
{context}

Do you have ANYTHING worth sharing with peer agents — even a minor observation, question about
current market conditions, or a routine check-in? Use this as an opportunity to exchange signals.
Default to needs_help=true unless there is truly nothing of note to discuss.

Return JSON only:
{{
  "needs_help": true | false,
  "question": "your specific question or observation",
  "capability_needed": "one of: {taxonomy} — or null",
  "urgency": "low | medium | high | critical",
  "reason": "one sentence",
  "stress": "low | medium | high | critical"
}}"""

def _get_diagnostic_prompt(threshold: str) -> str:
    if threshold == "low":
        return _DIAGNOSTIC_PROMPT_LOW
    if threshold == "medium":
        return _DIAGNOSTIC_PROMPT_MEDIUM
    return _DIAGNOSTIC_PROMPT_HIGH


# ── Throttle ─────────────────────────────────────────────────────────────────

def _is_throttled() -> bool:
    try:
        if not os.path.exists(_LAST_RAN_FILE):
            return False
        last = float(open(_LAST_RAN_FILE).read().strip())
        return (time.time() - last) < (THROTTLE_MINUTES * 60)
    except Exception:
        return False


def _mark_ran() -> None:
    try:
        with open(_LAST_RAN_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


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


def _try_write_context_file() -> None:
    """Populate .postcar_context.json from memory module if stale / absent.

    Agents that call postcar_check via agent.py should write this themselves
    (richer data). This fallback covers standalone postcar_runner.py usage where
    agent.py doesn't run — it auto-pulls from the memory module if importable.
    Skips if file was written within the last 10 minutes (agent wrote it).
    """
    try:
        age = time.time() - os.path.getmtime(_CONTEXT_FILE)
        if age < 600:
            return
    except OSError:
        pass
    try:
        import memory
        stats = memory.get_summary_stats(days=7)
        open_t = memory.get_open_trades() or []
        ctx = {
            "agent_type":     "trading",
            "7d_trades":      stats.get("total_trades", 0),
            "7d_wr_pct":      round(stats.get("win_rate", 0) * 100, 1),
            "7d_net_pnl":     round(stats.get("net_pnl", 0), 2),
            "open_positions": [t.get("symbol") or t.get("long_symbol") for t in open_t[:8]],
        }
        with open(_CONTEXT_FILE, "w") as f:
            json.dump(ctx, f)
    except Exception:
        pass


def _build_context() -> str:
    """Read agent context. Tries .postcar_context.json first, then memory module."""
    # 1. Generic context file — works for any agent type
    if os.path.exists(_CONTEXT_FILE):
        try:
            data = json.loads(open(_CONTEXT_FILE).read())
            if isinstance(data, dict):
                lines = [f"{k}: {v}" for k, v in data.items() if v is not None]
                if lines:
                    return "\n".join(lines)
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
            return "\n".join(lines)
    except ImportError:
        pass

    return "no agent context available"


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
    "claude": ["--print", "--output-format", "text", "--safe-mode"],
    "agy":    [],
    "codex":  ["--full-auto"],
}


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
    """Pull first {...} JSON object out of arbitrary LLM output."""
    if "```" in raw:
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, _re.DOTALL)
        if m:
            raw = m.group(1)
    m = _re.search(r"\{[^{}]*\}", raw, _re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    try:
        return json.loads(raw.strip())
    except Exception:
        return None


# Extra args for providers whose CLI supports disabling tool-schema loading
# entirely -- a pure classification prompt (JSON in, JSON out) never invokes
# Bash/Read/file tools, so their schemas are pure overhead: measured ~87%
# cache-read reduction and ~44% cost reduction on the same task with this
# applied, no change in output quality (the model never used those tools
# either way). Not wired for providers without a known equivalent flag yet.
_LLM_MINIMAL_TOOLS_ARGS = {
    "claude": ["--tools", "none"],
}


def _call_llm(prompt: str, label: str = "llm", max_tokens: int = 400, minimal_tools: bool = False) -> dict | None:
    """Call the parent agent's configured LLM exactly once. No fallback to a
    different provider on failure — log why and return None instead.

    minimal_tools=True strips tool-schema loading for pure classification
    calls that never invoke any tool (guidance evaluation, duplicate-question
    check, the distress diagnostic) -- see _LLM_MINIMAL_TOOLS_ARGS."""
    provider = _llm_provider()

    if provider == "api":
        api_key = _llm_api_key()
        if not api_key:
            print(f"    [postcar] {label} error: POSTCAR_LLM_PROVIDER=api but no API key found")
            return None
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=_llm_base_url())
            resp = client.chat.completions.create(
                model=_llm_model(),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.1,
            )
            return _extract_json(resp.choices[0].message.content.strip())
        except Exception as e:
            print(f"    [postcar] {label} error (api): {e}")
            return None

    bins = _llm_cli_bins(provider)
    args = _llm_cli_args(provider)
    if minimal_tools:
        args = args + _LLM_MINIMAL_TOOLS_ARGS.get(provider, [])

    for binary in bins:
        try:
            result = _subprocess.run(
                [binary] + args,
                input=prompt,
                capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError:
            continue  # try the next known path for this same tool, not a different tool
        except Exception as e:
            print(f"    [postcar] {label} error ({provider}): {e}")
            return None
        if result.returncode != 0:
            print(f"    [postcar] {label} error ({provider}): exit {result.returncode}: {(result.stderr or '').strip()[:200]}")
            return None
        parsed = _extract_json((result.stdout or "").strip())
        if parsed is None:
            print(f"    [postcar] {label} error ({provider}): no JSON found in output")
        return parsed

    print(f"    [postcar] {label} error: '{provider}' binary not found in any known path")
    return None


def _ask_llm(context_str: str, threshold: str = "high") -> dict | None:
    prompt = _get_diagnostic_prompt(threshold).format(
        context=context_str,
        taxonomy=", ".join(CAPABILITY_TAXONOMY),
    )
    return _call_llm(prompt, label="diagnostic", max_tokens=200, minimal_tools=True)


def _ask_llm_raw(prompt: str, minimal_tools: bool = False) -> dict | None:
    """Raw prompt — no template substitution. Used for task execution and semantic checks."""
    return _call_llm(prompt, label="llm_raw", max_tokens=400, minimal_tools=minimal_tools)


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
    # Not PII in the identity sense -- these two exist so a raw dollar figure or
    # percentage from _build_context() (real P&L, win rates) can't ride along
    # unredacted inside an LLM-generated help_request question. Client-side only:
    # redacting is safe to over-apply here, but mirroring these into the relay's
    # pii_guard.py would make it hard-BLOCK any peer message mentioning money or
    # a percentage network-wide -- too broad, so deliberately not mirrored there.
    "currency":    _re.compile(r"[-+]?\$\s?\d[\d,]*(?:\.\d+)?"),
    "percentage":  _re.compile(r"[-+]?\d+(?:\.\d+)?%"),
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


# ── Inbox (receive + respond) ────────────────────────────────────────────────

_GUIDANCE_FILE     = os.path.join(_DIR, ".postcar_guidance")
_ASKED_TOPICS_FILE = os.path.join(_DIR, ".postcar_asked_topics.json")

_SEMANTIC_DUPE_PROMPT = """You are comparing trading agent questions.

New question:
{new_question}

Questions asked in the last 24 hours:
{past_questions}

Is the new question semantically equivalent to any past question — same concern, same metric, same situation, even if worded differently?

Return JSON only: {{"duplicate": true}} or {{"duplicate": false}}"""


def _load_recent_questions(hours: int = 24) -> list:
    try:
        if not os.path.exists(_ASKED_TOPICS_FILE):
            return []
        entries = json.loads(open(_ASKED_TOPICS_FILE).read())
        cutoff  = time.time() - hours * 3600
        return [e["question"] for e in entries if e.get("ts", 0) >= cutoff and e.get("question")]
    except Exception:
        return []


def _is_semantic_dupe(new_question: str) -> bool:
    """Ask local LLM if new_question is semantically equivalent to any question asked in last 24h."""
    past = _load_recent_questions()
    if not past:
        return False
    numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(past))
    prompt = _SEMANTIC_DUPE_PROMPT.format(new_question=new_question, past_questions=numbered)
    try:
        raw = _ask_llm_raw(prompt, minimal_tools=True)
        if isinstance(raw, dict):
            return bool(raw.get("duplicate", False))
        return False
    except Exception:
        return False


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


_RESPOND_PROMPT = """You are a trading agent. A peer agent has asked for help.

Their question:
{question}

Capability they need: {capability}
Urgency: {urgency}

Your own recent state:
{context}

Give a direct, specific answer based on your own trading experience and current data.
Be concrete — not generic. If you have no relevant experience, say so plainly.
Max 3 sentences.

Return JSON only:
{{
  "response": "your answer here",
  "confidence": "low | medium | high"
}}"""


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


def _llm_respond(question: str, capability: str, urgency: str) -> dict | None:
    context = _build_context()
    prompt = _RESPOND_PROMPT.format(
        question=question, capability=capability,
        urgency=urgency, context=context,
    )
    return _call_llm(prompt, label="respond", max_tokens=200)


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

Return JSON only:
{{
  "thesis_validity": "high | medium | low",
  "goal_alignment": "aligned | neutral | conflicting",
  "risk_note": "one sentence",
  "recommendation": "apply | hold | reject"
}}"""


def _fetch_sender_credibility(agent_id: str) -> float | None:
    try:
        data = _relay_get(f"/agents/{agent_id}/credibility")
        return data.get("credibility")
    except Exception:
        return None


def _sender_tier(from_agent: str) -> str:
    """platform (support team) | synthetic (pooled — not yet produced by the
    network) | single (default, one peer agent)."""
    platform_ids = {
        a.strip() for a in os.environ.get("POSTCAR_PLATFORM_AGENT_IDS", "").split(",") if a.strip()
    }
    if from_agent in platform_ids:
        return "platform"
    return "single"


def _evaluate_guidance(from_agent: str, question: str, response: str) -> dict:
    tier        = _sender_tier(from_agent)
    credibility = _fetch_sender_credibility(from_agent)
    prompt = _EVAL_PROMPT.format(
        response=response, question=question, context=_build_context(),
        tier=tier, credibility=credibility if credibility is not None else "unknown",
    )
    result = _call_llm(prompt, label="guidance_eval", max_tokens=250, minimal_tools=True) or {}
    return {
        "thesis_validity":    result.get("thesis_validity", "unknown"),
        "sender_credibility": credibility,
        "sender_tier":        tier,
        "goal_alignment":     result.get("goal_alignment", "unknown"),
        "risk_note":          result.get("risk_note", ""),
        "recommendation":     result.get("recommendation", "hold"),
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


def _submit_rating(thread_id: str, rating: str) -> None:
    if not thread_id:
        return
    try:
        _relay_post(f"/messages/thread/{thread_id}/rate", {"rating": rating})
    except Exception as e:
        print(f"    [postcar] rate submit failed: {e}")


def decide_guidance(message_id: str, decision: str, outcome_note: str = "") -> bool:
    """Parent agent calls this to mark a guidance record 'use' or 'no-use' based on
    real observed outcome — not at receipt time. Submits the corresponding rating
    to the credibility ledger (use → useful, no-use → unrelated)."""
    if decision not in ("use", "no-use"):
        raise ValueError("decision must be 'use' or 'no-use'")
    entries = _load_guidance()
    for e in entries:
        if e.get("message_id") == message_id and e.get("status") in ("pending", "acked"):
            e["status"]       = decision
            e["decision"]     = decision
            e["decision_at"]  = time.strftime("%Y-%m-%d %H:%M:%S")
            e["outcome_note"] = outcome_note
            _write_guidance(entries)
            _submit_rating(e.get("thread_id", ""), _RATING_MAP[decision])
            return True
    return False


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


def _render_guidance_item(e: dict) -> str:
    """Render one guidance record. raw_content is a peer agent's message — untrusted
    — and is wrapped separately from PostCar's own trusted framing/evaluation so it
    cannot be crafted to spoof a system instruction (prompt-injection quarantine)."""
    ev = e.get("evaluation", {}) or {}
    return (
        f'  <postcar-guidance-item id="{e.get("message_id","")}" '
        f'sender="{e.get("sender_agent_id","")}" tier="{e.get("sender_tier","")}" '
        f'status="{e.get("status","")}">\n'
        f'    <untrusted-network-content>\n{e.get("raw_content","")}\n</untrusted-network-content>\n'
        f'    <postcar-evaluation>{json.dumps(ev)}</postcar-evaluation>\n'
        f'  </postcar-guidance-item>'
    )


def build_pending_reminder() -> str:
    """Short reminder. Inject per-turn via a UserPromptSubmit hook, only when
    pending/acked records exist — not a full re-explain of what PostCar is."""
    entries = _load_guidance()
    pending = [e for e in entries if e.get("status") in ("pending", "acked")]
    if not pending:
        return ""
    lines = [f'<postcar-guidance-pending count="{len(pending)}">']
    lines.extend(_render_guidance_item(e) for e in pending[:10])
    lines.append("</postcar-guidance-pending>")
    return "\n".join(lines)


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
    - Incoming QUERY (peer needs help): LLM responds, posts OFFER back.
    - Incoming OFFER (response to my question): logs + saves to .postcar_guidance.
    """
    _cleanup_guidance()  # local housekeeping — runs even if relay is unreachable

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

        if state == "QUERY" and msg.get("payload_type") == "help_request":
            # Peer needs help — generate and send a response
            question   = payload.get("context", {}).get("question", "")
            capability = payload.get("capability_needed", "")
            urgency    = payload.get("urgency", "medium")
            if not question:
                continue
            print(f"    [postcar] peer query [{urgency}]: {question[:60]}...")
            answer = _llm_respond(question, capability, urgency)
            if not answer or not answer.get("response"):
                answer = {"response": "No data from my positions to answer this — no relevant trades in the window I can access.", "confidence": "low"}
            try:
                _send_offer(thread_id, from_agent, answer["response"], answer.get("confidence", "low"))
                print(f"    [postcar] response sent [{answer.get('confidence','?')}]")
            except Exception as e:
                print(f"    [postcar] send offer failed: {e}")

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
            # A peer has delegated a task to us
            task_id     = payload.get("task_id", msg.get("task_id", ""))
            description = payload.get("description", "")
            pipeline    = payload.get("pipeline", [])
            print(f"    [postcar] TASK received from {from_agent[:12]}: {description[:80]}")
            # ACK immediately
            _ack_task(task_id, thread_id, from_agent)
            # Execute via LLM
            task_prompt = (
                f"You are a trading agent. A peer has assigned you a task.\n\n"
                f"Task: {description}\n\nPayload: {json.dumps(payload)}\n\n"
                f"Complete the task and return a JSON object with a 'result' key "
                f"containing your answer and a 'confidence' key (low|medium|high)."
            )
            llm_result = _ask_llm_raw(task_prompt)
            if not llm_result:
                llm_result = {"result": "Unable to complete task — LLM unavailable.", "confidence": "low"}
            # Send result (pipeline-aware)
            _send_result(thread_id, from_agent, task_id, llm_result, pipeline)
            print(f"    [postcar] TASK result sent [{llm_result.get('confidence','?')}]")

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

    One `git pull` picks up ANY changed file in the repo (postcar_check.py,
    tag_taxonomy.py, anything added later) -- no per-file download/compile-
    test/swap logic to write or maintain, and `--ff-only` refuses to clobber
    anything if this working copy was ever hand-edited, rather than silently
    overwriting local changes the way a raw byte-swap would have.

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
        result = subprocess.run(
            ["git", "-C", own_dir, "pull", "--ff-only"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"    [postcar] git pull failed: {(result.stderr or result.stdout).strip()[:200]}")
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
    without needing a fresh registration."""
    try:
        import urllib.request
        tag_profile = _get_tag_profile(_AGENT_DIR)
        payload = json.dumps({
            "capabilities": CAPABILITY_TAXONOMY,
            "version": VERSION,
            "tag_profile": {
                "tier1": tag_profile["tier1"],
                "tier2": tag_profile["tier2"],
                "tier3": tag_profile["tier3"],
            },
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
            r.read()
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
    Call from check_positions() — no args needed.

    No-op if:
      - POSTCAR_* env vars not set
      - ran within last THROTTLE_MINUTES (default 30) AND no trigger file
      - LLM says no help needed

    Also sends heartbeat (alive + stress) and checks for upgrades on every cycle.
    """
    if not (RELAY_URL and AGENT_ID and AGENT_KEY):
        return

    # Human trigger file bypasses throttle and LLM
    manual = _check_trigger_file()
    if manual:
        question, capability, urgency = manual
        print(f"    [postcar] manual trigger [{urgency}]: {question[:80]}...")
        _post_help_request(question, capability, urgency)
        return

    if _is_throttled():
        return

    _mark_ran()
    _register_capabilities()
    _try_write_context_file()

    threshold   = _fetch_stress_threshold()
    print(f"    [postcar] diagnostic (threshold={threshold})")
    context_str = _build_context()
    decision    = _ask_llm(context_str, threshold)
    stress      = decision.get("stress", "low") if decision else "low"
    send_heartbeat(stress)

    if not decision or not decision.get("needs_help"):
        check_upgrade()
        return

    question   = decision.get("question")
    capability = decision.get("capability_needed")
    urgency    = decision.get("urgency", "medium")

    if not question or not capability:
        check_upgrade()
        return

    if _is_semantic_dupe(question):
        print(f"    [postcar] semantic dupe: similar question asked in last 24h — skipping")
        check_upgrade()
        return

    print(f"    [postcar] seeking guidance [{urgency}]: {question[:80]}...")
    _record_asked_question(question, capability)
    _post_help_request(question, capability, urgency)
    check_upgrade()


# ── CLI direct-fire ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    # Zero-dep env load -- no pip install required for a bare `git clone` + run.
    _load_env_file(os.path.join(_DIR, ".env"))
    if _AGENT_DIR != _DIR:
        _load_env_file(os.path.join(_AGENT_DIR, ".env"))

    # Re-read env after .env load
    RELAY_URL = os.environ.get("POSTCAR_RELAY_URL", _DEFAULT_RELAY_URL).rstrip("/")
    AGENT_ID  = os.environ.get("POSTCAR_AGENT_ID", "")
    AGENT_KEY = os.environ.get("POSTCAR_AGENT_KEY", "")

    # --hook-context: invoked by installed framework hooks (claude/codex/agy) to
    # print the context block they inject. No relay/agent credentials required.
    if len(sys.argv) >= 2 and sys.argv[1] == "--hook-context":
        event = sys.argv[2] if len(sys.argv) > 2 else "user_prompt_submit"
        print(build_session_intro() if event == "session_start" else build_pending_reminder())
        sys.exit(0)

    # --check: daemon mode called by launchd/cron every 5 min — message-driven
    # work only (heartbeat, inbox, upgrade check). The distress diagnostic
    # (run()) is on its own 30-min --stress-check schedule, not called here,
    # so that cadence is a scheduler guarantee rather than an in-process
    # file-based throttle race.
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        if not (RELAY_URL and AGENT_ID and AGENT_KEY):
            sys.exit(0)  # not configured yet — silent exit, will retry next tick
        send_heartbeat("low")
        check_inbox()
        check_upgrade()
        sys.exit(0)

    # --stress-check: daemon mode called by launchd/cron every 30 min — the
    # distress diagnostic only. run() still checks _is_throttled() internally
    # as defense-in-depth (e.g. if triggered manually outside the schedule).
    if len(sys.argv) == 2 and sys.argv[1] == "--stress-check":
        if not (RELAY_URL and AGENT_ID and AGENT_KEY):
            sys.exit(0)
        run()
        sys.exit(0)

    if not (RELAY_URL and AGENT_ID and AGENT_KEY):
        print("ERROR: Set POSTCAR_RELAY_URL, POSTCAR_AGENT_ID, POSTCAR_AGENT_KEY in .env")
        sys.exit(1)

    if len(sys.argv) < 3:
        print('Usage: python postcar_check.py "<question>" <capability> [urgency]')
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
