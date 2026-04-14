"""
Orchestrator for job_applier.

Ties together: browser → CV parsing → page analysis → form filling →
submission → verification.

Handles multi-step / wizard forms automatically.

Public API
----------
apply(url, cv_path, ...)   →  SubmissionResult
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import dotenv

from .browser import create_driver
from .cv_parser import load_cv
from .form_filler import click_element, fill_form
from .models import CVData, FormMapping, SubmissionResult
from .page_analyzer import analyze_page, search_in_iframes
from .verifier import capture_state, verify_submission

log = logging.getLogger(__name__)

# How many "next" clicks to follow before giving up on multi-step forms
MAX_STEPS = 8


# ---------------------------------------------------------------------------
# Per-step helpers
# ---------------------------------------------------------------------------


def _get_mapping(driver, cv: CVData) -> Optional[FormMapping]:
    """
    Try to find a valid form mapping on the current page.
    Falls back to iframe search if the main frame yields nothing.
    """
    mapping = analyze_page(driver, cv)
    if mapping.field_mappings or mapping.cv_upload_selector:
        return mapping

    # Try iframes
    mapping = search_in_iframes(driver, cv)
    return mapping


def _has_useful_mapping(m: Optional[FormMapping]) -> bool:
    return m is not None and bool(m.field_mappings or m.cv_upload_selector)


# ---------------------------------------------------------------------------
# Main apply() function
# ---------------------------------------------------------------------------


def apply(
    url: str,
    cv_path: str,
    *,
    auto_submit: bool = False,
    headless: bool = True,
    wait_for_load: float = 10.0,
    generate_cover_letter: bool = False,
    job_description: str = "",
    verify: bool = True,
) -> SubmissionResult:
    """
    Open a job application page, fill in all fields from the CV, and optionally submit.

    Parameters
    ----------
    url : str
        Full URL of the job application page.
    cv_path : str
        Path to the applicant's CV (.pdf or .docx).
    auto_submit : bool
        If False (default), stop before clicking Submit and print a summary.
    headless : bool
        Run Chrome without a visible window.
    wait_for_load : float
        Seconds to wait after page/step load before analysing.
    generate_cover_letter : bool
        Auto-generate a cover letter from the CV.
    job_description : str
        Optional raw job description text to improve cover letter relevance.
    verify : bool
        If True (default), verify the submission was received after clicking Submit.

    Returns
    -------
    SubmissionResult
    """
    dotenv.load_dotenv()

    # ── CV ────────────────────────────────────────────────────────────────
    log.info("Loading CV: %s", cv_path)
    cv = load_cv(cv_path, generate_cover_letter_flag=generate_cover_letter)
    log.info("Applicant: %s <%s>", cv.full_name, cv.email)

    # ── Browser ───────────────────────────────────────────────────────────
    driver = create_driver(headless=headless)

    try:
        log.info("Navigating to %s", url)
        driver.get(url)
        time.sleep(wait_for_load)

        step = 0
        while step < MAX_STEPS:
            step += 1
            log.info("── Step %d ──────────────────────────", step)
            log.debug("Current URL before mapping: %s", driver.current_url)
            #log.debug("Page source before mapping: %s", driver.page_source[:500]) # Log first 500 chars

            mapping = _get_mapping(driver, cv)

            if not _has_useful_mapping(mapping):
                log.warning("No form fields found on step %d. Stopping.", step)
                break

            # Fill this step's fields
            fill_results = fill_form(driver, mapping, cv, cv_path=cv_path)
            _log_fill_summary(fill_results)

            # Determine next action
            if mapping.next_button_selector:
                # Multi-step: click Next / Continue
                log.info("Multi-step form — clicking Next: %s", mapping.next_button_selector)
                clicked = click_element(driver, mapping.next_button_selector)
                if not clicked:
                    log.error("Could not click Next button. Stopping.")
                    break
                time.sleep(wait_for_load)
                continue  # analyse the next step

            # Final step — Submit button
            if mapping.submit_selector:
                log.info("Submit button found: %s", mapping.submit_selector)

                if not auto_submit:
                    log.info(
                        "auto_submit=False — stopping before submission.\n"
                        "Run with auto_submit=True (or --yes flag) to submit."
                    )
                    return SubmissionResult(
                        success=False,
                        confidence=0.0,
                        evidence="auto_submit disabled — form was filled but not submitted",
                        final_url=driver.current_url,
                    )

                before = capture_state(driver)
                log.info("Clicking Submit…")
                clicked = click_element(driver, mapping.submit_selector)

                if not clicked:
                    log.error("Could not click Submit button.")
                    return SubmissionResult(
                        success=False,
                        confidence=0.0,
                        evidence="Submit button was found but could not be clicked",
                        final_url=driver.current_url,
                    )

                if verify:
                    result = verify_submission(driver, before)
                else:
                    time.sleep(3)
                    result = SubmissionResult(
                        success=True,
                        confidence=0.5,
                        evidence="Submission clicked; verification disabled",
                        final_url=driver.current_url,
                    )

                log.info("Result: %s", result)
                return result

            else:
                log.warning("No submit or next button found on step %d.", step)
                break

        return SubmissionResult(
            success=False,
            confidence=0.0,
            evidence=f"Could not complete form after {step} step(s).",
            final_url=driver.current_url,
        )

    finally:
        log.info("Closing browser.")
        driver.quit()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _log_fill_summary(results: dict) -> None:
    ok = [s for s, v in results.items() if v]
    fail = [s for s, v in results.items() if not v]
    log.info("  Filled: %d fields OK, %d failed", len(ok), len(fail))
    for s in fail:
        log.debug("  FAILED: %s", s)
