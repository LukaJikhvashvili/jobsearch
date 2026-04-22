"""
Fuzzy + translation matching for filter option text.

Problem: the user says "software engineer", the site shows "Software Engineering"
or "პროგრამული უზრუნველყოფა" (Georgian). A substring check misses both.

Solution: three-layer matching:
  1. Exact substring (fast path)
  2. Fuzzy similarity via rapidfuzz (handles small typos / word order)
  3. Translation lookup (Georgian ↔ English common job/location terms)

Usage:
    from .filter_match import best_match_score, THRESHOLD

    if best_match_score(user_value, option_text) >= THRESHOLD:
        # select this option
"""

from typing import Optional

# Minimum similarity score (0–100) to accept a match
THRESHOLD = 72

# ---------------------------------------------------------------------------
# Translation tables — Georgian ↔ English
# Only the most common job-board terms; extend as you encounter new ones.
# ---------------------------------------------------------------------------

_GEO_TO_EN: dict[str, str] = {
    # Cities
    "თბილისი": "tbilisi",
    "ბათუმი": "batumi",
    "ქუთაისი": "kutaisi",
    "რუსთავი": "rustavi",
    "გორი": "gori",
    "ზუგდიდი": "zugdidi",
    "ფოთი": "poti",
    "თელავი": "telavi",
    # Job categories
    "პროგრამული უზრუნველყოფა": "software",
    "ინჟინერია": "engineering",
    "ფინანსები": "finance",
    "მარკეტინგი": "marketing",
    "გაყიდვები": "sales",
    "ადამიანური რესურსები": "hr",
    "იურიდიული": "legal",
    "ლოჯისტიკა": "logistics",
    "ჯანდაცვა": "healthcare",
    "განათლება": "education",
    "დიზაინი": "design",
    "ადმინისტრაცია": "administration",
    "ბუღალტერია": "accounting",
    # Date filters
    "დღეს": "today",
    "ამ კვირაში": "week",
    "ამ თვეში": "month",
}

# Build reverse table (English → Georgian)
_EN_TO_GEO: dict[str, str] = {v: k for k, v in _GEO_TO_EN.items()}

# Merged lookup (both directions)
_TRANSLATIONS: dict[str, str] = {**_GEO_TO_EN, **_EN_TO_GEO}


def translate(text: str) -> Optional[str]:
    """Return the translation of text if found, else None."""
    key = text.strip().lower()
    return _TRANSLATIONS.get(key)


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------


def _fuzzy_score(a: str, b: str) -> int:
    """
    Return similarity score 0–100 using rapidfuzz if available,
    falling back to a simple longest-common-substring ratio.
    """
    try:
        from rapidfuzz import fuzz

        # token_set_ratio handles word-order differences ("Engineer Software" vs "Software Engineer")
        return max(
            fuzz.token_set_ratio(a, b),
            fuzz.partial_ratio(a, b),
        )
    except ImportError:
        # Fallback: character overlap ratio
        a_set = set(a.lower().split())
        b_set = set(b.lower().split())
        if not a_set or not b_set:
            return 0
        overlap = len(a_set & b_set)
        return int(100 * overlap / max(len(a_set), len(b_set)))


def best_match_score(user_value: str, option_text: str) -> int:
    """
    Return the best similarity score (0–100) between user_value and option_text.

    Checks (in order):
      1. Normalised substring (exact, fast)
      2. Direct fuzzy score
      3. Translated user_value vs option_text
      4. Translated option_text vs user_value
    """
    uv = user_value.strip().lower()
    ot = option_text.strip().lower()

    # 1. Exact substring
    if uv in ot or ot in uv:
        return 100

    # 2. Fuzzy
    score = _fuzzy_score(uv, ot)
    if score >= THRESHOLD:
        return score

    # 3. Translate user_value → try against option
    uv_translated = (translate(uv) or "").lower()
    if uv_translated:
        s = _fuzzy_score(uv_translated, ot)
        score = max(score, s)
        if uv_translated in ot or ot in uv_translated:
            return 100

    # 4. Translate option → try against user_value
    ot_translated = (translate(ot) or "").lower()
    if ot_translated:
        s = _fuzzy_score(uv, ot_translated)
        score = max(score, s)
        if uv in ot_translated or ot_translated in uv:
            return 100

    return score


def matches(user_value: str, option_text: str, threshold: int = THRESHOLD) -> bool:
    return best_match_score(user_value, option_text) >= threshold
