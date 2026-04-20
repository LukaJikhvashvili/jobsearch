"""
ScraperRunner — Playwright execution engine.

Flow:
  1. Apply user filters (URL params first, then DOM interactions)
  2. Paginate through filtered listing pages
  3. Extract title + company from each card
  4. Optionally visit each detail page to enrich the listing
"""

import logging
from typing import AsyncIterator, Optional
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .models import (
    ApplicationMethod,
    AttrType,
    DetailNavType,
    DetailNavigation,
    FieldSelector,
    FilterDimension,
    FilterEntry,
    FilterMechanism,
    FiltersConfig,
    JobListing,
    SiteAdapter,
    UserFilters,
)
from .pagination import get_pagination_strategy

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " "AppleWebKit/537.36 (KHTML, like Gecko) " "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Field extraction (BeautifulSoup)
# ---------------------------------------------------------------------------


def _extract(soup: BeautifulSoup, field: Optional[FieldSelector], base_url: str = "") -> Optional[str]:
    if field is None or not field.selector:
        return None
    el = soup.select_one(field.selector)
    if el is None:
        return None
    attr = field.attr
    if attr == AttrType.TEXT:
        return el.get_text(separator=" ", strip=True) or None
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
    def __init__(self, adapter: SiteAdapter, headless: bool = True):
        self.adapter = adapter
        self.headless = headless
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def __aenter__(self) -> "ScraperRunner":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            java_script_enabled=True,
            ignore_https_errors=True,
        )
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

    # ------------------------------------------------------------------ public

    async def run(self, filters: Optional[UserFilters] = None, enrich: bool = True) -> AsyncIterator[JobListing]:
        """
        Full pipeline.  Pass a UserFilters instance to narrow results before
        iterating. Set enrich=False to skip detail-page visits (fast mode).
        """
        async for listing in self.scrape_listings(filters=filters):
            if enrich and listing.url:
                listing = await self.enrich_with_detail(listing)
            yield listing

    # ------------------------------------------------------------------ listings

    async def scrape_listings(self, filters: Optional[UserFilters] = None) -> AsyncIterator[JobListing]:
        adapter = self.adapter
        page: Page = await self._context.new_page()

        # ── Step 1: build the starting URL (URL-param filters applied here) ──
        start_url = self._build_filtered_url(adapter.listings_url, filters)

        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.error("Failed to load listings page %s: %s", start_url, exc)
            await page.close()
            return

        # ── Step 2: apply DOM-based filters ──
        if filters and not filters.is_empty():
            await self._apply_dom_filters(page, adapter.listings.filters, filters)

        # ── Step 3: paginate and yield cards ──
        strategy = get_pagination_strategy(
            adapter.listings.pagination,
            container_selector=adapter.listings.container,
        )
        seen_urls: set[str] = set()

        async for current_page in strategy.pages(page, current_url := page.url):
            html = await current_page.content()
            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(adapter.listings.container)

            if not cards:
                logger.warning("0 cards with selector '%s' on %s", adapter.listings.container, current_page.url)
                continue

            logger.info("%d cards on %s", len(cards), current_page.url)
            fields = adapter.listings.fields
            nav = adapter.listings.navigation

            for i, card in enumerate(cards):
                url = self._url_from_soup(card, nav, adapter.base_url)
                if url is None and nav.type in (DetailNavType.CARD_CLICK, DetailNavType.BUTTON_CLICK):
                    url = await self._click_card_for_url(current_page, adapter.listings.container, i, nav)

                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)

                yield JobListing(
                    site=adapter.site,
                    title=_extract(card, fields.get("title")),
                    company=_extract(card, fields.get("company")),
                    url=url,
                    adapter_version=adapter.version,
                )

        await page.close()

    # ================================================================== FILTERING

    def _build_filtered_url(self, base_listings_url: str, filters: Optional[UserFilters]) -> str:
        """
        Inject all URL_PARAM filters into the listings URL.
        DOM-based filters are handled separately after page load.
        """
        if not filters or filters.is_empty():
            return base_listings_url

        available = self.adapter.listings.filters.available
        url_filters = [f for f in available if f.mechanism == FilterMechanism.URL_PARAM]
        if not url_filters:
            return base_listings_url

        parsed = urlparse(base_listings_url)
        qs = parse_qs(parsed.query, keep_blank_values=True)

        for entry in url_filters:
            value = self._user_value_for(filters, entry.dimension)
            if value is None or not entry.param_name:
                continue

            if value:
                qs[entry.param_name] = [str(value)]
                logger.debug("URL filter: %s=%s", entry.param_name, value)

        new_query = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(parsed._replace(query=new_query))

    async def _apply_dom_filters(self, page: Page, filters_config: FiltersConfig, user_filters: UserFilters) -> None:
        """
        Apply all DOM-based filter interactions in order, then click
        the submit button if one is configured.
        """
        dom_mechanisms = {
            FilterMechanism.SEARCH_FIELD,
            FilterMechanism.DROPDOWN,
            FilterMechanism.CHECKBOX_GROUP,
            FilterMechanism.TAG_FILTER,
            FilterMechanism.RADIO_GROUP,
            FilterMechanism.DATE_RANGE,
        }
        applied_any = False

        for entry in filters_config.available:
            if entry.mechanism not in dom_mechanisms:
                continue

            value = self._user_value_for(user_filters, entry.dimension)
            if value is None:
                continue

            try:
                success = await self._apply_single_filter(page, entry, str(value))
                if success:
                    applied_any = True
                    logger.info("Filter applied: %s=%s (%s)", entry.dimension, value, entry.mechanism)
            except Exception as exc:
                logger.warning("Filter failed: %s=%s — %s", entry.dimension, value, exc)

        # Click submit button if any DOM filter was applied and a button exists
        if applied_any and filters_config.submit_selector:
            try:
                btn = page.locator(filters_config.submit_selector).first
                if await btn.is_visible(timeout=3_000):
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                    logger.info("Clicked submit: %s", filters_config.submit_selector)
            except Exception as exc:
                logger.warning("Submit click failed: %s", exc)

    async def _apply_single_filter(self, page: Page, entry: FilterEntry, value: str) -> bool:
        """Dispatch to the right interaction for each filter mechanism."""

        # ── Search field ────────────────────────────────────────────────────
        if entry.mechanism == FilterMechanism.SEARCH_FIELD:
            if not entry.selector:
                return False
            inp = page.locator(entry.selector).first
            await inp.wait_for(state="visible", timeout=5_000)
            await inp.triple_click()  # select all existing text
            await inp.fill(value)
            # Try pressing Enter to auto-submit; the submit button handles it otherwise
            await inp.press("Enter")
            await page.wait_for_timeout(800)
            return True

        # ── Dropdown ────────────────────────────────────────────────────────
        elif entry.mechanism == FilterMechanism.DROPDOWN:
            if not entry.selector:
                return False
            sel = page.locator(entry.selector).first
            await sel.wait_for(state="visible", timeout=5_000)
            # Try exact match first, then partial text match via JS
            try:
                await sel.select_option(label=value)
                return True
            except Exception:
                pass
            # Fallback: find option whose text contains the value (case-insensitive)
            matched = await page.evaluate(
                """([sel, val]) => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    const opt = Array.from(el.options).find(
                        o => o.text.toLowerCase().includes(val.toLowerCase())
                    );
                    if (opt) { el.value = opt.value; el.dispatchEvent(new Event('change')); return opt.value; }
                    return null;
                }""",
                [entry.selector, value],
            )
            return matched is not None

        # ── Tag filter (pill buttons) ───────────────────────────────────────
        elif entry.mechanism == FilterMechanism.TAG_FILTER:
            item_sel = entry.item_selector or entry.selector
            if not item_sel:
                return False
            items = page.locator(item_sel)
            count = await items.count()
            for i in range(count):
                item = items.nth(i)
                text = (await item.inner_text()).strip()
                if value.lower() in text.lower():
                    await item.click()
                    await page.wait_for_timeout(400)
                    return True
            return False

        # ── Checkbox group ──────────────────────────────────────────────────
        elif entry.mechanism == FilterMechanism.CHECKBOX_GROUP:
            item_sel = entry.item_selector
            if not item_sel:
                return False
            # Checkboxes: find the one whose associated label contains value
            matched = await page.evaluate(
                """([sel, val]) => {
                    const inputs = document.querySelectorAll(sel);
                    for (const inp of inputs) {
                        const label = inp.labels?.[0]?.textContent ||
                                      inp.closest('label')?.textContent ||
                                      inp.nextElementSibling?.textContent || '';
                        if (label.toLowerCase().includes(val.toLowerCase())) {
                            if (!inp.checked) inp.click();
                            return label.trim();
                        }
                    }
                    return null;
                }""",
                [item_sel, value],
            )
            return matched is not None

        # ── Radio group ─────────────────────────────────────────────────────
        elif entry.mechanism == FilterMechanism.RADIO_GROUP:
            item_sel = entry.item_selector
            if not item_sel:
                return False
            matched = await page.evaluate(
                """([sel, val]) => {
                    const radios = document.querySelectorAll(sel);
                    for (const r of radios) {
                        const label = r.labels?.[0]?.textContent ||
                                      r.closest('label')?.textContent ||
                                      r.nextElementSibling?.textContent || '';
                        if (label.toLowerCase().includes(val.toLowerCase())) {
                            r.click(); return label.trim();
                        }
                    }
                    return null;
                }""",
                [item_sel, value],
            )
            return matched is not None

        # ── Date range ──────────────────────────────────────────────────────
        elif entry.mechanism == FilterMechanism.DATE_RANGE:
            # value is expected as "YYYY-MM-DD" or a human string; we set only
            # the from-date (meaning "posted since") and leave to-date as today.
            from_sel = entry.date_from_selector
            if not from_sel:
                return False
            try:
                inp = page.locator(from_sel).first
                await inp.wait_for(state="visible", timeout=5_000)
                await inp.fill(value)
                return True
            except Exception:
                return False

        return False

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _user_value_for(user_filters: UserFilters, dimension: FilterDimension) -> Optional[str]:
        """Return the user's value for a given dimension, as a string."""
        mapping = {
            FilterDimension.KEYWORD: user_filters.keyword,
            FilterDimension.LOCATION: user_filters.location,
            FilterDimension.CATEGORY: user_filters.category,
            FilterDimension.SALARY: str(user_filters.salary_min) if user_filters.salary_min else None,
            FilterDimension.DATE_POSTED: user_filters.date_posted,
        }
        return mapping.get(dimension)


    @staticmethod
    def _url_from_soup(card: BeautifulSoup, nav: DetailNavigation, base_url: str) -> Optional[str]:
        if nav.type == DetailNavType.DIRECT_LINK:
            el = card.select_one(nav.link_selector) if nav.link_selector else card.find("a", href=True)
            if el:
                href = el.get("href", "")
                return urljoin(base_url, href) if href else None

        elif nav.type == DetailNavType.DATA_ATTR:
            attr = nav.data_attribute or "data-href"
            val = card.get(attr) or (card.select_one(f"[{attr}]") or {}).get(attr)
            return urljoin(base_url, val) if val else None

        elif nav.type == DetailNavType.BUTTON_CLICK:
            if nav.link_selector:
                el = card.select_one(nav.link_selector)
                if el:
                    href = el.get("href", "")
                    if href:
                        return urljoin(base_url, href)

        elif nav.type == DetailNavType.CARD_CLICK:
            href = card.get("href", "")
            if href:
                return urljoin(base_url, href)
            a = card.find("a", href=True)
            if a:
                return urljoin(base_url, a["href"])

        return None

    async def _click_card_for_url(
        self, listings_page: Page, container_selector: str, card_index: int, nav: DetailNavigation
    ) -> Optional[str]:
        tab: Page = await self._context.new_page()
        try:
            await tab.goto(listings_page.url, wait_until="domcontentloaded", timeout=30_000)
            await tab.wait_for_timeout(800)
            cards = tab.locator(container_selector)
            if await cards.count() <= card_index:
                return None
            card_loc = cards.nth(card_index)
            target = (
                card_loc if (nav.click_container or not nav.link_selector) else card_loc.locator(nav.link_selector).first
            )
            async with tab.expect_navigation(timeout=15_000):
                await target.click()
            return tab.url
        except Exception as exc:
            logger.warning("click_card_for_url failed at index %d: %s", card_index, exc)
            return None
        finally:
            await tab.close()

    # ================================================================== DETAIL ENRICHMENT

    async def enrich_with_detail(self, listing: JobListing) -> JobListing:
        if not listing.url:
            return listing
        page: Page = await self._context.new_page()
        try:
            await page.goto(listing.url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(800)
            html = await page.content()
            listing.raw_html = html
            soup = BeautifulSoup(html, "lxml")
            d = self.adapter.detail.fields

            listing.location = _extract(soup, d.get("location"))
            listing.salary = _extract(soup, d.get("salary"))
            listing.posted_date = _extract(soup, d.get("posted_date"))
            listing.description = _extract(soup, d.get("description"))
            listing.requirements = _extract(soup, d.get("requirements"))

            listing = self._detect_application(listing, soup, html)
        except Exception as exc:
            logger.error("Detail enrichment failed for %s: %s", listing.url, exc)
        finally:
            await page.close()
        return listing

    def _detect_application(self, listing, soup, html) -> JobListing:
        app = self.adapter.detail.application
        method = app.method
        if app.confidence < 0.70:
            method = self._heuristic_application_method(soup, listing.url or "")
        listing.application_method = method
        if method == ApplicationMethod.EMAIL:
            listing.application_email = self._find_email(soup, app.email_selector)
        elif method in (ApplicationMethod.ATS_REDIRECT, ApplicationMethod.EXTERNAL_LINK):
            listing.application_url = self._find_apply_url(
                soup, app.external_url_selector or app.apply_button_selector, listing.url or ""
            )
        elif method == ApplicationMethod.ON_PAGE_FORM:
            listing.application_url = listing.url
        return listing

    def _heuristic_application_method(self, soup, page_url) -> ApplicationMethod:
        from .schema_generator import ATS_DOMAINS

        base_domain = urlparse(page_url).netloc.lower()
        for a in soup.select("a[href^='mailto:']"):
            if a["href"].replace("mailto:", "").strip():
                return ApplicationMethod.EMAIL
        for form in soup.find_all("form"):
            combined = form.get_text(" ") + " ".join(
                i.get("name", "") + i.get("placeholder", "") for i in form.find_all(["input", "textarea"])
            )
            if any(k in combined.lower() for k in ("resume", "cv", "cover", "apply", "upload")):
                return ApplicationMethod.ON_PAGE_FORM
        for a in soup.find_all("a", href=True):
            if any(ats in a["href"].lower() for ats in ATS_DOMAINS):
                return ApplicationMethod.ATS_REDIRECT
        for a in soup.find_all("a", href=True):
            if any(k in a.get_text().lower() for k in ("apply", "send cv", "submit")):
                href = a["href"]
                if href.startswith("http") and urlparse(href).netloc.lower() != base_domain:
                    return ApplicationMethod.EXTERNAL_LINK
        return ApplicationMethod.UNKNOWN

    @staticmethod
    def _find_email(soup, hint_selector):
        if hint_selector:
            el = soup.select_one(hint_selector)
            if el:
                return el.get("href", "").replace("mailto:", "").split("?")[0].strip() or el.get_text(strip=True)
        el = soup.select_one("a[href^='mailto:']")
        return el["href"].replace("mailto:", "").split("?")[0].strip() if el else None

    @staticmethod
    def _find_apply_url(soup, hint_selector, page_url):
        if hint_selector:
            el = soup.select_one(hint_selector)
            if el:
                return urljoin(page_url, el.get("href", "")) or None
        for a in soup.find_all("a", href=True):
            if "apply" in a.get_text().lower() or "apply" in a["href"].lower():
                return urljoin(page_url, a["href"])
        return None
