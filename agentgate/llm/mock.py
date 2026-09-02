"""Deterministic offline provider.

This is what tests, CI and the mock-mode eval run against. It never touches the
network and always returns the same JSON for the same input.

Two deliberate design choices:

* It is seeded from ``eval/manifest.json`` so the eval harness produces real,
  non-trivial numbers offline.
* It is seeded with **deliberate misses and deliberate false positives**. A mock
  that scores 100% proves nothing about the harness, so per-category recall is
  gated below 1.0 and each file attracts a small number of plausible-looking
  bogus findings.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
from typing import Any

from ..config import EVAL_DIR, get_settings
from ..models import Category, LLMResponse, Severity
from .base import LLMProvider, Message

MODEL_ID = "mock-reviewer-v1"

_FILE_RE = re.compile(r"^FILE:\s*(\S+)\s*$")
_LINE_RE = re.compile(r"^\s*(\d+)\|\s?(.*)$")
_AGENT_RE = re.compile(r"^AGENT:\s*(\w+)\s*$", re.MULTILINE)
_FENCE_OPEN_RE = re.compile(r"^<<<UNTRUSTED_DIFF_([0-9a-fA-F]+)\s*$")

# Per-category recall ceiling, in percent. Below 100 on purpose -- see module docstring.
_RECALL_GATE = {
    Category.SECURITY: 97,
    Category.CORRECTNESS: 85,
    Category.TESTING: 86,
}

# Chance, per (agent, file), that the mock invents a plausible but wrong finding.
_FP_RATE_PERCENT = 10

_AGENT_CATEGORY = {
    "security": Category.SECURITY,
    "correctness": Category.CORRECTNESS,
    "tests": Category.TESTING,
}

# Heuristics so the mock is also useful on arbitrary diffs, not just the golden set.
_HEURISTICS: dict[Category, list[tuple[re.Pattern[str], str, Severity, str, str]]] = {
    Category.SECURITY: [
        (
            re.compile(r"(?:execute|executemany)\s*\(\s*f['\"]"),
            "sql-string-interpolation",
            Severity.BLOCKER,
            "SQL query is built by string interpolation of untrusted input.",
            "Use a parameterised query with placeholders instead of an f-string.",
        ),
        (
            re.compile(r"(?:execute|executemany)\s*\(.*%\s*\("),
            "sql-string-interpolation",
            Severity.BLOCKER,
            "SQL query is assembled with % formatting rather than bind parameters.",
            "Pass the values as query parameters.",
        ),
        (
            re.compile(r"\bos\.system\s*\(|shell\s*=\s*True|create_subprocess_shell\s*\("),
            "command-injection",
            Severity.BLOCKER,
            "A shell command is built from interpolated values.",
            "Use subprocess.run with an argument list and shell=False.",
        ),
        (
            re.compile(r"\b(?:pickle|marshal)\.loads?\s*\(|\byaml\.load\s*\((?!.*Safe)"),
            "unsafe-deserialisation",
            Severity.HIGH,
            "Untrusted bytes are deserialised with an executable format.",
            "Use json, or yaml.safe_load.",
        ),
        (
            re.compile(r"(?i)\b(?:api_key|secret|password|token)\s*=\s*['\"][A-Za-z0-9_\-]{8,}['\"]"),
            "hardcoded-secret",
            Severity.HIGH,
            "A credential is hardcoded in source.",
            "Read it from the environment and keep it out of version control.",
        ),
        (
            re.compile(r"\bhashlib\.(?:md5|sha1)\s*\(|\bverify\s*=\s*False\b"),
            "unsafe-crypto",
            Severity.HIGH,
            "A broken hash or a disabled TLS check is used.",
            "Use sha256/bcrypt and leave certificate verification on.",
        ),
        (
            re.compile(r"verify_signature|jwt\.decode\s*\([^)]*options\s*="),
            "jwt-signature-not-verified",
            Severity.BLOCKER,
            "A JWT is decoded without verifying its signature.",
            "Decode with the signing key and leave signature verification enabled.",
        ),
        (
            re.compile(r"os\.path\.join\s*\([^)]*(?:filename|user|request|name)"),
            "path-traversal",
            Severity.HIGH,
            "A filesystem path is joined with unvalidated user input.",
            "Normalise the path and assert it stays inside the intended root.",
        ),
        (
            re.compile(r"\beval\s*\(|\bexec\s*\("),
            "code-execution",
            Severity.BLOCKER,
            "Dynamic code execution on interpolated input.",
            "Replace with an explicit parser or a lookup table.",
        ),
    ],
    Category.CORRECTNESS: [
        (
            re.compile(r"def\s+\w+\s*\([^)]*=\s*(?:\[\]|\{\}|set\(\))"),
            "mutable-default-argument",
            Severity.MEDIUM,
            "A mutable default argument is shared across every call.",
            "Default to None and build the container inside the function.",
        ),
        (
            re.compile(r"^\s*except\s*:\s*$|:\s*pass\s*$"),
            "swallowed-exception",
            Severity.MEDIUM,
            "An exception is caught and discarded, hiding failures.",
            "Log the exception or narrow the except clause and re-raise.",
        ),
        (
            re.compile(r"range\s*\(\s*\w+\s*\+\s*1\s*\)|<=\s*len\s*\("),
            "off-by-one",
            Severity.HIGH,
            "The loop bound runs one element past the end of the sequence.",
            "Iterate to len(seq) exclusive, or iterate the sequence directly.",
        ),
        (
            re.compile(r"(?<!await )\basyncio\.(?:sleep|gather)\s*\("),
            "unawaited-coroutine",
            Severity.HIGH,
            "A coroutine is called but never awaited.",
            "Await the call, or schedule it explicitly with asyncio.create_task.",
        ),
        (
            re.compile(r"=\s*(?:\w+\.)?open\s*\("),
            "unclosed-resource",
            Severity.MEDIUM,
            "A file handle is opened without a context manager.",
            "Use `with open(...) as fh:` so the handle is always closed.",
        ),
        (
            re.compile(r"\bglobal\s+\w+"),
            "unsynchronised-shared-state",
            Severity.MEDIUM,
            "Shared mutable state is updated without synchronisation.",
            "Guard the update with a lock, or make the state task-local.",
        ),
    ],
    Category.TESTING: [
        (
            re.compile(r"assert\s+True\b|assert\s+1\s*==\s*1\b"),
            "assertion-cannot-fail",
            Severity.MEDIUM,
            "The assertion is trivially true and can never fail.",
            "Assert on the value actually produced by the code under test.",
        ),
    ],
}


def _h(*parts: str) -> int:
    """Stable small integer from strings. Deterministic across processes."""
    raw = "|".join(parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


@functools.lru_cache(maxsize=1)
def _manifest() -> tuple[dict[str, Any], ...]:
    """Ground truth, if it exists yet. Absent during the early build phases."""
    path = EVAL_DIR / "manifest.json"
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if isinstance(data, dict):
        data = data.get("defects", [])
    return tuple(d for d in data if isinstance(d, dict))


def _detect_agent(messages: list[Message]) -> str:
    for msg in messages:
        m = _AGENT_RE.search(msg.get("content", ""))
        if m:
            return m.group(1).lower()
    return "security"


def _untrusted_lines(messages: list[Message]) -> list[str]:
    """Return only the lines inside the nonce fence.

    Critical: the system prompt contains few-shot examples in the very same
    ``FILE:`` / ``<line>|`` format. Parsing the whole prompt would make the mock
    "find" defects in its own examples. Only content the sanitizer fenced counts
    as the diff under review.
    """
    lines: list[str] = []
    for msg in messages:
        inside = False
        closing = ""
        for raw in msg.get("content", "").splitlines():
            if not inside:
                opened = _FENCE_OPEN_RE.match(raw.strip())
                if opened:
                    inside = True
                    closing = f"{opened.group(1)}_UNTRUSTED_DIFF>>>"
                continue
            if raw.strip() == closing:
                inside = False
                continue
            lines.append(raw)

    if lines:
        return lines
    # No fence: an unfenced call (a direct provider test). Fall back to the
    # non-system turns, which still excludes the few-shot examples.
    return [
        raw
        for msg in messages
        if msg.get("role") != "system"
        for raw in msg.get("content", "").splitlines()
    ]


def _parse_payload(messages: list[Message]) -> dict[str, list[tuple[int, str]]]:
    """Recover ``{file: [(line, text), ...]}`` from the fenced diff payload."""
    files: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    for raw in _untrusted_lines(messages):
        fm = _FILE_RE.match(raw)
        if fm:
            current = fm.group(1)
            files.setdefault(current, [])
            continue
        if current is None:
            continue
        lm = _LINE_RE.match(raw)
        if lm:
            files[current].append((int(lm.group(1)), lm.group(2)))
    return {f: ls for f, ls in files.items() if ls}


def _gt_for(file: str, category: Category) -> list[dict[str, Any]]:
    """Ground truth for exactly this path.

    Matched on the full path, not the basename. ``eval/golden/cache.py`` and
    ``eval/seeded/cache.py`` share a basename but only the seeded copy carries the
    defects -- matching loosely would make the mock "find" seeded defects in the
    clean modules and destroy the clean-run false-positive measurement.
    """
    path = file.replace("\\", "/")
    return [
        d
        for d in _manifest()
        if d.get("file", "").replace("\\", "/") == path
        and d.get("category") == category.value
    ]


def _severity_for(rule: str, category: Category) -> Severity:
    if category is Category.SECURITY:
        return Severity.BLOCKER if _h("sev", rule) % 100 < 45 else Severity.HIGH
    if category is Category.CORRECTNESS:
        return Severity.HIGH if _h("sev", rule) % 100 < 35 else Severity.MEDIUM
    return Severity.LOW if _h("sev", rule) % 100 < 60 else Severity.MEDIUM


def _synthesize(agent: str, files: dict[str, list[tuple[int, str]]]) -> dict[str, Any]:
    category = _AGENT_CATEGORY.get(agent, Category.SECURITY)
    gate = _RECALL_GATE[category]
    findings: list[dict[str, Any]] = []
    suppressed: set[tuple[str, int]] = set()

    for file, lines in sorted(files.items()):
        visible = {n for n, _ in lines}
        lo, hi = (min(visible), max(visible)) if visible else (0, 0)

        # --- 1. ground-truth recall, gated so some defects are deliberately missed ---
        for defect in _gt_for(file, category):
            line = int(defect.get("line", 0))
            if not (lo - 2 <= line <= hi + 2):
                continue
            did = str(defect.get("id", f"{file}:{line}"))
            if _h("recall", did) % 100 >= gate:
                suppressed.add((file, line))
                continue
            findings.append(
                {
                    "file": file,
                    "line": line,
                    "category": category.value,
                    "severity": _severity_for(str(defect.get("rule", "issue")), category).value,
                    "rule": str(defect.get("rule", "issue")),
                    "message": str(defect.get("description", "Defect detected on this line.")),
                    "suggestion": "Address the issue on this line before merging.",
                    "confidence": round(0.62 + (_h("conf", did) % 30) / 100.0, 2),
                }
            )

        # --- 2. pattern heuristics, so arbitrary diffs also get a real review ---
        for line_no, text in lines:
            for pattern, rule, severity, message, suggestion in _HEURISTICS[category]:
                if pattern.search(text):
                    findings.append(
                        {
                            "file": file,
                            "line": line_no,
                            "category": category.value,
                            "severity": severity.value,
                            "rule": rule,
                            "message": message,
                            "suggestion": suggestion,
                            "confidence": round(
                                0.55 + (_h("hconf", file, str(line_no), rule) % 35) / 100.0, 2
                            ),
                        }
                    )
                    break

        # --- 3. deliberate false positives: plausible, wrong, deterministic ---
        # Decided once per (agent, file), not per line. A per-line coin flip would
        # make the false-positive count a function of how many lines the diff
        # touches, which turns the clean-run FP number into an artifact of file
        # size rather than a measurement of the reviewer.
        if _h("fp", agent, file) % 100 < _FP_RATE_PERCENT:
            defect_lines = {int(d.get("line", 0)) for d in _gt_for(file, category)}
            eligible = [
                (n, t)
                for n, t in lines
                if len(t.strip()) >= 24
                and not t.strip().startswith(("#", '"""', "'''"))
                # Never land on a real defect -- an accidental hit would count as a
                # detection rather than the false positive this is meant to be.
                and all(abs(n - g) > 3 for g in defect_lines)
            ]
            if eligible:
                line_no, _text = eligible[_h("fpline", agent, file) % len(eligible)]
                findings.append(
                    {
                        "file": file,
                        "line": line_no,
                        "category": category.value,
                        "severity": Severity.LOW.value,
                        "rule": f"possible-{category.value}-concern",
                        "message": (
                            "This line may warrant a second look; the intent is not obvious."
                        ),
                        "suggestion": "Add a clarifying comment or a narrower type.",
                        "confidence": 0.34,
                    }
                )

    # A gated miss stays missed even if a heuristic would otherwise have caught it.
    findings = [f for f in findings if (f["file"], f["line"]) not in suppressed]

    # Deduplicate within this agent's own output.
    seen: set[tuple[str, int, str]] = set()
    unique: list[dict[str, Any]] = []
    for f in findings:
        k = (f["file"], f["line"], f["rule"])
        if k in seen:
            continue
        seen.add(k)
        unique.append(f)

    return {
        "agent": agent,
        "findings": unique,
        "notes": None if unique else "No issues found in this agent's area of responsibility.",
    }


class MockProvider(LLMProvider):
    """Offline, deterministic, zero-cost. Same diff in, same findings out."""

    name = "mock"

    def __init__(self, model: str = MODEL_ID, api_key: str = "", base_url: str = "") -> None:
        super().__init__(model=model or MODEL_ID, api_key=api_key, base_url=base_url)

    async def raw_complete(self, messages: list[Message], **kw: Any) -> LLMResponse:
        settings = get_settings()
        latency_ms = max(0, int(kw.get("latency_ms", settings.mock_latency_ms)))
        if latency_ms:
            await asyncio.sleep(latency_ms / 1000.0)

        agent = _detect_agent(messages)
        payload = _parse_payload(messages)
        verdict = _synthesize(agent, payload)
        text = json.dumps(verdict)

        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        return LLMResponse(
            text=text,
            input_tokens=max(1, prompt_chars // 4),
            output_tokens=max(1, len(text) // 4),
            model=self.model,
            provider=self.name,
            latency_ms=latency_ms,
        )
