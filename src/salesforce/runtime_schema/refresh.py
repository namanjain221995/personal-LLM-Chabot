"""Coordinates a schema refresh: fetch, snapshot, write, reindex, manifest."""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cache import SchemaCache
from .config import SchemaConfig
from .database import SCHEMA_VERSION, Database
from .fetcher import SchemaFetcher
from .repository import SchemaRepository
from .search import SchemaSearch

log = logging.getLogger(__name__)


class RefreshService:
    def __init__(self, config: SchemaConfig, database: Database,
                 repository: SchemaRepository, search: SchemaSearch,
                 fetcher: SchemaFetcher, cache: SchemaCache | None = None) -> None:
        self.config = config
        self.db = database
        self.repo = repository
        self.search = search
        self.fetcher = fetcher
        self.cache = cache

    def _snapshot(self) -> Path | None:
        """Copy the current database aside before it is overwritten."""
        if not self.config.snapshots_enabled:
            return None
        source = self.config.database_path
        if not source.is_file() or source.stat().st_size == 0:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        target = self.config.snapshots_path / stamp
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / source.name)
        if self.config.manifest_path.is_file():
            shutil.copy2(self.config.manifest_path, target / "manifest.json")
        self._prune_snapshots()
        log.info("snapshot written to %s", target)
        return target

    def _prune_snapshots(self) -> None:
        keep = max(1, self.config.snapshots_keep)
        directories = sorted(
            [p for p in self.config.snapshots_path.iterdir() if p.is_dir()],
            reverse=True)
        for stale in directories[keep:]:
            shutil.rmtree(stale, ignore_errors=True)

    def refresh(self, only: list[str] | None = None,
                rebuild_index: bool = True) -> dict[str, Any]:
        refresh_id = f"rs_{uuid.uuid4().hex[:16]}"
        self.repo.start_refresh(refresh_id, self.config.environment,
                                self.fetcher.source_name)
        snapshot = self._snapshot()

        previous_hashes = self.repo.hashes()
        bundle = self.fetcher.fetch(only)
        changed = [o.api_name for o in bundle.objects
                   if previous_hashes.get(o.api_name) != o.raw_hash]

        counts = self.repo.write_bundle(bundle, source=self.fetcher.source_name)

        index_counts: dict[str, int] = {}
        if rebuild_index and self.config.fts_enabled:
            # Only changed objects need reindexing -- that is what raw_hash is
            # for. A first run has no previous hashes, so everything is changed.
            scope = changed if previous_hashes else None
            if scope is None or scope:
                index_counts = self.search.rebuild(scope)

        status = "success" if not bundle.errors else "partial"
        self.repo.finish_refresh(refresh_id, status=status, counts=counts,
                                 errors=bundle.errors)

        if self.cache is not None:
            self.cache.invalidate(changed or None)

        manifest = self.write_manifest(status)
        return {"refresh_id": refresh_id, "status": status, "counts": counts,
                "changed_objects": len(changed), "indexed": index_counts,
                "errors": bundle.errors, "snapshot": str(snapshot) if snapshot else None,
                "manifest": manifest}

    def write_manifest(self, status: str) -> dict[str, Any]:
        counts = self.repo.counts()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "environment": self.config.environment,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": self.fetcher.source_name,
            "object_count": counts["objects"],
            "field_count": counts["fields"],
            "relationship_count": counts["relationships"],
            "child_relationship_count": counts["child_relationships"],
            "picklist_value_count": counts["picklist_values"],
            "record_type_count": counts["record_types"],
            "object_alias_count": counts["object_aliases"],
            "field_alias_count": counts["field_aliases"],
            "database": self.config.database_path.name,
            "refresh_status": status,
        }
        path = self.config.manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return manifest

    def export(self) -> dict[str, str]:
        """Normalized schema as JSON, for inspection only. SQLite stays primary."""
        if not self.config.exports_enabled:
            return {}
        base = self.config.exports_path
        written: dict[str, str] = {}
        connection = self.db.connection
        for name, table, directory in (
                ("objects", "objects", "objects"),
                ("fields", "fields", "fields"),
                ("relationships", "relationships", "relationships"),
                ("child_relationships", "child_relationships", "child_relationships"),
                ("picklist_values", "picklist_values", "picklists"),
                ("record_types", "record_types", "record_types")):
            rows = [dict(r) for r in connection.execute(f"SELECT * FROM {table}")]
            target = base / directory / f"{name}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(rows, indent=2, default=str) + "\n",
                              encoding="utf-8")
            written[name] = str(target)
        return written
