"""
browser/launcher.py — Stealth Playwright browser context factory.

Handles:
  - Launching Chromium with anti-detection patches (playwright-stealth).
  - Persistent browser context (cookies / login sessions survive restarts).
  - Per-site session isolation via named context profiles.
  - Graceful teardown.

Usage:
    from browser.launcher import BrowserManager

    async with BrowserManager() as bm:
        page = await bm.new_page("jobs_ge")
        await page.goto("https://jobs.ge")
"""

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright_stealth import stealth_async

from config import BROWSER, BROWSER_STATE, DELAYS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JavaScript patches injected into every page to mask automation signals
# ---------------------------------------------------------------------------
_STEALTH_INIT_SCRIPT = """
// 1. Hide webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. Fake plugins array (empty = bot)
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});

// 3. Fake languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});

// 4. Pass chrome object check
window.chrome = { runtime: {} };

// 5. Permissions API — don't expose automation
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""


class BrowserManager:
    """
    Manages a single Playwright instance with multiple named contexts.

    Each site gets its own persistent context (separate cookie jar / storage).
    Contexts are created lazily and reused across calls.

    Example:
        async with BrowserManager() as bm:
            page = await bm.new_page("linkedin")
            # ... use page ...
            await bm.close_page(page)
    """

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        # site_name → BrowserContext
        self._contexts: dict[str, BrowserContext] = {}

    # ------------------------------------------------------------------
    # Context manager entry / exit
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    async def start(self) -> None:
        """Launch the Playwright browser."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=BROWSER.headless,
            slow_mo=BROWSER.slow_mo_ms,
            args=BROWSER.args,
        )
        logger.info(
            f"Browser launched | headless={BROWSER.headless} | "
            f"slow_mo={BROWSER.slow_mo_ms}ms"
        )

    async def stop(self) -> None:
        """Close all contexts and the browser cleanly."""
        for name, ctx in self._contexts.items():
            try:
                await ctx.close()
                logger.debug(f"Context '{name}' closed.")
            except Exception as e:
                logger.warning(f"Error closing context '{name}': {e}")
        self._contexts.clear()

        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped.")

    # ------------------------------------------------------------------
    # Context / Page management
    # ------------------------------------------------------------------

    async def _get_or_create_context(self, site_name: str) -> BrowserContext:
        """
        Return existing context for site, or create a new persistent one.

        Persistent contexts store cookies and localStorage on disk so that
        login sessions survive between runs.
        """
        if site_name in self._contexts:
            return self._contexts[site_name]

        state_dir = BROWSER_STATE / site_name
        state_dir.mkdir(parents=True, exist_ok=True)

        ctx = await self._browser.new_context(
            viewport={
                "width":  BROWSER.viewport_width,
                "height": BROWSER.viewport_height,
            },
            user_agent=BROWSER.user_agent,
            locale=BROWSER.locale,
            timezone_id=BROWSER.timezone,
            # Load saved storage state if it exists (cookies, localStorage)
            storage_state=str(state_dir / "state.json")
                if (state_dir / "state.json").exists()
                else None,
        )

        # Inject stealth patches on every new page/frame within this context
        await ctx.add_init_script(_STEALTH_INIT_SCRIPT)

        self._contexts[site_name] = ctx
        logger.info(f"New browser context created for '{site_name}' → {state_dir}")
        return ctx

    async def new_page(self, site_name: str = "default") -> Page:
        """
        Open a new page inside the named site context.

        The returned page already has:
          - playwright-stealth applied
          - Random viewport jitter (±20px) to vary fingerprint
          - Default navigation timeout of 30s
        """
        ctx = await self._get_or_create_context(site_name)
        page = await ctx.new_page()

        # Apply playwright-stealth (patches fingerprinting APIs)
        await stealth_async(page)

        # Small random viewport jitter to vary fingerprint across sessions
        jitter_w = random.randint(-20, 20)
        jitter_h = random.randint(-10, 10)
        await page.set_viewport_size({
            "width":  BROWSER.viewport_width  + jitter_w,
            "height": BROWSER.viewport_height + jitter_h,
        })

        page.set_default_timeout(30_000)       # 30s navigation timeout
        page.set_default_navigation_timeout(30_000)

        logger.debug(f"New page opened in context '{site_name}'")
        return page

    async def save_session(self, site_name: str) -> None:
        """
        Persist cookies and storage state for a site to disk.
        Call after a successful login so the session survives restarts.
        """
        if site_name not in self._contexts:
            logger.warning(f"No context for '{site_name}' — nothing to save.")
            return

        state_dir = BROWSER_STATE / site_name
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / "state.json"

        await self._contexts[site_name].storage_state(path=str(state_file))
        logger.info(f"Session saved for '{site_name}' → {state_file}")

    async def close_page(self, page: Page) -> None:
        """Close a single page gracefully."""
        try:
            await page.close()
        except Exception as e:
            logger.debug(f"Page close warning: {e}")

    def is_logged_in(self, site_name: str) -> bool:
        """Check if a persistent session file exists for the site."""
        state_file = BROWSER_STATE / site_name / "state.json"
        return state_file.exists()


# ---------------------------------------------------------------------------
# Human-like Page Actions
# ---------------------------------------------------------------------------

class HumanActions:
    """
    Stateless collection of human-mimicking browser actions.
    All methods accept a Playwright Page and operate on it.

    Design: These are thin wrappers that inject random delays and
    movement patterns. Business logic stays in the caller.
    """

    @staticmethod
    async def random_delay(min_s: float = None, max_s: float = None) -> None:
        """Sleep for a random duration within the configured action range."""
        lo = min_s if min_s is not None else DELAYS.action_min
        hi = max_s if max_s is not None else DELAYS.action_max
        await asyncio.sleep(random.uniform(lo, hi))

    @staticmethod
    async def page_load_delay() -> None:
        """Delay to simulate reading time after a page loads."""
        await asyncio.sleep(random.uniform(DELAYS.page_load_min, DELAYS.page_load_max))

    @staticmethod
    async def between_jobs_delay() -> None:
        """Longer pause between processing jobs — avoids request bursts."""
        delay = random.uniform(DELAYS.between_jobs_min, DELAYS.between_jobs_max)
        logger.debug(f"Between-job pause: {delay:.1f}s")
        await asyncio.sleep(delay)

    @staticmethod
    async def scroll_page(
        page: Page,
        scrolls: int = None,
        direction: str = "down",
    ) -> None:
        """
        Scroll the page in chunks, simulating human reading behavior.

        Args:
            page: Playwright page.
            scrolls: Number of scroll steps. Random 3-7 if not specified.
            direction: "down" or "up".
        """
        n = scrolls or random.randint(3, 7)
        sign = 1 if direction == "down" else -1

        for i in range(n):
            # Vary scroll distance: 300-700px per step
            dist = random.randint(300, 700) * sign
            await page.mouse.wheel(0, dist)
            # Short pause between scrolls (0.3-1.2s) — not uniform
            await asyncio.sleep(random.uniform(0.3, 1.2))

        logger.debug(f"Scrolled {direction} {n} steps")

    @staticmethod
    async def scroll_to_bottom(page: Page) -> None:
        """Scroll all the way to the bottom of the page gradually."""
        await page.evaluate("""
            () => new Promise(resolve => {
                let total = document.body.scrollHeight;
                let current = 0;
                const step = () => {
                    const chunk = Math.floor(Math.random() * 400) + 200;
                    current = Math.min(current + chunk, total);
                    window.scrollTo(0, current);
                    if (current < total) {
                        setTimeout(step, Math.random() * 800 + 300);
                    } else {
                        resolve();
                    }
                };
                step();
            })
        """)
        await asyncio.sleep(1.0)

    @staticmethod
    async def human_click(page: Page, selector: str) -> None:
        """
        Click an element with slight random offset from center.
        Moves mouse to element first (more natural than direct click).
        """
        element = await page.wait_for_selector(selector, timeout=10_000)
        box = await element.bounding_box()
        if not box:
            await element.click()
            return

        # Click slightly off-center
        x = box["x"] + box["width"]  * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

        # Move mouse to element first
        await page.mouse.move(x, y, steps=random.randint(5, 15))
        await asyncio.sleep(random.uniform(0.1, 0.4))
        await page.mouse.click(x, y)
        await HumanActions.random_delay(0.3, 1.0)

    @staticmethod
    async def human_type(
        page: Page,
        selector: str,
        text: str,
        clear_first: bool = True,
    ) -> None:
        """
        Type text character-by-character at a realistic speed.
        Occasionally makes a typo and corrects it for extra realism.
        """
        element = await page.wait_for_selector(selector, timeout=10_000)
        await element.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))

        if clear_first:
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)
            await page.keyboard.press("Delete")
            await asyncio.sleep(0.2)

        # Type with variable speed
        cps_min = DELAYS.typing_cps_min
        cps_max = DELAYS.typing_cps_max

        for i, char in enumerate(text):
            # ~5% chance of a typo on non-special chars
            if (
                random.random() < 0.05
                and char.isalpha()
                and len(text) > 5
                and i < len(text) - 1
            ):
                # Type a wrong char, then backspace
                wrong = random.choice("qwertyuiopasdfghjklzxcvbnm")
                await page.keyboard.type(wrong)
                await asyncio.sleep(random.uniform(0.08, 0.2))
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.05, 0.15))

            await page.keyboard.type(char)
            # Variable delay between keystrokes
            delay = 1.0 / random.uniform(cps_min, cps_max)
            await asyncio.sleep(delay)

        await asyncio.sleep(random.uniform(0.2, 0.5))

    @staticmethod
    async def move_mouse_randomly(page: Page, steps: int = 3) -> None:
        """
        Move the mouse to random positions — simulates idle human behavior.
        Call occasionally while waiting for page loads.
        """
        w = BROWSER.viewport_width
        h = BROWSER.viewport_height
        for _ in range(steps):
            x = random.randint(100, w - 100)
            y = random.randint(100, h - 100)
            await page.mouse.move(x, y, steps=random.randint(3, 8))
            await asyncio.sleep(random.uniform(0.1, 0.4))

    @staticmethod
    async def safe_goto(
        page: Page,
        url: str,
        wait_until: str = "domcontentloaded",
    ) -> bool:
        """
        Navigate to URL with error handling and post-load delay.

        Returns True on success, False on navigation failure.
        """
        try:
            logger.debug(f"Navigating to: {url}")
            await page.goto(url, wait_until=wait_until, timeout=30_000)
            await HumanActions.page_load_delay()
            await HumanActions.move_mouse_randomly(page, steps=2)
            return True
        except Exception as e:
            logger.error(f"Navigation failed for {url}: {e}")
            return False

    @staticmethod
    async def wait_for_content(
        page: Page,
        selector: str,
        timeout_ms: int = 15_000,
    ) -> bool:
        """
        Wait for a selector to appear. Returns False instead of raising on timeout.
        """
        try:
            await page.wait_for_selector(selector, timeout=timeout_ms)
            return True
        except Exception:
            logger.warning(f"Selector not found within {timeout_ms}ms: {selector}")
            return False

    @staticmethod
    async def get_page_html(page: Page) -> str:
        """Return the full outer HTML of the current page."""
        return await page.content()
