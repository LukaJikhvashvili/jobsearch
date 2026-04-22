"""
Login wall detection and credential-based authentication.

Flow:
  1. _is_login_wall(page) — heuristic check after navigating to a URL
  2. If detected, attempt login using stored credentials
  3. Re-navigate to the original URL after login
  4. If no credentials stored, raise AuthRequired so the caller can prompt the user

Credentials are stored in .scraper_credentials (JSON, gitignored).
Each site gets one entry: { "username": "...", "password": "..." }
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_CREDS_FILE = Path(".scraper_credentials")

# ── Login wall detection signals ────────────────────────────────────────────
# Any combination of these strongly suggests we hit a login page.
_LOGIN_URL_HINTS = [
    "/login",
    "/signin",
    "/sign-in",
    "/auth",
    "/account/login",
    "/user/login",
    "/session/new",
    "/log-in",
    "/გვერდი",
    "/შესვლა",
]
_LOGIN_TEXT_HINTS = [
    "sign in",
    "log in",
    "login",
    "please log in",
    "please sign in",
    "email and password",
    "username and password",
    "შესვლა",
    "ავტორიზაცია",
    "შეიყვანეთ პაროლი",
]
_LOGIN_FORM_SIGNALS = [
    'input[type="password"]',
    'input[name*="password"]',
    'input[name*="pass"]',
    'form[action*="login"]',
    'form[action*="signin"]',
    'form[action*="auth"]',
]


class AuthRequired(Exception):
    """Raised when a login wall is detected and no credentials are available."""

    def __init__(self, site: str, url: str):
        self.site = site
        self.url = url
        super().__init__(
            f"Login wall detected for '{site}' at {url}. "
            f"Provide credentials with: store_credentials('{site}', 'user', 'pass')"
        )


@dataclass
class Credentials:
    username: str
    password: str


# ---------------------------------------------------------------------------
# Credential store
# ---------------------------------------------------------------------------


def store_credentials(site: str, username: str, password: str) -> None:
    """Save credentials for a site. Call this once manually."""
    data: dict = {}
    if _CREDS_FILE.exists():
        try:
            data = json.loads(_CREDS_FILE.read_text())
        except Exception:
            pass
    data[site] = {"username": username, "password": password}
    _CREDS_FILE.write_text(json.dumps(data, indent=2))
    logger.info("Credentials stored for %s", site)


def load_credentials(site: str) -> Optional[Credentials]:
    if not _CREDS_FILE.exists():
        return None
    try:
        data = json.loads(_CREDS_FILE.read_text())
        entry = data.get(site)
        if entry:
            return Credentials(username=entry["username"], password=entry["password"])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def is_login_wall(page: Page) -> bool:
    """
    Return True if the current page looks like a login/auth page.
    Uses three independent signals — any one is sufficient.
    """
    current_url = page.url.lower()

    # Signal 1: login-pattern in URL
    if any(hint in current_url for hint in _LOGIN_URL_HINTS):
        logger.debug("Login wall detected via URL: %s", page.url)
        return True

    # Signal 2: password input on page
    for selector in _LOGIN_FORM_SIGNALS:
        try:
            el = page.locator(selector).first
            if await el.count() > 0:
                logger.debug("Login wall detected via form selector: %s", selector)
                return True
        except Exception:
            continue

    # Signal 3: login keywords in visible text
    try:
        body_text = (await page.inner_text("body")).lower()
        if any(hint in body_text for hint in _LOGIN_TEXT_HINTS):
            logger.debug("Login wall detected via body text")
            return True
    except Exception:
        pass

    return False


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


async def attempt_login(page: Page, site: str, original_url: str) -> bool:
    """
    Try to log in using stored credentials. Returns True on success.
    Raises AuthRequired if no credentials exist for this site.
    """
    creds = load_credentials(site)
    if not creds:
        raise AuthRequired(site, original_url)

    logger.info("Attempting login for %s …", site)

    # Find username and password fields
    user_selectors = [
        'input[type="email"]',
        'input[type="text"][name*="user"]',
        'input[type="text"][name*="email"]',
        'input[name*="login"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="username" i]',
        'input[type="text"]:first-of-type',
    ]
    pass_selectors = [
        'input[type="password"]',
    ]
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'button:has-text("Login")',
        'button:has-text("შესვლა")',
    ]

    try:
        # Fill username
        for sel in user_selectors:
            try:
                inp = page.locator(sel).first
                if await inp.is_visible(timeout=1_000):
                    await inp.fill(creds.username)
                    logger.debug("Filled username with selector: %s", sel)
                    break
            except Exception:
                continue

        # Fill password
        for sel in pass_selectors:
            try:
                inp = page.locator(sel).first
                if await inp.is_visible(timeout=1_000):
                    await inp.fill(creds.password)
                    logger.debug("Filled password")
                    break
            except Exception:
                continue

        # Submit
        for sel in submit_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click()
                    logger.debug("Clicked submit: %s", sel)
                    break
            except Exception:
                continue

        # Wait for navigation
        await page.wait_for_load_state("networkidle", timeout=10_000)

        # Verify we're no longer on a login wall
        if not await is_login_wall(page):
            logger.info("Login successful for %s", site)
            # Navigate to original destination
            await page.goto(original_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1500)
            return True
        else:
            logger.warning("Login attempt failed for %s (still on login wall)", site)
            return False

    except Exception as exc:
        logger.error("Login error for %s: %s", site, exc)
        return False
