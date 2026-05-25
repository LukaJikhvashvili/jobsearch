"""
SiteProfiler — single-phase listings-only schema generation.

Renders the listings page fully with Playwright, extracts:
  1. Sample job cards HTML   (for container/field/navigation detection)
  2. Pagination area HTML    (for pagination type detection)

Both sections use the RENDERED DOM (post-JS), so onclick and data-*
attributes are visible to the LLM.
"""

import logging
import time
from typing import Optional
from playwright.async_api import async_playwright, BrowserContext, Page

from .config import PlaywrightConfig
from .html_cleaner import clean_html
from .models import SiteAdapter
from .schema_generator import SchemaGenerator
from .telemetry import TelemetryCollector

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " "AppleWebKit/537.36 (KHTML, like Gecko) " "Chrome/124.0.0.0 Safari/537.36"
)
_JS_SETTLE_MS = 2500


class SiteProfiler:
    def __init__(
        self,
        generator: SchemaGenerator,
        headless: bool = True,
        js_settle_ms: int = _JS_SETTLE_MS,
        playwright_config: Optional[PlaywrightConfig] = None,
        event_bus=None,
        telemetry: Optional[TelemetryCollector] = None,
    ):
        if playwright_config is not None:
            self._pw_config = playwright_config
        else:
            self._pw_config = PlaywrightConfig(
                headless=headless,
                js_settle_ms=js_settle_ms,
            )
        self.generator = generator
        self.headless = self._pw_config.headless
        self.js_settle_ms = self._pw_config.js_settle_ms
        self.event_bus = event_bus
        self.telemetry = telemetry

    async def profile(self, site: str, listings_url: str) -> SiteAdapter:
        cfg = self._pw_config
        if self.event_bus:
            await self.event_bus.publish("adapter_generation_started", site=site, url=listings_url)

        start = time.time()
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=cfg.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(
                    user_agent=cfg.user_agent,
                    viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
                    locale="en-US",
                )
                try:
                    adapter = await self._capture_and_generate(context, site, listings_url)
                finally:
                    await browser.close()

            if self.event_bus:
                await self.event_bus.publish("adapter_generation_completed", site=site)

            if self.telemetry:
                self.telemetry.track_adapter_generation(site, time.time() - start, success=True)
            return adapter
        except Exception as exc:
            if self.telemetry:
                self.telemetry.track_adapter_generation(site, time.time() - start, success=False, error=str(exc))
            raise

    async def _capture_and_generate(self, context: BrowserContext, site: str, listings_url: str) -> SiteAdapter:
        cfg = self._pw_config
        logger.info("[Profiler] Loading: %s", listings_url)
        page = await context.new_page()

        await page.goto(listings_url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout_ms)
        await self._wait_for_js(page)
        await self._reveal_dynamic_content(page)

        raw_html = await page.content()
        listings_html = clean_html(raw_html)
        await page.close()

        if self.event_bus:
            await self.event_bus.publish("page_captured", site=site, url=listings_url)

        logger.info("[Profiler] Sending to LLM: cards=%d chars", len(listings_html))
        return self.generator.generate(site, listings_url, listings_html)

    async def _wait_for_js(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        await page.wait_for_timeout(self.js_settle_ms)

    async def _reveal_dynamic_content(self, page: Page) -> None:
        """Scroll to reveal lazy content and pagination controls."""
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.4)")
        await page.wait_for_timeout(600)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(400)

    async def profile_with_pipeline(self, site: str, listings_url: str) -> SiteAdapter:
        """Alternative generation path using the composable pipeline."""
        from .pipeline import (
            AdapterGenerationPipeline,
            HTMLCaptureStage,
            LLMAnalysisStage,
            SchemaValidationStage,
            AdapterEnrichmentStage,
        )

        stages = [
            HTMLCaptureStage(self._pw_config),
            LLMAnalysisStage(self.generator),
            SchemaValidationStage(),
            AdapterEnrichmentStage(),
        ]
        pipeline = AdapterGenerationPipeline(stages=stages, event_bus=self.event_bus)
        return await pipeline.execute(site, listings_url)
