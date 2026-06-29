"""
tests/test_context_builder.py — Tests for context_builder module.
"""

from __future__ import annotations

import json
import os

import pytest

from context_builder import auto_register, derive_tags, scan_directory


# ---------------------------------------------------------------------------
# scan_directory tests
# ---------------------------------------------------------------------------


class TestScanDirectory:
    def test_name_from_h1(self, tmp_path):
        """Name is extracted from the first H1 heading in CLAUDE.md."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# TestAgent\n\nThis agent handles trading operations.\n",
            encoding="utf-8",
        )
        result = scan_directory(str(tmp_path))
        assert result["name"] == "TestAgent"

    def test_domain_hints_trading(self, tmp_path):
        """'trading' keyword in CLAUDE.md is detected as domain hint."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# TestAgent\n\nThis agent handles trading operations.\n",
            encoding="utf-8",
        )
        result = scan_directory(str(tmp_path))
        assert "trading" in result["domain_hints"]

    def test_name_fallback_to_dirname(self, tmp_path):
        """When no H1 heading is found, name falls back to directory basename."""
        (tmp_path / "notes.md").write_text("No heading here.\n", encoding="utf-8")
        result = scan_directory(str(tmp_path))
        assert result["name"] == tmp_path.name

    def test_empty_dir(self, tmp_path):
        """Empty directory returns basename as name, empty lists for keywords."""
        result = scan_directory(str(tmp_path))
        assert result["name"] == tmp_path.name
        assert result["tech_stack"] == []
        assert result["domain_hints"] == []
        assert result["description"] == ""
        assert result["raw_text"] == ""

    def test_description_first_paragraph(self, tmp_path):
        """description is the first non-heading paragraph (max 200 chars)."""
        (tmp_path / "README.md").write_text(
            "# MyBot\n\nThis is a research agent that does analytics.\n",
            encoding="utf-8",
        )
        result = scan_directory(str(tmp_path))
        assert result["description"].startswith("This is a research agent")

    def test_tech_stack_detected(self, tmp_path):
        """Tech keywords are extracted from markdown content."""
        (tmp_path / "CLAUDE.md").write_text(
            "# Stack\n\nBuilt with python and fastapi.\n",
            encoding="utf-8",
        )
        result = scan_directory(str(tmp_path))
        assert "python" in result["tech_stack"]
        assert "fastapi" in result["tech_stack"]

    def test_raw_text_limit(self, tmp_path):
        """raw_text is capped at 2000 characters."""
        (tmp_path / "CLAUDE.md").write_text("x" * 5000, encoding="utf-8")
        result = scan_directory(str(tmp_path))
        assert len(result["raw_text"]) <= 2000

    def test_priority_order_claude_md_first(self, tmp_path):
        """CLAUDE.md is read before README.md — its H1 wins."""
        (tmp_path / "CLAUDE.md").write_text("# FromClaude\n\nContent.\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("# FromReadme\n\nContent.\n", encoding="utf-8")
        result = scan_directory(str(tmp_path))
        assert result["name"] == "FromClaude"


# ---------------------------------------------------------------------------
# derive_tags tests
# ---------------------------------------------------------------------------


class TestDeriveTags:
    def test_trading_domain_hints(self):
        """domain_hints=['trading'] produces tier1 with 'domain:finance'."""
        context = {
            "domain_hints": ["trading"],
            "description": "A trading agent.",
        }
        result = derive_tags(context)
        assert "domain:finance" in result["tier1"]
        assert "identity:trading-agent" in result["tier1"]

    def test_trading_tier2_tags(self):
        """trading domain also adds tier2 strategy/skill tags."""
        context = {"domain_hints": ["trading"], "description": ""}
        result = derive_tags(context)
        assert "strategy:systematic" in result["tier2"]
        assert "skill:risk-management" in result["tier2"]

    def test_empty_hints_empty_tags(self):
        """No domain hints -> empty tier1, tier2, and flat."""
        context = {"domain_hints": [], "description": ""}
        result = derive_tags(context)
        assert result["tier1"] == []
        assert result["tier2"] == []
        assert result["flat"] == []

    def test_tier3_uses_description(self):
        """tier3 is the first 150 chars of the description."""
        desc = "An analytics platform for real-time data." * 5
        context = {"domain_hints": [], "description": desc}
        result = derive_tags(context)
        assert result["tier3"] == desc[:150]

    def test_tier3_fallback(self):
        """tier3 defaults to 'autonomous agent' when description is empty."""
        context = {"domain_hints": [], "description": ""}
        result = derive_tags(context)
        assert result["tier3"] == "autonomous agent"

    def test_flat_is_union_no_dupes(self):
        """flat contains unique tags from tier1 + tier2 without duplicates."""
        context = {"domain_hints": ["trading", "finance"], "description": ""}
        result = derive_tags(context)
        # trading -> tier1: domain:finance, identity:trading-agent
        # finance -> tier1: domain:finance  (duplicate)
        assert result["flat"].count("domain:finance") == 1

    def test_multiple_domains(self):
        """Multiple domain hints accumulate tags correctly."""
        context = {"domain_hints": ["ml", "research"], "description": ""}
        result = derive_tags(context)
        assert "domain:ml" in result["tier1"]
        assert "domain:research" in result["tier1"]

    def test_unknown_hint_ignored(self):
        """Unknown domain hints produce no tags."""
        context = {"domain_hints": ["blockchain"], "description": ""}
        result = derive_tags(context)
        assert result["tier1"] == []
        assert result["flat"] == []


# ---------------------------------------------------------------------------
# auto_register tests
# ---------------------------------------------------------------------------


class TestAutoRegister:
    def test_returns_cached_profile(self, tmp_path):
        """If .postcar_profile.json exists, it is returned without scanning."""
        profile = {
            "registered": True,
            "name": "CachedAgent",
            "tag_profile": {"tier1": ["domain:finance"], "tier2": [], "tier3": "x", "flat": ["domain:finance"]},
        }
        (tmp_path / ".postcar_profile.json").write_text(json.dumps(profile), encoding="utf-8")

        # Create a CLAUDE.md that would produce a different name if scanned
        (tmp_path / "CLAUDE.md").write_text("# DifferentAgent\n\nSomething.\n", encoding="utf-8")

        result = auto_register(str(tmp_path), client=None)
        assert result["name"] == "CachedAgent"
        assert result["registered"] is True

    def test_no_client_returns_unregistered(self, tmp_path):
        """Without a client, auto_register returns registered=False."""
        (tmp_path / "CLAUDE.md").write_text("# MyAgent\n\nTrading agent.\n", encoding="utf-8")
        result = auto_register(str(tmp_path), client=None)
        assert result["registered"] is False
        assert result["name"] == "MyAgent"
        assert "tag_profile" in result

    def test_with_client_saves_profile(self, tmp_path):
        """With a client object, .postcar_profile.json is saved to agent_dir."""
        (tmp_path / "CLAUDE.md").write_text("# SaveAgent\n\nAnalytics platform.\n", encoding="utf-8")

        # Use a non-None sentinel object — no actual relay call is made
        fake_client = object()
        result = auto_register(str(tmp_path), client=fake_client)

        profile_path = tmp_path / ".postcar_profile.json"
        assert profile_path.exists()
        assert result["note"] == "Run manual registration"
        assert result["registered"] is False

    def test_with_client_profile_content(self, tmp_path):
        """Saved .postcar_profile.json is valid JSON matching returned result."""
        (tmp_path / "CLAUDE.md").write_text("# JSONAgent\n\nfinance analytics.\n", encoding="utf-8")
        fake_client = object()
        result = auto_register(str(tmp_path), client=fake_client)

        with open(tmp_path / ".postcar_profile.json", encoding="utf-8") as fh:
            saved = json.load(fh)

        assert saved["name"] == result["name"]
        assert saved["tag_profile"] == result["tag_profile"]

    def test_name_override(self, tmp_path):
        """Explicit name parameter overrides extracted name."""
        (tmp_path / "CLAUDE.md").write_text("# ExtractedName\n\nSomething.\n", encoding="utf-8")
        result = auto_register(str(tmp_path), client=None, name="OverrideName")
        assert result["name"] == "OverrideName"

    def test_tag_profile_present_no_client(self, tmp_path):
        """tag_profile is included in result even without a client."""
        (tmp_path / "CLAUDE.md").write_text("# TradingBot\n\nA trading system.\n", encoding="utf-8")
        result = auto_register(str(tmp_path), client=None)
        tp = result["tag_profile"]
        assert "tier1" in tp
        assert "tier2" in tp
        assert "tier3" in tp
        assert "flat" in tp
