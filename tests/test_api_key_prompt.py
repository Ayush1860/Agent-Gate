"""The API key can be supplied at runtime, so rotating it never means editing a file."""

from __future__ import annotations

import os

import pytest

from agentgate.cli import build_parser, ensure_api_key
from agentgate.config import get_settings


def _args(*argv):
    return build_parser().parse_args(list(argv))


def _fail_prompt(_msg):
    raise AssertionError("should not prompt")


def test_mock_provider_never_prompts():
    ensure_api_key(_args("eval", "--provider", "mock"), prompt=_fail_prompt)


def test_live_provider_without_key_prompts_and_sets_env(monkeypatch):
    monkeypatch.setenv("AGENTGATE_API_KEY", "")
    get_settings.cache_clear()
    ensure_api_key(_args("eval", "--provider", "openai_compat"), prompt=lambda _m: " new-key ")
    assert os.environ["AGENTGATE_API_KEY"] == "new-key"
    assert get_settings().api_key == "new-key"


def test_existing_key_is_used_without_prompting(monkeypatch):
    monkeypatch.setenv("AGENTGATE_API_KEY", "already-set")
    get_settings.cache_clear()
    ensure_api_key(_args("eval", "--provider", "openai_compat"), prompt=_fail_prompt)


def test_ask_key_overrides_a_configured_key(monkeypatch):
    monkeypatch.setenv("AGENTGATE_API_KEY", "old-key")
    get_settings.cache_clear()
    ensure_api_key(
        _args("--ask-key", "eval", "--provider", "openai_compat"), prompt=lambda _m: "rotated"
    )
    assert get_settings().api_key == "rotated"


def test_compare_with_a_live_provider_prompts(monkeypatch):
    monkeypatch.setenv("AGENTGATE_API_KEY", "")
    get_settings.cache_clear()
    ensure_api_key(_args("eval", "--compare", "mock,openai_compat:x"), prompt=lambda _m: "k")
    assert get_settings().api_key == "k"


def test_empty_input_aborts(monkeypatch):
    monkeypatch.setenv("AGENTGATE_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(SystemExit):
        ensure_api_key(_args("eval", "--provider", "openai_compat"), prompt=lambda _m: "  ")


def test_dashboard_never_prompts():
    ensure_api_key(_args("dashboard"), prompt=_fail_prompt)


@pytest.mark.parametrize("bad", ["\x16", "AIza\x16", "key with space", "kéy"])
def test_control_or_non_ascii_input_is_rejected(monkeypatch, bad):
    """Windows Ctrl+V at a hidden prompt yields \x16, not the clipboard."""
    monkeypatch.setenv("AGENTGATE_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(SystemExit, match="right-click"):
        ensure_api_key(_args("eval", "--provider", "openai_compat"), prompt=lambda _m: bad)
