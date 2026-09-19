"""Dataset extraction and profiling run off the event loop (dataset review,
2026-09-19): on the loop, a legitimate 100k-row .xlsx held every user's
stream for ~8.5 s."""
from __future__ import annotations

import asyncio
import os
import threading

from app import uploads


def test_profiling_runs_in_a_worker_thread(tmp_path, monkeypatch):
    raw = tmp_path / "sales.csv"
    raw.write_text("region,revenue\nNorth,10\nSouth,20\n")
    monkeypatch.setattr(uploads, "upload_root", lambda c, u: str(tmp_path / "root"))
    seen = {}

    def fake_profile(path):
        seen["thread"] = threading.get_ident()
        return []

    monkeypatch.setattr(uploads.profiler, "profile_directory", fake_profile)

    async def main():
        seen["loop_thread"] = threading.get_ident()
        try:
            await uploads._finalise_dataset("c1", "u1", "sales.csv", str(raw), os.path.getsize(raw))
        except Exception:
            pass  # what happens after profiling (DB rows) is not this test's concern

    asyncio.run(main())
    assert "thread" in seen, "profile_directory was never called"
    assert seen["thread"] != seen["loop_thread"]
