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
    "img",
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
