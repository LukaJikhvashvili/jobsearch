import re
from bs4 import BeautifulSoup, Comment

# Tags that add no structural value and bloat the token count
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
    "meta",
    "head",
]

# Attributes worth keeping (everything else is stripped to reduce noise)
_KEEP_ATTRS = {
    "class",
    "id",
    "href",
    "src",
    "type",
    "name",
    "action",
    "method",
    "placeholder",
    "aria-label",
    "data-url",
    "data-href",
    "data-link",
}


def clean_html(html: str, max_chars: int = 40_000) -> str:
    """
    Strip scripts, styles, comments and noisy attributes from raw HTML.
    Returns a compact string safe to send to an LLM.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove noisy tags entirely
    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    # Remove HTML comments
    for node in soup.find_all(string=lambda t: isinstance(t, Comment)):
        node.extract()

    # Remove elements hidden via inline style
    hidden_re = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
    for tag in soup.find_all(style=hidden_re):
        tag.decompose()

    # Strip non-essential attributes from every remaining tag
    for tag in soup.find_all(True):
        keep = {k: v for k, v in tag.attrs.items() if k in _KEEP_ATTRS}
        tag.attrs = keep

    cleaned = str(soup)

    # Collapse runs of whitespace / blank lines
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r">\s+<", "><", cleaned)

    # Hard truncate with a best-effort attempt to end on a tag boundary
    if len(cleaned) > max_chars:
        truncated = cleaned[:max_chars]
        boundary = truncated.rfind("</")
        if boundary > max_chars * 0.75:
            truncated = truncated[:boundary]
        cleaned = truncated

    return cleaned.strip()


def extract_sample_cards(html: str, n_cards: int = 3, max_chars: int = 20_000) -> str:
    """
    Heuristically pull a small sample of the most-repeated element
    (likely the job card) from a listings page.

    Falls back to full clean_html() if no clear pattern is found.
    """
    soup = BeautifulSoup(html, "lxml")

    # Count (tag, frozenset(classes)) occurrences
    freq: dict[tuple, int] = {}
    for tag in soup.find_all(True):
        classes = frozenset(tag.get("class", []))
        key = (tag.name, classes)
        freq[key] = freq.get(key, 0) + 1

    # Must appear at least 3 times to be a repeating card
    candidates = {k: v for k, v in freq.items() if v >= 3}
    if not candidates:
        return clean_html(html, max_chars)

    best_tag, best_classes = max(candidates, key=lambda k: candidates[k])
    matches = soup.find_all(best_tag, class_=list(best_classes) or None)

    sample_html = "\n".join(str(el) for el in matches[:n_cards])
    return clean_html(sample_html, max_chars)
