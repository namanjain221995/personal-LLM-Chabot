"""Offline sql-engine tests: §10 meta contract and DuckDB external-access
lockdown. No vLLM servers/GPU — llm calls are monkeypatched, DuckDB is a
temp file."""
import asyncio

import duckdb
import pytest

from app.config import settings
from app.engines import sql as sql_engine


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    """A tiny read-only warehouse the engine can query."""
    db_path = str(tmp_path / "warehouse.duckdb")
    con = duckdb.connect(db_path)
    con.execute("CREATE TABLE opportunities (stage VARCHAR, amount DOUBLE)")
    con.execute(
        "INSERT INTO opportunities VALUES ('Prospecting', 100.0), ('Closed Won', 250.0)"
    )
    con.close()
    monkeypatch.setattr(settings, "duckdb_path", db_path)
    return db_path


def test_execute_blocks_filesystem_and_network(warehouse):
    """§1/§12 root-cause fix: enable_external_access=false on the connection.

    Even if a hostile SELECT slipped past sql_guard, DuckDB itself must
    refuse filesystem/network table functions.
    """
    for hostile in (
        "SELECT content FROM read_text('/etc/hostname')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM read_csv('https://attacker.example/x.csv')",
    ):
        with pytest.raises(duckdb.Error):
            sql_engine._execute(hostile, fetch_cap=10)
    # Normal warehouse queries still work.
    columns, rows = sql_engine._execute("SELECT * FROM opportunities ORDER BY amount", 10)
    assert columns == ["stage", "amount"]
    assert len(rows) == 2


def test_run_sql_engine_emits_single_contract_meta(warehouse, monkeypatch):
    """§10: exactly ONE meta, carrying route + data (row objects) +
    top-level truncated, emitted after the token stream."""

    async def fake_chat_completion(messages, **kwargs):
        return "SELECT stage, amount FROM opportunities ORDER BY amount"

    async def fake_stream(messages, **kwargs):
        for tok in ("Two ", "rows."):
            yield tok

    monkeypatch.setattr(sql_engine.llm, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(sql_engine.llm, "stream_chat_completion", fake_stream)

    events = []

    async def emit(event, data):
        events.append((event, data))

    answer = asyncio.run(sql_engine.run_sql_engine("total by stage", [], emit))
    assert answer == "Two rows."

    metas = [d for e, d in events if e == "meta"]
    assert len(metas) == 1, "meta must be emitted exactly once per turn (§10)"
    meta = metas[0]

    assert meta["route"] == "sql"          # §10 key is `route`, not `engine`
    assert isinstance(meta["data"], list)  # array of row objects
    assert meta["data"][0] == {"stage": "Prospecting", "amount": 100.0}
    assert meta["truncated"] is False      # top-level sibling of data
    assert "engine" not in meta and "export_file" not in meta

    # Single FINAL meta: all tokens precede it (§10: "before done").
    kinds = [e for e, _ in events]
    assert kinds.index("meta") > max(i for i, k in enumerate(kinds) if k == "token")


def test_export_rides_report_files_contract_key(warehouse, monkeypatch):
    """§10: exports surface as report_files [{filename, type, size}]."""

    async def fake_chat_completion(messages, **kwargs):
        return "SELECT stage, amount FROM opportunities"

    async def fake_stream(messages, **kwargs):
        yield "Done."

    monkeypatch.setattr(sql_engine.llm, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(sql_engine.llm, "stream_chat_completion", fake_stream)
    monkeypatch.setattr(settings, "reports_dir", str(settings.duckdb_path).rsplit("/", 1)[0])

    events = []

    async def emit(event, data):
        events.append((event, data))

    asyncio.run(sql_engine.run_sql_engine("export the pipeline to csv", [], emit))
    (meta,) = [d for e, d in events if e == "meta"]

    assert "export_file" not in meta
    files = meta["report_files"]
    assert len(files) == 1
    assert files[0]["filename"].endswith(".csv")
    assert files[0]["type"] == "csv"
    assert isinstance(files[0]["size"], int) and files[0]["size"] > 0
