"""
HTML cleaning utilities.

Key design decision: we keep onclick, data-*, and aria-* attributes
so the LLM can distinguish JS-driven navigation (card_click) from
plain href links (direct_link). Without onclick, every card looks
like a direct_link even if it has no <a> tag.
"""

import re
from bs4 import BeautifulSoup, Comment

_NOISE_TAGS = [
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "canvas",
    "video",
    "audio",
    "picture",
    "source",
    "link",
    "meta",  # keep <head> so pagination/title hints survive
]

# Attributes kept during cleaning.
# onclick / data-* / aria-* are deliberately preserved — they reveal
# JS navigation patterns the LLM needs to classify correctly.
_KEEP_ATTRS = {
    # Structure & identity
    "class",
    "id",
    "type",
    "name",
    "role",
    # Links & navigation
    "href",
    "src",
    "action",
    "method",
    # Forms
    "placeholder",
    "value",
    "for",
    # JS navigation signals  ← THE FIX for card_click detection
    "onclick",
    "data-url",
    "data-href",
    "data-link",
    "data-id",
    "data-job-id",
    "data-vacancy-id",
    # Accessibility (helps LLM understand interactive elements)
    "aria-label",
    "aria-disabled",
    "aria-selected",
    # Generic data-* pass-through handled separately below
}


def clean_html(html: str, max_chars: int = 40_000, keep_data_attrs: bool = True) -> str:
    """
    Strip noise but preserve JS navigation signals and data attributes.
    """
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    for node in soup.find_all(string=lambda t: isinstance(t, Comment)):
        node.extract()

    hidden_re = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
    for tag in soup.find_all(style=hidden_re):
        tag.decompose()

    for tag in soup.find_all(True):
        keep = {}
        for k, v in tag.attrs.items():
            if k in _KEEP_ATTRS:
                keep[k] = v
            elif keep_data_attrs and k.startswith("data-"):
                keep[k] = v  # keep ALL data-* attributes
        tag.attrs = keep

    cleaned = str(soup)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r">\s+<", "><", cleaned)

    if len(cleaned) > max_chars:
        truncated = cleaned[:max_chars]
        boundary = truncated.rfind("</")
        if boundary > max_chars * 0.75:
            truncated = truncated[:boundary]
        cleaned = truncated

    return cleaned.strip()


def extract_pagination_area(html: str, max_chars: int = 8_000) -> str:
    """
    Extract the bottom portion of the page where pagination controls live.
    Also checks for common pagination wrapper selectors.
    """
    soup = BeautifulSoup(html, "lxml")

    # Try common pagination container selectors first
    pagination_selectors = [
        "[class*='pagination']",
        "[class*='paging']",
        "[class*='pages']",
        "[id*='pagination']",
        "[id*='paging']",
        "nav[aria-label*='page']",
        ".load-more",
        "[class*='load-more']",
        "[class*='infinite']",
        "[data-infinite]",
    ]
    for sel in pagination_selectors:
        try:
            el = soup.select_one(sel)
            if el:
                return clean_html(str(el), max_chars)
        except Exception:
            continue

    # Fallback: last 25% of body HTML
    body = soup.find("body")
    if body:
        body_str = str(body)
        bottom = body_str[int(len(body_str) * 0.75) :]
        return clean_html(bottom, max_chars)

    return ""
