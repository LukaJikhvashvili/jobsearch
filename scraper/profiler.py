"""
Two-phase site profiler.

Phase 1 — Listings capture
    Open the listings page with Playwright.
    Extract and clean the HTML.
    Pass to SchemaGenerator.generate_phase1() → partial adapter dict.

Phase 2 — Detail navigation + capture
    Use the navigation config from Phase 1 to find and visit ONE real detail page.
    Extract and clean that HTML.
    Pass to SchemaGenerator.generate_phase2() → full SiteAdapter.

The Profiler is the only place that knows about Playwright during generation.
SchemaGenerator stays pure (AI calls only).

Usage:
    profiler = SiteProfiler(generator)
    adapter  = await profiler.profile("jobs.ge", "https://jobs.ge/en/")
"""

import logging
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright, BrowserContext, Page

from .html_cleaner import clean_html
from .models import SiteAdapter
from .schema_generator import SchemaGenerator

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " "AppleWebKit/537.36 (KHTML, like Gecko) " "Chrome/124.0.0.0 Safari/537.36"
)


class SiteProfiler:
    """
    Orchestrates the two-phase schema generation process using a live browser.
    """

    def __init__(self, generator: SchemaGenerator, headless: bool = True, wait_ms: int = 2000):
        self.generator = generator
        self.headless = headless
        self.wait_ms = wait_ms

    # ------------------------------------------------------------------ public

    async def profile(self, site: str, listings_url: str) -> SiteAdapter:
        """
        Full two-phase profile: open listings → Phase 1 LLM call →
        navigate to detail → Phase 2 LLM call → return SiteAdapter.
        """
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            try:
                adapter = await self._run(context, site, listings_url)
            finally:
                await browser.close()

        return adapter

    # ------------------------------------------------------------------ internals

    async def _run(self, context: BrowserContext, site: str, listings_url: str) -> SiteAdapter:

        # ── Phase 1: Listings ─────────────────────────────────────────────────
        logger.info("[Phase 1] Loading listings page: %s", listings_url)
        listings_page = await context.new_page()
        await listings_page.goto(listings_url, wait_until="domcontentloaded", timeout=30_000)
        await listings_page.wait_for_timeout(self.wait_ms)

        # Mild scroll to trigger lazy-loaded cards
        await listings_page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.4)")
        await listings_page.wait_for_timeout(800)

        raw_listings_html = await listings_page.content()
        listings_html = clean_html(raw_listings_html, max_chars=40_000)

        logger.info("[Phase 1] Sending %d chars to LLM …", len(listings_html))
        partial = self.generator.generate_phase1(site, listings_url, listings_html)

        # ── Phase 2: Detail navigation ────────────────────────────────────────
        logger.info("[Phase 2] Navigating to a detail page …")
        detail_url, detail_html = await self._get_detail(context, listings_page, partial, listings_url)
        await listings_page.close()

        if not detail_html:
            raise RuntimeError(
                f"[Phase 2] Could not reach a detail page for {site}. " "Try providing a detail_url manually."
            )

        logger.info("[Phase 2] Detail page captured: %s (%d chars)", detail_url, len(detail_html))
        adapter = self.generator.generate_phase2(partial, detail_url, detail_html)
        return adapter

    async def _get_detail(
        self, context: BrowserContext, listings_page: Page, partial: dict, listings_url: str
    ) -> tuple[str, str]:
        """
        Use the navigation config from Phase 1 to reach a real detail page.
        Returns (detail_url, cleaned_detail_html).
        Falls back to heuristic link-finding if navigation config is unreliable.
        """
        nav = partial.get("listings", {}).get("navigation", {})
        nav_type = nav.get("type")
        nav_confidence = nav.get("confidence", 1.0)
        container_sel = partial.get("listings", {}).get("container", "")

        # ── Strategy A: soup-based URL extraction (fast, no extra request) ──
        if nav_confidence >= 0.70 and nav_type in ("direct_link", "data_attr", "button_click"):
            url = await self._extract_url_from_soup(listings_page, nav, container_sel, partial.get("base_url", ""))
            if url:
                html = await self._fetch_detail_html(context, url)
                if html:
                    return url, html

        # ── Strategy B: Playwright click (card_click or low-confidence) ──
        if nav_type in ("card_click", "button_click") or nav_confidence < 0.70:
            url = await self._click_first_card(context, listings_page, container_sel, nav, listings_url)
            if url:
                html = await self._fetch_detail_html(context, url)
                if html:
                    return url, html

        # ── Strategy C: pure heuristic fallback ──
        logger.warning("[Phase 2] Navigation config unusable, falling back to heuristic …")
        url = await self._heuristic_first_link(listings_page, listings_url)
        if url:
            html = await self._fetch_detail_html(context, url)
            if html:
                return url, html

        return "", ""

    # ------------------------------------------------------------------ soup extraction

    async def _extract_url_from_soup(self, page: Page, nav: dict, container_sel: str, base_url: str) -> Optional[str]:
        """
        Extract the first detail URL from the listings page HTML using BeautifulSoup
        and the navigation config — no extra browser request needed.
        """
        from bs4 import BeautifulSoup

        html = await page.content()
        soup = BeautifulSoup(html, "lxml")

        cards = soup.select(container_sel) if container_sel else []
        if not cards:
            return None

        card = cards[0]
        nav_type = nav.get("type")
        link_sel = nav.get("link_selector")
        data_attr = nav.get("data_attribute")

        if nav_type == "direct_link":
            el = card.select_one(link_sel) if link_sel else card.find("a", href=True)
            if el:
                href = el.get("href", "")
                return urljoin(base_url, href) if href else None

        elif nav_type == "data_attr" and data_attr:
            el = card if card.get(data_attr) else card.select_one(f"[{data_attr}]")
            val = el.get(data_attr) if el else None
            return urljoin(base_url, val) if val else None

        elif nav_type == "button_click":
            # Button might still have an href
            el = card.select_one(link_sel) if link_sel else None
            if el:
                href = el.get("href", "")
                return urljoin(base_url, href) if href else None

        return None

    # ------------------------------------------------------------------ Playwright click

    async def _click_first_card(
        self, context: BrowserContext, listings_page: Page, container_sel: str, nav: dict, listings_url: str
    ) -> Optional[str]:
        """
        Open a fresh tab, reload the listings page, click the first card
        (or its button), and capture the URL we land on.
        """
        tab = await context.new_page()
        try:
            await tab.goto(listings_url, wait_until="domcontentloaded", timeout=30_000)
            await tab.wait_for_timeout(self.wait_ms)

            if not container_sel:
                return None

            cards = tab.locator(container_sel)
            if await cards.count() == 0:
                return None

            first_card = cards.first
            link_sel = nav.get("link_selector")
            click_container = nav.get("click_container", False)

            if click_container or not link_sel:
                target = first_card
            else:
                target = first_card.locator(link_sel).first

            async with tab.expect_navigation(timeout=15_000):
                await target.click()

            detail_url = tab.url
            logger.debug("[Phase 2] Clicked to: %s", detail_url)
            return detail_url

        except Exception as exc:
            logger.warning("[Phase 2] Click navigation failed: %s", exc)
            return None
        finally:
            await tab.close()

    # ------------------------------------------------------------------ heuristic fallback

    async def _heuristic_first_link(self, page: Page, base_url: str) -> Optional[str]:
        """
        Last resort: scan all <a> tags for anything that looks like a job detail URL.
        """
        import re
        from bs4 import BeautifulSoup

        html = await page.content()
        soup = BeautifulSoup(html, "lxml")

        job_hints = ["/job/", "/vacancy/", "/position/", "/careers/", "/jobs/", "/offer/", "/posting/"]
        id_pattern = re.compile(r"/\d{3,}[/?#]?")

        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            full = urljoin(base_url, href)
            if any(hint in full for hint in job_hints):
                return full

        for a in soup.find_all("a", href=True):
            href = a["href"]
            full = urljoin(base_url, href)
            if id_pattern.search(href):
                return full

        return None

    # ------------------------------------------------------------------ detail fetch

    async def _fetch_detail_html(self, context: BrowserContext, url: str, max_chars: int = 30_000) -> str:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(self.wait_ms)
            raw = await page.content()
            return clean_html(raw, max_chars=max_chars)
        except Exception as exc:
            logger.warning("[Phase 2] Failed to load detail page %s: %s", url, exc)
            return ""
        finally:
            await page.close()
