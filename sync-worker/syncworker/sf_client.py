"""Read-only Salesforce data client.

STRICTLY READ-ONLY: this module touches only
  - POST /services/data/{v}/jobs/query        (create a Bulk API 2.0 QUERY job)
  - GET  /services/data/{v}/jobs/query/{id}   (poll job state)
  - GET  /services/data/{v}/jobs/query/{id}/results
  - GET  /services/data/{v}/query             (REST SOQL, incremental)
No endpoint that creates, updates or deletes Salesforce data is ever called.

Every response's Sforce-Limit-Info header is parsed and a warning is logged
when daily API usage exceeds 80%.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from collections.abc import Iterator

import httpx

from .sf_auth import TokenManager

log = logging.getLogger("syncworker.sf_client")

LIMIT_INFO_HEADER = "Sforce-Limit-Info"
LIMIT_WARN_THRESHOLD = 0.80
READ_ONLY_BULK_OPERATIONS = frozenset({"query", "queryAll"})

_API_USAGE_RE = re.compile(r"api-usage=(\d+)/(\d+)")
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def parse_limit_info(header_value: str | None) -> tuple[int, int] | None:
    """Parse 'api-usage=used/total' out of a Sforce-Limit-Info header.

    Returns (used, total) or None if the header is absent/malformed.
    """
    if not header_value:
        return None
    match = _API_USAGE_RE.search(header_value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def check_api_limits(
    header_value: str | None, logger: logging.Logger = log
) -> float | None:
    """Log a warning when API usage is at/above LIMIT_WARN_THRESHOLD.

    Returns the usage ratio (0..1) when the header parsed, else None.
    """
    parsed = parse_limit_info(header_value)
    if parsed is None:
        return None
    used, total = parsed
    if total <= 0:
        return None
    ratio = used / total
    if ratio >= LIMIT_WARN_THRESHOLD:
        logger.warning(
            "salesforce daily api usage above %d%%",
            int(LIMIT_WARN_THRESHOLD * 100),
            extra={
                "event": "sf_api_limit_warning",
                "api_used": used,
                "api_total": total,
                "api_pct": round(ratio * 100, 1),
            },
        )
    return ratio


def _validate_identifiers(object_name: str, fields: list[str] | tuple[str, ...]):
    if not _IDENT_RE.match(object_name):
        raise ValueError(f"invalid object name: {object_name!r}")
    for f in fields:
        if not _IDENT_RE.match(f):
            raise ValueError(f"invalid field name: {f!r}")


def build_full_soql(object_name: str, fields: tuple[str, ...] | list[str]) -> str:
    _validate_identifiers(object_name, fields)
    return f"SELECT {', '.join(fields)} FROM {object_name}"


_WATERMARK_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?(Z|[+-]\d{4})$"
)


def build_incremental_soql(
    object_name: str,
    fields: tuple[str, ...] | list[str],
    watermark: str,
    watermark_field: str = "SystemModstamp",
) -> str:
    """SOQL for records modified after the watermark (SF datetime literal).

    watermark_field is SystemModstamp where it exists; Share/History/Feed
    shadows and some setup objects only carry LastModifiedDate or CreatedDate.
    """
    _validate_identifiers(object_name, [*fields, watermark_field])
    if not _WATERMARK_RE.match(watermark):
        raise ValueError(f"invalid watermark datetime literal: {watermark!r}")
    return (
        f"SELECT {', '.join(fields)} FROM {object_name} "
        f"WHERE {watermark_field} > {watermark} ORDER BY {watermark_field} ASC"
    )


def build_deleted_soql(
    object_name: str, watermark: str, watermark_field: str = "SystemModstamp"
) -> str:
    """SOQL for records soft-deleted after the watermark (queryAll only).

    Deleting a record updates its SystemModstamp, so the same watermark that
    drives the incremental sync also finds the deletes. Plain /query never
    returns IsDeleted rows — run this through soql_query_all.
    """
    _validate_identifiers(object_name, ["Id", watermark_field])
    if not _WATERMARK_RE.match(watermark):
        raise ValueError(f"invalid watermark datetime literal: {watermark!r}")
    return (
        f"SELECT Id FROM {object_name} "
        f"WHERE IsDeleted = true AND {watermark_field} > {watermark}"
    )


class SalesforceClient:
    """Thin read-only wrapper over the Bulk API 2.0 query and REST SOQL APIs."""

    def __init__(
        self,
        token_manager: TokenManager,
        api_version: str = "v61.0",
        http: httpx.Client | None = None,
        poll_interval: float = 5.0,
        bulk_page_size: int = 10000,
    ) -> None:
        self._tm = token_manager
        self._v = api_version
        self._http = http or httpx.Client(timeout=120.0)
        self._poll_interval = poll_interval
        self._bulk_page_size = bulk_page_size

    # ── plumbing ────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict | None = None,
        _retry_auth: bool = True,
        **kwargs,
    ) -> httpx.Response:
        if method not in ("GET", "POST"):  # POST only ever creates query jobs
            raise ValueError(f"method {method} not allowed (read-only client)")
        token, instance_url = self._tm.get_token()
        url = f"{instance_url}{path}" if path.startswith("/") else path
        merged = {"Authorization": f"Bearer {token}", **(headers or {})}
        resp = self._http.request(method, url, headers=merged, **kwargs)
        if resp.status_code == 401 and _retry_auth:
            log.info("access token rejected, refreshing", extra={"event": "sf_token_refresh"})
            self._tm.invalidate()
            return self._request(
                method, path, headers=headers, _retry_auth=False, **kwargs
            )
        check_api_limits(resp.headers.get(LIMIT_INFO_HEADER))
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Salesforce puts the actual reason (errorCode + message) in the
            # response body; surface it or 4xx errors are undebuggable.
            # Error bodies never contain credentials.
            detail = resp.text[:400].replace("\n", " ")
            raise httpx.HTTPStatusError(
                f"{exc.args[0] if exc.args else exc} | salesforce says: {detail}",
                request=exc.request,
                response=exc.response,
            ) from None
        return resp

    def describe_field_types(self, object_name: str) -> dict:
        """{field name: Salesforce type} for fields visible to this user (cached)."""
        cache = getattr(self, "_describe_cache", None)
        if cache is None:
            cache = self._describe_cache = {}
        if object_name not in cache:
            resp = self._request(
                "GET", f"/services/data/{self._v}/sobjects/{object_name}/describe"
            )
            cache[object_name] = {
                f["name"]: f.get("type", "") for f in resp.json().get("fields", [])
            }
        return cache[object_name]

    def describe_fields(self, object_name: str) -> set:
        """Field names actually visible to this user in this org (cached).

        Orgs hide fields via field-level security; querying a hidden field
        fails with "No such column". The sync filters its configured field
        list against this set instead of failing the whole cycle.
        """
        return set(self.describe_field_types(object_name))

    def clear_describe_cache(self) -> None:
        """Forget cached describes so the next cycle sees fields created since.

        The cache used to live for the whole process, which meant a field
        added in Salesforce while the worker ran was never auto-adopted until
        the container restarted. Clearing per cycle costs one describe call
        per object per cycle — negligible against org API limits.
        """
        self._describe_cache = {}

    def list_objects(self) -> dict:
        """{API name: label} for every queryable object this user can see."""
        resp = self._request("GET", f"/services/data/{self._v}/sobjects/")
        return {
            s["name"]: s.get("label", s["name"])
            for s in resp.json().get("sobjects", [])
            if s.get("queryable") and not s.get("deprecatedAndHidden")
        }

    # ── Bulk API 2.0 full extract ───────────────────────────────────────────

    def bulk_query(self, soql: str, operation: str = "query") -> Iterator[list[dict]]:
        """Run a Bulk API 2.0 query job; yield batches of record dicts (CSV rows)."""
        if operation not in READ_ONLY_BULK_OPERATIONS:
            raise ValueError(f"bulk operation must be read-only, got {operation!r}")

        payload = {"operation": operation, "query": soql}
        resp = self._request(
            "POST", f"/services/data/{self._v}/jobs/query", json=payload
        )
        job_id = resp.json()["id"]
        log.info(
            "bulk query job created",
            extra={"event": "bulk_job_created", "job_id": job_id},
        )

        while True:
            info = self._request(
                "GET", f"/services/data/{self._v}/jobs/query/{job_id}"
            ).json()
            state = info.get("state")
            if state == "JobComplete":
                break
            if state in ("Failed", "Aborted"):
                raise RuntimeError(
                    f"bulk query job {job_id} ended in state {state}: "
                    f"{info.get('errorMessage', '')}"
                )
            time.sleep(self._poll_interval)

        locator: str | None = None
        while True:
            params: dict = {"maxRecords": self._bulk_page_size}
            if locator:
                params["locator"] = locator
            resp = self._request(
                "GET",
                f"/services/data/{self._v}/jobs/query/{job_id}/results",
                params=params,
                headers={"Accept": "text/csv"},
            )
            rows = [dict(r) for r in csv.DictReader(io.StringIO(resp.text))]
            if rows:
                yield rows
            locator = resp.headers.get("Sforce-Locator")
            if not locator or locator == "null":
                break

    # ── incremental REST SOQL ───────────────────────────────────────────────

    def soql_query(self, soql: str) -> Iterator[list[dict]]:
        """Run a REST SOQL query; yield batches, following nextRecordsUrl."""
        yield from self._soql_query_endpoint("query", soql)

    def soql_query_all(self, soql: str) -> Iterator[list[dict]]:
        """Like soql_query but via /queryAll, which includes soft-deleted rows.

        Still strictly read-only — queryAll only widens visibility to the
        recycle bin. Used for delete detection (build_deleted_soql).
        """
        yield from self._soql_query_endpoint("queryAll", soql)

    def _soql_query_endpoint(self, endpoint: str, soql: str) -> Iterator[list[dict]]:
        resp = self._request(
            "GET", f"/services/data/{self._v}/{endpoint}", params={"q": soql}
        )
        body = resp.json()
        while True:
            records = [
                {k: v for k, v in rec.items() if k != "attributes"}
                for rec in body.get("records", [])
            ]
            if records:
                yield records
            next_url = body.get("nextRecordsUrl")
            if body.get("done", True) or not next_url:
                break
            body = self._request("GET", next_url).json()
