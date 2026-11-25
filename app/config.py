"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["gemini", "openai", "anthropic"]

# Documented standard API token pricing (USD per 1M tokens) used for cost estimates.
MODEL_PRICING_PER_MILLION: dict[str, tuple[float, float]] = {
    # (input_usd_per_1m, output_usd_per_1m)
    # Google Gemini (active)
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.5-flash": (1.50, 9.00),
    # OpenAI (active)
    "gpt-6-luna": (0.10, 0.50),
    # Anthropic (active, sub-100K prompt tier)
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}


class Settings(BaseSettings):
    """Centralized application settings backed by .env / environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "AI-Powered Website Link Analysis API"
    app_version: str = "1.0.0"
    app_env: str = Field(default="development")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=4000, ge=1, le=65535)
    log_level: str = Field(default="INFO")

    # LLM Provider selection
    llm_provider: LLMProvider = Field(default="gemini")
    llm_fallback_provider: Optional[str] = Field(default="openai")

    # API Keys (never logged or returned)
    gemini_api_key: Optional[str] = Field(default=None)
    gemini_model: str = Field(default="gemini-3.5-flash-lite")

    openai_api_key: Optional[str] = Field(default=None)
    openai_model: str = Field(default="gpt-6-luna")

    anthropic_api_key: Optional[str] = Field(default=None)
    anthropic_model: str = Field(default="claude-haiku-5-5")

    # Extraction & network limits
    request_timeout_seconds: float = Field(default=15.0, gt=0)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    max_html_bytes: int = Field(default=2_097_152, ge=10_240)
    max_extracted_chars: int = Field(default=12_000, ge=1_000, le=60_000)
    enable_subpage_crawl: bool = Field(default=True)
    max_subpages: int = Field(default=2, ge=0, le=5)
    max_redirects: int = Field(default=5, ge=0, le=10)

    # Caching (cost optimization)
    cache_enabled: bool = Field(default=True)
    cache_ttl_seconds: int = Field(default=3600, ge=0)
    cache_max_size: int = Field(default=256, ge=1)

    def get_api_key(self, provider: str) -> Optional[str]:
        """Return the configured API key for a provider, ignoring placeholder values."""
        key_map = {
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }
        raw = key_map.get(provider)
        if not raw:
            return None
        cleaned = raw.strip()
        if not cleaned or cleaned.startswith("your_") or cleaned.endswith("_here"):
            return None
        return cleaned

    def get_model(self, provider: str) -> str:
        """Return the configured model name for a provider."""
        model_map = {
            "gemini": self.gemini_model,
            "openai": self.openai_model,
            "anthropic": self.anthropic_model,
        }
        return model_map[provider]

    def get_active_providers(self) -> list[str]:
        """Return ordered list of providers to try (primary, then fallback if valid)."""
        providers: list[str] = [self.llm_provider]
        fallback = (self.llm_fallback_provider or "").strip().lower()
        if fallback in ("gemini", "openai", "anthropic") and fallback != self.llm_provider:
            if self.get_api_key(fallback):
                providers.append(fallback)
        return providers


def estimate_llm_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> Optional[float]:
    """Estimate USD cost for a call from documented per-million-token rates."""
    rates = MODEL_PRICING_PER_MILLION.get(model)
    if not rates:
        return None
    in_rate, out_rate = rates
    cost = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
    return round(cost, 6)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
