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


def clean_html(html: str, keep_data_attrs: bool = True) -> str:
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

    return cleaned.strip()
