"""
config.py — Central configuration for the AI Job Application Assistant.

All settings live here. No hardcoded values anywhere else.
Edit this file to add new job sites, change models, tune delays, etc.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env (GEMINI_API_KEY lives there — never commit it)
# ---------------------------------------------------------------------------
load_dotenv()


# ---------------------------------------------------------------------------
# Project Paths
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent
ASSETS_DIR = ROOT_DIR / "assets"
TAILORED_DIR = ASSETS_DIR / "tailored"
PROFILES_DIR = ROOT_DIR / "profiles"
LOGS_DIR = ROOT_DIR / "logs"
DB_PATH = ROOT_DIR / "job_assistant.db"
BASE_CV_PATH = ASSETS_DIR / "base_cv.pdf"
USER_PROFILE = PROFILES_DIR / "user_profile.json"
BROWSER_STATE = ROOT_DIR / ".browser_state"  # persistent cookies/session dir

# Auto-create directories that must exist
for _dir in [ASSETS_DIR, TAILORED_DIR, PROFILES_DIR, LOGS_DIR, BROWSER_STATE]:
    _dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

if not GEMINI_API_KEY:
    raise EnvironmentError("GEMINI_API_KEY is not set. Add it to your .env file:\n" "  GEMINI_API_KEY=your_key_here")

# Model selection:
#   Flash  → fast + cheap  → scraping, scoring, form detection  (most calls)
#   Pro    → smarter       → CV tailoring only  (1 call per application)
GEMINI_FLASH_MODEL = "gemini-3.1-flash-lite"
GEMINI_PRO_MODEL = "gemini-3-flash"

# Free-tier rate limits (requests per minute / per day)
# Flash: 15 RPM / 1500 RPD  |  Pro: 2 RPM / 50 RPD
GEMINI_FLASH_RPM = 15  # leave 1 buffer
GEMINI_FLASH_RPD = 500  # leave 100 buffer
GEMINI_PRO_RPM = 5
GEMINI_PRO_RPD = 20

# Max tokens to send to Gemini per call (controls cost & speed)
GEMINI_MAX_INPUT_CHARS = 40_000  # ~10_000 tokens (Crawl4AI output is truncated to this)
GEMINI_MAX_OUTPUT_TOKENS = 2048


# ---------------------------------------------------------------------------
# Browser / Playwright
# ---------------------------------------------------------------------------
@dataclass
class BrowserConfig:
    headless: bool = False  # False = visible (safer for anti-bot, easier to debug)
    slow_mo_ms: int = 80  # ms between each Playwright action
    viewport_width: int = 1366
    viewport_height: int = 768
    locale: str = "en-US"
    timezone: str = "Asia/Tbilisi"  # Georgia timezone
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    # Stealth: disable WebDriver flags
    args: list = field(
        default_factory=lambda: [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--disable-extensions",
        ]
    )


BROWSER = BrowserConfig()


# ---------------------------------------------------------------------------
# Human-like Delay Ranges (seconds)
# ---------------------------------------------------------------------------
@dataclass
class DelayConfig:
    # Between page loads / navigations
    page_load_min: float = 3.0
    page_load_max: float = 8.0
    # Between individual actions (click, type keystroke, etc.)
    action_min: float = 0.5
    action_max: float = 2.5
    # Between jobs being processed (so we don't hammer sites)
    between_jobs_min: float = 15.0
    between_jobs_max: float = 45.0
    # Between Gemini API calls (respect free tier RPM)
    gemini_min: float = 5.0
    gemini_max: float = 10.0
    # Typing speed: chars per second range
    typing_cps_min: int = 4
    typing_cps_max: int = 9


DELAYS = DelayConfig()


# ---------------------------------------------------------------------------
# Job Sites Configuration
# Each entry defines how to search; NO CSS selectors — Gemini handles parsing.
# ---------------------------------------------------------------------------
@dataclass
class SiteConfig:
    name: str
    base_url: str
    search_url_template: str  # {query} and {location} placeholders
    requires_login: bool = False
    login_url: Optional[str] = None
    # Crawl4AI: which content tags to prioritize when cleaning the page
    content_selector_hint: str = "main, article, .job, .vacancy, [class*='job'], [class*='result']"
    # Max results pages to scan per search
    max_pages: int = 3
    # Seconds to wait for dynamic content to load
    dynamic_wait_s: float = 3.0


JOB_SITES: dict[str, SiteConfig] = {
    "jobs_ge": SiteConfig(
        name="Jobs.ge",
        base_url="https://jobs.ge",
        search_url_template="https://jobs.ge/?q={query}&l={location}",
        content_selector_hint=".vacancies-list, .vacancy-item, main",
        max_pages=3,
    ),
    "hh_ge": SiteConfig(
        name="hh.ge",
        base_url="https://hh.ge",
        search_url_template="https://hh.ge/search/vacancy?text={query}&area=&l={location}",
        content_selector_hint=".vacancy-search-list, .vacancy-card",
        max_pages=3,
    ),
    "linkedin": SiteConfig(
        name="LinkedIn",
        base_url="https://www.linkedin.com",
        search_url_template=("https://www.linkedin.com/jobs/search/" "?keywords={query}&location={location}&f_TPR=r86400"),
        requires_login=True,
        login_url="https://www.linkedin.com/login",
        content_selector_hint=".jobs-search__results-list, .job-card-container",
        max_pages=2,
        dynamic_wait_s=5.0,
    ),
    "indeed": SiteConfig(
        name="Indeed",
        base_url="https://www.indeed.com",
        search_url_template="https://www.indeed.com/jobs?q={query}&l={location}",
        content_selector_hint="#mosaic-provider-jobcards, .job_seen_beacon",
        max_pages=3,
    ),
    "remoteco": SiteConfig(
        name="Remote.co",
        base_url="https://remote.co",
        search_url_template="https://remote.co/remote-jobs/search/?search_keywords={query}",
        content_selector_hint=".job_listings, .job_listing",
        max_pages=2,
    ),
}


# ---------------------------------------------------------------------------
# CV & PDF Generation
# ---------------------------------------------------------------------------
@dataclass
class CVConfig:
    # ReportLab PDF settings
    font_name: str = "Helvetica"
    font_size_body: int = 10
    font_size_heading: int = 13
    font_size_section: int = 11
    page_margin_pt: int = 50  # points (1pt = 1/72 inch)
    line_spacing: float = 14.0
    # ATS safety: avoid tables, columns, images in generated PDF
    ats_safe_mode: bool = True
    # Output filename pattern — {job_id} replaced at runtime
    output_filename_pattern: str = "tailored_cv_{job_id}.pdf"


CV_CONFIG = CVConfig()


# ---------------------------------------------------------------------------
# Matching / Scoring
# ---------------------------------------------------------------------------
@dataclass
class MatchConfig:
    # Jobs below this score are skipped (not tailored/applied)
    min_score_to_apply: int = 55
    # Jobs above this score skip manual review prompt (auto-queue for CV tailoring)
    auto_tailor_above: int = 75


MATCH = MatchConfig()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = "INFO"
LOG_FILE = LOGS_DIR / "app.log"
LOG_MAX_MB = 10
LOG_BACKUPS = 3


# ---------------------------------------------------------------------------
# Application Safety
# ---------------------------------------------------------------------------
# Never submit without explicit "yes" from the user — hardcoded safeguard.
REQUIRE_HUMAN_CONFIRM_BEFORE_SUBMIT: bool = True

# Dry-run mode: go through entire flow but skip the final submit click.
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() == "true"
