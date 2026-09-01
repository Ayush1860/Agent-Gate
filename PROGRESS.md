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
