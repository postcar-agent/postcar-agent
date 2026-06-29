"""
context_builder.py — CLAUDE.md scanner, tag derivation, and auto-registration helper.

Kit-only writes: .postcar_profile.json (and related postcar files).
No imports of agent source code except read-only adapters.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TECH_KEYWORDS = [
    "python", "typescript", "fastapi", "django", "react", "go", "rust", "nodejs",
]

DOMAIN_KEYWORDS = [
    "trading", "finance", "marketing", "analytics", "ml", "research", "operations", "ops",
]

DOMAIN_TAG_MAP: Dict[str, Dict[str, List[str]]] = {
    "trading": {
        "tier1": ["domain:finance", "identity:trading-agent"],
        "tier2": ["strategy:systematic", "skill:risk-management"],
    },
    "finance": {
        "tier1": ["domain:finance"],
        "tier2": [],
    },
    "marketing": {
        "tier1": ["domain:marketing", "identity:marketing-agent"],
        "tier2": [],
    },
    "analytics": {
        "tier1": ["domain:analytics", "identity:analytics-agent"],
        "tier2": [],
    },
    "ml": {
        "tier1": ["domain:ml", "skill:model-training"],
        "tier2": [],
    },
    "research": {
        "tier1": ["domain:research", "identity:research-agent"],
        "tier2": [],
    },
    "operations": {
        "tier1": ["domain:operations", "identity:ops-agent"],
        "tier2": [],
    },
    "ops": {
        "tier1": ["domain:operations"],
        "tier2": [],
    },
}

# ---------------------------------------------------------------------------
# FUNCTION 1: scan_directory
# ---------------------------------------------------------------------------


def scan_directory(agent_dir: str) -> Dict[str, Any]:
    """Scan agent_dir for markdown files and extract structured context.

    Reads CLAUDE.md (if exists), README.md (if exists), then other *.md
    files in the root of agent_dir.

    Returns a dict with keys:
        name         – first H1 heading found, or os.path.basename(agent_dir)
        description  – first non-heading paragraph (first 200 chars)
        tech_stack   – list of tech keywords found
        domain_hints – list of domain keywords found
        raw_text     – first 2000 chars of concatenated markdown content
    """
    agent_dir = os.path.abspath(agent_dir)

    # Determine read order: CLAUDE.md first, README.md second, rest alphabetically
    priority = ["CLAUDE.md", "README.md"]
    try:
        all_md = [f for f in os.listdir(agent_dir) if f.endswith(".md")]
    except OSError:
        all_md = []

    ordered: List[str] = []
    for p in priority:
        if p in all_md:
            ordered.append(p)
    for f in sorted(all_md):
        if f not in ordered:
            ordered.append(f)

    # Concatenate content
    chunks: List[str] = []
    for filename in ordered:
        filepath = os.path.join(agent_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                chunks.append(fh.read())
        except OSError:
            pass

    combined = "\n\n".join(chunks)
    raw_text = combined[:2000]

    # Extract name from first H1 heading
    name: Optional[str] = None
    for line in combined.splitlines():
        m = re.match(r"^#\s+(.+)", line)
        if m:
            name = m.group(1).strip()
            break
    if not name:
        name = os.path.basename(agent_dir)

    # Extract description: first non-empty, non-heading paragraph
    description = ""
    in_block = False
    for line in combined.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            continue
        if stripped.startswith("#"):
            continue
        if stripped:
            description = stripped[:200]
            break

    # Keyword matching (case-insensitive)
    lower_text = combined.lower()
    tech_stack = [kw for kw in TECH_KEYWORDS if kw in lower_text]
    domain_hints = [kw for kw in DOMAIN_KEYWORDS if kw in lower_text]

    return {
        "name": name,
        "description": description,
        "tech_stack": tech_stack,
        "domain_hints": domain_hints,
        "raw_text": raw_text,
    }


# ---------------------------------------------------------------------------
# FUNCTION 2: derive_tags
# ---------------------------------------------------------------------------


def derive_tags(context: Dict[str, Any]) -> Dict[str, Any]:
    """Derive a tag profile from a scan_directory context dict.

    Returns:
        {
            "tier1": [...],   # identity / domain tags (deduplicated)
            "tier2": [...],   # skill / strategy tags (deduplicated)
            "tier3": str,     # free-text description (max 150 chars)
            "flat": [...],    # unique union of tier1 + tier2
        }
    """
    tier1: List[str] = []
    tier2: List[str] = []

    for hint in context.get("domain_hints", []):
        mapping = DOMAIN_TAG_MAP.get(hint)
        if mapping:
            for tag in mapping.get("tier1", []):
                if tag not in tier1:
                    tier1.append(tag)
            for tag in mapping.get("tier2", []):
                if tag not in tier2:
                    tier2.append(tag)

    description = context.get("description", "") or ""
    tier3 = description[:150] if description else "autonomous agent"

    flat: List[str] = []
    for tag in tier1 + tier2:
        if tag not in flat:
            flat.append(tag)

    return {
        "tier1": tier1,
        "tier2": tier2,
        "tier3": tier3,
        "flat": flat,
    }


# ---------------------------------------------------------------------------
# FUNCTION 3: auto_register
# ---------------------------------------------------------------------------


def auto_register(
    agent_dir: str,
    client: Any,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Scan agent_dir, derive tags, cache profile, and optionally register.

    Steps:
    1. If .postcar_profile.json exists in agent_dir, load and return it.
    2. scan_directory -> derive_tags.
    3. agent_name = name or context["name"].
    4. If client is None, return dict without writing file.
    5. Otherwise save .postcar_profile.json and return result with note.

    Full auto-registration requires owner credentials (not in .env by default).
    """
    agent_dir = os.path.abspath(agent_dir)
    profile_path = os.path.join(agent_dir, ".postcar_profile.json")

    # Step 1: return cached profile if it exists
    if os.path.isfile(profile_path):
        try:
            with open(profile_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass  # fall through and rebuild

    # Step 2: scan + derive
    context = scan_directory(agent_dir)
    tag_profile = derive_tags(context)

    # Step 3: resolve name
    agent_name = name or context["name"]

    # Step 4: no client
    if client is None:
        return {
            "registered": False,
            "tag_profile": tag_profile,
            "name": agent_name,
        }

    # Step 5: save profile (registration requires owner key — skip relay call)
    result: Dict[str, Any] = {
        "registered": False,
        "tag_profile": tag_profile,
        "name": agent_name,
        "note": "Run manual registration",
    }
    try:
        with open(profile_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except OSError:
        pass  # non-fatal; return result anyway

    return result
