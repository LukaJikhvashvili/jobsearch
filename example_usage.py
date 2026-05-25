"""
example_usage.py — three workflows:

  1. Profile a new site (two-phase: listings → detail)
  2. Scrape jobs using a saved adapter
  3. Auto-refresh stale adapters

Demonstrates both:
  - Legacy direct instantiation (backward-compatible)
  - New container-based approach (recommended)
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

from scraper import (
    ClaudeProvider,
    GeminiProvider,
    JobListing,
    SchemaGenerator,
    SiteProfiler,
    ScraperConfig,
    ScraperContainer,
    TelemetryCollector,
)
from scraper.events import Events
from scraper.models import UserFilters

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

# ---------------------------------------------------------------------------
# Container-based setup (recommended)
# ---------------------------------------------------------------------------

config = ScraperConfig.from_env()
container = ScraperContainer(config)
store = container.get_adapter_store()

# ---------------------------------------------------------------------------
# Telemetry setup
# ---------------------------------------------------------------------------

telemetry = TelemetryCollector()

# Optional: register a callback for real-time span logging
telemetry.on_span(lambda span: print(
    f"  [telemetry] {span.operation}  {span.site}  "
    f"{'OK' if span.success else 'FAIL'}  {span.duration_s:.2f}s"
))

# Subscribe to events for logging
container.event_bus.subscribe(
    Events.ADAPTER_GENERATION_COMPLETED,
    lambda event_type, data: print(f"  Adapter generated in {data.get('duration', 0):.1f}s"),
)
container.event_bus.subscribe(
    Events.SCRAPING_PAGE_COMPLETED,
    lambda event_type, data: print(f"  Page scraped: {data.get('url', '?')}"),
)


# ---------------------------------------------------------------------------
# Workflow 1: Generate a new adapter (pipeline-based)
# ---------------------------------------------------------------------------


async def generate_adapter(site_name: str, listings_url: str) -> None:
    print(f"\n── Profiling {site_name} ──")
    print("  Generating adapter via pipeline …")

    pipeline = container.get_pipeline()
    adapter = await pipeline.execute(site_name, listings_url)

    print(f"  Nav type:     {adapter.listings.navigation.type}")
    print(f"  Pagination:   {adapter.listings.pagination.type}")
    store.save(adapter)
    print(f"  Saved → adapters/{site_name}.json")


# ---------------------------------------------------------------------------
# Workflow 1b: Legacy direct instantiation (still works)
# ---------------------------------------------------------------------------


def _make_generator() -> SchemaGenerator:
    """Legacy helper — direct instantiation without container."""
    gemini_key = os.environ["GEMINI_API_KEY"]
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    return SchemaGenerator(
        primary=GeminiProvider(api_key=gemini_key),
        fallback=ClaudeProvider(api_key=anthropic_key) if anthropic_key else None,
    )


async def generate_adapter_legacy(site_name: str, listings_url: str) -> None:
    """Legacy workflow — still fully supported."""
    print(f"\n── Profiling {site_name} (legacy) ──")
    profiler = SiteProfiler(generator=_make_generator(), headless=False, telemetry=telemetry)
    adapter = await profiler.profile(site_name, listings_url)
    store.save(adapter)
    print(f"  Saved → adapters/{site_name}.json")


# ---------------------------------------------------------------------------
# Workflow 2: Scrape jobs (container-based)
# ---------------------------------------------------------------------------


async def scrape_jobs(site_name: str, filters: UserFilters = None) -> list[JobListing]:
    adapter = store.load(site_name)
    if adapter is None:
        raise ValueError(f"No adapter for '{site_name}'. Run generate_adapter() first.")
    if adapter.is_stale:
        print(f"⚠  Adapter for {site_name} is stale — consider re-profiling")

    jobs: list[JobListing] = []
    print(f"\n── Scraping {site_name} ──")

    runner = container.get_runner(adapter)
    async with runner:
        async for job in runner.run(filters=filters):
            jobs.append(job)
            print(f"  [{len(jobs):>3}] {job.title or '?':<45} " f"{job.company or '?':<25} ")

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
    "myjobs.ge": "https://myjobs.ge/ka/vacancy",
}


async def main():
    for site_name, listings_url in SITES.items():
        if store.needs_refresh(site_name):
            await generate_adapter(site_name, listings_url)

    filters = UserFilters(location="Tbilisi", keyword="analyst", date_posted="last week")

    all_jobs: list[JobListing] = []
    for site_name in SITES:
        jobs = await scrape_jobs(site_name, filters=filters)
        all_jobs.extend(jobs)

    print(f"\n{'─'*60}")
    print(f"Total: {len(all_jobs)} jobs")

    # Print telemetry summary
    summary = telemetry.get_summary()
    print(f"\n── Telemetry Summary ──")
    print(f"  Operations: {summary['total_spans']}  "
          f"Failed: {summary['failed_spans']}  "
          f"Success rate: {summary['success_rate']:.0%}")
    for name, total in summary['metric_totals'].items():
        print(f"  {name}: {total}")


if __name__ == "__main__":
    asyncio.run(main())
