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
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx
import pandas as pd

from .config import ObjectConfig, Settings, load_object_configs, load_settings
from .jsonlog import setup_logging
from .objects import is_credential_field
from .rag_index import OpenAIEmbedder, RagIndexer
from .secrets import fetch_sf_credentials
from .sf_auth import SalesforceAuthError, TokenManager
from .sf_client import (SalesforceClient, build_deleted_soql, build_full_soql,
                        build_incremental_soql)
from .storage import Store, normalize_records, sf_datetime_literal, write_parquet_batch

log = logging.getLogger("syncworker.main")

INITIAL_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 30 * 60.0
RAG_BACKFILL_RECORD_LIMIT = 500


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

#: Never adopted: base64 blobs break Bulk CSV results, and encrypted fields
#: are credentials that must not land in an LLM-queryable warehouse.
UNADOPTABLE_TYPES = COMPOUND_TYPES + ("base64", "encryptedstring")

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
        if name in known or ftype in UNADOPTABLE_TYPES or is_credential_field(name):
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


def _full_extract_batches(
    client: SalesforceClient, soql: str, object_name: str
) -> Iterator[list[dict]]:
    """Bulk API 2.0 first; REST SOQL fallback for entities Bulk cannot query.

    Picklist-master tables (CaseStatus, LeadStatus, OpportunityStage, ...)
    fail Bulk job creation with INVALIDENTITY. bulk_query is a generator, so
    that error surfaces on the FIRST batch — before any rows were yielded —
    which makes switching to plain REST here safe from double-processing.
    These tables are tiny; REST pagination handles them in one page.
    """
    batches = client.bulk_query(soql)
    try:
        first = next(batches)
    except StopIteration:
        return
    except httpx.HTTPStatusError as exc:
        reason = str(exc)
        if "INVALIDENTITY" not in reason and "not supported by the Bulk API" not in reason:
            raise
        log.info(
            "bulk api refused entity; full extract via REST instead",
            extra={"event": "bulk_fallback_rest", "object": object_name},
        )
        yield from client.soql_query(soql)
        return
    yield first
    yield from batches


def _purge_local(
    object_name: str,
    ids: list[str] | set[str],
    indexer: RagIndexer | None,
    *,
    source: str,
    store: Store | None = None,
) -> None:
    """Drop locally-held rows and RAG chunks for records deleted in Salesforce.

    `store=None` means the warehouse rows are already gone (reconcile_full
    deletes as it detects) and only the RAG index still needs purging.
    """
    ids = [str(i) for i in ids]
    if not ids:
        return
    rows_removed = store.delete_ids(object_name, ids) if store else len(ids)
    if indexer is not None:
        try:
            indexer.delete_records(ids)
        except Exception:
            # Same policy as indexing: RAG must not block the data sync.
            log.error(
                "rag purge failed",
                exc_info=True,
                extra={"event": "rag_purge_error", "object": object_name},
            )
    log.info(
        "deleted records purged",
        extra={"event": "deleted_purged", "object": object_name,
               "records": len(ids), "rows_removed": rows_removed,
               "source": source},
    )


def _record_ids(records: list[dict]) -> list[str]:
    return sorted({str(record["Id"]) for record in records if record.get("Id")})


def _index_or_defer(
    object_name: str,
    records: list[dict],
    rag_fields: list[str] | tuple[str, ...],
    indexer: RagIndexer,
    store: Store,
    *,
    source: str,
) -> bool:
    """Index warehouse-backed records or persist them for a later backfill."""
    record_ids = _record_ids(records)
    if not record_ids:
        return True
    try:
        chunks = indexer.index_records(object_name, records, tuple(rag_fields))
    except Exception as exc:
        # Persisting the retry marker is the condition for advancing the data
        # watermark. If that write itself fails, propagate: the unchanged
        # watermark makes Salesforce return this idempotent batch next cycle.
        store.mark_rag_pending(
            object_name,
            record_ids,
            f"{type(exc).__name__}: {exc}",
        )
        log.error(
            "rag indexing deferred; records remain queued for backfill",
            exc_info=True,
            extra={
                "event": "rag_index_deferred",
                "object": object_name,
                "records": len(record_ids),
                "source": source,
            },
        )
        return False
    store.clear_rag_pending(object_name, record_ids)
    if source == "backfill":
        log.info(
            "rag backfill completed",
            extra={
                "event": "rag_backfill_done",
                "object": object_name,
                "records": len(record_ids),
                "chunks": chunks,
            },
        )
    return True


def _retry_pending_rag(
    object_name: str,
    rag_fields: list[str] | tuple[str, ...],
    indexer: RagIndexer,
    store: Store,
) -> None:
    records = store.pending_rag_records(
        object_name, rag_fields, limit=RAG_BACKFILL_RECORD_LIMIT
    )
    if not records:
        return
    log.info(
        "retrying deferred rag records",
        extra={
            "event": "rag_backfill_start",
            "object": object_name,
            "records": len(records),
        },
    )
    _index_or_defer(
        object_name,
        records,
        rag_fields,
        indexer,
        store,
        source="backfill",
    )


def _recycle_bin_ids(client, obj: ObjectConfig, watermark: str) -> list[str]:
    """Ids soft-deleted since the watermark, or [] when the bin cannot be read.

    Best effort by design: visibility follows sharing rules and the bin keeps
    ~15 days, so a failure here is logged and the sync continues. `python -m
    syncworker.objects resync <Object>` forces the exact full-extract
    reconcile if drift is suspected.
    """
    try:
        deleted: list[str] = []
        for batch in client.soql_query_all(
            build_deleted_soql(obj.name, watermark, obj.watermark_field)
        ):
            deleted.extend(str(r["Id"]) for r in batch if r.get("Id"))
        return deleted
    except Exception:
        log.error(
            "delete detection failed; local copy may keep deleted rows",
            exc_info=True,
            extra={"event": "delete_sync_error", "object": obj.name},
        )
        return []


def sync_object(
    obj: ObjectConfig,
    client: SalesforceClient,
    store: Store,
    indexer: RagIndexer | None,
    settings: Settings,
) -> int:
    """Sync one object; returns the number of records processed.

    LOCK DISCIPLINE. The warehouse is opened in exactly two short sessions:
    one for the DuckDB work before the extract, one for the work after it.
    Every Salesforce call, every embedding call and the extract itself run
    BETWEEN them with the file unlocked, because a session holds the write
    lock for its whole lifetime and the orchestrator's queries cannot open
    the file at all while it does. Opening the warehouse costs ~0.3 s on this
    catalog, so the two sessions replace four to five per-operation opens
    without ever holding the lock across network I/O.
    """
    started = time.monotonic()
    wm_field = obj.watermark_field
    # Watermark = time the extraction started, so records modified while the
    # extract ran are re-fetched next cycle (upsert makes that idempotent).
    cycle_start = sf_datetime_literal(datetime.now(timezone.utc))

    # Drop configured fields this org/user cannot see (field-level security)
    # instead of failing the whole cycle on "No such column".
    fields, rag_fields = list(obj.fields), list(obj.rag_fields)
    # Some objects are never soft-deleted and carry no IsDeleted at all
    # (User is deactivated, not deleted) — asking queryAll about them is a
    # guaranteed INVALID_FIELD every cycle.
    supports_deletes = True
    try:
        visible = client.describe_fields(obj.name)
    except SalesforceAuthError:
        # A refused credential is not a describe problem, and pretending it is
        # ("using configured fields as-is") hides the real fault behind a
        # warning while the sync marches on to fail again on the next object.
        raise
    except Exception:
        log.warning(
            "describe failed; using configured fields as-is",
            extra={"event": "describe_failed", "object": obj.name},
        )
    else:
        supports_deletes = wm_field is not None and "IsDeleted" in visible
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

    with store.session():
        # No watermark field → no incremental filter exists for this object;
        # every cycle is a full extract, kept honest by reconcile_full.
        watermark = store.get_watermark(obj.name) if wm_field else None

        # A configured object with zero records must still exist as an (empty)
        # table: "how many X are there" should answer 0, not "table not found".
        added_columns = store.ensure_table(obj.name, fields)

        # A COLUMN THAT JUST APPEARED NEEDS ONE FULL EXTRACT.
        #
        # It holds NULL for every row already stored, and the incremental
        # SELECT below fetches only records modified since the watermark —
        # typically a handful. Nothing would ever revisit the rest: the
        # watermark has already moved past them. The column then reads as
        # "almost entirely empty", and "how many candidates have X" answers 2
        # instead of 3,400 with no error to suggest anything is wrong.
        #
        # The trigger is the warehouse's own schema, not "was a field adopted
        # this cycle": adoption compares describe against the YAML config,
        # which is never rewritten, so an adopted field is re-adopted EVERY
        # cycle and that test would force a full extract every five minutes
        # forever. A column is added exactly once.
        #
        # Two things happen, and both matter. The stored watermark is CLEARED
        # so that if this extract fails partway — leaving the column half
        # populated — the next cycle is full again and finishes the job; a
        # successful cycle re-stamps it below and the object goes back to
        # incremental. The local variable is ALSO dropped, because the
        # full-vs-incremental decision for this cycle reads it, not the row.
        if added_columns and watermark is not None:
            log.info(
                "columns added to an existing table; forcing a full extract "
                "to backfill them",
                extra={"event": "adoption_backfill", "object": obj.name,
                       "columns": added_columns},
            )
            store.clear_watermark(obj.name)
            watermark = None

    # Retry failures from PRIOR cycles before fetching changes. This runs even
    # when the incremental query returns zero rows, closing the old hole where
    # an embedding service that was cold on first sync left records unindexed
    # forever after their data watermark advanced. Outside the session: it
    # talks to the embedding service.
    if indexer is not None and rag_fields:
        _retry_pending_rag(obj.name, rag_fields, indexer, store)

    if watermark is None:
        mode = "full"
        batches = _full_extract_batches(
            client, build_full_soql(obj.name, fields), obj.name
        )
    else:
        mode = "incremental"
        batches = client.soql_query(
            build_incremental_soql(obj.name, fields, watermark, wm_field)
        )

    log.info(
        "object sync started",
        extra={"event": "object_sync_start", "object": obj.name, "mode": mode,
               "watermark": watermark},
    )

    # Batches stream from Salesforce and are written as they arrive, so this
    # loop interleaves HTTP with DuckDB writes by nature. Each upsert takes
    # its own short connection; the lock is free while the next page loads.
    total = 0
    extracted_ids: set[str] = set()
    for batch in batches:
        records = normalize_records(batch)
        df = pd.DataFrame(records)
        parquet_path = write_parquet_batch(df, obj.name, settings.parquet_dir)
        store.upsert(obj.name, df)
        total += len(records)
        if mode == "full":
            extracted_ids.update(
                str(r["Id"]) for r in records if r.get("Id")
            )
        log.info(
            "batch stored",
            extra={"event": "batch_stored", "object": obj.name, "rows": len(records),
                   "parquet": parquet_path},
        )
        if indexer is not None and rag_fields:
            _index_or_defer(
                obj.name,
                records,
                rag_fields,
                indexer,
                store,
                source="changed_batch",
            )

    # Records deleted in Salesforce used to live here forever — the
    # SystemModstamp filter cannot see them. Two complementary answers:
    # a FULL extract is a complete snapshot, so local rows absent from it
    # are gone from the org (exact); an incremental cycle asks the recycle
    # bin via queryAll for Ids soft-deleted since the watermark (best effort).
    # The bin is read here, before the lock is taken again.
    deleted: list[str] = []
    if mode == "incremental" and supports_deletes:
        deleted = _recycle_bin_ids(client, obj, watermark)

    # Describe is cached from the top of this function, so this is normally
    # free; the one case it is not (that describe failed) is a network call,
    # which is why it happens out here.
    try:
        specs = client.describe_field_specs(obj.name)
    except Exception:
        specs = []

    with store.session():
        if mode == "full" and total > 0:
            _purge_local(obj.name, store.reconcile_full(obj.name, extracted_ids),
                         indexer, source="full_reconcile")
        elif deleted:
            try:
                _purge_local(obj.name, deleted, indexer, source="recycle_bin",
                             store=store)
            except Exception:
                # Same policy as reading the bin: best effort, never blocking.
                log.error(
                    "delete purge failed; local copy may keep deleted rows",
                    exc_info=True,
                    extra={"event": "delete_sync_error", "object": obj.name},
                )

        # Objects with no watermark field stay unstamped: full extract every
        # cycle.
        if wm_field:
            store.set_watermark(obj.name, cycle_start)

        # Rebuild this object's typed view from the SAME describe the extract
        # used, so the view can never describe a shape the table does not
        # have. Runs every cycle rather than once: a field adopted above needs
        # a column in the view, and an unchanged view is skipped cheaply.
        #
        # Deliberately AFTER the watermark. A failure here must not make the
        # sync re-fetch data it already stored correctly -- the raw table is
        # the durable copy and stays queryable with or without a view.
        if specs:
            store.refresh_typed_view(obj.name, specs, settings.sf_org_timezone)

    log.info(
        "object sync finished",
        extra={"event": "object_sync_done", "object": obj.name, "mode": mode,
               "rows": total, "new_watermark": cycle_start,
               "seconds": round(time.monotonic() - started, 2)},
    )
    return total


#: Standard objects worth adopting the moment this user gains access to them
#: (the org admin grants Read later) — activity and email history. Everything
#: else standard is either already configured or Salesforce plumbing.
WANTED_STANDARD_OBJECTS = frozenset({"Task", "Event", "EmailMessage", "TaskStatus"})

#: Auto-generated shadows of a base object — never data in their own right.
_COMPANION_OBJECT_SUFFIXES = ("ChangeEvent", "__Share", "__History", "__Feed", "__hd")

#: Incremental-sync timestamp, in preference order. SystemModstamp where it
#: exists; Share/History/Feed shadows and some setup objects only carry
#: LastModifiedDate or CreatedDate.
WATERMARK_CANDIDATES = ("SystemModstamp", "LastModifiedDate", "CreatedDate")


def discover_new_objects(
    objects: list[ObjectConfig],
    client: SalesforceClient,
    settings: Settings,
) -> list[ObjectConfig]:
    """Adopt objects that exist in Salesforce but are not configured.

    Owner-requested (SYNC_AUTO_OBJECTS): a custom object created in the org —
    or a wanted standard object this user is newly allowed to read — starts
    syncing on the next cycle with every adoptable field, no config edit.
    Derived fresh each cycle rather than persisted (config.yaml is mounted
    read-only); the watermark in _sync_meta still makes re-syncs incremental.
    Only objects with Id + SystemModstamp qualify — nothing else can be
    upserted and watermarked.
    """
    if not settings.sync_auto_objects:
        return []
    try:
        available = client.list_objects()
    except SalesforceAuthError:
        # Discovery is best-effort, but a refused credential is not something
        # to shrug off with an empty list — it is the first call of the cycle
        # and re-raising here means one token request per cycle, not one per
        # object after it.
        raise
    except Exception:
        return []
    known = {o.name for o in objects}
    adopted: list[ObjectConfig] = []
    for name in sorted(available):
        if name in known or name.endswith(_COMPANION_OBJECT_SUFFIXES):
            continue
        if not (name.endswith("__c") or name in WANTED_STANDARD_OBJECTS):
            continue
        try:
            types = client.describe_field_types(name)
        except Exception:
            continue
        wm = next((f for f in WATERMARK_CANDIDATES if f in types), None)
        if "Id" not in types or wm is None:
            continue
        fields, rag = ["Id"], []
        for f, ftype in types.items():
            if f == "Id" or ftype in UNADOPTABLE_TYPES or is_credential_field(f):
                continue
            if len(fields) >= settings.sync_max_fields:
                break
            fields.append(f)
            if ftype in LONG_TEXT_TYPES:
                rag.append(f)
        if wm not in fields:
            fields.append(wm)
        adopted.append(ObjectConfig(name, tuple(fields), tuple(rag), wm))
        log.info(
            "adopted new Salesforce object",
            extra={"event": "object_adopted", "object": name,
                   "fields": len(fields), "indexed_for_search": rag},
        )
    return adopted


def report_new_objects(
    objects: list[ObjectConfig], client: SalesforceClient
) -> list[str]:
    """Log objects that exist in Salesforce but are not configured.

    The passive counterpart to discover_new_objects, for deployments where
    auto-adoption is OFF: a new object means a full extract of something
    nobody asked for — which on an org like this can be tens of thousands of
    rows, and may be an integration's private junk table. Surfacing it lets
    someone decide, and `python -m syncworker.objects add <Name>` is one
    command.
    """
    try:
        available = client.list_objects()
    except SalesforceAuthError:
        # Same reasoning as discover_new_objects: an empty list here would
        # report "no new objects" when the truth is that nobody could log in.
        raise
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
    connects_before = store.connects
    # Fresh describes every cycle: fields created in Salesforce since the
    # last cycle must be visible to adopt_new_fields without a restart.
    client.clear_describe_cache()
    objects = objects + discover_new_objects(objects, client, settings)
    if settings.sync_report_new_objects and not settings.sync_auto_objects:
        report_new_objects(objects, client)
    total = 0
    failed: list = []
    for position, obj in enumerate(objects):
        # One inaccessible/broken object must not block the other seven —
        # log it loudly, keep syncing, and retry it next cycle.
        try:
            total += sync_object(obj, client, store, indexer, settings)
        except SalesforceAuthError as exc:
            # Not per-object: the org has refused our credentials, so every
            # remaining object would fail the same way. Continuing would fire
            # two doomed token requests per object — hundreds per cycle — which
            # buys nothing and risks tripping Salesforce's login lockout.
            log.error(
                "salesforce authentication failed; abandoning this cycle",
                extra={"event": "auth_failed", "object": obj.name,
                       "sf_error": exc.error, "detail": str(exc),
                       "objects_skipped": len(objects) - position},
            )
            raise
        except Exception:
            failed.append(obj.name)
            log.error(
                "object sync failed; continuing with remaining objects",
                exc_info=True,
                extra={"event": "object_sync_error", "object": obj.name},
            )
    # Publish the readers' snapshot BEFORE announcing the cycle, so "complete"
    # means "and the orchestrator can see it". Failure here is logged and never
    # fatal: the previous snapshot stays in place and stays queryable.
    published = ""
    try:
        published = store.publish_snapshot()
    except Exception:  # noqa: BLE001 — a stale snapshot beats a broken cycle
        log.error(
            "failed to publish the warehouse snapshot; readers keep the previous one",
            exc_info=True,
            extra={"event": "snapshot_publish_error"},
        )
    log.info(
        "sync cycle complete",
        extra={"event": "cycle_done", "objects": len(objects), "rows": total,
               "snapshot_published": bool(published),
               "failed_objects": failed,
               "seconds": round(time.monotonic() - started, 1),
               # Read-write opens of the warehouse this cycle. ~2 per object
               # is the design; ~4-5 means the sessions are not taking effect.
               "warehouse_connects": store.connects - connects_before},
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
        OpenAIEmbedder(
            settings.embed_via,
            settings.embed_model,
            api_key=settings.embed_api_key,
        ),
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
