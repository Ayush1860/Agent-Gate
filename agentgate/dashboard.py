"""Streamlit telemetry dashboard.

Reads ``runs/traces.jsonl`` plus the latest eval report. Functional, not pretty --
the point is that every number the README claims can be seen coming out of the
system rather than taken on trust.

    agentgate dashboard        # or: streamlit run agentgate/dashboard.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

# `streamlit run agentgate/dashboard.py` executes this file as a top-level script
# with no parent package, so relative imports would fail. Absolute imports plus an
# explicit path guard keep it working both as a script and as `agentgate.dashboard`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agentgate.config import REPO_ROOT, get_settings  # noqa: E402
from agentgate.telemetry import read_reviews, read_traces  # noqa: E402

AGENT_NODES = ("agent_security", "agent_correctness", "agent_tests")


st.set_page_config(page_title="AgentGate telemetry", layout="wide")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=5)
def load_traces(path_str: str) -> pd.DataFrame:
    rows = read_traces(Path(path_str))
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    for column in ("started_at", "ended_at"):
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], errors="coerce", utc=True)
    for column in ("input_tokens", "output_tokens", "duration_ms", "retry_count"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0)
    frame["total_tokens"] = frame.get("input_tokens", 0) + frame.get("output_tokens", 0)
    return frame


@st.cache_data(ttl=5)
def load_reviews(path_str: str) -> pd.DataFrame:
    rows = read_reviews(Path(path_str))
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["recorded_at"] = pd.to_datetime(
        frame.get("recorded_at"), errors="coerce", utc=True
    )
    return frame


@st.cache_data(ttl=5)
def load_eval_report(path_str: str) -> dict[str, Any] | None:
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pct(series: pd.Series, q: float) -> float:
    return float(series.quantile(q)) if len(series) else 0.0


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
settings = get_settings()

st.title("AgentGate — telemetry")
st.caption(
    f"provider `{settings.provider}` · model `{settings.model}` · "
    f"traces `{settings.trace_file}`"
)

with st.sidebar:
    st.header("Sources")
    trace_path = st.text_input("Trace file", value=str(settings.trace_file))
    review_path = st.text_input("Review log", value=str(settings.review_file))
    report_path = st.text_input("Eval report", value=str(REPO_ROOT / "eval_report.json"))
    if st.button("Reload"):
        st.cache_data.clear()

traces = load_traces(trace_path)

if traces.empty:
    st.info(
        "No traces recorded yet. Run a review to populate the log:\n\n"
        "```bash\nagentgate review --diff eval/fixtures/injection.patch\n```"
    )
    # st.stop() halts inside the Streamlit runtime. Outside it the call is a no-op,
    # and everything below assumes a populated frame, so stop explicitly too.
    st.stop()
    sys.exit(0)

runs = (
    traces.groupby("run_id")
    .agg(
        started_at=("started_at", "min"),
        ended_at=("ended_at", "max"),
        nodes=("node", "count"),
        tokens=("total_tokens", "sum"),
        cost_usd=("cost_usd", "sum"),
        retries=("retry_count", "sum"),
        failures=("success", lambda s: int((~s.astype(bool)).sum())),
        provider=("provider", "last"),
        model=("model", "last"),
    )
    .reset_index()
    .sort_values("started_at", ascending=False)
)
runs["wall_ms"] = (
    (runs["ended_at"] - runs["started_at"]).dt.total_seconds() * 1000
).round(0)

# --------------------------------------------------------------------------- #
# Headline
# --------------------------------------------------------------------------- #
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Runs", len(runs))
c2.metric("Node executions", len(traces))
c3.metric("Total tokens", f"{int(traces['total_tokens'].sum()):,}")
c4.metric("Total cost", f"${traces['cost_usd'].sum():.6f}")
c5.metric("Mean cost / review", f"${runs['cost_usd'].mean():.6f}")

c6, c7, c8, c9 = st.columns(4)
c6.metric("Retries / 429s", int(traces["retry_count"].sum()))
c7.metric("Node failures", int((~traces["success"].astype(bool)).sum()))
c8.metric("p50 review latency", f"{_pct(runs['wall_ms'], 0.50):.0f} ms")
c9.metric("p95 review latency", f"{_pct(runs['wall_ms'], 0.95):.0f} ms")

# --------------------------------------------------------------------------- #
# Runs over time
# --------------------------------------------------------------------------- #
st.subheader("Runs over time")
by_minute = (
    runs.set_index("started_at")
    .resample("1min")
    .agg(runs=("run_id", "count"), tokens=("tokens", "sum"))
    .fillna(0)
)
left, right = st.columns(2)
left.caption("Reviews per minute")
left.bar_chart(by_minute["runs"])
right.caption("Tokens per minute")
right.bar_chart(by_minute["tokens"])

# --------------------------------------------------------------------------- #
# Latency per node
# --------------------------------------------------------------------------- #
st.subheader("Latency per node (p50 / p95)")
latency = (
    traces.groupby("node")["duration_ms"]
    .agg(
        count="count",
        p50=lambda s: round(float(s.quantile(0.50)), 1),
        p95=lambda s: round(float(s.quantile(0.95)), 1),
        max="max",
    )
    .reset_index()
    .sort_values("p95", ascending=False)
)
st.dataframe(latency, width="stretch", hide_index=True)

fan_out = traces[traces["node"].isin(AGENT_NODES)]
if not fan_out.empty:
    spans = fan_out.groupby("run_id").apply(
        lambda g: pd.Series(
            {
                "span_ms": (g["ended_at"].max() - g["started_at"].min()).total_seconds() * 1000,
                "serial_ms": g["duration_ms"].sum(),
            }
        ),
        include_groups=False,
    )
    if not spans.empty:
        saved = 1 - (spans["span_ms"].sum() / max(spans["serial_ms"].sum(), 1))
        st.caption(
            f"**Fan-out concurrency:** the three specialists took "
            f"{spans['span_ms'].mean():.0f} ms of wall clock on average against "
            f"{spans['serial_ms'].mean():.0f} ms of summed agent time — "
            f"{saved:.0%} saved by running them in parallel."
        )

st.bar_chart(latency.set_index("node")["p95"])

# --------------------------------------------------------------------------- #
# Cost per review
# --------------------------------------------------------------------------- #
st.subheader("Cost and tokens per review")
st.dataframe(
    runs[
        [
            "run_id",
            "started_at",
            "provider",
            "model",
            "nodes",
            "tokens",
            "cost_usd",
            "wall_ms",
            "retries",
            "failures",
        ]
    ].head(50),
    width="stretch",
    hide_index=True,
)

# --------------------------------------------------------------------------- #
# Eval report
# --------------------------------------------------------------------------- #
st.subheader("Latest evaluation")
report = load_eval_report(report_path)

if report is None:
    st.info(
        "No eval report found. Generate one:\n\n"
        "```bash\nagentgate eval --provider mock\n```"
    )
else:
    metrics, totals, clean = report["metrics"], report["totals"], report["clean_run"]
    e1, e2, e3, e4 = st.columns(4)
    e1.metric(
        "Detection rate",
        f"{metrics['detection_rate']:.1%}",
        f"{totals['matched']}/{totals['ground_truth_defects']} defects",
    )
    e2.metric(
        "False-positive rate",
        f"{metrics['false_positive_rate']:.1%}",
        f"{totals['false_positives']}/{totals['findings_produced']} findings",
    )
    e3.metric("Clean-run FPs", clean["clean_run_fp_count"])
    e4.metric("Subtle detection", f"{metrics['subtle_detection_rate']:.1%}")

    st.caption("Detection by category")
    st.dataframe(
        pd.DataFrame(report["per_category"]).T.reset_index(names="category"),
        width="stretch",
        hide_index=True,
    )

    if report.get("missed_defects"):
        with st.expander(f"Missed defects ({len(report['missed_defects'])})"):
            st.dataframe(
                pd.DataFrame(report["missed_defects"]),
                width="stretch",
                hide_index=True,
            )

    if report.get("comparison"):
        st.subheader("Provider comparison")
        st.caption(
            "Same golden set, same seeded defects, same matching rules — "
            "so model choice is a measured decision."
        )
        st.dataframe(
            pd.DataFrame(report["comparison"]["table"]),
            width="stretch",
            hide_index=True,
        )

# --------------------------------------------------------------------------- #
# Findings and injections
# --------------------------------------------------------------------------- #
st.subheader("Findings and injection defence")
reviews = load_reviews(review_path)

if reviews.empty:
    st.info("No completed reviews recorded yet.")
else:
    severities = ["blocker", "high", "medium", "low", "info"]
    by_severity = pd.DataFrame(list(reviews["by_severity"])).reindex(
        columns=severities, fill_value=0
    )
    by_category = pd.DataFrame(list(reviews["by_category"])).reindex(
        columns=["security", "correctness", "testing"], fill_value=0
    )

    left, right = st.columns(2)
    left.caption("Findings by severity")
    left.bar_chart(by_severity.sum())
    right.caption("Findings by category")
    right.bar_chart(by_category.sum())

    injections = int(reviews["injection_detected"].astype(bool).sum())
    v1, v2, v3, v4 = st.columns(4)
    v1.metric("Reviews", len(reviews))
    v2.metric("Blocked", int((reviews["verdict"] == "block").sum()))
    v3.metric("Injections detected", injections)
    v4.metric("Degraded agents", int(reviews["degraded_agents"].sum()))

    if injections:
        pattern_counts: dict[str, int] = {}
        for patterns in reviews.loc[
            reviews["injection_detected"].astype(bool), "injection_patterns"
        ]:
            for pattern in patterns or []:
                pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1
        st.caption("Injection patterns tripped")
        st.bar_chart(pd.Series(pattern_counts).sort_values(ascending=False))

with st.expander("Raw trace log"):
    st.dataframe(
        traces.sort_values("started_at", ascending=False).head(300),
        width="stretch",
        hide_index=True,
    )
