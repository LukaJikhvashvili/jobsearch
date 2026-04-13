"""
Browser management for job_applier.

Provides a factory for a configured Selenium Chrome WebDriver.
"""

from __future__ import annotations

import logging
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

log = logging.getLogger(__name__)

DEFAULT_WAIT = 20  # seconds


def create_driver(
    headless: bool = True,
    download_dir: Optional[str] = None,
    window_size: str = "1920,1080",
) -> webdriver.Chrome:
    """
    Create and return a configured Chrome WebDriver.

    Parameters
    ----------
    headless : bool
        Run Chrome without a visible window.
    download_dir : str, optional
        Directory for file downloads (defaults to system default).
    window_size : str
        Viewport dimensions as "width,height".

    Returns
    -------
    webdriver.Chrome
    """
    opts = Options()

    if headless:
        opts.add_argument("--headless=new")

    opts.add_argument(f"--window-size={window_size}")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # Realistic user-agent to avoid bot-detection
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    if download_dir:
        prefs = {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
        }
        opts.add_experimental_option("prefs", prefs)

    try:
        # Try webdriver-manager first (auto-downloads matching chromedriver)
        from webdriver_manager.chrome import ChromeDriverManager

        service = Service(ChromeDriverManager().install())
    except Exception:
        # Fall back to PATH chromedriver
        service = Service()

    driver = webdriver.Chrome(service=service, options=opts)

    # Mask navigator.webdriver property
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )

    log.info("Chrome WebDriver started (headless=%s)", headless)
    return driver


def make_wait(driver: webdriver.Chrome, timeout: int = DEFAULT_WAIT) -> WebDriverWait:
    """Return a WebDriverWait instance for the given driver."""
    return WebDriverWait(driver, timeout)
