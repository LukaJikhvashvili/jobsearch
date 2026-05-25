"""Lightweight async event bus for extension hooks."""
import logging
from typing import Any, Callable, Coroutine, Dict, List, Union

logger = logging.getLogger(__name__)

# Type alias for event handlers
EventHandler = Union[Callable[..., None], Callable[..., Coroutine]]


class EventBus:
    """Simple publish-subscribe event system."""

    def __init__(self):
        self._handlers: Dict[str, List[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        if event_type in self._handlers:
            self._handlers[event_type] = [h for h in self._handlers[event_type] if h != handler]

    async def publish(self, event_type: str, **data: Any) -> None:
        for handler in self._handlers.get(event_type, []):
            try:
                import asyncio
                result = handler(event_type, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("Event handler failed for %s: %s", event_type, exc)


# Well-known event types (constants)
class Events:
    ADAPTER_GENERATION_STARTED = "adapter_generation_started"
    ADAPTER_GENERATION_COMPLETED = "adapter_generation_completed"
    ADAPTER_GENERATION_FAILED = "adapter_generation_failed"
    PAGE_CAPTURED = "page_captured"
    LLM_CALL_STARTED = "llm_call_started"
    LLM_CALL_COMPLETED = "llm_call_completed"
    LLM_CALL_FAILED = "llm_call_failed"
    SCRAPING_STARTED = "scraping_started"
    SCRAPING_PAGE_COMPLETED = "scraping_page_completed"
    SCRAPING_COMPLETED = "scraping_completed"
    FILTER_APPLIED = "filter_applied"
    FILTER_FAILED = "filter_failed"
    RETRY_ATTEMPTED = "retry_attempted"
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
