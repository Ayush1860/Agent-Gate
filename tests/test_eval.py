"""The golden evaluation suite: ground truth, matching rules, and the metrics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentgate.models import Category, Finding, Severity
from eval.report import render_comparison_markdown, render_markdown, write_report
from eval.runner import (
    GOLDEN,
    MANIFEST,
    SEEDED,
    Defect,
    load_manifest,
    make_clean_diff,
    make_diff,
    match_findings,
    run_eval,
)

MODULES = sorted(p.name for p in GOLDEN.glob("*.py"))


# --------------------------------------------------------------------------- #
# Ground truth
# --------------------------------------------------------------------------- #
def test_there_are_ten_golden_modules():
    assert len(MODULES) == 10


def test_every_golden_module_has_a_seeded_twin():
    seeded = sorted(p.name for p in SEEDED.glob("*.py"))
    assert seeded == MODULES


def test_the_manifest_declares_exactly_twenty_five_defects():
    assert len(load_manifest()) == 25


def test_defects_are_spread_across_the_three_categories():
    counts: dict[str, int] = {}
    for defect in load_manifest():
        counts[defect.category] = counts.get(defect.category, 0) + 1
    assert counts == {"security": 10, "correctness": 10, "testing": 5}


def test_at_least_five_defects_are_marked_subtle():
    assert sum(1 for d in load_manifest() if d.subtle) >= 5


def test_defect_ids_are_unique():
    ids = [d.id for d in load_manifest()]
    assert len(ids) == len(set(ids))


def test_every_defect_has_a_rule_and_a_description():
    for defect in load_manifest():
        assert defect.rule, f"{defect.id} has no rule"
        assert len(defect.description) > 20, f"{defect.id} has a thin description"


def test_every_manifest_line_exists_in_its_seeded_file():
    for defect in load_manifest():
        path = Path(defect.file)
        assert path.exists(), f"{defect.id} points at a missing file"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 1 <= defect.line <= len(lines), f"{defect.id} line {defect.line} out of range"


def test_seeded_modules_actually_differ_from_the_clean_ones():
    for name in MODULES:
        clean = (GOLDEN / name).read_text(encoding="utf-8")
        seeded = (SEEDED / name).read_text(encoding="utf-8")
        has_defect = any(d.basename == name for d in load_manifest())
        assert (clean != seeded) == has_defect


def test_the_manifest_on_disk_matches_a_fresh_generation():
    """Guards against hand-editing a seeded file without re-running the seeder."""
    import eval.seed_defects as seeder

    regenerated = seeder.build()
    on_disk = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert regenerated == on_disk


# --------------------------------------------------------------------------- #
# Diff generation
# --------------------------------------------------------------------------- #
def test_the_seeded_diff_only_contains_the_defective_lines():
    diff = make_diff(GOLDEN / "retry.py", SEEDED / "retry.py")
    added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    assert any("attempts + 2" in ln for ln in added)
    assert not any("attempts + 1" in ln for ln in added)


def test_every_module_produces_a_parseable_diff():
    from agentgate.diff import parse_diff

    for name in MODULES:
        diff = make_diff(GOLDEN / name, SEEDED / name)
        if not diff:
            continue
        hunks = parse_diff(diff)
        assert hunks, f"{name} produced an unparseable diff"
        assert all(h.file == f"eval/seeded/{name}" for h in hunks)


def test_the_clean_diff_presents_the_whole_module_as_added():
    from agentgate.diff import parse_diff, total_added_lines

    diff = make_clean_diff(GOLDEN / "cache.py")
    hunks = parse_diff(diff)
    source_lines = (GOLDEN / "cache.py").read_text(encoding="utf-8").splitlines()
    assert total_added_lines(hunks) == len(source_lines)
    assert hunks[0].file == "eval/golden/cache.py"


def test_a_manifest_defect_line_survives_into_the_diff():
    """The whole measurement rests on this: defects must be on *added* lines."""
    from agentgate.diff import parse_diff

    misplaced = []
    for defect in load_manifest():
        diff = make_diff(GOLDEN / defect.basename, SEEDED / defect.basename)
        added_line_numbers = {
            n for hunk in parse_diff(diff) for n, _ in hunk.added_lines
        }
        if defect.line not in added_line_numbers:
            misplaced.append(f"{defect.id} (line {defect.line})")
    assert not misplaced, f"defects not on added lines: {misplaced}"


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _finding(file="eval/seeded/cache.py", line=50, category=Category.CORRECTNESS, rule="r"):
    return Finding(
        file=file,
        line=line,
        category=category,
        severity=Severity.MEDIUM,
        rule=rule,
        message="m",
        suggestion="s",
        confidence=0.7,
    )


def _defect(file="eval/seeded/cache.py", line=50, category="correctness", did="D1"):
    return Defect(id=did, file=file, line=line, category=category, rule="r", description="d")


def test_an_exact_hit_matches():
    matches, fps, missed = match_findings([_finding()], [_defect()], tolerance=3)
    assert len(matches) == 1 and not fps and not missed


@pytest.mark.parametrize("offset", [-3, -1, 0, 2, 3])
def test_a_finding_within_tolerance_matches(offset):
    matches, _, _ = match_findings([_finding(line=50 + offset)], [_defect()], tolerance=3)
    assert len(matches) == 1


@pytest.mark.parametrize("offset", [-4, 4, 20])
def test_a_finding_outside_tolerance_is_a_false_positive(offset):
    matches, fps, missed = match_findings([_finding(line=50 + offset)], [_defect()], tolerance=3)
    assert not matches and len(fps) == 1 and len(missed) == 1


def test_a_category_mismatch_never_matches():
    matches, fps, missed = match_findings(
        [_finding(category=Category.SECURITY)], [_defect(category="correctness")], tolerance=3
    )
    assert not matches and len(fps) == 1 and len(missed) == 1


def test_a_different_file_never_matches():
    matches, _, _ = match_findings(
        [_finding(file="eval/seeded/retry.py")], [_defect()], tolerance=3
    )
    assert not matches


def test_files_are_compared_by_basename_so_path_prefixes_do_not_break_matching():
    matches, _, _ = match_findings([_finding(file="cache.py")], [_defect()], tolerance=3)
    assert len(matches) == 1


def test_each_defect_can_only_be_matched_once():
    findings = [_finding(line=50), _finding(line=51, rule="other")]
    matches, fps, _ = match_findings(findings, [_defect()], tolerance=3)
    assert len(matches) == 1
    assert len(fps) == 1


def test_each_finding_can_only_claim_one_defect():
    defects = [_defect(line=50, did="D1"), _defect(line=52, did="D2")]
    matches, _, missed = match_findings([_finding(line=51)], defects, tolerance=3)
    assert len(matches) == 1
    assert len(missed) == 1


def test_the_closest_defect_wins_when_two_are_in_range():
    defects = [_defect(line=48, did="FAR"), _defect(line=50, did="NEAR")]
    matches, _, _ = match_findings([_finding(line=50)], defects, tolerance=3)
    assert matches[0].defect_id == "NEAR"


def test_nearby_defects_are_each_matched_by_their_own_finding():
    defects = [_defect(line=50, did="A"), _defect(line=53, did="B")]
    findings = [_finding(line=50), _finding(line=53, rule="other")]
    matches, fps, missed = match_findings(findings, defects, tolerance=3)
    assert {m.defect_id for m in matches} == {"A", "B"}
    assert not fps and not missed


# --------------------------------------------------------------------------- #
# End-to-end metrics
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def report():
    return run_eval()


def test_the_eval_produces_real_numbers_not_placeholders(report):
    metrics = report["metrics"]
    assert 0.0 < metrics["detection_rate"] < 1.0, "a mock that scores 0% or 100% proves nothing"
    assert metrics["false_positive_rate"] > 0.0, "deliberate false positives must show up"
    assert report["totals"]["ground_truth_defects"] == 25
    assert report["totals"]["modules_reviewed"] == 10


def test_detection_clears_the_configured_drift_gate(report):
    from agentgate.config import get_settings

    assert report["metrics"]["detection_rate"] >= get_settings().eval_min_detection


def test_every_category_is_reported_separately(report):
    assert set(report["per_category"]) == {"security", "correctness", "testing"}
    for stats in report["per_category"].values():
        assert stats["defects"] > 0
        assert 0.0 <= stats["detection_rate"] <= 1.0


def test_matched_plus_missed_equals_the_ground_truth(report):
    totals = report["totals"]
    assert totals["matched"] + totals["missed"] == totals["ground_truth_defects"]


def test_matched_plus_false_positives_equals_the_findings_produced(report):
    totals = report["totals"]
    assert totals["matched"] + totals["false_positives"] == totals["findings_produced"]


def test_the_clean_run_is_reported_separately(report):
    clean = report["clean_run"]
    assert clean["modules_reviewed"] == 10
    assert clean["clean_run_fp_count"] >= 0


def test_no_clean_module_finding_reuses_a_seeded_defect_rule(report):
    """The clean modules have no defects, so no seeded rule may appear there."""
    seeded_rules = {d.rule for d in load_manifest()}
    assert not (set(report["clean_run"]["by_rule"]) & seeded_rules)


def test_latency_and_cost_are_measured(report):
    metrics = report["metrics"]
    assert metrics["p50_latency_ms"] > 0
    assert metrics["p95_latency_ms"] >= metrics["p50_latency_ms"]
    assert metrics["mean_tokens_per_review"] > 0
    assert metrics["mean_cost_per_review_usd"] == 0.0  # the mock provider is free


def test_the_eval_is_deterministic_in_mock_mode(report):
    again = run_eval()
    assert again["metrics"]["detection_rate"] == report["metrics"]["detection_rate"]
    assert again["metrics"]["false_positive_rate"] == report["metrics"]["false_positive_rate"]
    assert again["clean_run"]["clean_run_fp_count"] == report["clean_run"]["clean_run_fp_count"]


def test_the_eval_records_no_agent_errors(report):
    assert report["errors"] == []


def test_seeded_modules_mostly_get_blocked(report):
    assert report["verdicts"]["block"] >= 5


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def test_the_markdown_report_contains_the_headline_numbers(report):
    md = render_markdown(report)
    assert "# AgentGate — evaluation report" in md
    assert "Detection rate" in md
    assert "False-positive rate" in md
    assert "Clean-run false positives" in md
    assert "## Detection by category" in md
    assert "## Missed defects" in md


def test_both_report_artifacts_are_written(report, tmp_path):
    json_path, md_path = write_report(report, json_path=tmp_path / "eval_report.json")
    assert json_path.exists() and md_path.exists()
    assert md_path.name == "eval_report.md"
    reloaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert reloaded["metrics"] == report["metrics"]


def test_the_comparison_table_renders_every_provider(report):
    comparison = {
        "generated_at": "now",
        "providers": ["mock", "openai_compat"],
        "table": [
            {
                "provider": "mock",
                "model": "mock-reviewer-v1",
                "detection_rate": 0.56,
                "false_positive_rate": 0.36,
                "p95_latency_ms": 150.0,
                "cost_per_review_usd": 0.0,
                "cost_per_detected_defect_usd": 0.0,
                "clean_run_fp_count": 6,
            },
            {
                "provider": "openai_compat",
                "model": "gemini-2.0-flash",
                "detection_rate": 0.72,
                "false_positive_rate": 0.21,
                "p95_latency_ms": 4200.0,
                "cost_per_review_usd": 0.0012,
                "cost_per_detected_defect_usd": 0.0067,
                "clean_run_fp_count": 3,
            },
        ],
    }
    md = render_comparison_markdown(comparison)
    assert "## Provider comparison" in md
    assert "gemini-2.0-flash" in md
    assert "56.0%" in md and "72.0%" in md
    assert "Cost / detected defect" in md

    combined = render_markdown(report, comparison)
    assert "## Provider comparison" in combined
