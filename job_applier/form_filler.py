"""
Form filler for job_applier.

Handles: text/email/tel/url inputs, textareas, <select> dropdowns,
radio buttons, checkboxes, and file uploads.

All interactions use explicit waits + JavaScript fallbacks and retry
on StaleElementReferenceException.

Public API
----------
fill_form(driver, mapping, cv, cv_path)  →  Dict[str, bool]   (selector → success)
click_element(driver, selector)          →  bool
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from selenium.common.exceptions import (
    ElementNotInteractableException,
    StaleElementReferenceException,
    NoSuchElementException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from .models import CVData, FormMapping

log = logging.getLogger(__name__)

_WAIT_TIMEOUT = 10
_RETRY_LIMIT = 3


# ---------------------------------------------------------------------------
# Low-level element helpers
# ---------------------------------------------------------------------------


def _find(driver: Any, selector: str, timeout: int = _WAIT_TIMEOUT) -> Optional[Any]:
    """
    Locate an element by CSS selector with an explicit wait.
    Returns the element or None if not found / timed-out.
    """
    try:
        return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
    except (TimeoutException, NoSuchElementException):
        pass
    # fallback: direct find
    try:
        return driver.find_element(By.CSS_SELECTOR, selector)
    except Exception:
        return None


def _scroll_into_view(driver: Any, el: Any) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.15)


def _safe_click(driver: Any, el: Any) -> bool:
    """Try a normal click, then JS click as fallback."""
    _scroll_into_view(driver, el)
    try:
        el.click()
        return True
    except Exception:
        pass
    try:
        driver.execute_script("arguments[0].click();", el)
        return True
    except Exception as exc:
        log.debug("Click failed: %s", exc)
        return False


def _js_set_value(driver: Any, el: Any, value: str) -> None:
    """Set an input's value via JavaScript (bypasses readonly / React controlled)."""
    driver.execute_script(
        """
        var el = arguments[0], val = arguments[1];
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeInputValueSetter.call(el, val);
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        """,
        el,
        value,
    )


# ---------------------------------------------------------------------------
# Field-type handlers
# ---------------------------------------------------------------------------


def _fill_text(driver: Any, el: Any, value: str) -> bool:
    """Fill a plain text / email / tel / url / textarea input."""
    _scroll_into_view(driver, el)
    try:
        el.click()
    except Exception:
        pass
    try:
        el.clear()
        el.send_keys(value)
        return True
    except ElementNotInteractableException:
        pass
    # JS fallback for React / Angular controlled inputs
    try:
        _js_set_value(driver, el, value)
        return True
    except Exception as exc:
        log.debug("Text fill JS fallback failed: %s", exc)
        return False


def _fill_select(driver: Any, el: Any, value: str) -> bool:
    """Select an option from a <select> element by visible text or value."""
    sel = Select(el)
    # Try exact visible text
    try:
        sel.select_by_visible_text(value)
        return True
    except Exception:
        pass
    # Try value attribute
    try:
        sel.select_by_value(value)
        return True
    except Exception:
        pass
    # Try partial/lowercase match
    for opt in sel.options:
        if value.lower() in opt.text.lower() or opt.get_attribute("value", "").lower() in value.lower():
            try:
                sel.select_by_visible_text(opt.text)
                return True
            except Exception:
                pass
    log.debug("select: no option matched %r", value)
    return False


def _fill_radio(driver: Any, name: str, value: str) -> bool:
    """Select a radio button whose value/label matches."""
    radios = driver.find_elements(By.CSS_SELECTOR, f'input[type="radio"][name="{name}"]')
    if not radios:
        return False
    for r in radios:
        rv = (r.get_attribute("value") or "").lower()
        if value.lower() in rv or rv in value.lower():
            return _safe_click(driver, r)
    # fallback: select first
    return _safe_click(driver, radios[0])


def _fill_checkbox(driver: Any, el: Any, desired: bool = True) -> bool:
    """Ensure a checkbox is in the desired checked state."""
    current = el.is_selected()
    if current != desired:
        return _safe_click(driver, el)
    return True


# ---------------------------------------------------------------------------
# Main fill_field dispatcher
# ---------------------------------------------------------------------------


def fill_field(driver: Any, selector: str, value: str) -> bool:
    """
    Fill a single form field identified by CSS selector.

    Handles: text, email, tel, url, number, textarea, select, radio, checkbox.
    Retries up to _RETRY_LIMIT times on StaleElementReferenceException.

    Returns True if the field was filled successfully.
    """
    for attempt in range(_RETRY_LIMIT):
        el = _find(driver, selector)
        if el is None:
            log.warning("fill_field: element not found — %s", selector)
            return False
        try:
            tag = el.tag_name.lower()
            ftype = (el.get_attribute("type") or tag).lower()

            if tag == "select":
                return _fill_select(driver, el, value)

            if ftype == "checkbox":
                # Interpret truthy strings
                desired = value.lower() not in ("false", "0", "no", "")
                return _fill_checkbox(driver, el, desired)

            if ftype == "radio":
                name = el.get_attribute("name") or ""
                return _fill_radio(driver, name, value)

            if ftype == "file":
                # File inputs are handled separately
                return False

            # All text-like inputs
            return _fill_text(driver, el, value)

        except StaleElementReferenceException:
            log.debug("StaleElementReference on %s (attempt %d)", selector, attempt + 1)
            time.sleep(0.5)

    log.warning("fill_field: gave up after %d attempts — %s", _RETRY_LIMIT, selector)
    return False


# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------


def upload_file(driver: Any, selector: str, file_path: str) -> bool:
    """
    Send an absolute file path to a file input element.

    Parameters
    ----------
    driver : WebDriver
    selector : str
        CSS selector for the file input.
    file_path : str
        Absolute path to the file (resolved before sending).

    Returns
    -------
    bool
        True on success.
    """
    abs_path = str(Path(file_path).resolve())
    el = _find(driver, selector)
    if el is None:
        log.warning("upload_file: file input not found — %s", selector)
        return False

    try:
        # Make file inputs interactable in headless mode
        driver.execute_script("arguments[0].style.display='block';", el)
        driver.execute_script("arguments[0].style.visibility='visible';", el)
        el.send_keys(abs_path)
        log.info("Uploaded %s → %s", abs_path, selector)
        return True
    except Exception as exc:
        log.error("File upload failed (%s): %s", selector, exc)
        return False


# ---------------------------------------------------------------------------
# Click helper
# ---------------------------------------------------------------------------


def click_element(driver: Any, selector: str, timeout: int = _WAIT_TIMEOUT) -> bool:
    """
    Reliably click an element identified by CSS selector.

    Returns True on success.
    """
    try:
        el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
    except TimeoutException:
        el = _find(driver, selector)
        if el is None:
            log.warning("click_element: not found — %s", selector)
            return False

    return _safe_click(driver, el)


# ---------------------------------------------------------------------------
# fill_form — orchestrates all field interactions
# ---------------------------------------------------------------------------


def fill_form(
    driver: Any,
    mapping: FormMapping,
    cv: CVData,
    cv_path: Optional[str] = None,
) -> Dict[str, bool]:
    """
    Fill all mapped form fields, upload the CV, and tick agreement checkboxes.

    Parameters
    ----------
    driver : WebDriver
    mapping : FormMapping
        Output of page_analyzer.map_fields().
    cv : CVData
        Applicant data.
    cv_path : str, optional
        Path to the CV file for upload. Required if mapping.cv_upload_selector is set.

    Returns
    -------
    Dict[str, bool]
        Mapping of selector → success flag for every attempted interaction.
    """
    results: Dict[str, bool] = {}
    cv_dict = cv.to_dict()

    # 1. Fill named fields from CV data
    for field_key, selector in mapping.field_mappings.items():
        value = cv_dict.get(field_key, "")
        if not value:
            log.debug("Skipping empty CV field: %s", field_key)
            continue
        log.info("  Filling %-30s → %s", field_key, selector)
        ok = fill_field(driver, selector, value)
        results[selector] = ok
        if ok:
            time.sleep(0.1)  # brief pause to avoid triggering rate limits / animations

    # 2. Fill AI-generated values (fields not in CV)
    for selector, value in mapping.generated_values.items():
        if not value:
            continue
        log.info("  Filling (generated) %s", selector)
        ok = fill_field(driver, selector, value)
        results[selector] = ok

    # 3. Upload CV file
    if mapping.cv_upload_selector:
        if cv_path:
            ok = upload_file(driver, mapping.cv_upload_selector, cv_path)
            results[mapping.cv_upload_selector] = ok
        else:
            log.warning("cv_upload_selector is set but no cv_path provided — skipping upload")

    # 4. Tick agreement / consent checkboxes
    for selector in mapping.checkboxes_to_check:
        log.info("  Checking consent checkbox: %s", selector)
        el = _find(driver, selector)
        if el:
            ok = _fill_checkbox(driver, el, desired=True)
            results[selector] = ok

    success_count = sum(1 for v in results.values() if v)
    total = len(results)
    log.info("Form fill complete: %d/%d fields OK", success_count, total)
    return results
