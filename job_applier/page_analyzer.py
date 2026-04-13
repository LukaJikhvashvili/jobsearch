"""
Page analyzer for job_applier.

Captures a rich DOM snapshot via JavaScript, then uses AI to map
CV fields to page selectors.

Public API
----------
snapshot_page(driver)           →  List[FormField]
map_fields(fields, cv)          →  FormMapping
analyze_page(driver, cv)        →  FormMapping   (convenience)
search_in_iframes(driver, cv)   →  Optional[FormMapping]
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException

from .ai_client import ai_json
from .models import CVData, FormField, FormMapping

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JavaScript DOM snapshot
# ---------------------------------------------------------------------------

_DOM_SNAPSHOT_JS = """
(function() {
  function getLabel(el) {
    // aria-label
    var al = el.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    // aria-labelledby
    var alby = el.getAttribute('aria-labelledby');
    if (alby) {
      var labelEl = document.getElementById(alby);
      if (labelEl) return labelEl.textContent.trim();
    }
    // <label for="id">
    if (el.id) {
      var lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lbl) return lbl.textContent.trim();
    }
    // parent <label> or closest .form-group label
    var p = el.parentElement;
    for (var depth = 0; depth < 6 && p; depth++) {
      if (p.tagName === 'LABEL') return p.textContent.replace(el.value||'','').trim();
      var innerLbl = p.querySelector('label');
      if (innerLbl && !innerLbl.contains(el)) return innerLbl.textContent.trim();
      if (['FORM','BODY','HTML'].includes(p.tagName)) break;
      p = p.parentElement;
    }
    // title / placeholder fallback
    return el.getAttribute('title') || el.getAttribute('placeholder') || '';
  }

  function getSelector(el) {
    if (el.id) return '#' + el.id;
    if (el.getAttribute('name'))
      return el.tagName.toLowerCase() + '[name="' + el.getAttribute('name') + '"]';
    // data-testid / data-cy / data-qa
    for (var attr of ['data-testid','data-cy','data-qa','data-id']) {
      var v = el.getAttribute(attr);
      if (v) return el.tagName.toLowerCase() + '[' + attr + '="' + v + '"]';
    }
    // fallback: path of nth-of-type
    var path = [];
    var cur = el;
    for (var d = 0; d < 5 && cur && cur.tagName; d++) {
      var tag = cur.tagName.toLowerCase();
      var siblings = cur.parentElement
        ? Array.from(cur.parentElement.children).filter(c => c.tagName === cur.tagName)
        : [];
      if (siblings.length > 1) {
        path.unshift(tag + ':nth-of-type(' + (siblings.indexOf(cur)+1) + ')');
      } else {
        path.unshift(tag);
      }
      cur = cur.parentElement;
    }
    return path.join(' > ');
  }

  var seen = new Set();
  var results = [];
  var nodes = document.querySelectorAll(
    'input:not([type="hidden"]), textarea, select, ' +
    'button, [role="button"], [type="submit"], [role="checkbox"], [role="radio"]'
  );

  nodes.forEach(function(el, i) {
    var type = (el.getAttribute('type') || el.tagName).toLowerCase();
    var isFile = el.tagName === 'INPUT' && type === 'file';
    var rect = el.getBoundingClientRect();
    var visible = isFile || (rect.width > 0 && rect.height > 0 &&
                              window.getComputedStyle(el).visibility !== 'hidden' &&
                              window.getComputedStyle(el).display !== 'none');
    if (!visible) return;

    var sel = getSelector(el);
    if (seen.has(sel)) return;
    seen.add(sel);

    var opts = [];
    if (el.tagName === 'SELECT') {
      opts = Array.from(el.options).map(o => o.text.trim()).filter(Boolean);
    }
    // radio group sibling values
    if (type === 'radio' && el.getAttribute('name')) {
      document.querySelectorAll('input[type="radio"][name="' + el.getAttribute('name') + '"]')
        .forEach(r => { if (r.value) opts.push(r.value); });
    }

    results.push({
      index: i,
      tag: el.tagName.toLowerCase(),
      type: type,
      label: getLabel(el),
      placeholder: el.getAttribute('placeholder') || '',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      required: el.required || el.getAttribute('aria-required') === 'true' || false,
      value: el.value || '',
      text: el.textContent.trim().substring(0, 120),
      options: opts,
      accept: el.getAttribute('accept') || '',
      selector: sel,
      disabled: el.disabled || false
    });
  });
  return JSON.stringify(results);
})();
"""


# ---------------------------------------------------------------------------
# DOM snapshot
# ---------------------------------------------------------------------------


def snapshot_page(driver: Any) -> List[FormField]:
    """
    Execute the DOM snapshot script and return a list of FormField objects.

    Parameters
    ----------
    driver : WebDriver
        Active Selenium WebDriver (current frame/window is used).

    Returns
    -------
    List[FormField]
    """
    try:
        raw = driver.execute_script(_DOM_SNAPSHOT_JS)
        items: List[Dict] = json.loads(raw)
    except Exception as exc:
        log.error("DOM snapshot failed: %s", exc)
        return []

    fields = []
    for item in items:
        if item.get("disabled"):
            continue
        fields.append(
            FormField(
                selector=item["selector"],
                tag=item["tag"],
                type=item["type"],
                label=item.get("label", ""),
                placeholder=item.get("placeholder", ""),
                name=item.get("name", ""),
                id_attr=item.get("id", ""),
                required=bool(item.get("required", False)),
                options=item.get("options", []),
                accept=item.get("accept", ""),
                text=item.get("text", ""),
                element_index=item.get("index", 0),
            )
        )

    log.debug("DOM snapshot: %d interactive elements found", len(fields))
    return fields


def _fields_to_prompt_lines(fields: List[FormField]) -> str:
    """Convert field list to a compact text description for the AI prompt."""
    lines = []
    for f in fields:
        parts = [f"[{f.tag}/{f.type}]", f'selector="{f.selector}"']
        if f.label:
            parts.append(f'label="{f.label}"')
        if f.placeholder:
            parts.append(f'placeholder="{f.placeholder}"')
        if f.required:
            parts.append("required")
        if f.options:
            parts.append(f"options={f.options[:6]}")
        if f.accept:
            parts.append(f'accept="{f.accept}"')
        if f.text and f.tag in ("button", "a"):
            parts.append(f'text="{f.text}"')
        lines.append("  " + " ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AI field mapping
# ---------------------------------------------------------------------------

_MAPPING_SYSTEM = """You are a job-application automation assistant.
Your job is to map an applicant's CV data fields to interactive HTML form elements,
then identify the file-upload input, any agreement checkboxes, and the submit button.
Return only valid JSON — no extra text."""

_MAPPING_SCHEMA = """
{
  "field_mappings": {
    "<cv_field_name>": "<exact selector string from the elements list>"
  },
  "generated_values": {
    "<selector>": "<AI-generated answer for fields not in CV, e.g. salary, availability, motivations>"
  },
  "cv_upload_selector": "<selector or null>",
  "submit_button_selector": "<selector or null>",
  "next_button_selector": "<selector or null — only for multi-step forms>",
  "checkboxes_to_check": ["<selector>", "..."]
}
"""


def map_fields(fields: List[FormField], cv: CVData) -> FormMapping:
    """
    Ask the AI to map CV fields to page elements and identify special controls.

    Parameters
    ----------
    fields : List[FormField]
        Output of snapshot_page().
    cv : CVData
        Applicant data to match against.

    Returns
    -------
    FormMapping
    """
    if not fields:
        log.warning("No fields to map — returning empty FormMapping")
        return FormMapping()

    fields_text = _fields_to_prompt_lines(fields)
    cv_block = cv.to_prompt_block()

    prompt = f"""Below is a list of interactive elements on a job-application page,
followed by the applicant's CV data. Produce a JSON mapping as described.

RULES:
1. Only include fields that exist in the elements list.
2. For text/email/tel/url inputs, map the correct CV field to the selector.
3. For <select> or radio groups, choose the option value closest to the CV data.
4. For fields NOT in the CV (e.g. salary expectation, notice period, motivations,
   "why do you want to work here?"), put a sensible short answer in "generated_values"
   keyed by selector.
5. "cv_upload_selector" must be a file input (accept contains .pdf or .docx or is empty).
6. "checkboxes_to_check" should include checkboxes for consent / terms / agreement.
7. If this looks like a multi-step form with a "Next" or "Continue" button (not Submit),
   set "next_button_selector" and leave "submit_button_selector" null.
8. Use EXACT selector strings from the list below.

APPLICANT CV DATA:
{cv_block}

PAGE ELEMENTS:
{fields_text}

RESPONSE SCHEMA:
{_MAPPING_SCHEMA}"""

    try:
        raw = ai_json(prompt, system=_MAPPING_SYSTEM, max_tokens=1500)
        mapping = FormMapping.from_dict(raw)
        log.info(
            "Field mapping: %d fields, upload=%s, submit=%s",
            len(mapping.field_mappings),
            mapping.cv_upload_selector,
            mapping.submit_selector,
        )
        return mapping
    except Exception as exc:
        log.error("AI field mapping failed: %s", exc)
        return FormMapping()


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def analyze_page(driver: Any, cv: CVData) -> FormMapping:
    """
    One-shot helper: snapshot the current page and return an AI-produced FormMapping.

    Parameters
    ----------
    driver : WebDriver
    cv : CVData

    Returns
    -------
    FormMapping
    """
    fields = snapshot_page(driver)
    return map_fields(fields, cv)


def search_in_iframes(driver: Any, cv: CVData) -> Optional[FormMapping]:
    """
    Iterate through page iframes looking for a form.
    Returns the first non-empty FormMapping found, or None.
    """
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    log.info("Searching %d iframes for a form…", len(iframes))

    for i, frame in enumerate(iframes):
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
            mapping = analyze_page(driver, cv)
            if mapping.field_mappings or mapping.cv_upload_selector:
                log.info("Form found in iframe %d", i)
                return mapping
        except WebDriverException as exc:
            log.debug("Iframe %d inaccessible: %s", i, exc)

    driver.switch_to.default_content()
    return None
