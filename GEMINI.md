# JobSearch Scraper

An AI-powered autonomous job scraper that leverages Gemini to generate site-specific scraping adapters. It uses Playwright for browser automation and BeautifulSoup for extraction, allowing it to handle both static and dynamic (JS-heavy) job boards.

## Project Overview

The project is designed to solve the "fragile scraper" problem by using LLMs to analyze site structures and generate JSON-based `SiteAdapter` configurations. These adapters describe how to find job listings, handle pagination, navigate to detail pages, and extract structured data.

### Core Components

- **`scraper/runner.py`**: The execution engine that uses a `SiteAdapter` to crawl sites and extract `JobListing` objects.
- **`scraper/profiler.py`**: A two-phase orchestrator that visits a site, captures its HTML, and uses the `SchemaGenerator` to build a new adapter.
- **`scraper/schema_generator.py`**: The AI logic layer. It cleans HTML and prompts Gemini (primary) or Claude (fallback) to generate the scraping schema.
- **`scraper/models.py`**: Pydantic models defining the `SiteAdapter` schema and `JobListing` structure.
- **`scraper/html_cleaner.py`**: Utility to strip noisy HTML (scripts, styles, etc.) to keep AI prompts efficient.
- **`adapters/`**: A directory for storing generated JSON adapters.

## Getting Started

### Prerequisites

- Python 3.10+
- A Google Gemini API Key (`GEMINI_API_KEY`)
- (Optional) An Anthropic API Key (`ANTHROPIC_API_KEY`) for fallbacks.

### Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### Configuration

Create a `.env` file in the root directory:

```env
GEMINI_API_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here_optional
GEMINI_MODEL=gemini-2.0-flash  # Optional, defaults to flash
```

### Usage

The `example_usage.py` script demonstrates the three main workflows:

1.  **Profiling**: Generate a new adapter for a site.
2.  **Scraping**: Use an existing adapter to extract jobs.
3.  **Maintenance**: Check if adapters are stale and refresh them.

```bash
python example_usage.py
```

## Development Conventions

### Schema-First Design
The scraping logic is entirely driven by the `SiteAdapter` model in `scraper/models.py`. Any changes to the scraping capabilities (e.g., new pagination types) should start by updating the Pydantic models.

### Two-Phase Profiling
- **Phase 1 (Listings)**: Identifies the job card container, basic fields (title, company), pagination strategy, and navigation method to reach the detail page.
- **Phase 2 (Detail)**: Analyzes a single job's detail page to extract description, requirements, and application methods (email, ATS redirect, form).

### Extraction Strategy
The `ScraperRunner` supports several navigation types:
- `direct_link`: Standard `<a>` tags.
- `card_click`: JS-driven clicks on the entire card.
- `button_click`: JS-driven clicks on specific buttons.
- `data_attr`: URLs stored in data attributes.

### Heuristics & AI
The project uses a hybrid approach. It prefers AI-generated selectors but falls back to robust heuristics (e.g., `mailto:` detection, ATS domain matching) when AI confidence is low.
