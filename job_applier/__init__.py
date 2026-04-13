"""
job_applier — automated job-application helper.

Quickstart
----------
    from job_applier import apply

    result = apply(
        url="https://example.com/careers/apply",
        cv_path="my_cv.pdf",
        auto_submit=True,
        headless=True,
    )
    print(result)

Individual modules are importable for custom pipelines:

    from job_applier import (
        extract_cv_text, parse_cv_data, load_cv,
        snapshot_page, map_fields, analyze_page,
        fill_form, upload_file, click_element,
        capture_state, verify_submission,
        create_driver,
        CVData, FormMapping, SubmissionResult,
    )

Environment variables (set in .env):
    AI_PROVIDER        = anthropic (default) | gemini
    ANTHROPIC_API_KEY  = sk-ant-...   (required for anthropic)
    GEMINI_API_KEY     = ...          (required for gemini)
"""

from .orchestrator import apply
from .cv_parser import extract_cv_text, parse_cv_data, load_cv, generate_cover_letter
from .page_analyzer import snapshot_page, map_fields, analyze_page, search_in_iframes
from .form_filler import fill_form, upload_file, fill_field, click_element
from .verifier import capture_state, verify_submission, PageState
from .browser import create_driver
from .models import CVData, FormMapping, SubmissionResult, FormField
from .ai_client import ai_complete, ai_json, get_ai_client

__all__ = [
    # High-level
    "apply",
    # CV
    "extract_cv_text",
    "parse_cv_data",
    "load_cv",
    "generate_cover_letter",
    # Page analysis
    "snapshot_page",
    "map_fields",
    "analyze_page",
    "search_in_iframes",
    # Form filling
    "fill_form",
    "fill_field",
    "upload_file",
    "click_element",
    # Verification
    "capture_state",
    "verify_submission",
    "PageState",
    # Browser
    "create_driver",
    # Models
    "CVData",
    "FormMapping",
    "SubmissionResult",
    "FormField",
    # AI
    "ai_complete",
    "ai_json",
    "get_ai_client",
]
