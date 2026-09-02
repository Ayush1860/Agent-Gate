"""The CLI surface and the FastAPI endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentgate.api import app
from agentgate.cli import (
    COMMENT_MARKER,
    build_parser,
    format_comment,
    format_text,
    main,
    should_fail,
)
from agentgate.models import Category, Finding, ReviewResult, Severity

FIXTURES = Path(__file__).resolve().parent.parent / "eval" / "fixtures"
INJECTION = FIXTURES / "injection.patch"

SAMPLE_DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -10,2 +10,4 @@ import sqlite3
 def connect(path):
     return sqlite3.connect(path)
+def find_user(conn, email):
+    return conn.execute(f"SELECT * FROM users WHERE email = '{email}'").fetchone()
"""


def _result(verdict="block", findings=None, **kw):
    base = dict(
        run_id="r1",
        verdict=verdict,
        findings=findings if findings is not None else [],
        provider="mock",
        model="mock-reviewer-v1",
        total_tokens=1234,
        total_cost_usd=0.0,
        duration_ms=150,
    )
    base.update(kw)
    return ReviewResult(**base)


def _finding(severity=Severity.HIGH, **kw):
    base = dict(
        file="app/db.py",
        line=13,
        category=Category.SECURITY,
        severity=severity,
        rule="sql-string-interpolation",
        message="Interpolated SQL.",
        suggestion="Parameterise it.",
        confidence=0.9,
    )
    base.update(kw)
    return Finding(**base)


# --------------------------------------------------------------------------- #
# Exit-code policy
# --------------------------------------------------------------------------- #
def test_a_block_verdict_fails_the_default_gate():
    assert should_fail(_result("block"), "block") is True


@pytest.mark.parametrize("verdict", ["comment", "approve"])
def test_a_non_blocking_verdict_passes_the_default_gate(verdict):
    assert should_fail(_result(verdict), "block") is False


def test_fail_on_medium_catches_a_medium_finding():
    result = _result("comment", [_finding(severity=Severity.MEDIUM)])
    assert should_fail(result, "medium") is True
    assert should_fail(result, "high") is False


def test_fail_on_high_ignores_low_findings():
    result = _result("comment", [_finding(severity=Severity.LOW)])
    assert should_fail(result, "high") is False
    assert should_fail(result, "medium") is False


def test_an_unknown_threshold_falls_back_to_the_verdict():
    assert should_fail(_result("block"), "nonsense") is True


# --------------------------------------------------------------------------- #
# Comment formatting
# --------------------------------------------------------------------------- #
def test_the_comment_carries_the_marker_used_to_find_it_on_re_runs():
    assert format_comment(_result()).startswith(COMMENT_MARKER)


def test_the_comment_states_the_verdict_and_lists_findings():
    body = format_comment(_result("block", [_finding()]))
    assert "Blocked" in body
    assert "`app/db.py:13`" in body
    assert "sql-string-interpolation" in body
    assert "Parameterise it." in body


def test_a_clean_review_says_so_instead_of_rendering_an_empty_table():
    body = format_comment(_result("approve"))
    assert "Approved" in body
    assert "No findings on the changed lines." in body
    assert "| --- |" not in body


def test_a_detected_injection_is_called_out_in_the_comment():
    body = format_comment(
        _result(injection_detected=True, injection_patterns=["role-reassignment"])
    )
    assert "Prompt injection detected" in body
    assert "`role-reassignment`" in body


def test_pipes_in_finding_text_cannot_break_the_markdown_table():
    body = format_comment(_result("block", [_finding(message="a | b | c")]))
    assert "a \\| b \\| c" in body


def test_degraded_agents_are_disclosed_not_hidden():
    body = format_comment(_result(errors=["security: agent unavailable: RuntimeError: boom"]))
    assert "Degraded agents" in body
    assert "agent unavailable" in body


def test_the_footer_records_provenance():
    body = format_comment(_result())
    assert "mock:mock-reviewer-v1" in body
    assert "run `r1`" in body
    assert "1,234 tokens" in body


def test_the_text_renderer_covers_the_same_ground():
    text = format_text(_result("block", [_finding()]))
    assert "verdict: block" in text
    assert "app/db.py:13" in text
    assert "Parameterise it." in text


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def test_diff_and_pr_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["review", "--diff", "a.patch", "--pr", "o/r#1"])


def test_the_review_defaults_are_sane():
    args = build_parser().parse_args(["review", "--diff", "a.patch"])
    assert args.json is False
    assert args.fail_on is None  # falls back to AGENTGATE_FAIL_ON


def test_a_bad_fail_on_value_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["review", "--diff", "a.patch", "--fail-on", "whenever"])


def test_the_eval_subcommand_accepts_compare_and_gate():
    args = build_parser().parse_args(["eval", "--compare", "mock,anthropic", "--gate"])
    assert args.compare == "mock,anthropic"
    assert args.gate is True


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# --------------------------------------------------------------------------- #
# End-to-end CLI
# --------------------------------------------------------------------------- #
def test_review_of_the_injection_fixture_exits_one(capsys):
    code = main(["review", "--diff", str(INJECTION)])
    assert code == 1
    assert "verdict: block" in capsys.readouterr().out


def test_review_json_output_is_a_valid_review_result(capsys):
    main(["review", "--diff", str(INJECTION), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "block"
    assert payload["injection_detected"] is True
    assert ReviewResult.model_validate(payload)


def test_review_writes_the_comment_file_when_asked(tmp_path, capsys):
    out = tmp_path / "comment.md"
    main(["review", "--diff", str(INJECTION), "--json", "--comment-file", str(out)])
    body = out.read_text(encoding="utf-8")
    assert body.startswith(COMMENT_MARKER)
    assert "Prompt injection detected" in body


def test_a_clean_diff_exits_zero(tmp_path, capsys):
    patch = tmp_path / "clean.patch"
    patch.write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n",
        encoding="utf-8",
    )
    assert main(["review", "--diff", str(patch)]) == 0


def test_a_missing_patch_file_is_a_clear_error():
    with pytest.raises(SystemExit, match="patch file not found"):
        main(["review", "--diff", "does-not-exist.patch"])


def test_a_malformed_pr_reference_is_rejected():
    with pytest.raises(SystemExit, match="expected owner/repo#123"):
        main(["review", "--pr", "not-a-reference"])


def test_the_eval_subcommand_writes_both_artifacts(tmp_path, capsys):
    out = tmp_path / "report.json"
    assert main(["eval", "--provider", "mock", "--out", str(out)]) == 0
    assert out.exists()
    assert out.with_suffix(".md").exists()
    assert "detection=" in capsys.readouterr().out


def test_the_eval_gate_passes_at_the_configured_minimum(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTGATE_EVAL_MIN_DETECTION", "0.40")
    from agentgate import config

    config.get_settings.cache_clear()
    assert main(["eval", "--out", str(tmp_path / "r.json"), "--gate"]) == 0


def test_the_eval_gate_fails_when_detection_regresses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTGATE_EVAL_MIN_DETECTION", "0.99")
    from agentgate import config

    config.get_settings.cache_clear()
    assert main(["eval", "--out", str(tmp_path / "r.json"), "--gate"]) == 1
    assert "below the gate" in capsys.readouterr().err


def test_compare_needs_at_least_two_providers(tmp_path):
    with pytest.raises(SystemExit, match="at least two providers"):
        main(["eval", "--compare", "mock", "--out", str(tmp_path / "r.json")])


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    return TestClient(app)


def test_health_reports_the_active_provider(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["provider"] == "mock"


def test_post_review_accepts_json(client):
    response = client.post("/review", json={"diff": SAMPLE_DIFF})
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "block"
    assert any(f["rule"] == "sql-string-interpolation" for f in body["findings"])


def test_post_review_accepts_a_raw_patch_body(client):
    response = client.post(
        "/review",
        content=SAMPLE_DIFF.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "block"


def test_post_review_detects_injection(client):
    response = client.post(
        "/review", json={"diff": INJECTION.read_text(encoding="utf-8")}
    )
    body = response.json()
    assert body["injection_detected"] is True
    assert body["verdict"] == "block"


def test_an_empty_body_is_a_422(client):
    assert client.post("/review", json={"diff": "   "}).status_code == 422


def test_a_budget_abort_surfaces_as_402(client, monkeypatch):
    monkeypatch.setenv("AGENTGATE_TOKEN_BUDGET_PER_RUN", "10")
    from agentgate import config

    config.get_settings.cache_clear()
    response = client.post("/review", json={"diff": SAMPLE_DIFF})
    assert response.status_code == 402
    assert "budget" in response.json()["detail"]


def test_runs_are_listed_after_a_review(client):
    client.post("/review", json={"diff": SAMPLE_DIFF, "run_id": "api-run-1"})
    body = client.get("/runs").json()
    assert body["count"] >= 1
    assert any(r["run_id"] == "api-run-1" for r in body["runs"])


def test_a_single_run_returns_its_node_traces(client):
    client.post("/review", json={"diff": SAMPLE_DIFF, "run_id": "api-run-2"})
    body = client.get("/runs/api-run-2").json()
    assert body["run"]["run_id"] == "api-run-2"
    nodes = {n["node"] for n in body["nodes"]}
    assert {"parse_diff", "sanitize", "aggregator", "finalize"} <= nodes


def test_an_unknown_run_is_a_404(client):
    assert client.get("/runs/nope").status_code == 404


def test_metrics_aggregate_the_trace_log(client):
    client.post("/review", json={"diff": SAMPLE_DIFF, "run_id": "api-run-3"})
    body = client.get("/metrics").json()
    assert body["runs"] >= 1
    assert body["total_tokens"] > 0
    assert "agent_security" in body["latency_ms_by_node"]
    assert body["latency_ms_by_node"]["agent_security"]["p95"] >= 0


def test_metrics_on_an_empty_trace_log_do_not_crash(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTGATE_TRACE_FILE", str(tmp_path / "empty.jsonl"))
    from agentgate import config

    config.get_settings.cache_clear()
    assert client.get("/metrics").json()["runs"] == 0


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def test_the_dashboard_runs_as_a_top_level_script(tmp_path, monkeypatch):
    """`streamlit run agentgate/dashboard.py` executes the file with no parent
    package. Relative imports work when the module is imported normally but blow
    up under Streamlit, so this loads it the way Streamlit does."""
    import runpy
    import warnings

    from agentgate.graph import review

    # Give the dashboard some data so it exercises the aggregation paths rather
    # than short-circuiting on an empty trace log.
    review(INJECTION.read_text(encoding="utf-8"), run_id="dash-1")

    script = Path(__file__).resolve().parent.parent / "agentgate" / "dashboard.py"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        runpy.run_path(str(script), run_name="__main__")


def test_the_dashboard_handles_an_empty_trace_log(tmp_path, monkeypatch):
    import runpy
    import warnings

    monkeypatch.setenv("AGENTGATE_TRACE_FILE", str(tmp_path / "none.jsonl"))
    monkeypatch.setenv("AGENTGATE_REVIEW_FILE", str(tmp_path / "none-reviews.jsonl"))
    from agentgate import config

    config.get_settings.cache_clear()

    script = Path(__file__).resolve().parent.parent / "agentgate" / "dashboard.py"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # The empty-log guard stops the script; it must do so cleanly rather than
        # falling through into the aggregation and raising a KeyError.
        try:
            runpy.run_path(str(script), run_name="__main__")
        except BaseException as exc:  # noqa: BLE001
            assert type(exc).__name__ in ("SystemExit", "StopException"), (
                f"unexpected {exc!r}"
            )
