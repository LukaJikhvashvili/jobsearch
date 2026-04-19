"""
AI-powered SiteAdapter generator.

Primary:  Gemini 2.0 Flash  (free tier, fast)
Fallback: Claude Haiku       (cheap, reliable)

Both providers receive the same structured prompt and are expected to
return a single JSON object conforming to the SiteAdapter schema.
"""

import json
import logging
import re
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from .models import SiteAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known ATS domains — used in the prompt so the model classifies correctly
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
# Prompt templates
# ---------------------------------------------------------------------------
_SCHEMA_REFERENCE = r"""
{
  "site":               "string  — domain, e.g. jobs.ge",
  "base_url":           "string  — origin, e.g. https://jobs.ge",
  "listings_url":       "string  — the listings page URL you analysed",
  "requires_js":        "boolean — true if page needs JS to render cards",
  "requires_auth":      "boolean",
  "overall_confidence": "float   0.0–1.0",
  "notes":              "string or null",
 
  "listings": {
    "container": "CSS selector matching each job card (the repeating element)",
    "fields": {
      "title":       { "selector": "CSS or null", "attr": "text", "confidence": 0.0–1.0 },
      "company":     { "selector": "CSS or null", "attr": "text", "confidence": 0.0–1.0 },
      "location":    { "selector": "CSS or null", "attr": "text", "confidence": 0.0–1.0 },
      "salary":      { "selector": "CSS or null", "attr": "text", "confidence": 0.0–1.0 },
      "posted_date": { "selector": "CSS or null", "attr": "text", "confidence": 0.0–1.0 }
    },
    "pagination": {
      "type":          "url_param | next_button | infinite_scroll | none",
      "param_name":    "string or null  — e.g. 'page' for ?page=2",
      "start_page":    1,
      "next_selector": "CSS selector for the Next button/link or null",
      "max_pages":     50,
      "delay_ms":      1200
    },
    "navigation": {
      "type":           "direct_link | card_click | button_click | data_attr",
      "link_selector":  "CSS selector (relative to container) for the <a> or <button> — null if card_click",
      "data_attribute": "attribute name holding the URL, e.g. data-href — only for data_attr type, else null",
      "click_container": "boolean — true only when type is card_click",
      "confidence":     0.0–1.0,
      "notes":          "brief explanation of how you determined the navigation pattern or null"
    }
  },
 
  "detail": {
    "fields": {
      "description":  { "selector": "CSS or null", "attr": "html", "confidence": 0.0–1.0 },
      "requirements": { "selector": "CSS or null", "attr": "html", "confidence": 0.0–1.0 },
      "salary":       { "selector": "CSS or null", "attr": "text", "confidence": 0.0–1.0 }
    },
    "application": {
      "method":                 "on_page_form | ats_redirect | external_link | email | unknown",
      "form_selector":          "CSS for the <form> element or null",
      "apply_button_selector":  "CSS for the primary apply CTA or null",
      "external_url_selector":  "CSS for the outbound apply <a> or null",
      "email_selector":         "CSS for the <a mailto:…> or null",
      "ats_domain":             "detected ATS domain string or null",
      "confidence":             0.0–1.0,
      "notes":                  "short explanation of method detection or null"
    }
  }
}
"""

_SYSTEM_PROMPT = f"""You are an expert web scraping engineer.
You will receive cleaned HTML from a job board — a listings page and one detail page.
Your job is to produce a JSON adapter that maps CSS selectors to every important field.
 
STRICT RULES:
1. Return ONLY a single valid JSON object. No markdown fences, no prose, no explanation.
2. If a field is absent from the page, set its selector to null. Never fabricate a selector.
3. URL fields (job links) MUST use attr "href". Rich text fields (descriptions) use attr "html".
4. Prefer stable class/id selectors. Avoid nth-child unless there is no other option.
5. For selectors that live INSIDE the job card container, write them relative to the container
   (i.e. omit the container prefix — the runner will call container.select(field_selector)).
6. The listings.fields block must NOT contain a "url" key. URL resolution is handled
   entirely by listings.navigation — do not duplicate it as a field.
 
PAGINATION CLASSIFICATION:
  url_param      — page changes via a query param (?page=2) or path segment (/page/2)
  next_button    — there is a clickable Next / › / >> element
  infinite_scroll — no visible pagination; content loads on scroll
  none           — single page, no pagination needed
 
NAVIGATION CLASSIFICATION (check the LISTINGS page — how each card links to its detail page):
  direct_link  — each card contains an <a href="…"> that goes directly to the detail page.
                 Set link_selector to the CSS of that <a> (relative to container).
  card_click   — the entire card is a clickable element driven by JS; there is no plain <a>.
                 Set click_container: true. link_selector should be null.
  button_click — there is a dedicated button or CTA inside the card (not wrapping the whole card).
                 Set link_selector to that button's CSS.
  data_attr    — the detail URL is stored in a data-* attribute on the card or a child element
                 (e.g. data-href, data-url, data-link). Set data_attribute to the attribute name
                 and link_selector to the element that carries it.
 
APPLICATION METHOD CLASSIFICATION (check the DETAIL page):
  on_page_form   — a <form> with name/email/resume fields is visible on the page itself
  ats_redirect   — any apply button/link points to a known ATS domain:
                   {", ".join(ATS_DOMAINS)}
  external_link  — apply button links to a different domain that is NOT a known ATS
  email          — application is via a mailto: link or a plaintext email address
  unknown        — cannot determine with confidence
 
CONFIDENCE SCORING:
  ≥ 0.90  very clear, unambiguous selector
  0.70–0.89  likely correct but may need verification
  < 0.70  uncertain — flag in notes
 
OUTPUT SCHEMA (fill every key; use null for missing values):
{_SCHEMA_REFERENCE}"""


def _build_user_prompt(site: str, listings_url: str, listings_html: str, detail_html: str) -> str:
    return (
        f"Site: {site}\n"
        f"Listings URL: {listings_url}\n\n"
        f"=== LISTINGS PAGE HTML ===\n{listings_html}\n\n"
        f"=== JOB DETAIL PAGE HTML ===\n{detail_html}\n\n"
        "Generate the adapter JSON now."
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
    Uses Gemini 3.1 Flash Lite (free tier).
    """

    def __init__(self, api_key: str, model: str = os.getenv("GEMINI_BASE_MODEL")):
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model_name = model

    def generate(self, system: str, user: str) -> str:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model_name,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
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
    """
    Claude Haiku — cheap and fast, used as fallback.
    """

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
# Output parsing & validation
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    """Remove ```json … ``` wrappers that some models add despite instructions."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_and_validate(raw: str, site: str, listings_url: str) -> SiteAdapter:
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)

    # Inject fields the model may have omitted
    data.setdefault("site", site)
    data.setdefault("base_url", f"{urlparse(listings_url).scheme}://{urlparse(listings_url).netloc}")
    data.setdefault("listings_url", listings_url)
    data.setdefault("generated_at", datetime.utcnow().isoformat())

    return SiteAdapter.model_validate(data)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


class SchemaGenerator:
    """
    Orchestrates one or more AI providers to generate a SiteAdapter.
    Tries the primary provider first; falls back to secondary on any error.
    """

    def __init__(self, primary: AIProvider, fallback: Optional[AIProvider] = None):
        self.primary = primary
        self.fallback = fallback

    def generate(self, site: str, listings_url: str, listings_html: str, detail_html: str) -> SiteAdapter:
        user_prompt = _build_user_prompt(site, listings_url, listings_html, detail_html)
        providers = [p for p in (self.primary, self.fallback) if p is not None]

        last_error: Exception = RuntimeError("No providers configured")
        for provider in providers:
            try:
                logger.info("Generating adapter for %s via %s …", site, provider.name)
                raw = provider.generate(_SYSTEM_PROMPT, user_prompt)
                adapter = _parse_and_validate(raw, site, listings_url)
                logger.info(
                    "Adapter ready  site=%s  confidence=%.2f  provider=%s",
                    site,
                    adapter.overall_confidence,
                    provider.name,
                )
                return adapter
            except Exception as exc:
                logger.warning("%s failed for %s: %s", provider.name, site, exc)
                last_error = exc

        raise RuntimeError(f"All providers failed for {site}") from last_error
