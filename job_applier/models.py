"""
Data models for job_applier package.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass
class CVData:
    """Structured personal data extracted from a CV/resume."""

    full_name: str = ""
    email: str = ""
    phone_number: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    current_role: str = ""
    location: str = ""
    summary: str = ""
    skills: str = ""  # comma-separated or short prose
    years_of_experience: str = ""
    education: str = ""
    cover_letter: str = ""  # AI-generated when needed
    file_path: str = "" # Path to the original CV file for cache validation

    # ---- helpers ----

    def to_dict(self) -> Dict[str, Any]:
        """Return only non-empty fields as a plain dict."""
        return {k: v for k, v in self.__dict__.items() if v and v.strip()}

    def to_prompt_block(self) -> str:
        """Human-readable summary for AI prompts."""
        lines = []
        for k, v in self.to_dict().items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CVData":
        valid = {k: str(v) for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class FormField:
    """Represents a single interactive element on the page."""

    selector: str
    tag: str
    type: str  # input type or tag name
    label: str = ""
    placeholder: str = ""
    name: str = ""
    id_attr: str = ""
    required: bool = False
    options: List[str] = field(default_factory=list)  # for <select> / radio
    accept: str = ""  # for file inputs
    text: str = ""  # visible button/label text
    element_index: int = 0


@dataclass
class FormMapping:
    """AI-produced mapping of CV fields to page selectors."""

    field_mappings: Dict[str, str] = field(default_factory=dict)
    cv_upload_selector: Optional[str] = None
    submit_selector: Optional[str] = None
    next_button_selector: Optional[str] = None  # multi-step forms
    checkboxes_to_check: List[str] = field(default_factory=list)  # agree / terms
    generated_values: Dict[str, str] = field(default_factory=dict)  # AI-synthesized answers

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FormMapping":
        return cls(
            field_mappings=d.get("field_mappings", {}),
            cv_upload_selector=d.get("cv_upload_selector") or None,
            submit_selector=d.get("submit_button_selector") or d.get("submit_selector") or None,
            next_button_selector=d.get("next_button_selector") or None,
            checkboxes_to_check=d.get("checkboxes_to_check", []),
            generated_values=d.get("generated_values", {}),
        )


@dataclass
class SubmissionResult:
    """Outcome of a form submission attempt."""

    success: bool
    confidence: float  # 0.0 – 1.0
    evidence: str  # human-readable explanation
    final_url: str = ""
    error_messages: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        status = "✓ SUCCESS" if self.success else "✗ FAILED"
        return f"{status} (confidence={self.confidence:.0%}) — {self.evidence}"
