"""Provider registry plus the shared call path.

Every LLM call in AgentGate goes through :func:`complete`, which owns the four
things that must not be duplicated per provider:

* a **concurrency semaphore**, so a parallel fan-out does not trip free-tier limits;
* **exponential backoff with jitter** on 429/5xx, honouring ``Retry-After``;
* the **token budget**, which aborts loudly rather than silently burning credits;
* **cost accounting** against the ``MODEL_PRICES`` table in ``config.py``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable

from ..config import cost_usd, get_settings, price_for
from ..models import LLMResponse
from ..telemetry import current_run, record_retries, record_usage
from .anthropic import AnthropicProvider
from .base import (
    FatalLLMError,
    LLMError,
    LLMProvider,
    Message,
    RateLimitError,
    TransientError,
)
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

log = logging.getLogger("agentgate.llm")

__all__ = [
    "AnthropicProvider",
    "FatalLLMError",
    "LLMError",
    "LLMProvider",
    "Message",
    "MockProvider",
    "OpenAICompatProvider",
    "PROVIDERS",
    "RateLimitError",
    "TransientError",
    "complete",
    "get_provider",
    "reset_provider_cache",
]

PROVIDERS: dict[str, type[LLMProvider]] = {
    "mock": MockProvider,
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
}

_PROVIDER_CACHE: dict[tuple[str, str, str], LLMProvider] = {}
_SEMAPHORES: dict[tuple[int, int], asyncio.Semaphore] = {}
_WARNED_MODELS: set[str] = set()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def get_provider(
    name: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> LLMProvider:
    """Return the configured provider. Reads ``AGENTGATE_PROVIDER`` unless overridden."""
    settings = get_settings()
    pname = (name or settings.provider).strip().lower()
    if pname not in PROVIDERS:
        raise FatalLLMError(
            f"unknown provider {pname!r}; expected one of {', '.join(sorted(PROVIDERS))}"
        )
    pmodel = model or settings.model
    key = (pname, pmodel, base_url if base_url is not None else settings.base_url)
    cached = _PROVIDER_CACHE.get(key)
    if cached is not None:
        return cached
    provider = PROVIDERS[pname](
        model=pmodel,
        api_key=settings.api_key if api_key is None else api_key,
        base_url=settings.base_url if base_url is None else base_url,
    )
    _PROVIDER_CACHE[key] = provider
    return provider


def reset_provider_cache() -> None:
    """Drop cached provider instances. Used by tests and by ``--compare``."""
    _PROVIDER_CACHE.clear()
    _SEMAPHORES.clear()


# --------------------------------------------------------------------------- #
# Shared call path
# --------------------------------------------------------------------------- #
def _semaphore(limit: int) -> asyncio.Semaphore:
    """One semaphore per (event loop, limit). Semaphores are not loop-portable."""
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, limit)
    sem = _SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _SEMAPHORES[key] = sem
    return sem


def _sleep_for(attempt: int, exc: Exception, base: float, cap: float) -> float:
    """Exponential backoff with full jitter, overridden by ``Retry-After``."""
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        return min(float(retry_after), cap)
    window = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0.0, window)


def _estimate_prompt_tokens(messages: list[Message]) -> int:
    return max(1, sum(len(m.get("content", "")) for m in messages) // 4)


async def complete(
    messages: list[Message],
    provider: LLMProvider | None = None,
    *,
    sleeper: Callable[[float], Any] | None = None,
    **kw: Any,
) -> LLMResponse:
    """Issue one completion with retries, concurrency limiting and accounting.

    ``sleeper`` exists so the backoff test does not have to wait in real time.
    """
    settings = get_settings()
    prov = provider or get_provider()
    sleep = sleeper or asyncio.sleep

    run = current_run()
    projected = _estimate_prompt_tokens(messages) + settings.max_output_tokens
    if run is not None:
        run.check_budget(projected)

    last_error: Exception | None = None
    retry_count = 0

    async with _semaphore(max(1, settings.concurrency)):
        for attempt in range(1, max(1, settings.max_attempts) + 1):
            try:
                resp = await prov.raw_complete(messages, **kw)
            except (RateLimitError, TransientError) as exc:
                last_error = exc
                if attempt >= settings.max_attempts:
                    break
                delay = _sleep_for(attempt, exc, settings.backoff_base_s, settings.backoff_max_s)
                retry_count += 1
                log.warning(
                    "%s attempt %d/%d failed (%s); retrying in %.2fs",
                    prov.name,
                    attempt,
                    settings.max_attempts,
                    type(exc).__name__,
                    delay,
                )
                await sleep(delay)
                continue
            except FatalLLMError:
                raise

            resp.retry_count = retry_count
            _account(prov, resp)
            return resp

    record_retries(retry_count)
    raise TransientError(
        f"{prov.name} failed after {settings.max_attempts} attempts: {last_error}"
    ) from last_error


def _account(prov: LLMProvider, resp: LLMResponse) -> None:
    """Price the call and push it into the trace. An unknown model costs 0.0 plus a warning."""
    model = resp.model or prov.model
    key = f"{prov.name}:{model}"
    if price_for(prov.name, model) is None and key not in _WARNED_MODELS:
        _WARNED_MODELS.add(key)
        log.warning(
            "no price entry for %s; cost will be reported as 0.00 -- "
            "add it to MODEL_PRICES in agentgate/config.py",
            key,
        )
    cost = cost_usd(prov.name, model, resp.input_tokens, resp.output_tokens)
    record_usage(resp, cost)
