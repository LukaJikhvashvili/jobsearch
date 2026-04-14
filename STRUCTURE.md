# AI Job Application Assistant — Project Structure

```
job_assistant/
│
├── config.py                  # All settings: API keys, paths, delays, model names, site configs
├── gemini_helper.py           # Gemini client wrapper, structured prompts, rate limiting, token tracking
│
├── browser/
│   ├── __init__.py
│   ├── launcher.py            # Playwright context with stealth, persistent sessions, human-like behavior
│   └── actions.py             # Reusable human-like actions: scroll, click, type, wait, hover
│
├── scraper/
│   ├── __init__.py
│   ├── page_cleaner.py        # Crawl4AI integration: fetch + clean page → Markdown/structured text
│   ├── job_scanner.py         # Scan search results pages → extract job list (LLM-powered, no selectors)
│   └── job_detail.py          # Navigate to job detail page → extract full description as JSON
│
├── cv/
│   ├── __init__.py
│   ├── cv_parser.py           # Parse base CV PDF with PyMuPDF → clean text + structured sections
│   ├── cv_tailor.py           # Gemini Pro: rewrite CV sections to match job, keyword inject
│   └── pdf_generator.py       # ReportLab: render tailored CV text → ATS-friendly PDF
│
├── matching/
│   ├── __init__.py
│   └── scorer.py              # Gemini Flash: score job vs CV, return match%, gaps, highlights
│
├── application/
│   ├── __init__.py
│   ├── form_filler.py         # LLM-guided: detect form fields, map profile → fields, Playwright fill
│   └── submitter.py           # Human-in-the-loop: show preview, wait for user confirm, then submit
│
├── database/
│   ├── __init__.py
│   └── db.py                  # SQLite: jobs, applications, cv_versions, user_profile, logs tables
│
├── profiles/
│   └── user_profile.json      # Stored: name, email, phone, links, answers to common questions
│
├── assets/
│   ├── base_cv.pdf            # User's original CV (uploaded once)
│   └── tailored/              # Auto-generated tailored CVs per job application
│
├── logs/
│   └── app.log                # Rotating file log
│
├── main.py                    # CLI entry point: interactive menu to scan / apply / review
├── requirements.txt           # All dependencies pinned
└── .env                       # GEMINI_API_KEY, etc. (never commit)
```

## File Responsibilities at a Glance

| File                         | Purpose                     | Gemini Calls      |
| ---------------------------- | --------------------------- | ----------------- |
| `config.py`                  | Central config, no logic    | 0                 |
| `gemini_helper.py`           | Client, rate limit, prompts | N/A (wrapper)     |
| `browser/launcher.py`        | Stealth Playwright context  | 0                 |
| `browser/actions.py`         | Human-like clicks/scrolls   | 0                 |
| `scraper/page_cleaner.py`    | Crawl4AI → Markdown         | 0                 |
| `scraper/job_scanner.py`     | Job list from results page  | 1 per page        |
| `scraper/job_detail.py`      | Full job details            | 1 per job         |
| `matching/scorer.py`         | Match score + gaps          | 1 per job         |
| `cv/cv_tailor.py`            | Rewrite CV for job          | 1 per application |
| `cv/pdf_generator.py`        | Render PDF                  | 0                 |
| `application/form_filler.py` | Detect + fill form          | 1 per form        |
| `application/submitter.py`   | Preview + confirm           | 0                 |
| `database/db.py`             | Persist everything          | 0                 |

**Target: ≤ 4 Gemini calls per job application end-to-end**
