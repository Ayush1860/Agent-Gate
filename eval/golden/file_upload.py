"""Upload handling: name sanitisation, containment, size limits and safe modes."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".pdf", ".csv", ".txt"})
FILE_MODE = 0o640
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class UploadRejected(Exception):
    """Raised when an upload fails validation. Never leaks the attempted path."""


def safe_name(filename: str) -> str:
    """Reduce a client-supplied name to a single harmless path segment."""
    base = Path(filename).name
    cleaned = _UNSAFE.sub("_", base).lstrip(".")
    if not cleaned:
        raise UploadRejected("filename is empty after sanitisation")
    return cleaned[:120]


def _resolved_target(root: Path, name: str) -> Path:
    """Resolve inside ``root`` and prove the result did not escape it."""
    root = root.resolve()
    target = (root / name).resolve()
    if root not in target.parents:
        raise UploadRejected("resolved path escapes the upload root")
    return target


def validate(filename: str, blob: bytes) -> str:
    if len(blob) > MAX_UPLOAD_BYTES:
        raise UploadRejected(f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
    name = safe_name(filename)
    if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise UploadRejected("file type is not allowed")
    return name


def store(root: str, filename: str, blob: bytes) -> Path:
    """Validate then persist an upload. Returns the path actually written."""
    name = validate(filename, blob)
    target = _resolved_target(Path(root), name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as fh:
        fh.write(blob)
    target.chmod(FILE_MODE)
    return target


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path: Path) -> list[str]:
    """Read a newline-delimited manifest, skipping blanks."""
    with path.open("r", encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]
