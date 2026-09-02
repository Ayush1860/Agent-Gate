"""Render an eval run as ``eval_report.json`` and a readable ``eval_report.md``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_JSON = Path("eval_report.json")
DEFAULT_MD = Path("eval_report.md")


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _usd(value: float) -> str:
    if value == 0:
        return "$0.00 (free)"
    if value < 0.01:
        return f"${value:.6f}"
    return f"${value:.4f}"


def render_markdown(report: dict[str, Any], comparison: dict[str, Any] | None = None) -> str:
    totals = report["totals"]
    metrics = report["metrics"]
    clean = report["clean_run"]

    lines: list[str] = [
        "# AgentGate — evaluation report",
        "",
        f"- **Provider:** `{report['provider']}`",
        f"- **Model:** `{report['model']}`",
        f"- **Generated:** {report['generated_at']}",
        f"- **Modules reviewed:** {totals['modules_reviewed']}",
        "",
        "## Headline metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Detection rate | **{_pct(metrics['detection_rate'])}** "
        f"({totals['matched']}/{totals['ground_truth_defects']} seeded defects) |",
        f"| False-positive rate | **{_pct(metrics['false_positive_rate'])}** "
        f"({totals['false_positives']}/{totals['findings_produced']} findings) |",
        f"| Subtle-defect detection | {_pct(metrics['subtle_detection_rate'])} |",
        f"| Clean-run false positives | **{clean['clean_run_fp_count']}** "
        f"across {clean['modules_reviewed']} clean modules "
        f"({clean['clean_run_fp_per_module']}/module) |",
        f"| &nbsp;&nbsp;of which security/correctness | "
        f"**{clean.get('clean_run_fp_excluding_testing', '?')}** "
        f"(unambiguously wrong; see note below) |",
        f"| Mean cost per review | {_usd(metrics['mean_cost_per_review_usd'])} |",
        f"| Cost per detected defect | {_usd(metrics['cost_per_detected_defect_usd'])} |",
        f"| p50 latency per review | {metrics['p50_latency_ms']:.0f} ms |",
        f"| p95 latency per review | {metrics['p95_latency_ms']:.0f} ms |",
        f"| Mean tokens per review | {metrics['mean_tokens_per_review']:,} |",
        "",
        "## Detection by category",
        "",
        "| Category | Seeded | Detected | Rate |",
        "| --- | --- | --- | --- |",
    ]

    for category, stats in report["per_category"].items():
        lines.append(
            f"| {category} | {stats['defects']} | {stats['detected']} | "
            f"{_pct(stats['detection_rate'])} |"
        )

    verdicts = report["verdicts"]
    lines += [
        "",
        "## Verdicts issued",
        "",
        "| Verdict | Modules |",
        "| --- | --- |",
        f"| block | {verdicts.get('block', 0)} |",
        f"| comment | {verdicts.get('comment', 0)} |",
        f"| approve | {verdicts.get('approve', 0)} |",
        "",
        "## Clean-run false positives",
        "",
        "The clean modules contain no seeded defects, so a finding here is a false "
        "positive. One caveat, stated plainly: the clean run presents each module to "
        "the reviewer as an **entire newly added file**, so a `testing` finding of the "
        "form *\"this new code has no tests\"* is a true observation about that diff "
        "rather than a mistake. Security and correctness findings on clean, idiomatic "
        "code are unambiguously wrong, and that is the number to judge precision on.",
        "",
    ]
    if clean["by_rule"]:
        lines += ["| Rule | Count |", "| --- | --- |"]
        lines += [f"| `{rule}` | {count} |" for rule, count in clean["by_rule"].items()]
    else:
        lines.append("_No findings on the clean modules._")

    missed = report["missed_defects"]
    lines += ["", f"## Missed defects ({len(missed)})", ""]
    if missed:
        lines += ["| ID | File | Line | Rule | Subtle |", "| --- | --- | --- | --- | --- |"]
        lines += [
            f"| {d['id']} | `{d['file']}` | {d['line']} | `{d['rule']}` | "
            f"{'yes' if d['subtle'] else 'no'} |"
            for d in missed
        ]
    else:
        lines.append("_Every seeded defect was detected._")

    if report.get("errors"):
        lines += ["", "## Errors", ""]
        lines += [f"- {e}" for e in dict.fromkeys(report["errors"])]

    if comparison:
        lines += ["", *render_comparison_markdown(comparison).splitlines()]

    lines += [
        "",
        "---",
        "",
        "Reproduce with:",
        "",
        "```bash",
        f"agentgate eval --provider {report['provider']}",
        "```",
        "",
    ]
    return "\n".join(lines)


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    lines = [
        "## Provider comparison",
        "",
        "The same golden set, the same seeded defects, the same matching rules — "
        "so model choice becomes a measured decision rather than a default.",
        "",
        "| Provider | Model | Detection | FP rate | Clean FPs | p95 latency | "
        "Cost / review | Cost / detected defect |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in comparison["table"]:
        lines.append(
            f"| `{row['provider']}` | `{row['model']}` | "
            f"{_pct(row['detection_rate'])} | {_pct(row['false_positive_rate'])} | "
            f"{row['clean_run_fp_count']} | {row['p95_latency_ms']:.0f} ms | "
            f"{_usd(row['cost_per_review_usd'])} | "
            f"{_usd(row['cost_per_detected_defect_usd'])} |"
        )
    lines += [
        "",
        "Reproduce with:",
        "",
        "```bash",
        f"agentgate eval --compare {','.join(comparison['providers'])}",
        "```",
    ]
    return "\n".join(lines)


def write_report(
    report: dict[str, Any],
    json_path: Path = DEFAULT_JSON,
    md_path: Path | None = None,
    comparison: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Write both artifacts. The Markdown path defaults to the JSON path with .md."""
    json_path = Path(json_path)
    md_path = Path(md_path) if md_path else json_path.with_suffix(".md")

    payload = dict(report)
    if comparison:
        payload["comparison"] = comparison

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(
        render_markdown(report, comparison), encoding="utf-8", newline="\n"
    )
    return json_path, md_path


def summary_line(report: dict[str, Any]) -> str:
    """One line for CI logs and the CLI."""
    metrics = report["metrics"]
    totals = report["totals"]
    return (
        f"provider={report['provider']} model={report['model']} "
        f"detection={_pct(metrics['detection_rate'])} "
        f"({totals['matched']}/{totals['ground_truth_defects']}) "
        f"fp_rate={_pct(metrics['false_positive_rate'])} "
        f"clean_fps={report['clean_run']['clean_run_fp_count']} "
        f"p95={metrics['p95_latency_ms']:.0f}ms "
        f"cost/review={_usd(metrics['mean_cost_per_review_usd'])}"
    )
