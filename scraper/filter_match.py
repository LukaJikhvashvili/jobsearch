"""
Filter value matching with runtime translation and fuzzy scoring.

When the user says "marketing" and the page is in Georgian, we need to:
  1. Detect that the page is in Georgian (stored as page_languages in adapter)
  2. Translate "marketing" → "მარკეტინგი" at runtime
  3. Fuzzy-match the translated value against the option text on the page

Translation uses deep-translator (GoogleTranslator, free, no API key).
Rapidfuzz handles typos and word-order differences.

Public API:
    translate_value(text, source_lang, target_lang) -> str
    best_match_score(user_value, option_text, page_languages) -> float
    best_translated_value(user_value, page_languages) -> str
    matches(user_value, option_text, page_languages) -> bool
"""

import logging
from functools import lru_cache
from typing import List, Optional

logger = logging.getLogger(__name__)

THRESHOLD = 60  # minimum score (0–100) to accept a match


# ---------------------------------------------------------------------------
# Runtime translation via deep-translator
# ---------------------------------------------------------------------------


@lru_cache(maxsize=512)
def translate_value(text: str, source_lang: str, target_lang: str) -> str:
    """
    Translate text from source_lang to target_lang.
    Returns original text on failure (graceful degradation).
    Uses @lru_cache so the same translation is only fetched once per session.
    """
    if source_lang == target_lang or not text.strip():
        return text
    try:
        from deep_translator import GoogleTranslator

        result = GoogleTranslator(source=source_lang, target=target_lang).translate(text)
        logger.debug("Translated '%s' [%s→%s] → '%s'", text, source_lang, target_lang, result)
        return result or text
    except Exception as exc:
        logger.debug("Translation failed ('%s' %s→%s): %s", text, source_lang, target_lang, exc)
        return text


def best_translated_value(user_value: str, page_languages: List[str], user_lang: str = "en") -> List[str]:
    """
    Return all useful translations of user_value for the given page languages.
    Always includes the original value. Deduplicates results.

    Example: user_value="marketing", page_languages=["ka","en"]
      → ["marketing", "მარკეტინგი"]
    """
    variants = [user_value]
    for lang in page_languages:
        if lang == user_lang:
            continue
        translated = translate_value(user_value.strip().lower(), user_lang, lang)
        if translated and translated.lower() != user_value.lower():
            variants.append(translated)
    return list(dict.fromkeys(variants))  # deduplicate, preserve order


# ---------------------------------------------------------------------------
# Fuzzy scoring
# ---------------------------------------------------------------------------


def _fuzzy_score(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz

        return max(
            fuzz.token_set_ratio(a, b),
            fuzz.partial_ratio(a, b),
        )
    except ImportError:
        a_set = set(a.lower().split())
        b_set = set(b.lower().split())
        if not a_set or not b_set:
            return 0.0
        overlap = len(a_set & b_set)
        return 100.0 * overlap / max(len(a_set), len(b_set))


# ---------------------------------------------------------------------------
# Main matching functions
# ---------------------------------------------------------------------------


def best_match_score(
    user_value: str, option_text: str, page_languages: Optional[List[str]] = None, user_lang: str = "en"
) -> float:
    """
    Return best similarity score (0–100) between user_value and option_text.

    Matching layers:
      1. Exact substring (fast path, returns 100 immediately)
      2. Fuzzy score on original values
      3. For each translated variant of user_value:
           a. Exact substring against option_text
           b. Fuzzy score against option_text
      4. Translate option_text to user_lang, fuzzy against user_value
    """
    uv = user_value.strip().lower()
    ot = option_text.strip().lower()

    if not uv or not ot:
        return 0.0

    # 1. Exact substring
    if uv in ot or ot in uv:
        return 100.0

    # 2. Direct fuzzy
    score = _fuzzy_score(uv, ot)
    if score >= THRESHOLD:
        return score

    langs = page_languages or ["en"]

    # 3. Translated user_value variants vs option_text
    variants = best_translated_value(uv, langs, user_lang)
    for variant in variants[1:]:  # skip [0] — it's the original, already checked
        v = variant.strip().lower()
        if v in ot or ot in v:
            return 100.0
        s = _fuzzy_score(v, ot)
        score = max(score, s)

    # 4. Translate option_text back to user_lang, compare against original
    for lang in langs:
        if lang == user_lang:
            continue
        try:
            ot_in_user_lang = translate_value(ot, lang, user_lang).lower()
            if uv in ot_in_user_lang or ot_in_user_lang in uv:
                return 100.0
            s = _fuzzy_score(uv, ot_in_user_lang)
            score = max(score, s)
        except Exception:
            pass

    return score


def matches(
    user_value: str,
    option_text: str,
    page_languages: Optional[List[str]] = None,
    threshold: float = THRESHOLD,
    user_lang: str = "en",
) -> bool:
    return best_match_score(user_value, option_text, page_languages, user_lang) >= threshold


def get_best_translated_input(user_value: str, page_languages: List[str], user_lang: str = "en") -> str:
    """
    For search_field inputs: return the best single translated value to type in.
    If the page is multilingual, prefer the non-English translation.
    """
    variants = best_translated_value(user_value, page_languages, user_lang)
    # Prefer translated variant (last one), fallback to original
    return variants[-1] if len(variants) > 1 else variants[0]
