"""
CV parsing module for job_applier.

Functions
---------
extract_cv_text(file_path)  →  raw text
parse_cv_data(cv_text)      →  CVData
load_cv(file_path)          →  CVData   (convenience wrapper)
"""

from __future__ import annotations

import logging
from pathlib import Path

from .ai_client import ai_json
from .models import CVData

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def extract_cv_text(file_path: str) -> str:
    """
    Extract plain text from a PDF or DOCX resume/CV file.

    Parameters
    ----------
    file_path : str | Path
        Absolute or relative path to the file.

    Returns
    -------
    str
        Extracted plain text (may include blank lines).

    Raises
    ------
    ValueError
        If the file extension is not .pdf or .docx.
    FileNotFoundError
        If the file does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"CV file not found: {file_path}")

    ext = path.suffix.lower()

    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages)

    if ext == ".docx":
        import docx as python_docx

        doc = python_docx.Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs]
        # Also pull text from tables
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    paragraphs.append(cell.text)
        return "\n".join(paragraphs)

    raise ValueError(f"Unsupported CV format '{ext}'. Provide a .pdf or .docx file.")


# ---------------------------------------------------------------------------
# Structured data extraction
# ---------------------------------------------------------------------------

_CV_EXTRACTION_SCHEMA = """
{
  "full_name":           "<string>",
  "email":               "<string>",
  "phone_number":        "<string>",
  "linkedin_url":        "<string>",
  "github_url":          "<string>",
  "portfolio_url":       "<string>",
  "current_role":        "<most recent job title>",
  "location":            "<city, country if present>",
  "summary":             "<2-4 sentence professional summary>",
  "skills":              "<comma-separated list of technical/professional skills>",
  "years_of_experience": "<total years as a number or range, e.g. '5' or '5-7'>",
  "education":           "<most recent degree, institution, year>"
}
"""


def parse_cv_data(cv_text: str) -> CVData:
    """
    Use the configured AI to extract structured personal data from raw CV text.

    Parameters
    ----------
    cv_text : str
        Plain text extracted from the CV file.

    Returns
    -------
    CVData
        Populated dataclass. Missing fields are left as empty strings.
    """
    prompt = f"""You are a CV parser. Extract structured personal and professional data
from the CV text below. Return ONLY a JSON object matching this schema exactly
(use empty string "" for any field not found):

SCHEMA:
{_CV_EXTRACTION_SCHEMA}

CV TEXT:
{cv_text}
"""
    try:
        raw = ai_json(prompt, max_tokens=1024)
        cv = CVData.from_dict(raw)
        log.info("CV parsed — name=%r role=%r", cv.full_name, cv.current_role)
        return cv
    except Exception as exc:
        log.error("Failed to parse CV data: %s", exc)
        return CVData()


def generate_cover_letter(cv: CVData, job_url: str = "", job_description: str = "") -> str:
    """
    Generate a concise cover letter tailored to the CV and optional job description.

    Parameters
    ----------
    cv : CVData
        Parsed applicant data.
    job_url : str, optional
        URL of the job posting (for context label only).
    job_description : str, optional
        Raw job description text if available.

    Returns
    -------
    str
        3-4 paragraph cover letter (plain text, no markdown).
    """
    jd_block = f"\n\nJOB DESCRIPTION:\n{job_description}" if job_description else ""
    prompt = f"""Write a concise, professional cover letter (3 paragraphs, no fluff) for the
applicant below applying to a job{' at ' + job_url if job_url else ''}.
Use a warm but professional tone. Do NOT use markdown or bullet points.

APPLICANT:
{cv.to_prompt_block()}{jd_block}
"""
    try:
        letter = ai_json.__func__ if False else get_ai_client_text(prompt)  # plain text call
        return letter.strip()
    except Exception as exc:
        log.warning("Cover letter generation failed: %s", exc)
        return (
            f"Dear Hiring Manager,\n\nI am writing to express my interest in this position. "
            f"With my background as {cv.current_role or 'a professional'} and skills in "
            f"{cv.skills or 'my field'}, I believe I would be a strong fit.\n\n"
            f"I look forward to discussing how I can contribute to your team.\n\n"
            f"Sincerely,\n{cv.full_name}"
        )


def get_ai_client_text(prompt: str) -> str:
    """Helper to call ai_complete without circular import."""
    from .ai_client import ai_complete

    return ai_complete(prompt, max_tokens=600)


def load_cv(file_path: str, generate_cover_letter_flag: bool = True) -> CVData:
    """
    End-to-end helper: extract text from file, parse with AI, optionally draft a cover letter.

    Parameters
    ----------
    file_path : str
        Path to .pdf or .docx resume.
    generate_cover_letter_flag : bool
        If True (default), a cover letter is auto-generated and stored in cv.cover_letter.

    Returns
    -------
    CVData
        Fully populated CVData instance.
    """
    log.info("Extracting text from %s", file_path)
    text = extract_cv_text(file_path)

    log.info("Parsing CV data with AI…")
    cv = parse_cv_data(text)

    if generate_cover_letter_flag and not cv.cover_letter:
        log.info("Generating cover letter…")
        cv.cover_letter = generate_cover_letter(cv)

    return cv
