#!/usr/bin/env python3
"""
apply_job.py — CLI entry point for job_applier.

Usage
-----
    python apply_job.py <URL> <CV_FILE> [options]

Examples
--------
    # Dry run (fill fields but don't submit)
    python apply_job.py https://company.com/apply resume.pdf

    # Auto-submit, show browser window
    python apply_job.py https://company.com/apply resume.pdf --yes --no-headless

    # Pass job description for a better cover letter
    python apply_job.py https://company.com/apply resume.pdf --yes --jd job.txt

    # Use Gemini instead of Claude
    AI_PROVIDER=gemini python apply_job.py https://company.com/apply resume.pdf
"""

import argparse
import logging
import sys
import os
from pathlib import Path

import dotenv

# Load .env before anything else so AI_PROVIDER / keys are available
dotenv.load_dotenv()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )
    # Silence noisy third-party loggers unless verbose
    if not verbose:
        for lib in ("selenium", "urllib3", "httpcore", "httpx", "WDM"):
            logging.getLogger(lib).setLevel(logging.WARNING)


def _check_env(provider: str) -> None:
    if provider == "gemini":
        if not os.getenv("GEMINI_API_KEY"):
            print("ERROR: GEMINI_API_KEY is not set in .env or environment.", file=sys.stderr)
            sys.exit(1)
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            print(
                "ERROR: ANTHROPIC_API_KEY is not set.\n" "  Set it in .env, or use Gemini: AI_PROVIDER=gemini",
                file=sys.stderr,
            )
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="apply_job.py",
        description="Automate job applications using AI + Selenium.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("url", help="Job application page URL")
    parser.add_argument("cv", help="Path to your CV/resume (.pdf or .docx)")

    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Auto-submit the form (default: fill only, do not submit)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window while running",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip submission verification (faster)",
    )
    parser.add_argument(
        "--no-cover-letter",
        action="store_true",
        help="Do not auto-generate a cover letter",
    )
    parser.add_argument(
        "--jd",
        metavar="FILE",
        help="Path to a plain-text job description file (improves cover letter)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=4.0,
        metavar="SECONDS",
        help="Seconds to wait after each page load (default: 4)",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "gemini"],
        default=None,
        help="AI provider override (default: reads AI_PROVIDER from .env, else anthropic)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Override AI_PROVIDER env var if --provider flag is set
    if args.provider:
        os.environ["AI_PROVIDER"] = args.provider

    provider = os.getenv("AI_PROVIDER", "anthropic").lower()
    _setup_logging(args.verbose)
    _check_env(provider)

    log = logging.getLogger(__name__)
    log.info("AI provider: %s", provider)

    # Validate CV path
    cv_path = Path(args.cv)
    if not cv_path.exists():
        print(f"ERROR: CV file not found: {args.cv}", file=sys.stderr)
        sys.exit(1)

    # Optional job description
    job_description = ""
    if args.jd:
        jd_path = Path(args.jd)
        if not jd_path.exists():
            print(f"ERROR: Job description file not found: {args.jd}", file=sys.stderr)
            sys.exit(1)
        job_description = jd_path.read_text(encoding="utf-8")
        log.info("Job description loaded (%d chars)", len(job_description))

    # Import here so logging is configured first
    from job_applier import apply

    log.info("Starting application: %s", args.url)
    result = apply(
        url=args.url,
        cv_path=str(cv_path.resolve()),
        auto_submit=args.yes,
        headless=not args.no_headless,
        wait_for_load=args.wait,
        generate_cover_letter=not args.no_cover_letter,
        job_description=job_description,
        verify=not args.no_verify,
    )

    # ── Final report ──────────────────────────────────────────────────────
    print()
    print("=" * 60)
    if result.success:
        print("✓  APPLICATION SUBMITTED SUCCESSFULLY")
    else:
        print("✗  APPLICATION NOT SUBMITTED")
    print(f"   Confidence : {result.confidence:.0%}")
    print(f"   Evidence   : {result.evidence}")
    print(f"   Final URL  : {result.final_url}")
    if result.error_messages:
        print("   Errors     :")
        for err in result.error_messages:
            print(f"     • {err}")
    print("=" * 60)

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
