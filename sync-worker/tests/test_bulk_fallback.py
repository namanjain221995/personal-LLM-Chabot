"""Entities the Bulk API refuses (INVALIDENTITY) fall back to REST SOQL.

Picklist-master tables like CaseStatus, LeadStatus and OpportunityStage are
real, queryable business data — the stage/status vocabularies the LLM needs —
but Bulk API 2.0 rejects the whole query job for them. The failure happens at
job creation, before any batch is yielded, so switching to REST is safe.
"""
import httpx
import pytest

from syncworker.main import _full_extract_batches


def _http_error(body: str) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.my.salesforce.com/jobs/query")
    resp = httpx.Response(400, request=req)
    return httpx.HTTPStatusError(
        f"Client error '400 Bad Request' | salesforce says: {body}",
        request=req, response=resp,
    )


class FakeClient:
    def __init__(self, bulk_error=None, bulk_batches=(), rest_batches=()):
        self._bulk_error = bulk_error
        self._bulk_batches = list(bulk_batches)
        self._rest_batches = list(rest_batches)
        self.rest_called = False

    def bulk_query(self, soql):
        if self._bulk_error is not None:
            raise self._bulk_error
        yield from self._bulk_batches

    def soql_query(self, soql):
        self.rest_called = True
        yield from self._rest_batches


ROWS = [[{"Id": "1", "MasterLabel": "New"}], [{"Id": "2", "MasterLabel": "Closed"}]]


def test_invalidentity_falls_back_to_rest():
    client = FakeClient(
        bulk_error=_http_error('[{"errorCode":"INVALIDENTITY","message":"Entity '
                               "'CaseStatus' is not supported by the Bulk API.\"}]"),
        rest_batches=ROWS,
    )
    got = list(_full_extract_batches(client, "SELECT Id FROM CaseStatus", "CaseStatus"))
    assert got == ROWS and client.rest_called


def test_a_supported_entity_streams_through_bulk_untouched():
    client = FakeClient(bulk_batches=ROWS)
    got = list(_full_extract_batches(client, "SELECT Id FROM Account", "Account"))
    assert got == ROWS and not client.rest_called


def test_other_http_errors_still_raise():
    """Only the known can't-bulk-this-entity error may divert to REST; a 400
    from e.g. a malformed query must stay loud."""
    client = FakeClient(bulk_error=_http_error('[{"errorCode":"API_ERROR"}]'),
                        rest_batches=ROWS)
    with pytest.raises(httpx.HTTPStatusError):
        list(_full_extract_batches(client, "SELECT bogus FROM Account", "Account"))
    assert not client.rest_called


def test_an_empty_bulk_result_stays_empty():
    client = FakeClient(bulk_batches=[])
    assert list(_full_extract_batches(client, "SELECT Id FROM Account", "Account")) == []
    assert not client.rest_called
