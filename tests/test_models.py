"""Data-model invariants. Everything else depends on these holding."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentgate.config import cost_usd, price_for
from agentgate.models import (
    AgentVerdict,
    Category,
    Finding,
    LLMResponse,
    ReviewResult,
    RunTrace,
    Severity,
    finding_id,
)


def _finding(**kw):
    base = dict(
        file="a/b.py",
        line=12,
        category=Category.SECURITY,
        severity=Severity.HIGH,
        rule="sql-string-interpolation",
        message="Interpolated SQL.",
        suggestion="Parameterise it.",
        confidence=0.8,
    )
    base.update(kw)
    return Finding(**base)


def test_finding_id_is_stable_and_derived_from_file_line_rule():
    a = _finding()
    b = _finding(message="different wording entirely", confidence=0.1)
    assert a.id == b.id == finding_id("a/b.py", 12, "sql-string-interpolation")
    assert len(a.id) == 12


def test_finding_id_changes_when_the_location_changes():
    assert _finding().id != _finding(line=13).id
    assert _finding().id != _finding(file="a/c.py").id
    assert _finding().id != _finding(rule="command-injection").id


def test_explicit_id_is_preserved():
    assert _finding(id="deadbeef1234").id == "deadbeef1234"


def test_rule_is_slugified():
    assert _finding(rule="  SQL String Interpolation  ").rule == "sql-string-interpolation"


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_confidence_is_bounded(bad):
    with pytest.raises(ValidationError):
        _finding(confidence=bad)


def test_negative_line_is_rejected():
    with pytest.raises(ValidationError):
        _finding(line=-1)


def test_agent_verdict_defaults_to_empty():
    v = AgentVerdict(agent="security")
    assert v.findings == [] and v.notes is None


def test_review_result_counts_by_severity():
    result = ReviewResult(
        run_id="r1",
        verdict="block",
        findings=[
            _finding(line=1, severity=Severity.BLOCKER),
            _finding(line=2, severity=Severity.HIGH),
            _finding(line=3, severity=Severity.HIGH),
        ],
    )
    counts = result.counts_by_severity()
    assert counts["blocker"] == 1
    assert counts["high"] == 2
    assert counts["low"] == 0


def test_llm_response_total_tokens():
    assert LLMResponse(text="{}", input_tokens=100, output_tokens=40).total_tokens == 140


def test_run_trace_accumulates_tokens_cost_and_retries():
    from agentgate.models import NodeTrace

    trace = RunTrace(run_id="r1")
    for i in range(3):
        trace.add(
            NodeTrace(
                run_id="r1",
                node=f"n{i}",
                started_at="t0",
                ended_at="t1",
                duration_ms=100,
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.001,
                retry_count=1,
            )
        )
    assert trace.total_tokens == 45
    assert trace.total_cost_usd == pytest.approx(0.003)
    assert trace.duration_ms == 300
    assert trace.retry_count == 3


def test_known_model_is_priced_from_the_table():
    assert price_for("anthropic", "claude-haiku-4-5-20251001") == (1.00, 5.00)
    # 1M in + 1M out at (1.00, 5.00)
    assert cost_usd("anthropic", "claude-haiku-4-5-20251001", 1_000_000, 1_000_000) == 6.00


def test_unknown_model_costs_zero_and_does_not_raise():
    assert price_for("openai_compat", "no-such-model-9000") is None
    assert cost_usd("openai_compat", "no-such-model-9000", 5000, 5000) == 0.0


def test_mock_provider_is_free():
    assert cost_usd("mock", "mock-reviewer-v1", 10_000, 10_000) == 0.0


# --------------------------------------------------------------------------- #
# Alias models are never priced
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "model",
    [
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-3-flash-preview",
        "some-model-exp",
    ],
)
def test_a_moving_alias_is_never_priced(model):
    """A repointed alias would make the cost silently wrong, which is worse than
    reporting it as absent."""
    from agentgate.config import is_alias

    assert is_alias(model)
    assert price_for("openai_compat", model) is None
    assert cost_usd("openai_compat", model, 1_000_000, 1_000_000) == 0.0


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gemini-3.6-flash", (0.75, 3.75)),
        ("gemini-3.5-flash-lite", (0.30, 2.50)),
        ("gemini-2.5-flash-lite", (0.10, 0.40)),
    ],
)
def test_pinned_gemini_models_are_priced(model, expected):
    from agentgate.config import is_alias

    assert not is_alias(model)
    assert price_for("openai_compat", model) == expected


def test_the_retired_gemini_2_0_flash_is_not_in_the_table():
    """It 404s on the live API; keeping a price for it would imply it still works."""
    assert price_for("openai_compat", "gemini-2.0-flash") is None


def test_an_alias_warns_that_it_cannot_be_priced(caplog):
    import asyncio

    from agentgate import llm
    from agentgate.llm.base import LLMProvider
    from agentgate.models import LLMResponse

    class AliasProvider(LLMProvider):
        name = "openai_compat"

        async def raw_complete(self, messages, **kw) -> LLMResponse:
            return LLMResponse(
                text="{}",
                input_tokens=10,
                output_tokens=10,
                model="gemini-flash-lite-latest",
                provider=self.name,
            )

    llm._WARNED_MODELS.discard("openai_compat:gemini-flash-lite-latest")
    with caplog.at_level("WARNING", logger="agentgate.llm"):
        asyncio.run(
            llm.complete(
                [{"role": "user", "content": "x"}],
                provider=AliasProvider(model="gemini-flash-lite-latest"),
            )
        )
    assert any("moving alias" in r.message for r in caplog.records)
    assert any("Pin a concrete model id" in r.message for r in caplog.records)
