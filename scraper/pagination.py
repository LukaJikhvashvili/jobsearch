"""
Pagination strategies.

Root causes of the original failures:
  - InfiniteScrollPagination: used networkidle to detect new content, which
    times out frequently. Fixed: count-based detection (card count stabilises).
  - NextButtonPagination: used URL-change as loop guard, which breaks SPAs
    that don't change the URL. Fixed: content-hash comparison instead.
  - Both: _wait_settled called networkidle which is unreliable on SPA pages.
    Fixed: prefer card-count stabilisation over networkidle.
"""

import hashlib
import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator
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
        self.container_selector = container_selector

    @abstractmethod
    def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]: ...

    async def _count_cards(self, page: Page) -> int:
        if not self.container_selector:
            return 1
        try:
            return await page.locator(self.container_selector).count()
        except Exception:
            return 0

    async def _content_hash(self, page: Page) -> str:
        """
        Hash the current card list text — used to detect whether new content
        loaded without relying on URL changes or networkidle.
        """
        try:
            sel = self.container_selector or "body"
            text = await page.locator(sel).all_inner_texts()
            return hashlib.md5("".join(text).encode()).hexdigest()
        except Exception:
            return ""

    async def _wait_for_new_content(self, page: Page, prev_hash: str, max_wait_ms: int = 6_000, poll_ms: int = 400) -> bool:
        """
        Poll until content changes (new cards loaded) or timeout.
        Returns True if new content detected, False if timed out.
        """
        elapsed = 0
        while elapsed < max_wait_ms:
            await page.wait_for_timeout(poll_ms)
            elapsed += poll_ms
            new_hash = await self._content_hash(page)
            if new_hash and new_hash != prev_hash:
                return True
        return False

    async def _settle(self, page: Page) -> None:
        """Light settle: just wait the configured delay. No networkidle."""
        await page.wait_for_timeout(self.config.delay_ms)


# ---------------------------------------------------------------------------
# No pagination
# ---------------------------------------------------------------------------


class NoPagination(PaginationStrategy):
    async def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]:
        yield page


# ---------------------------------------------------------------------------
# URL parameter
# ---------------------------------------------------------------------------


class UrlParamPagination(PaginationStrategy):
    async def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]:
        parsed = urlparse(start_url)
        param = self.config.param_name or "page"

        # Track card identifiers across pages.
        # When a page adds zero new cards (all already seen), we've looped —
        # the site is returning its last real page repeatedly.
        seen_ids: set[str] = set()
        consecutive_no_new = 0

        for page_num in range(
            self.config.start_page,
            self.config.start_page + self.config.max_pages,
        ):
            url = self._build_url(parsed, param, page_num)
            logger.debug("UrlParam → %s", url)

            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await self._settle(page)

            # Collect card identifiers on this page
            page_ids = await self._collect_card_ids(page)

            if not page_ids:
                consecutive_no_new += 1
                if consecutive_no_new >= 2:
                    logger.info("Empty page x2 — stopping at p%d", page_num)
                    break
                continue

            new_ids = page_ids - seen_ids
            if not new_ids:
                consecutive_no_new += 1
                logger.info(
                    "Page %d returned %d cards but all already seen — " "last real page was p%d (stopping)",
                    page_num,
                    len(page_ids),
                    page_num - consecutive_no_new,
                )
                if consecutive_no_new >= 1:
                    break
                continue

            consecutive_no_new = 0
            seen_ids |= new_ids
            logger.debug("Page %d: %d new cards (%d total)", page_num, len(new_ids), len(seen_ids))
            yield page

    async def _collect_card_ids(self, page: Page) -> set[str]:
        """
        Build a set of stable identifiers for the cards on the current page.
        Uses href links when available, falls back to visible text.
        """
        if not self.container_selector:
            return set()
        try:
            ids: set[str] = set()
            cards = page.locator(self.container_selector)
            count = await cards.count()
            for i in range(min(count, 50)):  # cap to avoid slow pages
                card = cards.nth(i)
                # Prefer href (stable), fall back to text (less stable but works)
                try:
                    a = card.locator("a[href]").first
                    href = await a.get_attribute("href", timeout=500)
                    if href:
                        ids.add(href.strip())
                        continue
                except Exception:
                    pass
                try:
                    text = (await card.inner_text(timeout=500)).strip()[:120]
                    if text:
                        ids.add(text)
                except Exception:
                    pass
            return ids
        except Exception:
            return set()

    @staticmethod
    def _build_url(parsed, param: str, page_num: int) -> str:
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs[param] = [str(page_num)]
        return urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in qs.items()})))


# ---------------------------------------------------------------------------
# Next-button
# ---------------------------------------------------------------------------


class NextButtonPagination(PaginationStrategy):
    """
    Fixed:
    - Content-hash loop guard (works on SPAs that don't change URL)
    - Disabled state checks cover more patterns
    - Scroll button into view before clicking (avoids click interception)
    - After click, waits for content change rather than networkidle
    """

    async def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]:
        page_count = 0

        while page_count < self.config.max_pages:
            page_count += 1
            yield page  # runner processes this page

            selector = self.config.next_selector
            if not selector:
                logger.warning("next_selector missing — stopping")
                break

            btn = page.locator(selector).first

            # ── Visibility ──────────────────────────────────────────────────
            try:
                visible = await btn.is_visible(timeout=4_000)
            except Exception:
                visible = False
            if not visible:
                logger.info("Next button gone — end of results (page %d)", page_count)
                break

            # ── Disabled check — multiple patterns ───────────────────────────
            if await self._is_disabled(btn):
                logger.info("Next button disabled — end of results (page %d)", page_count)
                break

            # ── Capture state before click ───────────────────────────────────
            prev_hash = await self._content_hash(page)

            # ── Scroll into view, then click ─────────────────────────────────
            try:
                await btn.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass

            logger.debug("Clicking next (page %d)", page_count)
            try:
                await btn.click(timeout=5_000)
            except Exception as exc:
                logger.warning("Next button click failed: %s", exc)
                break

            # ── Wait for new content ─────────────────────────────────────────
            changed = await self._wait_for_new_content(page, prev_hash, max_wait_ms=8_000)
            if not changed:
                # Content didn't change — check if we're really at the end
                new_count = await self._count_cards(page)
                if new_count == 0:
                    logger.info("Content unchanged and 0 cards — end of results")
                    break
                # Content same but not empty → might be a legitimate duplicate; stop
                logger.info("Content unchanged after next-click — stopping to avoid loop")
                break

    @staticmethod
    async def _is_disabled(btn) -> bool:
        try:
            disabled = await btn.get_attribute("disabled")
            aria_dis = await btn.get_attribute("aria-disabled")
            css_class = (await btn.get_attribute("class") or "").lower()
            aria_cur = (await btn.get_attribute("aria-current") or "").lower()
            if disabled is not None:
                return True
            if aria_dis in ("true", "1"):
                return True
            if any(w in css_class for w in ("disabled", "inactive", "is-disabled")):
                return True
            if "last" in aria_cur:
                return True
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Infinite scroll
# ---------------------------------------------------------------------------


class InfiniteScrollPagination(PaginationStrategy):
    """
    Fixed:
    - Uses card COUNT stabilisation (not document height) as the stop signal.
      Height can stabilise while cards are still loading; count is more reliable.
    - Scrolls in smaller steps (viewport height) instead of jumping to the
      bottom in one go — some virtual-scroll implementations only load when
      the sentinel enters the viewport gradually.
    - Handles "Load more" button as a mid-scroll action.
    - Yields ALL cards at once at the end (single yield, no dedup complexity).
    """

    async def pages(self, page: Page, start_url: str) -> AsyncIterator[Page]:
        prev_count = -1
        stall_rounds = 0
        max_stalls = 4  # consecutive same-count rounds before stopping
        scroll_round = 0

        logger.info("InfiniteScroll starting on %s", page.url)

        while scroll_round < self.config.max_pages:
            scroll_round += 1

            # Scroll one viewport at a time
            await page.evaluate(
                """
                window.scrollBy({ top: window.innerHeight, behavior: 'smooth' });
            """
            )
            await page.wait_for_timeout(self.config.delay_ms)

            # Click "Load more" button if present
            if self.config.next_selector:
                btn = page.locator(self.config.next_selector).first
                try:
                    if await btn.is_visible(timeout=800):
                        await btn.click()
                        await page.wait_for_timeout(self.config.delay_ms)
                        logger.debug("Clicked load-more (round %d)", scroll_round)
                except Exception:
                    pass

            current_count = await self._count_cards(page)
            logger.debug(
                "Scroll round %d: %d cards (was %d)",
                scroll_round,
                current_count,
                prev_count,
            )

            if current_count == prev_count:
                stall_rounds += 1
                if stall_rounds >= max_stalls:
                    logger.info(
                        "Card count stabilised at %d after %d rounds — done",
                        current_count,
                        scroll_round,
                    )
                    break
            else:
                stall_rounds = 0
                prev_count = current_count

        # Scroll back to top so cards are in natural DOM order
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)

        yield page


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_pagination_strategy(config: PaginationConfig, container_selector: str = "") -> PaginationStrategy:
    mapping = {
        PaginationType.NONE: NoPagination,
        PaginationType.URL_PARAM: UrlParamPagination,
        PaginationType.NEXT_BUTTON: NextButtonPagination,
        PaginationType.INFINITE_SCROLL: InfiniteScrollPagination,
    }
    return mapping.get(config.type, NoPagination)(config, container_selector)
