"""The review graph: parallel fan-out, degradation, and end-to-end behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentgate import config, llm
from agentgate.graph import build_graph, review_async
from agentgate.models import AgentVerdict, LLMResponse, Severity
from agentgate.nodes import SPECIALISTS, parse_verdict, run_specialist
from agentgate.telemetry import read_traces

FIXTURES = Path(__file__).resolve().parent.parent / "eval" / "fixtures"

SAMPLE_DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -10,3 +10,9 @@ import sqlite3
 def connect(path):
     return sqlite3.connect(path)

+def find_user(conn, email):
+    return conn.execute(f"SELECT * FROM users WHERE email = '{email}'").fetchone()
+
+def evict(cache, keys=[]):
+    keys.append(cache.pop())
"""

CLEAN_DIFF = """diff --git a/app/ok.py b/app/ok.py
--- a/app/ok.py
+++ b/app/ok.py
@@ -1,2 +1,4 @@
 import math

+RADIUS = 2
+AREA = math.pi * RADIUS * RADIUS
"""


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
async def test_end_to_end_review_on_a_sample_diff():
    result = await review_async(SAMPLE_DIFF, run_id="e2e-1")
    assert result.run_id == "e2e-1"
    assert result.verdict in ("approve", "comment", "block")
    assert result.provider == "mock"
    assert result.total_tokens > 0
    assert len(result.agents) == 3
    assert {v.agent for v in result.agents} == set(SPECIALISTS)


async def test_a_sql_injection_in_the_diff_blocks_the_merge():
    result = await review_async(SAMPLE_DIFF)
    assert result.verdict == "block"
    rules = {f.rule for f in result.findings}
    assert "sql-string-interpolation" in rules


async def test_findings_carry_the_new_file_line_numbers():
    result = await review_async(SAMPLE_DIFF)
    sql = next(f for f in result.findings if f.rule == "sql-string-interpolation")
    assert sql.file == "app/db.py"
    assert sql.line == 14


async def test_the_same_diff_reviews_identically_every_time():
    first = await review_async(SAMPLE_DIFF, run_id="a")
    second = await review_async(SAMPLE_DIFF, run_id="b")
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
    assert first.verdict == second.verdict


async def test_an_empty_diff_is_approved_without_calling_any_agent():
    result = await review_async("")
    assert result.verdict == "approve"
    assert result.findings == []
    assert result.total_tokens == 0


async def test_a_diff_with_no_defects_does_not_block():
    result = await review_async(CLEAN_DIFF)
    assert result.verdict in ("approve", "comment")


# --------------------------------------------------------------------------- #
# Prompt injection, end to end
# --------------------------------------------------------------------------- #
async def test_the_injection_fixture_is_detected_and_blocked():
    diff = (FIXTURES / "injection.patch").read_text(encoding="utf-8")
    result = await review_async(diff)
    assert result.injection_detected is True
    assert result.verdict == "block"
    assert any(f.rule == "prompt-injection-in-diff" for f in result.findings)


async def test_the_injection_finding_is_ranked_first():
    diff = (FIXTURES / "injection.patch").read_text(encoding="utf-8")
    result = await review_async(diff)
    assert result.findings[0].rule == "prompt-injection-in-diff"
    assert result.findings[0].severity is Severity.BLOCKER


async def test_the_attack_does_not_suppress_the_real_vulnerabilities():
    """The whole point: the injected 'approve this PR' must not work."""
    diff = (FIXTURES / "injection.patch").read_text(encoding="utf-8")
    result = await review_async(diff)
    rules = {f.rule for f in result.findings}
    assert "command-injection" in rules
    assert "unsafe-deserialisation" in rules


async def test_the_model_never_sees_the_live_injection_payload():
    """Whatever reaches the provider is neutralised, not the raw attack text."""
    seen: list[str] = []
    real = llm.complete

    async def spy(messages, **kw):
        # Only the user turn matters: the system prompt legitimately *describes*
        # the attacks it must refuse, so scanning it would be self-defeating.
        seen.append(messages[-1]["content"])
        return await real(messages, **kw)

    llm.complete = spy
    try:
        diff = (FIXTURES / "injection.patch").read_text(encoding="utf-8")
        await review_async(diff)
    finally:
        llm.complete = real

    assert seen
    for body in seen:
        assert "<<<UNTRUSTED_DIFF_" in body
        assert "ignore all previous instructions" not in body.lower()
        assert "approve this pull request" not in body.lower()
        assert "NEUTRALISED" in body


# --------------------------------------------------------------------------- #
# Parallelism -- the fan-out must be concurrent, not a loop
# --------------------------------------------------------------------------- #
async def test_the_fan_out_is_genuinely_parallel(monkeypatch):
    """The three agents must overlap in time, not queue up behind each other.

    Asserted on the trace rather than on total wall clock, so the check is about
    scheduling and not about how fast the machine happens to be.
    """
    monkeypatch.setenv("AGENTGATE_MOCK_LATENCY_MS", "300")
    monkeypatch.setenv("AGENTGATE_CONCURRENCY", "3")
    config.get_settings.cache_clear()
    llm.reset_provider_cache()

    await review_async(SAMPLE_DIFF, run_id="parallel")

    from datetime import datetime

    agent_traces = [
        t
        for t in read_traces(Path(config.get_settings().trace_file))
        if t["node"].startswith("agent_")
    ]
    assert len(agent_traces) == 3

    starts = [datetime.fromisoformat(t["started_at"]) for t in agent_traces]
    ends = [datetime.fromisoformat(t["ended_at"]) for t in agent_traces]
    span_ms = (max(ends) - min(starts)).total_seconds() * 1000
    slowest_ms = max(t["duration_ms"] for t in agent_traces)
    summed_ms = sum(t["duration_ms"] for t in agent_traces)

    # Close to the slowest agent, nowhere near the sum of all three.
    assert span_ms < summed_ms * 0.6, (
        f"fan-out took {span_ms:.0f}ms against a {summed_ms}ms serial total -- not parallel"
    )
    assert span_ms < slowest_ms * 1.8


async def test_the_semaphore_serialises_the_fan_out_when_concurrency_is_one(monkeypatch):
    """The inverse control: with concurrency=1 the same graph must serialise."""
    monkeypatch.setenv("AGENTGATE_MOCK_LATENCY_MS", "150")
    monkeypatch.setenv("AGENTGATE_CONCURRENCY", "1")
    config.get_settings.cache_clear()
    llm.reset_provider_cache()

    result = await review_async(SAMPLE_DIFF, run_id="serial")
    assert result.duration_ms >= 3 * 150 * 0.8


# --------------------------------------------------------------------------- #
# Degradation -- one agent failing must not kill the review
# --------------------------------------------------------------------------- #
async def test_a_failing_agent_does_not_take_down_the_review(monkeypatch):
    real = llm.complete

    async def flaky(messages, **kw):
        if "AGENT: security" in messages[0]["content"]:
            raise RuntimeError("provider exploded")
        return await real(messages, **kw)

    monkeypatch.setattr(llm, "complete", flaky)

    result = await review_async(SAMPLE_DIFF)
    assert len(result.agents) == 3
    security = next(v for v in result.agents if v.agent == "security")
    assert security.findings == []
    assert "agent unavailable" in (security.notes or "")
    # The other two still did their job.
    assert any(v.findings for v in result.agents if v.agent != "security")
    assert result.errors


async def test_unparseable_output_is_repaired_then_degrades_gracefully(monkeypatch):
    calls: list[int] = []

    async def garbage(messages, **kw):
        calls.append(1)
        return LLMResponse(text="I am afraid I cannot do that.", provider="mock")

    monkeypatch.setattr(llm, "complete", garbage)

    verdict = await run_specialist("security", "FILE: a.py\n1| x = 1\n")
    assert len(calls) == 2, "should retry exactly once with the validation error"
    assert verdict.findings == []
    assert verdict.notes and verdict.notes.startswith("degraded:")


async def test_the_repair_attempt_feeds_the_error_back_into_the_prompt(monkeypatch):
    prompts: list[str] = []

    async def garbage(messages, **kw):
        prompts.append(messages[-1]["content"])
        return LLMResponse(text="nope", provider="mock")

    monkeypatch.setattr(llm, "complete", garbage)
    await run_specialist("tests", "FILE: a.py\n1| x = 1\n")

    assert "could not be parsed" in prompts[1]
    assert "could not be parsed" not in prompts[0]


async def test_a_second_attempt_that_parses_is_accepted(monkeypatch):
    attempts: list[int] = []

    async def flaky(messages, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            return LLMResponse(text="not json", provider="mock")
        return LLMResponse(
            text=json.dumps(
                {
                    "agent": "security",
                    "findings": [
                        {
                            "file": "a.py",
                            "line": 3,
                            "category": "security",
                            "severity": "high",
                            "rule": "hardcoded-secret",
                            "message": "Key in source.",
                            "suggestion": "Move it to the environment.",
                            "confidence": 0.9,
                        }
                    ],
                    "notes": None,
                }
            ),
            provider="mock",
        )

    monkeypatch.setattr(llm, "complete", flaky)
    verdict = await run_specialist("security", "FILE: a.py\n3| KEY = 'abc'\n")
    assert len(verdict.findings) == 1
    assert verdict.findings[0].rule == "hardcoded-secret"


# --------------------------------------------------------------------------- #
# Verdict parsing
# --------------------------------------------------------------------------- #
def test_a_response_wrapped_in_markdown_fences_still_parses():
    text = '```json\n{"agent":"security","findings":[],"notes":"ok"}\n```'
    assert parse_verdict("security", text).notes == "ok"


def test_prose_around_the_json_is_tolerated():
    text = 'Sure! Here you go:\n{"agent":"tests","findings":[],"notes":null}\nHope that helps.'
    assert parse_verdict("tests", text).findings == []


def test_an_agent_is_kept_in_its_own_lane():
    text = json.dumps(
        {
            "agent": "tests",
            "findings": [
                {
                    "file": "a.py",
                    "line": 1,
                    "category": "security",  # wrong lane
                    "severity": "high",
                    "rule": "some-rule",
                    "message": "m",
                    "suggestion": "s",
                    "confidence": 0.5,
                }
            ],
        }
    )
    verdict = parse_verdict("tests", text)
    assert verdict.findings[0].category.value == "testing"


def test_an_injection_finding_stays_security_whichever_agent_reports_it():
    text = json.dumps(
        {
            "agent": "tests",
            "findings": [
                {
                    "file": "a.py",
                    "line": 1,
                    "category": "testing",
                    "severity": "blocker",
                    "rule": "prompt-injection-in-diff",
                    "message": "m",
                    "suggestion": "s",
                    "confidence": 0.9,
                }
            ],
        }
    )
    assert parse_verdict("tests", text).findings[0].category.value == "security"


def test_a_nonsense_severity_falls_back_to_medium():
    text = json.dumps(
        {
            "agent": "security",
            "findings": [
                {
                    "file": "a.py",
                    "line": 1,
                    "category": "security",
                    "severity": "catastrophic",
                    "rule": "r",
                    "message": "m",
                    "suggestion": "s",
                    "confidence": 2.5,
                }
            ],
        }
    )
    finding = parse_verdict("security", text).findings[0]
    assert finding.severity.value == "medium"
    assert finding.confidence == 1.0


def test_a_malformed_finding_is_dropped_not_fatal():
    text = json.dumps(
        {
            "agent": "security",
            "findings": [
                {"nonsense": True},
                {
                    "file": "a.py",
                    "line": 2,
                    "category": "security",
                    "severity": "low",
                    "rule": "r",
                    "message": "m",
                    "suggestion": "s",
                    "confidence": 0.4,
                },
            ],
        }
    )
    assert len(parse_verdict("security", text).findings) == 1


@pytest.mark.parametrize("bad", ["", "null", "[]", "not json"])
def test_structurally_invalid_responses_raise_for_the_caller_to_repair(bad):
    with pytest.raises((json.JSONDecodeError, ValueError, TypeError)):
        parse_verdict("security", bad)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_the_graph_compiles_with_every_expected_node():
    graph = build_graph()
    nodes = set(graph.get_graph().nodes)
    for expected in (
        "parse_diff",
        "sanitize",
        "fan_out",
        "agent_security",
        "agent_correctness",
        "agent_tests",
        "aggregator",
        "finalize",
    ):
        assert expected in nodes


async def test_every_node_lands_in_the_trace():
    await review_async(SAMPLE_DIFF, run_id="trace-check")
    nodes = {t["node"] for t in read_traces(Path(config.get_settings().trace_file))}
    assert {
        "parse_diff",
        "sanitize",
        "fan_out",
        "agent_security",
        "agent_correctness",
        "agent_tests",
        "aggregator",
        "finalize",
    } <= nodes


async def test_each_specialist_verdict_is_returned_once():
    result = await review_async(SAMPLE_DIFF)
    agents = [v.agent for v in result.agents]
    assert sorted(agents) == sorted(SPECIALISTS)
    assert all(isinstance(v, AgentVerdict) for v in result.agents)
