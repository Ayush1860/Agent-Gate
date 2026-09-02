"""Central configuration for AgentGate.

Everything tunable lives here or in the environment. No model name, base URL or
price may be hardcoded anywhere else in the codebase.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
RUNS_DIR = REPO_ROOT / "runs"
EVAL_DIR = REPO_ROOT / "eval"


# --------------------------------------------------------------------------- #
# Price table
# --------------------------------------------------------------------------- #
# USD per 1,000,000 tokens, as (input, output). Keyed by "provider:model".
# These are list prices at time of writing and will drift -- they are here so the
# cost numbers are reproducible and auditable, not because they are eternal.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # deterministic offline provider -- free by construction
    "mock:mock-reviewer-v1": (0.0, 0.0),
    # Anthropic (native SDK)
    "anthropic:claude-haiku-4-5-20251001": (1.00, 5.00),
    "anthropic:claude-sonnet-5": (3.00, 15.00),
    "anthropic:claude-opus-5": (5.00, 25.00),
    # Google Gemini via its OpenAI-compatible endpoint
    "openai_compat:gemini-2.0-flash": (0.10, 0.40),
    "openai_compat:gemini-2.5-flash": (0.30, 2.50),
    # xAI
    "openai_compat:grok-3-mini": (0.30, 0.50),
    # Groq
    "openai_compat:llama-3.3-70b-versatile": (0.59, 0.79),
    # DeepSeek
    "openai_compat:deepseek-chat": (0.27, 1.10),
    # OpenRouter (a representative free-tier route)
    "openai_compat:meta-llama/llama-3.3-70b-instruct:free": (0.0, 0.0),
}


def price_for(provider: str, model: str) -> tuple[float, float] | None:
    """Return (input_per_mtok, output_per_mtok) or None when the model is unknown."""
    return MODEL_PRICES.get(f"{provider}:{model}")


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost of one call. Unknown models cost 0.0 -- the caller warns, never crashes."""
    price = price_for(provider, model)
    if price is None:
        return 0.0
    inp, out = price
    return (input_tokens / 1_000_000.0) * inp + (output_tokens / 1_000_000.0) * out


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    """Environment-driven settings. Every field maps to an ``AGENTGATE_*`` env var."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTGATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- provider selection ---
    provider: str = Field(default="mock", description="mock | openai_compat | anthropic")
    model: str = Field(default="mock-reviewer-v1")
    api_key: str = Field(default="", description="Provider API key. Never commit this.")
    base_url: str = Field(default="", description="OpenAI-compatible /v1 base URL.")

    # --- generation ---
    temperature: float = 0.0
    max_output_tokens: int = 1400
    request_timeout_s: float = 60.0

    # --- rate-limit survival ---
    concurrency: int = Field(default=3, description="Max simultaneous in-flight LLM calls.")
    max_attempts: int = Field(default=4, description="Total tries per call, including the first.")
    backoff_base_s: float = 0.5
    backoff_max_s: float = 20.0

    # --- spend guards ---
    token_budget_per_run: int = Field(default=120_000)
    token_budget_per_eval: int = Field(default=2_000_000)

    # --- review policy ---
    max_findings: int = 20
    fail_on: str = Field(default="block", description="block | high | medium")

    # --- eval drift gate ---
    eval_min_detection: float = Field(default=0.40, description="CI fails below this rate.")
    eval_line_tolerance: int = Field(default=3, description="+/- lines for a finding to match.")

    # --- mock provider (offline determinism) ---
    mock_latency_ms: int = Field(
        default=120, description="Simulated per-call latency; lets tests prove real fan-out."
    )

    # --- telemetry ---
    trace_file: Path = Field(default=RUNS_DIR / "traces.jsonl")
    #: One line per completed review: verdict, findings by severity/category,
    #: injection flag. The node-level trace does not carry findings, and the
    #: dashboard needs them.
    review_file: Path = Field(default=RUNS_DIR / "reviews.jsonl")
    trace_enabled: bool = True

    def price_key(self) -> str:
        return f"{self.provider}:{self.model}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
