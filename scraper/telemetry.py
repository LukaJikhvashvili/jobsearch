"""Structured telemetry collection for scraper operations."""
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MetricEvent:
    """A single recorded metric."""
    name: str
    value: float
    tags: Dict[str, str] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SpanRecord:
    """A timed operation span."""
    operation: str
    site: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_s: Optional[float] = None
    success: bool = True
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class TelemetryCollector:
    """
    Collects structured metrics and spans.

    Supports:
    - Timed spans (profiling, scraping, LLM calls, page loads)
    - Counter metrics (jobs found, pages scraped, filters applied, errors)
    - Custom metric sinks via on_span / on_metric callbacks
    """

    def __init__(self):
        self._spans: List[SpanRecord] = []
        self._metrics: List[MetricEvent] = []
        self._on_span_callbacks: List[Callable[[SpanRecord], None]] = []
        self._on_metric_callbacks: List[Callable[[MetricEvent], None]] = []

    # ---- Callback registration ----

    def on_span(self, callback: Callable[[SpanRecord], None]) -> None:
        """Register a callback invoked whenever a span completes."""
        self._on_span_callbacks.append(callback)

    def on_metric(self, callback: Callable[[MetricEvent], None]) -> None:
        """Register a callback invoked whenever a metric is recorded."""
        self._on_metric_callbacks.append(callback)

    # ---- Span tracking ----

    @contextmanager
    def span(self, operation: str, site: str = "", **metadata):
        """
        Context manager to time an operation.

        Usage:
            with telemetry.span("scrape_page", site="jobs.ge", page=3):
                ...
        """
        record = SpanRecord(
            operation=operation,
            site=site,
            started_at=datetime.now(timezone.utc),
            metadata=metadata,
        )
        try:
            yield record
            record.success = True
        except Exception as exc:
            record.success = False
            record.error = str(exc)
            raise
        finally:
            record.ended_at = datetime.now(timezone.utc)
            record.duration_s = (record.ended_at - record.started_at).total_seconds()
            self._spans.append(record)
            self._emit_span(record)

    def _emit_span(self, record: SpanRecord) -> None:
        status = "OK" if record.success else f"FAIL: {record.error}"
        logger.info(
            "[Telemetry] %s  site=%s  duration=%.2fs  status=%s  %s",
            record.operation, record.site, record.duration_s or 0,
            status, record.metadata,
        )
        for cb in self._on_span_callbacks:
            try:
                cb(record)
            except Exception:
                pass

    # ---- Metric recording ----

    def record(self, name: str, value: float = 1.0, **tags: str) -> None:
        """Record a metric (counter, gauge, etc.)."""
        event = MetricEvent(name=name, value=value, tags=tags)
        self._metrics.append(event)
        logger.debug("[Telemetry] metric %s=%.2f  tags=%s", name, value, tags)
        for cb in self._on_metric_callbacks:
            try:
                cb(event)
            except Exception:
                pass

    # ---- Convenience methods ----

    def track_adapter_generation(self, site: str, duration_s: float, success: bool, error: str = "") -> None:
        self.record("adapter_generation", 1.0, site=site, success=str(success))
        self.record("adapter_generation_duration_s", duration_s, site=site)
        if not success:
            self.record("adapter_generation_errors", 1.0, site=site, error=error)

    def track_llm_call(self, site: str, provider: str, duration_s: float, success: bool, error: str = "") -> None:
        self.record("llm_call", 1.0, site=site, provider=provider, success=str(success))
        self.record("llm_call_duration_s", duration_s, site=site, provider=provider)
        if not success:
            self.record("llm_call_errors", 1.0, site=site, provider=provider, error=error)

    def track_page_scraped(self, site: str, page_num: int, cards_found: int) -> None:
        self.record("page_scraped", 1.0, site=site, page=str(page_num))
        self.record("cards_found", float(cards_found), site=site, page=str(page_num))

    def track_scraping_complete(self, site: str, total_jobs: int, total_pages: int, duration_s: float) -> None:
        self.record("scraping_complete", 1.0, site=site)
        self.record("total_jobs", float(total_jobs), site=site)
        self.record("total_pages", float(total_pages), site=site)
        self.record("scraping_duration_s", duration_s, site=site)

    def track_filter_applied(self, site: str, dimension: str, mechanism: str, success: bool) -> None:
        self.record("filter_applied", 1.0, site=site, dimension=dimension, mechanism=mechanism, success=str(success))

    def track_extraction(self, site: str, field_name: str, success: bool) -> None:
        self.record("field_extraction", 1.0, site=site, field=field_name, success=str(success))

    # ---- Reporting ----

    def get_summary(self) -> Dict[str, Any]:
        """Return a summary dict of all collected telemetry."""
        total_spans = len(self._spans)
        failed_spans = sum(1 for s in self._spans if not s.success)
        metric_counts: Dict[str, float] = {}
        for m in self._metrics:
            metric_counts[m.name] = metric_counts.get(m.name, 0) + m.value
        return {
            "total_spans": total_spans,
            "failed_spans": failed_spans,
            "success_rate": (total_spans - failed_spans) / total_spans if total_spans else 0,
            "metric_totals": metric_counts,
            "spans": [
                {
                    "operation": s.operation,
                    "site": s.site,
                    "duration_s": s.duration_s,
                    "success": s.success,
                    "error": s.error,
                }
                for s in self._spans
            ],
        }

    def reset(self) -> None:
        """Clear all collected data."""
        self._spans.clear()
        self._metrics.clear()


# Singleton for convenience — can also instantiate your own
_default: Optional[TelemetryCollector] = None


def get_telemetry() -> TelemetryCollector:
    """Get the default singleton TelemetryCollector."""
    global _default
    if _default is None:
        _default = TelemetryCollector()
    return _default
