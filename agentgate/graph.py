"""LangGraph wiring.

::

    parse_diff -> sanitize -> fan_out -> [security | correctness | tests] -> aggregator -> finalize
                                          (these three run concurrently)

The three specialists are wired as genuine parallel edges, not a loop. Wall-clock
time for the fan-out is close to the slowest single agent rather than the sum of
all three; ``tests/test_graph.py`` asserts exactly that.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from langgraph.graph import END, START, StateGraph

from .config import get_settings
from .diff import parse_diff as parse_unified_diff
from .models import ReviewResult
from .nodes import ReviewState
from .nodes.aggregator import aggregator_node
from .nodes.correctness import correctness_node
from .nodes.security import security_node
from .nodes.tests import tests_node
from .sanitizer import sanitize_hunks
from .telemetry import current_run, new_run, traced

log = logging.getLogger("agentgate.graph")


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
@traced("parse_diff")
async def parse_diff_node(state: ReviewState) -> dict:
    hunks = parse_unified_diff(state.get("diff_text", "") or "")
    log.info(
        "parsed %d hunk(s) across %d file(s)",
        len(hunks),
        len({h.file for h in hunks}),
    )
    return {"hunks": hunks}


@traced("sanitize")
async def sanitize_node(state: ReviewState) -> dict:
    result = sanitize_hunks(state.get("hunks") or [])
    if result.detected:
        log.warning(
            "prompt injection detected in diff: %s", ", ".join(result.patterns)
        )
    return {
        "payload": result.wrapped,
        "injection_detected": result.detected,
        "injection_patterns": result.patterns,
        "injection_matches": result.matches,
    }


@traced("fan_out")
async def fan_out_node(state: ReviewState) -> dict:
    """Branch point. Keeps the concurrent hand-off visible in the trace."""
    return {}


def route_after_fan_out(state: ReviewState) -> list[str]:
    """Fan out to all three specialists, or skip them when there is nothing to review.

    Returning a list is what makes LangGraph schedule the branches concurrently.
    """
    if not (state.get("hunks") or []):
        log.info("no reviewable lines in diff; skipping the specialist agents")
        return ["aggregator"]
    return ["agent_security", "agent_correctness", "agent_tests"]


@traced("finalize")
async def finalize_node(state: ReviewState) -> dict:
    findings = state.get("findings") or []
    verdict = state.get("verdict") or "approve"
    log.info(
        "verdict=%s findings=%d injection=%s",
        verdict,
        len(findings),
        bool(state.get("injection_detected")),
    )
    return {"findings": findings, "verdict": verdict}


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def build_graph():
    """Compile the review graph. Cheap enough to call per run."""
    builder = StateGraph(ReviewState)

    builder.add_node("parse_diff", parse_diff_node)
    builder.add_node("sanitize", sanitize_node)
    builder.add_node("fan_out", fan_out_node)
    builder.add_node("agent_security", security_node)
    builder.add_node("agent_correctness", correctness_node)
    builder.add_node("agent_tests", tests_node)
    builder.add_node("aggregator", aggregator_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "parse_diff")
    builder.add_edge("parse_diff", "sanitize")
    builder.add_edge("sanitize", "fan_out")

    # Parallel fan-out: one conditional edge returning several targets.
    builder.add_conditional_edges(
        "fan_out",
        route_after_fan_out,
        ["agent_security", "agent_correctness", "agent_tests", "aggregator"],
    )

    # Fan-in: the aggregator waits for every specialist that ran.
    builder.add_edge("agent_security", "aggregator")
    builder.add_edge("agent_correctness", "aggregator")
    builder.add_edge("agent_tests", "aggregator")

    builder.add_edge("aggregator", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile()


_COMPILED = None


def get_graph():
    """Process-wide compiled graph."""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph()
    return _COMPILED


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
async def review_async(
    diff_text: str,
    run_id: str | None = None,
    token_budget: int | None = None,
) -> ReviewResult:
    """Run one full review. Returns a :class:`ReviewResult`, never a partial dict."""
    settings = get_settings()
    rid = run_id or uuid.uuid4().hex[:12]

    started = time.perf_counter()
    with new_run(rid, token_budget=token_budget) as ctx:
        state = await get_graph().ainvoke(
            {
                "run_id": rid,
                "diff_text": diff_text,
                "agents": [],
                "errors": [],
            }
        )
        trace = ctx.trace
    wall_ms = int((time.perf_counter() - started) * 1000)

    return ReviewResult(
        run_id=rid,
        verdict=state.get("verdict", "approve"),
        findings=state.get("findings") or [],
        agents=state.get("agents") or [],
        injection_detected=bool(state.get("injection_detected")),
        injection_patterns=state.get("injection_patterns") or [],
        provider=settings.provider,
        model=settings.model,
        total_tokens=trace.total_tokens,
        total_cost_usd=trace.total_cost_usd,
        # Wall clock, not the sum of node durations -- the three specialists
        # overlap, so summing them would overstate how long the review took.
        duration_ms=wall_ms,
        errors=state.get("errors") or [],
    )


def review(
    diff_text: str,
    run_id: str | None = None,
    token_budget: int | None = None,
) -> ReviewResult:
    """Synchronous wrapper for the CLI and for tests."""
    if current_run() is not None:
        raise RuntimeError("review() called inside an active run; use review_async()")
    return asyncio.run(review_async(diff_text, run_id=run_id, token_budget=token_budget))
