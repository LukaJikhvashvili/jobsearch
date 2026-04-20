from enum import Enum
from typing import Optional, Dict, List
from pydantic import BaseModel, Field
from datetime import datetime


# ---------------------------------------------------------------------------
# Shared field extraction
# ---------------------------------------------------------------------------


class AttrType(str, Enum):
    TEXT = "text"
    HTML = "html"
    HREF = "href"
    SRC = "src"
    VALUE = "value"


class FieldSelector(BaseModel):
    selector: Optional[str] = None
    attr: AttrType = AttrType.TEXT
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Detail navigation
# ---------------------------------------------------------------------------


class DetailNavType(str, Enum):
    DIRECT_LINK = "direct_link"  # plain <a href> on the card
    CARD_CLICK = "card_click"  # whole card is JS-clickable, no <a>
    BUTTON_CLICK = "button_click"  # a specific CTA button inside the card
    DATA_ATTR = "data_attr"  # URL lives in a data-* attribute


class DetailNavigation(BaseModel):
    type: DetailNavType
    link_selector: Optional[str] = None  # CSS relative to container
    data_attribute: Optional[str] = None  # e.g. "data-href"
    click_container: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class FilterMechanism(str, Enum):
    URL_PARAM = "url_param"  # ?q=python&location=tbilisi
    SEARCH_FIELD = "search_field"  # <input type="text|search">
    DROPDOWN = "dropdown"  # <select> element
    CHECKBOX_GROUP = "checkbox_group"  # group of <input type="checkbox">
    TAG_FILTER = "tag_filter"  # clickable pill / chip buttons
    RADIO_GROUP = "radio_group"  # <input type="radio"> group
    DATE_RANGE = "date_range"  # from-date / to-date inputs


class FilterDimension(str, Enum):
    KEYWORD = "keyword"  # job title / keyword search
    LOCATION = "location"
    CATEGORY = "category"  # job category / industry
    SALARY = "salary"
    DATE_POSTED = "date_posted"  # recency filter


class FilterEntry(BaseModel):
    """One filter control on the listings page."""

    dimension: FilterDimension
    mechanism: FilterMechanism

    # URL_PARAM ── query-string parameter name (e.g. "q", "city")
    param_name: Optional[str] = None

    # DOM-based mechanisms ── the top-level interactable element
    selector: Optional[str] = None

    # DROPDOWN ── <option> elements live here (defaults to selector + " option")
    options_selector: Optional[str] = None

    # CHECKBOX_GROUP / TAG_FILTER / RADIO_GROUP
    # ── CSS for individual items; runner matches by visible text
    item_selector: Optional[str] = None

    # DATE_RANGE ── separate from / to inputs
    date_from_selector: Optional[str] = None
    date_to_selector: Optional[str] = None

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: Optional[str] = None


class FiltersConfig(BaseModel):
    """All filters discovered on the listings page."""

    available: List[FilterEntry] = []
    # Selector for a "Search" / "Apply filters" button.
    # Needed when DOM filters don't auto-submit (e.g. select + button).
    submit_selector: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Runtime filter values — provided by the user when calling run()
# ---------------------------------------------------------------------------


class UserFilters(BaseModel):
    keyword: Optional[str] = None
    location: Optional[str] = None
    category: Optional[str] = None
    salary_min: Optional[int] = None
    # Accepted values: "today" | "week" | "month" | "3months"
    date_posted: Optional[str] = None

    def is_empty(self) -> bool:
        return not any(
            [
                self.keyword,
                self.location,
                self.category,
                self.salary_min,
                self.date_posted,
            ]
        )


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class PaginationType(str, Enum):
    URL_PARAM = "url_param"
    NEXT_BUTTON = "next_button"
    INFINITE_SCROLL = "infinite_scroll"
    NONE = "none"


class PaginationConfig(BaseModel):
    type: PaginationType
    param_name: Optional[str] = None
    start_page: int = 1
    next_selector: Optional[str] = None
    max_pages: int = 50
    delay_ms: int = 1200


# ---------------------------------------------------------------------------
# Listings config (card-level only — title + company)
# ---------------------------------------------------------------------------


class ListingsConfig(BaseModel):
    container: str  # CSS for the repeating job card
    fields: Dict[str, FieldSelector]  # only "title" and "company"
    pagination: PaginationConfig
    navigation: DetailNavigation
    filters: FiltersConfig = Field(default_factory=FiltersConfig)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class ApplicationMethod(str, Enum):
    ON_PAGE_FORM = "on_page_form"
    ATS_REDIRECT = "ats_redirect"
    EXTERNAL_LINK = "external_link"
    EMAIL = "email"
    UNKNOWN = "unknown"


class ApplicationConfig(BaseModel):
    method: ApplicationMethod
    form_selector: Optional[str] = None
    apply_button_selector: Optional[str] = None
    external_url_selector: Optional[str] = None
    email_selector: Optional[str] = None
    ats_domain: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Detail config  (all the rich fields live here)
# ---------------------------------------------------------------------------


class DetailConfig(BaseModel):
    fields: Dict[str, FieldSelector]  # location, salary, posted_date, description, requirements
    application: ApplicationConfig


# ---------------------------------------------------------------------------
# Top-level adapter
# ---------------------------------------------------------------------------


class SiteAdapter(BaseModel):
    site: str
    base_url: str
    listings_url: str
    listings: ListingsConfig
    detail: DetailConfig
    requires_js: bool = False
    requires_auth: bool = False
    overall_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    version: int = 1
    is_stale: bool = False
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Scraped job (populated progressively: listings pass → detail pass)
# ---------------------------------------------------------------------------


class JobListing(BaseModel):
    site: str
    # From listings card
    title: Optional[str] = None
    company: Optional[str] = None
    url: Optional[str] = None
    # From detail page
    location: Optional[str] = None
    salary: Optional[str] = None
    posted_date: Optional[str] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    # Application
    application_method: Optional[ApplicationMethod] = None
    application_url: Optional[str] = None
    application_email: Optional[str] = None
    # Meta
    raw_html: Optional[str] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    adapter_version: int = 1
