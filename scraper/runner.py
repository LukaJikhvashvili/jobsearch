"""
ScraperRunner: the Playwright-based execution engine.

Given a SiteAdapter it will:
  1. Navigate through all listing pages (using the appropriate pagination strategy)
  2. Extract job card fields from each page
  3. Optionally visit each job's detail page to enrich the listing
     and detect the application method

Usage:
    async with ScraperRunner(adapter) as runner:
        async for job in runner.run(enrich=True):
            print(job.title, job.application_method)
"""

import logging
from typing import AsyncIterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .models import (
    ApplicationMethod,
    AttrType,
    DetailNavType,
    DetailNavigation,
    FieldSelector,
    JobListing,
    SiteAdapter,
)
from .pagination import get_pagination_strategy

logger = logging.getLogger(__name__)

# Browser user-agent that avoids bot-detection on most sites
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " "AppleWebKit/537.36 (KHTML, like Gecko) " "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Field extraction helper
# ---------------------------------------------------------------------------


def _extract(
    soup: BeautifulSoup,
    field: Optional[FieldSelector],
    base_url: str = "",
) -> Optional[str]:
    """
    Extract a single value from a BeautifulSoup node using a FieldSelector.
    Returns None if the selector is missing or finds nothing.
    """
    if field is None or not field.selector:
        return None

    el = soup.select_one(field.selector)
    if el is None:
        return None

    attr = field.attr
    if attr == AttrType.TEXT:
        text = el.get_text(separator=" ", strip=True)
        return text or None
    elif attr == AttrType.HTML:
        return str(el) or None
    elif attr == AttrType.HREF:
        href = el.get("href", "")
        return urljoin(base_url, href) if href else None
    elif attr == AttrType.SRC:
        src = el.get("src", "")
        return urljoin(base_url, src) if src else None
    elif attr == AttrType.VALUE:
        return el.get("value") or el.get_text(strip=True) or None

    return None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ScraperRunner:
    def __init__(
        self, adapter: SiteAdapter, headless: bool = True, concurrency: int = 1  # detail pages fetched sequentially for now
    ):
        self.adapter = adapter
        self.headless = headless
        self.concurrency = concurrency
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    # ------------------------------------------------------------------ context manager

    async def __aenter__(self) -> "ScraperRunner":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        # Suppress noisy resource types to speed up loading
        await self._context.route(
            "**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,otf}",
            lambda route: route.abort(),
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------ listings scrape

    async def scrape_listings(self) -> AsyncIterator[JobListing]:
        """
        Iterate through all listing pages and yield a partial JobListing
        (listing-page fields only) for each job card found.
        """
        adapter = self.adapter
        page: Page = await self._context.new_page()

        try:
            await page.goto(
                adapter.listings_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as e:
            logger.error("Failed to load listings page %s: %s", adapter.listings_url, e)
            await page.close()
            return

        strategy = get_pagination_strategy(
            adapter.listings.pagination,
            container_selector=adapter.listings.container,
        )

        seen_urls: set[str] = set()

        async for current_page in strategy.pages(page, adapter.listings_url):
            html = await current_page.content()
            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(adapter.listings.container)

            if not cards:
                logger.warning(
                    "0 cards found with selector '%s' on %s",
                    adapter.listings.container,
                    current_page.url,
                )
                continue

            logger.info("%d cards on %s", len(cards), current_page.url)
            fields = adapter.listings.fields

            nav = adapter.listings.navigation

            for i, card in enumerate(cards):
                # ---- Resolve the detail page URL from this card ----
                url = self._url_from_soup(card, nav, adapter.base_url)

                # For JS-only click navigation, we must ask Playwright
                # to click the element and capture where it navigates.
                # We do this lazily (only when soup extraction returned nothing).
                _needs_click = url is None and nav.type in (
                    DetailNavType.CARD_CLICK,
                    DetailNavType.BUTTON_CLICK,
                )
                if _needs_click:
                    url = await self._click_card_for_url(current_page, adapter.listings.container, i, nav)

                # Deduplicate across pagination pages
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)

                yield JobListing(
                    site=adapter.site,
                    title=_extract(card, fields.get("title")),
                    company=_extract(card, fields.get("company")),
                    location=_extract(card, fields.get("location")),
                    salary=_extract(card, fields.get("salary")),
                    url=url,
                    posted_date=_extract(card, fields.get("posted_date")),
                    adapter_version=adapter.version,
                )

        await page.close()

    # ------------------------------------------------------------------ URL resolution from soup

    @staticmethod
    def _url_from_soup(card: BeautifulSoup, nav: DetailNavigation, base_url: str) -> Optional[str]:
        """
        Resolve the detail page URL from a card's static HTML using the
        navigation config. Returns None for click-based types when the
        href is not present in the markup (pure JS navigation).
        """
        if nav.type == DetailNavType.DIRECT_LINK:
            # Explicit link selector
            if nav.link_selector:
                el = card.select_one(nav.link_selector)
                if el:
                    href = el.get("href", "")
                    return urljoin(base_url, href) if href else None
            # Fallback: first <a> in the card
            a = card.find("a", href=True)
            return urljoin(base_url, a["href"]) if a else None

        elif nav.type == DetailNavType.DATA_ATTR:
            attr = nav.data_attribute or "data-href"
            # Check the card element itself first, then children
            val = card.get(attr)
            if not val:
                el = card.select_one(f"[{attr}]")
                val = el.get(attr) if el else None
            return urljoin(base_url, val) if val else None

        elif nav.type == DetailNavType.BUTTON_CLICK:
            # Many "button" CTAs are actually <a> tags — try href first
            if nav.link_selector:
                el = card.select_one(nav.link_selector)
                if el:
                    href = el.get("href", "")
                    if href:
                        return urljoin(base_url, href)
            return None  # genuine button — needs Playwright click

        elif nav.type == DetailNavType.CARD_CLICK:
            # Sometimes card-click sites still put an href on the container
            href = card.get("href", "")
            if href:
                return urljoin(base_url, href)
            # Or an <a> wrapping everything
            a = card.find("a", href=True)
            if a:
                return urljoin(base_url, a["href"])
            return None  # truly JS-only — needs Playwright click

        return None

    # ------------------------------------------------------------------ Playwright click navigation

    async def _click_card_for_url(
        self, listings_page: Page, container_selector: str, card_index: int, nav: DetailNavigation
    ) -> Optional[str]:
        """
        For JS-driven navigation (card_click / button_click), open a new
        browser tab, re-load the listings page, click the nth card (or its
        button), wait for navigation, and return the resulting URL.

        Uses a fresh tab so the main listings page is never disrupted.
        """
        tab: Page = await self._context.new_page()
        try:
            await tab.goto(
                listings_page.url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await tab.wait_for_timeout(800)

            cards_locator = tab.locator(container_selector)
            count = await cards_locator.count()
            if card_index >= count:
                logger.warning("click_card_for_url: card %d not found (only %d cards)", card_index, count)
                return None

            card_loc = cards_locator.nth(card_index)

            if nav.type == DetailNavType.CARD_CLICK or nav.click_container:
                target = card_loc
            elif nav.link_selector:
                target = card_loc.locator(nav.link_selector).first
            else:
                target = card_loc

            # Click and wait for navigation
            async with tab.expect_navigation(timeout=15_000):
                await target.click()

            detail_url = tab.url
            logger.debug("click_card_for_url: card %d → %s", card_index, detail_url)
            return detail_url

        except Exception as exc:
            logger.warning("click_card_for_url failed for card %d: %s", card_index, exc)
            return None
        finally:
            await tab.close()

    # ------------------------------------------------------------------ detail enrichment

    async def enrich_with_detail(self, listing: JobListing) -> JobListing:
        """
        Visit the job's detail page and fill in description, requirements,
        salary (if missing) and — most importantly — application info.
        """
        if not listing.url:
            return listing

        page: Page = await self._context.new_page()
        try:
            await page.goto(listing.url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(800)

            html = await page.content()
            listing.raw_html = html

            soup = BeautifulSoup(html, "lxml")
            d_fields = self.adapter.detail.fields

            listing.description = _extract(soup, d_fields.get("description"))
            listing.requirements = _extract(soup, d_fields.get("requirements"))

            # Only overwrite salary if listing page had none
            if not listing.salary:
                listing.salary = _extract(soup, d_fields.get("salary"))

            # -------------------------------------------------------- application detection
            listing = self._detect_application(listing, soup, html)

        except Exception as exc:
            logger.error("Detail enrichment failed for %s: %s", listing.url, exc)
        finally:
            await page.close()

        return listing

    def _detect_application(self, listing: JobListing, soup: BeautifulSoup, html: str) -> JobListing:
        """
        Determine application method with a layered detection strategy.
        The adapter's AI-generated config is the first layer; heuristics
        fill in the gaps when the adapter config has low confidence or
        the selector is null.
        """
        app = self.adapter.detail.application
        method = app.method
        confidence = app.confidence

        # Re-detect if adapter is uncertain (< 0.70 confidence)
        if confidence < 0.70 or method == ApplicationMethod.UNKNOWN:
            method = self._heuristic_application_method(soup, html, listing.url or "")

        listing.application_method = method

        # Resolve URLs / emails based on method
        if method == ApplicationMethod.EMAIL:
            listing.application_email = self._find_email(soup, app.email_selector)

        elif method in (ApplicationMethod.ATS_REDIRECT, ApplicationMethod.EXTERNAL_LINK):
            listing.application_url = self._find_apply_url(
                soup, app.external_url_selector or app.apply_button_selector, listing.url or ""
            )

        elif method == ApplicationMethod.ON_PAGE_FORM:
            listing.application_url = listing.url  # form is on this page

        return listing

    def _heuristic_application_method(self, soup: BeautifulSoup, html: str, page_url: str) -> ApplicationMethod:
        """
        Multi-signal heuristic that checks in priority order:
          1. mailto: links           → email
          2. on-page apply form      → on_page_form
          3. ATS domain links        → ats_redirect
          4. any external apply link → external_link
          5. fallback                → unknown
        """
        from .schema_generator import ATS_DOMAINS
        from urllib.parse import urlparse

        base_domain = urlparse(page_url).netloc.lower()

        # --- Signal 1: mailto link ---
        for a in soup.select("a[href^='mailto:']"):
            email = a["href"].replace("mailto:", "").split("?")[0].strip()
            if email:
                logger.debug("Heuristic: email detected (%s)", email)
                return ApplicationMethod.EMAIL

        # --- Signal 2: on-page form with apply-like fields ---
        apply_keywords = {"resume", "cv", "cover", "apply", "application", "upload"}
        for form in soup.find_all("form"):
            form_text = form.get_text(" ", strip=True).lower()
            input_names = " ".join(
                (inp.get("name", "") + " " + inp.get("placeholder", "")).lower()
                for inp in form.find_all(["input", "textarea"])
            )
            combined = form_text + " " + input_names
            if any(kw in combined for kw in apply_keywords):
                logger.debug("Heuristic: on-page form detected")
                return ApplicationMethod.ON_PAGE_FORM

        # --- Signal 3: ATS links ---
        for a in soup.find_all("a", href=True):
            href: str = a["href"].lower()
            if any(ats in href for ats in ATS_DOMAINS):
                logger.debug("Heuristic: ATS redirect → %s", href)
                return ApplicationMethod.ATS_REDIRECT

        # --- Signal 4: apply button → external link ---
        apply_btn_re = ["apply", "send cv", "submit", "application"]
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if any(kw in text for kw in apply_btn_re):
                href = a["href"]
                if href.startswith("http"):
                    link_domain = urlparse(href).netloc.lower()
                    if link_domain and link_domain != base_domain:
                        logger.debug("Heuristic: external apply link → %s", href)
                        return ApplicationMethod.EXTERNAL_LINK

        logger.debug("Heuristic: unknown application method for %s", page_url)
        return ApplicationMethod.UNKNOWN

    @staticmethod
    def _find_email(soup: BeautifulSoup, hint_selector: Optional[str]) -> Optional[str]:
        # Try adapter hint first
        if hint_selector:
            el = soup.select_one(hint_selector)
            if el:
                href = el.get("href", "")
                return href.replace("mailto:", "").split("?")[0].strip() or el.get_text(strip=True)

        # Fallback: any mailto link
        el = soup.select_one("a[href^='mailto:']")
        if el:
            return el["href"].replace("mailto:", "").split("?")[0].strip()

        return None

    @staticmethod
    def _find_apply_url(soup: BeautifulSoup, hint_selector: Optional[str], page_url: str) -> Optional[str]:
        if hint_selector:
            el = soup.select_one(hint_selector)
            if el:
                return urljoin(page_url, el.get("href", "")) or None

        # Fallback: first link with 'apply' in text or href
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            href: str = a["href"].lower()
            if "apply" in text or "apply" in href:
                return urljoin(page_url, a["href"])

        return None

    # ------------------------------------------------------------------ full pipeline

    async def run(self, enrich: bool = True) -> AsyncIterator[JobListing]:
        """
        Full pipeline: yield enriched JobListings.
        Set enrich=False to get listing-page data only (faster).
        """
        async for listing in self.scrape_listings():
            if enrich and listing.url:
                listing = await self.enrich_with_detail(listing)
            yield listing
