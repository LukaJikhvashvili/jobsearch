"""
Two-phase site profiler.

Phase 1 — Listings capture
    Render the page fully with Playwright (post-JS content).
    Send TWO HTML sections to the LLM:
      • Full page  — for field/navigation/container detection
      • Pagination area — for pagination type detection
    Both use the RENDERED DOM, not raw source, so onclick/data-* attributes
    from JS frameworks are visible to the LLM.

Phase 2 — Detail navigation + capture
    Use the navigation config from Phase 1 to visit a real detail page.
    Detect login walls and attempt authentication before capturing detail HTML.

Auth
    If a login wall is encountered, raises AuthRequired unless credentials
    are stored. Call auth.store_credentials(site, user, pass) once manually.
"""

import logging
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright, BrowserContext, Page

from .auth import AuthRequired, attempt_login, is_login_wall
from .html_cleaner import clean_html, extract_pagination_area, clean_html
from .models import SiteAdapter
from .schema_generator import SchemaGenerator

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " "AppleWebKit/537.36 (KHTML, like Gecko) " "Chrome/124.0.0.0 Safari/537.36"
)

# How long to wait after page load for JS to finish rendering
_JS_SETTLE_MS = 2500


class SiteProfiler:
    def __init__(self, generator: SchemaGenerator, headless: bool = True, js_settle_ms: int = _JS_SETTLE_MS):
        self.generator = generator
        self.headless = headless
        self.js_settle_ms = js_settle_ms

    # ------------------------------------------------------------------ public

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
                adapter = await self._run(context, site, listings_url)
            finally:
                await browser.close()
        return adapter

    # ------------------------------------------------------------------ phase orchestration

    async def _run(self, context: BrowserContext, site: str, listings_url: str) -> SiteAdapter:

        # ── Phase 1: Listings ─────────────────────────────────────────────────
        logger.info("[Phase 1] Loading: %s", listings_url)
        listings_page = await context.new_page()

        await listings_page.goto(listings_url, wait_until="domcontentloaded", timeout=30_000)
        await self._wait_for_js(listings_page)

        # Scroll to trigger lazy-loaded content AND reveal infinite-scroll/load-more
        await self._reveal_dynamic_content(listings_page)

        # Capture RENDERED HTML (post-JS) for both sections
        raw_html = await listings_page.content()

        listings_html = clean_html(raw_html, max_chars=60_000)
        pagination_html = extract_pagination_area(raw_html, max_chars=10_000)

        logger.info(
            "[Phase 1] HTML sent: listings=%d chars  pagination=%d chars",
            len(listings_html),
            len(pagination_html),
        )
        partial = self.generator.generate_phase1(site, listings_url, listings_html, pagination_html)

        # ── Phase 2: Detail capture ───────────────────────────────────────────
        logger.info("[Phase 2] Navigating to detail page …")
        detail_url, detail_html = await self._get_detail(context, listings_page, partial, listings_url, site)
        await listings_page.close()

        if not detail_html:
            raise RuntimeError(f"[Phase 2] Could not capture a detail page for {site}.")

        logger.info("[Phase 2] Captured: %s (%d chars)", detail_url, len(detail_html))
        adapter = self.generator.generate_phase2(partial, detail_url, detail_html)
        return adapter

    # ------------------------------------------------------------------ JS rendering helpers

    async def _wait_for_js(self, page: Page) -> None:
        """Wait for JS frameworks to finish their initial render."""
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        await page.wait_for_timeout(self.js_settle_ms)

    async def _reveal_dynamic_content(self, page: Page) -> None:
        """
        Scroll the page in steps to trigger lazy-loading and reveal
        pagination controls / load-more buttons at the bottom.
        """
        # Scroll to 40% (reveals mid-page content)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.4)")
        await page.wait_for_timeout(600)
        # Scroll to bottom (reveals pagination / infinite scroll sentinel)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
        # Scroll back to top so card selectors work from natural position
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(400)

    # ------------------------------------------------------------------ detail navigation

    async def _get_detail(
        self, context: BrowserContext, listings_page: Page, partial: dict, listings_url: str, site: str
    ) -> tuple[str, str]:
        nav = partial.get("listings", {}).get("navigation", {})
        nav_type = nav.get("type")
        nav_confidence = nav.get("confidence", 0.0)
        container_sel = partial.get("listings", {}).get("container", "")
        base_url = partial.get("base_url", "")

        # Strategy A: static href extraction from rendered DOM
        if nav_confidence >= 0.70 and nav_type in ("direct_link", "data_attr", "button_click"):
            url = await self._url_from_rendered_dom(listings_page, nav, container_sel, base_url)
            if url:
                html = await self._fetch_detail_html(context, url, site)
                if html:
                    return url, html

        # Strategy B: Playwright click (card_click / button_click / low-confidence)
        if nav_type in ("card_click", "button_click") or nav_confidence < 0.70:
            url = await self._click_first_card(context, listings_page, container_sel, nav, listings_url)
            if url:
                html = await self._fetch_detail_html(context, url, site)
                if html:
                    return url, html

        # Strategy C: heuristic link scan
        logger.warning("[Phase 2] Falling back to heuristic link scan …")
        url = await self._heuristic_first_link(listings_page, listings_url)
        if url:
            html = await self._fetch_detail_html(context, url, site)
            if html:
                return url, html

        return "", ""

    async def _url_from_rendered_dom(self, page: Page, nav: dict, container_sel: str, base_url: str) -> Optional[str]:
        """
        Use Playwright's live DOM (not BeautifulSoup) to extract the first
        detail URL. This works on JS-rendered cards where BS4 would miss
        dynamically inserted href values.
        """
        nav_type = nav.get("type")
        link_sel = nav.get("link_selector")
        data_attr = nav.get("data_attribute")

        try:
            if not container_sel:
                return None

            first_card = page.locator(container_sel).first

            if nav_type == "direct_link":
                target = first_card.locator(link_sel).first if link_sel else first_card.locator("a").first
                href = await target.get_attribute("href", timeout=3_000)
                return urljoin(base_url, href) if href else None

            elif nav_type == "data_attr" and data_attr:
                # Try on the card itself first, then any child
                val = await first_card.get_attribute(data_attr, timeout=2_000)
                if not val:
                    child = first_card.locator(f"[{data_attr}]").first
                    val = await child.get_attribute(data_attr, timeout=2_000)
                return urljoin(base_url, val) if val else None

            elif nav_type == "button_click" and link_sel:
                target = first_card.locator(link_sel).first
                href = await target.get_attribute("href", timeout=2_000)
                return urljoin(base_url, href) if href else None

        except Exception as exc:
            logger.debug("_url_from_rendered_dom failed: %s", exc)

        return None

    async def _click_first_card(
        self, context: BrowserContext, listings_page: Page, container_sel: str, nav: dict, listings_url: str
    ) -> Optional[str]:
        """
        Open a fresh tab, reload listings, click the first card/button,
        capture the resulting URL. Handles both navigation events and
        URL changes without full navigation (SPA pattern).
        """
        tab = await context.new_page()
        try:
            await tab.goto(listings_url, wait_until="domcontentloaded", timeout=30_000)
            await self._wait_for_js(tab)

            if not container_sel:
                return None

            cards = tab.locator(container_sel)
            if await cards.count() == 0:
                return None

            first_card = cards.first
            link_sel = nav.get("link_selector")
            click_cont = nav.get("click_container", False)

            target = first_card if (click_cont or not link_sel) else first_card.locator(link_sel).first

            before_url = tab.url

            # Use expect_navigation for hard navigations; fall back to URL polling for SPAs
            try:
                async with tab.expect_navigation(wait_until="domcontentloaded", timeout=12_000):
                    await target.click()
            except Exception:
                # SPA: click may update URL without triggering navigation event
                await target.click()
                await tab.wait_for_timeout(2_000)

            await self._wait_for_js(tab)

            after_url = tab.url
            if after_url != before_url and after_url != listings_url:
                logger.debug("[Phase 2] Clicked to: %s", after_url)
                return after_url

            return None

        except Exception as exc:
            logger.warning("[Phase 2] Click navigation failed: %s", exc)
            return None
        finally:
            await tab.close()

    async def _heuristic_first_link(self, page: Page, base_url: str) -> Optional[str]:
        import re
        from bs4 import BeautifulSoup

        html = await page.content()
        soup = BeautifulSoup(html, "lxml")

        job_hints = ["/job/", "/vacancy/", "/position/", "/careers/", "/jobs/", "/offer/", "/posting/", "/განცხადება/"]
        id_pattern = re.compile(r"/\d{3,}[/?#]?")

        for a in soup.find_all("a", href=True):
            full = urljoin(base_url, a["href"])
            if any(h in full for h in job_hints):
                return full

        for a in soup.find_all("a", href=True):
            if id_pattern.search(a["href"]):
                return urljoin(base_url, a["href"])

        return None

    # ------------------------------------------------------------------ detail fetch + auth

    async def _fetch_detail_html(self, context: BrowserContext, url: str, site: str, max_chars: int = 30_000) -> str:
        """
        Fetch detail page HTML. If a login wall is detected:
          1. Attempt login using stored credentials
          2. Re-fetch the original URL
          3. Raise AuthRequired if no credentials stored
        """
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await self._wait_for_js(page)

            # ── Auth check ────────────────────────────────────────────────
            # if await is_login_wall(page):
            #     logger.warning("[Phase 2] Login wall at %s — attempting auth …", url)
            #     success = await attempt_login(page, site, url)
            #     if not success:
            #         # attempt_login raises AuthRequired if no creds, but if
            #         # wrong creds it returns False — propagate as error
            #         raise RuntimeError(
            #             f"Login failed for {site}. Check credentials with "
            #             f"auth.store_credentials('{site}', 'user', 'pass')"
            #         )
            #     # After login, page is already navigated back to original URL
            #     await self._wait_for_js(page)

            raw = await page.content()
            return clean_html(raw, max_chars=max_chars)

        except (AuthRequired, RuntimeError):
            raise
        except Exception as exc:
            logger.warning("[Phase 2] Failed to load %s: %s", url, exc)
            return ""
        finally:
            await page.close()
