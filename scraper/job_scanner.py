"""
scraper/job_scanner.py — Universal job listing scanner.

No CSS selectors. No XPath.
Playwright navigates, Crawl4AI cleans, Gemini extracts.

Flow per site:
  1. Build search URL from site config + query/location.
  2. Load page with Playwright (stealth, human delays).
  3. Scroll to load dynamic content.
  4. Clean HTML → Markdown via Crawl4AI.
  5. Send Markdown to Gemini Flash → structured JobListingsPage.
  6. Follow pagination up to site.max_pages limit.
  7. Return deduplicated list of JobListing objects.

Usage:
    async with BrowserManager() as bm:
        scanner = JobScanner(bm, gemini_helper, page_cleaner)
        jobs = await scanner.scan(
            site_name="jobs_ge",
            query="software engineer",
            location="Tbilisi",
        )
"""

import asyncio
import logging
import random
from urllib.parse import urljoin, quote_plus

from browser.launcher import BrowserManager, HumanActions
from config import JOB_SITES, SiteConfig
from gemini_helper import GeminiHelper, JobListing, JobListingsPage
from scraper.page_cleaner import PageCleaner

logger = logging.getLogger(__name__)


class JobScanner:
    """
    Scans job search result pages for any supported site.

    Works universally because extraction is LLM-based — the same code
    handles jobs.ge, hh.ge, LinkedIn, Indeed, etc.
    Only the URL template and wait times differ per site (in config.py).
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        gemini: GeminiHelper,
        cleaner: PageCleaner,
    ):
        self._bm = browser_manager
        self._gemini = gemini
        self._cleaner = cleaner

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def scan(self, site_name: str, query: str, location: str = "", max_pages: int = None) -> list[JobListing]:
        """
        Scan a job site and return a deduplicated list of job listings.

        Args:
            site_name: Key from config.JOB_SITES (e.g. "jobs_ge").
            query:     Search keywords (e.g. "python developer").
            location:  Location filter (e.g. "Tbilisi"). Empty = no filter.
            max_pages: Override site default page limit.

        Returns:
            List of JobListing objects, deduplicated by link.
        """
        if site_name not in JOB_SITES:
            raise ValueError(f"Unknown site '{site_name}'. " f"Available: {list(JOB_SITES.keys())}")

        site = JOB_SITES[site_name]
        limit = max_pages or site.max_pages

        logger.info(f"[{site.name}] Starting scan | query='{query}' | " f"location='{location}' | max_pages={limit}")

        # Open page in site-specific context (persistent session)
        page = await self._bm.new_page(site_name)
        all_jobs: list[JobListing] = []
        seen_links: set[str] = set()

        try:
            # Check / perform login if required
            if site.requires_login and not self._bm.is_logged_in(site_name):
                logged_in = await self._handle_login(page, site)
                if not logged_in:
                    logger.error(f"[{site.name}] Login required but failed — skipping scan.")
                    return []

            search_url = self._build_search_url(site, query, location)
            current_url = search_url
            page_num = 1

            while page_num <= limit and current_url:
                logger.info(f"[{site.name}] Scanning page {page_num}/{limit}: {current_url}")

                # Navigate
                success = await HumanActions.safe_goto(page, current_url)
                if not success:
                    logger.warning(f"[{site.name}] Navigation failed on page {page_num}, stopping.")
                    break

                # Wait for dynamic content
                await asyncio.sleep(site.dynamic_wait_s)

                # Scroll to trigger lazy-loaded listings
                await HumanActions.scroll_page(page, scrolls=random.randint(4, 7))
                await asyncio.sleep(1.5)

                # Scroll back up slightly (natural reading behavior)
                await HumanActions.scroll_page(page, scrolls=2, direction="up")
                await asyncio.sleep(random.uniform(0.5, 1.5))

                # Get the page HTML
                html = await HumanActions.get_page_html(page)

                # Clean with Crawl4AI (specialized for listing pages)
                markdown = await self._cleaner.clean_for_job_listing(html, url=current_url)

                if not markdown.strip():
                    logger.warning(f"[{site.name}] Empty Markdown on page {page_num}, stopping.")
                    break

                # Extract jobs with Gemini (1 call per page)
                try:
                    result: JobListingsPage = await self._gemini.extract_job_listings(markdown, site_name=site.name)
                except Exception as e:
                    import traceback

                    logger.error(f"[{site.name}] Gemini extraction failed on page 1: {e}")
                    logger.error(traceback.format_exc())
                    break

                # Resolve relative URLs and deduplicate
                new_jobs = 0
                for job in result.jobs:
                    job.link = self._resolve_url(job.link, site.base_url)
                    if job.link not in seen_links:
                        seen_links.add(job.link)
                        all_jobs.append(job)
                        new_jobs += 1

                logger.info(
                    f"[{site.name}] Page {page_num}: "
                    f"{len(result.jobs)} found | {new_jobs} new | "
                    f"total so far: {len(all_jobs)}"
                )

                # Pagination: determine next URL
                if not result.has_next_page or page_num >= limit:
                    break

                current_url = await self._find_next_page_url(
                    page=page,
                    hint=result.next_page_hint,
                    current_url=current_url,
                    site=site,
                )

                page_num += 1

                # Human pause between pages
                if current_url and page_num <= limit:
                    delay = random.uniform(
                        DELAYS_BETWEEN_PAGES_MIN,
                        DELAYS_BETWEEN_PAGES_MAX,
                    )
                    logger.debug(f"[{site.name}] Next page in {delay:.1f}s…")
                    await asyncio.sleep(delay)

        finally:
            await self._bm.close_page(page)

        logger.info(f"[{site.name}] Scan complete | " f"{len(all_jobs)} unique jobs found")
        return all_jobs

    async def scan_multiple_sites(
        self,
        sites: list[str],
        query: str,
        location: str = "",
    ) -> dict[str, list[JobListing]]:
        """
        Scan multiple sites sequentially (not parallel — avoids rate limit issues).

        Returns dict of site_name → [JobListing, ...]
        """
        results = {}
        for site_name in sites:
            try:
                jobs = await self.scan(site_name, query, location)
                results[site_name] = jobs
            except Exception as e:
                logger.error(f"Scan failed for '{site_name}': {e}")
                results[site_name] = []

            # Pause between different sites
            if site_name != sites[-1]:
                await asyncio.sleep(random.uniform(10, 20))

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_search_url(site: SiteConfig, query: str, location: str) -> str:
        """Build the search URL by substituting encoded query/location."""
        return site.search_url_template.format(
            query=quote_plus(query),
            location=quote_plus(location) if location else "",
        )

    @staticmethod
    def _resolve_url(link: str, base_url: str) -> str:
        """Convert relative links to absolute URLs."""
        if not link:
            return ""
        if link.startswith("http"):
            return link
        return urljoin(base_url, link)

    async def _find_next_page_url(
        self,
        page,
        hint: str,
        current_url: str,
        site: SiteConfig,
    ) -> str | None:
        """
        Try to find the URL for the next page.

        Strategy (in order):
          1. If `hint` text is given, find a link with that text on the page.
          2. Look for common "next" button patterns.
          3. Return None (stop pagination).
        """
        next_selectors = [
            f"text={hint}" if hint else None,
            "a[aria-label='Next']",
            "a[aria-label='next']",
            "a.next",
            "a[rel='next']",
            ".pagination__next a",
            "[class*='next'] a",
            "a:has-text('Next')",
            "a:has-text('>')",
            "a:has-text('→')",
            "button:has-text('Next')",
        ]

        for selector in next_selectors:
            if not selector:
                continue
            try:
                element = await page.query_selector(selector)
                if element:
                    href = await element.get_attribute("href")
                    if href:
                        return self._resolve_url(href, site.base_url)
                    # Button with JS onclick — click it and return new URL
                    await element.click()
                    await asyncio.sleep(site.dynamic_wait_s)
                    return page.url
            except Exception:
                continue

        logger.debug("No next-page element found — pagination ends.")
        return None

    async def _handle_login(self, page, site: SiteConfig) -> bool:
        """
        Prompt user to log in manually in the visible browser window.
        Waits up to 2 minutes for them to complete login, then saves session.

        This is intentionally manual — automated login is fragile and risks
        account flagging. User does it once; session is reused thereafter.
        """
        logger.info(f"[{site.name}] Login required. " f"Please log in at {site.login_url} in the browser window.")
        print(f"\n{'='*60}")
        print(f"[{site.name}] Manual login required.")
        print(f"1. The browser will open {site.login_url}")
        print(f"2. Please log in manually.")
        print(f"3. After you're logged in, press ENTER here to continue.")
        print(f"{'='*60}")

        await HumanActions.safe_goto(page, site.login_url)

        input("\nPress ENTER after you have logged in successfully: ")

        # Verify we're no longer on the login page
        current = page.url
        if "login" in current.lower() or "signin" in current.lower():
            logger.warning(f"[{site.name}] Still on login page — login may have failed.")
            return False

        # Save the session for next time
        await self._bm.save_session(site.name.lower().replace(".", "_"))
        logger.info(f"[{site.name}] Login successful, session saved.")
        return True


# ---------------------------------------------------------------------------
# Module-level delay constants (separate from DELAYS config for clarity)
# ---------------------------------------------------------------------------
DELAYS_BETWEEN_PAGES_MIN = 5.0  # seconds between pagination requests
DELAYS_BETWEEN_PAGES_MAX = 12.0
