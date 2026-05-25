"""Pluggable field extraction system."""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import AttrType, FieldSelector, SiteAdapter


class FieldExtractor(ABC):
    """
    Base class for field extractors.
    Each extractor is responsible for populating specific fields on a JobListing.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for logging."""
        ...

    @abstractmethod
    def extract(self, card: BeautifulSoup, adapter: SiteAdapter, base_url: str) -> Dict[str, Optional[str]]:
        """
        Extract fields from a single job card.
        Returns a dict of field_name -> value (or None).
        Only return keys this extractor is responsible for.
        """
        ...


def _extract_field(soup: BeautifulSoup, field: Optional[FieldSelector], base_url: str = "") -> Optional[str]:
    """Low-level extraction helper (moved from runner.py)."""
    if field is None or not field.selector:
        return None
    el = soup.select_one(field.selector)
    if el is None:
        return None
    attr = field.attr
    if attr == AttrType.TEXT:
        return el.get_text(separator=" ", strip=True) or None
    elif attr == AttrType.HTML:
        return str(el) or None
    elif attr == AttrType.HREF:
        href = el.get("href", "")
        return urljoin(base_url, href) if href else None
    elif attr == AttrType.SRC:
        src = el.get("src", "")
        return urljoin(base_url, src) if src else None
    elif attr == AttrType.VALUE:
        return el.get("value") or el.get_text(strip=True) or None
    return None


class CoreFieldExtractor(FieldExtractor):
    """Extracts title and company — the standard fields."""

    @property
    def name(self) -> str:
        return "core_fields"

    def extract(self, card: BeautifulSoup, adapter: SiteAdapter, base_url: str) -> Dict[str, Optional[str]]:
        fields = adapter.listings.fields
        return {
            "title": _extract_field(card, fields.get("title"), base_url),
            "company": _extract_field(card, fields.get("company"), base_url),
        }


class SalaryExtractor(FieldExtractor):
    """Extracts salary info if a 'salary' field selector is present in the adapter."""

    @property
    def name(self) -> str:
        return "salary"

    def extract(self, card: BeautifulSoup, adapter: SiteAdapter, base_url: str) -> Dict[str, Optional[str]]:
        fields = adapter.listings.fields
        if "salary" not in fields:
            return {}
        return {"salary": _extract_field(card, fields.get("salary"), base_url)}


class LocationExtractor(FieldExtractor):
    """Extracts location info if a 'location' field selector is present."""

    @property
    def name(self) -> str:
        return "location"

    def extract(self, card: BeautifulSoup, adapter: SiteAdapter, base_url: str) -> Dict[str, Optional[str]]:
        fields = adapter.listings.fields
        if "location" not in fields:
            return {}
        return {"location": _extract_field(card, fields.get("location"), base_url)}


class PostedDateExtractor(FieldExtractor):
    """Extracts posted_date if a 'posted_date' field selector is present."""

    @property
    def name(self) -> str:
        return "posted_date"

    def extract(self, card: BeautifulSoup, adapter: SiteAdapter, base_url: str) -> Dict[str, Optional[str]]:
        fields = adapter.listings.fields
        if "posted_date" not in fields:
            return {}
        return {"posted_date": _extract_field(card, fields.get("posted_date"), base_url)}


class GenericFieldExtractor(FieldExtractor):
    """
    Catch-all extractor: extracts ANY field defined in the adapter's fields dict
    that isn't already handled by other extractors.
    Useful for custom fields added to adapters without writing new code.
    """

    def __init__(self, skip_fields: Optional[set] = None):
        self._skip = skip_fields or {"title", "company", "salary", "location", "posted_date"}

    @property
    def name(self) -> str:
        return "generic_fields"

    def extract(self, card: BeautifulSoup, adapter: SiteAdapter, base_url: str) -> Dict[str, Optional[str]]:
        result = {}
        for field_name, field_sel in adapter.listings.fields.items():
            if field_name in self._skip:
                continue
            result[field_name] = _extract_field(card, field_sel, base_url)
        return result


# ---- Registry / Pipeline ----


class ExtractorPipeline:
    """Runs a list of FieldExtractors in order, merging results."""

    def __init__(self, extractors: Optional[List[FieldExtractor]] = None):
        if extractors is None:
            # Default pipeline: core fields + all optional ones
            extractors = [
                CoreFieldExtractor(),
                SalaryExtractor(),
                LocationExtractor(),
                PostedDateExtractor(),
                GenericFieldExtractor(),
            ]
        self.extractors = extractors

    def extract_all(self, card: BeautifulSoup, adapter: SiteAdapter, base_url: str) -> Dict[str, Optional[str]]:
        """Run all extractors and merge results. Later extractors do NOT overwrite earlier ones."""
        merged: Dict[str, Optional[str]] = {}
        for extractor in self.extractors:
            fields = extractor.extract(card, adapter, base_url)
            for k, v in fields.items():
                if k not in merged:
                    merged[k] = v
        return merged
