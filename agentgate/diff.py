"""Unified-diff parser.

Only added and modified lines are reviewed -- a reviewer that comments on code the
PR did not touch is noise. Every hunk carries line numbers in the *new* file, so
findings anchor where GitHub expects them.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import DiffHunk

_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")
_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_NEW_FILE_RE = re.compile(r"^\+\+\+\s+(?:b/)?(.+?)\s*$")

# Files whose contents are not worth sending to a reviewer.
_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".woff", ".woff2", ".ttf", ".eot", ".so", ".dll", ".dylib", ".pyc",
    ".lock", ".min.js", ".min.css",
}


def _is_reviewable(path: str) -> bool:
    if path in ("/dev/null", ""):
        return False
    lowered = path.lower()
    return not any(lowered.endswith(suffix) for suffix in _SKIP_SUFFIXES)


def parse_diff(text: str) -> list[DiffHunk]:
    """Parse a unified diff into hunks of added/modified lines.

    Tolerant by design: a malformed or truncated patch yields whatever hunks could
    be recovered rather than an exception, because a parse failure must not be able
    to take down a review.
    """
    hunks: list[DiffHunk] = []
    current_file: str | None = None
    new_line = 0
    pending: DiffHunk | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None and pending.added_lines:
            hunks.append(pending)
        pending = None

    for raw in text.splitlines():
        header = _GIT_HEADER_RE.match(raw)
        if header:
            flush()
            current_file = header.group(2)
            continue

        if raw.startswith("+++ "):
            flush()
            m = _NEW_FILE_RE.match(raw)
            if m:
                path = m.group(1).split("\t")[0]
                current_file = None if path == "/dev/null" else path
            continue

        if raw.startswith("--- "):
            # The old-file header carries no information we need.
            continue

        hunk = _HUNK_RE.match(raw)
        if hunk:
            flush()
            new_line = int(hunk.group(1))
            if current_file and _is_reviewable(current_file):
                pending = DiffHunk(file=current_file, start_line=new_line, added_lines=[])
            continue

        if pending is None:
            continue

        if raw.startswith("+"):
            pending.added_lines.append((new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            # A removed line does not advance the new-file counter.
            continue
        elif raw.startswith("\\"):
            # "\ No newline at end of file"
            continue
        else:
            # Context line (leading space, or an empty line in a lenient patch).
            new_line += 1

    flush()
    return hunks


def parse_diff_file(path: str | Path) -> list[DiffHunk]:
    return parse_diff(Path(path).read_text(encoding="utf-8", errors="replace"))


def changed_files(hunks: list[DiffHunk]) -> list[str]:
    seen: list[str] = []
    for hunk in hunks:
        if hunk.file not in seen:
            seen.append(hunk.file)
    return seen


def total_added_lines(hunks: list[DiffHunk]) -> int:
    return sum(len(h.added_lines) for h in hunks)


def render_hunks(hunks: list[DiffHunk], max_lines: int = 600) -> str:
    """Render hunks in the one format the whole system agrees on.

    ``FILE: <path>`` followed by ``<new-file line number>| <code>``. The mock
    provider parses this back out, so the format is a contract -- change it here
    and change ``agentgate/llm/mock.py`` with it.
    """
    out: list[str] = []
    emitted = 0
    current: str | None = None
    truncated = False

    for hunk in hunks:
        if hunk.file != current:
            current = hunk.file
            out.append(f"FILE: {hunk.file}")
        for line_no, text in hunk.added_lines:
            if emitted >= max_lines:
                truncated = True
                break
            out.append(f"{line_no}| {text}")
            emitted += 1
        if truncated:
            break

    if truncated:
        out.append(f"[diff truncated at {max_lines} added lines]")
    return "\n".join(out)
