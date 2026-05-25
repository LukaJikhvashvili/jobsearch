"""
ScraperRunner — Playwright execution engine.

Phase 1 only: scrapes listings pages and yields JobListings with
title, company, and URL populated. Detail enrichment is deferred to Phase 2.

Flow:
  1. Build filtered start URL (URL-param filters injected directly)
  2. Load page, apply any DOM-based filters, wait for results to settle
  3. Paginate using the strategy from the adapter
  4. For each card: extract title + company, resolve detail URL
"""

import logging
import time
from typing import AsyncIterator, Optional
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

from .config import PlaywrightConfig
from .extractors import ExtractorPipeline, _extract_field
from .telemetry import TelemetryCollector

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .models import (
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
from .filter_match import (
    best_match_score,
    get_best_translated_input,
    THRESHOLD,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ScraperRunner:
    def __init__(
        self,
        adapter: SiteAdapter,
        headless: bool = True,
        playwright_config: Optional[PlaywrightConfig] = None,
        event_bus=None,
        extractor_pipeline: Optional[ExtractorPipeline] = None,
        telemetry: Optional[TelemetryCollector] = None,
    ):
        self.adapter = adapter
        if playwright_config is not None:
            self._pw_config = playwright_config
        else:
            self._pw_config = PlaywrightConfig(headless=headless)
        self.headless = self._pw_config.headless
        self.event_bus = event_bus
        self.extractor_pipeline = extractor_pipeline if extractor_pipeline is not None else ExtractorPipeline()
        self.telemetry = telemetry
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def __aenter__(self) -> "ScraperRunner":
        cfg = self._pw_config
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=cfg.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        # Use the first page language as the browser locale.
        # Helps avoid bot-detection on non-English sites (e.g. Georgian sites
        # expect ka-GE locale in headers).
        lang = self.adapter.page_languages[0] if self.adapter.page_languages else "en"
        locale_map = {
            "ka": "ka-GE",
            "ru": "ru-RU",
            "de": "de-DE",
            "fr": "fr-FR",
            "tr": "tr-TR",
            "az": "az-AZ",
        }
        locale = locale_map.get(lang, f"{lang}-{lang.upper()}")

        self._context = await self._browser.new_context(
            user_agent=cfg.user_agent,
            viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
            locale=locale,
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        # Block images/fonts — speeds up scraping significantly
        block_pattern = ",".join(cfg.block_resources)
        await self._context.route(
            f"**/*.{{{block_pattern}}}",
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

    async def run(self, filters: Optional[UserFilters] = None) -> AsyncIterator[JobListing]:
        """
        Yield JobListings with title, company, and URL populated.
        Pass UserFilters to narrow results before iterating.
        """
        async for listing in self.scrape_listings(filters=filters):
            yield listing

    # ------------------------------------------------------------------ listings

    async def scrape_listings(self, filters: Optional[UserFilters] = None) -> AsyncIterator[JobListing]:
        adapter = self.adapter
        page_languages = list(adapter.page_languages)

        if self.event_bus:
            await self.event_bus.publish("scraping_started", site=adapter.site)

        # Step 1 — build filtered start URL (URL params injected)
        start_url = self._build_filtered_url(adapter.listings_url, filters, page_languages)

        page: Page = await self._context.new_page()
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=self._pw_config.navigation_timeout_ms)
        except Exception as exc:
            logger.error("Failed to load %s: %s", start_url, exc)
            await page.close()
            return

        # Step 2 — apply DOM-based filters
        if filters and not filters.is_empty():
            await self._apply_dom_filters(page, adapter.listings.filters, filters, page_languages)

        # Step 3 — paginate
        # Pass `page.url` as start_url to pagination so any redirect or
        # DOM-filter URL change is reflected in the paginator's base URL.
        strategy = get_pagination_strategy(
            adapter.listings.pagination,
            container_selector=adapter.listings.container,
        )

        # Secondary dedup: catches duplicates from next_button / infinite_scroll
        # that don't go through the UrlParamPagination card-ID tracker.
        seen_urls: set[str] = set()

        total_jobs = 0
        total_pages = 0
        scrape_start = time.time()

        async for current_page in strategy.pages(page, page.url):
            total_pages += 1
            html = await current_page.content()
            soup = BeautifulSoup(html, "lxml")
            cards = soup.select(adapter.listings.container)

            if not cards:
                logger.warning(
                    "0 cards with selector '%s' on %s",
                    adapter.listings.container,
                    current_page.url,
                )
                continue

            logger.info("%d cards on %s", len(cards), current_page.url)

            if self.telemetry:
                self.telemetry.track_page_scraped(adapter.site, total_pages, len(cards))

            nav = adapter.listings.navigation

            for i, card in enumerate(cards):
                # Resolve detail URL from static HTML first (fast)
                url = self._url_from_soup(card, nav, adapter.base_url)

                # For JS-driven navigation, fall back to Playwright click
                if url is None and nav.type in (DetailNavType.CARD_CLICK, DetailNavType.BUTTON_CLICK):
                    url = await self._click_card_for_url(current_page, adapter.listings.container, i, nav)

                # Secondary dedup (complements pagination-level dedup)
                if url:
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                extracted = self.extractor_pipeline.extract_all(card, adapter, adapter.base_url)
                total_jobs += 1
                yield JobListing(
                    site=adapter.site,
                    url=url,
                    adapter_version=adapter.version,
                    **{k: v for k, v in extracted.items() if k in JobListing.model_fields},
                )

            if self.event_bus:
                await self.event_bus.publish("scraping_page_completed", site=adapter.site, url=current_page.url)

        if self.telemetry:
            self.telemetry.track_scraping_complete(
                adapter.site, total_jobs, total_pages, time.time() - scrape_start
            )

        if self.event_bus:
            await self.event_bus.publish("scraping_completed", site=adapter.site)

        await page.close()

    # ================================================================== FILTERING

    def _build_filtered_url(self, base_url: str, filters: Optional[UserFilters], page_languages: list[str]) -> str:
        """
        Inject URL_PARAM filters into the starting URL.
        Values are translated to the page's primary language when needed.
        """
        if not filters or filters.is_empty():
            return base_url

        url_filters = [f for f in self.adapter.listings.filters.available if f.mechanism == FilterMechanism.URL_PARAM]
        if not url_filters:
            return base_url

        parsed = urlparse(base_url)
        qs = parse_qs(parsed.query, keep_blank_values=True)

        for entry in url_filters:
            value = self._user_value_for(filters, entry.dimension)
            if value is None or not entry.param_name:
                continue
            # Translate value to the page's primary non-English language for URL params
            translated = get_best_translated_input(str(value), page_languages)
            qs[entry.param_name] = [translated]
            logger.debug("URL filter: %s=%s", entry.param_name, translated)

        new_query = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(parsed._replace(query=new_query))

    async def _apply_dom_filters(
        self, page: Page, filters_config: FiltersConfig, user_filters: UserFilters, page_languages: list[str]
    ) -> None:
        """
        Apply DOM-based filters in order, submit if needed, then wait for
        result confirmation.
        """
        dom_mechs = {
            FilterMechanism.SEARCH_FIELD,
            FilterMechanism.DROPDOWN,
            FilterMechanism.CHECKBOX_GROUP,
            FilterMechanism.TAG_FILTER,
            FilterMechanism.RADIO_GROUP,
            FilterMechanism.DATE_RANGE,
        }
        applied_any = False

        for entry in filters_config.available:
            if entry.mechanism not in dom_mechs:
                continue
            value = self._user_value_for(user_filters, entry.dimension)
            if value is None:
                continue
            try:
                ok = await self._apply_single_filter(page, entry, str(value), page_languages)
                if ok:
                    applied_any = True
                    logger.info("Filter applied: %s=%s (%s)", entry.dimension, value, entry.mechanism)
                    if self.event_bus:
                        await self.event_bus.publish("filter_applied", dimension=entry.dimension, value=value)
                    if self.telemetry:
                        self.telemetry.track_filter_applied(
                            self.adapter.site, entry.dimension.value, entry.mechanism.value, success=True
                        )
                else:
                    logger.warning("Filter not applied (no match): %s=%s", entry.dimension, value)
                    if self.telemetry:
                        self.telemetry.track_filter_applied(
                            self.adapter.site, entry.dimension.value, entry.mechanism.value, success=False
                        )
            except Exception as exc:
                logger.warning("Filter error: %s=%s — %s", entry.dimension, value, exc)
                if self.event_bus:
                    await self.event_bus.publish("filter_failed", dimension=entry.dimension, error=str(exc))
                if self.telemetry:
                    self.telemetry.track_filter_applied(
                        self.adapter.site, entry.dimension.value, entry.mechanism.value, success=False
                    )

        if not applied_any:
            return

        # Submit button
        if filters_config.submit_selector:
            try:
                btn = page.locator(filters_config.submit_selector).first
                if await btn.is_visible(timeout=3_000):
                    await btn.click()
                    logger.info("Clicked submit: %s", filters_config.submit_selector)
            except Exception as exc:
                logger.warning("Submit click failed: %s", exc)

        # Wait for results to reflect filters
        await self._wait_for_filter_confirmation(page)

    async def _apply_single_filter(self, page: Page, entry: FilterEntry, value: str, page_languages: list[str]) -> bool:

        # ── Search field ────────────────────────────────────────────────────
        if entry.mechanism == FilterMechanism.SEARCH_FIELD:
            if not entry.selector:
                return False
            # Type in the page's language
            input_value = get_best_translated_input(value, page_languages)
            inp = page.locator(entry.selector).first
            await inp.wait_for(state="visible", timeout=5_000)
            await inp.fill(input_value)
            await inp.press("Enter")
            await page.wait_for_timeout(800)
            return True

        # ── Dropdown ────────────────────────────────────────────────────────
        elif entry.mechanism == FilterMechanism.DROPDOWN:
            if not entry.selector:
                return False
            sel = page.locator(entry.selector).first
            await sel.wait_for(state="visible", timeout=5_000)

            # Check if it's a standard <select> element
            is_select = await page.evaluate(
                "([sel]) => document.querySelector(sel)?.tagName === 'SELECT'",
                [entry.selector],
            )

            if is_select:
                # Try exact label match first
                try:
                    await sel.select_option(label=value)
                    return True
                except Exception:
                    pass

                # Fuzzy match across all <select> options
                options_info = await page.evaluate(
                    """([sel]) => {
                        const el = document.querySelector(sel);
                        if (!el || !el.options) return [];
                        return Array.from(el.options).map((o, i) => (
                            {i, text: o.text || o.innerText || '', value: o.value}
                        ));
                    }""",
                    [entry.selector],
                )
                best_score, best_val = 0, None
                for opt in options_info:
                    score = best_match_score(value, opt["text"], page_languages)
                    if score > best_score:
                        best_score, best_val = score, opt["value"]
                if best_val is not None and best_score >= THRESHOLD:
                    await page.evaluate(
                        """([sel, val]) => {
                            const el = document.querySelector(sel);
                            if (el) { el.value = val; el.dispatchEvent(new Event('change')); }
                        }""",
                        [entry.selector, best_val],
                    )
                    return True

            # Universal custom dropdown fallback
            return await self._apply_custom_dropdown(page, entry, value, page_languages)

        # ── Tag filter ──────────────────────────────────────────────────────
        elif entry.mechanism == FilterMechanism.TAG_FILTER:
            item_sel = entry.item_selector or entry.selector
            if not item_sel:
                return False
            items = page.locator(item_sel)
            count = await items.count()
            best_score, best_idx = 0, -1
            for i in range(count):
                text = (await items.nth(i).inner_text()).strip()
                score = best_match_score(value, text, page_languages)
                if score > best_score:
                    best_score, best_idx = score, i
            if best_idx >= 0 and best_score >= THRESHOLD:
                await items.nth(best_idx).click()
                await page.wait_for_timeout(400)
                return True
            return False

        # ── Checkbox group ──────────────────────────────────────────────────
        elif entry.mechanism == FilterMechanism.CHECKBOX_GROUP:
            item_sel = entry.item_selector
            if not item_sel:
                return False
            items_info = await page.evaluate(
                """([sel]) => {
                    const inputs = document.querySelectorAll(sel);
                    return Array.from(inputs).map((inp, i) => ({
                        i,
                        label: (inp.labels?.[0]?.textContent ||
                                inp.closest('label')?.textContent ||
                                inp.nextElementSibling?.textContent || '').trim(),
                        checked: inp.checked
                    }));
                }""",
                [item_sel],
            )
            best_score, best_i = 0, -1
            for info in items_info:
                score = best_match_score(value, info["label"], page_languages)
                if score > best_score:
                    best_score, best_i = score, info["i"]
            if best_i >= 0 and best_score >= THRESHOLD:
                await page.evaluate(
                    """([sel, idx]) => {
                        const inp = document.querySelectorAll(sel)[idx];
                        if (inp && !inp.checked) inp.click();
                        return inp ? true : false;
                    }""",
                    [item_sel, best_i],
                )
                return True
            return False

        # ── Radio group ─────────────────────────────────────────────────────
        elif entry.mechanism == FilterMechanism.RADIO_GROUP:
            item_sel = entry.item_selector
            if not item_sel:
                return False
            items_info = await page.evaluate(
                """([sel]) => {
                    const radios = document.querySelectorAll(sel);
                    return Array.from(radios).map((r, i) => ({
                        i,
                        label: (r.labels?.[0]?.textContent ||
                                r.closest('label')?.textContent ||
                                r.nextElementSibling?.textContent || '').trim()
                    }));
                }""",
                [item_sel],
            )
            best_score, best_i = 0, -1
            for info in items_info:
                score = best_match_score(value, info["label"], page_languages)
                if score > best_score:
                    best_score, best_i = score, info["i"]
            if best_i >= 0 and best_score >= THRESHOLD:
                await page.evaluate(
                    """([sel, idx]) => {
                        const r = document.querySelectorAll(sel)[idx];
                        if (r) r.click();
                        return r ? true : false;
                    }""",
                    [item_sel, best_i],
                )
                return True
            return False

        # ── Date range ──────────────────────────────────────────────────────
        elif entry.mechanism == FilterMechanism.DATE_RANGE:
            from_sel = entry.date_from_selector
            if not from_sel:
                return False
            inp = page.locator(from_sel).first
            await inp.wait_for(state="visible", timeout=5_000)
            await inp.fill(value)
            return True

        return False

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _user_value_for(user_filters: UserFilters, dimension: FilterDimension) -> Optional[str]:
        mapping = {
            FilterDimension.KEYWORD: user_filters.keyword,
            FilterDimension.LOCATION: user_filters.location,
            FilterDimension.CATEGORY: user_filters.category,
            FilterDimension.SALARY: str(user_filters.salary_min) if user_filters.salary_min else None,
            FilterDimension.DATE_POSTED: user_filters.date_posted,
        }
        return mapping.get(dimension)

    async def _count_cards(self, page: Page) -> int:
        sel = self.adapter.listings.container
        try:
            return await page.locator(sel).count()
        except Exception:
            return 0

    async def _wait_for_filter_confirmation(self, page: Page, max_wait_ms: int = 8_000, poll_ms: int = 300) -> bool:
        """
        Poll until card count changes from the pre-filter value, or networkidle
        fires. Returns True when a change is confirmed.
        """
        before = await self._count_cards(page)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
            after = await self._count_cards(page)
            if page.url != page.url or after != before:  # URL change or count change
                return True
        except Exception:
            pass
        elapsed = 0
        while elapsed < max_wait_ms:
            await page.wait_for_timeout(poll_ms)
            elapsed += poll_ms
            if await self._count_cards(page) != before:
                return True
        return False

    # ================================================================== CUSTOM DROPDOWN

    async def _install_mutation_observer(self, page: Page) -> None:
        """Install a MutationObserver on document.body that records added Element nodes."""
        await page.evaluate("""() => {
            window.__ddAdded = [];
            window.__ddPanels = [];
            window.__ddObserver = new MutationObserver((mutations) => {
                for (const m of mutations) {
                    for (const node of m.addedNodes) {
                        if (node.nodeType === 1) window.__ddAdded.push(node);
                    }
                }
            });
            window.__ddObserver.observe(document.body, { childList: true, subtree: true });
        }""")

    async def _harvest_dropdown_items(self, page: Page) -> list:
        """Disconnect observer and extract visible text items from added panels."""
        return await page.evaluate("""() => {
            if (window.__ddObserver) window.__ddObserver.disconnect();
            const panels = window.__ddAdded || [];
            window.__ddPanels = panels;
            const results = [];
            for (let pi = 0; pi < panels.length; pi++) {
                const panel = panels[pi];
                if (!panel.querySelectorAll) continue;
                let items = panel.querySelectorAll(
                    'li, [role="option"], [role="treeitem"], [role="menuitem"]'
                );
                if (items.length === 0) {
                    items = panel.querySelectorAll('*');
                    items = Array.from(items).filter(
                        el => el.children.length === 0 && el.textContent.trim()
                    );
                }
                for (let ei = 0; ei < items.length; ei++) {
                    const text = (items[ei].textContent || '').trim();
                    if (text) results.push({ text, panelIdx: pi, elIdx: ei });
                }
            }
            return results;
        }""")

    async def _harvest_visible_items_in_panel(self, page: Page) -> list:
        """Scan all currently visible items in stored panels (for post-search picking)."""
        return await page.evaluate("""() => {
            const panels = window.__ddPanels || [];
            const results = [];
            for (let pi = 0; pi < panels.length; pi++) {
                const panel = panels[pi];
                if (!panel.querySelectorAll) continue;
                let items = panel.querySelectorAll(
                    'li, [role="option"], [role="treeitem"], [role="menuitem"]'
                );
                if (items.length === 0) {
                    items = panel.querySelectorAll('*');
                    items = Array.from(items).filter(
                        el => el.children.length === 0 && el.textContent.trim()
                    );
                }
                for (let ei = 0; ei < items.length; ei++) {
                    const el = items[ei];
                    if (!el.offsetParent && el.style?.display === 'none') continue;
                    const text = (el.textContent || '').trim();
                    if (text) results.push({ text, panelIdx: pi, elIdx: ei });
                }
            }
            return results;
        }""")

    def _fuzzy_pick(self, items: list, value: str, page_languages: list) -> dict | None:
        """Run fuzzy matching against harvested items, return best match above THRESHOLD."""
        best_score, best_match = 0.0, None
        for item in items:
            score = best_match_score(value, item["text"], page_languages)
            if score > best_score:
                best_score = score
                best_match = item
        if best_match and best_score >= THRESHOLD:
            return best_match
        return None

    async def _click_harvested_item(self, page: Page, match: dict) -> bool:
        """Click an item by locating it in the stored panels via text content match."""
        clicked = await page.evaluate("""([panelIdx, elIdx, text]) => {
            const panels = window.__ddPanels || [];
            if (panelIdx >= panels.length) return false;
            const panel = panels[panelIdx];
            if (!panel.querySelectorAll) return false;
            let items = panel.querySelectorAll(
                'li, [role="option"], [role="treeitem"], [role="menuitem"]'
            );
            if (items.length === 0) {
                items = Array.from(panel.querySelectorAll('*')).filter(
                    el => el.children.length === 0 && el.textContent.trim()
                );
            }
            if (elIdx < items.length) {
                items[elIdx].click();
                return true;
            }
            // Fallback: find by text
            for (const el of items) {
                if ((el.textContent || '').trim() === text) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""", [match["panelIdx"], match["elIdx"], match["text"]])
        return bool(clicked)

    async def _try_panel_search(
        self, page: Page, entry: FilterEntry, value: str, page_languages: list
    ) -> bool:
        """Try to use the search input inside the dropdown panel to filter options."""
        search_selector = entry.panel_search_selector
        if not search_selector:
            # Auto-detect search input in added panels
            search_selector = await page.evaluate("""() => {
                const panels = window.__ddAdded || [];
                const selectors = [
                    'input[type="search"]', 'input[type="text"]',
                    'input[placeholder*="search" i]', 'input[role="searchbox"]',
                    '.p-treeselect-filter', '.p-select-filter',
                    'input[placeholder*="filter" i]'
                ];
                for (const panel of panels) {
                    if (!panel.querySelector) continue;
                    for (const sel of selectors) {
                        const inp = panel.querySelector(sel);
                        if (inp) {
                            // Build a usable selector
                            if (inp.id) return '#' + inp.id;
                            if (inp.className && typeof inp.className === 'string') {
                                const cls = inp.className.trim().split(/\\s+/);
                                if (cls.length) return inp.tagName.toLowerCase() + '.' + cls.join('.');
                            }
                            return sel;
                        }
                    }
                }
                return null;
            }""")

        if not search_selector:
            return False

        try:
            inp = page.locator(search_selector).first
            if not await inp.is_visible(timeout=2_000):
                return False
            input_value = get_best_translated_input(value, page_languages)
            await inp.fill(input_value)
            await page.wait_for_timeout(600)

            items = await self._harvest_visible_items_in_panel(page)
            match = self._fuzzy_pick(items, value, page_languages)
            if match:
                return await self._click_harvested_item(page, match)
        except Exception as exc:
            logger.debug("Panel search failed: %s", exc)

        return False

    async def _expand_and_search_tree(
        self, page: Page, value: str, page_languages: list,
        current_items: list, depth: int, max_depth: int,
    ) -> bool:
        """Recursively expand tree nodes and search for the target value."""
        if depth >= max_depth:
            await page.keyboard.press("Escape")
            return False

        # Find expandable elements in currently visible panels
        expandables = await page.evaluate("""() => {
            const panels = window.__ddPanels || [];
            const results = [];
            const selectors = [
                '[aria-expanded="false"]', '.p-treenode-toggle',
                '[class*="expand"]', '[class*="toggle"]', '[role="treeitem"]'
            ];
            for (let pi = 0; pi < panels.length; pi++) {
                const panel = panels[pi];
                if (!panel.querySelectorAll) continue;
                const seen = new Set();
                for (const sel of selectors) {
                    for (const el of panel.querySelectorAll(sel)) {
                        if (seen.has(el)) continue;
                        seen.add(el);
                        const text = (el.textContent || '').trim().substring(0, 100);
                        results.push({ panelIdx: pi, text, selector: sel });
                    }
                }
            }
            return results;
        }""")

        if not expandables:
            return False

        # Score expandables by partial match to prioritize relevant branches
        scored = []
        for exp in expandables:
            score = best_match_score(value, exp["text"], page_languages)
            scored.append((score, exp))
        scored.sort(key=lambda x: -x[0])

        for _score, exp in scored:
            # Install fresh observer before expanding
            await self._install_mutation_observer(page)

            # Click the expandable element
            clicked = await page.evaluate("""([panelIdx, selector, text]) => {
                const panels = window.__ddPanels || [];
                if (panelIdx >= panels.length) return false;
                const panel = panels[panelIdx];
                const candidates = panel.querySelectorAll(selector);
                for (const el of candidates) {
                    if ((el.textContent || '').trim().substring(0, 100) === text) {
                        // Click toggle button if present, otherwise click the element
                        const toggle = el.querySelector(
                            '.p-treenode-toggle, [class*="toggle"], button'
                        );
                        (toggle || el).click();
                        return true;
                    }
                }
                return false;
            }""", [exp["panelIdx"], exp["selector"], exp["text"]])

            if not clicked:
                continue

            await page.wait_for_timeout(400)

            # Harvest newly revealed children
            children = await self._harvest_dropdown_items(page)
            if not children:
                continue

            # Check for a direct match among children
            match = self._fuzzy_pick(children, value, page_languages)
            if match:
                return await self._click_harvested_item(page, match)

            # Recurse deeper
            found = await self._expand_and_search_tree(
                page, value, page_languages, children, depth + 1, max_depth
            )
            if found:
                return True

        await page.keyboard.press("Escape")
        return False

    async def _apply_custom_dropdown(
        self, page: Page, entry: FilterEntry, value: str, page_languages: list
    ) -> bool:
        """
        Main orchestrator for custom dropdown interaction.
        Uses MutationObserver to detect panels, supports search and tree expansion.
        """
        max_depth = entry.max_depth or 3

        # 1. Install MutationObserver
        await self._install_mutation_observer(page)

        # 2. Click the trigger element
        try:
            trigger = page.locator(entry.selector).first
            await trigger.click()
        except Exception as exc:
            logger.warning("Custom dropdown trigger click failed: %s", exc)
            return False

        # 3. Wait for panel to appear
        await page.wait_for_timeout(500)

        # 4. Try panel search first (fastest path for searchable dropdowns)
        if await self._try_panel_search(page, entry, value, page_languages):
            return True

        # 5. Harvest items from the panel
        items = await self._harvest_dropdown_items(page)

        # 6. Try direct fuzzy match
        if items:
            match = self._fuzzy_pick(items, value, page_languages)
            if match:
                return await self._click_harvested_item(page, match)

        # 7. Try tree expansion (nested/hierarchical dropdowns)
        if await self._expand_and_search_tree(
            page, value, page_languages, items, 0, max_depth
        ):
            return True

        # 8. Nothing worked — close the dropdown
        await page.keyboard.press("Escape")
        return False

    # ================================================================== NAVIGATION

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
        """
        Open a fresh tab, reload the listings page, click card N, and
        capture the resulting URL. Handles both hard navigations and SPAs.
        """
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
            before_url = tab.url
            try:
                async with tab.expect_navigation(wait_until="domcontentloaded", timeout=12_000):
                    await target.click()
            except Exception:
                await target.click()
                await tab.wait_for_timeout(2_000)
            after_url = tab.url
            if after_url != before_url and after_url != listings_page.url:
                return after_url
            return None
        except Exception as exc:
            logger.warning("_click_card_for_url failed at index %d: %s", card_index, exc)
            return None
        finally:
            await tab.close()
