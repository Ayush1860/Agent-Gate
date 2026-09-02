"""Shared test fixtures.

The whole suite runs offline against the mock provider. No test may require a
live API key or network access.

Hermeticity is enforced at **session** scope, not function scope. A function-scoped
fixture is not enough: pytest instantiates higher-scoped fixtures first, so a
module-scoped fixture that runs the eval would pick up the developer's real `.env`
and quietly bill a live provider. That happened, and it is why the session-scoped
pin below exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Environment the suite is pinned to, whatever the developer's .env says.
OFFLINE_ENV = {
    "AGENTGATE_PROVIDER": "mock",
    "AGENTGATE_MODEL": "mock-reviewer-v1",
    "AGENTGATE_API_KEY": "",
    "AGENTGATE_BASE_URL": "",
    "AGENTGATE_MOCK_LATENCY_MS": "60",
    "AGENTGATE_CONCURRENCY": "3",
    "AGENTGATE_MAX_ATTEMPTS": "4",
    "AGENTGATE_BACKOFF_BASE_S": "0.5",
    "AGENTGATE_BACKOFF_MAX_S": "20",
    "AGENTGATE_MAX_OUTPUT_TOKENS": "1400",
    "AGENTGATE_TOKEN_BUDGET_PER_RUN": "120000",
    "AGENTGATE_MAX_FINDINGS": "20",
    "AGENTGATE_FAIL_ON": "block",
    "AGENTGATE_EVAL_MIN_DETECTION": "0.40",
    "AGENTGATE_EVAL_LINE_TOLERANCE": "3",
}


@pytest.fixture(scope="session", autouse=True)
def hermetic_session(tmp_path_factory):
    """Pin the entire session offline before any other fixture, of any scope, runs."""
    mp = pytest.MonkeyPatch()
    for key, value in OFFLINE_ENV.items():
        mp.setenv(key, value)

    session_runs = tmp_path_factory.mktemp("runs")
    mp.setenv("AGENTGATE_TRACE_FILE", str(session_runs / "traces.jsonl"))
    mp.setenv("AGENTGATE_REVIEW_FILE", str(session_runs / "reviews.jsonl"))

    # Ignore any developer .env entirely, so results never depend on local config.
    mp.setattr(
        "pydantic_settings.sources.DotEnvSettingsSource._read_env_files",
        lambda self: {},
        raising=False,
    )

    from agentgate import config, llm

    config.get_settings.cache_clear()
    llm.reset_provider_cache()
    yield
    mp.undo()
    config.get_settings.cache_clear()
    llm.reset_provider_cache()


@pytest.fixture(autouse=True)
def offline_env(tmp_path, monkeypatch, hermetic_session):
    """Per-test isolation: a fresh trace log and a clean settings cache."""
    for key, value in OFFLINE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AGENTGATE_TRACE_FILE", str(tmp_path / "traces.jsonl"))
    monkeypatch.setenv("AGENTGATE_REVIEW_FILE", str(tmp_path / "reviews.jsonl"))

    from agentgate import config, llm

    config.get_settings.cache_clear()
    llm.reset_provider_cache()
    yield
    config.get_settings.cache_clear()
    llm.reset_provider_cache()


@pytest.fixture(autouse=True)
def no_live_provider():
    """Canary: fail loudly rather than silently billing a real provider.

    Without this, a misconfigured fixture spends money and the only symptom is a
    slow test run.
    """
    yield
    from agentgate.config import get_settings

    settings = get_settings()
    assert settings.provider == "mock" or not settings.api_key, (
        f"a test finished with a live provider configured "
        f"({settings.provider}:{settings.model}) -- tests must never call a real API"
    )
