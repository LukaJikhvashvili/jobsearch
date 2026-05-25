"""Pipeline-based adapter generation with composable stages."""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import SiteAdapter
from .events import EventBus, Events

logger = logging.getLogger(__name__)


@dataclass
class GenerationContext:
    """Mutable context passed through the pipeline."""
    site: str
    listings_url: str
    # Populated by stages as they run
    raw_html: Optional[str] = None
    cleaned_html: Optional[str] = None
    llm_response: Optional[str] = None
    parsed_data: Optional[dict] = None
    adapter: Optional[SiteAdapter] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


class PipelineStage(ABC):
    """Base class for a pipeline stage."""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def process(self, context: GenerationContext) -> GenerationContext:
        ...


class HTMLCaptureStage(PipelineStage):
    """Renders the page with Playwright and captures HTML."""

    def __init__(self, playwright_config, cache=None):
        self.playwright_config = playwright_config
        self.cache = cache

    async def process(self, context: GenerationContext) -> GenerationContext:
        from playwright.async_api import async_playwright
        from .html_cleaner import clean_html

        # Check cache first
        if self.cache:
            cached = self.cache.get_html(context.listings_url)
            if cached:
                context.cleaned_html = cached
                logger.info("[Pipeline] HTML cache hit for %s", context.listings_url)
                return context

        cfg = self.playwright_config
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=cfg.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                user_agent=cfg.user_agent,
                viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
                locale="en-US",
            )
            try:
                page = await ctx.new_page()
                await page.goto(context.listings_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass
                await page.wait_for_timeout(cfg.js_settle_ms)
                # Scroll to reveal dynamic content
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.4)")
                await page.wait_for_timeout(600)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(800)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(400)

                raw_html = await page.content()
                context.raw_html = raw_html
                context.cleaned_html = clean_html(raw_html)
                await page.close()
            finally:
                await browser.close()

        if self.cache and context.cleaned_html:
            self.cache.set_html(context.listings_url, context.cleaned_html)

        return context


class LLMAnalysisStage(PipelineStage):
    """Sends cleaned HTML to the LLM and gets raw JSON response."""

    def __init__(self, schema_generator, cache=None):
        self.schema_generator = schema_generator
        self.cache = cache

    async def process(self, context: GenerationContext) -> GenerationContext:
        from .schema_generator import _PHASE1_SYSTEM, _phase1_user

        system = _PHASE1_SYSTEM
        user = _phase1_user(context.site, context.listings_url, context.cleaned_html)

        # Check LLM cache
        if self.cache:
            cached = self.cache.get_llm_response(system, user)
            if cached:
                context.llm_response = cached
                logger.info("[Pipeline] LLM cache hit for %s", context.site)
                return context

        raw = self.schema_generator._call(system, user, context.site)
        context.llm_response = raw

        if self.cache:
            self.cache.set_llm_response(system, user, raw)

        return context


class SchemaValidationStage(PipelineStage):
    """Parses and validates the LLM response into a SiteAdapter."""

    async def process(self, context: GenerationContext) -> GenerationContext:
        from .schema_generator import _repair_and_parse, _build_adapter

        data = _repair_and_parse(context.llm_response)
        context.parsed_data = data
        context.adapter = _build_adapter(data, context.site, context.listings_url)
        return context


class AdapterEnrichmentStage(PipelineStage):
    """Adds metadata and logs results."""

    async def process(self, context: GenerationContext) -> GenerationContext:
        adapter = context.adapter
        langs = adapter.page_languages
        n_filters = len(adapter.listings.filters.available)
        logger.info(
            "Schema generated  site=%s  nav=%s  pagination=%s  filters=%d  languages=%s",
            adapter.site,
            adapter.listings.navigation.type,
            adapter.listings.pagination.type,
            n_filters,
            langs,
        )
        return context


class AdapterGenerationPipeline:
    """Runs a sequence of stages to generate an adapter."""

    def __init__(self, stages: List[PipelineStage], event_bus: Optional[EventBus] = None):
        self.stages = stages
        self.event_bus = event_bus

    async def execute(self, site: str, listings_url: str) -> SiteAdapter:
        context = GenerationContext(site=site, listings_url=listings_url)

        if self.event_bus:
            await self.event_bus.publish(Events.ADAPTER_GENERATION_STARTED, site=site, url=listings_url)

        start_time = time.time()

        for stage in self.stages:
            logger.info("[Pipeline] Running stage: %s", stage.name)
            try:
                context = await stage.process(context)
            except Exception as exc:
                context.errors.append(f"{stage.name}: {exc}")
                if self.event_bus:
                    await self.event_bus.publish(
                        Events.ADAPTER_GENERATION_FAILED,
                        site=site, stage=stage.name, error=str(exc)
                    )
                raise

        duration = time.time() - start_time
        if self.event_bus:
            await self.event_bus.publish(
                Events.ADAPTER_GENERATION_COMPLETED,
                site=site, duration=duration
            )

        return context.adapter
