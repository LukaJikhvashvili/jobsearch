"""
example_usage.py — three workflows:

  1. Profile a new site and generate its adapter (run once per site)
  2. Scrape jobs using an existing adapter
  3. Re-profile a stale adapter automatically
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from scraper import (
    AdapterStore,
    ClaudeProvider,
    GeminiProvider,
    JobListing,
    ScraperRunner,
    SchemaGenerator,
    capture_site_html,
)

load_dotenv()  # Load API keys from .env file
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # optional fallback

store = AdapterStore(directory=Path("adapters"))


# ---------------------------------------------------------------------------
# Workflow 1: Generate a new adapter for a site
# ---------------------------------------------------------------------------


async def generate_adapter(listings_url: str, site_name: str) -> None:
    """
    Profile the site, generate a SiteAdapter via AI, validate and save it.
    Run this once per site (or when the adapter goes stale).
    """
    print(f"\n── Profiling {site_name} ──")

    # Step 1: capture real HTML from the live site
    listings_html, detail_html = await capture_site_html(listings_url)
    print(f"  Listings HTML: {len(listings_html):,} chars")
    print(f"  Detail HTML:   {len(detail_html):,} chars")

    # Step 2: ask the AI to generate the adapter
    generator = SchemaGenerator(
        primary=GeminiProvider(api_key=GEMINI_API_KEY, model="gemini-2.0-flash"),
        fallback=ClaudeProvider(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None,
    )
    adapter = generator.generate(
        site=site_name,
        listings_url=listings_url,
        listings_html=listings_html,
        detail_html=detail_html,
    )

    print(f"  Confidence:    {adapter.overall_confidence:.0%}")
    print(f"  Pagination:    {adapter.listings.pagination.type}")
    print(f"  Apply method:  {adapter.detail.application.method}")

    if adapter.overall_confidence < 0.60:
        print("  ⚠  Low confidence — review adapter before using it in production")

    # Step 3: save
    store.save(adapter)
    print(f"  Saved → adapters/{site_name}.json")


# ---------------------------------------------------------------------------
# Workflow 2: Scrape jobs using a saved adapter
# ---------------------------------------------------------------------------


async def scrape_jobs(site_name: str, enrich: bool = True) -> list[JobListing]:
    """
    Load a saved adapter and scrape jobs. Returns a list of JobListing objects.
    """
    adapter = store.load(site_name)
    if adapter is None:
        raise ValueError(f"No adapter for '{site_name}'. Run generate_adapter() first.")

    if adapter.is_stale:
        print(f"⚠  Adapter for {site_name} is stale — consider re-profiling")

    jobs: list[JobListing] = []
    print(f"\n── Scraping {site_name} ──")

    async with ScraperRunner(adapter, headless=True) as runner:
        async for job in runner.run(enrich=enrich):
            jobs.append(job)
            print(
                f"  [{len(jobs):>3}] {job.title or '?':<45} "
                f"{job.company or '?':<25} "
                f"{(job.application_method.value if job.application_method else '?')}"
            )

    print(f"\n  Total: {len(jobs)} jobs scraped from {site_name}")
    return jobs


# ---------------------------------------------------------------------------
# Workflow 3: Auto-refresh stale adapters
# ---------------------------------------------------------------------------


async def refresh_stale(sites: dict[str, str]) -> None:
    """
    sites: {site_name: listings_url}
    Re-profiles any site whose adapter is missing or stale.
    """
    for site_name, listings_url in sites.items():
        if store.needs_refresh(site_name):
            print(f"\nRefreshing stale adapter for {site_name} …")
            await generate_adapter(listings_url, site_name)
        else:
            print(f"  {site_name}: adapter up to date, skipping")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SITES = {
    "jobs.ge": "https://jobs.ge/en/",
    "hr.ge": "https://hr.ge",
}


async def main():
    # First run: generate adapters for each site
    for site_name, listings_url in SITES.items():
        if store.needs_refresh(site_name):
            await generate_adapter(listings_url, site_name)

    # Scrape all sites
    all_jobs: list[JobListing] = []
    for site_name in SITES:
        jobs = await scrape_jobs(site_name, enrich=True)
        all_jobs.extend(jobs)

    # Summary
    print(f"\n{'─'*60}")
    print(f"Total jobs collected: {len(all_jobs)}")
    by_method = {}
    for j in all_jobs:
        m = j.application_method.value if j.application_method else "unknown"
        by_method[m] = by_method.get(m, 0) + 1
    for method, count in sorted(by_method.items(), key=lambda x: -x[1]):
        print(f"  {method:<20} {count}")


if __name__ == "__main__":
    asyncio.run(main())
