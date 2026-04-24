# Project: Job Search Aggregator & Scraper

A robust, AI-powered job search scraper designed to handle diverse job boards through a decoupled "Adapter" architecture. It uses LLMs (Gemini/Claude) to automatically profile sites and generate scraping schemas.

## Project Overview

The project is divided into two main phases:
1.  **Profiling (Phase 1):** Uses `SiteProfiler` and `SchemaGenerator` (powered by Gemini) to analyze a job site's listings page. It identifies CSS selectors for job titles, companies, links, pagination, and filters, saving them as a `SiteAdapter` (JSON).
2.  **Scraping (Phase 2):** Uses `ScraperRunner` to execute the scraping logic based on a loaded `SiteAdapter`. It handles complex interactions like Playwright-based filtering, pagination (URL params, next button, infinite scroll), and detail URL resolution.

### Core Technologies
- **Python 3.x**
- **Playwright:** Browser automation for JS-heavy sites and bot-detection bypass.
- **BeautifulSoup4 & LXML:** Fast HTML parsing and field extraction.
- **Pydantic:** Strict data modeling for adapters and job listings.
- **Google Gemini (google-genai):** Primary LLM for generating site-specific scraping schemas.
- **Anthropic (Claude):** Fallback LLM for schema generation.
- **Deep Translator:** Automatic translation of search filters for multi-language support (e.g., Georgian).

## Building and Running

### Prerequisites
- Python 3.10+
- A `.env` file with `GEMINI_API_KEY` (and optionally `ANTHROPIC_API_KEY`).

### Installation
```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### Key Commands
- **Run Example Workflow:** `python example_usage.py`
  - This script demonstrates profiling a site, saving the adapter, and then running a filtered scrape.
- **Manage Adapters:** Adapters are stored in the `adapters/` directory as JSON files. They are considered stale after 30 days.

## Project Structure

- `scraper/`: Core logic
    - `models.py`: Pydantic schemas for `SiteAdapter`, `JobListing`, and `UserFilters`.
    - `runner.py`: The Playwright execution engine for scraping.
    - `profiler.py`: Logic for analyzing sites and generating adapters via AI.
    - `schema_generator.py`: LLM provider integrations (Gemini/Claude).
    - `adapter_store.py`: CRUD operations for JSON adapters.
    - `pagination.py`: Strategy-based pagination handlers.
    - `filter_match.py`: Fuzzy matching and translation for DOM-based filters.
- `adapters/`: Directory containing generated site configurations.
- `example_usage.py`: Entry point for common workflows.

## Development Conventions

- **Async/Await:** All I/O and browser interactions are asynchronous.
- **Schema-Driven:** Don't hardcode site-specific selectors in `runner.py`. Instead, update or refine the `SiteAdapter` schema in `models.py` and the generation logic in `profiler.py`.
- **Bot Detection:** `ScraperRunner` uses stealth-like settings (custom User-Agent, locale mapping, disabling automation flags) to avoid being blocked.
- **Type Safety:** Use Pydantic's `BaseModel` for all data structures to ensure consistency between the AI-generated adapters and the runner.
