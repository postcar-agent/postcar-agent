"""Tests for llm.py — provider detection and call utilities."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from llm import call_llm, detect_provider, make_template_query


class TestDetectProvider:
    def test_no_cli_no_env_returns_none(self):
        """All which() calls return None and no env vars set -> None."""
        with patch("shutil.which", return_value=None), patch.dict(
            "os.environ",
            {},
            clear=True,
        ):
            assert detect_provider() is None

    def test_claude_cli_found(self):
        """which('claude') returns a path -> 'claude-cli'."""
        def which_side_effect(cmd):
            return "/usr/local/bin/claude" if cmd == "claude" else None

        with patch("shutil.which", side_effect=which_side_effect):
            assert detect_provider() == "claude-cli"

    def test_anthropic_api_key_set(self):
        """which returns None for all, ANTHROPIC_API_KEY set -> 'claude-api'."""
        with patch("shutil.which", return_value=None), patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "sk-test-key"},
            clear=True,
        ):
            assert detect_provider() == "claude-api"

    def test_gemini_cli_found(self):
        """which('gemini') returns path -> 'gemini-cli' (claude not found)."""
        def which_side_effect(cmd):
            return "/usr/local/bin/gemini" if cmd == "gemini" else None

        with patch("shutil.which", side_effect=which_side_effect):
            assert detect_provider() == "gemini-cli"

    def test_google_api_key_set(self):
        """which returns None, GOOGLE_API_KEY set -> 'gemini-api'."""
        with patch("shutil.which", return_value=None), patch.dict(
            "os.environ",
            {"GOOGLE_API_KEY": "gk-test-key"},
            clear=True,
        ):
            assert detect_provider() == "gemini-api"

    def test_openai_api_key_set(self):
        """which returns None, OPENAI_API_KEY set -> 'openai-api'."""
        with patch("shutil.which", return_value=None), patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "sk-openai-test"},
            clear=True,
        ):
            assert detect_provider() == "openai-api"

    def test_cli_takes_priority_over_api_key(self):
        """CLI binary found takes priority over env API key."""
        def which_side_effect(cmd):
            return "/usr/local/bin/claude" if cmd == "claude" else None

        with patch("shutil.which", side_effect=which_side_effect), patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "sk-test-key"},
            clear=True,
        ):
            assert detect_provider() == "claude-cli"


class TestCallLlm:
    def test_subprocess_success_returns_stdout(self):
        """Mock subprocess.run with returncode=0 and stdout='answer' -> 'answer'."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "answer"

        def which_side_effect(cmd):
            return "/usr/local/bin/claude" if cmd == "claude" else None

        with patch("shutil.which", side_effect=which_side_effect), patch(
            "subprocess.run", return_value=mock_result
        ) as mock_run:
            result = call_llm("test prompt")
            assert result == "answer"
            mock_run.assert_called_once()

    def test_no_provider_returns_empty_string(self):
        """No provider detected -> returns ''."""
        with patch("shutil.which", return_value=None), patch.dict(
            "os.environ", {}, clear=True
        ):
            result = call_llm("test prompt")
            assert result == ""

    def test_subprocess_failure_falls_back_gracefully(self):
        """subprocess.run returns non-zero, fallback also fails -> ''."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        def which_side_effect(cmd):
            return "/usr/local/bin/claude" if cmd == "claude" else None

        with patch("shutil.which", side_effect=which_side_effect), patch(
            "subprocess.run", return_value=mock_result
        ):
            result = call_llm("test prompt")
            assert result == ""

    def test_subprocess_exception_returns_empty_string(self):
        """subprocess.run raises exception -> returns ''."""
        def which_side_effect(cmd):
            return "/usr/local/bin/claude" if cmd == "claude" else None

        with patch("shutil.which", side_effect=which_side_effect), patch(
            "subprocess.run", side_effect=OSError("not found")
        ):
            result = call_llm("test prompt")
            assert result == ""

    def test_gemini_cli_call(self):
        """Gemini CLI path is called correctly."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "gemini answer"

        def which_side_effect(cmd):
            return "/usr/local/bin/gemini" if cmd == "gemini" else None

        with patch("shutil.which", side_effect=which_side_effect), patch(
            "subprocess.run", return_value=mock_result
        ) as mock_run:
            result = call_llm("test prompt")
            assert result == "gemini answer"
            args = mock_run.call_args[0][0]
            assert args[0] == "gemini"


class TestMakeTemplateQuery:
    def test_contains_topic(self):
        """Result string contains the topic."""
        result = make_template_query("burnout", "fatigue, insomnia")
        assert "burnout" in result

    def test_contains_indicators(self):
        """Result string contains the indicators."""
        result = make_template_query("stress", "headache, irritability")
        assert "headache, irritability" in result

    def test_format(self):
        """Result matches expected template format."""
        result = make_template_query("anxiety", "racing thoughts")
        assert result == (
            "I need guidance on: anxiety. My current stress indicators: racing thoughts."
        )

    def test_empty_strings(self):
        """Works with empty strings."""
        result = make_template_query("", "")
        assert isinstance(result, str)
        assert "I need guidance on:" in result
