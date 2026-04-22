"""
Two-phase AI-powered SiteAdapter generator.

Phase 1 — Listings page analysis
    Discovers: card container, title+company selectors, pagination,
               navigation to detail, and all available filter controls.

Phase 2 — Detail page analysis
    Discovers: location, salary, posted_date, description, requirements,
               and application method.
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
ATS_DOMAINS = [
    "greenhouse.io",
    "lever.co",
    "workday.com",
    "bamboohr.com",
    "icims.com",
    "taleo.net",
    "smartrecruiters.com",
    "jobvite.com",
    "recruitee.com",
    "workable.com",
    "breezy.hr",
    "ashbyhq.com",
    "applytojob.com",
    "successfactors.com",
    "myworkdayjobs.com",
    "careers-page.com",
    "jazz.co",
    "rippling.com",
]

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

_PHASE1_SYSTEM = f"""You are an expert web scraping engineer analysing a job listings page.
Extract ONLY what is visible on the listings page. Do not invent detail-page fields.

{_SHARED_RULES}

─── LISTINGS FIELDS ────────────────────────────────────────────────────────
Extract ONLY these two fields from each job card:
  title   — the job title / position name
  company — the employer / company name

DO NOT attempt to extract location, salary, posted_date or description from
the listings page. Those live on the detail page and will be handled in Phase 2.

─── NAVIGATION TYPES ───────────────────────────────────────────────────────
How each card links to its detail page:
  direct_link  — card has <a href="…"> pointing directly to detail page
                 → link_selector: CSS of that <a> (relative to container)
  card_click   — whole card navigates via JS onclick, no plain <a>
                 → click_container: true, link_selector: null
  button_click — a specific button/CTA inside the card triggers navigation
                 → link_selector: that button's CSS
  data_attr    — URL stored in a data-* attribute (data-href, data-url …)
                 → data_attribute: the attribute name

─── PAGINATION TYPES ───────────────────────────────────────────────────────
  url_param       — ?page=2 or /page/2 in the URL
  next_button     — clickable Next / > / >> element
  infinite_scroll — content loads on scroll, no visible page numbers
  none            — single page

─── FILTER MECHANISMS ──────────────────────────────────────────────────────
Carefully scan for ANY filtering UI on the page and classify each one:

  url_param      — filter applied via URL query parameter
                   → param_name: the query key (e.g. "q", "location", "cat")
  search_field   — <input type="text|search"> for keyword/title search
                   → selector: the input element
  dropdown       — <select> element
                   → selector: the <select>
                   → options_selector: selector for its <option> elements
                     (usually omit — defaults to selector + " option")
  checkbox_group — group of checkboxes for multi-select (categories, tags)
                   → selector: the wrapping container
                   → item_selector: CSS for each individual checkbox input
  tag_filter     — clickable pill / chip / button group
                   → selector: the wrapping container
                   → item_selector: CSS for each pill/chip
  radio_group    — mutually exclusive radio buttons
                   → selector: the wrapping container
                   → item_selector: CSS for each radio input
  date_range     — from-date and to-date inputs
                   → date_from_selector / date_to_selector

FILTER DIMENSIONS (what each filter controls):
  keyword     — job title or keyword search
  location    — city, region, country
  category    — job category, industry, department
  salary      — salary range or minimum
  date_posted — recency (today / this week / this month …)

For EVERY filter found, determine its dimension and mechanism and add it to
filters.available. If multiple mechanisms exist for the same dimension
(e.g. both a search field AND a URL param for keyword), include both.
If a Submit / Search button is needed after DOM interaction, set submit_selector.

OUTPUT SCHEMA — fill every key, null for absent:
{{
  "site":         "domain, e.g. jobs.ge",
  "base_url":     "origin URL",
  "listings_url": "the URL provided",
  "requires_js":  false,
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
          "param_name":         "query key or null",
          "selector":           "CSS or null",
          "options_selector":   "CSS or null",
          "item_selector":      "CSS or null",
          "date_from_selector": "CSS or null",
          "date_to_selector":   "CSS or null",
          "confidence":         1.0,
          "notes":              "null"
        }}
      ],
      "submit_selector": "CSS for Search/Apply button, or null",
      "notes": "null"
    }}
  }}
}}"""


# NEW
def _phase1_user(site: str, listings_url: str, listings_html: str, pagination_html: str = "") -> str:
    pagination_section = f"\n=== PAGINATION AREA HTML (bottom of page) ===\n{pagination_html}\n" if pagination_html else ""
    return (
        f"Site: {site}\n"
        f"Listings URL: {listings_url}\n\n"
        f"=== SAMPLE JOB CARDS HTML (3 cards, post-JS render) ===\n{listings_html}\n"
        f"{pagination_section}\n"
        "IMPORTANT: The HTML above is the RENDERED DOM captured by a real browser after "
        "JavaScript execution. onclick attributes, data-* attributes, and dynamically "
        "inserted content are all present. Use them to determine navigation type.\n\n"
        "Analyse this listings page and return the Phase 1 JSON."
    )


# ---------------------------------------------------------------------------
# Phase 2 — Detail prompt
# ---------------------------------------------------------------------------

_PHASE2_SYSTEM = f"""You are an expert web scraping engineer analysing a single job detail page.
The listings page has already been mapped. Now extract the rich detail fields.

{_SHARED_RULES}

─── DETAIL FIELDS TO EXTRACT ───────────────────────────────────────────────
  location    — city, region, remote status
  salary      — salary range or description
  posted_date — when the job was posted
  description — full job description  (use attr "html" to preserve formatting)
  requirements — required skills/qualifications (use attr "html")

─── APPLICATION METHOD ─────────────────────────────────────────────────────
  on_page_form  — <form> with name/email/resume fields is present on THIS page
  ats_redirect  — apply button/link points to a known ATS domain:
                  {", ".join(ATS_DOMAINS)}
  external_link — apply button links to a non-ATS external domain
  email         — application via mailto: link or visible email address
  unknown       — cannot determine

OUTPUT SCHEMA — fill every key, null for absent:
{{
  "detail": {{
    "fields": {{
      "location":     {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}},
      "salary":       {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}},
      "posted_date":  {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}},
      "description":  {{"selector": "CSS or null", "attr": "html", "confidence": 1.0}},
      "requirements": {{"selector": "CSS or null", "attr": "html", "confidence": 1.0}}
    }},
    "application": {{
      "method":                "on_page_form | ats_redirect | external_link | email | unknown",
      "form_selector":         "CSS or null",
      "apply_button_selector": "CSS or null",
      "external_url_selector": "CSS or null",
      "email_selector":        "CSS or null",
      "ats_domain":            "domain string or null",
      "confidence":            1.0,
      "notes":                 "null"
    }}
  }},
  "requires_js":        false,
  "overall_confidence": 1.0,
  "notes":              "null"
}}"""


def _phase2_user(detail_url: str, detail_html: str) -> str:
    return (
        f"Detail page URL: {detail_url}\n\n"
        f"=== JOB DETAIL PAGE HTML ===\n{detail_html}\n\n"
        "Analyse this detail page and return the Phase 2 JSON."
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
    """New google-genai SDK. Model read from GEMINI_MODEL env var."""

    def __init__(self, api_key: str):
        from google import genai
        from google.genai import types

        model = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
        self._model_name = model
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
    """Claude Haiku — fallback."""

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
# JSON helpers
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse(raw: str) -> dict:
    stripped = _strip_fences(raw)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        logger.warning("JSON parse failed, attempting recovery: %s", e)
        # Simple recovery for truncated LLM output
        if not stripped.endswith("}"):
            logger.info("Attempting to close unclosed JSON object")
            stripped += "}"
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            logger.error("Failed to parse JSON even after recovery:\n%s", stripped)
            raise


def _build_partial_from_phase1(data: dict, site: str, listings_url: str) -> dict:
    parsed = urlparse(listings_url)
    data.setdefault("site", site)
    data.setdefault("base_url", f"{parsed.scheme}://{parsed.netloc}")
    data.setdefault("listings_url", listings_url)
    data.setdefault("requires_js", False)
    # Ensure filters key exists even if LLM omitted it
    data.setdefault("listings", {}).setdefault("filters", {"available": []})
    return data


def _merge_phase2(partial: dict, phase2: dict) -> dict:
    partial["detail"] = phase2["detail"]
    partial["requires_js"] = partial.get("requires_js") or phase2.get("requires_js", False)
    partial["overall_confidence"] = phase2.get("overall_confidence", 0.8)
    if phase2.get("notes"):
        existing = partial.get("notes") or ""
        partial["notes"] = (existing + " | " + phase2["notes"]).strip(" |")
    partial["generated_at"] = datetime.utcnow().isoformat()
    return partial


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


class SchemaGenerator:
    def __init__(self, primary: AIProvider, fallback: Optional[AIProvider] = None):
        self.primary = primary
        self.fallback = fallback

    # NEW
    def generate_phase1(self, site: str, listings_url: str, listings_html: str, pagination_html: str = "") -> dict:
        raw = self._call(_PHASE1_SYSTEM, _phase1_user(site, listings_url, listings_html, pagination_html), site, 1)
        data = _parse(raw)
        partial = _build_partial_from_phase1(data, site, listings_url)
        filters = partial.get("listings", {}).get("filters", {})
        n_filters = len(filters.get("available", []))
        logger.info(
            "Phase 1  site=%s  nav=%s  pagination=%s  filters=%d",
            site,
            partial.get("listings", {}).get("navigation", {}).get("type", "?"),
            partial.get("listings", {}).get("pagination", {}).get("type", "?"),
            n_filters,
        )
        return partial

    def generate_phase2(self, partial: dict, detail_url: str, detail_html: str) -> SiteAdapter:
        site = partial.get("site", detail_url)
        raw = self._call(_PHASE2_SYSTEM, _phase2_user(detail_url, detail_html), site, 2)
        phase2_data = _parse(raw)
        merged = _merge_phase2(partial, phase2_data)
        adapter = SiteAdapter.model_validate(merged)
        logger.info(
            "Phase 2  site=%s  apply=%s  confidence=%.2f",
            site,
            adapter.detail.application.method,
            adapter.overall_confidence,
        )
        return adapter

    def _call(self, system: str, user: str, site: str, phase: int) -> str:
        providers = [p for p in (self.primary, self.fallback) if p is not None]
        last_error: Exception = RuntimeError("No providers configured")
        for provider in providers:
            try:
                logger.info("Phase %d  site=%s  provider=%s", phase, site, provider.name)
                return provider.generate(system, user)
            except Exception as exc:
                logger.warning("Phase %d  %s failed: %s", phase, provider.name, exc)
                last_error = exc
        raise RuntimeError(f"All providers failed (phase {phase}, {site})") from last_error
