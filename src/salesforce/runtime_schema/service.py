"""The public interface to the runtime schema.

Everything outside this subsystem goes through here. Callers get progressive
retrieval -- a compact catalog to choose from, detailed schema once a candidate
is picked -- and never a SQLite handle.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .cache import SchemaCache
from .config import SchemaConfig, load_config
from .database import Database
from .fetcher import build_fetcher
from .refresh import RefreshService
from .repository import SchemaRepository
from .search import SchemaSearch
from .usage import UsageTracker

log = logging.getLogger(__name__)


class RuntimeSchemaService:
    def __init__(self, config: SchemaConfig | None = None, *,
                 root: str | Path = "salesforce_knowledge",
                 environment: str | None = None) -> None:
        self.config = config or load_config(root, environment)
        self.db = Database(self.config.database_path)
        self.repo = SchemaRepository(self.db)
        self.search_index = SchemaSearch(self.db, self.repo)
        self.usage = UsageTracker(self.db)
        self.cache = SchemaCache(self.repo, self.usage)
        self._fetcher = None

    # -- lifecycle --------------------------------------------------------
    def fetcher(self):
        if self._fetcher is None:
            mirror = self.config.mirror_path
            if mirror and not Path(mirror).is_absolute():
                mirror = str(Path.cwd() / mirror)
            knowledge = self.config.business_knowledge_path
            if knowledge and not Path(knowledge).is_absolute():
                knowledge = str(Path.cwd() / knowledge)
            self._fetcher = build_fetcher(self.config.fetcher, mirror_path=mirror,
                                          business_knowledge=knowledge or None)
        return self._fetcher

    def start(self) -> dict[str, Any]:
        """Load the catalog and the hot objects. Safe on an empty database."""
        catalog_size = self.cache.load_catalog()
        report = self.reload_hot_objects()
        return {"catalog_size": catalog_size, **report}

    def reload_hot_objects(self) -> dict[str, Any]:
        """Re-apply pinned config and current auto-hot set.

        Pinned comes from YAML every time, so removing a name there actually
        unpins it rather than leaving it hot forever.
        """
        if not self.config.cache_enabled:
            return {"cache": "disabled"}
        known = set(self.repo.object_names())
        pinned = [n for n in self.config.pinned if n in known]
        missing_pins = [n for n in self.config.pinned if n not in known]
        self.usage.set_pinned(pinned)

        auto = self.config.auto_promotion
        auto_hot: list[str] = []
        if auto.enabled:
            promote = self.usage.promotion_candidates(
                min_access_count=auto.min_access_count,
                max_hot_objects=auto.max_hot_objects)
            demote = self.usage.demotion_candidates(
                min_access_count=auto.min_access_count)
            if promote:
                self.usage.mark_auto_hot(promote, True)
            if demote:
                self.usage.mark_auto_hot(demote, False)
            auto_hot = [n for n in promote if n in known]

        self._apply_hot_flags(pinned, auto_hot)
        report = self.cache.load_hot_objects(pinned, auto_hot,
                                             auto.max_hot_objects)
        report["missing_pins"] = missing_pins
        return report

    def _apply_hot_flags(self, pinned: list[str], auto_hot: list[str]) -> None:
        connection = self.db.connection
        connection.execute("UPDATE objects SET is_hot_object=0, hot_source=NULL")
        for name in auto_hot:
            connection.execute(
                "UPDATE objects SET is_hot_object=1, hot_source='usage'"
                " WHERE api_name=?", (name,))
        # Pinned last: a manually pinned object must read as 'manual' even if
        # usage would also have promoted it, and must never be auto-demoted.
        for name in pinned:
            connection.execute(
                "UPDATE objects SET is_hot_object=1, hot_source='manual'"
                " WHERE api_name=?", (name,))

    # -- discovery --------------------------------------------------------
    def get_object_catalog(self) -> list[dict[str, Any]]:
        return self.cache.catalog()

    def search_objects(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.search_index.search_objects(query, limit)

    def search_fields(self, query: str, object_api_name: str | None = None,
                      limit: int = 20) -> list[dict[str, Any]]:
        return self.search_index.search_fields(query, object_api_name, limit)

    # -- detail -----------------------------------------------------------
    def get_object_schema(self, object_api_name: str) -> dict[str, Any] | None:
        detail, level = self.cache.get_detailed(object_api_name)
        if detail is None:
            return None
        self.record_object_access(object_api_name)
        return {**detail, "served_from": level}

    def get_field_schema(self, object_api_name: str,
                         field_api_name: str) -> dict[str, Any] | None:
        self.record_object_access(object_api_name)
        return self.repo.get_field(object_api_name, field_api_name)

    def get_relationships(self, object_api_name: str) -> list[dict[str, Any]]:
        self.record_object_access(object_api_name)
        return self.repo.get_relationships(object_api_name)

    def get_child_relationships(self, object_api_name: str) -> list[dict[str, Any]]:
        self.record_object_access(object_api_name)
        return self.repo.get_child_relationships(object_api_name)

    def get_picklist_values(self, object_api_name: str,
                            field_api_name: str) -> list[dict[str, Any]]:
        self.record_object_access(object_api_name)
        return self.repo.get_picklist_values(object_api_name, field_api_name)

    def get_record_types(self, object_api_name: str) -> list[dict[str, Any]]:
        self.record_object_access(object_api_name)
        return self.repo.get_record_types(object_api_name)

    # -- usage ------------------------------------------------------------
    def record_object_access(self, object_api_name: str) -> None:
        if self.config.auto_promotion.enabled:
            self.usage.record_access(object_api_name,
                                     self.config.auto_promotion.window_hours)

    # -- refresh ----------------------------------------------------------
    def refresh_schema(self, only: list[str] | None = None,
                       rebuild_index: bool = True) -> dict[str, Any]:
        service = RefreshService(self.config, self.db, self.repo,
                                 self.search_index, self.fetcher(), self.cache)
        result = service.refresh(only, rebuild_index)
        self.cache.load_catalog()
        self.reload_hot_objects()
        return result

    def export_schema(self) -> dict[str, str]:
        service = RefreshService(self.config, self.db, self.repo,
                                 self.search_index, self.fetcher(), self.cache)
        return service.export()

    def counts(self) -> dict[str, int]:
        return self.repo.counts()

    def served_from(self, object_api_name: str) -> str | None:
        """Which cache level holds this object: L1 memory, L2 SQLite, or None.

        L3 is the fetcher and is reachable only from `refresh_schema`, never
        from a question -- so a query-time lookup is never served from L3 and
        this never returns it.
        """
        return self.cache.tier_for(object_api_name)

    def cache_state(self) -> dict[str, Any]:
        return self.cache.state()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "RuntimeSchemaService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
