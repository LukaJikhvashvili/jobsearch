# JobSearch Scraper Project

This project is an automated job board scraper designed to navigate, filter, and extract job listings from various career sites. It uses Playwright for browser automation and BeautifulSoup for parsing HTML.

## Project Overview

- **Core Engine:** Uses `playwright` for headless browser interaction and `beautifulsoup4` for HTML extraction.
- **Data Models:** Uses `pydantic` for structured job listing data.
- **AI Integration:** Includes support for `google-genai` (Gemini) and `anthropic` (Claude) to assist in parsing or enrichment.
- **Architecture:** The scraper is built around a `SiteAdapter` pattern, where specific site behavior (selectors, navigation, filters) is defined in JSON configuration files located in the `adapters/` directory.

## Getting Started

### Prerequisites

- Python 3.10+
- `pip`
- Playwright browsers (installed via `playwright install`)

### Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Running the Scraper

The scraper is designed to be used by creating an instance of `ScraperRunner` with a specific site adapter.

An example of usage is provided in `example_usage.py`.

## Directory Structure

- `scraper/`: Core engine source code.
  - `runner.py`: The main `ScraperRunner` class.
  - `models.py`: Pydantic data models for job listings and site configurations.
  - `html_cleaner.py`: Utility for cleaning HTML content.
  - `pagination.py`: Logic for handling different pagination styles.
  - `schema_generator.py`: Utilities for generating or validating schemas.
- `adapters/`: JSON configuration files for specific job sites.
- `example_usage.py`: Example entry point demonstrating how to run a scraper for a specific site.

## Development Conventions

- **Adapters:** New site support should be added by creating a new JSON file in `adapters/` that follows the schema defined in `scraper/models.py`.
- **Async/Await:** The project relies heavily on `asyncio` for Playwright operations.
- **Type Safety:** Use Pydantic models for data interchange between components.
