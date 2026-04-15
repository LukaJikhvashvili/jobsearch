"""
scraper/page_cleaner.py — Turn raw HTML into clean Markdown for Gemini.

Why Crawl4AI?
  Raw HTML sent to Gemini wastes tokens on nav bars, footers, ads, scripts.
  Crawl4AI strips noise and outputs compact Markdown — reducing token cost
  by ~70-90% vs sending raw HTML.

Two modes:
  1. fetch_and_clean(url)   — Crawl4AI fetches the URL itself (async, fast).
  2. clean_html(html_str)   — You already have the HTML (from Playwright).
                               Crawl4AI cleans it without a second HTTP request.

Mode 2 is preferred here because Playwright already navigated (sessions,
cookies, anti-bot solved). We hand the HTML to Crawl4AI for cleaning only.
"""

import logging
import re
from typing import Optional

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from config import GEMINI_MAX_INPUT_CHARS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Noise patterns to strip from Markdown output before sending to Gemini.
# These survive even after Crawl4AI cleans the HTML.
# ---------------------------------------------------------------------------
_NOISE_PATTERNS = [
    r"!\[.*?\]\(.*?\)",            # Markdown images (irrelevant)
    r"\[cookie[^\]]*\]\([^)]*\)",  # Cookie banner links
    r"(accept all cookies?.*?\n)", # Cookie consent text
    r"(subscribe to.*?newsletter.*?\n)",
    r"(\bjavascript:\S+)",
    r"(©.*?\d{4}.*?\n)",           # Copyright footers
]
_NOISE_RE = re.compile(
    "|".join(_NOISE_PATTERNS),
    flags=re.IGNORECASE | re.MULTILINE,
)


class PageCleaner:
    """
    Wraps Crawl4AI to produce clean, token-efficient Markdown from HTML.

    Typical usage pattern:
        cleaner = PageCleaner()

        # After Playwright loads a page:
        html = await page.content()
        markdown = await cleaner.clean_html(html, url=page.url)

        # Then send `markdown` to Gemini.
    """

    def __init__(self):
        # PruningContentFilter: removes low-content blocks (navbars, ads, etc.)
        # threshold 0.45 is a good balance — keeps job description, drops chrome.
        self._content_filter = PruningContentFilter(
            threshold=0.45,
            threshold_type="fixed",
            min_word_threshold=10,   # drop blocks with fewer than 10 words
        )
        self._md_generator = DefaultMarkdownGenerator(
            content_filter=self._content_filter,
            options={
                "ignore_links": False,   # keep links (we need job URLs)
                "body_width": 0,         # no line wrapping
            },
        )
        self._config = CrawlerRunConfig(
            markdown_generator=self._md_generator,
            excluded_tags=[
                "script", "style", "noscript", "iframe",
                "header", "footer", "nav", "aside",
                "svg", "canvas", "video", "audio",
            ],
            exclude_external_links=True,    # drop outbound links (noise)
            exclude_social_media_links=True,
            remove_overlay_elements=True,   # removes popups/banners
            wait_until="domcontentloaded",
        )

    async def clean_html(
        self,
        html: str,
        url: str = "about:blank",
        max_chars: Optional[int] = None,
    ) -> str:
        """
        Clean raw HTML string into compact Markdown.

        Args:
            html:      Full HTML string from Playwright page.content().
            url:       The page URL (used by Crawl4AI for link resolution).
            max_chars: Truncate output to this many chars (default: GEMINI_MAX_INPUT_CHARS).

        Returns:
            Clean Markdown string, truncated and noise-filtered.
        """
        limit = max_chars or GEMINI_MAX_INPUT_CHARS

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(
                url=url,
                config=self._config,
                html_content=html,   # pass pre-loaded HTML, skip HTTP request
            )

        if not result.success:
            logger.warning(f"Crawl4AI failed for {url}: {result.error_message}")
            # Fallback: basic HTML tag stripping
            return _basic_html_strip(html)[:limit]

        markdown = result.markdown_v2.fit_markdown or result.markdown or ""
        markdown = _remove_noise(markdown)
        markdown = _collapse_whitespace(markdown)

        if len(markdown) > limit:
            logger.debug(f"Markdown truncated {len(markdown)} → {limit} chars for {url}")
            markdown = markdown[:limit]

        logger.debug(
            f"Page cleaned | url={url[:60]} | "
            f"html={len(html)} chars → md={len(markdown)} chars "
            f"({100*len(markdown)//max(len(html),1)}% of original)"
        )
        return markdown

    async def fetch_and_clean(
        self,
        url: str,
        max_chars: Optional[int] = None,
    ) -> str:
        """
        Fetch a URL and return clean Markdown in one step.

        Use this when Playwright is NOT available (e.g. background jobs).
        For most scraping, prefer clean_html() after Playwright loads the page.
        """
        limit = max_chars or GEMINI_MAX_INPUT_CHARS

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url, config=self._config)

        if not result.success:
            logger.warning(f"Crawl4AI fetch failed for {url}: {result.error_message}")
            return ""

        markdown = result.markdown_v2.fit_markdown or result.markdown or ""
        markdown = _remove_noise(markdown)
        markdown = _collapse_whitespace(markdown)

        if len(markdown) > limit:
            markdown = markdown[:limit]

        logger.debug(f"Fetched+cleaned {url[:60]} → {len(markdown)} chars")
        return markdown

    async def clean_for_job_listing(self, html: str, url: str) -> str:
        """
        Specialised cleaning for search results pages.
        Uses a looser filter to preserve more card-level content.
        """
        # For results pages we want denser content — lower pruning threshold
        results_filter = PruningContentFilter(
            threshold=0.3,   # keep more blocks
            threshold_type="fixed",
            min_word_threshold=5,
        )
        results_generator = DefaultMarkdownGenerator(
            content_filter=results_filter,
            options={"ignore_links": False, "body_width": 0},
        )
        results_config = CrawlerRunConfig(
            markdown_generator=results_generator,
            excluded_tags=[
                "script", "style", "noscript", "iframe",
                "header", "nav", "aside", "svg",
            ],
            exclude_social_media_links=True,
            remove_overlay_elements=True,
        )

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(
                url=url,
                config=results_config,
                html_content=html,
            )

        if not result.success:
            return _basic_html_strip(html)[:GEMINI_MAX_INPUT_CHARS]

        markdown = result.markdown_v2.fit_markdown or result.markdown or ""
        markdown = _remove_noise(markdown)
        markdown = _collapse_whitespace(markdown)
        return markdown[:GEMINI_MAX_INPUT_CHARS]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _remove_noise(text: str) -> str:
    """Strip common noise patterns from Markdown."""
    return _NOISE_RE.sub("", text)


def _collapse_whitespace(text: str) -> str:
    """Collapse 3+ blank lines into 2, strip leading/trailing whitespace."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _basic_html_strip(html: str) -> str:
    """
    Emergency fallback: remove all HTML tags and collapse whitespace.
    Not as clean as Crawl4AI but better than nothing.
    """
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
