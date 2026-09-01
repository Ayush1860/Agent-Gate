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
