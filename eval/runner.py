"""Golden evaluation harness.

For each golden module it generates the clean -> seeded unified diff, runs the full
review graph over it, and matches the findings against ``manifest.json``.

A finding matches a ground-truth defect when the **file** matches, the **line** is
within ``AGENTGATE_EVAL_LINE_TOLERANCE`` (default 3), and the **category** matches.
Each defect can be matched at most once; where several findings are eligible the
closest line wins, so two nearby defects cannot both be claimed by one finding.

The clean modules are also reviewed against an empty baseline. Anything reported
there is a pure false positive, and is counted separately as ``clean_run_fp_count``
because it is the most honest FP signal available.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgate import llm
from agentgate.config import EVAL_DIR, get_settings
from agentgate.graph import review_async
from agentgate.models import Finding, ReviewResult
from agentgate.telemetry import BudgetExceeded

#: Number of specialist agents; a review losing all of them is a dead review.
SPECIALIST_COUNT = 3


class ProviderUnavailable(RuntimeError):
    """The provider stopped answering; continuing would measure the rate limiter."""


GOLDEN = EVAL_DIR / "golden"
SEEDED = EVAL_DIR / "seeded"
MANIFEST = EVAL_DIR / "manifest.json"


# --------------------------------------------------------------------------- #
# Ground truth
# --------------------------------------------------------------------------- #
@dataclass
class Defect:
    id: str
    file: str
    line: int
    category: str
    rule: str
    description: str = ""
    subtle: bool = False

    @property
    def basename(self) -> str:
        return self.file.replace("\\", "/").rsplit("/", 1)[-1]


def load_manifest(path: Path | None = None) -> list[Defect]:
    raw = json.loads((path or MANIFEST).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("defects", [])
    return [
        Defect(
            id=str(d["id"]),
            file=str(d["file"]),
            line=int(d["line"]),
            category=str(d["category"]),
            rule=str(d.get("rule", "")),
            description=str(d.get("description", "")),
            subtle=bool(d.get("subtle", False)),
        )
        for d in raw
    ]


# --------------------------------------------------------------------------- #
# Diff generation
# --------------------------------------------------------------------------- #
def make_diff(clean: Path, seeded: Path) -> str:
    """Unified diff from the clean module to the seeded one."""
    before = clean.read_text(encoding="utf-8").splitlines(keepends=True)
    after = seeded.read_text(encoding="utf-8").splitlines(keepends=True)
    rel = f"eval/seeded/{seeded.name}"
    body = "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=3,
        )
    )
    if not body:
        return ""
    return f"diff --git a/{rel} b/{rel}\n{body}"


def make_clean_diff(clean: Path) -> str:
    """Present the clean module as if it were newly added -- no defects to find."""
    after = clean.read_text(encoding="utf-8").splitlines(keepends=True)
    rel = f"eval/golden/{clean.name}"
    body = "".join(
        difflib.unified_diff([], after, fromfile="/dev/null", tofile=f"b/{rel}", n=3)
    )
    return f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n{body}"


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
@dataclass
class Match:
    defect_id: str
    finding_id: str
    file: str
    defect_line: int
    finding_line: int
    category: str
    defect_rule: str
    finding_rule: str
    distance: int


def match_findings(
    findings: list[Finding],
    defects: list[Defect],
    tolerance: int,
) -> tuple[list[Match], list[Finding], list[Defect]]:
    """Greedy closest-line matching, one defect per finding and vice versa."""
    candidates: list[tuple[int, int, int]] = []  # (distance, finding idx, defect idx)
    for fi, finding in enumerate(findings):
        f_base = finding.file.replace("\\", "/").rsplit("/", 1)[-1]
        for di, defect in enumerate(defects):
            if f_base != defect.basename:
                continue
            if finding.category.value != defect.category:
                continue
            distance = abs(finding.line - defect.line)
            if distance <= tolerance:
                candidates.append((distance, fi, di))

    candidates.sort()
    used_findings: set[int] = set()
    used_defects: set[int] = set()
    matches: list[Match] = []

    for distance, fi, di in candidates:
        if fi in used_findings or di in used_defects:
            continue
        used_findings.add(fi)
        used_defects.add(di)
        finding, defect = findings[fi], defects[di]
        matches.append(
            Match(
                defect_id=defect.id,
                finding_id=finding.id,
                file=defect.file,
                defect_line=defect.line,
                finding_line=finding.line,
                category=defect.category,
                defect_rule=defect.rule,
                finding_rule=finding.rule,
                distance=distance,
            )
        )

    unmatched_findings = [f for i, f in enumerate(findings) if i not in used_findings]
    missed_defects = [d for i, d in enumerate(defects) if i not in used_defects]
    return matches, unmatched_findings, missed_defects


#: Model to use for a provider named without one. Anything not listed here falls
#: back to AGENTGATE_MODEL from the environment.
DEFAULT_MODELS = {"mock": "mock-reviewer-v1"}


def parse_provider_spec(spec: str) -> tuple[str, str | None]:
    """Split ``provider`` or ``provider:model``.

    Split on the *first* colon only, because model ids legitimately contain one
    (``meta-llama/llama-3.3-70b-instruct:free``).
    """
    provider, _, model = spec.strip().partition(":")
    provider = provider.strip()
    model = model.strip()
    return provider, (model or DEFAULT_MODELS.get(provider))


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
@dataclass
class ModuleRun:
    module: str
    kind: str  # "seeded" | "clean"
    verdict: str = "approve"
    findings: list[Finding] = field(default_factory=list)
    duration_ms: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    injection_detected: bool = False
    errors: list[str] = field(default_factory=list)


async def _run_one(module: str, kind: str, diff: str, run_id: str) -> ModuleRun:
    started = time.perf_counter()
    try:
        result: ReviewResult = await review_async(diff, run_id=run_id)
    except BudgetExceeded:
        raise
    except Exception as exc:  # a single module must not sink the whole eval
        return ModuleRun(
            module=module,
            kind=kind,
            duration_ms=int((time.perf_counter() - started) * 1000),
            errors=[f"{type(exc).__name__}: {exc}"],
        )
    return ModuleRun(
        module=module,
        kind=kind,
        verdict=result.verdict,
        findings=list(result.findings),
        duration_ms=result.duration_ms,
        tokens=result.total_tokens,
        cost_usd=result.total_cost_usd,
        injection_detected=result.injection_detected,
        errors=list(result.errors),
    )


async def run_eval_async(
    provider: str | None = None,
    include_clean: bool = True,
    modules: list[str] | None = None,
) -> dict[str, Any]:
    """Run the full golden set and return the metrics dictionary."""
    settings = get_settings()
    if provider:
        import os

        from agentgate import config

        name, model = parse_provider_spec(provider)
        os.environ["AGENTGATE_PROVIDER"] = name
        if model:
            os.environ["AGENTGATE_MODEL"] = model
        config.get_settings.cache_clear()
        llm.reset_provider_cache()
        settings = get_settings()

    defects = load_manifest()
    names = modules or sorted(p.name for p in GOLDEN.glob("*.py"))
    if not names:
        raise SystemExit("no golden modules found; nothing to evaluate")

    seeded_runs: list[ModuleRun] = []
    clean_runs: list[ModuleRun] = []
    dead_streak = 0

    def _check_alive(run: ModuleRun) -> None:
        """Abort once the provider has clearly stopped answering.

        A retry against an exhausted quota consumes another request from that same
        quota, so hammering makes recovery slower, not faster. Two consecutive
        reviews where *every* agent degraded means the eval is generating numbers
        that measure the rate limiter rather than the reviewer.
        """
        nonlocal dead_streak
        every_agent_failed = not run.findings and len(run.errors) >= SPECIALIST_COUNT
        dead_streak = dead_streak + 1 if every_agent_failed else 0
        if dead_streak >= 2:
            raise ProviderUnavailable(
                f"aborting the eval: every agent failed on {dead_streak} consecutive "
                f"reviews against {settings.provider}:{settings.model}. "
                f"Last error: {run.errors[0] if run.errors else 'unknown'}. "
                "The numbers from here would measure the rate limiter, not the reviewer."
            )

    for name in names:
        clean_path, seeded_path = GOLDEN / name, SEEDED / name
        if not seeded_path.exists():
            continue
        diff = make_diff(clean_path, seeded_path)
        if diff:
            run = await _run_one(name, "seeded", diff, f"eval-{settings.provider}-{name}")
            seeded_runs.append(run)
            _check_alive(run)

    if include_clean:
        for name in names:
            run = await _run_one(
                name,
                "clean",
                make_clean_diff(GOLDEN / name),
                f"evalclean-{settings.provider}-{name}",
            )
            clean_runs.append(run)
            _check_alive(run)

    return summarise(settings.provider, settings.model, defects, seeded_runs, clean_runs)


def run_eval(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_eval_async(**kwargs))


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    k = (len(ordered) - 1) * pct
    lower, upper = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower), 2)


def summarise(
    provider: str,
    model: str,
    defects: list[Defect],
    seeded_runs: list[ModuleRun],
    clean_runs: list[ModuleRun],
) -> dict[str, Any]:
    tolerance = get_settings().eval_line_tolerance

    all_findings = [f for run in seeded_runs for f in run.findings]
    matches, unmatched, missed = match_findings(all_findings, defects, tolerance)

    total_defects = len(defects)
    total_findings = len(all_findings)
    detection_rate = round(len(matches) / total_defects, 4) if total_defects else 0.0
    fp_rate = round(len(unmatched) / total_findings, 4) if total_findings else 0.0

    per_category: dict[str, dict[str, Any]] = {}
    for category in sorted({d.category for d in defects}):
        in_cat = [d for d in defects if d.category == category]
        hit = [m for m in matches if m.category == category]
        per_category[category] = {
            "defects": len(in_cat),
            "detected": len(hit),
            "detection_rate": round(len(hit) / len(in_cat), 4) if in_cat else 0.0,
        }

    subtle = [d for d in defects if d.subtle]
    subtle_hit = [m for m in matches if any(d.id == m.defect_id and d.subtle for d in defects)]

    latencies = [float(r.duration_ms) for r in seeded_runs]
    costs = [r.cost_usd for r in seeded_runs]
    clean_findings = [f for run in clean_runs for f in run.findings]

    return {
        "provider": provider,
        "model": model,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "totals": {
            "modules_reviewed": len(seeded_runs),
            "ground_truth_defects": total_defects,
            "findings_produced": total_findings,
            "matched": len(matches),
            "missed": len(missed),
            "false_positives": len(unmatched),
        },
        "metrics": {
            "detection_rate": detection_rate,
            "false_positive_rate": fp_rate,
            "subtle_detection_rate": (
                round(len(subtle_hit) / len(subtle), 4) if subtle else 0.0
            ),
            "mean_cost_per_review_usd": (
                round(statistics.fmean(costs), 8) if costs else 0.0
            ),
            "total_cost_usd": round(sum(costs), 8),
            "cost_per_detected_defect_usd": (
                round(sum(costs) / len(matches), 8) if matches else 0.0
            ),
            "p50_latency_ms": _percentile(latencies, 0.50),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "mean_tokens_per_review": (
                int(statistics.fmean([r.tokens for r in seeded_runs])) if seeded_runs else 0
            ),
            "total_tokens": sum(r.tokens for r in seeded_runs),
        },
        "per_category": per_category,
        "clean_run": {
            "modules_reviewed": len(clean_runs),
            "clean_run_fp_count": len(clean_findings),
            "clean_run_fp_per_module": (
                round(len(clean_findings) / len(clean_runs), 3) if clean_runs else 0.0
            ),
            "by_rule": _count_by(clean_findings, "rule"),
        },
        "verdicts": _count_verdicts(seeded_runs),
        "missed_defects": [
            {"id": d.id, "file": d.file, "line": d.line, "rule": d.rule, "subtle": d.subtle}
            for d in sorted(missed, key=lambda d: d.id)
        ],
        "false_positive_findings": [
            {"file": f.file, "line": f.line, "rule": f.rule, "severity": f.severity.value}
            for f in unmatched
        ][:40],
        "matches": [m.__dict__ for m in sorted(matches, key=lambda m: m.defect_id)],
        "errors": [e for run in seeded_runs + clean_runs for e in run.errors],
    }


def _count_by(findings: list[Finding], attr: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for finding in findings:
        key = str(getattr(finding, attr))
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _count_verdicts(runs: list[ModuleRun]) -> dict[str, int]:
    out = {"approve": 0, "comment": 0, "block": 0}
    for run in runs:
        out[run.verdict] = out.get(run.verdict, 0) + 1
    return out


# --------------------------------------------------------------------------- #
# Provider comparison
# --------------------------------------------------------------------------- #
async def compare_providers_async(providers: list[str]) -> dict[str, Any]:
    """Run the same golden set against several configured providers.

    Each entry is ``provider`` or ``provider:model``. Naming the model matters:
    switching provider alone would leave ``AGENTGATE_MODEL`` pointing at the
    previous provider's model, and the comparison table would mislabel a row.
    """
    import os

    from agentgate import config

    original_provider = os.environ.get("AGENTGATE_PROVIDER", "")
    original_model = os.environ.get("AGENTGATE_MODEL", "")
    results: dict[str, Any] = {}
    try:
        for spec in providers:
            provider, model = parse_provider_spec(spec)
            os.environ["AGENTGATE_PROVIDER"] = provider
            if model:
                os.environ["AGENTGATE_MODEL"] = model
            config.get_settings.cache_clear()
            llm.reset_provider_cache()
            results[spec] = await run_eval_async()
    finally:
        os.environ["AGENTGATE_PROVIDER"] = original_provider
        if original_model:
            os.environ["AGENTGATE_MODEL"] = original_model
        else:
            os.environ.pop("AGENTGATE_MODEL", None)
        config.get_settings.cache_clear()
        llm.reset_provider_cache()

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "providers": providers,
        "results": results,
        "table": [
            {
                "provider": report["provider"],
                "spec": spec,
                "model": report["model"],
                "detection_rate": report["metrics"]["detection_rate"],
                "false_positive_rate": report["metrics"]["false_positive_rate"],
                "p95_latency_ms": report["metrics"]["p95_latency_ms"],
                "cost_per_review_usd": report["metrics"]["mean_cost_per_review_usd"],
                "cost_per_detected_defect_usd": report["metrics"][
                    "cost_per_detected_defect_usd"
                ],
                "clean_run_fp_count": report["clean_run"]["clean_run_fp_count"],
            }
            for spec, report in results.items()
        ],
    }


def compare_providers(providers: list[str]) -> dict[str, Any]:
    return asyncio.run(compare_providers_async(providers))
