"""Multi-level caching for HTML, LLM responses, and translations."""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional
from abc import ABC, abstractmethod

from .config import CacheConfig
from .events import EventBus

logger = logging.getLogger(__name__)


class CacheBackend(ABC):
    @abstractmethod
    def get(self, key: str) -> Optional[str]: ...

    @abstractmethod
    def set(self, key: str, value: str, ttl_s: int) -> None: ...

    @abstractmethod
    def invalidate(self, key: str) -> None: ...


class FileCache(CacheBackend):
    """Simple file-based cache with TTL."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe_key = hashlib.sha256(key.encode()).hexdigest()
        return self.directory / f"{safe_key}.json"

    def get(self, key: str) -> Optional[str]:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("expires_at", 0) < time.time():
                path.unlink(missing_ok=True)
                return None
            return data["value"]
        except Exception:
            return None

    def set(self, key: str, value: str, ttl_s: int) -> None:
        path = self._path(key)
        data = {"value": value, "expires_at": time.time() + ttl_s, "key_hint": key[:100]}
        path.write_text(json.dumps(data), encoding="utf-8")

    def invalidate(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class NullCache(CacheBackend):
    """No-op cache for when caching is disabled."""

    def get(self, key: str) -> Optional[str]:
        return None

    def set(self, key: str, value: str, ttl_s: int) -> None:
        pass

    def invalidate(self, key: str) -> None:
        pass


class ScraperCache:
    """Facade providing domain-specific cache operations."""

    def __init__(self, config: Optional[CacheConfig] = None, event_bus: Optional[EventBus] = None):
        cfg = config or CacheConfig()
        self.event_bus = event_bus
        self.config = cfg

        if cfg.enabled:
            self._html_cache = FileCache(cfg.cache_directory / "html")
            self._llm_cache = FileCache(cfg.cache_directory / "llm")
        else:
            self._html_cache = NullCache()
            self._llm_cache = NullCache()

    def _cache_key(self, prefix: str, *parts: str) -> str:
        combined = "|".join(parts)
        return f"{prefix}:{hashlib.sha256(combined.encode()).hexdigest()}"

    def get_html(self, url: str) -> Optional[str]:
        key = self._cache_key("html", url)
        result = self._html_cache.get(key)
        return result

    def set_html(self, url: str, html: str) -> None:
        key = self._cache_key("html", url)
        self._html_cache.set(key, html, self.config.html_cache_ttl_s)

    def get_llm_response(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        key = self._cache_key("llm", system_prompt, user_prompt)
        return self._llm_cache.get(key)

    def set_llm_response(self, system_prompt: str, user_prompt: str, response: str) -> None:
        key = self._cache_key("llm", system_prompt, user_prompt)
        self._llm_cache.set(key, response, self.config.llm_cache_ttl_s)
