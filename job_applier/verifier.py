"""
Submission verifier for job_applier.

Determines whether a form submission was actually successful by comparing
page state before and after the click, then asking the AI to judge the result.

Public API
----------
capture_state(driver)                      →  PageState
verify_submission(driver, before, after)   →  SubmissionResult
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from selenium.webdriver.common.by import By

from .ai_client import ai_json
from .models import SubmissionResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

SUCCESS_KEYWORDS = [
    "thank you",
    "thanks",
    "application received",
    "application submitted",
    "successfully submitted",
    "we have received",
    "we'll be in touch",
    "we will be in touch",
    "confirmation",
    "submitted successfully",
    "your application",
    "application complete",
    "check your email",
    "application sent",
    "köszönjük",  # Hungarian – common ATS
]

ERROR_KEYWORDS = [
    "error",
    "required",
    "invalid",
    "please fill",
    "cannot be blank",
    "must be",
    "field is required",
    "something went wrong",
    "failed",
    "not submitted",
]

# ---------------------------------------------------------------------------
# Page state capture
# ---------------------------------------------------------------------------


@dataclass
class PageState:
    url: str = ""
    title: str = ""
    body_text: str = ""
    visible_headings: List[str] = field(default_factory=list)
    alert_texts: List[str] = field(default_factory=list)
    has_form: bool = False

    def success_score(self) -> float:
        """Heuristic 0-1 score based on keyword presence."""
        text = (self.body_text + " " + " ".join(self.visible_headings)).lower()
        hits = sum(1 for kw in SUCCESS_KEYWORDS if kw in text)
        err_hits = sum(1 for kw in ERROR_KEYWORDS if kw in text)
        score = min(hits * 0.25, 1.0) - min(err_hits * 0.15, 0.5)
        return max(0.0, score)


def capture_state(driver: Any) -> PageState:
    """Capture the current observable page state."""
    state = PageState()
    try:
        state.url = driver.current_url
        state.title = driver.title or ""
        # Visible body text (first 3000 chars)
        body_el = driver.find_elements(By.TAG_NAME, "body")
        if body_el:
            state.body_text = body_el[0].text[:3000]
        # Headings
        for tag in ("h1", "h2", "h3"):
            els = driver.find_elements(By.TAG_NAME, tag)
            state.visible_headings += [e.text.strip() for e in els if e.text.strip()]
        # Alert / toast messages
        for sel in (".alert", ".toast", "[role='alert']", ".notification", ".snackbar"):
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            state.alert_texts += [e.text.strip() for e in els if e.text.strip()]
        # Form still present?
        state.has_form = bool(driver.find_elements(By.CSS_SELECTOR, "form, input, textarea"))
    except Exception as exc:
        log.debug("capture_state error: %s", exc)
    return state


# ---------------------------------------------------------------------------
# AI verification
# ---------------------------------------------------------------------------

_VERIFY_SYSTEM = """You are an expert at determining whether a web form submission succeeded.
Analyse the before/after page snapshots and return a JSON verdict."""

_VERIFY_SCHEMA = """
{
  "success": true | false,
  "confidence": 0.0-1.0,
  "evidence": "<one sentence explanation>",
  "error_messages": ["<any visible error text>"]
}
"""


def _ai_verify(before: PageState, after: PageState) -> Optional[SubmissionResult]:
    prompt = f"""A job application form was submitted. Determine if it was successful.

BEFORE SUBMISSION:
  URL: {before.url}
  Title: {before.title}
  Has form: {before.has_form}
  Headings: {before.visible_headings[:5]}
  Alerts: {before.alert_texts[:3]}
  Body snippet: {before.body_text[:400]}

AFTER SUBMISSION:
  URL: {after.url}
  Title: {after.title}
  Has form: {after.has_form}
  Headings: {after.visible_headings[:5]}
  Alerts: {after.alert_texts[:3]}
  Body snippet: {after.body_text[:600]}

Respond with this JSON schema (no extra text):
{_VERIFY_SCHEMA}

Indicators of SUCCESS: URL changed to confirmation page, "thank you" or "received"
message appeared, form disappeared, confirmation email mentioned.

Indicators of FAILURE: Same URL with form still visible, error/required field messages,
validation highlights, no visible change.
"""
    try:
        raw = ai_json(prompt, system=_VERIFY_SYSTEM, max_tokens=512)
        return SubmissionResult(
            success=bool(raw.get("success", False)),
            confidence=float(raw.get("confidence", 0.0)),
            evidence=raw.get("evidence", ""),
            final_url=after.url,
            error_messages=raw.get("error_messages", []),
        )
    except Exception as exc:
        log.warning("AI verification failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Heuristic verification (no AI)
# ---------------------------------------------------------------------------


def _heuristic_verify(before: PageState, after: PageState) -> SubmissionResult:
    url_changed = before.url != after.url
    form_gone = before.has_form and not after.has_form
    score = after.success_score()

    success = bool(url_changed or form_gone or score >= 0.5)
    confidence = min(0.3 + score + (0.2 if url_changed else 0) + (0.2 if form_gone else 0), 1.0)

    text = (after.body_text + " " + " ".join(after.visible_headings)).lower()
    errors = [kw for kw in ERROR_KEYWORDS if kw in text]

    return SubmissionResult(
        success=success,
        confidence=round(confidence, 2),
        evidence=(f"URL changed={url_changed}, form gone={form_gone}, " f"success keywords hit={round(score/0.25)}"),
        final_url=after.url,
        error_messages=errors,
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def verify_submission(
    driver: Any,
    before: PageState,
    wait_seconds: float = 4.0,
    use_ai: bool = True,
) -> SubmissionResult:
    """
    Capture the post-submission page state and determine whether the
    application was sent successfully.

    Parameters
    ----------
    driver : WebDriver
        Browser session after the submit click.
    before : PageState
        State captured before the submit click (from capture_state()).
    wait_seconds : float
        How long to wait for the page to settle before capturing state.
    use_ai : bool
        If True, use the AI for a richer verdict; else use heuristics only.

    Returns
    -------
    SubmissionResult
    """
    log.info("Waiting %.1fs for post-submission page…", wait_seconds)
    time.sleep(wait_seconds)

    after = capture_state(driver)

    result: Optional[SubmissionResult] = None
    if use_ai:
        result = _ai_verify(before, after)

    if result is None:
        result = _heuristic_verify(before, after)

    log.info("Submission verdict: %s", result)
    return result
