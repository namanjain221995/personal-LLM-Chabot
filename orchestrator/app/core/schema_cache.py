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
        try:
            schema = self._load(db_path)
        except Exception:
            # A refresh can fail transiently — most commonly the sync-worker
            # holding the file's write lock for a batch. The schema changes
            # rarely; a stale copy grounds the SQL prompt far better than an
            # error. Only ever raise when there is nothing cached at all.
            if hit is not None:
                return hit[1]
            raise
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


# ---------------------------------------------------------------------------
# Grounding selection: the warehouse mirrors the WHOLE org (owner request,
# 2026-08-06) — hundreds of Share/History/Feed shadows and setup objects
# alongside the business tables. Dumping 900+ tables into every SQL prompt
# would bury the ~160 the model actually needs, so grounding is selective:
# business tables always, everything else only when the question asks for it.
# ---------------------------------------------------------------------------

#: Shadow suffixes: per-record plumbing generated from a base object.
_SHADOW_SUFFIXES = ("Share", "History", "Feed", "ChangeEvent", "__hd")

#: Standard objects that hold this org's business data (matches the curated
#: sync import). Custom objects (__c) are recognized by suffix instead.
_STANDARD_BUSINESS = frozenset({
    "Account", "AccountContactRelation", "Asset", "Campaign", "CampaignMember",
    "Case", "CaseComment", "CaseStatus", "Contact", "ContentDocument",
    "ContentVersion", "Contract", "EmailMessage", "Event", "Group", "Lead",
    "LeadStatus", "Note", "Opportunity", "OpportunityContactRole",
    "OpportunityLineItem", "OpportunityStage", "Order", "OrderItem",
    "Pricebook2", "PricebookEntry", "Product2", "Profile", "Quote",
    "QuoteLineItem", "RecordType", "SocialPersona", "SocialPost", "Task",
    "TaskStatus", "User", "UserRole",
})

#: At most this many question-matched non-business tables join the prompt —
#: a question containing "share" must not drag in 500 Share shadows.
_MAX_MATCHED_EXTRAS = 40


def _is_business_table(table: str) -> bool:
    if table in _STANDARD_BUSINESS:
        return True
    return table.endswith("__c") and not table.endswith(
        ("__Share", "__History", "__Feed")
    )


def relevant_schema(
    schema: Dict[str, List[Tuple[str, str]]], question: str
) -> Dict[str, List[Tuple[str, str]]]:
    """The slice of the warehouse worth grounding this question on.

    Business tables are always included (they answer virtually every real
    question). Shadow/system tables join only when a word of the question
    names them: short words (4-5 chars) must equal a whole name token —
    "this" must not light up Accoun[this]tory — while longer words also
    match as substrings of the collapsed name, so "permissionset" finds
    PermissionSet. Asking about revenue never pays for ApexClass.
    """
    import re

    words = {w for w in re.findall(r"[a-z0-9_]+", question.lower()) if len(w) >= 4}

    def matches(table: str) -> bool:
        tokens = {
            t.lower()
            for t in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", table)
            if t
        }
        collapsed = re.sub(r"[^a-z0-9]", "", table.lower())
        return any(w in tokens or (len(w) >= 6 and w in collapsed) for w in words)

    keep: Dict[str, List[Tuple[str, str]]] = {}
    extras = 0
    for table, cols in schema.items():
        if table.startswith("_"):
            continue  # _sync_meta and friends: internal bookkeeping
        if _is_business_table(table):
            keep[table] = cols
        elif extras < _MAX_MATCHED_EXTRAS and matches(table):
            keep[table] = cols
            extras += 1
    return keep or dict(schema)


schema_cache = SchemaCache()
