"""The shared LLM call path: determinism, backoff, concurrency and the budget.

Nothing here touches the network. The retry tests drive a purpose-built flaky
provider and inject a fake sleeper, so the suite stays fast.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentgate import config, llm
from agentgate.llm.base import (
    FatalLLMError,
    LLMProvider,
    RateLimitError,
    TransientError,
)
from agentgate.models import LLMResponse
from agentgate.telemetry import BudgetExceeded, new_run

SAMPLE_PROMPT = [
    {"role": "system", "content": "AGENT: security\nYou review diffs."},
    {
        "role": "user",
        "content": (
            "FILE: app/db.py\n"
            "  10| def get_user(conn, name):\n"
            '  11|     return conn.execute(f"SELECT * FROM users WHERE name = \'{name}\'")\n'
        ),
    },
]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_get_provider_reads_the_environment():
    provider = llm.get_provider()
    assert provider.name == "mock"


def test_unknown_provider_is_a_clear_error():
    with pytest.raises(FatalLLMError, match="unknown provider"):
        llm.get_provider("does-not-exist")


def test_openai_compat_requires_a_base_url():
    with pytest.raises(FatalLLMError, match="AGENTGATE_BASE_URL"):
        llm.get_provider("openai_compat", model="x", api_key="k", base_url="")


def test_anthropic_requires_a_key():
    with pytest.raises(FatalLLMError, match="AGENTGATE_API_KEY"):
        llm.get_provider("anthropic", model="claude-haiku-4-5-20251001", api_key="")


# --------------------------------------------------------------------------- #
# Mock provider
# --------------------------------------------------------------------------- #
async def test_mock_is_deterministic_for_the_same_input():
    provider = llm.get_provider()
    first = await provider.raw_complete(SAMPLE_PROMPT)
    second = await provider.raw_complete(SAMPLE_PROMPT)
    assert first.text == second.text


async def test_mock_output_parses_as_an_agent_verdict():
    from agentgate.models import AgentVerdict

    resp = await llm.get_provider().raw_complete(SAMPLE_PROMPT)
    verdict = AgentVerdict.model_validate_json(resp.text)
    assert verdict.agent == "security"
    assert any(f.rule == "sql-string-interpolation" for f in verdict.findings)


async def test_mock_respects_the_requesting_agent():
    prompt = [
        {"role": "system", "content": "AGENT: correctness\n"},
        {
            "role": "user",
            "content": "FILE: app/x.py\n  4| def f(items=[]):\n  5|     items.append(1)\n",
        },
    ]
    resp = await llm.get_provider().raw_complete(prompt)
    payload = json.loads(resp.text)
    assert payload["agent"] == "correctness"
    assert all(f["category"] == "correctness" for f in payload["findings"])


async def test_mock_returns_no_findings_on_clean_code():
    prompt = [
        {"role": "system", "content": "AGENT: security\n"},
        {"role": "user", "content": "FILE: app/ok.py\n  1| x = 1\n  2| y = x + 1\n"},
    ]
    payload = json.loads((await llm.get_provider().raw_complete(prompt)).text)
    assert payload["findings"] == []
    assert payload["notes"]


async def test_mock_reports_token_usage():
    resp = await llm.get_provider().raw_complete(SAMPLE_PROMPT)
    assert resp.input_tokens > 0 and resp.output_tokens > 0
    assert resp.provider == "mock"


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #
class FlakyProvider(LLMProvider):
    """Fails ``failures`` times with ``exc``, then succeeds."""

    name = "mock"  # priced as free so cost accounting stays quiet

    def __init__(self, failures: int, exc: Exception | None = None) -> None:
        super().__init__(model="mock-reviewer-v1")
        self.failures = failures
        self.calls = 0
        self.exc = exc or RateLimitError("429 Too Many Requests")

    async def raw_complete(self, messages, **kw) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return LLMResponse(
            text='{"agent":"security","findings":[],"notes":null}',
            input_tokens=10,
            output_tokens=5,
            model=self.model,
            provider=self.name,
        )


@pytest.fixture
def fake_sleep():
    """Records requested delays instead of waiting for them."""
    delays: list[float] = []

    async def _sleep(seconds: float) -> None:
        delays.append(seconds)

    _sleep.delays = delays  # type: ignore[attr-defined]
    return _sleep


async def test_a_429_is_survived_by_backoff_not_a_crash(fake_sleep):
    provider = FlakyProvider(failures=2)
    resp = await llm.complete(SAMPLE_PROMPT, provider=provider, sleeper=fake_sleep)
    assert provider.calls == 3
    assert resp.retry_count == 2
    assert len(fake_sleep.delays) == 2


async def test_backoff_windows_grow_exponentially(monkeypatch, fake_sleep):
    # Remove jitter so the growth is observable rather than probabilistic.
    monkeypatch.setattr(llm.random, "uniform", lambda _lo, hi: hi)
    monkeypatch.setenv("AGENTGATE_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("AGENTGATE_BACKOFF_BASE_S", "1.0")
    config.get_settings.cache_clear()

    provider = FlakyProvider(failures=3)
    await llm.complete(SAMPLE_PROMPT, provider=provider, sleeper=fake_sleep)
    assert fake_sleep.delays == [1.0, 2.0, 4.0]


async def test_jitter_keeps_the_delay_inside_the_window(fake_sleep):
    provider = FlakyProvider(failures=3)
    await llm.complete(SAMPLE_PROMPT, provider=provider, sleeper=fake_sleep)
    settings = config.get_settings()
    for attempt, delay in enumerate(fake_sleep.delays, start=1):
        window = min(settings.backoff_max_s, settings.backoff_base_s * (2 ** (attempt - 1)))
        assert 0.0 <= delay <= window


async def test_retry_after_header_overrides_the_backoff_window(fake_sleep):
    provider = FlakyProvider(failures=1, exc=RateLimitError("429", retry_after=7.5))
    await llm.complete(SAMPLE_PROMPT, provider=provider, sleeper=fake_sleep)
    assert fake_sleep.delays == [7.5]


async def test_retry_after_is_capped_at_backoff_max(monkeypatch, fake_sleep):
    monkeypatch.setenv("AGENTGATE_BACKOFF_MAX_S", "5")
    config.get_settings.cache_clear()
    provider = FlakyProvider(failures=1, exc=RateLimitError("429", retry_after=600))
    await llm.complete(SAMPLE_PROMPT, provider=provider, sleeper=fake_sleep)
    assert fake_sleep.delays == [5.0]


async def test_5xx_is_also_retried(fake_sleep):
    provider = FlakyProvider(failures=2, exc=TransientError("503 upstream"))
    await llm.complete(SAMPLE_PROMPT, provider=provider, sleeper=fake_sleep)
    assert provider.calls == 3


async def test_retries_are_bounded_then_the_error_surfaces(fake_sleep):
    provider = FlakyProvider(failures=99)
    with pytest.raises(TransientError, match="failed after 4 attempts"):
        await llm.complete(SAMPLE_PROMPT, provider=provider, sleeper=fake_sleep)
    assert provider.calls == 4


async def test_fatal_errors_are_not_retried(fake_sleep):
    provider = FlakyProvider(failures=99, exc=FatalLLMError("401 bad key"))
    with pytest.raises(FatalLLMError):
        await llm.complete(SAMPLE_PROMPT, provider=provider, sleeper=fake_sleep)
    assert provider.calls == 1
    assert fake_sleep.delays == []


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
async def test_budget_abort_names_the_budget_and_the_consumption(monkeypatch):
    monkeypatch.setenv("AGENTGATE_TOKEN_BUDGET_PER_RUN", "100")
    config.get_settings.cache_clear()
    with new_run("budget-test"):
        with pytest.raises(BudgetExceeded) as excinfo:
            await llm.complete(SAMPLE_PROMPT)
    message = str(excinfo.value)
    assert "budget-test" in message
    assert "100" in message


async def test_budget_is_charged_as_calls_complete(monkeypatch):
    monkeypatch.setenv("AGENTGATE_TOKEN_BUDGET_PER_RUN", "1000000")
    config.get_settings.cache_clear()
    with new_run("charge-test") as ctx:
        assert ctx.tokens_consumed == 0
        resp = await llm.complete(SAMPLE_PROMPT)
        assert ctx.tokens_consumed == resp.total_tokens


async def test_a_zero_budget_means_unlimited(monkeypatch):
    monkeypatch.setenv("AGENTGATE_TOKEN_BUDGET_PER_RUN", "0")
    config.get_settings.cache_clear()
    with new_run("unlimited"):
        assert await llm.complete(SAMPLE_PROMPT)


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
class SlowProvider(LLMProvider):
    name = "mock"

    def __init__(self) -> None:
        super().__init__(model="mock-reviewer-v1")
        self.in_flight = 0
        self.peak = 0

    async def raw_complete(self, messages, **kw) -> LLMResponse:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.05)
            return LLMResponse(text="{}", input_tokens=1, output_tokens=1, provider=self.name)
        finally:
            self.in_flight -= 1


async def test_the_semaphore_caps_simultaneous_calls(monkeypatch):
    monkeypatch.setenv("AGENTGATE_CONCURRENCY", "2")
    config.get_settings.cache_clear()
    llm.reset_provider_cache()

    provider = SlowProvider()
    await asyncio.gather(*(llm.complete(SAMPLE_PROMPT, provider=provider) for _ in range(8)))
    assert provider.peak <= 2


async def test_cost_accounting_warns_once_for_an_unknown_model(caplog):
    class UnpricedProvider(LLMProvider):
        name = "openai_compat"

        async def raw_complete(self, messages, **kw) -> LLMResponse:
            return LLMResponse(
                text="{}",
                input_tokens=10,
                output_tokens=10,
                model="totally-unknown-model",
                provider=self.name,
            )

    llm._WARNED_MODELS.discard("openai_compat:totally-unknown-model")
    provider = UnpricedProvider(model="totally-unknown-model")
    with caplog.at_level("WARNING", logger="agentgate.llm"):
        await llm.complete(SAMPLE_PROMPT, provider=provider)
        await llm.complete(SAMPLE_PROMPT, provider=provider)
    assert sum("no price entry" in r.message for r in caplog.records) == 1
