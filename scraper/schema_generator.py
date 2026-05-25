"""
Phase 1 — Listings page analysis only.

Discovers:
  - Card container + title/company selectors
  - Pagination type and config
  - Navigation to detail page
  - All available filter controls
  - Page language(s)

Phase 2 (detail page analysis) is deferred — detail stays None in the adapter.
"""

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from .models import SiteAdapter

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 8192  # Adjust as needed based on expected schema size and LLM limits

# ---------------------------------------------------------------------------
_SHARED_RULES = """STRICT RULES:
1. Return ONLY a single valid JSON object. No markdown fences, no prose.
2. If something is absent from the page, set its selector/value to null.
3. Prefer class/id selectors. Avoid nth-child unless unavoidable.
4. Selectors inside a card container must be RELATIVE to that container."""

# ---------------------------------------------------------------------------
_PHASE1_SYSTEM = f"""You are an expert web scraping engineer analysing a job listings page.
Extract ONLY what is visible on the listings page.

{_SHARED_RULES}

─── LANGUAGE DETECTION ─────────────────────────────────────────────────────
Detect the language(s) of the page content. Return as a JSON array of
ISO 639-1 two-letter codes (e.g. ["ka"] for Georgian-only, ["ka","en"] for
a bilingual Georgian/English site, ["en"] for English-only).
Common codes: en=English, ka=Georgian, ru=Russian, de=German, fr=French,
tr=Turkish, az=Azerbaijani, hy=Armenian.

─── LISTINGS FIELDS ────────────────────────────────────────────────────────
Extract ONLY these two fields from each job card:
  title   — the job title / position name
  company — the employer / company name

─── NAVIGATION TYPES ───────────────────────────────────────────────────────
  direct_link  — card has <a href="…"> pointing directly to detail page
                 → link_selector: CSS of that <a> (relative to container)
  card_click   — whole card navigates via JS onclick, no plain <a>
                 → click_container: true, link_selector: null
  button_click — a specific button/CTA inside the card triggers navigation
                 → link_selector: that button's CSS
  data_attr    — URL stored in a data-* attribute
                 → data_attribute: the attribute name

─── PAGINATION TYPES ───────────────────────────────────────────────────────
Examine BOTH the job cards HTML AND the pagination area HTML carefully.

  url_param       — page number in URL query param (?page=2) or path (/page/2)
  next_button     — a visible Next / › / >> / შემდეგი button or link exists
  infinite_scroll — no page controls at all; content loads on scroll
  none            — all jobs fit on one page, no pagination

IMPORTANT: if you see a "next" button, arrow, or translated equivalent → use next_button.
If you see page numbers or a ?page= param → use url_param.
If neither → use infinite_scroll or none.

─── FILTER MECHANISMS ──────────────────────────────────────────────────────
  url_param      → param_name: query key
  search_field   → selector: input element
  dropdown       → selector: <select>
  checkbox_group → selector: container, item_selector: each checkbox
  tag_filter     → selector: container, item_selector: each pill/chip
  radio_group    → selector: container, item_selector: each radio
  date_range     → date_from_selector, date_to_selector

FILTER DIMENSIONS:
  keyword / location / category / salary / date_posted

OUTPUT SCHEMA — fill every key, null for absent:
{{
  "site":             "domain, e.g. jobs.ge",
  "base_url":         "origin URL",
  "listings_url":     "the URL provided",
  "requires_js":      false,
  "page_languages":   ["en"],
  "notes":            null,
  "listings": {{
    "container": "CSS for repeating job card",
    "fields": {{
      "title":   {{"selector": "CSS or null", "attr": "text"}},
      "company": {{"selector": "CSS or null", "attr": "text"}}
    }},
    "pagination": {{
      "type":          "url_param | next_button | infinite_scroll | none",
      "param_name":    "query param name or null",
      "start_page":    1,
      "next_selector": "CSS for Next button or null",
      "max_pages":     50,
      "delay_ms":      1200
    }},
    "navigation": {{
      "type":            "direct_link | card_click | button_click | data_attr",
      "link_selector":   "CSS relative to container, or null",
      "data_attribute":  "attribute name or null",
      "click_container": false,
      "notes":           "how you determined this or null"
    }},
    "filters": {{
      "available": [
        {{
          "dimension":          "keyword | location | category | salary | date_posted",
          "mechanism":          "url_param | search_field | dropdown | checkbox_group | tag_filter | radio_group | date_range",
          "param_name":         null,
          "selector":           null,
          "options_selector":   null,
          "item_selector":      null,
          "date_from_selector": null,
          "date_to_selector":   null,
          "notes":              null
        }}
      ],
      "submit_selector": null,
      "notes": null
    }}
  }}
}}"""


def _phase1_user(site: str, listings_url: str, listings_html: str) -> str:
    return (
        f"Site: {site}\n"
        f"Listings URL: {listings_url}\n\n"
        f"=== SAMPLE JOB LISTINGS PAGE HTML (post-JS render) ===\n{listings_html}\n"
        "NOTE: This HTML is the RENDERED DOM after JavaScript execution. "
        "onclick attrs, data-* attrs, and dynamic content are all present. "
        "Use them to correctly classify navigation and paginations types.\n\n"
        "Return the Phase 1 JSON now."
    )


# ---------------------------------------------------------------------------
# AI providers
# ---------------------------------------------------------------------------


class AIProvider(ABC):
    @abstractmethod
    def generate(self, system: str, user: str) -> str: ...

    @property
    def name(self) -> str:
        return self.__class__.__name__


class GeminiProvider(AIProvider):
    def __init__(self, api_key: str):
        from google import genai
        from google.genai import types

        self._model_name = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
        self._client = genai.Client(api_key=api_key)
        self._types = types

    def generate(self, system: str, user: str) -> str:
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=f"{system}\n\n{user}",
            config=self._types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
        return response.text

    @property
    def name(self) -> str:
        return f"Gemini({self._model_name})"


class ClaudeProvider(AIProvider):
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001"):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def generate(self, system: str, user: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text

    @property
    def name(self) -> str:
        return f"Claude({self._model})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    """Return the substring spanning the outermost { … } pair."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def _repair_and_parse(raw: str) -> dict:
    """
    Multi-strategy JSON parsing with progressive fallback.

    1. Direct parse after fence-stripping (happy path).
    2. Extract outermost { … } and retry.
    3. json-repair library (optional — `pip install json-repair`).
    4. Brace-counting truncation: walk the string keeping a stack;
       truncate at the last position where all open structures were closed,
       then close any remaining open ones.

    Raises json.JSONDecodeError only when all strategies are exhausted.
    """
    cleaned = _strip_fences(raw)

    # 1 — happy path
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 2 — trim noise outside the JSON object
    extracted = _extract_json_object(cleaned)
    try:
        return json.loads(extracted)
    except json.JSONDecodeError:
        pass

    # 3 — json-repair (optional dependency)
    try:
        import json_repair  # type: ignore

        repaired = json_repair.repair_json(extracted)
        if repaired:
            result = json.loads(repaired) if isinstance(repaired, str) else repaired
            if isinstance(result, dict):
                return result
    except Exception:
        pass

    # 4 — brace-counting: find the last position where depth returned to 0
    stack: list[str] = []
    close_map = {"{": "}", "[": "]"}
    last_complete = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(extracted):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            stack.append(close_map[ch])
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()
                if not stack:
                    last_complete = i + 1

    if last_complete:
        try:
            return json.loads(extracted[:last_complete])
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError(
        f"All repair strategies failed ({len(raw)} chars). First 300: " + raw[:300],
        raw,
        0,
    )


def _build_adapter(data: dict, site: str, listings_url: str) -> SiteAdapter:
    parsed = urlparse(listings_url)
    data.setdefault("site", site)
    data.setdefault("base_url", f"{parsed.scheme}://{parsed.netloc}")
    data.setdefault("listings_url", listings_url)
    data.setdefault("requires_js", False)
    data.setdefault("page_languages", ["en"])
    data.setdefault("detail", None)
    data.setdefault("generated_at", datetime.utcnow().isoformat())
    # Ensure filters key exists
    data.setdefault("listings", {}).setdefault("filters", {"available": []})
    return SiteAdapter.model_validate(data)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


class SchemaGenerator:
    def __init__(self, primary: AIProvider, fallback: Optional[AIProvider] = None):
        self.primary = primary
        self.fallback = fallback

    def generate(self, site: str, listings_url: str, listings_html: str) -> SiteAdapter:
        """
        Single-phase generation. Returns a fully validated SiteAdapter.
        detail is None until Phase 2 is implemented.
        """
        system = _PHASE1_SYSTEM
        user = _phase1_user(site, listings_url, listings_html)

        raw = self._call(system, user, site)
        data = _repair_and_parse(raw)
        adapter = _build_adapter(data, site, listings_url)

        langs = adapter.page_languages
        n_filters = len(adapter.listings.filters.available)
        logger.info(
            "Schema generated  site=%s  nav=%s  pagination=%s  filters=%d  languages=%s",
            site,
            adapter.listings.navigation.type,
            adapter.listings.pagination.type,
            n_filters,
            langs,
        )
        return adapter

    def _call(self, system: str, user: str, site: str) -> str:
        providers = [p for p in (self.primary, self.fallback) if p is not None]
        last_error: Exception = RuntimeError("No providers configured")
        for provider in providers:
            try:
                logger.info("Generating schema  site=%s  provider=%s", site, provider.name)
                return provider.generate(system, user)
            except Exception as exc:
                logger.warning("%s failed for %s: %s", provider.name, site, exc)
                last_error = exc
        raise RuntimeError(f"All providers failed for {site}") from last_error
