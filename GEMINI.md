# Job Scraper Engine

This project is a sophisticated web scraping engine designed to crawl, filter, and extract job listing data from various career portals. It uses a metadata-driven approach where "adapters" (JSON configurations) define how to navigate, filter, and parse specific websites.

## Core Architecture

- **`scraper/runner.py`**: The main execution engine. It uses Playwright to handle browser interactions (filtering, pagination, clicking) and BeautifulSoup for HTML parsing.
- **`scraper/models.py`**: Contains the Pydantic data models that define the structure of adapters, configuration, and scraped data.
- **`adapters/`**: Directory containing JSON configuration files (e.g., `jobs_ge.json`) which define the specific DOM selectors, navigation strategies, and filtering mechanisms for target websites.

## Technologies

- **Language**: Python
- **Automation**: Playwright (for dynamic, JS-heavy sites)
- **Parsing**: BeautifulSoup4 (with `lxml`)
- **Data Validation**: Pydantic
- **AI Integration**: Google Gemini and Anthropic (Claude) for intelligent adapter generation and data parsing.

## Building and Running

Ensure you have a virtual environment set up and the necessary dependencies installed:

```bash
# Install dependencies
pip install -r requirements.txt
```

### Running Scrapes

The engine is modular. The primary entry point for execution is the `ScraperRunner` class.

Example usage is provided in `example_usage.py`:
```bash
python example_usage.py
```

## Development Conventions

- **Adapters**: New websites should be added as JSON files in the `adapters/` directory following the schema defined in `scraper/models.py`.
- **Filtering**: The engine supports both URL-based parameter filtering and DOM-based interaction (e.g., clicking dropdowns, filling search fields).
- **Enrichment**: Scrapers operate in two passes:
  1.  **Listings pass**: Scrapes the listing page for basic info (title, company, URL).
  2.  **Detail pass**: Visits individual job URLs to enrich data (description, requirements, salary, location, application method).
- **Safety**: The `ScraperRunner` includes built-in safeguards like user-agent configuration, disabling unnecessary resource loading (images/fonts), and `no-sandbox` flags for container environments.
