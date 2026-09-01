"""Aggregation: dedupe across agents, corroboration, ranking, cap and verdict."""

from __future__ import annotations

import pytest

from agentgate import config
from agentgate.models import AgentVerdict, Category, Finding, Severity
from agentgate.nodes.aggregator import (
    aggregator_node,
    decide_verdict,
    merge_findings,
    rank,
)
from agentgate.sanitizer import InjectionMatch


def make(file="a.py", line=10, rule="r", severity=Severity.MEDIUM, confidence=0.5,
         category=Category.SECURITY, message="m", agents=None):
    return Finding(
        file=file,
        line=line,
        category=category,
        severity=severity,
        rule=rule,
        message=message,
        suggestion="s",
        confidence=confidence,
        agents=agents or [],
    )


def verdicts(*pairs):
    return [AgentVerdict(agent=a, findings=list(fs)) for a, fs in pairs]


# --------------------------------------------------------------------------- #
# Dedupe and corroboration
# --------------------------------------------------------------------------- #
def test_the_same_file_line_rule_from_two_agents_becomes_one_finding():
    merged = merge_findings(
        verdicts(
            ("security", [make(confidence=0.6)]),
            ("correctness", [make(confidence=0.7, category=Category.CORRECTNESS)]),
        )
    )
    assert len(merged) == 1


def test_agreement_between_agents_raises_confidence():
    merged = merge_findings(
        verdicts(("security", [make(confidence=0.6)]), ("tests", [make(confidence=0.7)]))
    )
    assert merged[0].confidence == pytest.approx(0.8)


def test_confidence_is_capped_below_certainty():
    merged = merge_findings(
        verdicts(("security", [make(confidence=0.95)]), ("tests", [make(confidence=0.97)]))
    )
    assert merged[0].confidence == 0.99


def test_a_merged_finding_records_every_agent_that_reported_it():
    merged = merge_findings(
        verdicts(("security", [make()]), ("tests", [make()]), ("correctness", [make()]))
    )
    assert merged[0].agents == ["correctness", "security", "tests"]


def test_the_strongest_severity_wins_on_merge():
    merged = merge_findings(
        verdicts(
            ("security", [make(severity=Severity.LOW)]),
            ("correctness", [make(severity=Severity.BLOCKER)]),
        )
    )
    assert merged[0].severity is Severity.BLOCKER


def test_a_weaker_severity_does_not_downgrade_the_merged_finding():
    merged = merge_findings(
        verdicts(
            ("security", [make(severity=Severity.BLOCKER)]),
            ("correctness", [make(severity=Severity.INFO)]),
        )
    )
    assert merged[0].severity is Severity.BLOCKER


def test_different_lines_are_not_merged():
    merged = merge_findings(verdicts(("security", [make(line=10), make(line=11)])))
    assert len(merged) == 2


def test_different_rules_on_the_same_line_are_not_merged():
    merged = merge_findings(
        verdicts(("security", [make(rule="a"), make(rule="b")]))
    )
    assert len(merged) == 2


def test_the_more_detailed_message_survives_the_merge():
    short = make(message="Bad.")
    long = make(message="Bad because the value is interpolated into the query string.")
    merged = merge_findings(verdicts(("security", [short]), ("tests", [long])))
    assert merged[0].message == long.message


def test_merging_no_verdicts_yields_nothing():
    assert merge_findings([]) == []


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def test_ranking_is_by_severity_first():
    ranked = rank(
        [
            make(line=1, severity=Severity.LOW, confidence=0.99),
            make(line=2, severity=Severity.BLOCKER, confidence=0.10),
            make(line=3, severity=Severity.MEDIUM, confidence=0.99),
        ]
    )
    assert [f.severity for f in ranked] == [Severity.BLOCKER, Severity.MEDIUM, Severity.LOW]


def test_confidence_breaks_ties_within_a_severity():
    ranked = rank(
        [
            make(line=1, severity=Severity.HIGH, confidence=0.40),
            make(line=2, severity=Severity.HIGH, confidence=0.90),
        ]
    )
    assert [f.confidence for f in ranked] == [0.90, 0.40]


def test_ranking_is_stable_for_identical_severity_and_confidence():
    findings = [
        make(file="b.py", line=2, rule="z"),
        make(file="a.py", line=1, rule="a"),
    ]
    assert [f.file for f in rank(findings)] == ["a.py", "b.py"]


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "severity,expected",
    [
        (Severity.BLOCKER, "block"),
        (Severity.HIGH, "block"),
        (Severity.MEDIUM, "comment"),
        (Severity.LOW, "comment"),
        (Severity.INFO, "approve"),
    ],
)
def test_the_verdict_follows_the_worst_severity_present(severity, expected):
    assert decide_verdict([make(severity=severity)]) == expected


def test_no_findings_means_approve():
    assert decide_verdict([]) == "approve"


def test_one_blocker_among_many_low_findings_still_blocks():
    findings = [make(line=i, severity=Severity.LOW) for i in range(10)]
    findings.append(make(line=99, severity=Severity.BLOCKER))
    assert decide_verdict(findings) == "block"


# --------------------------------------------------------------------------- #
# The node itself
# --------------------------------------------------------------------------- #
async def test_findings_are_capped_at_the_configured_maximum(monkeypatch):
    monkeypatch.setenv("AGENTGATE_MAX_FINDINGS", "5")
    config.get_settings.cache_clear()

    many = [make(line=i, severity=Severity.LOW) for i in range(30)]
    out = await aggregator_node({"agents": verdicts(("security", many))})
    assert len(out["findings"]) == 5


async def test_the_cap_keeps_the_most_severe_findings(monkeypatch):
    monkeypatch.setenv("AGENTGATE_MAX_FINDINGS", "2")
    config.get_settings.cache_clear()

    findings = [make(line=i, severity=Severity.INFO) for i in range(10)]
    findings.append(make(line=50, severity=Severity.BLOCKER))
    findings.append(make(line=51, severity=Severity.HIGH))

    out = await aggregator_node({"agents": verdicts(("security", findings))})
    assert [f.severity for f in out["findings"]] == [Severity.BLOCKER, Severity.HIGH]


async def test_a_detected_injection_is_injected_as_a_blocker_and_ranked_first():
    matches = [InjectionMatch("override-previous-instructions", "app/x.py", 7, "...")]
    out = await aggregator_node(
        {
            "agents": verdicts(("security", [make(severity=Severity.LOW)])),
            "injection_matches": matches,
        }
    )
    assert out["verdict"] == "block"
    assert out["findings"][0].rule == "prompt-injection-in-diff"
    assert out["findings"][0].file == "app/x.py"


async def test_the_injection_finding_is_not_duplicated_by_an_agent_reporting_it():
    matches = [InjectionMatch("role-reassignment", "app/x.py", 7, "...")]
    agent_copy = make(
        file="app/x.py", line=7, rule="prompt-injection-in-diff", severity=Severity.BLOCKER
    )
    out = await aggregator_node(
        {
            "agents": verdicts(("security", [agent_copy])),
            "injection_matches": matches,
        }
    )
    injections = [f for f in out["findings"] if f.rule == "prompt-injection-in-diff"]
    assert len(injections) == 1
    assert injections[0].agents == ["sanitizer"]


async def test_an_aggregation_with_nothing_to_aggregate_approves():
    out = await aggregator_node({"agents": []})
    assert out == {"findings": [], "verdict": "approve"}
