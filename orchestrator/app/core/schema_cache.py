"""DuckDB schema cache used to ground SQL generation prompts.

duckdb is imported lazily and the database is always opened read_only.
"""
from __future__ import annotations

import time
from typing import Dict, List, Tuple


class SchemaCache:
    """TTL cache of {table: [(column, type), ...]} keyed by database path."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[float, Dict[str, List[Tuple[str, str]]]]] = {}

    def get(self, db_path: str, force_refresh: bool = False) -> Dict[str, List[Tuple[str, str]]]:
        now = time.monotonic()
        hit = self._cache.get(db_path)
        if hit is not None and not force_refresh and (now - hit[0]) < self.ttl_seconds:
            return hit[1]
        schema = self._load(db_path)
        self._cache[db_path] = (now, schema)
        return schema

    def invalidate(self, db_path: str | None = None) -> None:
        if db_path is None:
            self._cache.clear()
        else:
            self._cache.pop(db_path, None)

    @staticmethod
    def _load(db_path: str) -> Dict[str, List[Tuple[str, str]]]:
        import duckdb  # lazy

        # Same lockdown config as the sql engine's _execute: introspection
        # needs no external access, and DuckDB rejects concurrent
        # connections to one file whose configs differ.
        con = duckdb.connect(
            db_path,
            read_only=True,
            config={
                "enable_external_access": False,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
            },
        )
        try:
            rows = con.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'main' "
                "ORDER BY table_name, ordinal_position"
            ).fetchall()
        finally:
            con.close()

        schema: Dict[str, List[Tuple[str, str]]] = {}
        for table, column, dtype in rows:
            schema.setdefault(table, []).append((column, dtype))
        return schema


def format_schema(schema: Dict[str, List[Tuple[str, str]]]) -> str:
    """Render a schema dict as compact `table(col TYPE, ...)` lines."""
    lines = []
    for table, cols in schema.items():
        cols_txt = ", ".join(f"{c} {t}" for c, t in cols)
        lines.append(f"{table}({cols_txt})")
    return "\n".join(lines)


schema_cache = SchemaCache()
