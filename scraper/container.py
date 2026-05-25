"""Dependency injection container — wires all components from config."""
from typing import Optional
from .config import ScraperConfig
from .events import EventBus
from .cache import ScraperCache
from .schema_generator import AIProvider, GeminiProvider, ClaudeProvider, SchemaGenerator
from .profiler import SiteProfiler
from .runner import ScraperRunner
from .adapter_store import AdapterStore
from .pipeline import (
    AdapterGenerationPipeline,
    HTMLCaptureStage,
    LLMAnalysisStage,
    SchemaValidationStage,
    AdapterEnrichmentStage,
)
from .models import SiteAdapter


class ScraperContainer:
    """Central factory that builds fully-wired components from config."""

    def __init__(self, config: Optional[ScraperConfig] = None, event_bus: Optional[EventBus] = None):
        self.config = config or ScraperConfig.from_env()
        self.event_bus = event_bus or EventBus()
        self._primary_provider: Optional[AIProvider] = None
        self._fallback_provider: Optional[AIProvider] = None

    def get_primary_provider(self) -> Optional[AIProvider]:
        if self._primary_provider is None and self.config.ai.gemini_api_key:
            self._primary_provider = GeminiProvider(
                api_key=self.config.ai.gemini_api_key,
                model=self.config.ai.gemini_model,
                max_output_tokens=self.config.ai.max_output_tokens,
                temperature=self.config.ai.temperature,
            )
        return self._primary_provider

    def get_fallback_provider(self) -> Optional[AIProvider]:
        if self._fallback_provider is None and self.config.ai.anthropic_api_key:
            self._fallback_provider = ClaudeProvider(
                api_key=self.config.ai.anthropic_api_key,
                model=self.config.ai.anthropic_model,
                max_output_tokens=self.config.ai.max_output_tokens,
            )
        return self._fallback_provider

    def get_schema_generator(self) -> SchemaGenerator:
        primary = self.get_primary_provider()
        if primary is None:
            raise RuntimeError("No primary AI provider configured (set GEMINI_API_KEY)")
        return SchemaGenerator(
            primary=primary,
            fallback=self.get_fallback_provider(),
            event_bus=self.event_bus,
        )

    def get_profiler(self) -> SiteProfiler:
        return SiteProfiler(
            generator=self.get_schema_generator(),
            playwright_config=self.config.playwright,
            event_bus=self.event_bus,
        )

    def get_runner(self, adapter: SiteAdapter) -> ScraperRunner:
        return ScraperRunner(
            adapter=adapter,
            playwright_config=self.config.playwright,
            event_bus=self.event_bus,
        )

    def get_adapter_store(self) -> AdapterStore:
        return AdapterStore(
            directory=self.config.storage.adapter_directory,
            stale_after_days=self.config.storage.stale_after_days,
        )

    def get_cache(self) -> ScraperCache:
        return ScraperCache(config=self.config.cache, event_bus=self.event_bus)

    def get_pipeline(self) -> AdapterGenerationPipeline:
        cache = self.get_cache()
        generator = self.get_schema_generator()
        stages = [
            HTMLCaptureStage(self.config.playwright, cache=cache),
            LLMAnalysisStage(generator, cache=cache),
            SchemaValidationStage(),
            AdapterEnrichmentStage(),
        ]
        return AdapterGenerationPipeline(stages=stages, event_bus=self.event_bus)
