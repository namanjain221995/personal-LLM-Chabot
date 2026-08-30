#!/usr/bin/env python3
"""Prometheus exporter for this project's FILE-BASED data stores.

PostgreSQL has a real exporter (postgres_exporter) and needs nothing from here.
But most of this platform's data does not live in PostgreSQL: the Salesforce
warehouse is a DuckDB file, the two vector indexes are LanceDB directories, the
sync worker lands Parquet, and reports/workspaces/brain are directories on a
volume. None of those speak a wire protocol, so nothing scrapes them — yet they
are exactly what fills the disk and exactly what goes stale.

WHAT THIS ANSWERS THAT NOTHING ELSE DOES:

  * "Is the Salesforce warehouse actually being refreshed?" The sync worker
    publishes an atomic read snapshot (`warehouse.read.duckdb`); the AGE of
    that file is the honest freshness signal. A snapshot that stops advancing
    means every Salesforce answer is quietly serving stale data while every
    container still reports healthy.
  * "What is eating the disk?" LanceDB grows with every page the crawler
    stores; Parquet is a landing zone that is never pruned.
  * "Did the vector index actually grow after that crawl?"

DESIGN. Standard library only, same as the GPU exporter, so it runs on a stock
python:3.12-slim with no pip install. The data volume is mounted READ-ONLY: this
process can never modify the stores it measures.

Directory walks are cached (default 30 s) because `du` over a 1.6 GB LanceDB
directory is not free, and nothing here changes second to second.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LISTEN_HOST = os.environ.get("DATA_STORES_EXPORTER_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("DATA_STORES_EXPORTER_PORT", "9836"))
ROOT = Path(os.environ.get("DATA_STORES_ROOT", "/data"))
REPORTS = Path(os.environ.get("DATA_STORES_REPORTS", "/reports"))
#: A walk of every store costs real I/O; these values move on the scale of
#: minutes, so a 30 s cache is generous and keeps the scrape cheap.
CACHE_TTL_S = float(os.environ.get("DATA_STORES_CACHE_TTL", "30"))

log = logging.getLogger("data-stores-exporter")

#: (metric label, path, kind). `kind` only documents what the thing is; every
#: store is measured the same way.
STORES = [
    ("warehouse_live", ROOT / "warehouse.duckdb", "duckdb"),
    # The atomic snapshot the orchestrator actually reads. Its age is the
    # single most useful number in this exporter.
    ("warehouse_snapshot", ROOT / "warehouse.read.duckdb", "duckdb"),
    ("lancedb_salesforce", ROOT / "lancedb", "lancedb"),
    ("lancedb_web", ROOT / "lancedb-web", "lancedb"),
    ("parquet_landing", ROOT / "parquet", "parquet"),
    ("workspaces", ROOT / "workspaces", "workspace"),
    ("brain", ROOT / "brain", "knowledge"),
    ("reports", REPORTS, "reports"),
]


def measure(path: Path) -> dict | None:
    """Size, file count and newest mtime for a file or directory tree."""
    try:
        if not path.exists():
            return None
        if path.is_file():
            st = path.stat()
            return {"bytes": st.st_size, "files": 1, "mtime": st.st_mtime}
        total = 0
        files = 0
        newest = 0.0
        for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
            for name in filenames:
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue  # a file the sync worker replaced mid-walk
                total += st.st_size
                files += 1
                if st.st_mtime > newest:
                    newest = st.st_mtime
        return {"bytes": total, "files": files, "mtime": newest or path.stat().st_mtime}
    except Exception:  # noqa: BLE001 — a missing store is data, not an error
        log.debug("could not measure %s", path, exc_info=True)
        return None


class Collector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._body = ""
        self._at = 0.0

    def render(self) -> str:
        with self._lock:
            if self._body and time.monotonic() - self._at < CACHE_TTL_S:
                return self._body
            self._body = self._build()
            self._at = time.monotonic()
            return self._body

    def _build(self) -> str:
        started = time.monotonic()
        now = time.time()
        lines: list[str] = []

        def head(name: str, mtype: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")

        measured = [(label, kind, measure(path)) for label, path, kind in STORES]
        present = [(l, k, m) for l, k, m in measured if m]

        head("techsara_store_present", "gauge",
             "1 if this data store exists on disk, 0 if it is missing.")
        for label, kind, m in measured:
            lines.append(
                f'techsara_store_present{{store="{label}",kind="{kind}"}} {1 if m else 0}'
            )

        head("techsara_store_size_bytes", "gauge",
             "Total bytes on disk for this store.")
        for label, kind, m in present:
            lines.append(
                f'techsara_store_size_bytes{{store="{label}",kind="{kind}"}} {m["bytes"]}'
            )

        head("techsara_store_files", "gauge",
             "Number of files in this store.")
        for label, kind, m in present:
            lines.append(
                f'techsara_store_files{{store="{label}",kind="{kind}"}} {m["files"]}'
            )

        head("techsara_store_age_seconds", "gauge",
             ("Seconds since the newest file in this store was written. For "
              "warehouse_snapshot this is the Salesforce data's staleness: the "
              "sync worker republishes it every cycle, so an age that keeps "
              "climbing means answers are being served from an old copy while "
              "every container still looks healthy."))
        for label, kind, m in present:
            lines.append(
                f'techsara_store_age_seconds{{store="{label}",kind="{kind}"}} '
                f'{max(0.0, now - m["mtime"]):.1f}'
            )

        head("techsara_store_total_bytes", "gauge",
             "Sum of every measured store - what this platform's data costs on disk.")
        lines.append(f'techsara_store_total_bytes {sum(m["bytes"] for _l, _k, m in present)}')

        head("techsara_data_stores_scrape_duration_seconds", "gauge",
             "Wall time spent walking the stores for this sample.")
        lines.append(
            f"techsara_data_stores_scrape_duration_seconds {time.monotonic() - started:.6f}"
        )
        return "\n".join(lines) + "\n"


COLLECTOR = Collector()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = self.path.rstrip("/")
        if path in ("/metrics", ""):
            body = COLLECTOR.render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("DATA_STORES_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    found = [label for label, path, _kind in STORES if path.exists()]
    log.info("watching %d/%d stores: %s", len(found), len(STORES), ", ".join(found))
    missing = [label for label, path, _k in STORES if not path.exists()]
    if missing:
        # Not an error: a fresh install has no warehouse until the first sync.
        log.info("not present yet: %s", ", ".join(missing))
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    log.info("listening on %s:%d/metrics", LISTEN_HOST, LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
