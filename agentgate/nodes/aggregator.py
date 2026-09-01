"""Merge the three specialist verdicts into one ranked, deduplicated result.

Agreement between independent agents is evidence, so a finding two agents both
report comes out with higher confidence than either reported alone.
"""

from __future__ import annotations

import logging

from ..config import get_settings
from ..models import SEVERITY_ORDER, AgentVerdict, Finding, Severity
from ..sanitizer import injection_finding
from ..telemetry import traced
from . import ReviewState

log = logging.getLogger("agentgate.aggregator")

# Reported by any agent, this is always a blocker -- see sanitizer.py.
BLOCKING_SEVERITIES = (Severity.BLOCKER, Severity.HIGH)
COMMENTING_SEVERITIES = (Severity.MEDIUM, Severity.LOW)


def merge_findings(verdicts: list[AgentVerdict]) -> list[Finding]:
    """Collapse duplicates on (file, line, rule), keeping the strongest reading."""
    merged: dict[tuple[str, int, str], Finding] = {}

    for verdict in verdicts:
        for finding in verdict.findings:
            agent = verdict.agent
            existing = merged.get(finding.key)
            if existing is None:
                clone = finding.model_copy(deep=True)
                clone.agents = sorted(set(clone.agents or []) | {agent})
                merged[finding.key] = clone
                continue

            # Two agents pointing at the same line is corroboration, not noise.
            if agent not in existing.agents:
                existing.agents = sorted(set(existing.agents) | {agent})
            if SEVERITY_ORDER[finding.severity] < SEVERITY_ORDER[existing.severity]:
                existing.severity = finding.severity
            existing.confidence = min(
                0.99,
                round(max(existing.confidence, finding.confidence) + 0.10, 4),
            )
            if len(finding.message) > len(existing.message):
                existing.message = finding.message
                existing.suggestion = finding.suggestion or existing.suggestion

    return list(merged.values())


def rank(findings: list[Finding]) -> list[Finding]:
    """Severity first, then confidence, then a stable tiebreak on location."""
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_ORDER[f.severity],
            -f.confidence,
            f.file,
            f.line,
            f.rule,
        ),
    )


def decide_verdict(findings: list[Finding]) -> str:
    """``block`` on blocker/high, ``comment`` on medium/low, otherwise ``approve``."""
    severities = {f.severity for f in findings}
    if severities & set(BLOCKING_SEVERITIES):
        return "block"
    if severities & set(COMMENTING_SEVERITIES):
        return "comment"
    return "approve"


@traced("aggregator")
async def aggregator_node(state: ReviewState) -> dict:
    settings = get_settings()
    verdicts: list[AgentVerdict] = list(state.get("agents") or [])

    findings = merge_findings(verdicts)

    # The sanitizer's own finding is authoritative and is added here rather than
    # relying on an agent to have noticed the attack.
    matches = list(state.get("injection_matches") or [])
    if matches:
        attack = injection_finding(matches)
        findings = [f for f in findings if f.key != attack.key]
        findings.insert(0, attack)

    ranked = rank(findings)
    capped = ranked[: settings.max_findings]
    if len(ranked) > len(capped):
        log.info(
            "capped findings at %d (%d were produced)", settings.max_findings, len(ranked)
        )

    return {"findings": capped, "verdict": decide_verdict(capped)}
