"""
Profiler: captures HTML snapshots from a live site to feed the SchemaGenerator.

It opens the listings page with Playwright, finds the first job link,
opens the detail page, and returns cleaned HTML for both.

Usage:
    from scraper.profiler import capture_site_html
    listings_html, detail_html = await capture_site_html("https://jobs.ge/en/")
"""

import logging
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from .html_cleaner import clean_html, extract_sample_cards

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " "AppleWebKit/537.36 (KHTML, like Gecko) " "Chrome/124.0.0.0 Safari/537.36"
)


async def capture_site_html(
    listings_url: str,
    detail_url: Optional[str] = None,
    headless: bool = True,
    wait_ms: int = 2000,
    listings_max_chars: int = 40_000,
    detail_max_chars: int = 30_000,
) -> tuple[str, str]:
    """
    Navigate to listings_url and (optionally) detail_url using a real browser.
    If detail_url is not provided, the profiler clicks the first job link it finds.

    Returns:
        (listings_html_cleaned, detail_html_cleaned)
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        # -- Listings page --------------------------------------------------
        logger.info("Profiler: loading listings page %s", listings_url)
        page = await context.new_page()
        await page.goto(listings_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(wait_ms)

        # Scroll a bit to trigger any lazy-loaded content
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        await page.wait_for_timeout(1000)

        listings_raw = await page.content()

        # Try to extract just a few sample cards to keep token count low
        listings_html = extract_sample_cards(listings_raw, n_cards=3, max_chars=listings_max_chars)

        # -- Detail page ----------------------------------------------------
        if not detail_url:
            detail_url = await _find_first_job_link(page, listings_url)

        if not detail_url:
            logger.warning("Could not find a job detail link on %s", listings_url)
            detail_html = ""
        else:
            logger.info("Profiler: loading detail page %s", detail_url)
            detail_page = await context.new_page()
            await detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
            await detail_page.wait_for_timeout(wait_ms)
            detail_raw = await detail_page.content()
            detail_html = clean_html(detail_raw, max_chars=detail_max_chars)
            await detail_page.close()

        await browser.close()

    return listings_html, detail_html


async def _find_first_job_link(page, base_url: str) -> Optional[str]:
    """
    Heuristically find the first job listing link on the page.
    Tries common patterns before falling back to any <a> with a job-like href.
    """
    from bs4 import BeautifulSoup

    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    # Patterns that commonly appear in job listing URLs
    job_url_hints = ["/job/", "/vacancy/", "/position/", "/career/", "/jobs/", "/offer/"]

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        full = urljoin(base_url, href)
        if any(hint in full for hint in job_url_hints):
            return full

    # Broader fallback: any internal link that looks like a detail page
    # (has an integer ID segment)
    import re

    id_pattern = re.compile(r"/\d{3,}/")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if id_pattern.search(href):
            return urljoin(base_url, href)

    return None
