# AgentGate — Build Progress

Running log of what was built, what was decided, and what remains.
Decisions are recorded so a later session can resume cold. The phase-by-phase
history is at the bottom; **current status and remaining work are here at the top.**

---

## Status: complete and working, with known gaps

**Last updated:** 2026-09-02

| | |
| --- | --- |
| Tests | **330 passing**, offline, no API key required (~24s) |
| Commits | 14 (7 build phases + 7 fixes driven by the live run) |
| Working tree | clean |
| Mock-mode eval | 56.0% detection (14/25), 36.4% FP, 6 clean-run FPs |
| Live eval | Gemini Flash-Lite: 52.0% detection, 18.8% FP, 0 security/correctness clean FPs |
| Docker | `docker compose up` verified against current source; API + dashboard both serve |
| CI workflows | written and YAML-valid; **never executed on a real PR** |

### Resuming cold

```bash
python -m venv .venv && .venv/Scripts/activate     # or source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
pytest -q                                          # 330 pass, no key needed
agentgate eval --provider mock                     # regenerates eval_report.{json,md}
agentgate review --diff eval/fixtures/injection.patch   # must print block, exit 1
```

Everything defaults to the offline `mock` provider. A live provider is a `.env` edit;
`.env.example` carries measured per-provider notes.

---

## What's left to do

Ordered by how much it matters. Items 1 and 2 are the only places where shipped
behaviour falls short of the spec.

### 1. ~~Enforce `AGENTGATE_TOKEN_BUDGET_PER_EVAL`~~ — *done (dbb1b26)*

The spec asks for a token ceiling "per run **and per eval invocation**". The per-run
budget is enforced (`RunContext.check_budget`, aborts with `BudgetExceeded`). The
per-eval field exists in `config.py:122` and **is read by nothing** — `grep` returns
exactly one hit, its own definition.

*Fix:* thread a cumulative counter through `run_eval_async()` that sums
`ReviewResult.total_tokens` across all 20 reviews and aborts the same way the per-run
budget does. The abort path already exists (`ProviderUnavailable` is the model to
follow); this needs the accounting, a message naming budget vs consumed, and a test.

### 2. ~~Test the `--pr` GitHub fetch path~~ — *done (dbb1b26)*

`agentgate/cli.py:fetch_pr_diff()` has coverage for the malformed-reference case only.
The success path, the 404 branch and the auth header have **no test at all**.

*Fix:* an `httpx.MockTransport` test asserting the `Accept:
application/vnd.github.v3.diff` header, the constructed URL, a 200 returning diff text,
and the 404/4xx messages. No network needed. This is the same pattern already used in
`tests/test_providers.py`.

### 3. Run the workflows on a real pull request — *unverified*

`review.yml` and `eval-gate.yml` are written, YAML-validated, and every component they
invoke is tested locally. Neither has executed inside GitHub Actions. Unknowns:
the `github-script` comment create/update path, the base-ref diff on a real merge
commit, and whether `pull-requests: write` is sufficient in practice.

*Fix:* push to a GitHub remote, open a throwaway PR containing
`eval/fixtures/injection.patch`, confirm one comment appears, push again, confirm the
comment is *updated* rather than duplicated, and confirm the check fails red.

### 4. ~~Re-run the comparison against a pinned model~~ — *done 2026-09-30: $0.001596/review, 52.0% detection*

The measured live run used `gemini-flash-lite-latest`, and AgentGate deliberately
refuses to price a moving alias, so cost per review and cost per detected defect are
`$0.00` in the table. `.env` is now pinned to `gemini-3.5-flash-lite`, which *is* priced.

*Fix:* `agentgate eval --compare mock,openai_compat:gemini-3.5-flash-lite`
(~60 requests, ~10 min, pace it against the free-tier ceiling). Then update the
README comparison table. Detection/FP should land close to the alias run; the point is
turning the cost column from absent into measured.

### 5. More than one live run per provider — *n=1*

Every live number comes from a single pass of 20 reviews. Enough to be honest, not
enough for a confidence interval. Temperature is 0, but these models are not
bit-deterministic.

*Fix:* three runs per provider, report median and spread. Costs ~180 requests.

### 6. Exercise the `anthropic` provider for real — *stub only*

`AnthropicProvider` is covered by tests against a stubbed SDK client (message parsing,
out-of-band system prompt, rate-limit mapping). It has never made a real call, so the
SDK-version assumptions are unverified.

*Fix:* set an Anthropic key in `.env` and run the injection fixture. One review, 3 calls.

### 7. Housekeeping

- **Rotate the Gemini API key.** It was pasted into a chat transcript. `.env` is
  gitignored and untracked (verified), but the key should be treated as exposed.
- **Dashboard's eval panel is empty under Docker.** `eval_report.json` is written on the
  host; the container only mounts the `runs` volume. Either mount the report or run the
  eval inside the container. The sidebar accepts an arbitrary path, so it is
  configurable rather than broken.
- **`MODEL_PRICES` needs a revisit on 2027-01-01.** The `gemini-3.6-flash` and
  `gemini-3.7-flash` entries are promotional and double on that date; there is a comment
  in `config.py` saying so.

### Deliberately not done

- **No inline per-line PR comments.** The spec asks for a single formatted comment,
  updated in place. Inline review comments would need the Reviews API and a mapping from
  findings to diff positions.
- **Golden modules are fixtures, not runtime code.** `eval/golden/*.py` import `jwt` and
  `yaml`, which are not in `requirements.txt`. They are never imported or executed —
  only diffed as text. Deliberate: adding those deps would imply they run.
- **The mock provider will never score well on subtle defects.** Its 33% subtle-detection
  is a pattern-matching ceiling. That is the point of having a live comparison.

---

## Build history

Phase by phase, oldest first. Kept for the decision record.
**Test counts and commit counts inside this section are accurate as of that phase,**
not as of now — see the status table at the top for current figures.

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

## Phase 4 — golden eval suite with 25 seeded defects and provider comparison

**Status:** complete

### Done
- 10 clean, idiomatic modules in `eval/golden/` (jwt auth, db access, file upload, rate limiter,
  retry, csv parser, cache, payments, async job queue, config loader), 60–85 lines each.
- `eval/seed_defects.py` generates `eval/seeded/` **and** `eval/manifest.json` in one pass.
- Exactly 25 defects: 10 security, 10 correctness, 5 testing; 6 marked `subtle`.
- `eval/runner.py` — diff generation, greedy closest-line matching, metrics, clean-run pass,
  `--compare` support. `eval/report.py` — `eval_report.json` + `eval_report.md`.
- `tests/test_eval.py` (44 tests). **197 tests passing overall.**

### Mock-mode numbers (reproducible: `agentgate eval --provider mock`)

| Metric | Value |
| --- | --- |
| Detection rate | **56.0%** (14/25) |
| False-positive rate | **36.4%** (8/22 findings) |
| Subtle-defect detection | 33.3% (2/6) |
| Clean-run false positives | **6** across 10 clean modules |
| Per-category | security 80%, correctness 40%, testing 40% |
| p50 / p95 latency | ~150 ms / ~157 ms |
| Cost per review | $0.00 (mock provider is free by construction) |

### Decisions
- **Ground truth is generated, never hand-written.** `seed_defects.py` produces the seeded
  files and the manifest in the same pass, so a line number cannot drift from its defect.
  A test regenerates the manifest and asserts it equals the copy on disk.
- **Defect line numbers are resolved by an `anchor` searched *inside the replacement block only*.**
  A short anchor like `cur = conn.cursor()` appears elsewhere in the file; scoping the search to
  the block the seeder just wrote makes it unambiguous without inventing artificial markers.
- **Every defect must land on an *added* line.** A defect created purely by deleting code
  produces no added lines, so the reviewer would never see it and the measurement would be
  silently broken. `test_a_manifest_defect_line_survives_into_the_diff` enforces this for all 25.
- **Matching is greedy by closest line**, one defect per finding and one finding per defect.
  `cache.py` has two correctness defects 3 lines apart, so nearest-first assignment is what stops
  a single finding from claiming the wrong one.
- **Fixed a measurement bug:** the mock looked up ground truth by *basename*, so
  `eval/golden/cache.py` matched `eval/seeded/cache.py` and the mock "found" seeded defects in
  the clean modules — 27 fake clean-run FPs. Ground truth now matches on the full path, and a
  test asserts no seeded rule may ever appear in the clean-run results. Clean FPs dropped to 6,
  all of them genuinely invented findings.
- **False positives are decided once per (agent, file), not per line.** A per-line coin flip made
  the FP count a function of diff size, so the clean run (whole file added) drowned in them.
  The generator also refuses to land within ±3 lines of a real defect, so an invented finding can
  never accidentally score as a detection.
- The 5 testing defects are all `untested-*` rules anchored on newly added branches, which is
  exactly what the tests agent is told to look for. There is no separate seeded test file — the
  spec fixes the golden set at 10 source modules.
- 56% detection is the honest number. It is capped by the mock's deliberate recall gates
  (security 80%, correctness/testing 60%) and by subtle defects the pattern layer cannot see.

---

## Phase 5 — CLI, API, and CI quality gates

**Status:** complete

### Done
- `agentgate/cli.py` — `review` (`--diff` / `--pr` / stdin, `--json`, `--fail-on`,
  `--comment-file`), `eval` (`--provider`, `--out`, `--compare`, `--gate`), `serve`, `dashboard`.
- `agentgate/api.py` — `POST /review` (JSON or raw patch), `GET /runs`, `GET /runs/{run_id}`,
  `GET /metrics`, plus `GET /health`.
- `pyproject.toml` + `agentgate/__main__.py` so both `agentgate ...` and `python -m agentgate`
  work. Dependencies still come from `requirements.txt` — no poetry, no uv.
- `.github/workflows/review.yml` and `.github/workflows/eval-gate.yml`.
- `tests/test_cli_api.py` (40 tests). **237 tests passing.**

### Decisions
- **`--comment-file` writes the Markdown comment from Python**, and the workflow just posts the
  file. Formatting stays where it can be unit-tested rather than living in inline shell.
- **Comment updates are keyed on an HTML marker** (`<!-- agentgate-review -->`) rather than on
  the comment author, so re-runs update one comment instead of spamming the thread.
- **Pipes in finding text are escaped** — an unescaped `|` in a message would silently break the
  Markdown table. Tested.
- **`--fail-on block` is verdict-based; `high`/`medium` are severity-based.** `block` and `high`
  coincide today (a block *is* blocker-or-high), but keeping them separate means the verdict
  rule can change without silently changing what `--fail-on high` means.
- **A budget abort is HTTP 402, not 500.** The request was well-formed; the run was refused on
  cost grounds, and the caller needs to tell those apart.
- **The review workflow skips gracefully with a `::notice`** when `AGENTGATE_API_KEY` is unset,
  because a fork PR cannot see repository secrets and a hard failure there would be noise.
- **The eval-gate workflow re-runs `seed_defects.py` and fails on any diff**, so a hand-edited
  seeded module or a stale manifest is caught in CI rather than quietly skewing the numbers.
- The eval gate also re-runs the injection fixture end to end and asserts exit code 1, so the
  headline security claim is verified on every push rather than only in the unit tests.

---

## Phase 6 — telemetry dashboard and containerisation

**Status:** complete

### Done
- `agentgate/dashboard.py` — runs over time, p50/p95 latency per node, cost/tokens per review,
  findings by severity and category, injection-detection counts with a per-pattern breakdown,
  retry/429 count, and the latest eval report including the provider-comparison table.
- `runs/reviews.jsonl` — one summary line per completed review.
- `Dockerfile` (non-root, healthcheck, deps cached ahead of source) + `docker-compose.yml`
  (api on :8000, dashboard on :8501, shared `runs` volume, optional `.env`) + `.dockerignore`.
- **Verified for real:** built the image, ran `docker compose up`, posted the injection fixture
  to the containerised API (`block`, 4 findings), and loaded the dashboard in a browser.

### Decisions
- **Added a second log, `runs/reviews.jsonl`, rather than widening `NodeTrace`.** The node trace
  is one-row-per-node and carries cost and latency but no findings; the dashboard needs findings
  by severity and category. Keeping them separate leaves the trace schema clean.
- **Fixed a real bug caught only by opening the page.** `streamlit run agentgate/dashboard.py`
  executes the file as a top-level script with no parent package, so `from .config import ...`
  raised `ImportError` at render time. The HTTP healthcheck passed (Streamlit serves fine and
  only fails when the script runs), so this was invisible to `curl` — it took a screenshot to
  see it. Now uses absolute imports plus a `sys.path` guard, with
  `test_the_dashboard_runs_as_a_top_level_script` loading it via `runpy` exactly as Streamlit
  does.
- **`st.stop()` is followed by `sys.exit(0)` in the empty-log guard.** `st.stop()` only halts
  inside the Streamlit runtime; outside it the call is a no-op and the code below assumed a
  populated frame, raising `KeyError: 'run_id'`.
- The dashboard's fan-out panel computes the concurrency saving from live traces. Observed in
  the container: **125 ms wall clock against 368 ms of summed agent time — 66% saved.**
- The eval-report panel degrades to an empty state in the container, because the eval runs on the
  host. The sidebar takes an arbitrary path, so this is configurable rather than broken.

---

## Phase 7 — README

**Status:** complete

### Done
`README.md` with quickstart (free-tier provider as the default path), architecture including a
Mermaid diagram of the graph, results with real mock-mode numbers, a threat-model section with
the payload and before/after behaviour, a cost section, CLI/API reference, CI integration, and a
"reproduce every number" table mapping each claim to the command that produces it.

### Decisions
- **The results table carries the honest 56% detection**, with the weak axis (correctness, 40%)
  called out and explained rather than buried. Four of the six missed correctness defects are the
  ones marked subtle, which is exactly where a real model should beat this mock.
- **Live-model rows are explicit `TODO(live-model)` placeholders.** Filling them with numbers I
  have not run would be the one thing the spec says not to do.
- **Cost is split into "measured" and "projected".** The only measured spend is $0.00 (mock).
  What is genuinely measured is token volume — 3,119 in / 228 out per review — and the per-model
  table is that volume multiplied by the `MODEL_PRICES` list prices, labelled as a projection and
  flagged as a floor for output tokens because a live model is more verbose than the mock.
- **The threat model states what it does *not* defend against** — paraphrased instructions with
  no pattern signature, semantic manipulation, and attacks outside the diff.

---

## Post-phase hardening — live-provider coverage

Closing the one Definition-of-Done item that was asserted but never demonstrated.

### Done
- `tests/test_providers.py` (29 tests): `openai_compat` driven through an `httpx` mock
  transport (happy path, request shape, 429 with `Retry-After`, HTTP-date `Retry-After`,
  5xx vs 4xx classification, timeouts, unparseable bodies), and `anthropic` driven through a
  stub SDK client (message parsing, out-of-band system prompt, rate-limit mapping).
- **The `.env`-only provider switch is now demonstrated, not claimed.**
  `test_the_same_command_runs_against_two_providers_with_no_code_change` runs the identical
  review twice against a **real localhost HTTP server** speaking the OpenAI-compatible
  protocol, changing only environment variables. It asserts the second run really used the
  endpoint (token counts match what the stub returned).
- **266 tests passing.**

### Decisions
- **`RunContext` is now the single source of truth for run totals.** Cost was previously
  summed from `NodeTrace` rows, which are only produced inside a `@traced` node — so a call
  made outside one was charged tokens but not cost. The LLM layer now charges both to the run
  context, and the per-node trace remains the *breakdown* rather than the total. Found by a test
  that called `llm.complete` directly.
- The `openai_compat` tests inject an `httpx.MockTransport` into the provider's client rather
  than monkeypatching `httpx.post`, so the real request construction, header and URL logic runs.
- The provider-switch test uses a real socket rather than a mock transport, because the claim
  being verified is that *nothing but configuration* changes — a mock transport would be a code
  change smuggled into the test.

### Definition of done — verified at this point in the build

*(Historical snapshot. Current status is in the table at the top of this file.)*

| Requirement | Status |
| --- | --- |
| `pytest` passes with no network and no API key | 266 passed |
| `agentgate eval --provider mock` writes real numbers | 56.0% detection, 36.4% FP, 6 clean FPs |
| Injection fixture detected and returns `block` | verdict `block`, exit code 1 |
| Provider switch needs only a `.env` edit | verified against a live localhost endpoint |
| A 429 is survived by backoff | 6 tests, including through the real provider |
| `docker compose up` starts API and dashboard | verified; both exercised in a browser |
| README documents how to reproduce every number | "Reproducing every number" table |
| `git log` shows at least 7 commits | 8 |

## Live-model run — measured results and what they cost to learn

Ran the golden suite against a real Gemini free-tier key. Seven bugs, none of which the
mock-mode green board could have surfaced.

### Measured comparison (one full run each, 20 reviews per provider)

| | `mock` | `gemini-flash-lite-latest` |
| --- | --- | --- |
| Detection | 56.0% (14/25) | 52.0% (13/25) |
| FP rate, seeded diffs | 36.4% (8/22) | **18.8%** (3/16) |
| Clean-run FPs | 6 | 24 |
| — of which security/correctness | 1 | **0** |
| Subtle | 33.3% | 16.7% |
| security / correctness / testing | 8/10, 4/10, 2/5 | 7/10, 3/10, 3/5 |
| p50 / p95 | 389 / 395 ms | 3,852 / 19,880 ms |
| Tokens per review | 3,346 | 3,630 (3,411 in / 220 out) |
| Agent failures | 0/60 | 0/60 (3 rate limits survived by backoff) |

### Bugs found by going live

1. **The test suite was never hermetic.** Module-scoped fixtures initialise before
   function-scoped ones, so the moment a real `.env` existed, `pytest` called the live API —
   196s and four failures. Offline pin is now session-scoped, plus a canary fixture.
2. **Thinking tokens were unbilled.** Gemini reports reasoning tokens only in `total_tokens`
   (`prompt=18, completion=12, total=173`). Cost was understated ~10x.
3. **Output budget truncated the JSON.** Reasoning draws from the same budget; at 1,400 the
   `tests` agent degraded on every review.
4. **Backoff ignored the real retry delay.** Google sends 429 with no `Retry-After`; the wait is
   in the body. We waited ~2s when asked for ~17 and burned every attempt.
5. **A degraded agent was traced as successful.** A run where all three agents died looked
   healthy on the dashboard.
6. **Retries were only counted on success.** A call that burned six attempts reported
   `retry_count=0` — the case where it matters most.
7. **`--compare` switched provider but not model**, mislabelling rows.

### Design corrections

- **Retrying into a hard quota is self-harm, not resilience.** A 429 retry spends another
  request from the quota being waited on. The eval now aborts (`ProviderUnavailable`, exit 3)
  after two consecutive reviews in which every agent degraded, rather than grinding for
  20 minutes producing numbers that measure the rate limiter.
- **Aliases are never priced.** `gemini-flash-lite-latest` returns no price plus a warning to
  pin a concrete id, even though its likely target is in the table. A repointed alias yields a
  silently wrong cost, which is worse than a loudly absent one.
- **`MODEL_PRICES` refreshed from Google's published pricing** (checked 2026-09-02), with the
  retired `gemini-2.0-flash` removed and a note that 3.6/3.7 Flash prices double on 2027-01-01.

### An honest flaw in my own eval

The clean run presents each module as an **entire newly added file**, so all 24 of Gemini's
clean-run findings are the tests agent correctly observing that new code arrives with no tests
(18 are `untested-error-path`). On that diff those are *true statements*, counted as false
positives only because the manifest has no such defect. The report now splits clean-run FPs by
category: the unambiguous precision number is the security/correctness count, which is **0**.

The mock's 56% is **not** a competing score — it is seeded from the answer key with gated
recall. It exists to make the harness deterministic at zero cost, not to beat a real model.
