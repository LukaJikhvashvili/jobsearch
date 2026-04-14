"""
AI client abstraction for job_applier.

Supports:
  - Anthropic Claude  (recommended – better structured reasoning)
  - Google Gemini     (fallback / configurable)

Set AI_PROVIDER=anthropic (default) or AI_PROVIDER=gemini in your .env.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional
import dotenv

dotenv.load_dotenv()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_json_response(text: str) -> Dict[str, Any]:
    """Strip markdown fences and parse JSON, raising ValueError on failure."""
    text = text.strip()
    for fence in ("```json", "```"):
        if fence in text:
            text = text.split(fence, 1)[1]
            text = text.rsplit("```", 1)[0]
            text = text.strip()
            break
    return json.loads(text)


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


class _AnthropicProvider:
    """Thin wrapper around the Anthropic Messages API."""

    MODEL = "claude-sonnet-4-20250514"

    def __init__(self, api_key: str) -> None:
        import anthropic  # local import so Gemini-only users don't need it

        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2048) -> str:
        messages = [{"role": "user", "content": prompt}]
        kwargs: Dict[str, Any] = dict(
            model=self.MODEL,
            max_tokens=max_tokens,
            messages=messages,
        )
        if system:
            kwargs["system"] = system
        response = self._client.messages.create(**kwargs)
        return response.content[0].text

    def complete_json(self, prompt: str, *, system: str = "", max_tokens: int = 2048) -> Dict[str, Any]:
        json_hint = "Respond with ONLY valid JSON. No markdown fences, no extra text."
        text = self.complete(prompt + json_hint, system=system, max_tokens=max_tokens)
        return _parse_json_response(text)


class _GeminiProvider:
    """Thin wrapper around the Google GenAI SDK."""

    MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    def __init__(self, api_key: str) -> None:
        from google import genai  # local import
        from google.genai import types as gtypes

        self._client = genai.Client(api_key=api_key)
        self._types = gtypes

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2048) -> str:
        contents = prompt
        if system:
            contents = f"{system}{prompt}"
        response = self._client.models.generate_content(model=self.MODEL, contents=contents)
        return response.text

    def complete_json(self, prompt: str, *, system: str = "", max_tokens: int = 2048) -> Dict[str, Any]:
        contents = prompt
        if system:
            contents = f"{system}{prompt}"
        try:
            response = self._client.models.generate_content(
                model=self.MODEL,
                contents=contents,
                config=self._types.GenerateContentConfig(response_mime_type="application/json"),
            )
            return json.loads(response.text)
        except Exception:
            # fallback without JSON mode
            text = self.complete(prompt, system=system)
            return _parse_json_response(text)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

_PROVIDER: Optional[_AnthropicProvider | _GeminiProvider] = None


def get_ai_client() -> "_AnthropicProvider | _GeminiProvider":
    """Return a shared AI provider instance (lazy-initialised)."""
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER

    provider_name = os.getenv("AI_PROVIDER", "anthropic").lower()

    if provider_name == "gemini":
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            raise EnvironmentError("GEMINI_API_KEY is not set in environment.")
        _PROVIDER = _GeminiProvider(key)
        log.info("Using Gemini provider (%s)", _GeminiProvider.MODEL)
    else:
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is not set. " "Set it in .env, or switch to Gemini with AI_PROVIDER=gemini."
            )
        _PROVIDER = _AnthropicProvider(key)
        log.info("Using Anthropic Claude provider (%s)", _AnthropicProvider.MODEL)

    return _PROVIDER


def ai_complete(prompt: str, *, system: str = "", max_tokens: int = 2048) -> str:
    """Send a free-text prompt and return the model's response."""
    return get_ai_client().complete(prompt, system=system, max_tokens=max_tokens)


def ai_json(prompt: str, *, system: str = "", max_tokens: int = 2048) -> Dict[str, Any]:
    """Send a prompt and return parsed JSON from the model's response."""
    return get_ai_client().complete_json(prompt, system=system, max_tokens=max_tokens)
