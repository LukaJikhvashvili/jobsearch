"""
SiteProfiler — single-phase listings-only schema generation.

Renders the listings page fully with Playwright, extracts:
  1. Sample job cards HTML   (for container/field/navigation detection)
  2. Pagination area HTML    (for pagination type detection)

Both sections use the RENDERED DOM (post-JS), so onclick and data-*
attributes are visible to the LLM.
"""

import logging
from playwright.async_api import async_playwright, BrowserContext, Page

from .html_cleaner import extract_pagination_area, clean_html
from .models import SiteAdapter
from .schema_generator import SchemaGenerator

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " "AppleWebKit/537.36 (KHTML, like Gecko) " "Chrome/124.0.0.0 Safari/537.36"
)
_JS_SETTLE_MS = 2500


class SiteProfiler:
    def __init__(self, generator: SchemaGenerator, headless: bool = True, js_settle_ms: int = _JS_SETTLE_MS):
        self.generator = generator
        self.headless = headless
        self.js_settle_ms = js_settle_ms

    async def profile(self, site: str, listings_url: str) -> SiteAdapter:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            try:
                adapter = await self._capture_and_generate(context, site, listings_url)
            finally:
                await browser.close()
        return adapter

    async def _capture_and_generate(self, context: BrowserContext, site: str, listings_url: str) -> SiteAdapter:
        logger.info("[Profiler] Loading: %s", listings_url)
        page = await context.new_page()

        await page.goto(listings_url, wait_until="domcontentloaded", timeout=30_000)
        await self._wait_for_js(page)
        await self._reveal_dynamic_content(page)

        raw_html = await page.content()
        cards_html = clean_html(raw_html, max_chars=60_000)
        pagination_html = extract_pagination_area(raw_html, max_chars=15_000)
        await page.close()

        logger.info(
            "[Profiler] Sending to LLM: cards=%d chars  pagination=%d chars",
            len(cards_html),
            len(pagination_html),
        )
        return self.generator.generate(site, listings_url, cards_html, pagination_html)

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
