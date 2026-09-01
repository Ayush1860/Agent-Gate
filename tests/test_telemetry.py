"""Telemetry: one JSONL line per node execution, with cost and tokens attached."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentgate import llm
from agentgate.config import get_settings
from agentgate.telemetry import new_run, read_traces, traced


def _trace_path() -> Path:
    return Path(get_settings().trace_file)


async def test_traced_writes_one_line_per_node_execution():
    @traced("demo_node")
    async def node(state):
        return {"ok": True}

    with new_run("run-abc"):
        await node({})
        await node({})

    lines = read_traces(_trace_path())
    assert len(lines) == 2
    assert {line["node"] for line in lines} == {"demo_node"}
    assert {line["run_id"] for line in lines} == {"run-abc"}


async def test_traced_captures_token_usage_from_the_llm_layer():
    @traced("reviewer")
    async def node(state):
        return await llm.complete(
            [
                {"role": "system", "content": "AGENT: security\n"},
                {"role": "user", "content": "FILE: a.py\n  1| x = 1\n"},
            ]
        )

    with new_run("run-tokens") as ctx:
        resp = await node({})

    entry = read_traces(_trace_path())[0]
    assert entry["input_tokens"] == resp.input_tokens
    assert entry["output_tokens"] == resp.output_tokens
    assert entry["provider"] == "mock"
    assert entry["cost_usd"] == 0.0  # the mock provider is free by construction
    assert ctx.trace.total_tokens == resp.total_tokens


async def test_traced_records_failures_and_re_raises():
    @traced("exploding_node")
    async def node(state):
        raise ValueError("boom")

    with new_run("run-error"):
        with pytest.raises(ValueError, match="boom"):
            await node({})

    entry = read_traces(_trace_path())[0]
    assert entry["success"] is False
    assert "ValueError: boom" in entry["error"]


def test_traced_works_on_sync_nodes():
    @traced("sync_node")
    def node(state):
        return {"done": True}

    with new_run("run-sync"):
        assert node({}) == {"done": True}

    assert read_traces(_trace_path())[0]["node"] == "sync_node"


async def test_duration_is_recorded():
    import asyncio

    @traced("slow_node")
    async def node(state):
        await asyncio.sleep(0.05)

    with new_run("run-slow"):
        await node({})

    assert read_traces(_trace_path())[0]["duration_ms"] >= 40


def test_read_traces_skips_malformed_lines(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"node":"a"}\nnot json at all\n\n{"node":"b"}\n', encoding="utf-8")
    assert [t["node"] for t in read_traces(path)] == ["a", "b"]


def test_read_traces_on_a_missing_file_returns_empty(tmp_path):
    assert read_traces(tmp_path / "nope.jsonl") == []


async def test_traces_are_valid_json_lines():
    @traced("json_node")
    async def node(state):
        return None

    with new_run("run-json"):
        await node({})

    raw = _trace_path().read_text(encoding="utf-8").strip()
    parsed = json.loads(raw)
    assert parsed["node"] == "json_node"
