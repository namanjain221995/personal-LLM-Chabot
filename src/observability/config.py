"""Where traces are written, read and exported.

One resolver, so the service, the exporter and the inspection script cannot
disagree about which file is canonical. Environment variables win over the
YAML, because a container sets the path and the repository cannot know it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The historic location, still honoured. Traces written there before the move
# are not orphaned by it -- see `candidate_databases`.
LEGACY_DATABASE = "/data/traces/knowledge_traces.sqlite"

DEFAULT_ROOT = "observability/tracing"
DEFAULT_DATABASE = f"{DEFAULT_ROOT}/db/traces.sqlite"
DEFAULT_EXPORTS = f"{DEFAULT_ROOT}/exports"


@dataclass
class TracingConfig:
    enabled: bool = True
    database_path: str = DEFAULT_DATABASE
    exports_directory: str = DEFAULT_EXPORTS
    duckdb_export_path: str = f"{DEFAULT_EXPORTS}/traces.duckdb"
    environment: str = "production"
    # Names kept because scripts and documentation already refer to them.
    traces_csv: str = "query_traces.csv"
    events_csv: str = "query_trace_events.csv"

    @property
    def database(self) -> Path:
        return Path(self.database_path)

    @property
    def exports(self) -> Path:
        return Path(self.exports_directory)

    def candidate_databases(self) -> list[Path]:
        """Every store a reader should look in, canonical first.

        The legacy path stays readable after the move. Trace history is the
        evidence the new architecture is judged on; relocating the write path
        must not make the old evidence unreachable, and nothing here deletes
        it.
        """
        seen: list[Path] = [self.database]
        for extra in (LEGACY_DATABASE,):
            path = Path(extra)
            if path not in seen:
                seen.append(path)
        return seen


def load_tracing_config(root: str | Path = "salesforce_knowledge",
                        ) -> TracingConfig:
    config = TracingConfig()
    try:
        import yaml
    except ImportError:
        yaml = None                                     # type: ignore[assignment]

    if yaml is not None:
        path = Path(root) / "config" / "schema_config.yaml"
        if path.is_file():
            raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                   ).get("observability") or {}
            tracing = raw.get("tracing") or {}
            database = tracing.get("database") or {}
            exports = tracing.get("exports") or {}
            config.enabled = bool(tracing.get("enabled", True))
            config.database_path = str(database.get("path",
                                                    config.database_path))
            config.exports_directory = str(exports.get("directory",
                                                       config.exports_directory))
            config.duckdb_export_path = str(
                exports.get("duckdb", f"{config.exports_directory}/traces.duckdb"))
        environments = Path(root) / "config" / "environments.yaml"
        if environments.is_file():
            config.environment = str(
                (yaml.safe_load(environments.read_text(encoding="utf-8")) or {}
                 ).get("default", config.environment))

    # A deployment's own settings win. SFK_* is the current namespace;
    # KNOWLEDGE_* and the bare TRACE_* names are honoured so an existing
    # deployment keeps working through the rename.
    override = _first("SFK_TRACE_DB", "TRACE_DB", "KNOWLEDGE_TRACE_DB")
    if override:
        config.database_path = override
    exports = _first("SFK_TRACE_EXPORT_DIR", "TRACE_EXPORT_DIR")
    if exports:
        config.exports_directory = exports
        config.duckdb_export_path = f"{exports}/traces.duckdb"
    tracing = _first("SFK_TRACING", "KNOWLEDGE_TRACING")
    if tracing:
        config.enabled = tracing.strip().lower() == "true"
    # SALESFORCE_ENVIRONMENT is canonical. SF_ENVIRONMENT is the old
    # pipeline's name for the same fact and is accepted as an alias -- without
    # it, a deployment that only sets the old one records every trace as the
    # YAML default while the rest of the app reads something else.
    environment = _first("SALESFORCE_ENVIRONMENT", "SF_ENVIRONMENT")
    if environment and environment != "unknown":
        config.environment = environment
    return config


def _first(*names: str) -> str:
    """The first of these environment variables that is set and non-empty."""
    import os
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""
