"""The Files API published (2026-09-14): one vocabulary, one route table and
one schema.

Before this, `/v1/files` and `/v1/uploads` were mounted and served but not
published, and three places disagreed about what a client could meet:

* the seven Files error codes lived in `files/wire.FILE_CODES`, outside the
  closed table of `publicapi/errors.py`, so the OpenAPI `code` enum — and every
  SDK generated from it — said `file_not_found` could not exist;
* CONTRACT §7 listed eleven routes while the router served twenty-five;
* the OpenAPI document named a `file.progress` event the events route never
  sends, and no `411` on the raw part route that answers one.

(Citations on a streamed answer are not a seam of this file: the
`response.output_text.annotation.added` event belongs to the stream grammar
of `publicapi/events.py` and is tested with it.)

Each test below reads the primary source on both sides of one of those seams,
so the sides cannot drift apart again without a red build.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Dict

import pytest

from app.apifiles import events as file_events, service
from app.publicapi import errors, models, openapi as openapi_module, router as public_router
from app.publicapi.files import routes as file_routes, wire

REPO = Path(__file__).resolve().parents[2]
CONTRACT = (REPO / "docs" / "developer-platform" / "CONTRACT.md").read_text(encoding="utf-8")
FILE_INPUTS_PAGE = (REPO / "frontend" / "content" / "docs" / "pages" / "fileInputs.ts").read_text(encoding="utf-8")
SURFACE = (REPO / ".github" / "workflows" / "scripts" / "public-api-surface.txt").read_text(encoding="utf-8")

FILES_CODES = {
    "file_not_found",
    "upload_not_found",
    "file_not_ready",
    "upload_state_conflict",
    "checksum_mismatch",
    "incomplete_body",
    "storage_unavailable",
}


def _section(start: str, end: str) -> str:
    begin = CONTRACT.index(start)
    return CONTRACT[begin:CONTRACT.index(end, begin + 1)]


# ----------------------------------------------------- the error vocabulary --


def test_every_files_code_is_a_row_of_the_closed_table_with_the_status_and_type_the_files_routes_send():
    assert set(wire.FILE_CODES) == FILES_CODES
    table = errors.error_codes()
    for code, (status, wire_type, _should_retry) in wire.FILE_CODES.items():
        assert code in table, f"{code} is outside the closed table"
        assert (table[code].status, table[code].type) == (status, wire_type), code
        retry_after = 60 if status == 503 else None
        built = wire.FilesApiError(code, "x", retry_after=retry_after)
        assert (built.status, built.type) == (status, wire_type)


def test_the_files_error_class_takes_status_and_type_from_the_table_and_refuses_a_code_it_does_not_hold():
    with pytest.raises(ValueError):
        wire.FilesApiError("file_vanished", "no")
    # The two statuses a code may take besides its row stay overridable.
    assert wire.length_required().status == 411 and wire.length_required().code == "invalid_request_error"
    assert wire.range_not_satisfiable(10).status == 416
    assert wire.range_not_satisfiable(10).headers()["Content-Range"] == "bytes */10"


def test_the_model_input_facade_builds_a_plain_api_error_for_a_files_code_now_that_the_table_holds_it():
    for built in (service.file_not_found("input.0.content.0.file_id"), service.file_not_ready("input", 5)):
        assert type(built) is errors.ApiError
        assert built.status == errors.status_for(built.code)


def test_a_file_not_ready_and_a_cut_body_are_retryable_and_a_full_disk_is_left_to_its_header():
    assert {"file_not_ready", "incomplete_body"} <= errors.RETRYABLE_CODES
    assert "storage_unavailable" not in errors.RETRYABLE_CODES
    assert wire.storage_unavailable().headers()["x-should-retry"] == "false"
    assert wire.storage_busy().headers()["x-should-retry"] == "true"


def test_the_contract_error_table_is_exactly_the_closed_table_with_each_codes_status():
    section = _section("## 9. Response and error envelope", "## 10. Streaming")
    rows = {m.group(1): int(m.group(2)) for m in re.finditer(r"^\| `([a-z_]+)` \| (\d{3}) \|", section, re.M)}
    assert rows == {code: spec.status for code, spec in errors.error_codes().items()}
    assert "`storage_unavailable` | 503 |" in section and "type `api_error`" in section


# ------------------------------------------------------ the route table --


def _contract_routes() -> Dict[str, str]:
    section = _section("## 7. Public endpoints", "### Scopes")
    return {
        f"{m.group(1)} {m.group(2)}": m.group(3)
        for m in re.finditer(r"^\| (GET|POST|PUT|PATCH|DELETE) \| `(/v1/[^`]+)` \| (`[a-z.]+`|none) \|", section, re.M)
    }


def _surface_routes() -> set:
    return {line.strip() for line in SURFACE.splitlines() if line.strip() and not line.lstrip().startswith("#")}


def test_the_contract_the_ci_surface_file_and_the_served_schema_name_the_same_twenty_five_operations():
    contract = _contract_routes()
    document = openapi_module.public_openapi()
    described = {f"{method.upper()} {path}" for path, item in document["paths"].items() for method in item}
    assert public_router.FILES_MOUNTED
    assert len(contract) == 25
    assert set(contract) == _surface_routes() == described


def test_each_file_route_in_the_contract_names_the_one_scope_its_handler_checks():
    document = openapi_module.public_openapi()
    file_routes_seen = 0
    for name, scope in _contract_routes().items():
        method, path = name.split(" ", 1)
        if not path.startswith(("/v1/files", "/v1/uploads")):
            continue
        file_routes_seen += 1
        description = document["paths"][path][method.lower()]["description"]
        assert description.startswith(f"Scope {scope} "), (name, scope, description[:60])
        assert scope.strip("`") in (file_routes.SCOPE_READ, file_routes.SCOPE_WRITE)
    assert file_routes_seen == 14
    # Resuming an upload is part of writing it (CONTRACT §7).
    assert _contract_routes()["GET /v1/uploads/{upload_id}"] == "`files.write`"


def test_the_contract_no_longer_lists_uploads_among_what_is_deliberately_not_exposed():
    section = _section("## 7. Public endpoints", "## 8. Request contract")
    not_exposed = section[section.index("Not exposed, deliberately"):]
    assert "deep research,\nuploads," not in not_exposed
    assert "the chat application's own uploads" in not_exposed
    assert "### 8.7 Files and uploads" in CONTRACT


# ------------------------------------------------------------- the schema --


def test_the_schemas_error_code_enum_is_the_closed_table_including_the_files_codes():
    envelope = openapi_module.public_openapi()["components"]["schemas"]["ErrorEnvelope"]["properties"]["error"]
    assert set(envelope["properties"]["code"]["enum"]) == set(errors.error_codes())
    assert FILES_CODES <= set(envelope["properties"]["code"]["enum"])
    assert "api_error" in envelope["properties"]["type"]["enum"]


def test_a_raw_part_documents_its_411_and_every_files_retry_status_documents_x_should_retry():
    paths = openapi_module.public_openapi()["paths"]
    put_part = paths["/v1/uploads/{upload_id}/parts/{part_number}"]["put"]["responses"]
    assert "411" in put_part
    for status in ("408", "409", "503"):
        assert "x-should-retry" in put_part[status]["headers"], status
        assert put_part[status]["headers"]["Retry-After"]["schema"]["minimum"] == 1
    create = paths["/v1/files"]["post"]["responses"]
    assert "storage_unavailable" in create["503"]["description"]
    assert "incomplete_body" in create["408"]["description"]
    derived = paths["/v1/files/{file_id}/derived"]["get"]["responses"]
    assert "file_not_ready" in derived["409"]["description"]


def test_the_file_events_operation_names_the_events_the_stream_really_sends():
    description = openapi_module.public_openapi()["paths"]["/v1/files/{file_id}/events"]["get"]["description"]
    for name in (file_events.FILE_PROCESSING, file_events.FILE_PROCESSED, file_events.FILE_FAILED):
        assert f"`{name}`" in description
    assert "file.progress" not in description


# ------------------------------------------ what a failed file's derived data is --


def _derived_row() -> str:
    section = _section("### 8.7 Files and uploads", "## 9. Response and error envelope")
    return re.search(r"^\| `GET …/derived`, `…/derived/\{name\}` \|.*$", section, re.M).group(0)


def _contract_code_row(code: str) -> str:
    section = _section("## 9. Response and error envelope", "## 10. Streaming")
    return re.search(rf"^\| `{code}` \|.*$", section, re.M).group(0)


def test_the_contract_keeps_409_for_a_file_still_processing_and_sends_a_failed_files_derived_data_to_a_400():
    # A retryable 409 on a file whose processing FAILED told a client
    # following the contract to retry for ever; the file never gets derived
    # data until its bytes are uploaded again.
    row = _derived_row()
    assert "while the file is assembling, queued or processing, `409 file_not_ready` with `Retry-After: 5`" in row
    assert "a file in `error`" in row
    assert "`400 invalid_request_error`, `param: file_id`" in row
    assert '"This file has no derived data: "' in row and "`status_details`" in row
    assert "not retry-safe" in row
    assert "before `processed`" not in row
    code_row = _contract_code_row("file_not_ready")
    assert "retry-safe" in code_row and "Never sent for a file in `error`" in code_row
    assert "before `processed`" not in code_row


def test_the_contracts_derived_answers_are_the_ones_the_derived_gate_sends():
    gate = getattr(file_routes, "_require_derived_ready", None)
    if gate is None:
        pytest.skip("this tree predates the derived readiness gate the contract describes")
    source = inspect.getsource(gate)
    assert 'wire.invalid_request("This file has no derived data: " + str(ready.sentence), param="file_id")' in source
    assert "raise wire.file_not_ready(5)" in source
    # Both derived routes go through it: the list and the download.
    assert inspect.getsource(file_routes).count("_require_derived_ready(row)") == 2
    assert wire.FILE_CODES["file_not_ready"] == (409, "invalid_request_error", True)
    assert wire.invalid_request("x", param="file_id").status == 400


# --------------------------------------- annotations on a response read back --


def test_a_response_read_back_is_documented_without_annotations_for_as_long_as_its_stored_body_has_none():
    # The stored Response body is built from the row's text alone: an
    # `output_text` part has no `annotations` field, and a background row
    # records only the citation counts. The contract and the file-inputs page
    # must say so, and must stop saying so the day the stored body carries
    # them.
    stores_annotations = "annotations" in models.OutputText.model_fields or "annotation" in inspect.getsource(
        public_router._row_to_wire
    )
    contract = _section("**Files as model input**", "## 9. Response and error envelope")
    page = FILE_INPUTS_PAGE[FILE_INPUTS_PAGE.index("Where the annotations are:"):]
    page = page[: page.index("## Errors")].replace("\\`", "`")  # the template literal escapes its backticks
    contract_says_none = "carries no annotations" in contract
    page_says_none = "has no annotations" in page
    assert contract_says_none is page_says_none is (not stores_annotations)
    if not stores_annotations:
        assert "`GET /v1/responses/{response_id}`" in contract and "background response" in contract
        assert "`GET /v1/responses/{id}`" in page and "background" in page
