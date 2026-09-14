"""The Files API tables (V36 files portion): idempotent, indexed, constrained."""
from __future__ import annotations

import psycopg
import pytest

from app import db
from app.apifiles import ids, schema
from tests.apifiles_test_support import isolated_app_db, make_caller  # noqa: F401 - fixture by name

SHA = "c" * 64


def test_the_files_schema_applies_twice_without_error():
    schema.ensure_schema()
    schema.ensure_schema()
    with db.connection() as con:
        present = {
            row["relname"]
            for row in con.execute(
                "SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relkind = 'r'", (list(schema.TABLES),)
            ).fetchall()
        }
    assert present == set(schema.TABLES)


def test_every_foreign_key_a_cascade_walks_is_indexed():
    """The V31 rule: an unindexed FK turns a project delete into a sequential
    scan per referencing row."""
    with db.connection() as con:
        fks = con.execute(
            "SELECT c.conrelid::regclass::text AS tbl, a.attname AS col "
            "  FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            " WHERE c.contype = 'f' AND c.conrelid::regclass::text = ANY(%s)",
            (list(schema.TABLES),),
        ).fetchall()
        indexed = {
            (row["tbl"], row["col"])
            for row in con.execute(
                "SELECT i.indrelid::regclass::text AS tbl, a.attname AS col FROM pg_index i "
                "  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
                " WHERE i.indrelid::regclass::text = ANY(%s)",
                (list(schema.TABLES),),
            ).fetchall()
        }
    assert fks, "the tables must have foreign keys"
    missing = sorted({(fk["tbl"], fk["col"]) for fk in fks} - indexed)
    assert missing == []


def _insert_blob(caller, **overrides):
    values = {"id": ids.new_blob_id(), "project_id": caller.project_id, "workspace_id": caller.workspace_id,
              "sha256": SHA, "bytes": 1, "kind": "pdf", "status": "queued"}
    values.update(overrides)
    columns = ", ".join(values)
    with db.connection() as con:
        con.execute(f"INSERT INTO api_file_blobs ({columns}) VALUES ({', '.join(['%s'] * len(values))})", list(values.values()))
    return values


@pytest.mark.parametrize(
    "overrides",
    [{"sha256": "C" * 64}, {"bytes": -1}, {"kind": "exe"}, {"status": "gone"}, {"lane": "gpu"}, {"error_code": "oops"}],
)
def test_blob_checks_refuse_bad_values(overrides):
    caller = make_caller()
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_blob(caller, **overrides)


def test_a_live_file_must_have_a_blob_an_assembly_or_an_error():
    caller = make_caller()
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as con:
            con.execute(
                "INSERT INTO api_files (id, project_id, workspace_id, filename, purpose, bytes) VALUES (%s, %s, %s, 'a', 'user_data', 1)",
                (ids.new_file_id(), caller.project_id, caller.workspace_id),
            )


@pytest.mark.parametrize("purpose,filename", [("batch", "a"), ("user_data", ""), ("user_data", "x" * 256)])
def test_file_checks_refuse_a_bad_purpose_or_filename(purpose, filename):
    caller = make_caller()
    blob = _insert_blob(caller)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as con:
            con.execute(
                "INSERT INTO api_files (id, project_id, workspace_id, blob_id, filename, purpose, bytes) VALUES (%s, %s, %s, %s, %s, %s, 1)",
                (ids.new_file_id(), caller.project_id, caller.workspace_id, blob["id"], filename, purpose),
            )


@pytest.mark.parametrize("number,size,sha", [(10_000, 1, SHA), (-1, 1, SHA), (0, 0, SHA), (0, 67_108_865, SHA), (0, 1, "x")])
def test_part_checks_refuse_out_of_range_numbers_sizes_and_digests(number, size, sha):
    caller = make_caller()
    upload = schema.create_api_upload(caller.project_id, caller.workspace_id, None, "a", "user_data", "x", 10, None,
                                      idle_ttl_s=60, max_ttl_s=120)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as con:
            con.execute(
                "INSERT INTO api_upload_parts (id, upload_id, part_number, bytes, sha256) VALUES (%s, %s, %s, %s, %s)",
                (ids.new_part_id(), upload["id"], number, size, sha),
            )


def test_an_upload_numbering_mode_is_one_of_two_words():
    caller = make_caller()
    upload = schema.create_api_upload(caller.project_id, caller.workspace_id, None, "a", "user_data", "x", 10, None,
                                      idle_ttl_s=60, max_ttl_s=120)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as con:
            con.execute("UPDATE api_uploads SET part_mode = 'mixed' WHERE id = %s", (upload["id"],))


def test_video_analyses_gain_a_lane_that_defaults_to_chat():
    with db.connection() as con:
        column = con.execute(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            " WHERE table_name = 'video_analyses' AND column_name = 'lane'"
        ).fetchone()
        check = con.execute(
            "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conname = 'video_analyses_lane'"
        ).fetchone()
    assert column["column_default"].startswith("'chat'") and column["is_nullable"] == "NO"
    assert "'chat'" in check["def"] and "'api'" in check["def"]


def test_deleting_a_project_cascades_to_its_blobs_files_uploads_and_parts():
    caller = make_caller()
    blob = _insert_blob(caller)
    file_row = schema.create_api_file(caller.project_id, caller.workspace_id, None, blob["id"], "a.pdf", "user_data", 1, "file", None)
    upload = schema.create_api_upload(caller.project_id, caller.workspace_id, None, "a", "user_data", "x", 10, None,
                                      idle_ttl_s=60, max_ttl_s=120)
    outcome = schema.upsert_api_upload_part(
        caller.project_id, upload["id"], part_number=0, bytes=5, sha256=SHA, mode="numbered",
        part_max_bytes=64, max_parts=10, upload_max_bytes=100, idle_ttl_s=60, max_ttl_s=120, before_commit=lambda n: None,
    )
    assert outcome.state == "ok" and file_row is not None
    with db.connection() as con:
        con.execute("DELETE FROM api_projects WHERE id = %s", (caller.project_id,))
        counts = con.execute(
            "SELECT (SELECT count(*) FROM api_file_blobs WHERE project_id = %s) AS blobs, "
            "       (SELECT count(*) FROM api_files WHERE project_id = %s) AS files, "
            "       (SELECT count(*) FROM api_uploads WHERE project_id = %s) AS uploads, "
            "       (SELECT count(*) FROM api_upload_parts WHERE upload_id = %s) AS parts",
            (caller.project_id, caller.project_id, caller.project_id, upload["id"]),
        ).fetchone()
    assert dict(counts) == {"blobs": 0, "files": 0, "uploads": 0, "parts": 0}


def test_a_blob_being_deleted_cannot_gain_a_new_file():
    caller = make_caller()
    blob = _insert_blob(caller, status="deleting")
    assert schema.create_api_file(caller.project_id, caller.workspace_id, None, blob["id"], "a", "user_data", 1, "file", None) is None
    with pytest.raises(schema.BlobBeingPurged):
        schema.create_api_file_with_blob(
            caller.project_id, caller.workspace_id, None, sha256=SHA, bytes=1, kind="pdf", mime_type="application/pdf",
            lane="cpu", filename="a", purpose="user_data", origin="file", expires_after_seconds=None,
            place_bytes=lambda row, created: None,
        )


def test_blob_updates_are_allow_listed():
    caller = make_caller()
    blob = _insert_blob(caller)
    updated = schema.update_api_file_blob(blob["id"], stage="text", facts={"pages": 3})
    assert updated["stage"] == "text" and updated["facts"] == {"pages": 3}
    with pytest.raises(ValueError):
        schema.update_api_file_blob(blob["id"], project_id="proj_" + "0" * 24)
