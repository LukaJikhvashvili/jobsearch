from enum import Enum
from typing import Optional, Dict
from pydantic import BaseModel, Field
from datetime import datetime


class AttrType(str, Enum):
    TEXT = "text"
    HTML = "html"
    HREF = "href"
    SRC = "src"
    VALUE = "value"


class DetailNavType(str, Enum):
    DIRECT_LINK  = "direct_link"   # <a href> on the card — just follow the href
    CARD_CLICK   = "card_click"    # whole card is clickable via JS, no plain <a>
    BUTTON_CLICK = "button_click"  # a specific button/CTA inside the card
    DATA_ATTR    = "data_attr"     # URL stored in data-href / data-url attribute


class DetailNavigation(BaseModel):
    """
    Describes how to travel from a job card on the listings page
    to the job detail page.
    """
    type: DetailNavType

    # DIRECT_LINK / BUTTON_CLICK — CSS selector for the <a> or <button>
    # relative to the card container.
    link_selector: Optional[str] = None

    # DATA_ATTR — the attribute name that holds the URL (e.g. "data-href")
    data_attribute: Optional[str] = None

    # CARD_CLICK — if true, click the container element itself
    click_container: bool = False

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: Optional[str] = None


class PaginationType(str, Enum):
    URL_PARAM = "url_param"
    NEXT_BUTTON = "next_button"
    INFINITE_SCROLL = "infinite_scroll"
    NONE = "none"


class ApplicationMethod(str, Enum):
    ON_PAGE_FORM = "on_page_form"
    ATS_REDIRECT = "ats_redirect"
    EXTERNAL_LINK = "external_link"
    EMAIL = "email"
    UNKNOWN = "unknown"


class FieldSelector(BaseModel):
    selector: Optional[str] = None
    attr: AttrType = AttrType.TEXT
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class PaginationConfig(BaseModel):
    type: PaginationType
    param_name: Optional[str] = None
    start_page: int = 1
    next_selector: Optional[str] = None
    max_pages: int = 50
    delay_ms: int = 1200


class ApplicationConfig(BaseModel):
    method: ApplicationMethod
    form_selector: Optional[str] = None
    apply_button_selector: Optional[str] = None
    external_url_selector: Optional[str] = None
    email_selector: Optional[str] = None
    ats_domain: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: Optional[str] = None


class ListingsConfig(BaseModel):
    container: str
    fields: Dict[str, FieldSelector]
    pagination: PaginationConfig
    navigation: DetailNavigation             # how to reach the detail page


class DetailConfig(BaseModel):
    fields: Dict[str, FieldSelector]
    application: ApplicationConfig


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


class JobListing(BaseModel):
    site: str
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    salary: Optional[str] = None
    url: Optional[str] = None
    posted_date: Optional[str] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    application_method: Optional[ApplicationMethod] = None
    application_url: Optional[str] = None
    application_email: Optional[str] = None
    raw_html: Optional[str] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    adapter_version: int = 1