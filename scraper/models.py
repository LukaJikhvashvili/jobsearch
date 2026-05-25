from enum import Enum
from typing import Optional, Dict, List
from pydantic import BaseModel, Field, field_validator
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


# ---------------------------------------------------------------------------
# Detail navigation
# ---------------------------------------------------------------------------


class DetailNavType(str, Enum):
    DIRECT_LINK = "direct_link"
    CARD_CLICK = "card_click"
    BUTTON_CLICK = "button_click"
    DATA_ATTR = "data_attr"


class DetailNavigation(BaseModel):
    type: DetailNavType
    link_selector: Optional[str] = None
    data_attribute: Optional[str] = None
    click_container: bool = False
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class FilterMechanism(str, Enum):
    URL_PARAM = "url_param"
    SEARCH_FIELD = "search_field"
    DROPDOWN = "dropdown"
    CHECKBOX_GROUP = "checkbox_group"
    TAG_FILTER = "tag_filter"
    RADIO_GROUP = "radio_group"
    DATE_RANGE = "date_range"


class FilterDimension(str, Enum):
    KEYWORD = "keyword"
    LOCATION = "location"
    CATEGORY = "category"
    SALARY = "salary"
    DATE_POSTED = "date_posted"


class FilterEntry(BaseModel):
    dimension: FilterDimension
    mechanism: FilterMechanism
    param_name: Optional[str] = None
    selector: Optional[str] = None
    options_selector: Optional[str] = None
    item_selector: Optional[str] = None
    date_from_selector: Optional[str] = None
    date_to_selector: Optional[str] = None
    notes: Optional[str] = None


class FiltersConfig(BaseModel):
    available: List[FilterEntry] = []
    submit_selector: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Runtime filter values
# ---------------------------------------------------------------------------


class UserFilters(BaseModel):
    keyword: Optional[str] = None
    location: Optional[str] = None
    category: Optional[str] = None
    salary_min: Optional[int] = None
    date_posted: Optional[str] = None  # "today" | "week" | "month" | "3months"

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

    @field_validator("start_page", mode="before")
    @classmethod
    def validate_start_page(cls, v):
        return 1 if v is None else v

    @field_validator("max_pages", mode="before")
    @classmethod
    def validate_max_pages(cls, v):
        return 50 if v is None else v

    @field_validator("delay_ms", mode="before")
    @classmethod
    def validate_delay_ms(cls, v):
        return 1200 if v is None else v


# ---------------------------------------------------------------------------
# Listings config
# ---------------------------------------------------------------------------


class ListingsConfig(BaseModel):
    container: str
    fields: Dict[str, FieldSelector]
    pagination: PaginationConfig
    navigation: DetailNavigation
    filters: FiltersConfig = Field(default_factory=FiltersConfig)


# ---------------------------------------------------------------------------
# Top-level adapter  (detail is optional — populated only in Phase 2)
# ---------------------------------------------------------------------------


class SiteAdapter(BaseModel):
    site: str
    base_url: str
    listings_url: str
    listings: ListingsConfig
    # Phase 2 not yet implemented — detail stays None until then
    detail: Optional[Dict] = None
    page_languages: List[str] = Field(default=["en"])  # ISO 639-1 codes
    requires_js: bool = False
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    version: int = 1
    is_stale: bool = False
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Scraped job
# ---------------------------------------------------------------------------


class JobListing(BaseModel):
    site: str
    title: Optional[str] = None
    company: Optional[str] = None
    url: Optional[str] = None
    # Detail fields — populated later
    location: Optional[str] = None
    salary: Optional[str] = None
    posted_date: Optional[str] = None
    description: Optional[str] = None
    requirements: Optional[str] = None
    application_method: Optional[str] = None
    application_url: Optional[str] = None
    application_email: Optional[str] = None
    raw_html: Optional[str] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    adapter_version: int = 1
