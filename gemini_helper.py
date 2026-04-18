"""
gemini_helper.py — Gemini API wrapper for the AI Job Application Assistant.

Responsibilities:
  - Single shared Gemini client (Flash + Pro models).
  - Rate-limit enforcement (RPM + RPD counters).
  - All prompt templates in one place — edit prompts here, never in business logic.
  - Structured JSON output with Pydantic validation.
  - Token/cost tracking for staying within free tier.

Usage:
    from gemini_helper import GeminiHelper, JobListing, JobDetail, MatchResult
    g = GeminiHelper()
    jobs = await g.extract_job_listings(page_markdown)
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Optional
from textwrap import dedent

from google import genai
from google.genai import types

from pydantic import BaseModel, Field, field_validator

from config import (
    GEMINI_API_KEY,
    GEMINI_FLASH_MODEL,
    GEMINI_PRO_MODEL,
    GEMINI_FLASH_RPM,
    GEMINI_FLASH_RPD,
    GEMINI_PRO_RPM,
    GEMINI_PRO_RPD,
    GEMINI_MAX_INPUT_CHARS,
    GEMINI_MAX_OUTPUT_TOKENS,
    DELAYS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic Models — structured output shapes
# Every Gemini call that returns JSON is validated against one of these.
# ---------------------------------------------------------------------------


class JobListing(BaseModel):
    """One job card from a search results page."""

    title: str
    company: str
    location: str
    link: str  # absolute URL or relative path
    salary: Optional[str] = None  # None if not shown
    short_description: Optional[str] = None
    posted_date: Optional[str] = None  # raw string, e.g. "2 days ago"
    tags: list[str] = Field(default_factory=list)  # e.g. ["remote", "python"]

    @field_validator("link")
    @classmethod
    def strip_tracking(cls, v: str) -> str:
        """Remove common tracking params to keep URLs clean."""
        for param in ["?trk=", "&trk=", "?utm_", "&utm_"]:
            if param in v:
                v = v.split(param)[0]
        return v.strip()


class JobListingsPage(BaseModel):
    """Container for all jobs on one results page."""

    jobs: list[JobListing]
    has_next_page: bool = False
    next_page_hint: Optional[str] = None  # text of the "next" button/link if found


class JobDetail(BaseModel):
    """Full parsed job description from a detail page."""

    title: str
    company: str
    location: str
    salary: Optional[str] = None
    employment_type: Optional[str] = None  # full-time, contract, etc.
    experience_required: Optional[str] = None  # e.g. "3+ years"
    skills_required: list[str] = Field(default_factory=list)
    skills_preferred: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)
    about_company: Optional[str] = None
    application_deadline: Optional[str] = None
    remote_policy: Optional[str] = None  # remote / hybrid / on-site
    apply_link: Optional[str] = None
    raw_description: str = ""  # full cleaned text, kept for CV tailoring


class MatchResult(BaseModel):
    """Output of comparing a CV against a job description."""

    score: int = Field(ge=0, le=100, description="0-100 fit score")
    verdict: str  # e.g. "Strong match", "Partial match", "Weak match"
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    missing_experience: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    recommendation: str  # short paragraph: apply / skip / apply with note


class TailoredCV(BaseModel):
    """Structured tailored CV sections, ready for PDF generation."""

    # Each field maps to a section in the output PDF
    summary: str
    experience: list[dict]  # [{role, company, dates, bullets: [...]}]
    skills: list[str]
    education: list[dict]  # [{degree, school, dates, details}]
    certifications: list[str] = Field(default_factory=list)
    projects: list[dict] = Field(default_factory=list)
    keywords_injected: list[str] = Field(default_factory=list)  # for logging


class FormField(BaseModel):
    """One field detected on an application form."""

    field_id: str  # best guess at input name/id/label
    label: str  # human-readable label
    field_type: str  # text, email, phone, textarea, select, file, checkbox
    required: bool = False
    options: list[str] = Field(default_factory=list)  # for select fields
    suggested_value: Optional[str] = None  # from user profile


class FormAnalysis(BaseModel):
    """Full analysis of an application form page."""

    fields: list[FormField]
    has_cv_upload: bool = False
    cv_upload_selector_hint: Optional[str] = None  # text near the upload button
    has_cover_letter: bool = False
    notes: str = ""  # any unusual requirements detected


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------


class _RateLimiter:
    """
    Sliding-window rate limiter for Gemini free tier.
    Tracks calls within the last 60 seconds (RPM) and last 24 hours (RPD).
    """

    def __init__(self, rpm: int, rpd: int, name: str):
        self.rpm = rpm
        self.rpd = rpd
        self.name = name
        self._minute_calls: deque[float] = deque()
        self._day_calls: deque[float] = deque()

    async def acquire(self) -> None:
        """Block until a call slot is available."""
        while True:
            now = time.time()

            # Purge stale timestamps
            while self._minute_calls and now - self._minute_calls[0] > 60:
                self._minute_calls.popleft()
            while self._day_calls and now - self._day_calls[0] > 86400:
                self._day_calls.popleft()

            if len(self._day_calls) >= self.rpd:
                wait = 86400 - (now - self._day_calls[0]) + 1
                logger.warning(f"[{self.name}] Daily limit reached. Waiting {wait:.0f}s.")
                await asyncio.sleep(wait)
                continue

            if len(self._minute_calls) >= self.rpm:
                wait = 60 - (now - self._minute_calls[0]) + 1
                logger.info(f"[{self.name}] RPM limit reached. Waiting {wait:.1f}s.")
                await asyncio.sleep(wait)
                continue

            # Slot available
            self._minute_calls.append(now)
            self._day_calls.append(now)
            return

    @property
    def daily_remaining(self) -> int:
        now = time.time()
        while self._day_calls and now - self._day_calls[0] > 86400:
            self._day_calls.popleft()
        return self.rpd - len(self._day_calls)


# ---------------------------------------------------------------------------
# Token Usage Tracker
# ---------------------------------------------------------------------------


class _UsageTracker:
    def __init__(self):
        self.flash_calls = 0
        self.flash_input_tokens = 0
        self.flash_output_tokens = 0
        self.pro_calls = 0
        self.pro_input_tokens = 0
        self.pro_output_tokens = 0

    def record(self, model: str, input_tok: int, output_tok: int) -> None:
        if "flash" in model.lower():
            self.flash_calls += 1
            self.flash_input_tokens += input_tok
            self.flash_output_tokens += output_tok
        else:
            self.pro_calls += 1
            self.pro_input_tokens += input_tok
            self.pro_output_tokens += output_tok

    def summary(self) -> str:
        return (
            f"Flash: {self.flash_calls} calls | "
            f"{self.flash_input_tokens}→{self.flash_output_tokens} tokens | "
            f"Pro: {self.pro_calls} calls | "
            f"{self.pro_input_tokens}→{self.pro_output_tokens} tokens"
        )


# ---------------------------------------------------------------------------
# Main Gemini Helper
# ---------------------------------------------------------------------------


class GeminiHelper:
    """
    Central interface for all Gemini calls.

    Key design decisions:
    - All prompts are defined as class-level strings (easy to tune).
    - Every call appends "Output ONLY valid JSON." to the prompt.
    - Response is validated with Pydantic before returning.
    - Random inter-call delay is added to respect RPM limits and look natural.
    """

    _EXTRACT_JOBS_PROMPT = dedent(
        """\
        You are a job listing extractor. Given cleaned webpage text, extract all job postings.

        Rules:
        - Extract every distinct job listing you find.
        - If a field is absent, use null.
        - For `link`: use the href/URL associated with the listing; keep as-is (relative or absolute).
        - For `tags`: extract tech stack, job type, or perks mentioned inline (max 6 tags).
        - `has_next_page`: true if you see pagination controls showing more results.
        - `next_page_hint`: text of the "next page" element (e.g. "Next →", "2", ">").

        Output ONLY valid JSON matching this schema — no markdown, no explanation:
        {
          "jobs": [
            {
              "title": "...",
              "company": "...",
              "location": "...",
              "link": "...",
              "salary": null,
              "short_description": "...",
              "posted_date": "...",
              "tags": []
            }
          ],
          "has_next_page": false,
          "next_page_hint": null
        }

        Page content:
        ```
        {page_content}
        ```
    """
    )

    _EXTRACT_JOB_DETAIL_PROMPT = dedent(
        """\
        You are a job detail extractor. Parse this job posting page into structured data.

        Rules:
        - `skills_required` vs `skills_preferred`: required = must-have, preferred = nice-to-have.
        - `employment_type`: one of: full-time, part-time, contract, freelance, internship.
        - `remote_policy`: one of: remote, hybrid, on-site, not-specified.
        - `raw_description`: full cleaned text of the job description (keep this complete).
        - If a field isn't mentioned, use null or empty list.

        Output ONLY valid JSON — no markdown, no explanation:
        {
          "title": "...",
          "company": "...",
          "location": "...",
          "salary": null,
          "employment_type": "...",
          "experience_required": "...",
          "skills_required": [],
          "skills_preferred": [],
          "responsibilities": [],
          "requirements": [],
          "benefits": [],
          "about_company": "...",
          "application_deadline": null,
          "remote_policy": "...",
          "apply_link": null,
          "raw_description": "..."
        }

        Page content:
        ```
        {page_content}
        ```
    """
    )

    _MATCH_SCORE_PROMPT = dedent(
        """\
        You are a senior recruiter. Score how well the candidate CV matches the job description.

        Scoring rubric (0-100):
        90-100: Exceeds requirements, near-perfect skill match
        75-89:  Strong match, minor gaps
        55-74:  Partial match, some important skills missing
        30-54:  Weak match, significant gaps
        0-29:   Mismatch

        Output ONLY valid JSON:
        {
          "score": 0,
          "verdict": "...",
          "matched_skills": [],
          "missing_skills": [],
          "missing_experience": [],
          "strengths": [],
          "recommendation": "..."
        }

        JOB DESCRIPTION:
        {job_description}

        CANDIDATE CV:
        {cv_text}
    """
    )

    _TAILOR_CV_PROMPT = dedent(
        """\
        You are an expert CV writer and ATS optimization specialist.

        Task: Rewrite the candidate's CV to better match the target job.

        STRICT rules:
        1. NEVER invent experience, skills, companies, or dates that aren't in the original CV.
        2. DO reorder bullet points to lead with most relevant achievements.
        3. DO naturally incorporate keywords from the job description (mirror their exact phrasing where honest).
        4. DO strengthen the professional summary to speak directly to this role.
        5. DO quantify achievements where numbers are already present (don't invent numbers).
        6. Keep total length similar to original — do not pad.
        7. Skills section: reorder to put most-relevant skills first; include all original skills.
        8. Track every keyword you inject in `keywords_injected`.

        Output ONLY valid JSON matching this schema exactly:
        {
          "summary": "2-4 sentence professional summary targeting this specific role",
          "experience": [
            {
              "role": "...",
              "company": "...",
              "dates": "...",
              "bullets": ["...", "..."]
            }
          ],
          "skills": ["skill1", "skill2"],
          "education": [
            {
              "degree": "...",
              "school": "...",
              "dates": "...",
              "details": "..."
            }
          ],
          "certifications": [],
          "projects": [],
          "keywords_injected": ["keyword1", "keyword2"]
        }

        TARGET JOB DESCRIPTION:
        {job_description}

        ORIGINAL CV:
        {cv_text}
    """
    )

    _ANALYZE_FORM_PROMPT = dedent(
        """\
        You are an expert at analyzing job application forms.
        Given this webpage text, identify all input fields the applicant must fill.

        Rules:
        - `field_type`: one of: text, email, phone, textarea, select, file, checkbox, radio, date.
        - `required`: true if the form marks it required (asterisk, "required", etc.).
        - `options`: for select/radio fields, list the choices.
        - `has_cv_upload`: true if there's a file upload for a CV/resume.
        - `cv_upload_selector_hint`: text label nearest to the CV upload button.

        Output ONLY valid JSON:
        {
          "fields": [
            {
              "field_id": "...",
              "label": "...",
              "field_type": "...",
              "required": false,
              "options": [],
              "suggested_value": null
            }
          ],
          "has_cv_upload": false,
          "cv_upload_selector_hint": null,
          "has_cover_letter": false,
          "notes": "..."
        }

        Form page content:
        ```
        {page_content}
        ```
    """
    )

    # ------------------------------------------------------------------
    # Generation configs
    # Built once and reused — temperature and mime type per model role.
    # ------------------------------------------------------------------

    _FLASH_CONFIG = types.GenerateContentConfig(
        response_mime_type="application/json",
        max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
        temperature=0.1,  # low temp = deterministic, good for extraction
    )

    _PRO_CONFIG = types.GenerateContentConfig(
        response_mime_type="application/json",
        max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
        temperature=0.2,  # slight creativity for CV rewriting
    )

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self):
        # Single client instance — new SDK style
        self._client = genai.Client(api_key=GEMINI_API_KEY)

        self._flash_limiter = _RateLimiter(GEMINI_FLASH_RPM, GEMINI_FLASH_RPD, "Flash")
        self._pro_limiter = _RateLimiter(GEMINI_PRO_RPM, GEMINI_PRO_RPD, "Pro")
        self.usage = _UsageTracker()

        logger.info(f"GeminiHelper ready | Flash: {GEMINI_FLASH_MODEL} | Pro: {GEMINI_PRO_MODEL}")

    # ------------------------------------------------------------------
    # Private: raw call wrapper
    # ------------------------------------------------------------------

    async def _call(self, prompt: str, use_pro: bool = False, context: str = "unknown") -> str:
        """
        Execute one Gemini call with rate limiting, retry, and usage tracking.

        Returns the raw response text (always JSON when using JSON mime type).
        Raises on unrecoverable error after 3 retries.
        """
        model_name = GEMINI_PRO_MODEL if use_pro else GEMINI_FLASH_MODEL
        config = self._PRO_CONFIG if use_pro else self._FLASH_CONFIG
        limiter = self._pro_limiter if use_pro else self._flash_limiter
        label = "Pro" if use_pro else "Flash"

        # Truncate input to stay within budget
        if len(prompt) > GEMINI_MAX_INPUT_CHARS:
            prompt = prompt[:GEMINI_MAX_INPUT_CHARS] + "\n[TRUNCATED]"
            logger.warning(f"[{label}][{context}] Prompt truncated to {GEMINI_MAX_INPUT_CHARS} chars.")

        for attempt in range(1, 4):
            await limiter.acquire()
            try:
                logger.debug(f"[{label}][{context}] Sending prompt ({len(prompt)} chars)…")
                t0 = time.monotonic()

                # New SDK exposes a native async interface — no executor needed.
                response = await self._client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
                
                logger.debug(f"[{label}] Raw response received")
                text = response.text
                if not text:
                    logger.warning(f"[{label}] Empty response text")
                    text = ""
                text = text.strip()

                # Track usage (approximate if metadata unavailable)
                meta = response.usage_metadata
                in_tok = meta.prompt_token_count if meta else len(prompt) // 4
                out_tok = meta.candidates_token_count if meta else len(text) // 4
                self.usage.record(model_name, in_tok, out_tok)

                logger.info(
                    f"[{label}][{context}] OK in {elapsed:.1f}s | "
                    f"~{in_tok}→{out_tok} tokens | "
                    f"Daily remaining: {limiter.daily_remaining}"
                )

                # Polite inter-call delay
                jitter = DELAYS.gemini_min + (DELAYS.gemini_max - DELAYS.gemini_min) * (time.monotonic() % 1)
                await asyncio.sleep(jitter)

                return text

            except Exception as exc:
                logger.warning(f"[{label}][{context}] Attempt {attempt}/3 failed: {exc}")
                if attempt < 3:
                    await asyncio.sleep(15 * attempt)  # back-off: 15s, 30s
                else:
                    raise RuntimeError(f"Gemini call failed after 3 attempts [{context}]: {exc}") from exc

        raise RuntimeError("Unreachable")

    # ------------------------------------------------------------------
    # Private: parse + validate JSON response
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: str, context: str) -> dict[str, Any]:
        """Strip markdown fences if present, then parse JSON."""
        text = raw.strip()
        # Remove ```json ... ``` or ``` ... ``` wrappers if model ignores mime type
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            text = text.rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"[{context}] JSON parse error: {e}\nRaw: {text[:500]}")
            raise ValueError(f"Gemini returned invalid JSON for [{context}]: {e}") from e

    # ------------------------------------------------------------------
    # Public API Methods
    # ------------------------------------------------------------------

    async def extract_job_listings(self, page_markdown: str, site_name: str = "unknown") -> JobListingsPage:
        """
        Extract job listings from a search results page.

        Args:
            page_markdown: Cleaned Markdown text from Crawl4AI.
            site_name: For logging only.

        Returns:
            JobListingsPage with list of JobListing objects.

        Gemini model: Flash (1 call per results page)
        """
        prompt = self._EXTRACT_JOBS_PROMPT.format(page_content=page_markdown)
        raw = await self._call(prompt, use_pro=False, context=f"scan:{site_name}")
        data = self._parse_json(raw, f"extract_job_listings:{site_name}")
        result = JobListingsPage.model_validate(data)
        logger.info(f"Extracted {len(result.jobs)} jobs from {site_name}")
        return result

    async def extract_job_detail(self, page_markdown: str, job_url: str = "unknown") -> JobDetail:
        """
        Extract full structured job details from a job detail page.

        Gemini model: Flash (1 call per job)
        """
        prompt = self._EXTRACT_JOB_DETAIL_PROMPT.format(page_content=page_markdown)
        raw = await self._call(prompt, use_pro=False, context=f"detail:{job_url[:60]}")
        data = self._parse_json(raw, "extract_job_detail")
        return JobDetail.model_validate(data)

    async def score_job_match(self, job_detail: JobDetail, cv_text: str) -> MatchResult:
        """
        Score how well the candidate's CV matches the job.

        Input is the raw_description (already extracted), so no extra scraping.
        Gemini model: Flash (combined with detail extraction or standalone)
        """
        # Build a concise job description from structured fields to save tokens
        job_desc = (
            f"Title: {job_detail.title}\n"
            f"Company: {job_detail.company}\n"
            f"Required skills: {', '.join(job_detail.skills_required)}\n"
            f"Preferred skills: {', '.join(job_detail.skills_preferred)}\n"
            f"Experience: {job_detail.experience_required or 'not specified'}\n"
            f"Responsibilities:\n"
            + "\n".join(f"- {r}" for r in job_detail.responsibilities[:10])
            + f"\n\nFull description:\n{job_detail.raw_description[:3000]}"
        )

        # Truncate CV to first 3000 chars (usually enough for scoring)
        cv_short = cv_text[:3000]

        prompt = self._MATCH_SCORE_PROMPT.format(
            job_description=job_desc,
            cv_text=cv_short,
        )
        raw = await self._call(prompt, use_pro=False, context="score_match")
        data = self._parse_json(raw, "score_job_match")
        return MatchResult.model_validate(data)

    async def tailor_cv(self, job_detail: JobDetail, cv_text: str) -> TailoredCV:
        """
        Rewrite the CV to better match the job. Uses Pro model for quality.

        This is the most important call — Pro is worth it here.
        Gemini model: Pro (1 call per application)
        """
        prompt = self._TAILOR_CV_PROMPT.format(
            job_description=job_detail.raw_description[:8000],
            cv_text=cv_text[:8000],
        )
        raw = await self._call(prompt, use_pro=True, context="tailor_cv")
        data = self._parse_json(raw, "tailor_cv")
        result = TailoredCV.model_validate(data)
        logger.info(
            f"CV tailored | {len(result.keywords_injected)} keywords injected: "
            f"{', '.join(result.keywords_injected[:10])}"
        )
        return result

    async def analyze_application_form(self, page_markdown: str, user_profile: dict[str, Any]) -> FormAnalysis:
        """
        Detect and analyze form fields on an application page.
        Pre-fills suggested values from user_profile where possible.

        Gemini model: Flash
        """
        prompt = self._ANALYZE_FORM_PROMPT.format(page_content=page_markdown)
        raw = await self._call(prompt, use_pro=False, context="analyze_form")
        data = self._parse_json(raw, "analyze_form")
        result = FormAnalysis.model_validate(data)

        # Map user profile data to suggested values
        profile_map = {
            "name": user_profile.get("full_name", ""),
            "email": user_profile.get("email", ""),
            "phone": user_profile.get("phone", ""),
            "linkedin": user_profile.get("linkedin_url", ""),
            "github": user_profile.get("github_url", ""),
            "location": user_profile.get("location", ""),
            "website": user_profile.get("website", ""),
        }

        for field in result.fields:
            label_lower = field.label.lower()
            for key, value in profile_map.items():
                if key in label_lower and value:
                    field.suggested_value = value
                    break

        logger.info(f"Form analysis: {len(result.fields)} fields detected")
        return result

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def log_usage_summary(self) -> None:
        """Print token usage summary — call at end of session."""
        logger.info(f"Session usage → {self.usage.summary()}")
        print(f"\n📊 Gemini usage: {self.usage.summary()}")

    @property
    def flash_daily_remaining(self) -> int:
        return self._flash_limiter.daily_remaining

    @property
    def pro_daily_remaining(self) -> int:
        return self._pro_limiter.daily_remaining
