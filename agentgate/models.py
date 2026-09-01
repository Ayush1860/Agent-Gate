"""Pydantic v2 data model. Everything else in AgentGate depends on this module."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Severity(str, Enum):
    BLOCKER = "blocker"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Category(str, Enum):
    SECURITY = "security"
    CORRECTNESS = "correctness"
    TESTING = "testing"


# Rank order used for sorting and for --fail-on threshold comparisons.
SEVERITY_ORDER: dict[Severity, int] = {
    Severity.BLOCKER: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


def finding_id(file: str, line: int, rule: str) -> str:
    """Stable 12-char id. Same defect in the same place always gets the same id."""
    raw = f"{file}:{line}:{rule}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


class Finding(BaseModel):
    """A single reviewer finding, anchored to a line in the new version of a file."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = ""
    file: str
    line: int = Field(ge=0)
    category: Category
    severity: Severity
    rule: str
    message: str
    suggestion: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # Which agents produced this finding. Populated by the aggregator on merge.
    agents: list[str] = Field(default_factory=list)

    @field_validator("rule")
    @classmethod
    def _slugish(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "-")[:64] or "unspecified"

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            # bypass validate_assignment recursion by writing through __dict__
            self.__dict__["id"] = finding_id(self.file, self.line, self.rule)

    @property
    def key(self) -> tuple[str, int, str]:
        """Dedupe key: same file, same line, same rule."""
        return (self.file, self.line, self.rule)


class AgentVerdict(BaseModel):
    """What one specialist agent returned. ``notes`` carries degradation reasons."""

    agent: str
    findings: list[Finding] = Field(default_factory=list)
    notes: str | None = None


class LLMResponse(BaseModel):
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    retry_count: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class DiffHunk(BaseModel):
    """One contiguous block of added/modified lines in the new version of a file."""

    file: str
    start_line: int
    added_lines: list[tuple[int, str]] = Field(default_factory=list)

    def render(self) -> str:
        return "\n".join(f"{n}| {text}" for n, text in self.added_lines)


class NodeTrace(BaseModel):
    """One graph-node execution. Serialised as a line in runs/traces.jsonl."""

    run_id: str
    node: str
    started_at: str
    ended_at: str
    duration_ms: int
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    retry_count: int = 0
    success: bool = True
    error: str | None = None


class RunTrace(BaseModel):
    """Accumulator for one review run. Owns the per-run token budget."""

    run_id: str
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    nodes: list[NodeTrace] = Field(default_factory=list)
    retry_count: int = 0

    @property
    def total_tokens(self) -> int:
        return sum(n.input_tokens + n.output_tokens for n in self.nodes)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(n.cost_usd for n in self.nodes), 6)

    @property
    def duration_ms(self) -> int:
        return sum(n.duration_ms for n in self.nodes)

    def add(self, node: NodeTrace) -> None:
        self.nodes.append(node)
        self.retry_count += node.retry_count


class ReviewResult(BaseModel):
    """The full output of one review. This is what the API and CLI return."""

    run_id: str
    verdict: Literal["approve", "comment", "block"]
    findings: list[Finding] = Field(default_factory=list)
    agents: list[AgentVerdict] = Field(default_factory=list)
    injection_detected: bool = False
    injection_patterns: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    duration_ms: int = 0
    errors: list[str] = Field(default_factory=list)

    def counts_by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {s.value: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.value] += 1
        return out
