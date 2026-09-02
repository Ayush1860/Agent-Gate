"""Per-node telemetry: token, latency and cost tracking written to runs/traces.jsonl.

The LLM layer reports usage into a context-local accumulator; the ``@traced``
decorator drains that accumulator when the node finishes and emits one
:class:`NodeTrace` per node execution.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import get_settings
from .models import LLMResponse, NodeTrace, RunTrace

log = logging.getLogger("agentgate.telemetry")

_WRITE_LOCK = threading.Lock()


class BudgetExceeded(RuntimeError):
    """Raised when a run would exceed its configured token ceiling."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _NodeUsage:
    """Usage accumulated inside one node execution."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    retry_count: int = 0
    provider: str = ""
    model: str = ""
    #: Set by mark_node_degraded(). A node that swallows an error to keep the
    #: graph alive still has to show up as failed in the trace.
    error: str | None = None


@dataclass
class RunContext:
    """Live state for one review run: the trace, the budget and the usage buckets."""

    run_id: str
    token_budget: int
    trace: RunTrace
    #: Run totals, charged by every completed call. Authoritative even for calls
    #: made outside a ``@traced`` node -- the per-node trace only sees calls that
    #: happen inside one.
    tokens_consumed: int = 0
    cost_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def check_budget(self, projected: int = 0) -> None:
        """Abort loudly rather than silently burning credits."""
        if self.token_budget <= 0:
            return
        if self.tokens_consumed + projected > self.token_budget:
            raise BudgetExceeded(
                f"token budget exceeded for run {self.run_id}: budget={self.token_budget:,}, "
                f"consumed={self.tokens_consumed:,}, this call would add ~{projected:,}"
            )

    def charge(self, tokens: int, cost: float = 0.0) -> None:
        with self._lock:
            self.tokens_consumed += tokens
            self.cost_usd += cost


_RUN_CTX: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "agentgate_run_ctx", default=None
)
_NODE_USAGE: contextvars.ContextVar[_NodeUsage | None] = contextvars.ContextVar(
    "agentgate_node_usage", default=None
)


def current_run() -> RunContext | None:
    return _RUN_CTX.get()


@contextmanager
def new_run(run_id: str | None = None, token_budget: int | None = None) -> Iterator[RunContext]:
    """Open a run scope. All telemetry and budget accounting is keyed to it."""
    settings = get_settings()
    rid = run_id or uuid.uuid4().hex[:12]
    ctx = RunContext(
        run_id=rid,
        token_budget=token_budget if token_budget is not None else settings.token_budget_per_run,
        trace=RunTrace(run_id=rid),
    )
    token = _RUN_CTX.set(ctx)
    try:
        yield ctx
    finally:
        _RUN_CTX.reset(token)


def record_usage(resp: LLMResponse, cost: float) -> None:
    """Called by the LLM layer after every completed call."""
    bucket = _NODE_USAGE.get()
    if bucket is not None:
        bucket.input_tokens += resp.input_tokens
        bucket.output_tokens += resp.output_tokens
        bucket.cost_usd += cost
        bucket.retry_count += resp.retry_count
        bucket.provider = resp.provider or bucket.provider
        bucket.model = resp.model or bucket.model
    ctx = _RUN_CTX.get()
    if ctx is not None:
        ctx.charge(resp.total_tokens, cost)


def mark_node_degraded(reason: str) -> None:
    """Record that this node degraded, even though it is returning normally.

    A specialist that loses its provider returns an empty verdict so the review
    survives. Without this the node is written to the trace as a success, and a
    run in which every agent failed looks perfectly healthy on the dashboard.
    """
    bucket = _NODE_USAGE.get()
    if bucket is not None and bucket.error is None:
        bucket.error = reason


def write_trace(node_trace: NodeTrace) -> None:
    """Append one JSON object per node execution. Never raises into the graph."""
    settings = get_settings()
    if not settings.trace_enabled:
        return
    path = Path(settings.trace_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = node_trace.model_dump_json()
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:  # pragma: no cover - disk failure is not worth crashing a review
        log.warning("could not write trace to %s: %s", path, exc)


def write_review_summary(summary: dict[str, Any]) -> None:
    """Append one line per completed review to ``runs/reviews.jsonl``.

    The node-level trace records cost and latency but not findings, and the
    dashboard needs findings by severity and category. Kept as a separate log so
    the trace schema stays one-row-per-node.
    """
    settings = get_settings()
    if not settings.trace_enabled:
        return
    path = Path(settings.review_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(summary) + "\n")
    except (OSError, TypeError, ValueError) as exc:  # pragma: no cover
        log.warning("could not write review summary to %s: %s", path, exc)


def read_reviews(path: Path | None = None) -> list[dict[str, Any]]:
    """Read reviews.jsonl, skipping malformed lines."""
    return _read_jsonl(Path(path or get_settings().review_file))


def read_traces(path: Path | None = None) -> list[dict[str, Any]]:
    """Read traces.jsonl, skipping any malformed lines rather than blowing up."""
    return _read_jsonl(Path(path or get_settings().trace_file))


def _read_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            log.warning("skipping malformed trace line in %s", p)
    return out


def traced(node_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap a graph node so its cost, latency and outcome land in the trace.

    Works on both sync and async callables. Exceptions are recorded and re-raised --
    swallowing them here would hide failures from the caller.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                bucket, tok, start = _enter()
                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:
                    _exit(node_name, bucket, tok, start, error=exc)
                    raise
                _exit(node_name, bucket, tok, start)
                return result

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            bucket, tok, start = _enter()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                _exit(node_name, bucket, tok, start, error=exc)
                raise
            _exit(node_name, bucket, tok, start)
            return result

        return sync_wrapper

    return decorator


def _enter() -> tuple[_NodeUsage, Any, datetime]:
    bucket = _NodeUsage()
    tok = _NODE_USAGE.set(bucket)
    return bucket, tok, _now()


def _exit(
    node_name: str,
    bucket: _NodeUsage,
    tok: Any,
    start: datetime,
    error: BaseException | None = None,
) -> None:
    _NODE_USAGE.reset(tok)
    end = _now()
    ctx = _RUN_CTX.get()
    settings = get_settings()
    nt = NodeTrace(
        run_id=ctx.run_id if ctx else "adhoc",
        node=node_name,
        started_at=start.isoformat(),
        ended_at=end.isoformat(),
        duration_ms=int((end - start).total_seconds() * 1000),
        provider=bucket.provider or settings.provider,
        model=bucket.model or settings.model,
        input_tokens=bucket.input_tokens,
        output_tokens=bucket.output_tokens,
        cost_usd=round(bucket.cost_usd, 8),
        retry_count=bucket.retry_count,
        success=error is None and bucket.error is None,
        error=(f"{type(error).__name__}: {error}" if error else bucket.error),
    )
    if ctx is not None:
        ctx.trace.add(nt)
    write_trace(nt)
