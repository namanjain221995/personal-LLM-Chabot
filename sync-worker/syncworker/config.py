"""Environment settings and config.yaml (synced objects) loading."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml

_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Settings:
    sync_interval_minutes: int
    #: Adopt fields added in Salesforce since the config was written.
    sync_auto_fields: bool
    #: Ceiling per object — an org with 500 fields would otherwise
    #: build a SELECT nobody wants and slow every cycle.
    sync_max_fields: int
    #: Report objects that exist in Salesforce but are not configured.
    sync_report_new_objects: bool
    parquet_dir: str
    duckdb_path: str
    lancedb_dir: str
    embed_via: str
    embed_model: str
    sf_api_version: str
    config_path: str


def load_settings() -> Settings:
    here = os.path.dirname(os.path.abspath(__file__))
    return Settings(
        sync_interval_minutes=int(os.getenv("SYNC_INTERVAL_MINUTES", "30")),
        sync_auto_fields=os.getenv("SYNC_AUTO_FIELDS", "true").lower()
        not in ("0", "false", "no"),
        sync_max_fields=int(os.getenv("SYNC_MAX_FIELDS", "80")),
        sync_report_new_objects=os.getenv("SYNC_REPORT_NEW_OBJECTS", "true").lower()
        not in ("0", "false", "no"),
        parquet_dir=os.getenv("PARQUET_DIR", "/data/parquet"),
        duckdb_path=os.getenv("DUCKDB_PATH", "/data/warehouse.duckdb"),
        lancedb_dir=os.getenv("LANCEDB_DIR", "/data/lancedb"),
        embed_via=os.getenv("EMBED_VIA", "http://vllm-embed:30003/v1").rstrip("/"),
        embed_model=os.getenv("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B"),
        sf_api_version=os.getenv("SF_API_VERSION", "v61.0"),
        config_path=os.getenv(
            "SYNC_CONFIG_PATH", os.path.join(here, "..", "config.yaml")
        ),
    )


@dataclass(frozen=True)
class ObjectConfig:
    name: str
    fields: tuple[str, ...]
    rag_fields: tuple[str, ...] = field(default=())


def load_object_configs(path: str) -> list[ObjectConfig]:
    """Load and validate the synced-object list from config.yaml."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or not isinstance(raw.get("objects"), list):
        raise ValueError("config.yaml must contain a top-level 'objects' list")

    objects: list[ObjectConfig] = []
    for entry in raw["objects"]:
        name = entry.get("name")
        fields_ = list(entry.get("fields") or [])
        rag_fields = list(entry.get("rag_fields") or [])
        if not name or not _IDENT_RE.match(name):
            raise ValueError(f"invalid object name in config.yaml: {name!r}")
        for f in fields_ + rag_fields:
            if not _IDENT_RE.match(str(f)):
                raise ValueError(f"invalid field name for {name}: {f!r}")
        if "Id" not in fields_ or "SystemModstamp" not in fields_:
            raise ValueError(f"{name}: fields must include Id and SystemModstamp")
        missing = [f for f in rag_fields if f not in fields_]
        if missing:
            raise ValueError(f"{name}: rag_fields not listed in fields: {missing}")
        objects.append(ObjectConfig(name, tuple(fields_), tuple(rag_fields)))

    if not objects:
        raise ValueError("config.yaml defines no objects")
    return objects
