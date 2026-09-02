"""Generate ``eval/seeded/`` and ``eval/manifest.json`` from ``eval/golden/``.

Ground truth and the seeded code are produced by the same pass, so a manifest line
number cannot drift away from the defect it describes. Re-run this script after
editing any golden module:

    python eval/seed_defects.py

Each defect is a contiguous find/replace against the clean file. ``anchor`` names
the exact defective line, and is searched **inside the replacement block only**, so
a short anchor that appears elsewhere in the file is still unambiguous.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden"
SEEDED = HERE / "seeded"
MANIFEST = HERE / "manifest.json"


@dataclass
class Defect:
    id: str
    file: str
    category: str
    rule: str
    description: str
    edits: list[tuple[str, str]]
    anchor: str
    subtle: bool = False
    #: Extra edits (imports and the like) that are part of the change but are not
    #: themselves the defect.
    support: list[tuple[str, str]] = field(default_factory=list)


DEFECTS: list[Defect] = [
    # ----------------------------------------------------------------- security
    Defect(
        id="SEC-01",
        file="jwt_auth.py",
        category="security",
        rule="jwt-signature-not-verified",
        description=(
            "decode_token disables signature verification, so any forged token is "
            "accepted as authentic."
        ),
        edits=[
            (
                "            leeway=LEEWAY_SECONDS,\n        )",
                '            leeway=LEEWAY_SECONDS,\n'
                '            options={"verify_signature": False},\n        )',
            )
        ],
        anchor='options={"verify_signature": False}',
    ),
    Defect(
        id="SEC-02",
        file="jwt_auth.py",
        category="security",
        rule="hardcoded-secret",
        description=(
            "A hardcoded signing key is used when the environment variable is absent, "
            "so production silently signs tokens with a public constant."
        ),
        edits=[
            (
                '    if not key:\n        raise AuthError("JWT_SIGNING_KEY is not configured")\n'
                "    return key",
                "    if not key:\n"
                "        # fall back so local development does not need any setup\n"
                '        secret = "s3cret-dev-signing-key-01"\n'
                "        return secret\n"
                "    return key",
            )
        ],
        anchor='secret = "s3cret-dev-signing-key-01"',
    ),
    Defect(
        id="SEC-03",
        file="db_access.py",
        category="security",
        rule="sql-string-interpolation",
        description=(
            "The email value is interpolated straight into the SQL string instead of "
            "being bound as a parameter."
        ),
        edits=[
            (
                '        cur.execute("SELECT * FROM users WHERE email = ?", (email,))',
                '        cur.execute(f"SELECT * FROM users WHERE email = \'{email}\'")',
            )
        ],
        anchor="cur.execute(f\"SELECT * FROM users WHERE email",
    ),
    Defect(
        id="SEC-04",
        file="db_access.py",
        category="security",
        rule="sql-order-by-injection",
        description=(
            "The ORDER BY allow-list check is skipped, so a caller-supplied sort key is "
            "interpolated directly into the query."
        ),
        edits=[
            (
                "    column = _validated_sort(sort_by)",
                "    column = sort_by",
            )
        ],
        anchor="column = sort_by",
        subtle=True,
    ),
    Defect(
        id="SEC-05",
        file="file_upload.py",
        category="security",
        rule="path-traversal",
        description=(
            "The raw client filename is joined onto the upload root without "
            "containment checks, so ../ escapes the directory."
        ),
        edits=[
            (
                "    target = _resolved_target(Path(root), name)",
                "    target = Path(os.path.join(root, filename))",
            )
        ],
        support=[("import hashlib\nimport re", "import hashlib\nimport os\nimport re")],
        anchor="os.path.join(root, filename)",
    ),
    Defect(
        id="SEC-06",
        file="file_upload.py",
        category="security",
        rule="overly-permissive-file-mode",
        description=(
            "Uploaded files are written world-writable, letting any local user replace "
            "their contents."
        ),
        edits=[("FILE_MODE = 0o640", "FILE_MODE = 0o777")],
        anchor="FILE_MODE = 0o777",
    ),
    Defect(
        id="SEC-07",
        file="csv_parser.py",
        category="security",
        rule="unsafe-deserialisation",
        description=(
            "The row cache is loaded with pickle, so a tampered cache file executes "
            "arbitrary code on read."
        ),
        edits=[
            (
                '    return json.loads(path.read_text(encoding="utf-8"))',
                "    return pickle.loads(path.read_bytes())",
            )
        ],
        support=[("import io\nimport json", "import io\nimport json\nimport pickle")],
        anchor="pickle.loads(path.read_bytes())",
    ),
    Defect(
        id="SEC-08",
        file="config_loader.py",
        category="security",
        rule="unsafe-yaml-load",
        description=(
            "YAML is parsed with FullLoader instead of safe_load, allowing object "
            "construction from a config file."
        ),
        edits=[
            (
                "        data = yaml.safe_load(text)",
                "        data = yaml.load(text, Loader=yaml.FullLoader)",
            )
        ],
        anchor="yaml.load(text, Loader=yaml.FullLoader)",
    ),
    Defect(
        id="SEC-09",
        file="job_queue.py",
        category="security",
        rule="command-injection",
        description=(
            "The archive hook builds a shell command by interpolating the archive and "
            "destination paths."
        ),
        edits=[
            (
                '    binary = shutil.which("tar")\n'
                "    if binary is None:\n"
                '        raise RuntimeError("tar is not available on this host")\n'
                "    proc = await asyncio.create_subprocess_exec(\n"
                "        binary,\n"
                '        "-xf",\n'
                "        archive,\n"
                '        "-C",\n'
                "        destination,\n"
                "        stdout=subprocess.DEVNULL,\n"
                "        stderr=subprocess.PIPE,\n"
                "    )",
                "    proc = await asyncio.create_subprocess_shell(\n"
                '        f"tar -xf {archive} -C {destination}",\n'
                "        stdout=subprocess.DEVNULL,\n"
                "        stderr=subprocess.PIPE,\n"
                "    )",
            )
        ],
        anchor='f"tar -xf {archive} -C {destination}"',
    ),
    Defect(
        id="SEC-10",
        file="payments.py",
        category="security",
        rule="predictable-token",
        description=(
            "Idempotency keys come from the non-cryptographic random module, making "
            "them predictable by an attacker who has seen a few."
        ),
        edits=[
            (
                "    return secrets.token_hex(IDEMPOTENCY_KEY_BYTES)",
                '    return "%032x" % random.getrandbits(128)',
            )
        ],
        support=[("import secrets", "import random")],
        anchor="random.getrandbits(128)",
        subtle=True,
    ),
    # -------------------------------------------------------------- correctness
    Defect(
        id="COR-01",
        file="jwt_auth.py",
        category="correctness",
        rule="unchecked-none",
        description=(
            "A token with no exp claim reaches int(None) and raises TypeError instead "
            "of being rejected as expired."
        ),
        edits=[
            (
                '    exp = payload.get("exp")\n'
                "    if exp is None:\n"
                "        return True\n"
                "    return int(exp) <= (now if now is not None else int(time.time()))",
                '    exp = payload.get("exp")\n'
                "    now_ts = now if now is not None else int(time.time())\n"
                "    return int(exp) <= now_ts",
            )
        ],
        anchor="return int(exp) <= now_ts",
    ),
    Defect(
        id="COR-02",
        file="db_access.py",
        category="correctness",
        rule="unclosed-resource",
        description=(
            "The cursor is created outside the context manager and is never closed, "
            "leaking a handle on every login."
        ),
        edits=[
            (
                "    with cursor(conn) as cur:\n"
                '        cur.execute("UPDATE users SET last_login = ? WHERE id = ?", '
                "(when, user_id))\n"
                "    conn.commit()",
                "    cur = conn.cursor()\n"
                '    cur.execute("UPDATE users SET last_login = ? WHERE id = ?", '
                "(when, user_id))\n"
                "    conn.commit()",
            )
        ],
        anchor="cur = conn.cursor()",
    ),
    Defect(
        id="COR-03",
        file="file_upload.py",
        category="correctness",
        rule="unclosed-resource",
        description=(
            "The upload file handle is opened without a context manager, so it leaks "
            "whenever the write raises."
        ),
        edits=[
            (
                '    with target.open("wb") as fh:\n        fh.write(blob)',
                '    fh = target.open("wb")\n    fh.write(blob)',
            )
        ],
        anchor='fh = target.open("wb")',
    ),
    Defect(
        id="COR-04",
        file="rate_limiter.py",
        category="correctness",
        rule="unsynchronised-shared-state",
        description=(
            "allow() reads and writes the token count without holding the lock, so two "
            "threads can both spend the last token."
        ),
        edits=[
            (
                "        with self._lock:\n"
                "            self._refill(time.monotonic())\n"
                "            if self._tokens < cost:\n"
                "                return False\n"
                "            self._tokens -= cost\n"
                "            return True",
                "        self._refill(time.monotonic())\n"
                "        if self._tokens < cost:\n"
                "            return False\n"
                "        self._tokens -= cost\n"
                "        return True",
            )
        ],
        anchor="self._tokens -= cost",
        subtle=True,
    ),
    Defect(
        id="COR-05",
        file="rate_limiter.py",
        category="correctness",
        rule="missing-upper-bound-clamp",
        description=(
            "Refill is no longer clamped to capacity, so an idle bucket accumulates "
            "unlimited tokens and the limit stops applying."
        ),
        edits=[
            (
                "        self._tokens = min(float(self.capacity), "
                "self._tokens + elapsed * self.rate)",
                "        self._tokens = self._tokens + elapsed * self.rate",
            )
        ],
        anchor="self._tokens = self._tokens + elapsed * self.rate",
        subtle=True,
    ),
    Defect(
        id="COR-06",
        file="retry.py",
        category="correctness",
        rule="off-by-one",
        description=(
            "The loop runs one more time than the caller asked for, so a 3-attempt "
            "policy issues 4 requests."
        ),
        edits=[
            (
                "    for attempt in range(1, attempts + 1):",
                "    for attempt in range(1, attempts + 2):",
            )
        ],
        anchor="range(1, attempts + 2)",
        subtle=True,
    ),
    Defect(
        id="COR-07",
        file="retry.py",
        category="correctness",
        rule="swallowed-exception",
        description=(
            "A bare except catches everything including CancelledError and discards "
            "the original error, so failures are reported without a cause."
        ),
        edits=[
            (
                "        except retryable as exc:\n            last = exc",
                "        except:\n            last = None",
            )
        ],
        anchor="except:",
    ),
    Defect(
        id="COR-08",
        file="cache.py",
        category="correctness",
        rule="mutable-default-argument",
        description=(
            "The default list is created once at definition time, so evicted keys "
            "accumulate across every call."
        ),
        edits=[
            (
                "    def evict(self, count: int, collected: list[str] | None = None) "
                "-> list[str]:",
                "    def evict(self, count: int, collected: list[str] = []) -> list[str]:",
            )
        ],
        support=[
            ("        collected = [] if collected is None else collected\n", ""),
        ],
        anchor="collected: list[str] = []",
    ),
    Defect(
        id="COR-09",
        file="cache.py",
        category="correctness",
        rule="off-by-one",
        description=(
            "The eviction bound uses >= instead of >, so the cache never holds more "
            "than capacity - 1 entries."
        ),
        edits=[
            (
                "            while len(self._entries) > self.capacity:",
                "            while len(self._entries) >= self.capacity:",
            )
        ],
        anchor="while len(self._entries) >= self.capacity:",
        subtle=True,
    ),
    Defect(
        id="COR-10",
        file="job_queue.py",
        category="correctness",
        rule="unawaited-coroutine",
        description=(
            "_record is a coroutine but is called without await, so no job result is "
            "ever recorded and the coroutine is garbage-collected unrun."
        ),
        edits=[
            (
                "                await self._record(job)",
                "                self._record(job)",
            )
        ],
        anchor="self._record(job)",
    ),
    # ------------------------------------------------------------------ testing
    Defect(
        id="TST-01",
        file="payments.py",
        category="testing",
        rule="untested-new-branch",
        description=(
            "A new partial-refund short circuit returns the full charge and no test "
            "exercises the new branch."
        ),
        edits=[
            (
                "def refund_minor(charged_minor: int, requested_minor: int) -> int:\n"
                '    """A refund can never exceed what was charged, and can never be '
                'negative."""\n'
                "    if requested_minor < 0:",
                "def refund_minor(\n"
                "    charged_minor: int, requested_minor: int, partial: bool = False\n"
                ") -> int:\n"
                '    """A refund can never exceed what was charged, and can never be '
                'negative."""\n'
                "    if partial and requested_minor == 0:\n"
                "        return charged_minor\n"
                "    if requested_minor < 0:",
            )
        ],
        anchor="if partial and requested_minor == 0:",
    ),
    Defect(
        id="TST-02",
        file="rate_limiter.py",
        category="testing",
        rule="untested-new-logic",
        description=(
            "A new reset() method mutates bucket state and has no corresponding test."
        ),
        edits=[
            (
                "    def available(self) -> int:",
                "    def reset(self) -> None:\n"
                '        """Refill the bucket to capacity after a configuration '
                'change."""\n'
                "        with self._lock:\n"
                "            self._tokens = float(self.capacity)\n"
                "            self._updated_at = time.monotonic()\n"
                "\n"
                "    def available(self) -> int:",
            )
        ],
        anchor="def reset(self) -> None:",
    ),
    Defect(
        id="TST-03",
        file="config_loader.py",
        category="testing",
        rule="untested-edge-case",
        description=(
            "A new worker-count boundary check is added with no test covering the "
            "zero or negative case."
        ),
        edits=[
            (
                '        raise ConfigError("port must be between 1 and 65535")\n'
                "    return config",
                '        raise ConfigError("port must be between 1 and 65535")\n'
                '    if config.get("workers") is not None and int(config["workers"]) < 1:\n'
                '        raise ConfigError("workers must be at least 1")\n'
                "    return config",
            )
        ],
        anchor='if config.get("workers") is not None',
    ),
    Defect(
        id="TST-04",
        file="csv_parser.py",
        category="testing",
        rule="untested-error-path",
        description=(
            "A new duplicate-header rejection path is added and nothing tests that it "
            "raises."
        ),
        edits=[
            (
                '        raise CsvError("file has no header row")',
                '        raise CsvError("file has no header row")\n'
                "    if len(set(reader.fieldnames)) != len(reader.fieldnames):\n"
                '        raise CsvError("duplicate column names in header")',
            )
        ],
        anchor="if len(set(reader.fieldnames)) != len(reader.fieldnames):",
    ),
    Defect(
        id="TST-05",
        file="file_upload.py",
        category="testing",
        rule="untested-new-logic",
        description=(
            "A new delete() entry point removes files from disk and has no test at all."
        ),
        edits=[
            (
                "def checksum(path: Path) -> str:",
                "def delete(root: str, filename: str) -> bool:\n"
                '    """Remove a stored upload. Returns False when it was already '
                'gone."""\n'
                "    target = _resolved_target(Path(root), safe_name(filename))\n"
                "    if not target.exists():\n"
                "        return False\n"
                "    target.unlink()\n"
                "    return True\n"
                "\n"
                "\n"
                "def checksum(path: Path) -> str:",
            )
        ],
        anchor="def delete(root: str, filename: str) -> bool:",
    ),
]


def _apply(text: str, find: str, replace: str, label: str) -> str:
    count = text.count(find)
    if count != 1:
        raise SystemExit(
            f"{label}: expected exactly one match for the find block, found {count}.\n"
            f"--- find ---\n{find}\n"
        )
    return text.replace(find, replace)


def build() -> list[dict[str, object]]:
    SEEDED.mkdir(parents=True, exist_ok=True)

    by_file: dict[str, list[Defect]] = {}
    for defect in DEFECTS:
        by_file.setdefault(defect.file, []).append(defect)

    modules = sorted(p.name for p in GOLDEN.glob("*.py"))
    if not modules:
        raise SystemExit("no golden modules found")

    manifest: list[dict[str, object]] = []

    for name in modules:
        text = (GOLDEN / name).read_text(encoding="utf-8")

        for defect in by_file.get(name, []):
            for find, replace in defect.support:
                text = _apply(text, find, replace, f"{defect.id} (support)")
            for find, replace in defect.edits:
                text = _apply(text, find, replace, defect.id)

        (SEEDED / name).write_text(text, encoding="utf-8", newline="\n")

        # Resolve line numbers against the file as it now stands on disk.
        lines = text.splitlines()
        for defect in by_file.get(name, []):
            block = defect.edits[0][1]
            if text.count(block) != 1:
                raise SystemExit(f"{defect.id}: replacement block is not unique after seeding")
            block_start = text[: text.index(block)].count("\n")
            block_len = block.count("\n") + 1

            line_no = None
            for offset in range(block_start, min(block_start + block_len, len(lines))):
                if defect.anchor in lines[offset]:
                    line_no = offset + 1
                    break
            if line_no is None:
                raise SystemExit(
                    f"{defect.id}: anchor {defect.anchor!r} not found inside its own "
                    f"replacement block (lines {block_start + 1}-{block_start + block_len})"
                )

            manifest.append(
                {
                    "id": defect.id,
                    "file": f"eval/seeded/{name}",
                    "line": line_no,
                    "category": defect.category,
                    "rule": defect.rule,
                    "description": defect.description,
                    "subtle": defect.subtle,
                }
            )

    manifest.sort(key=lambda d: str(d["id"]))
    return manifest


def main() -> int:
    manifest = build()

    counts: dict[str, int] = {}
    for entry in manifest:
        counts[str(entry["category"])] = counts.get(str(entry["category"]), 0) + 1

    if len(manifest) != 25:
        raise SystemExit(f"expected exactly 25 seeded defects, produced {len(manifest)}")

    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    print(f"wrote {len(list(SEEDED.glob('*.py')))} seeded modules")
    print(f"wrote {MANIFEST.relative_to(HERE.parent)} with {len(manifest)} defects")
    for category in sorted(counts):
        print(f"  {category:12} {counts[category]}")
    print(f"  {'subtle':12} {sum(1 for e in manifest if e['subtle'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
