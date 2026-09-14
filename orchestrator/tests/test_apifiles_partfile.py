"""`core/partfile.py`: a part is whole or absent; an assembly is whole or absent.

These are the V29 guarantees lifted into one module (Files API design §3.3).
No database: pure bytes on a tmp_path.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
from starlette.requests import ClientDisconnect

from app.core import partfile
from tests.apifiles_test_support import isolated_app_db  # noqa: F401 - no TRUNCATE, see the support module


def _stream(chunks, *, disconnect_after=None):
    async def gen():
        for index, chunk in enumerate(chunks):
            if disconnect_after is not None and index >= disconnect_after:
                raise ClientDisconnect()
            yield chunk
    return gen()


def _leftovers(directory):
    return sorted(os.listdir(directory))


def test_a_complete_part_is_hashed_fsynced_and_left_under_its_temporary_name(tmp_path):
    final = str(tmp_path / "parts" / "3")
    data = [b"a" * 1000, b"b" * 24]
    written = asyncio.run(partfile.write_part_stream(_stream(data), final_path=final, cap_bytes=4096, want_md5=True))
    assert written.bytes == 1024
    assert written.sha256 == hashlib.sha256(b"".join(data)).hexdigest()
    assert written.md5 == hashlib.md5(b"".join(data)).hexdigest()
    assert not os.path.exists(final), "write_part_stream must not rename; the caller re-reads state first"
    assert os.path.exists(written.path) and written.path.endswith(".tmp")
    partfile.commit_part(written, final)
    assert open(final, "rb").read() == b"".join(data)
    assert _leftovers(tmp_path / "parts") == ["3"]


def test_a_part_over_its_cap_leaves_no_file_behind(tmp_path):
    final = str(tmp_path / "p" / "0")
    with pytest.raises(partfile.PartTooLarge):
        asyncio.run(partfile.write_part_stream(_stream([b"x" * 600, b"x" * 600]), final_path=final, cap_bytes=1000))
    assert _leftovers(tmp_path / "p") == []


def test_a_client_that_disconnects_mid_part_leaves_no_file_behind(tmp_path):
    final = str(tmp_path / "p" / "0")
    with pytest.raises(partfile.PartIncomplete) as caught:
        asyncio.run(partfile.write_part_stream(_stream([b"x" * 500, b"y" * 500, b"z"], disconnect_after=2), final_path=final, cap_bytes=10_000))
    assert caught.value.bytes_written == 1000
    assert _leftovers(tmp_path / "p") == []


def test_a_body_shorter_than_its_declared_length_is_incomplete_not_a_smaller_part(tmp_path):
    final = str(tmp_path / "p" / "0")
    with pytest.raises(partfile.PartIncomplete):
        asyncio.run(partfile.write_part_stream(_stream([b"x" * 10]), final_path=final, cap_bytes=100, expected_bytes=11))
    assert _leftovers(tmp_path / "p") == []


def test_a_digest_mismatch_leaves_no_file_behind(tmp_path):
    final = str(tmp_path / "p" / "0")
    with pytest.raises(partfile.PartDigestMismatch):
        asyncio.run(partfile.write_part_stream(_stream([b"hello"]), final_path=final, cap_bytes=100, declared_sha256="0" * 64))
    assert _leftovers(tmp_path / "p") == []


def test_a_refusing_live_budget_leaves_no_file_behind(tmp_path):
    final = str(tmp_path / "p" / "0")

    class Refused(Exception):
        pass

    def budget(n):
        if n > 100:
            raise Refused()

    with pytest.raises(Refused):
        asyncio.run(partfile.write_part_stream(_stream([b"x" * 80, b"x" * 80]), final_path=final, cap_bytes=10_000, live_budget=budget))
    assert _leftovers(tmp_path / "p") == []


def _parts(tmp_path, blobs):
    paths = []
    for index, blob in enumerate(blobs):
        path = tmp_path / f"part{index}"
        path.write_bytes(blob)
        paths.append(str(path))
    return paths


def test_assembly_follows_the_given_order_and_hashes_in_the_same_pass(tmp_path):
    blobs = [os.urandom(3000), os.urandom(10), os.urandom(70_000)]
    paths = _parts(tmp_path, blobs)
    order = [2, 0, 1]
    dest = str(tmp_path / "out" / "assembled.tmp")
    result = partfile.assemble([paths[i] for i in order], dest, want_md5=True,
                               expected_sizes=[len(blobs[i]) for i in order], progress_every_bytes=1000)
    whole = b"".join(blobs[i] for i in order)
    assert open(dest, "rb").read() == whole
    assert (result.bytes, result.sha256, result.md5) == (len(whole), hashlib.sha256(whole).hexdigest(), hashlib.md5(whole).hexdigest())
    assert not os.path.exists(dest + ".assembling")


def test_assembly_never_exposes_a_short_file_at_the_destination(tmp_path):
    blobs = [b"a" * 5000, b"b" * 5000]
    paths = _parts(tmp_path, blobs)
    dest = str(tmp_path / "assembled")
    seen = []

    def on_progress(done):
        seen.append((done, os.path.exists(dest), os.path.exists(dest + ".assembling")))

    partfile.assemble(paths, dest, want_md5=False, on_progress=on_progress, progress_every_bytes=1024)
    mid_copy = [entry for entry in seen if entry[0] < 10_000]
    assert mid_copy, "progress must have ticked during the copy"
    assert all(not exists and assembling for _, exists, assembling in mid_copy)


def test_a_part_whose_size_on_disk_differs_from_its_record_fails_and_leaves_nothing(tmp_path):
    paths = _parts(tmp_path, [b"a" * 10, b"b" * 9])
    dest = str(tmp_path / "assembled")
    with pytest.raises(partfile.AssemblyError):
        partfile.assemble(paths, dest, want_md5=False, expected_sizes=[10, 10])
    assert not os.path.exists(dest) and not os.path.exists(dest + ".assembling")


def test_a_missing_part_fails_and_leaves_nothing(tmp_path):
    paths = _parts(tmp_path, [b"a" * 10])
    dest = str(tmp_path / "assembled")
    with pytest.raises(partfile.AssemblyError):
        partfile.assemble(paths + [str(tmp_path / "gone")], dest, want_md5=False)
    assert not os.path.exists(dest) and not os.path.exists(dest + ".assembling")


def test_an_assembly_asked_to_stop_stops_and_leaves_nothing(tmp_path):
    paths = _parts(tmp_path, [b"a" * 4096, b"b" * 4096])
    dest = str(tmp_path / "assembled")
    with pytest.raises(partfile.AssemblyStopped):
        partfile.assemble(paths, dest, want_md5=False, should_stop=lambda: True, progress_every_bytes=1024)
    assert not os.path.exists(dest) and not os.path.exists(dest + ".assembling")


# --------------------------------------------------- review fixes 2026-09-13 --


def test_an_assembly_checks_every_copy_buffer_for_a_stop_request_not_every_256_mib(tmp_path):
    """Review MEDIUM (2026-09-13): `should_stop` ran only on the 256 MiB tick,
    so a shutdown waited for up to 256 MiB of copying."""
    size = 3 * partfile.COPY_BUFFER_BYTES
    paths = _parts(tmp_path, [os.urandom(size)])
    dest = str(tmp_path / "assembled")
    calls = {"n": 0}

    def stop_on_second_buffer():
        calls["n"] += 1
        return calls["n"] >= 2

    with pytest.raises(partfile.AssemblyStopped):
        partfile.assemble(paths, dest, want_md5=False, should_stop=stop_on_second_buffer)
    assert calls["n"] == 2, "consulted after each 1 MiB buffer, with the default 256 MiB progress tick"
    assert not os.path.exists(dest) and not os.path.exists(dest + ".assembling")


def test_a_part_whose_bytes_differ_from_its_recorded_sha256_fails_the_assembly(tmp_path):
    """Review LOW (2026-09-13): a failure after a retried part's rename but
    before its row committed left new bytes under the old row's digest, and the
    assembly stitched them in silently."""
    first, second = b"a" * 4096, b"b" * 4096
    paths = _parts(tmp_path, [first, second])
    dest = str(tmp_path / "assembled")
    recorded = [hashlib.sha256(first).hexdigest(), hashlib.sha256(b"c" * 4096).hexdigest()]
    with pytest.raises(partfile.PartChecksumMismatch) as mismatch:
        partfile.assemble(paths, dest, want_md5=False, expected_sha256s=recorded)
    assert mismatch.value.index == 1
    assert not os.path.exists(dest) and not os.path.exists(dest + ".assembling")
    good = [hashlib.sha256(first).hexdigest(), None]
    assert partfile.assemble(paths, dest, want_md5=False, expected_sha256s=good).bytes == 8192
