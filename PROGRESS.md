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

## Phase 3 — multi-agent review graph with parallel fan-out

**Status:** complete

### Done
- `prompts/security.md`, `correctness.md`, `tests.md` — each states one specialisation, the
  untrusted-data rule, JSON-only schema, and **two** few-shot examples (one defect, one clean).
- `nodes/__init__.py` — `ReviewState`, prompt loading, message building, JSON extraction,
  finding coercion, `run_specialist()` (call -> parse -> one repair -> degrade).
- `nodes/security.py`, `correctness.py`, `tests.py` — thin wiring over `run_specialist`.
- `nodes/aggregator.py` — dedupe on (file, line, rule), confidence boost on agreement,
  severity ranking, cap, verdict, and insertion of the sanitizer's blocker finding.
- `graph.py` — LangGraph `StateGraph`, `review_async()` / `review()`.
- `tests/test_graph.py` + `tests/test_aggregator.py`. **153 tests passing.**

### Decisions
- **Parallelism is proved from the trace, not from wall clock.** The test compares the fan-out
  *span* (min start -> max end across the three agent nodes) against the *sum* of their
  durations and asserts span < 60% of sum. A wall-clock threshold would be flaky on a loaded
  machine; this measures scheduling. A companion test sets `CONCURRENCY=1` and asserts the
  same graph *does* serialise, so the first test cannot pass vacuously.
- **`ReviewState.agents` and `.errors` use `operator.add` reducers.** LangGraph rejects
  concurrent writes to a plain key from parallel branches.
- **The fan-out is a conditional edge returning a list of node names.** Returning a list is
  what makes LangGraph schedule branches concurrently. It also lets an empty diff skip the
  agents entirely — zero tokens on a docs-only PR.
- **`ReviewResult.duration_ms` is wall clock, not the sum of node durations.** The specialists
  overlap; summing would overstate the review time by ~3x and make the telemetry dishonest.
- **The sanitizer's injection finding is added by the aggregator, not by an agent.** It is
  deterministic and must not depend on a model noticing the attack. If an agent *also* reports
  it, the sanitizer's copy wins and the duplicate is dropped.
- **Fixed a real bug found by the first end-to-end run:** the mock provider was parsing the
  *whole* prompt, so the few-shot examples in the system prompt were being reported as findings
  in `app/cache.py` and `tests/test_billing.py`. The mock now reads only the content inside the
  nonce fence. This is exactly the failure mode the fence exists to prevent, so it is a good
  argument for the design.
- Agents are coerced back into their own lane on parse (a `tests` agent reporting a `security`
  category gets rewritten), with one exception: `prompt-injection-in-diff` is always security.

---
