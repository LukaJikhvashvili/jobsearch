"""Centralized configuration for the scraper system."""

from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


class PlaywrightConfig(BaseModel):
    headless: bool = False
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    viewport_width: int = 1280
    viewport_height: int = 900
    navigation_timeout_ms: int = 30_000
    js_settle_ms: int = 2500
    block_resources: list[str] = Field(default=["png", "jpg", "jpeg", "gif", "webp", "woff", "woff2", "ttf", "otf"])


class AIProviderConfig(BaseModel):
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-flash-latest"
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-haiku-4-5-20251001"
    max_output_tokens: int = 8192
    temperature: float = 0.1


class RetryConfig(BaseModel):
    max_attempts: int = 3
    initial_delay_s: float = 1.0
    max_delay_s: float = 30.0
    exponential_base: float = 2.0
    retryable_exceptions: list[str] = Field(default=["TimeoutError", "ConnectionError", "RuntimeError"])


class CacheConfig(BaseModel):
    enabled: bool = True
    html_cache_ttl_s: int = 3600  # 1 hour
    llm_cache_ttl_s: int = 86400  # 24 hours
    translation_cache_size: int = 512
    cache_directory: Path = Path(".cache/scraper")


class StorageConfig(BaseModel):
    adapter_directory: Path = Path("adapters")
    stale_after_days: int = 30


class PaginationDefaults(BaseModel):
    max_pages: int = 50
    delay_ms: int = 1200


class ScraperConfig(BaseModel):
    playwright: PlaywrightConfig = Field(default_factory=PlaywrightConfig)
    ai: AIProviderConfig = Field(default_factory=AIProviderConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    pagination: PaginationDefaults = Field(default_factory=PaginationDefaults)

    @classmethod
    def from_env(cls) -> "ScraperConfig":
        """Load config from environment variables with sensible defaults."""
        import os

        return cls(
            ai=AIProviderConfig(
                gemini_api_key=os.environ.get("GEMINI_API_KEY"),
                gemini_model=os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
                anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
                anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                max_output_tokens=int(os.environ.get("MAX_OUTPUT_TOKENS", "8192")),
            ),
            playwright=PlaywrightConfig(
                headless=os.environ.get("HEADLESS", "false").lower() == "true",
                js_settle_ms=int(os.environ.get("JS_SETTLE_MS", "2500")),
            ),
            retry=RetryConfig(
                max_attempts=int(os.environ.get("RETRY_MAX_ATTEMPTS", "3")),
            ),
            cache=CacheConfig(
                enabled=os.environ.get("CACHE_ENABLED", "true").lower() == "true",
            ),
            storage=StorageConfig(
                adapter_directory=Path(os.environ.get("ADAPTER_DIR", "adapters")),
                stale_after_days=int(os.environ.get("STALE_AFTER_DAYS", "30")),
            ),
        )
