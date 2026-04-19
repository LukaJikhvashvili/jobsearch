"""
Two-phase AI-powered SiteAdapter generator.

Phase 1 — Listings analysis
    Input : cleaned listings page HTML
    Output: listings container, fields, pagination, navigation config
            (a partial SiteAdapter — no detail block yet)

Phase 2 — Detail analysis
    Input : the Phase 1 result + cleaned detail page HTML
    Output: detail fields + application config
            (merged into the final SiteAdapter)

AI providers
    Primary : Gemini (model read from GEMINI_MODEL env var, default gemini-2.0-flash)
    Fallback: Claude Haiku
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
# Known ATS domains
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
# Shared rules injected into both prompts
# ---------------------------------------------------------------------------
_SHARED_RULES = """STRICT RULES:
1. Return ONLY a single valid JSON object. No markdown fences, no prose.
2. If a field is absent, set selector to null. Never fabricate a selector.
3. Prefer class/id selectors. Avoid nth-child unless there is no alternative.
4. Selectors inside a card container must be RELATIVE to that container.

CONFIDENCE SCORING:
  >= 0.90  clear and unambiguous
  0.70-0.89  likely correct, may need verification
  < 0.70  uncertain — explain in notes"""

# ---------------------------------------------------------------------------
# Phase 1 — Listings prompt
# ---------------------------------------------------------------------------
_PHASE1_SYSTEM = f"""You are an expert web scraping engineer analysing a job listings page.
Your job is to produce a JSON object describing the listings page structure only.
You will NOT see the detail page yet — focus entirely on what is visible here.

{_SHARED_RULES}

PAGINATION TYPES:
  url_param       — URL query param increments  (?page=2, ?p=3)
  next_button     — a clickable Next / > / >> element exists
  infinite_scroll — no visible pagination; content loads on scroll
  none            — single page

NAVIGATION TYPES (how each job card links to its detail page):
  direct_link  — card contains an <a href="…"> pointing to the detail page
                 → set link_selector to that <a>'s CSS (relative to container)
  card_click   — the whole card navigates via JS onclick, no plain <a> tag
                 → set click_container: true, link_selector: null
  button_click — a specific button/CTA inside the card triggers navigation
                 → set link_selector to that button's CSS
  data_attr    — the detail URL lives in a data-* attribute (data-href, data-url …)
                 → set data_attribute to the attribute name, link_selector to
                   the element carrying it

OUTPUT SCHEMA (fill every key; null for absent values):
{{
  "site":         "domain string, e.g. jobs.ge",
  "base_url":     "origin, e.g. https://jobs.ge",
  "listings_url": "the URL of the listings page provided",
  "requires_js":  false,
  "listings": {{
    "container": "CSS selector for the repeating job card element",
    "fields": {{
      "title":       {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}},
      "company":     {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}},
      "location":    {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}},
      "salary":      {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}},
      "posted_date": {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}}
    }},
    "pagination": {{
      "type":          "url_param | next_button | infinite_scroll | none",
      "param_name":    "query param name or null",
      "start_page":    1,
      "next_selector": "CSS for Next button/link or null",
      "max_pages":     50,
      "delay_ms":      1200
    }},
    "navigation": {{
      "type":           "direct_link | card_click | button_click | data_attr",
      "link_selector":  "CSS relative to container, or null for card_click",
      "data_attribute": "attribute name or null",
      "click_container": false,
      "confidence":     1.0,
      "notes":          "brief explanation or null"
    }}
  }}
}}"""


def _phase1_user(site: str, listings_url: str, listings_html: str) -> str:
    return (
        f"Site: {site}\n"
        f"Listings URL: {listings_url}\n\n"
        f"=== LISTINGS PAGE HTML ===\n{listings_html}\n\n"
        "Analyse this listings page and return the Phase 1 JSON."
    )


# ---------------------------------------------------------------------------
# Phase 2 — Detail prompt
# ---------------------------------------------------------------------------
_PHASE2_SYSTEM = f"""You are an expert web scraping engineer analysing a job detail page.
You have already mapped the listings page. Now analyse ONE detail page to complete the adapter.

{_SHARED_RULES}

APPLICATION METHOD (examine all links, forms, and buttons on this page):
  on_page_form  — a <form> with resume/email/name fields is present on this page itself
  ats_redirect  — the apply button/link points to a known ATS:
                  {", ".join(ATS_DOMAINS)}
  external_link — apply button links to a non-ATS external domain
  email         — application is via a mailto: link or visible email address
  unknown       — cannot determine

OUTPUT SCHEMA (fill every key; null for absent values):
{{
  "detail": {{
    "fields": {{
      "description":  {{"selector": "CSS or null", "attr": "html", "confidence": 1.0}},
      "requirements": {{"selector": "CSS or null", "attr": "html", "confidence": 1.0}},
      "salary":       {{"selector": "CSS or null", "attr": "text", "confidence": 1.0}}
    }},
    "application": {{
      "method":                "on_page_form | ats_redirect | external_link | email | unknown",
      "form_selector":         "CSS for <form> element or null",
      "apply_button_selector": "CSS for primary apply CTA or null",
      "external_url_selector": "CSS for outbound apply <a> or null",
      "email_selector":        "CSS for <a mailto:…> or null",
      "ats_domain":            "detected ATS domain or null",
      "confidence":            1.0,
      "notes":                 "brief explanation or null"
    }}
  }},
  "requires_js":        false,
  "overall_confidence": 1.0,
  "notes":              "any important notes about this site or null"
}}"""


def _phase2_user(detail_url: str, detail_html: str) -> str:
    return (
        f"Detail page URL: {detail_url}\n\n"
        f"=== JOB DETAIL PAGE HTML ===\n{detail_html}\n\n"
        "Analyse this detail page and return the Phase 2 JSON."
    )


# ---------------------------------------------------------------------------
# AI provider abstraction
# ---------------------------------------------------------------------------


class AIProvider(ABC):
    @abstractmethod
    def generate(self, system: str, user: str) -> str: ...

    @property
    def name(self) -> str:
        return self.__class__.__name__


class GeminiProvider(AIProvider):
    """
    Uses the new `google-genai` SDK (from google import genai).
    Model is read from the GEMINI_MODEL env var; falls back to gemini-2.0-flash.
    """

    def __init__(self, api_key: str):
        from google import genai
        from google.genai import types

        model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
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
    """Claude Haiku — fallback provider."""

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
# JSON parsing helpers
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse(raw: str) -> dict:
    return json.loads(_strip_fences(raw))


# ---------------------------------------------------------------------------
# Phase result models (plain dicts before full validation)
# ---------------------------------------------------------------------------


def _build_partial_from_phase1(data: dict, site: str, listings_url: str) -> dict:
    """Normalise and backfill Phase 1 output."""
    parsed = urlparse(listings_url)
    data.setdefault("site", site)
    data.setdefault("base_url", f"{parsed.scheme}://{parsed.netloc}")
    data.setdefault("listings_url", listings_url)
    data.setdefault("requires_js", False)
    return data


def _merge_phase2(partial: dict, phase2: dict) -> dict:
    """Merge Phase 2 output into the partial dict to produce a full adapter."""
    partial["detail"] = phase2["detail"]
    # Phase 2 may refine requires_js (e.g. detail page needs JS but listings didn't)
    partial["requires_js"] = partial.get("requires_js") or phase2.get("requires_js", False)
    partial["overall_confidence"] = phase2.get("overall_confidence", 0.8)
    if phase2.get("notes"):
        existing = partial.get("notes") or ""
        partial["notes"] = (existing + " | " + phase2["notes"]).strip(" |")
    partial["generated_at"] = datetime.utcnow().isoformat()
    return partial


# ---------------------------------------------------------------------------
# SchemaGenerator — public interface
# ---------------------------------------------------------------------------


class SchemaGenerator:
    """
    Two-phase SiteAdapter generator.

    Phase 1: LLM analyses listings HTML → returns listings config + navigation
    Phase 2: caller navigates to a real detail page using Phase 1 navigation,
             then calls generate_phase2() with that HTML → returns full adapter

    Typical usage (orchestrated by the Profiler):

        gen = SchemaGenerator(primary=GeminiProvider(api_key), fallback=...)

        partial = gen.generate_phase1(site, listings_url, listings_html)
        # → partial is a dict (not yet a full SiteAdapter)

        detail_url, detail_html = await profiler.navigate_to_detail(partial)

        adapter = gen.generate_phase2(partial, detail_url, detail_html)
        # → full SiteAdapter, ready to save
    """

    def __init__(self, primary: AIProvider, fallback: Optional[AIProvider] = None):
        self.primary = primary
        self.fallback = fallback

    # ------------------------------------------------------------------ Phase 1

    def generate_phase1(self, site: str, listings_url: str, listings_html: str) -> dict:
        """
        Analyse the listings page.
        Returns a plain dict (partial adapter) — NOT yet a SiteAdapter.
        The caller must navigate to a detail page and call generate_phase2().
        """
        system = _PHASE1_SYSTEM
        user = _phase1_user(site, listings_url, listings_html)

        raw = self._call(system, user, site, phase=1)
        data = _parse(raw)
        partial = _build_partial_from_phase1(data, site, listings_url)

        logger.info(
            "Phase 1 complete  site=%s  nav_type=%s  pagination=%s",
            site,
            partial.get("listings", {}).get("navigation", {}).get("type", "?"),
            partial.get("listings", {}).get("pagination", {}).get("type", "?"),
        )
        return partial

    # ------------------------------------------------------------------ Phase 2

    def generate_phase2(self, partial: dict, detail_url: str, detail_html: str) -> SiteAdapter:
        """
        Analyse the detail page and merge into the partial adapter.
        Returns a validated SiteAdapter.
        """
        site = partial.get("site", detail_url)

        system = _PHASE2_SYSTEM
        user = _phase2_user(detail_url, detail_html)

        raw = self._call(system, user, site, phase=2)
        phase2_data = _parse(raw)

        merged = _merge_phase2(partial, phase2_data)
        adapter = SiteAdapter.model_validate(merged)

        logger.info(
            "Phase 2 complete  site=%s  apply=%s  confidence=%.2f",
            site,
            adapter.detail.application.method,
            adapter.overall_confidence,
        )
        return adapter

    # ------------------------------------------------------------------ internals

    def _call(self, system: str, user: str, site: str, phase: int) -> str:
        providers = [p for p in (self.primary, self.fallback) if p is not None]
        last_error: Exception = RuntimeError("No providers configured")

        for provider in providers:
            try:
                logger.info("Phase %d  site=%s  provider=%s", phase, site, provider.name)
                return provider.generate(system, user)
            except Exception as exc:
                logger.warning("Phase %d  %s failed for %s: %s", phase, provider.name, site, exc)
                last_error = exc

        raise RuntimeError(f"All providers failed (phase {phase}, site {site})") from last_error
