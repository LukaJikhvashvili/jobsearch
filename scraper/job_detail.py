"""
scraper/job_detail.py — Navigate to a job detail page and extract full info.

Flow:
  1. Open job URL in Playwright.
  2. Scroll through the page (some sites lazy-load requirements sections).
  3. Clean HTML → Markdown via Crawl4AI.
  4. Send to Gemini Flash → structured JobDetail.
  5. Return JobDetail.

Usage:
    extractor = JobDetailExtractor(bm, gemini, cleaner)
    detail = await extractor.extract(job_listing)
"""

import asyncio
import logging
import random

from browser.launcher import BrowserManager, HumanActions
from gemini_helper import GeminiHelper, JobDetail, JobListing
from scraper.page_cleaner import PageCleaner

logger = logging.getLogger(__name__)


class JobDetailExtractor:
    """
    Extracts structured details from a job posting page.
    Works on any site — no site-specific selectors.
    """

    def __init__(self, browser_manager: BrowserManager, gemini: GeminiHelper, cleaner: PageCleaner):
        self._bm = browser_manager
        self._gemini = gemini
        self._cleaner = cleaner

    async def extract(self, job: JobListing, site_name: str = "default") -> JobDetail | None:
        """
        Navigate to job URL and extract full structured details.

        Args:
            job:       JobListing (must have a valid .link URL).
            site_name: Used for persistent browser context (cookies).

        Returns:
            JobDetail object, or None if extraction failed.
        """
        if not job.link:
            logger.warning(f"No link for job '{job.title}' — skipping detail extraction.")
            return None

        logger.info(f"Extracting detail: '{job.title}' @ {job.company} → {job.link[:80]}")

        page = await self._bm.new_page(site_name)
        try:
            success = await HumanActions.safe_goto(page, job.link)
            if not success:
                logger.warning(f"Failed to navigate to {job.link}")
                return None

            # Wait a moment for dynamic content (requirements sections often load late)
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # Scroll through the full page — some job sites lazy-load the body
            await HumanActions.scroll_to_bottom(page)
            await asyncio.sleep(1.5)
            await HumanActions.scroll_page(page, scrolls=2, direction="up")
            await asyncio.sleep(0.8)

            html = await HumanActions.get_page_html(page)

        finally:
            await self._bm.close_page(page)

        # Clean HTML → Markdown
        markdown = await self._cleaner.clean_html(html, url=job.link)

        if not markdown.strip():
            logger.warning(f"Empty content from {job.link}")
            return None

        # Extract structured data with Gemini (1 call)
        try:
            detail = await self._gemini.extract_job_detail(markdown, job_url=job.link)
        except Exception as e:
            logger.error(f"Gemini extraction failed for {job.link}: {e}")
            return None

        # Backfill missing fields from the listing if Gemini left them empty
        if not detail.title:
            detail.title = job.title
        if not detail.company:
            detail.company = job.company
        if not detail.location:
            detail.location = job.location

        logger.info(
            f"Detail extracted: '{detail.title}' | "
            f"{len(detail.skills_required)} required skills | "
            f"{len(detail.responsibilities)} responsibilities"
        )
        return detail

    async def extract_batch(
        self, jobs: list[JobListing], site_name: str = "default", max_concurrent: int = 1  # keep at 1 to avoid bans
    ) -> list[tuple[JobListing, JobDetail | None]]:
        """
        Extract details for multiple jobs with polite delays between each.

        Returns list of (JobListing, JobDetail | None) pairs.
        max_concurrent=1 is intentional — sequential is safer for anti-bot.
        """
        results = []
        for i, job in enumerate(jobs):
            logger.info(f"Detail extraction {i+1}/{len(jobs)}: {job.title}")
            detail = await self.extract(job, site_name=site_name)
            results.append((job, detail))

            # Pause between jobs
            if i < len(jobs) - 1:
                delay = random.uniform(15, 45)
                logger.debug(f"Waiting {delay:.1f}s before next job detail…")
                await asyncio.sleep(delay)

        return results
