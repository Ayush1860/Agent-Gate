"""The two live providers, exercised without a live provider.

``openai_compat`` is driven through an ``httpx`` mock transport and, for the
end-to-end provider-switch proof, through a real HTTP server on localhost.
``anthropic`` is driven through a stub standing in for the SDK client.

Nothing here reaches the internet and nothing needs an API key.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from agentgate import config, llm
from agentgate.llm.anthropic import AnthropicProvider
from agentgate.llm.base import FatalLLMError, RateLimitError, TransientError
from agentgate.llm.openai_compat import OpenAICompatProvider

VERDICT_JSON = json.dumps(
    {
        "agent": "security",
        "findings": [
            {
                "file": "app/db.py",
                "line": 14,
                "category": "security",
                "severity": "blocker",
                "rule": "sql-string-interpolation",
                "message": "Interpolated SQL.",
                "suggestion": "Parameterise it.",
                "confidence": 0.95,
            }
        ],
        "notes": None,
    }
)

MESSAGES = [
    {"role": "system", "content": "AGENT: security\n"},
    {"role": "user", "content": "FILE: app/db.py\n14| q = f\"...\"\n"},
]


def _provider(handler, model="gemini-2.0-flash") -> OpenAICompatProvider:
    """An openai_compat provider whose transport is a callable, not a socket."""
    provider = OpenAICompatProvider(
        model=model, api_key="test-key", base_url="https://example.invalid/v1"
    )
    provider._client = httpx.AsyncClient(
        base_url=provider.base_url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-key"},
    )
    return provider


def _ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gemini-2.0-flash",
            "choices": [{"message": {"role": "assistant", "content": VERDICT_JSON}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120},
        },
    )


# --------------------------------------------------------------------------- #
# openai_compat: the happy path
# --------------------------------------------------------------------------- #
async def test_a_successful_call_is_parsed_into_an_llm_response():
    resp = await _provider(_ok_response).raw_complete(MESSAGES)
    assert resp.text == VERDICT_JSON
    assert resp.input_tokens == 900
    assert resp.output_tokens == 120
    assert resp.provider == "openai_compat"
    assert resp.model == "gemini-2.0-flash"
    assert resp.latency_ms >= 0


async def test_the_request_body_carries_the_configured_model_and_messages():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _ok_response(request)

    await _provider(handler).raw_complete(MESSAGES)
    assert seen["model"] == "gemini-2.0-flash"
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert seen["temperature"] == 0.0
    assert seen["response_format"] == {"type": "json_object"}


async def test_the_call_goes_to_the_chat_completions_path():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _ok_response(request)

    await _provider(handler).raw_complete(MESSAGES)
    assert seen["url"] == "https://example.invalid/v1/chat/completions"


async def test_a_response_with_no_usage_block_does_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}]}
        )

    resp = await _provider(handler).raw_complete(MESSAGES)
    assert resp.input_tokens == 0 and resp.output_tokens == 0


async def test_reasoning_tokens_are_counted_as_output():
    """Thinking models bill reasoning tokens but report them only in total_tokens.

    Gemini 3.x returns e.g. prompt=18, completion=12, total=173 -- the missing 143
    are reasoning tokens. Trusting completion_tokens alone understates cost by an
    order of magnitude on a reasoning-heavy review.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gemini-3.6-flash",
                "choices": [{"message": {"content": VERDICT_JSON}}],
                "usage": {
                    "prompt_tokens": 18,
                    "completion_tokens": 12,
                    "total_tokens": 173,
                },
            },
        )

    resp = await _provider(handler).raw_complete(MESSAGES)
    assert resp.input_tokens == 18
    assert resp.output_tokens == 155  # 12 visible + 143 reasoning
    assert resp.total_tokens == 173


async def test_a_consistent_usage_block_is_left_alone():
    """When total_tokens == prompt + completion there is nothing to reconcile."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 120,
                    "total_tokens": 1020,
                },
            },
        )

    resp = await _provider(handler).raw_complete(MESSAGES)
    assert (resp.input_tokens, resp.output_tokens) == (900, 120)


async def test_a_missing_total_does_not_inflate_the_output_count():
    resp = await _provider(_ok_response).raw_complete(MESSAGES)
    assert (resp.input_tokens, resp.output_tokens) == (900, 120)


# --------------------------------------------------------------------------- #
# openai_compat: failure classification
# --------------------------------------------------------------------------- #
async def test_a_429_becomes_a_rate_limit_error_carrying_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"}, text="slow down")

    with pytest.raises(RateLimitError) as excinfo:
        await _provider(handler).raw_complete(MESSAGES)
    assert excinfo.value.retry_after == 12.0


async def test_an_http_date_retry_after_is_ignored_rather_than_crashing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )

    with pytest.raises(RateLimitError) as excinfo:
        await _provider(handler).raw_complete(MESSAGES)
    assert excinfo.value.retry_after is None  # falls back to computed backoff


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_is_transient(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream is unwell")

    with pytest.raises(TransientError):
        await _provider(handler).raw_complete(MESSAGES)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_4xx_is_fatal_and_not_retried(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="bad request")

    with pytest.raises(FatalLLMError):
        await _provider(handler).raw_complete(MESSAGES)


async def test_a_timeout_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(TransientError, match="timed out"):
        await _provider(handler).raw_complete(MESSAGES)


async def test_an_unparseable_body_is_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(FatalLLMError, match="unparseable"):
        await _provider(handler).raw_complete(MESSAGES)


# --------------------------------------------------------------------------- #
# openai_compat through the shared call path
# --------------------------------------------------------------------------- #
async def test_a_rate_limited_provider_survives_a_429_via_backoff(monkeypatch):
    """The Definition-of-Done case: a 429 must be survived, not crash the run."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return _ok_response(request)

    delays: list[float] = []

    async def sleeper(seconds: float) -> None:
        delays.append(seconds)

    resp = await llm.complete(MESSAGES, provider=_provider(handler), sleeper=sleeper)
    assert calls["n"] == 3
    assert resp.retry_count == 2
    assert delays == [0.01, 0.01]  # Retry-After was honoured
    assert json.loads(resp.text)["agent"] == "security"


async def test_a_fatal_error_is_not_retried_through_the_shared_path():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(FatalLLMError):
        await llm.complete(MESSAGES, provider=_provider(handler))
    assert calls["n"] == 1


async def test_cost_is_priced_from_the_table_for_a_known_model():
    from agentgate.telemetry import new_run

    with new_run("cost-check") as ctx:
        await llm.complete(MESSAGES, provider=_provider(_ok_response))
    # gemini-2.0-flash is (0.10, 0.40) per Mtok: 900 in, 120 out.
    expected = (900 / 1e6) * 0.10 + (120 / 1e6) * 0.40
    assert ctx.cost_usd == pytest.approx(expected, rel=1e-6)
    assert ctx.tokens_consumed == 1020


# --------------------------------------------------------------------------- #
# anthropic
# --------------------------------------------------------------------------- #
class _StubBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _StubUsage:
    input_tokens = 800
    output_tokens = 90


class _StubMessage:
    model = "claude-haiku-4-5-20251001"
    content = [_StubBlock(VERDICT_JSON)]
    usage = _StubUsage()


def _anthropic_provider(create):
    provider = AnthropicProvider(model="claude-haiku-4-5-20251001", api_key="test-key")

    class _Messages:
        async def create(self, **kw):
            return create(**kw)

    class _Client:
        messages = _Messages()

    import anthropic

    provider._client = _Client()
    provider._anthropic = anthropic
    return provider


async def test_the_anthropic_provider_parses_a_message_response():
    resp = await _anthropic_provider(lambda **kw: _StubMessage()).raw_complete(MESSAGES)
    assert resp.text == VERDICT_JSON
    assert resp.input_tokens == 800
    assert resp.output_tokens == 90
    assert resp.provider == "anthropic"


async def test_the_system_prompt_is_passed_out_of_band():
    seen: dict = {}

    def create(**kw):
        seen.update(kw)
        return _StubMessage()

    await _anthropic_provider(create).raw_complete(MESSAGES)
    assert "AGENT: security" in seen["system"]
    # The Messages API takes only user/assistant turns.
    assert [m["role"] for m in seen["messages"]] == ["user"]


async def test_an_anthropic_rate_limit_maps_to_rate_limit_error():
    import anthropic

    def create(**kw):
        raise anthropic.RateLimitError(
            "slow down",
            response=httpx.Response(
                429,
                headers={"retry-after": "3"},
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
            body=None,
        )

    with pytest.raises(RateLimitError) as excinfo:
        await _anthropic_provider(create).raw_complete(MESSAGES)
    assert excinfo.value.retry_after == 3.0


async def test_a_message_with_no_user_turn_is_rejected():
    provider = _anthropic_provider(lambda **kw: _StubMessage())
    with pytest.raises(FatalLLMError, match="at least one user message"):
        await provider.raw_complete([{"role": "system", "content": "only a system turn"}])


# --------------------------------------------------------------------------- #
# Definition of done: switching provider is a .env edit, not a code change
# --------------------------------------------------------------------------- #
class _StubOpenAIHandler(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible /chat/completions endpoint on localhost."""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps(
            {
                "model": "stub-model-1",
                "choices": [{"message": {"role": "assistant", "content": VERDICT_JSON}}],
                "usage": {"prompt_tokens": 700, "completion_tokens": 80},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence the default stderr logging
        pass


@pytest.fixture
def stub_openai_server():
    server = HTTPServer(("127.0.0.1", 0), _StubOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    server.server_close()


SAMPLE_DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -12,2 +12,3 @@ import sqlite3
 def connect(path):
     return sqlite3.connect(path)
+    q = "SELECT 1"
"""


def test_the_same_command_runs_against_two_providers_with_no_code_change(
    stub_openai_server, monkeypatch
):
    """Run the identical review twice, changing only environment variables.

    This is the ``.env``-only provider switch from the Definition of Done. The
    second provider is a real HTTP server on localhost speaking the
    OpenAI-compatible protocol, so the whole ``openai_compat`` path is exercised
    end to end -- request, response, usage parsing and cost accounting.
    """
    from agentgate.graph import review

    # --- run 1: the mock provider ---
    monkeypatch.setenv("AGENTGATE_PROVIDER", "mock")
    monkeypatch.setenv("AGENTGATE_MODEL", "mock-reviewer-v1")
    config.get_settings.cache_clear()
    llm.reset_provider_cache()
    first = review(SAMPLE_DIFF, run_id="switch-mock")

    # --- run 2: an OpenAI-compatible endpoint. Only the environment changed. ---
    monkeypatch.setenv("AGENTGATE_PROVIDER", "openai_compat")
    monkeypatch.setenv("AGENTGATE_MODEL", "stub-model-1")
    monkeypatch.setenv("AGENTGATE_BASE_URL", stub_openai_server)
    monkeypatch.setenv("AGENTGATE_API_KEY", "not-a-real-key")
    config.get_settings.cache_clear()
    llm.reset_provider_cache()
    second = review(SAMPLE_DIFF, run_id="switch-compat")

    assert first.provider == "mock"
    assert second.provider == "openai_compat"
    assert second.model == "stub-model-1"

    # The second run really did talk to the stub over HTTP.
    assert second.total_tokens == 3 * (700 + 80)
    assert len(second.agents) == 3
    assert second.verdict == "block"
    assert any(f.rule == "sql-string-interpolation" for f in second.findings)


def test_an_unknown_model_costs_zero_rather_than_crashing_the_run(
    stub_openai_server, monkeypatch, caplog
):
    """`stub-model-1` is not in MODEL_PRICES; the run must still complete."""
    from agentgate.graph import review

    monkeypatch.setenv("AGENTGATE_PROVIDER", "openai_compat")
    monkeypatch.setenv("AGENTGATE_MODEL", "stub-model-1")
    monkeypatch.setenv("AGENTGATE_BASE_URL", stub_openai_server)
    monkeypatch.setenv("AGENTGATE_API_KEY", "not-a-real-key")
    config.get_settings.cache_clear()
    llm.reset_provider_cache()
    llm._WARNED_MODELS.discard("openai_compat:stub-model-1")

    with caplog.at_level("WARNING", logger="agentgate.llm"):
        result = review(SAMPLE_DIFF, run_id="unpriced")

    assert result.total_cost_usd == 0.0
    assert result.total_tokens > 0
    assert any("no price entry" in r.message for r in caplog.records)


def test_provider_instances_are_cached_per_configuration():
    first = llm.get_provider("mock", model="mock-reviewer-v1")
    second = llm.get_provider("mock", model="mock-reviewer-v1")
    assert first is second
    llm.reset_provider_cache()
    assert llm.get_provider("mock", model="mock-reviewer-v1") is not first


def test_every_registered_provider_implements_the_interface():
    from agentgate.llm.base import LLMProvider

    for name, cls in llm.PROVIDERS.items():
        assert issubclass(cls, LLMProvider), name
        assert cls.raw_complete is not LLMProvider.raw_complete, name


def test_the_event_loop_is_not_left_running():
    """Guards against a provider leaking an open client into the next test."""
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()
