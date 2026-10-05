"""Per-thread connections must not outlive their threads.

Found live: a 500-question run died at question 145 with "Too many open files".
Each question ran on a fresh worker thread, and four components each kept that
thread's connection in a list forever -- +4 file handles per question against a
limit of 1,024. These tests churn threads and check the handles are released.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "brain/Salesforce-Org-Data-main/src"))

from graphrag.trace_store import TraceStore


def open_handles() -> int:
    return len(os.listdir("/proc/self/fd"))


def churn(use, threads: int = 40) -> None:
    """Run `use` once on each of many short-lived threads, one after another."""
    for _ in range(threads):
        worker = threading.Thread(target=use)
        worker.start()
        worker.join()


class ConnectionLifetimeTest(unittest.TestCase):
    def test_trace_store_releases_connections_of_finished_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TraceStore(str(Path(tmp) / "t.sqlite"))
            churn(lambda: store.db.execute("SELECT 1").fetchone())
            # The creating thread's connection, plus at most the last worker's.
            self.assertLessEqual(len(store._connections), 2)
            store.close()

    def test_open_handles_stay_flat_under_thread_churn(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TraceStore(str(Path(tmp) / "t.sqlite"))
            churn(lambda: store.db.execute("SELECT 1").fetchone(), threads=5)
            baseline = open_handles()
            churn(lambda: store.db.execute("SELECT 1").fetchone(), threads=60)
            # Before the fix this grew by ~60 (one handle per retired thread).
            self.assertLessEqual(open_handles() - baseline, 3)
            store.close()

    def test_a_live_threads_connection_is_never_closed_under_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TraceStore(str(Path(tmp) / "t.sqlite"))
            ready, finish, errors = threading.Event(), threading.Event(), []

            def long_lived():
                connection = store.db
                ready.set()
                finish.wait(5)
                try:
                    connection.execute("SELECT 1").fetchone()
                except Exception as exc:                    # noqa: BLE001
                    errors.append(exc)

            keeper = threading.Thread(target=long_lived)
            keeper.start()
            ready.wait(5)
            churn(lambda: store.db.execute("SELECT 1").fetchone(), threads=20)
            finish.set()
            keeper.join()
            self.assertEqual(errors, [])
            store.close()

    def test_duckdb_executor_releases_connections_of_finished_threads(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("duckdb is not installed")
        from record_query.config import DuckDBSettings
        from record_query.executor import DuckDBExecutor
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "w.duckdb")
            duckdb.connect(path).close()
            executor = DuckDBExecutor(DuckDBSettings(path=path, read_only=True))
            churn(lambda: executor.connection.execute("SELECT 1").fetchone())
            self.assertLessEqual(len(executor._connections), 1)
            executor.close()


if __name__ == "__main__":
    unittest.main()
