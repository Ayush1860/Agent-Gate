"""The provider interface every backend implements.

Providers implement exactly one method -- :meth:`LLMProvider.raw_complete` -- and
concern themselves only with talking to their endpoint. Retries, concurrency
limiting, budget enforcement and cost accounting are shared and live in
``agentgate/llm/__init__.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypedDict

from ..models import LLMResponse


class Message(TypedDict):
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMError(RuntimeError):
    """Base class for provider failures."""


class FatalLLMError(LLMError):
    """Not worth retrying: bad key, bad request, unknown model."""


class TransientError(LLMError):
    """Retryable: 5xx, connection reset, timeout."""


class RateLimitError(TransientError):
    """HTTP 429. Carries ``Retry-After`` in seconds when the server supplied one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMProvider(ABC):
    """One backend. Stateless with respect to a run; safe to share across tasks."""

    #: Registry key, e.g. "mock", "openai_compat", "anthropic".
    name: str = "base"

    def __init__(self, model: str, api_key: str = "", base_url: str = "") -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @abstractmethod
    async def raw_complete(self, messages: list[Message], **kw: Any) -> LLMResponse:
        """Issue exactly one request. Raise :class:`RateLimitError` /
        :class:`TransientError` for retryable failures and :class:`FatalLLMError`
        otherwise. Do not retry in here."""

    async def aclose(self) -> None:
        """Release any held network resources. Override where needed."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} model={self.model!r}>"
