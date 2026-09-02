"""Any OpenAI-compatible ``/chat/completions`` endpoint.

One implementation covers Google Gemini, xAI Grok, Groq, DeepSeek and OpenRouter.
Which one you get is decided entirely by ``AGENTGATE_BASE_URL`` / ``AGENTGATE_MODEL``
/ ``AGENTGATE_API_KEY`` -- switching provider is a ``.env`` edit, never a code change.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ..config import get_settings
from ..models import LLMResponse
from .base import FatalLLMError, LLMProvider, Message, RateLimitError, TransientError


#: Google returns the wait in the error *body* ("Please retry in 16.59s.") and in a
#: google.rpc.RetryInfo detail, with no Retry-After header at all. Falling back to
#: exponential backoff there means waiting ~2s when the server asked for ~17, so
#: the retries are burned for nothing and the agent degrades.
_BODY_DELAY_RE = re.compile(r"retry (?:in|after)\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.I)
_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"([0-9]+(?:\.[0-9]+)?)s"')


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait, from the header if present, otherwise from the body."""
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return float(raw)
        except ValueError:
            # HTTP-date form; not parsed, fall through to the body.
            pass

    try:
        body = response.text
    except Exception:  # pragma: no cover - body already consumed or undecodable
        return None

    for pattern in (_RETRY_DELAY_RE, _BODY_DELAY_RE):
        match = pattern.search(body)
        if match:
            return float(match.group(1))
    return None


class OpenAICompatProvider(LLMProvider):
    """Talks plain OpenAI chat-completions JSON. No vendor SDK involved."""

    name = "openai_compat"

    def __init__(self, model: str, api_key: str = "", base_url: str = "") -> None:
        super().__init__(model=model, api_key=api_key, base_url=base_url)
        if not self.base_url:
            raise FatalLLMError(
                "AGENTGATE_BASE_URL is required for the openai_compat provider "
                "(see .env.example for per-provider presets)."
            )
        if not self.api_key:
            raise FatalLLMError("AGENTGATE_API_KEY is required for the openai_compat provider.")
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=get_settings().request_timeout_s,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def raw_complete(self, messages: list[Message], **kw: Any) -> LLMResponse:
        settings = get_settings()
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "temperature": kw.get("temperature", settings.temperature),
            "max_tokens": kw.get("max_tokens", settings.max_output_tokens),
        }
        # Ask for JSON when the endpoint supports it; harmless noise when it does not.
        if kw.get("json_mode", True):
            body["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            resp = await self._http().post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise TransientError(f"request to {self.base_url} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientError(f"transport error talking to {self.base_url}: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if resp.status_code == 429:
            raise RateLimitError(
                f"rate limited by {self.base_url} ({resp.status_code})",
                retry_after=_retry_after(resp),
            )
        if resp.status_code >= 500:
            raise TransientError(f"upstream {resp.status_code} from {self.base_url}")
        if resp.status_code >= 400:
            # 4xx other than 429 will not fix itself on retry.
            raise FatalLLMError(f"{resp.status_code} from {self.base_url}: {resp.text[:400]}")

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise FatalLLMError(f"unparseable response from {self.base_url}: {exc}") from exc

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage.get("completion_tokens", 0) or 0)

        # Thinking models (Gemini 3.x, o-series) spend reasoning tokens that are
        # billed as output but reported only in total_tokens, not in
        # completion_tokens. Folding the difference in keeps cost honest --
        # otherwise a reasoning-heavy review looks far cheaper than it was.
        total_tokens = int(usage.get("total_tokens", 0) or 0)
        if total_tokens > input_tokens + output_tokens:
            output_tokens = total_tokens - input_tokens

        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=str(data.get("model") or self.model),
            provider=self.name,
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
