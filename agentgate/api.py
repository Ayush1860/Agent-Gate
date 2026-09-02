"""FastAPI surface over the review graph and the telemetry log."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import __version__
from .config import get_settings
from .graph import review_async
from .models import ReviewResult
from .telemetry import BudgetExceeded, read_traces

app = FastAPI(
    title="AgentGate",
    version=__version__,
    description="Multi-agent AI code review as a CI quality gate.",
)


class ReviewRequest(BaseModel):
    diff: str = Field(..., description="A unified diff to review.")
    run_id: str | None = None


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "provider": settings.provider,
        "model": settings.model,
    }


@app.post("/review", response_model=ReviewResult)
async def post_review(request: Request, body: Any = Body(default=None)) -> ReviewResult:
    """Review a unified diff.

    Accepts either ``{"diff": "..."}`` as JSON or the raw patch as ``text/plain``.
    """
    diff: str | None = None
    run_id: str | None = None

    if isinstance(body, dict) and "diff" in body:
        parsed = ReviewRequest.model_validate(body)
        diff, run_id = parsed.diff, parsed.run_id
    elif isinstance(body, str):
        diff = body
    else:
        raw = await request.body()
        diff = raw.decode("utf-8", errors="replace") if raw else None

    if not diff or not diff.strip():
        raise HTTPException(status_code=422, detail="request body must contain a unified diff")

    try:
        return await review_async(diff, run_id=run_id)
    except BudgetExceeded as exc:
        # 402: the run was refused on cost grounds, not because the input was bad.
        raise HTTPException(status_code=402, detail=str(exc)) from exc


def _runs_index() -> dict[str, dict[str, Any]]:
    """Fold traces.jsonl into one record per run."""
    runs: dict[str, dict[str, Any]] = {}
    for entry in read_traces():
        run_id = str(entry.get("run_id", ""))
        if not run_id:
            continue
        run = runs.setdefault(
            run_id,
            {
                "run_id": run_id,
                "started_at": entry.get("started_at"),
                "ended_at": entry.get("ended_at"),
                "provider": entry.get("provider", ""),
                "model": entry.get("model", ""),
                "nodes": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "retry_count": 0,
                "errors": 0,
                "node_ms": 0,
            },
        )
        run["nodes"] += 1
        run["input_tokens"] += int(entry.get("input_tokens", 0) or 0)
        run["output_tokens"] += int(entry.get("output_tokens", 0) or 0)
        run["cost_usd"] = round(run["cost_usd"] + float(entry.get("cost_usd", 0) or 0), 8)
        run["retry_count"] += int(entry.get("retry_count", 0) or 0)
        run["node_ms"] += int(entry.get("duration_ms", 0) or 0)
        run["errors"] += 0 if entry.get("success", True) else 1
        started, ended = entry.get("started_at"), entry.get("ended_at")
        if started and (run["started_at"] is None or started < run["started_at"]):
            run["started_at"] = started
        if ended and (run["ended_at"] is None or ended > run["ended_at"]):
            run["ended_at"] = ended
    return runs


@app.get("/runs")
async def list_runs(limit: int = 50) -> dict[str, Any]:
    runs = sorted(
        _runs_index().values(), key=lambda r: str(r.get("started_at") or ""), reverse=True
    )
    return {"count": len(runs), "runs": runs[: max(1, limit)]}


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    summary = _runs_index().get(run_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"no trace for run {run_id!r}")
    nodes = [t for t in read_traces() if t.get("run_id") == run_id]
    return {"run": summary, "nodes": nodes}


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    """Aggregate telemetry. Same source of truth as the dashboard."""
    traces = read_traces()
    if not traces:
        return {"runs": 0, "nodes": 0, "message": "no traces recorded yet"}

    per_node: dict[str, list[float]] = defaultdict(list)
    for entry in traces:
        per_node[str(entry.get("node", "?"))].append(float(entry.get("duration_ms", 0) or 0))

    runs = _runs_index()
    costs = [r["cost_usd"] for r in runs.values()]

    return {
        "runs": len(runs),
        "nodes": len(traces),
        "total_tokens": sum(
            int(t.get("input_tokens", 0) or 0) + int(t.get("output_tokens", 0) or 0)
            for t in traces
        ),
        "total_cost_usd": round(sum(costs), 8),
        "mean_cost_per_review_usd": round(statistics.fmean(costs), 8) if costs else 0.0,
        "retry_count": sum(int(t.get("retry_count", 0) or 0) for t in traces),
        "error_count": sum(1 for t in traces if not t.get("success", True)),
        "latency_ms_by_node": {
            node: {
                "count": len(values),
                "p50": _pct(values, 0.50),
                "p95": _pct(values, 0.95),
            }
            for node, values in sorted(per_node.items())
        },
    }


def _pct(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 2)
