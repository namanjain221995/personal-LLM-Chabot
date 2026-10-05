"""Configuration for the runtime schema subsystem."""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("salesforce_knowledge")


class ConfigError(RuntimeError):
    pass


@dataclass
class AutoPromotion:
    enabled: bool = True
    min_access_count: int = 20
    window_hours: int = 24
    max_hot_objects: int = 75


@dataclass
class SchemaConfig:
    root: Path = DEFAULT_ROOT
    environment: str = "production"
    version: str = "1.0"
    fetcher: str = "mirror"
    mirror_path: str = ""
    # Hand-reviewed business vocabulary. Loaded into the runtime schema as
    # manual aliases at refresh time; see business_knowledge.py.
    business_knowledge_path: str = ""
    db_path_template: str = "runtime_schema/db/{environment}/salesforce_runtime_schema.db"
    fts_enabled: bool = True
    vector_enabled: bool = False
    cache_enabled: bool = True
    hot_mode: str = "hybrid"
    pinned: list[str] = dataclass_field(default_factory=list)
    auto_promotion: AutoPromotion = dataclass_field(default_factory=AutoPromotion)
    exports_enabled: bool = True
    snapshots_enabled: bool = True
    snapshots_keep: int = 5

    @property
    def database_path(self) -> Path:
        return self.root / self.db_path_template.format(environment=self.environment)

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifests" / "runtime_schema_manifest.json"

    @property
    def exports_path(self) -> Path:
        return self.root / "runtime_schema" / "exports"

    @property
    def snapshots_path(self) -> Path:
        return self.root / "runtime_schema" / "snapshots"


def load_config(root: str | Path = DEFAULT_ROOT,
                environment: str | None = None) -> SchemaConfig:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("configuration needs PyYAML") from exc

    root = Path(root)
    config_file = root / "config" / "schema_config.yaml"
    if not config_file.is_file():
        raise ConfigError(f"missing {config_file}")
    raw: dict[str, Any] = (yaml.safe_load(config_file.read_text(encoding="utf-8"))
                           or {}).get("schema", {})

    if environment is None:
        env_file = root / "config" / "environments.yaml"
        environment = "production"
        if env_file.is_file():
            environment = (yaml.safe_load(env_file.read_text(encoding="utf-8"))
                           or {}).get("default", "production")

    cache = raw.get("cache") or {}
    hot = cache.get("hot_objects") or {}
    auto = hot.get("auto_promotion") or {}
    source = raw.get("source") or {}
    storage = raw.get("storage") or {}
    search = raw.get("search") or {}
    snapshots = raw.get("snapshots") or {}

    return SchemaConfig(
        root=root,
        environment=str(environment),
        version=str(raw.get("version", "1.0")),
        fetcher=str(source.get("fetcher", "mirror")),
        mirror_path=str(source.get("mirror_path", "")),
        business_knowledge_path=str(source.get("business_knowledge", "")),
        db_path_template=str(storage.get(
            "path_template",
            "runtime_schema/db/{environment}/salesforce_runtime_schema.db")),
        fts_enabled=bool(search.get("fts_enabled", True)),
        vector_enabled=bool(search.get("vector_enabled", False)),
        cache_enabled=bool(cache.get("enabled", True)),
        hot_mode=str(hot.get("mode", "hybrid")),
        pinned=[str(p) for p in (hot.get("pinned") or [])],
        auto_promotion=AutoPromotion(
            enabled=bool(auto.get("enabled", True)),
            min_access_count=int(auto.get("min_access_count", 20)),
            window_hours=int(auto.get("window_hours", 24)),
            max_hot_objects=int(auto.get("max_hot_objects", 75))),
        exports_enabled=bool((raw.get("exports") or {}).get("enabled", True)),
        snapshots_enabled=bool(snapshots.get("enabled", True)),
        snapshots_keep=int(snapshots.get("keep", 5)),
    )
