"""
test_scan.py — Quick integration test for Steps 1 & 2.

Run this to verify:
  ✓ Gemini client initialises and responds
  ✓ Playwright browser launches with stealth
  ✓ Crawl4AI cleans a real page
  ✓ Job listings are extracted as structured JSON

Usage:
    python test_scan.py

Expected output (example):
    ✓ Gemini OK — flash daily remaining: 1398
    ✓ Browser launched
    ✓ Navigated to jobs.ge
    ✓ Crawl4AI cleaned: 4821 chars
    ✓ Gemini extracted 12 jobs from Jobs.ge

    Sample job #1:
      Title:   Python Developer
      Company: Techsolutions LLC
      Location: Tbilisi
      Link:    https://jobs.ge/vacancy/12345
      Score:   N/A (not scored yet)
"""

import asyncio
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_scan")


async def main():
    # ----------------------------------------------------------------
    # 1. Test Gemini client init
    # ----------------------------------------------------------------
    print("\n--- Step 1: Gemini init ---")
    from gemini_helper import GeminiHelper
    g = GeminiHelper()
    print(f"✓ Gemini OK — flash daily remaining: {g.flash_daily_remaining}")

    # ----------------------------------------------------------------
    # 2. Test browser launch
    # ----------------------------------------------------------------
    print("\n--- Step 2: Browser launch ---")
    from browser.launcher import BrowserManager, HumanActions
    bm = BrowserManager()
    await bm.start()
    print("✓ Browser launched")

    # ----------------------------------------------------------------
    # 3. Test page navigation + Crawl4AI cleaning
    # ----------------------------------------------------------------
    print("\n--- Step 3: Navigation + Crawl4AI ---")
    from scraper.page_cleaner import PageCleaner

    cleaner = PageCleaner()
    page = await bm.new_page("jobs_ge")

    test_url = "https://jobs.ge/?q=python&l="
    success = await HumanActions.safe_goto(page, test_url)

    if not success:
        print("✗ Navigation failed — check your internet connection.")
        await bm.stop()
        return

    print(f"✓ Navigated to {test_url}")
    await asyncio.sleep(2)

    html = await HumanActions.get_page_html(page)
    markdown = await cleaner.clean_for_job_listing(html, url=test_url)

    print(f"✓ Crawl4AI cleaned: {len(html)} html → {len(markdown)} chars markdown")
    if len(markdown) < 100:
        print("⚠ Warning: markdown output is very short — the site may have changed.")
        print("  First 500 chars of markdown:")
        print(markdown[:500])

    await bm.close_page(page)

    # ----------------------------------------------------------------
    # 4. Test Gemini job extraction
    # ----------------------------------------------------------------
    print("\n--- Step 4: Gemini job extraction ---")
    from scraper.job_scanner import JobScanner

    scanner = JobScanner(bm, g, cleaner)

    jobs = await scanner.scan(
        site_name="jobs_ge",
        query="python developer",
        location="Tbilisi",
        max_pages=1,   # just 1 page for the test
    )

    print(f"✓ Extracted {len(jobs)} jobs from Jobs.ge")

    if jobs:
        job = jobs[0]
        print(f"\nSample job #1:")
        print(f"  Title:   {job.title}")
        print(f"  Company: {job.company}")
        print(f"  Location:{job.location}")
        print(f"  Link:    {job.link}")
        print(f"  Salary:  {job.salary or 'not listed'}")
        if job.tags:
            print(f"  Tags:    {', '.join(job.tags)}")

        # Save all jobs to a JSON file for inspection
        with open("test_jobs_output.json", "w", encoding="utf-8") as f:
            json.dump(
                [j.model_dump() for j in jobs],
                f,
                indent=2,
                ensure_ascii=False,
            )
        print(f"\n✓ Full results saved to test_jobs_output.json")

    # ----------------------------------------------------------------
    # 5. Usage summary
    # ----------------------------------------------------------------
    g.log_usage_summary()

    await bm.stop()
    print("\n✅ All steps passed.")


if __name__ == "__main__":
    asyncio.run(main())
