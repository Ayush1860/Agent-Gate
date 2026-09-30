"""Command-line entry point.

    agentgate review --diff <file.patch> [--json] [--fail-on block|high|medium]
    agentgate review --pr <owner/repo#123>
    agentgate eval [--provider mock|openai_compat|anthropic] [--out eval_report.json]
    agentgate eval --compare <provider_a>,<provider_b>
    agentgate serve
    agentgate dashboard
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import get_settings
from .models import SEVERITY_ORDER, ReviewResult, Severity
from .telemetry import BudgetExceeded

log = logging.getLogger("agentgate.cli")

PR_REF_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>\d+)$")

SEVERITY_ICON = {
    Severity.BLOCKER: "🛑",
    Severity.HIGH: "🔴",
    Severity.MEDIUM: "🟠",
    Severity.LOW: "🟡",
    Severity.INFO: "ℹ️",
}

VERDICT_HEADLINE = {
    "block": "🛑 **Blocked** — this pull request has blocking findings.",
    "comment": "🟠 **Comments** — non-blocking findings worth a look.",
    "approve": "✅ **Approved** — no issues found on the changed lines.",
}

# Marker used to find and update our own comment instead of posting a new one.
COMMENT_MARKER = "<!-- agentgate-review -->"


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def format_comment(result: ReviewResult) -> str:
    """Render a review as the Markdown comment posted on a pull request."""
    lines = [
        COMMENT_MARKER,
        "## AgentGate review",
        "",
        VERDICT_HEADLINE.get(result.verdict, result.verdict),
        "",
    ]

    if result.injection_detected:
        lines += [
            "> ⚠️ **Prompt injection detected in this diff.**",
            "> Instruction-like text aimed at the review system was found and neutralised "
            "before the agents ran. Patterns: "
            + ", ".join(f"`{p}`" for p in result.injection_patterns)
            + ".",
            "",
        ]

    if result.findings:
        counts = result.counts_by_severity()
        summary = " · ".join(
            f"{SEVERITY_ICON[Severity(sev)]} {count} {sev}"
            for sev, count in counts.items()
            if count
        )
        lines += [summary, "", "| | Location | Rule | Finding |", "| --- | --- | --- | --- |"]
        for finding in result.findings:
            icon = SEVERITY_ICON[finding.severity]
            message = finding.message.replace("|", "\\|")
            suggestion = finding.suggestion.replace("|", "\\|")
            detail = f"{message}<br/>_{suggestion}_" if suggestion else message
            lines.append(
                f"| {icon} | `{finding.file}:{finding.line}` | `{finding.rule}` | {detail} |"
            )
    else:
        lines.append("No findings on the changed lines.")

    if result.errors:
        lines += ["", "<details><summary>Degraded agents</summary>", ""]
        lines += [f"- {e}" for e in result.errors]
        lines += ["", "</details>"]

    lines += [
        "",
        "<sub>"
        f"{len(result.agents)} agents · {result.total_tokens:,} tokens · "
        f"${result.total_cost_usd:.6f} · {result.duration_ms} ms · "
        f"`{result.provider}:{result.model}` · run `{result.run_id}`"
        "</sub>",
    ]
    return "\n".join(lines)


def format_text(result: ReviewResult) -> str:
    """Plain-text rendering for a terminal."""
    lines = [
        f"verdict: {result.verdict}",
        f"findings: {len(result.findings)}",
    ]
    if result.injection_detected:
        lines.append(f"injection: DETECTED ({', '.join(result.injection_patterns)})")
    lines.append("")
    for finding in result.findings:
        lines.append(f"  [{finding.severity.value:<8}] {finding.file}:{finding.line}")
        lines.append(f"             {finding.rule} (confidence {finding.confidence:.2f})")
        lines.append(f"             {finding.message}")
        if finding.suggestion:
            lines.append(f"             -> {finding.suggestion}")
        lines.append("")
    for error in result.errors:
        lines.append(f"  ! {error}")
    lines.append(
        f"{result.total_tokens:,} tokens · ${result.total_cost_usd:.6f} · "
        f"{result.duration_ms} ms · {result.provider}:{result.model} · run {result.run_id}"
    )
    return "\n".join(lines)


def should_fail(result: ReviewResult, fail_on: str) -> bool:
    """Whether this review should exit non-zero."""
    if fail_on == "block":
        return result.verdict == "block"
    try:
        threshold = Severity(fail_on)
    except ValueError:
        return result.verdict == "block"
    return any(SEVERITY_ORDER[f.severity] <= SEVERITY_ORDER[threshold] for f in result.findings)


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
def fetch_pr_diff(ref: str, token: str | None = None) -> str:
    """Fetch a pull request's diff. ``ref`` is ``owner/repo#123``."""
    import httpx

    match = PR_REF_RE.match(ref.strip())
    if not match:
        raise SystemExit(f"invalid --pr reference {ref!r}; expected owner/repo#123")

    token = token or os.environ.get("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = (
        f"https://api.github.com/repos/{match['owner']}/{match['repo']}"
        f"/pulls/{match['number']}"
    )
    response = httpx.get(url, headers=headers, timeout=30.0, follow_redirects=True)
    if response.status_code == 404:
        raise SystemExit(
            f"pull request {ref} not found (private repositories need GITHUB_TOKEN set)"
        )
    if response.status_code >= 400:
        raise SystemExit(f"GitHub returned {response.status_code}: {response.text[:300]}")
    return response.text


# --------------------------------------------------------------------------- #
# API key at runtime
# --------------------------------------------------------------------------- #
def _providers_needed(args: argparse.Namespace) -> list[str]:
    """Provider names this command will call. Eval --compare may name several."""
    compare = getattr(args, "compare", None)
    if compare:
        return [p.split(":", 1)[0].strip().lower() for p in compare.split(",") if p.strip()]
    return [(getattr(args, "provider", None) or get_settings().provider).strip().lower()]


def ensure_api_key(args: argparse.Namespace, prompt=None) -> None:
    """Ask for the key on the terminal instead of reading it from a file.

    The key lives only in this process's environment, so rotating it means
    pasting the new one next run -- nothing on disk to edit or leak. Prompted
    when a live provider is used and no key is set, or always with --ask-key.
    """
    if args.command not in {"review", "eval", "serve"}:
        return
    if all(name == "mock" for name in _providers_needed(args)):
        return
    if get_settings().api_key and not getattr(args, "ask_key", False):
        return
    if prompt is None:
        if not sys.stdin.isatty():
            raise SystemExit(
                "no API key: set AGENTGATE_API_KEY in the environment "
                "(CI secret) or run interactively to be prompted"
            )
        import getpass

        prompt = getpass.getpass
    key = prompt("AgentGate API key (input hidden): ").strip()
    if not key:
        raise SystemExit("no API key entered")
    # On Windows, Ctrl+V at a hidden prompt records the control character \x16
    # rather than pasting. Sent as a header, it gets a bare HTML 400 from the API.
    if not key.isascii() or not key.isprintable() or " " in key:
        raise SystemExit(
            "the API key contains control or non-ASCII characters. On Windows, "
            "Ctrl+V does not paste at a hidden prompt: right-click to paste instead."
        )
    os.environ["AGENTGATE_API_KEY"] = key
    get_settings.cache_clear()
    from .llm import reset_provider_cache

    reset_provider_cache()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_review(args: argparse.Namespace) -> int:
    from .graph import review

    if args.pr:
        diff_text = fetch_pr_diff(args.pr)
    elif args.diff:
        path = Path(args.diff)
        if not path.exists():
            raise SystemExit(f"patch file not found: {path}")
        diff_text = path.read_text(encoding="utf-8", errors="replace")
    elif not sys.stdin.isatty():
        diff_text = sys.stdin.read()
    else:
        raise SystemExit("provide --diff <file.patch>, --pr <owner/repo#123>, or pipe a diff")

    try:
        result = review(diff_text)
    except BudgetExceeded as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(format_text(result))

    if args.comment_file:
        Path(args.comment_file).write_text(format_comment(result), encoding="utf-8")
        print(f"\nwrote PR comment to {args.comment_file}", file=sys.stderr)

    fail_on = args.fail_on or get_settings().fail_on
    if should_fail(result, fail_on):
        print(
            f"\nfailing: verdict={result.verdict} meets --fail-on {fail_on}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from eval.report import summary_line, write_report
    from eval.runner import ProviderUnavailable, compare_providers, run_eval

    out_path = Path(args.out)

    if args.compare:
        providers = [p.strip() for p in args.compare.split(",") if p.strip()]
        if len(providers) < 2:
            raise SystemExit("--compare needs at least two providers, e.g. mock,openai_compat")
        try:
            comparison = compare_providers(providers)
        except ProviderUnavailable as exc:
            print(f"aborted: {exc}", file=sys.stderr)
            return 3
        primary = comparison["results"][providers[0]]
        json_path, md_path = write_report(primary, json_path=out_path, comparison=comparison)
        for row in comparison["table"]:
            print(
                f"{row['provider']:<16} detection={row['detection_rate'] * 100:5.1f}%  "
                f"fp={row['false_positive_rate'] * 100:5.1f}%  "
                f"p95={row['p95_latency_ms']:.0f}ms  "
                f"cost/review=${row['cost_per_review_usd']:.6f}"
            )
        print(f"\nwrote {json_path} and {md_path}")
        return 0

    try:
        report = run_eval(provider=args.provider)
    except ProviderUnavailable as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        return 3
    json_path, md_path = write_report(report, json_path=out_path)
    print(summary_line(report))
    print(f"wrote {json_path} and {md_path}")

    minimum = get_settings().eval_min_detection
    if args.gate and report["metrics"]["detection_rate"] < minimum:
        print(
            f"\nFAIL: detection rate {report['metrics']['detection_rate']:.1%} is below the "
            f"gate of {minimum:.1%} (AGENTGATE_EVAL_MIN_DETECTION)",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("agentgate.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    import subprocess

    script = Path(__file__).resolve().parent / "dashboard.py"
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(script),
            "--server.port",
            str(args.port),
            "--server.address",
            args.host,
        ]
    )


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentgate",
        description="Multi-agent AI code review as a CI quality gate.",
    )
    parser.add_argument("--version", action="version", version=f"agentgate {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log INFO-level progress to stderr"
    )
    parser.add_argument(
        "--ask-key",
        action="store_true",
        help="prompt for the provider API key (hidden) even if one is configured",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="review a unified diff")
    source = review.add_mutually_exclusive_group()
    source.add_argument("--diff", help="path to a .patch/.diff file")
    source.add_argument("--pr", help="pull request reference, e.g. owner/repo#123")
    review.add_argument("--json", action="store_true", help="emit the full ReviewResult as JSON")
    review.add_argument(
        "--fail-on",
        choices=["block", "high", "medium"],
        help="exit non-zero at or above this level (default: AGENTGATE_FAIL_ON)",
    )
    review.add_argument(
        "--comment-file", help="also write the Markdown PR comment to this path"
    )
    review.set_defaults(func=cmd_review)

    ev = sub.add_parser("eval", help="run the golden evaluation suite")
    ev.add_argument("--provider", help="provider to evaluate (default: AGENTGATE_PROVIDER)")
    ev.add_argument("--out", default="eval_report.json", help="path for the JSON report")
    ev.add_argument(
        "--compare",
        help=(
            "comma-separated providers to compare. Each entry is provider or "
            "provider:model, e.g. mock,openai_compat:gemini-3.6-flash"
        ),
    )
    ev.add_argument(
        "--gate",
        action="store_true",
        help="exit non-zero if detection falls below AGENTGATE_EVAL_MIN_DETECTION",
    )
    ev.set_defaults(func=cmd_eval)

    serve = sub.add_parser("serve", help="run the FastAPI server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    dash = sub.add_parser("dashboard", help="run the Streamlit telemetry dashboard")
    dash.add_argument("--host", default="0.0.0.0")
    dash.add_argument("--port", type=int, default=8501)
    dash.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        ensure_api_key(args)
        return int(args.func(args))
    except BudgetExceeded as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
