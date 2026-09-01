"""The correctness specialist agent node.

Off-by-one, None handling, coroutines, leaks, races.

All of the call/parse/repair/degrade machinery lives in ``agentgate.nodes``; this
module is only the graph wiring and the node's name in the trace.
"""

from __future__ import annotations

from ..telemetry import traced
from . import ReviewState, run_specialist

AGENT = "correctness"


@traced("agent_correctness")
async def correctness_node(state: ReviewState) -> dict:
    verdict = await run_specialist(AGENT, state.get("payload", ""))
    errors = [f"{AGENT}: {verdict.notes}"] if _is_degraded(verdict) else []
    return {"agents": [verdict], "errors": errors}


def _is_degraded(verdict) -> bool:
    note = verdict.notes or ""
    return note.startswith(("agent unavailable", "degraded"))
