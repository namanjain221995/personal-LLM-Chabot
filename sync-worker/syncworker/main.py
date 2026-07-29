"""Sync loop entrypoint.

Every SYNC_INTERVAL_MINUTES: for each configured object, run a Bulk API 2.0
full extract on first sync (no watermark) or an incremental REST SOQL query
(SystemModstamp > watermark) afterwards; land batches as Parquet, upsert
them into DuckDB, and re-index long-text fields into LanceDB. Failures back
off exponentially. All logs are structured JSON on stdout.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone

import pandas as pd

from .config import ObjectConfig, Settings, load_object_configs, load_settings
from .jsonlog import setup_logging
from .rag_index import OpenAIEmbedder, RagIndexer
from .secrets import fetch_sf_credentials
from .sf_auth import TokenManager
from .sf_client import SalesforceClient, build_full_soql, build_incremental_soql
from .storage import Store, normalize_records, sf_datetime_literal, write_parquet_batch

log = logging.getLogger("syncworker.main")

INITIAL_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 30 * 60.0


class _StopFlag:
    def __init__(self) -> None:
        self.stop = False

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle)

    def _handle(self, signum, frame) -> None:  # noqa: ARG002
        log.info("shutdown signal received", extra={"event": "shutdown", "signal": signum})
        self.stop = True

    def sleep(self, seconds: float) -> None:
        """Sleep in small increments so signals interrupt promptly."""
        deadline = time.monotonic() + seconds
        while not self.stop and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


#: SOQL can select these; the Bulk API rejects the whole query, so a compound
#: field adopted automatically would break the object it was added to.
COMPOUND_TYPES = ("address", "location")

#: Worth chunking and embedding for semantic search.
LONG_TEXT_TYPES = ("textarea", "richtextarea")

#: Fields that carry no analytical value and only widen every SELECT.
_NOISE_SUFFIXES = ("__History", "__Share", "__Feed")


def adopt_new_fields(
    object_name: str,
    fields: list[str],
    rag_fields: list[str],
    client: SalesforceClient,
    settings: Settings,
) -> tuple[list[str], list[str]]:
    """Add fields that exist in Salesforce but are not yet configured.

    New fields are additive and safe: they widen the SELECT and appear as new
    columns. Long-text ones also join the RAG index, so a newly created notes
    field becomes searchable without anyone touching the config.

    Compound fields are never adopted — the Bulk API refuses a query that
    selects one, so auto-adding it would break the whole object.
    """
    try:
        types = client.describe_field_types(object_name)
    except Exception:
        return fields, rag_fields

    known = set(fields)
    added, added_rag = [], []
    for name, ftype in types.items():
        if name in known or ftype in COMPOUND_TYPES:
            continue
        if name.endswith(_NOISE_SUFFIXES):
            continue
        if len(fields) + len(added) >= settings.sync_max_fields:
            break
        added.append(name)
        if ftype in LONG_TEXT_TYPES:
            added_rag.append(name)

    if added:
        log.info(
            "adopted new Salesforce fields",
            extra={"event": "fields_adopted", "object": object_name,
                   "fields": added, "indexed_for_search": added_rag},
        )
    return fields + added, rag_fields + added_rag


def sync_object(
    obj: ObjectConfig,
    client: SalesforceClient,
    store: Store,
    indexer: RagIndexer | None,
    settings: Settings,
) -> int:
    """Sync one object; returns the number of records processed."""
    watermark = store.get_watermark(obj.name)
    cycle_start = sf_datetime_literal(datetime.now(timezone.utc))

    # Drop configured fields this org/user cannot see (field-level security)
    # instead of failing the whole cycle on "No such column".
    fields, rag_fields = list(obj.fields), list(obj.rag_fields)
    try:
        visible = client.describe_fields(obj.name)
    except Exception:
        log.warning(
            "describe failed; using configured fields as-is",
            extra={"event": "describe_failed", "object": obj.name},
        )
    else:
        dropped = [f for f in fields if f not in visible]
        if dropped:
            log.warning(
                "skipping fields not visible in this org",
                extra={"event": "fields_skipped", "object": obj.name,
                       "fields": dropped},
            )
            fields = [f for f in fields if f in visible]
            rag_fields = [f for f in rag_fields if f in visible]

        # ADOPT fields added in Salesforce since the config was written.
        # Without this the config is a snapshot: a field created today stays
        # invisible to this platform until someone remembers to edit YAML, and
        # nothing reports that it is missing.
        if settings.sync_auto_fields:
            fields, rag_fields = adopt_new_fields(
                obj.name, fields, rag_fields, client, settings
            )

    if watermark is None:
        mode = "full"
        batches = client.bulk_query(build_full_soql(obj.name, fields))
    else:
        mode = "incremental"
        batches = client.soql_query(
            build_incremental_soql(obj.name, fields, watermark)
        )

    log.info(
        "object sync started",
        extra={"event": "object_sync_start", "object": obj.name, "mode": mode,
               "watermark": watermark},
    )

    total = 0
    for batch in batches:
        records = normalize_records(batch)
        df = pd.DataFrame(records)
        parquet_path = write_parquet_batch(df, obj.name, settings.parquet_dir)
        store.upsert(obj.name, df)
        total += len(records)
        log.info(
            "batch stored",
            extra={"event": "batch_stored", "object": obj.name, "rows": len(records),
                   "parquet": parquet_path},
        )
        if indexer is not None and rag_fields:
            try:
                indexer.index_records(obj.name, records, rag_fields)
            except Exception:
                # RAG indexing must not block the data sync; the warehouse
                # stays authoritative and indexing retries on the next change.
                log.error(
                    "rag indexing failed",
                    exc_info=True,
                    extra={"event": "rag_index_error", "object": obj.name},
                )

    # Watermark = time the extraction started, so records modified while the
    # extract ran are re-fetched next cycle (upsert makes that idempotent).
    store.set_watermark(obj.name, cycle_start)
    log.info(
        "object sync finished",
        extra={"event": "object_sync_done", "object": obj.name, "mode": mode,
               "rows": total, "new_watermark": cycle_start},
    )
    return total


def report_new_objects(
    objects: list[ObjectConfig], client: SalesforceClient
) -> list[str]:
    """Log objects that exist in Salesforce but are not configured.

    Deliberately NOT auto-adopted, unlike new fields. A new field widens an
    existing SELECT; a new object means a full extract of something nobody
    asked for — which on an org like this can be tens of thousands of rows,
    and may be an integration's private junk table. Surfacing it lets someone
    decide, and `python -m syncworker.objects add <Name>` is one command.
    """
    try:
        available = client.list_objects()
    except Exception:
        return []
    known = {o.name for o in objects}
    # Custom objects first: they are what a team actually builds and asks for.
    new = sorted(
        (n for n in available if n not in known and n.endswith("__c")),
    )
    if new:
        log.info(
            "Salesforce objects not yet synced — add with "
            "'python -m syncworker.objects add <Name> --fields ...'",
            extra={"event": "new_objects_available", "count": len(new),
                   "objects": new[:25]},
        )
    return new


def run_cycle(
    objects: list[ObjectConfig],
    client: SalesforceClient,
    store: Store,
    indexer: RagIndexer | None,
    settings: Settings,
) -> None:
    started = time.monotonic()
    if settings.sync_report_new_objects:
        report_new_objects(objects, client)
    total = 0
    failed: list = []
    for obj in objects:
        # One inaccessible/broken object must not block the other seven —
        # log it loudly, keep syncing, and retry it next cycle.
        try:
            total += sync_object(obj, client, store, indexer, settings)
        except Exception:
            failed.append(obj.name)
            log.error(
                "object sync failed; continuing with remaining objects",
                exc_info=True,
                extra={"event": "object_sync_error", "object": obj.name},
            )
    log.info(
        "sync cycle complete",
        extra={"event": "cycle_done", "objects": len(objects), "rows": total,
               "failed_objects": failed,
               "seconds": round(time.monotonic() - started, 1)},
    )


def main() -> None:
    setup_logging()
    settings = load_settings()
    objects = load_object_configs(settings.config_path)
    log.info(
        "sync worker starting",
        extra={"event": "startup", "objects": [o.name for o in objects],
               "interval_minutes": settings.sync_interval_minutes},
    )

    creds = fetch_sf_credentials()
    client = SalesforceClient(TokenManager(creds), settings.sf_api_version)
    indexer = RagIndexer(
        settings.lancedb_dir,
        OpenAIEmbedder(settings.embed_via, settings.embed_model),
    )

    flag = _StopFlag()
    flag.install()

    backoff = INITIAL_BACKOFF_SECONDS
    while not flag.stop:
        try:
            # DuckDB allows one writer OR many readers on a file. Hold the
            # write connection ONLY while a cycle runs so the orchestrator's
            # read-only sql engine is never locked out during the sleep.
            store = Store(settings.duckdb_path)
            try:
                run_cycle(objects, client, store, indexer, settings)
            finally:
                store.close()
            backoff = INITIAL_BACKOFF_SECONDS
            flag.sleep(settings.sync_interval_minutes * 60)
        except Exception:
            log.error(
                "sync cycle failed",
                exc_info=True,
                extra={"event": "cycle_error", "retry_in_seconds": backoff},
            )
            flag.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    log.info("sync worker stopped", extra={"event": "stopped"})


if __name__ == "__main__":
    main()
