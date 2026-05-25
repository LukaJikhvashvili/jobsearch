"""Retry decorator with exponential backoff."""
import asyncio
import functools
import logging
import time
from typing import Callable, Optional, Tuple, Type

from .config import RetryConfig
from .events import EventBus, Events

logger = logging.getLogger(__name__)


def retry(
    config: Optional[RetryConfig] = None,
    retryable: Tuple[Type[Exception], ...] = (Exception,),
    event_bus: Optional[EventBus] = None,
):
    """
    Decorator for async functions. Retries with exponential backoff.

    Usage:
        @retry(config=my_config, retryable=(TimeoutError, ConnectionError))
        async def my_func():
            ...
    """
    cfg = config or RetryConfig()

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_error: Exception = RuntimeError("No attempts made")
            delay = cfg.initial_delay_s

            for attempt in range(1, cfg.max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable as exc:
                    last_error = exc
                    if attempt == cfg.max_attempts:
                        logger.error(
                            "All %d attempts failed for %s: %s",
                            cfg.max_attempts, func.__name__, exc
                        )
                        break

                    logger.warning(
                        "Attempt %d/%d failed for %s: %s — retrying in %.1fs",
                        attempt, cfg.max_attempts, func.__name__, exc, delay
                    )

                    if event_bus:
                        await event_bus.publish(
                            Events.RETRY_ATTEMPTED,
                            function=func.__name__,
                            attempt=attempt,
                            max_attempts=cfg.max_attempts,
                            error=str(exc),
                            delay=delay,
                        )

                    await asyncio.sleep(delay)
                    delay = min(delay * cfg.exponential_base, cfg.max_delay_s)

            raise last_error
        return wrapper
    return decorator


def retry_sync(
    config: Optional[RetryConfig] = None,
    retryable: Tuple[Type[Exception], ...] = (Exception,),
):
    """Synchronous version of retry for non-async functions (like LLM calls)."""
    cfg = config or RetryConfig()

    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error: Exception = RuntimeError("No attempts made")
            delay = cfg.initial_delay_s

            for attempt in range(1, cfg.max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except retryable as exc:
                    last_error = exc
                    if attempt == cfg.max_attempts:
                        break
                    logger.warning(
                        "Attempt %d/%d failed for %s: %s — retrying in %.1fs",
                        attempt, cfg.max_attempts, func.__name__, exc, delay
                    )
                    time.sleep(delay)
                    delay = min(delay * cfg.exponential_base, cfg.max_delay_s)
            raise last_error
        return wrapper
    return decorator
