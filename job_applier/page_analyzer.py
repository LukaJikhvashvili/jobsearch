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

_DOM_SNAPSHOT_JS = r"""
(function() {
  "use strict";
 
  function cssEscape(s) {
    try {
      if (typeof CSS !== 'undefined' && CSS.escape) return CSS.escape(s);
    } catch(e) {}
    return String(s).replace(/([^\w\-])/g, '\\$1');
  }
 
  function getLabel(el) {
    try {
      var al = el.getAttribute('aria-label');
      if (al && al.trim()) return al.trim();
 
      var alby = el.getAttribute('aria-labelledby');
      if (alby) {
        var lbEl = document.getElementById(alby);
        if (lbEl) return lbEl.textContent.trim();
      }
 
      if (el.id) {
        var lbl = document.querySelector('label[for="' + cssEscape(el.id) + '"]');
        if (lbl) return lbl.textContent.trim();
      }
 
      var p = el.parentElement;
      for (var depth = 0; depth < 6 && p; depth++) {
        if (p.tagName === 'LABEL') return p.textContent.replace(el.value || '', '').trim();
        var innerLbl = p.querySelector('label');
        if (innerLbl && !innerLbl.contains(el)) return innerLbl.textContent.trim();
        if (p.tagName === 'FORM' || p.tagName === 'BODY' || p.tagName === 'HTML') break;
        p = p.parentElement;
      }
      return el.getAttribute('title') || el.getAttribute('placeholder') || '';
    } catch(e) { return ''; }
  }
 
  function getSelector(el) {
    try {
      if (el.id) return '#' + cssEscape(el.id);
 
      var name = el.getAttribute('name');
      if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
 
      var testAttrs = ['data-testid', 'data-cy', 'data-qa', 'data-id'];
      for (var ai = 0; ai < testAttrs.length; ai++) {
        var av = el.getAttribute(testAttrs[ai]);
        if (av) return el.tagName.toLowerCase() + '[' + testAttrs[ai] + '="' + av + '"]';
      }
 
      var path = [];
      var cur = el;
      for (var d = 0; d < 5 && cur && cur.tagName; d++) {
        var tag = cur.tagName.toLowerCase();
        var siblings = cur.parentElement
          ? Array.prototype.filter.call(cur.parentElement.children, function(c) {
              return c.tagName === cur.tagName;
            })
          : [];
        if (siblings.length > 1) {
          path.unshift(tag + ':nth-of-type(' + (siblings.indexOf(cur) + 1) + ')');
        } else {
          path.unshift(tag);
        }
        cur = cur.parentElement;
      }
      return path.join(' > ');
    } catch(e) { return ''; }
  }
 
  try {
    var seen = {};
    var results = [];
    var nodes = document.querySelectorAll(
      'input:not([type="hidden"]), textarea, select, ' +
      'button, [role="button"], [type="submit"], [role="checkbox"], [role="radio"]'
    );
 
    for (var ni = 0; ni < nodes.length; ni++) {
      (function(el, i) {
        try {
          var type = (el.getAttribute('type') || el.tagName).toLowerCase();
          var isFile = (el.tagName === 'INPUT' && type === 'file');
 
          var rect = el.getBoundingClientRect();
          var style = window.getComputedStyle(el);
          var visible = isFile || (
            rect.width > 0 && rect.height > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none'
          );
          if (!visible) return;
 
          var sel = getSelector(el);
          if (!sel || seen[sel]) return;
          seen[sel] = true;
 
          var opts = [];
          if (el.tagName === 'SELECT') {
            for (var oi = 0; oi < el.options.length; oi++) {
              var ot = el.options[oi].text.trim();
              if (ot) opts.push(ot);
            }
          }
          if (type === 'radio' && el.getAttribute('name')) {
            var radios = document.querySelectorAll(
              'input[type="radio"][name="' + el.getAttribute('name') + '"]'
            );
            for (var ri = 0; ri < radios.length; ri++) {
              if (radios[ri].value) opts.push(radios[ri].value);
            }
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
        } catch(elemErr) { /* skip bad element */ }
      })(nodes[ni], ni);
    }
 
    return JSON.stringify(results);
  } catch(e) {
    return JSON.stringify([]);
  }
})();
"""


# ---------------------------------------------------------------------------
# DOM snapshot
# ---------------------------------------------------------------------------


def snapshot_page(driver: Any) -> List[FormField]:
    """
    Find interactive elements using Selenium's API and return a list of FormField objects.

    Parameters
    ----------
    driver : WebDriver
        Active Selenium WebDriver (current frame/window is used).

    Returns
    -------
    List[FormField]
    """
    fields = []
    log.debug("Starting DOM snapshot using Selenium API.")

    # List of common interactive elements to look for
    interactive_elements_css = [
        "input:not([type='hidden'])",
        "textarea",
        "select",
        "button",
        "[role='button']",
        "[type='submit']",
        "[role='checkbox']",
        "[role='radio']"
    ]

    for css_selector in interactive_elements_css:
        elements = driver.find_elements(By.CSS_SELECTOR, css_selector)
        for i, el in enumerate(elements):
            try:
                # Check visibility
                if not el.is_displayed():
                    continue

                # Basic attributes
                tag = el.tag_name.lower()
                el_type = el.get_attribute("type") or tag
                name = el.get_attribute("name") or ""
                id_attr = el.get_attribute("id") or ""
                placeholder = el.get_attribute("placeholder") or ""
                required = el.get_attribute("required") == "true" or el.get_attribute("aria-required") == "true"
                accept = el.get_attribute("accept") or ""
                text = el.text.strip()[:120] if el.text else ""

                # Generate a selector (simplified for now, prioritize ID/Name)
                selector = ""
                if id_attr:
                    selector = f"#{id_attr}"
                elif name:
                    selector = f"{tag}[name='{name}']"
                else:
                    selector = css_selector # Fallback, might not be unique

                # Options for select/radio
                options = []
                if tag == "select":
                    for option_el in el.find_elements(By.TAG_NAME, "option"):
                        option_text = option_el.text.strip()
                        if option_text:
                            options.append(option_text)
                elif el_type == "radio" and name:
                    # For radio buttons, we need to find all with the same name to get options
                    radio_group = driver.find_elements(By.CSS_SELECTOR, f"input[type='radio'][name='{name}']")
                    for radio_el in radio_group:
                        radio_value = radio_el.get_attribute("value")
                        if radio_value and radio_value not in options:
                            options.append(radio_value)
                
                # Try to get label text. This is tricky with Selenium directly.
                # For now, we'll try to find a <label> associated by 'for' attribute or parent.
                label_text = ""
                if id_attr:
                    try:
                        label_el = driver.find_element(By.CSS_SELECTOR, f"label[for='{id_attr}']")
                        label_text = label_el.text.strip()
                    except:
                        pass # No direct label for 'for' attribute
                
                if not label_text and el.get_attribute("aria-label"):
                    label_text = el.get_attribute("aria-label").strip()
                
                # Check parent element for label text
                if not label_text:
                    parent = el.find_element(By.XPATH, "..")
                    if parent.tag_name.lower() == 'label':
                        label_text = parent.text.strip().replace(el.get_attribute("value") or '', '').strip()
                

                fields.append(
                    FormField(
                        selector=selector,
                        tag=tag,
                        type=el_type,
                        label=label_text,
                        placeholder=placeholder,
                        name=name,
                        id_attr=id_attr,
                        required=required,
                        options=options,
                        accept=accept,
                        text=text,
                        element_index=i,
                    )
                )
            except Exception as elem_exc:
                log.warning("Error processing element %s: %s", css_selector, elem_exc)
                continue

    log.debug("DOM snapshot: %d interactive elements found via Selenium API.", len(fields))
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
