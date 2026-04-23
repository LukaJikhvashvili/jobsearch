"""
example_usage.py — three workflows:

  1. Profile a new site (two-phase: listings → detail)
  2. Scrape jobs using a saved adapter
  3. Auto-refresh stale adapters
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
    SiteProfiler,
)
from scraper.models import UserFilters

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

store = AdapterStore(directory=Path("adapters"))


def _make_generator() -> SchemaGenerator:
    gemini_key = os.environ["GEMINI_API_KEY"]
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    return SchemaGenerator(
        primary=GeminiProvider(api_key=gemini_key),  # model from GEMINI_MODEL env var
        fallback=ClaudeProvider(api_key=anthropic_key) if anthropic_key else None,
    )


# ---------------------------------------------------------------------------
# Workflow 1: Generate a new adapter (two-phase)
# ---------------------------------------------------------------------------


async def generate_adapter(site_name: str, listings_url: str) -> None:
    print(f"\n── Profiling {site_name} ──")
    print("  Phase 1: analysing listings page …")

    profiler = SiteProfiler(generator=_make_generator(), headless=False)
    adapter = await profiler.profile(site_name, listings_url)

    print(f"  Nav type:     {adapter.listings.navigation.type}")
    print(f"  Pagination:   {adapter.listings.pagination.type}")
    print(f"  Apply method: {adapter.detail.application.method}")
    print(f"  Confidence:   {adapter.overall_confidence:.0%}")

    if adapter.overall_confidence < 0.60:
        print("  ⚠  Low confidence — review before using in production")

    store.save(adapter)
    print(f"  Saved → adapters/{site_name}.json")


# ---------------------------------------------------------------------------
# Workflow 2: Scrape jobs
# ---------------------------------------------------------------------------


async def scrape_jobs(site_name: str, filters: UserFilters = None, enrich: bool = True) -> list[JobListing]:
    adapter = store.load(site_name)
    if adapter is None:
        raise ValueError(f"No adapter for '{site_name}'. Run generate_adapter() first.")
    if adapter.is_stale:
        print(f"⚠  Adapter for {site_name} is stale — consider re-profiling")

    jobs: list[JobListing] = []
    print(f"\n── Scraping {site_name} ──")

    async with ScraperRunner(adapter, headless=False) as runner:
        async for job in runner.run(filters=filters, enrich=enrich):
            jobs.append(job)
            print(
                f"  [{len(jobs):>3}] {job.title or '?':<45} "
                f"{job.company or '?':<25} "
                f"{job.application_method.value if job.application_method else '?'}"
            )

    print(f"\n  Total: {len(jobs)} jobs from {site_name}")
    return jobs


# ---------------------------------------------------------------------------
# Workflow 3: Refresh stale adapters
# ---------------------------------------------------------------------------


async def refresh_stale(sites: dict[str, str]) -> None:
    for site_name, listings_url in sites.items():
        if store.needs_refresh(site_name):
            await generate_adapter(site_name, listings_url)
        else:
            print(f"  {site_name}: adapter up to date")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SITES = {
    "jobs.ge": "https://jobs.ge/",
    "hr.ge": "https://www.hr.ge/search-posting",
    "awork.ge": "https://awork.ge/user/vacancy/",
    # "myjobs.ge": "https://myjobs.ge/ka/vacancy",
}


async def main():
    for site_name, listings_url in SITES.items():
        if store.needs_refresh(site_name):
            await generate_adapter(site_name, listings_url)

    filters = UserFilters(location="თბილისი", keyword="ანალიტიკოსი")

    all_jobs: list[JobListing] = []
    for site_name in SITES:
        jobs = await scrape_jobs(site_name, filters=filters, enrich=False)
        all_jobs.extend(jobs)

    print(f"\n{'─'*60}")
    print(f"Total: {len(all_jobs)} jobs")
    by_method: dict[str, int] = {}
    for j in all_jobs:
        m = j.application_method.value if j.application_method else "unknown"
        by_method[m] = by_method.get(m, 0) + 1
    for method, count in sorted(by_method.items(), key=lambda x: -x[1]):
        print(f"  {method:<20} {count}")


if __name__ == "__main__":
    asyncio.run(main())
