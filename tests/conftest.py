"""Shared test fixtures.

The whole suite runs offline against the mock provider. No test may require a
live API key or network access.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def offline_env(tmp_path, monkeypatch):
    """Force mock mode, isolate traces, and make sure no stray key leaks in."""
    monkeypatch.setenv("AGENTGATE_PROVIDER", "mock")
    monkeypatch.setenv("AGENTGATE_MODEL", "mock-reviewer-v1")
    monkeypatch.setenv("AGENTGATE_API_KEY", "")
    monkeypatch.setenv("AGENTGATE_BASE_URL", "")
    monkeypatch.setenv("AGENTGATE_TRACE_FILE", str(tmp_path / "traces.jsonl"))
    monkeypatch.setenv("AGENTGATE_MOCK_LATENCY_MS", "60")
    # Ignore any developer .env so results do not depend on the local machine.
    monkeypatch.setattr(
        "pydantic_settings.sources.DotEnvSettingsSource._read_env_files",
        lambda self: {},
        raising=False,
    )

    from agentgate import config, llm

    config.get_settings.cache_clear()
    llm.reset_provider_cache()
    yield
    config.get_settings.cache_clear()
    llm.reset_provider_cache()
