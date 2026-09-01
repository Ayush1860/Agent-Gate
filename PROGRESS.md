# AgentGate — Build Progress

Running log of what was built, what was decided, and what remains.
Newest phase at the bottom. Decisions are recorded so a later session can resume cold.

---

## Phase 1 — core models, config, provider-agnostic LLM layer

**Status:** in progress

### Planned
- Repo skeleton, `requirements.txt`, `.gitignore`
- `agentgate/config.py` — pydantic-settings, thresholds, `MODEL_PRICES`
- `agentgate/models.py` — Severity, Category, Finding, AgentVerdict, LLMResponse, ReviewResult, RunTrace
- `agentgate/llm/` — base ABC, mock, openai_compat, anthropic + shared semaphore/backoff/budget
- `agentgate/telemetry.py` — `@traced`, JSONL writer
- `.env.example` with every provider preset
- Tests: models, mock provider determinism, 429 backoff, budget abort

### Done
- `config.py`: `Settings` (env prefix `AGENTGATE_`), `MODEL_PRICES` keyed `provider:model`,
  `price_for()` / `cost_usd()`.
- `models.py`: Severity, Category, Finding (auto stable id = sha256(file:line:rule)[:12]),
  AgentVerdict, LLMResponse, DiffHunk, NodeTrace, RunTrace, ReviewResult.
- `llm/`: `base.py` ABC + typed error hierarchy; `mock.py`; `openai_compat.py`; `anthropic.py`;
  `__init__.py` holds the *only* call path — semaphore, backoff, budget, cost accounting.
- `telemetry.py`: `@traced` (sync + async), contextvar usage buckets, JSONL writer/reader.
- `.env.example` with 7 provider presets; `.gitignore`; `pytest.ini`; `tests/conftest.py`.
- 44 tests passing, fully offline.

### Decisions
- **`complete()` is a free function, not a base-class method.** Providers implement only
  `raw_complete()`. This keeps all shared logic in `llm/__init__.py` as the spec requires
  without a circular import between `base.py` and `__init__.py`.
- **Backoff/budget tests use a purpose-built `FlakyProvider` defined in the test file**, not a
  failure-injection mode bolted onto the mock provider. Keeps the mock honest and single-purpose.
- **A `sleeper` parameter is injected into `complete()`** so retry tests assert on the delay
  sequence without actually waiting. Default is `asyncio.sleep`.
- **Semaphores are keyed by `(event loop id, limit)`** — `asyncio.Semaphore` is not portable
  across loops, and pytest-asyncio creates a fresh loop per test.
- **Mock provider recall is deliberately gated** (security 80%, correctness 60%, testing 60%)
  and it emits deterministic false positives. A mock that scores 100% would prove nothing.
- **`token_budget = 0` means unlimited.** Chosen over a sentinel like `-1`.
- Unknown model -> cost 0.0 plus a one-time warning per model, never a crash.
- Prompt payload format is fixed as `FILE: <path>` then `<lineno>| <code>`, because the mock
  provider has to parse back out of the rendered prompt. `DiffHunk.render()` is the one
  producer of this format.

### Left for later phases
- `diff.py`, `sanitizer.py` (Phase 2); nodes/prompts/graph (Phase 3); eval suite (Phase 4).

---

## Phase 2 — diff parsing and prompt-injection defence

**Status:** complete

### Done
- `diff.py`: tolerant unified-diff parser -> `DiffHunk(file, start_line, added_lines)` with
  **new-file** line numbers. Skips deleted files, binaries and lockfiles. `render_hunks()` is the
  single producer of the `FILE:` / `<line>| <code>` prompt format.
- `sanitizer.py`: 9 detection patterns + 2 structural unicode checks, homoglyph folding,
  invisible-character stripping, inert-marker substitution, nonce-fenced wrapping,
  `injection_finding()` -> blocker/security/`prompt-injection-in-diff`.
- `eval/fixtures/injection.patch`: realistic PR carrying both a real vulnerability
  (world-writable chmod, pickle of user input, path traversal) **and** an attack on the
  reviewer. Trips 8 independent rules.
- `tests/test_injection.py` (6 attacks x 3 assertions each, 3 benign inputs x 2, plus
  neutralisation/nonce/attribution tests) and `tests/test_diff.py`.
- 99 tests passing.

### Decisions
- **Detection runs on a normalised copy, never on what is sent to the model.** Homoglyphs are
  folded and zero-width characters stripped *before* the pattern scan, so `іgnore` (Cyrillic і)
  and `ign​ore` are both caught by the plain-English pattern. The unicode trick is *also*
  reported in its own right.
- **Homoglyph detection is scoped to words mixing Latin with Cyrillic/Greek.** An all-Cyrillic
  comment and accented Latin (`café`) are legitimate and must not fire. Tested both ways.
- **Neutralisation replaces the matched span with `[NEUTRALISED:<rule>]` rather than deleting the
  line.** The reviewer still sees that something was there; the sanitizer raises the blocker
  finding itself, so no information is lost by not showing the model the live payload.
- **Patterns require instruction *shape*, not keywords.** `override-previous-instructions` needs an
  imperative verb, a scope word and an instruction noun in sequence. This is what keeps
  "see the setup instructions in README.md" quiet.
- **The fence nonce is 16 hex chars from `secrets`, regenerated per run.** An attacker cannot
  forge a closing fence for a tag they cannot predict; the test asserts a guessed fence fails.
- Parser is deliberately **tolerant, not strict**: a truncated or malformed patch returns the
  hunks it could recover. A parse failure must not be able to take down a review.

---
