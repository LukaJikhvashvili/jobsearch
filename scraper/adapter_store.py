"""
AdapterStore: persists SiteAdapter objects as versioned JSON files.

Layout:
    adapters/
        jobs_ge.json
        hr_ge.json
        …
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .models import SiteAdapter

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path("adapters")
_STALE_AFTER_DAYS = 30


class AdapterStore:
    def __init__(self, directory: Path = _DEFAULT_DIR):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers

    def _path(self, site: str) -> Path:
        safe = site.replace("https://", "").replace("http://", "")
        safe = safe.replace(".", "_").replace("/", "_").strip("_")
        return self.directory / f"{safe}.json"

    def _is_stale(self, adapter: SiteAdapter) -> bool:
        cutoff = datetime.utcnow() - timedelta(days=_STALE_AFTER_DAYS)
        return adapter.generated_at < cutoff

    # ------------------------------------------------------------------ CRUD

    def save(self, adapter: SiteAdapter) -> None:
        path = self._path(adapter.site)
        path.write_text(adapter.model_dump_json(indent=2), encoding="utf-8")
        logger.info("Saved adapter  site=%s  path=%s", adapter.site, path)

    def load(self, site: str) -> Optional[SiteAdapter]:
        path = self._path(site)
        if not path.exists():
            logger.debug("No adapter on disk for %s", site)
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            adapter = SiteAdapter.model_validate(data)
            if self._is_stale(adapter):
                logger.info("Adapter stale (>%d days)  site=%s", _STALE_AFTER_DAYS, site)
                adapter.is_stale = True
            return adapter
        except Exception as exc:
            logger.warning("Failed to load adapter for %s: %s", site, exc)
            return None

    def invalidate(self, site: str) -> None:
        """Mark an adapter as stale so it will be re-generated next run."""
        adapter = self.load(site)
        if adapter:
            adapter.is_stale = True
            self.save(adapter)
            logger.info("Invalidated adapter for %s", site)

    def delete(self, site: str) -> None:
        path = self._path(site)
        if path.exists():
            path.unlink()
            logger.info("Deleted adapter for %s", site)

    def list_sites(self) -> list[str]:
        return [p.stem.replace("_", ".") for p in self.directory.glob("*.json")]

    def needs_refresh(self, site: str) -> bool:
        adapter = self.load(site)
        return adapter is None or adapter.is_stale
