"""Native Anthropic SDK provider.

Kept separate from ``openai_compat`` because the Messages API takes the system
prompt out-of-band and reports usage under different keys.
"""

from __future__ import annotations

import time
from typing import Any

from ..config import get_settings
from ..models import LLMResponse
from .base import FatalLLMError, LLMProvider, Message, RateLimitError, TransientError


class AnthropicProvider(LLMProvider):
    """Wraps ``anthropic.AsyncAnthropic``. Imported lazily so the SDK stays optional."""

    name = "anthropic"

    def __init__(self, model: str, api_key: str = "", base_url: str = "") -> None:
        super().__init__(model=model, api_key=api_key, base_url=base_url)
        if not self.api_key:
            raise FatalLLMError("AGENTGATE_API_KEY is required for the anthropic provider.")
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
                raise FatalLLMError(
                    "the anthropic package is not installed; pip install -r requirements.txt"
                ) from exc
            self._anthropic = anthropic
            kwargs: dict[str, Any] = {
                "api_key": self.api_key,
                "timeout": get_settings().request_timeout_s,
                "max_retries": 0,  # retries are handled centrally in agentgate.llm
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    async def raw_complete(self, messages: list[Message], **kw: Any) -> LLMResponse:
        settings = get_settings()
        client = self._sdk()
        anthropic = self._anthropic

        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        turns = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        if not turns:
            raise FatalLLMError("anthropic provider requires at least one user message")

        started = time.perf_counter()
        try:
            resp = await client.messages.create(
                model=self.model,
                system="\n\n".join(system_parts) or anthropic.NOT_GIVEN,
                messages=turns,
                max_tokens=kw.get("max_tokens", settings.max_output_tokens),
                temperature=kw.get("temperature", settings.temperature),
            )
        except anthropic.RateLimitError as exc:
            retry_after = None
            headers = getattr(getattr(exc, "response", None), "headers", None)
            if headers is not None:
                try:
                    retry_after = float(headers.get("retry-after") or 0) or None
                except (TypeError, ValueError):
                    retry_after = None
            raise RateLimitError(f"anthropic rate limit: {exc}", retry_after=retry_after) from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise TransientError(f"anthropic transport error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientError(f"anthropic upstream {exc.status_code}: {exc}") from exc
            raise FatalLLMError(f"anthropic {exc.status_code}: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=text,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=str(getattr(resp, "model", self.model)),
            provider=self.name,
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                await close()
            self._client = None
