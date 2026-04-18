"""
Pagination strategies for the Playwright-based scraper runner.

Each strategy is an async generator that yields a Page object once per
"page of results". The runner reads job cards from the yielded page,
then the generator advances to the next page.

Strategies:
  NoPagination         — single page, yield once
  UrlParamPagination   — ?page=N style, increments until empty
  NextButtonPagination — clicks the Next button until it disappears / is disabled
  InfiniteScrollPagination — scrolls to bottom until height stabilises, then yields once
"""

import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

from playwright.async_api import Page

from .models import PaginationConfig, PaginationType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class PaginationStrategy(ABC):
    def __init__(self, config: PaginationConfig, container_selector: str = ""):
        self.config = config
        self.container_selector = container_selector  # used to detect empty pages

    @abstractmethod
    def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]: ...

    async def _count_cards(self, page: Page) -> int:
        if not self.container_selector:
            return 1  # assume non-empty if we have no selector to check
        try:
            return await page.locator(self.container_selector).count()
        except Exception:
            return 0

    async def _wait_settled(self, page: Page) -> None:
        """Wait for DOM to settle after navigation or interaction."""
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            # networkidle can time out on streaming pages — domcontentloaded is enough
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5_000)
            except Exception:
                pass
        await page.wait_for_timeout(self.config.delay_ms)


# ---------------------------------------------------------------------------
# No pagination
# ---------------------------------------------------------------------------


class NoPagination(PaginationStrategy):
    async def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]:
        yield page


# ---------------------------------------------------------------------------
# URL parameter pagination  (?page=2, ?page=3 …)
# ---------------------------------------------------------------------------


class UrlParamPagination(PaginationStrategy):
    """
    Increments a query-string parameter (default: 'page') from start_page
    until a page returns 0 job cards or max_pages is reached.
    Also handles path-segment pagination: /jobs/2/, /jobs/3/
    """

    async def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]:
        parsed = urlparse(start_url)
        param = self.config.param_name or "page"
        consecutive_empty = 0

        for page_num in range(
            self.config.start_page,
            self.config.start_page + self.config.max_pages,
        ):
            url = self._build_url(parsed, param, page_num)
            logger.debug("UrlParam → %s", url)

            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await self._wait_settled(page)

            count = await self._count_cards(page)
            if count == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    logger.info("Two consecutive empty pages — stopping at page %d", page_num)
                    break
                continue
            else:
                consecutive_empty = 0

            yield page

    @staticmethod
    def _build_url(parsed, param: str, page_num: int) -> str:
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs[param] = [str(page_num)]
        new_query = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(parsed._replace(query=new_query))


# ---------------------------------------------------------------------------
# Next-button pagination
# ---------------------------------------------------------------------------


class NextButtonPagination(PaginationStrategy):
    """
    Clicks a 'Next' button or link after processing each page.
    Stops when:
      - the button is not found
      - the button is disabled (disabled attr or aria-disabled=true)
      - the URL has not changed after clicking (loop guard)
      - max_pages reached
    """

    async def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]:
        visited_urls: set[str] = set()
        page_count = 0

        while page_count < self.config.max_pages:
            current_url = page.url
            if current_url in visited_urls:
                logger.info("URL repeated — pagination loop detected, stopping")
                break
            visited_urls.add(current_url)
            page_count += 1

            yield page  # ← runner reads this page

            # Find the next button AFTER the runner has finished with this page
            selector = self.config.next_selector
            if not selector:
                logger.warning("next_selector is empty — cannot paginate")
                break

            btn = page.locator(selector).first

            try:
                visible = await btn.is_visible(timeout=3_000)
            except Exception:
                visible = False

            if not visible:
                logger.info("Next button not visible — end of results")
                break

            # Check for disabled state
            disabled = await btn.get_attribute("disabled")
            aria_disabled = await btn.get_attribute("aria-disabled")
            css_class = await btn.get_attribute("class") or ""
            if disabled is not None or aria_disabled == "true" or "disabled" in css_class.lower():
                logger.info("Next button disabled — end of results")
                break

            logger.debug("Clicking next button (page %d)", page_count)
            await btn.click()
            await self._wait_settled(page)

            # Guard: if URL didn't change and card count is the same,
            # we're stuck (some sites re-render without navigating)
            if page.url == current_url:
                new_count = await self._count_cards(page)
                if new_count == 0:
                    logger.info("URL unchanged and 0 cards — stopping")
                    break


# ---------------------------------------------------------------------------
# Infinite scroll
# ---------------------------------------------------------------------------


class InfiniteScrollPagination(PaginationStrategy):
    """
    Scrolls the page to the bottom repeatedly until document height stops
    growing. Yields the fully-loaded page ONCE at the end so the runner
    gets all cards in a single pass (avoids deduplication complexity).

    max_pages here acts as max scroll rounds.
    """

    async def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]:
        prev_height: int = 0
        stall_rounds: int = 0
        max_stalls: int = 3  # how many consecutive unchanged-height rounds before we stop
        scroll_round: int = 0

        logger.info("Infinite scroll: starting on %s", page.url)

        while scroll_round < self.config.max_pages:
            current_height: int = await page.evaluate("document.body.scrollHeight")

            if current_height == prev_height:
                stall_rounds += 1
                logger.debug("Scroll stall %d/%d (height=%d)", stall_rounds, max_stalls, current_height)
                if stall_rounds >= max_stalls:
                    logger.info("Height stabilised — all content loaded (%d rounds)", scroll_round)
                    break
            else:
                stall_rounds = 0
                logger.debug("Scrolled to %d (was %d)", current_height, prev_height)

            prev_height = current_height

            # Scroll to bottom
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

            # Let new items load
            await self._wait_settled(page)

            # Some pages use a "Load more" button instead of auto-loading;
            # try clicking it if present (best-effort)
            if self.config.next_selector:
                btn = page.locator(self.config.next_selector).first
                try:
                    if await btn.is_visible(timeout=1_000):
                        await btn.click()
                        await self._wait_settled(page)
                except Exception:
                    pass

            scroll_round += 1

        # Single yield with all content
        yield page


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_pagination_strategy(
    config: PaginationConfig,
    container_selector: str = "",
) -> PaginationStrategy:
    mapping = {
        PaginationType.NONE: NoPagination,
        PaginationType.URL_PARAM: UrlParamPagination,
        PaginationType.NEXT_BUTTON: NextButtonPagination,
        PaginationType.INFINITE_SCROLL: InfiniteScrollPagination,
    }
    cls = mapping.get(config.type, NoPagination)
    return cls(config, container_selector)
