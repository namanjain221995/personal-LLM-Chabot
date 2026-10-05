"""Record-query configuration. Limits, thresholds and paths from YAML."""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path


@dataclass
class DuckDBSettings:
    path: str = "/data/warehouse.read.duckdb"
    schema: str = "main"
    read_only: bool = True


@dataclass
class ResultSettings:
    default_limit: int = 100
    max_limit: int = 1000
    max_response_bytes: int = 2_000_000
    # A truncated result costs one extra COUNT to learn how many rows matched.
    # Worth it: without the total, an answer can only say "showing 100", never
    # "100 of 284".
    count_total_when_truncated: bool = True


@dataclass
class FreshnessSettings:
    # Match the measured sync cadence; see schema_config.yaml.
    expected_sync_minutes: int = 40
    warning_after_minutes: int = 80
    reject_after_minutes: int = 180
    reject_when_stale: bool = False


@dataclass
class RecordQueryConfig:
    duckdb: DuckDBSettings = dataclass_field(default_factory=DuckDBSettings)
    result: ResultSettings = dataclass_field(default_factory=ResultSettings)
    freshness: FreshnessSettings = dataclass_field(default_factory=FreshnessSettings)
    default_join_type: str = "LEFT"


def _environment(root: Path) -> str:
    try:
        import yaml
    except ImportError:
        return "production"
    path = root / "config" / "environments.yaml"
    if not path.is_file():
        return "production"
    return str((yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                ).get("default", "production"))


def load_record_query_config(root: str | Path = "salesforce_knowledge",
                             environment: str | None = None
                             ) -> RecordQueryConfig:
    try:
        import yaml
    except ImportError:
        return RecordQueryConfig()
    path = Path(root) / "config" / "schema_config.yaml"
    if not path.is_file():
        return RecordQueryConfig()
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}
           ).get("record_query") or {}
    duck = raw.get("duckdb") or {}
    # Per-environment path wins over the shared fallback: a dev run must not
    # read production records just because nobody set a dev path.
    env = environment or _environment(Path(root))
    duck_path = (duck.get("paths") or {}).get(env) or duck.get(
        "path", "/data/warehouse.read.duckdb")
    result = raw.get("result") or {}
    fresh = raw.get("freshness") or {}
    joins = raw.get("joins") or {}
    return RecordQueryConfig(
        duckdb=DuckDBSettings(
            path=str(duck_path),
            schema=str(duck.get("schema", "main")),
            read_only=bool(duck.get("read_only", True))),
        result=ResultSettings(
            default_limit=int(result.get("default_limit", 100)),
            max_limit=int(result.get("max_limit", 1000)),
            max_response_bytes=int(result.get("max_response_bytes", 2_000_000)),
            count_total_when_truncated=bool(
                result.get("count_total_when_truncated", True))),
        freshness=FreshnessSettings(
            expected_sync_minutes=int(fresh.get("expected_sync_minutes", 40)),
            warning_after_minutes=int(fresh.get("warning_after_minutes", 80)),
            reject_after_minutes=int(fresh.get("reject_after_minutes", 180)),
            reject_when_stale=bool(fresh.get("reject_when_stale", False))),
        default_join_type=str(joins.get("default_type", "LEFT")).upper(),
    )
