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

# ---------------------------------------------------------------------------
_SHARED_RULES = """STRICT RULES:
1. Return ONLY a single valid JSON object. No markdown fences, no prose.
2. If something is absent from the page, set its selector/value to null.
3. Prefer class/id selectors. Avoid nth-child unless unavoidable.
4. Selectors inside a card container must be RELATIVE to that container.

CONFIDENCE SCORING:
  >= 0.90  clear and unambiguous
  0.70-0.89  likely correct, may need verification
  < 0.70  uncertain — explain in notes"""

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

IMPORTANT: if you see a "next" button, arrow, or translated equivalent
("შემდეგი", "siguiente", "suivant", "далее" etc.) → use next_button.
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
  "overall_confidence": 1.0,
  "notes":            null,
  "listings": {{
    "container": "CSS for repeating job card",
    "fields": {{
      "title":   {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}},
      "company": {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}}
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
      "confidence":      1.0,
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
          "confidence":         1.0,
          "notes":              null
        }}
      ],
      "submit_selector": null,
      "notes": null
    }}
  }}
}}"""


def _phase1_user(site: str, listings_url: str, listings_html: str, pagination_html: str = "") -> str:
    pagination_section = (
        f"\n=== PAGINATION AREA HTML (bottom of page, post-JS render) ===\n{pagination_html}\n" if pagination_html else ""
    )
    return (
        f"Site: {site}\n"
        f"Listings URL: {listings_url}\n\n"
        f"=== SAMPLE JOB CARDS HTML (post-JS render, 3 cards) ===\n{listings_html}\n"
        f"{pagination_section}\n"
        "NOTE: This HTML is the RENDERED DOM after JavaScript execution. "
        "onclick attrs, data-* attrs, and dynamic content are all present. "
        "Use them to correctly classify navigation type.\n\n"
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
                max_output_tokens=4096,
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
            max_tokens=4096,
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


def _parse(raw: str) -> dict:
    return json.loads(_strip_fences(raw))


def _build_adapter(data: dict, site: str, listings_url: str) -> SiteAdapter:
    parsed = urlparse(listings_url)
    data.setdefault("site", site)
    data.setdefault("base_url", f"{parsed.scheme}://{parsed.netloc}")
    data.setdefault("listings_url", listings_url)
    data.setdefault("requires_js", False)
    data.setdefault("page_languages", ["en"])
    data.setdefault("overall_confidence", 0.85)
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

    def generate(self, site: str, listings_url: str, listings_html: str, pagination_html: str = "") -> SiteAdapter:
        """
        Single-phase generation. Returns a fully validated SiteAdapter.
        detail is None until Phase 2 is implemented.
        """
        system = _PHASE1_SYSTEM
        user = _phase1_user(site, listings_url, listings_html, pagination_html)

        raw = self._call(system, user, site)
        data = _parse(raw)
        adapter = _build_adapter(data, site, listings_url)

        langs = adapter.page_languages
        n_filters = len(adapter.listings.filters.available)
        logger.info(
            "Schema generated  site=%s  nav=%s  pagination=%s  filters=%d  languages=%s  confidence=%.2f",
            site,
            adapter.listings.navigation.type,
            adapter.listings.pagination.type,
            n_filters,
            langs,
            adapter.overall_confidence,
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
