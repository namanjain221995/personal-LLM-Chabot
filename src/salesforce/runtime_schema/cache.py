"""In-memory hot-object cache.

    L1  this cache          full schema for hot objects
    L2  SQLite              everything, cold objects on demand
    L3  the fetcher         only on refresh

The compact catalog for all ~419 objects is held resident; detailed schema is
not. Holding every field for every object would be ~4,600 records to serve the
handful a question touches, so detail is loaded for pinned and auto-hot objects
and read from SQLite for the rest.

Pinned and auto-hot are tracked separately rather than as one set, because a
pinned object must survive demotion and a merged set cannot express that.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .repository import SchemaRepository
from .usage import UsageTracker

log = logging.getLogger(__name__)


class SchemaCache:
    def __init__(self, repository: SchemaRepository, usage: UsageTracker) -> None:
        self.repo = repository
        self.usage = usage
        self._lock = threading.RLock()
        self._catalog: dict[str, dict[str, Any]] = {}
        self._hot: dict[str, dict[str, Any]] = {}
        self._pinned: set[str] = set()
        self._auto_hot: set[str] = set()

    # -- loading ----------------------------------------------------------
    def load_catalog(self) -> int:
        with self._lock:
            self._catalog = {entry["api_name"]: entry
                             for entry in self.repo.object_catalog()}
        return len(self._catalog)

    def _detailed(self, api_name: str) -> dict[str, Any] | None:
        obj = self.repo.get_object(api_name)
        if obj is None:
            return None
        fields = self.repo.get_fields(api_name)
        picklists: dict[str, list[dict[str, Any]]] = {}
        for field in fields:
            if (field.get("data_type") or "").lower().endswith("picklist"):
                values = self.repo.get_picklist_values(api_name, field["api_name"])
                if values:
                    picklists[field["api_name"]] = values
        return {
            "object": obj,
            "fields": fields,
            "relationships": self.repo.get_relationships(api_name),
            "child_relationships": self.repo.get_child_relationships(api_name),
            "picklists": picklists,
            "record_types": self.repo.get_record_types(api_name),
            "aliases": self.repo.get_object_aliases(api_name),
        }

    def load_hot_objects(self, pinned: list[str], auto_hot: list[str],
                         max_hot: int) -> dict[str, Any]:
        """Load detail for pinned objects first, then auto-hot up to the cap.

        Pinned first and unconditionally: the cap may cut auto-hot objects, and
        it must never be the reason a pinned one is missing.
        """
        report: dict[str, Any] = {"pinned_loaded": [], "auto_loaded": [],
                                  "missing": [], "skipped_over_cap": []}
        with self._lock:
            self._hot.clear()
            self._pinned = set()
            self._auto_hot = set()

            for name in pinned:
                detail = self._detailed(name)
                if detail is None:
                    # A pinned name that is not in the org is a configuration
                    # error worth surfacing, not a silent no-op.
                    report["missing"].append(name)
                    log.warning("pinned object %s is not in the schema", name)
                    continue
                self._hot[name] = detail
                self._pinned.add(name)
                report["pinned_loaded"].append(name)

            for name in auto_hot:
                if name in self._hot:
                    continue
                if len(self._hot) >= max_hot:
                    report["skipped_over_cap"].append(name)
                    continue
                detail = self._detailed(name)
                if detail is None:
                    report["missing"].append(name)
                    continue
                self._hot[name] = detail
                self._auto_hot.add(name)
                report["auto_loaded"].append(name)
        return report

    def invalidate(self, names: list[str] | None = None) -> None:
        """Drop cached detail so the next read comes from SQLite."""
        with self._lock:
            if names is None:
                self._hot.clear()
                self._catalog.clear()
                return
            for name in names:
                self._hot.pop(name, None)
                self._catalog.pop(name, None)

    # -- reading ----------------------------------------------------------
    def catalog(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self._catalog:
                self.load_catalog()
            return list(self._catalog.values())

    def catalog_entry(self, api_name: str) -> dict[str, Any] | None:
        with self._lock:
            return self._catalog.get(api_name)

    def get_detailed(self, api_name: str) -> tuple[dict[str, Any] | None, str]:
        """Detailed schema, and which level served it."""
        with self._lock:
            cached = self._hot.get(api_name)
        if cached is not None:
            return cached, "L1"
        return self._detailed(api_name), "L2"

    def tier_for(self, api_name: str) -> str | None:
        """Which level WOULD serve this object's detail, without fetching it.

        Same branch `get_detailed` takes, asked as a question. Tracing needs
        the provenance and not the payload, and loading 4,600 field rows to
        find out where they came from would make the trace change the thing it
        observes.

        None means the object is not in this schema at all -- which is a
        different answer from "L2" and must not be flattened into one.
        """
        with self._lock:
            if api_name in self._hot:
                return "L1"
            if api_name in self._catalog:
                return "L2"
        return "L2" if self.repo.get_object(api_name) is not None else None

    def is_hot(self, api_name: str) -> bool:
        with self._lock:
            return api_name in self._hot

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"catalog_size": len(self._catalog),
                    "hot_size": len(self._hot),
                    "pinned": sorted(self._pinned),
                    "auto_hot": sorted(self._auto_hot)}
