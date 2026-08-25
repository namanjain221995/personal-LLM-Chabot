"""Pooling the warehouse connection across a batch of operations.

Opening the warehouse is not cheap and the cost tracks the CATALOG, not the
data: DuckDB loads every table definition on connect. Measured at this org's
shape it is ~4.5 ms on an empty file and ~291 ms at 1,023 tables, against
~0.1 ms of actual work per operation. A connection per operation therefore
spends a 1,023-object cycle acquiring locks rather than syncing.

`Store.session()` pins one connection for the block. These tests pin the
behaviour that makes that safe: call sites are untouched, sessions nest,
and the connection is always released — including when the body raises.
"""
import duckdb
import pandas as pd
import pytest

from syncworker import storage
from syncworker.storage import Store


@pytest.fixture()
def counting_connect(monkeypatch):
    """Count real duckdb.connect calls made by Store."""
    calls = []
    real = duckdb.connect

    def spy(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("database"))
        return real(*args, **kwargs)

    monkeypatch.setattr(storage.duckdb, "connect", spy)
    return calls


def _work(store):
    """Four operations — the shape of one object in a real sync cycle."""
    store.get_watermark("Account")
    store.ensure_table("Account", ["Id", "Name"])
    store.upsert("Account", pd.DataFrame([{"Id": "001A", "Name": "Acme"}]))
    store.set_watermark("Account", "t1")


# ---------------------------------------------------------------------------
# the pooling itself
# ---------------------------------------------------------------------------


def test_a_session_opens_one_connection_for_many_operations(tmp_path, counting_connect):
    store = Store(str(tmp_path / "wh.duckdb"))
    counting_connect.clear()  # ignore the constructor's own connection

    with store.session():
        _work(store)

    assert len(counting_connect) == 1


def test_without_a_session_every_operation_opens_its_own(tmp_path, counting_connect):
    """The unchanged path. Callers that never open a session must behave
    exactly as they did before sessions existed."""
    store = Store(str(tmp_path / "wh.duckdb"))
    counting_connect.clear()

    _work(store)

    assert len(counting_connect) == 4


def test_batching_scales_with_batches_not_operations(tmp_path, counting_connect):
    """Three sessions of four operations is three connections, not twelve —
    the property sync_object relies on to fold its head and tail operations
    into one open each."""
    store = Store(str(tmp_path / "wh.duckdb"))
    counting_connect.clear()

    for _ in range(3):
        with store.session():
            _work(store)

    assert len(counting_connect) == 3


# ---------------------------------------------------------------------------
# nesting
# ---------------------------------------------------------------------------


def test_a_nested_session_reuses_the_outer_connection(tmp_path, counting_connect):
    store = Store(str(tmp_path / "wh.duckdb"))
    counting_connect.clear()

    with store.session():
        with store.session():
            _work(store)

    assert len(counting_connect) == 1


def test_a_nested_session_does_not_close_the_outer_one(tmp_path):
    """The inner block exiting must leave the outer session usable — otherwise
    a helper that opens a session would strand its caller."""
    store = Store(str(tmp_path / "wh.duckdb"))

    with store.session():
        with store.session():
            store.set_watermark("Account", "inner")
        # still inside the outer session: this must not raise
        store.set_watermark("Contact", "outer")

    assert store.get_watermark("Account") == "inner"
    assert store.get_watermark("Contact") == "outer"


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_a_session_releases_the_connection_when_the_body_raises(tmp_path):
    """One failed object must not leak the write lock into the next batch."""
    store = Store(str(tmp_path / "wh.duckdb"))

    with pytest.raises(RuntimeError):
        with store.session():
            store.set_watermark("Account", "t1")
            raise RuntimeError("object sync blew up")

    assert store._pinned is None
    # a read-only connection proves the write lock is genuinely gone
    con = duckdb.connect(str(tmp_path / "wh.duckdb"), read_only=True)
    con.close()


def test_the_pin_is_cleared_after_a_normal_session(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    with store.session():
        assert store._pinned is not None
    assert store._pinned is None


def test_close_releases_an_abandoned_session(tmp_path):
    """Belt and braces for a caller that never unwound its session."""
    store = Store(str(tmp_path / "wh.duckdb"))
    store._pinned = store._connect()

    store.close()

    assert store._pinned is None
    con = duckdb.connect(str(tmp_path / "wh.duckdb"), read_only=True)
    con.close()


def test_close_is_idempotent_and_safe_without_a_session(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.close()
    store.close()  # must not raise


# ---------------------------------------------------------------------------
# durability
# ---------------------------------------------------------------------------


def test_writes_made_inside_a_session_survive_it(tmp_path):
    """Pooling must not change what actually lands on disk."""
    db = str(tmp_path / "wh.duckdb")
    store = Store(db)
    with store.session():
        store.upsert("Account", pd.DataFrame([{"Id": "001A", "Name": "Acme"}]))
        store.set_watermark("Account", "2026-08-26T00:00:00Z")
    store.close()

    reopened = Store(db)
    assert reopened.get_watermark("Account") == "2026-08-26T00:00:00Z"
    rows = reopened._con.execute('SELECT Id, Name FROM "Account"').fetchall()
    assert rows == [("001A", "Acme")]
    reopened.close()


def test_a_session_sees_its_own_earlier_writes(tmp_path):
    """Operations inside one session share a connection, so a later read has
    to observe an earlier write in the same block."""
    store = Store(str(tmp_path / "wh.duckdb"))
    with store.session():
        store.set_watermark("Account", "t1")
        assert store.get_watermark("Account") == "t1"
        store.upsert("Account", pd.DataFrame([{"Id": "001A", "Name": "Acme"}]))
        assert store.delete_ids("Account", ["001A"]) == 1
    store.close()


# ---------------------------------------------------------------------------
# transactions: an operation never leaks one into the next
# ---------------------------------------------------------------------------


def test_a_failed_operation_rolls_back_so_the_next_one_in_the_session_works(tmp_path):
    """Per-operation connections discarded an open transaction on close().
    A pooled connection has to do it explicitly, or DuckDB refuses the next
    BEGIN ("cannot start a transaction within a transaction") and the next
    ROLLBACK undoes everything since the leak — including work that had
    already succeeded."""
    store = Store(str(tmp_path / "wh.duckdb"))
    with store.session():
        store.set_watermark("Account", "committed")       # op A: autocommitted

        with pytest.raises(RuntimeError):                  # op B: leaks a BEGIN
            with store._connection() as con:
                con.execute("BEGIN TRANSACTION")
                con.execute(
                    "INSERT INTO _sync_meta VALUES ('Leaked', 'x', now())"
                )
                raise RuntimeError("operation blew up mid-transaction")

        # op C opens its own transaction — this is the call that failed
        # before the guard existed.
        store.upsert("Account", pd.DataFrame([{"Id": "001A", "Name": "Acme"}]))

        assert store.get_watermark("Account") == "committed"   # A survived
        assert store.get_watermark("Leaked") is None            # B rolled back
    store.close()


def test_rolling_back_when_nothing_is_open_is_silent(tmp_path):
    """The guard fires on every exception, including ones raised outside any
    transaction; DuckDB's 'no transaction is active' must not replace the
    real error."""
    store = Store(str(tmp_path / "wh.duckdb"))
    with store.session():
        with pytest.raises(ValueError, match="the real error"):
            with store._connection():
                raise ValueError("the real error")
        store.set_watermark("Account", "still works")
        assert store.get_watermark("Account") == "still works"
    store.close()


def test_the_connect_counter_matches_real_opens(tmp_path, counting_connect):
    """`warehouse_connects` in the cycle log is only useful if it is true."""
    store = Store(str(tmp_path / "wh.duckdb"))
    counting_connect.clear()
    base = store.connects

    _work(store)                      # 4 per-operation opens
    with store.session():
        _work(store)                  # 1 pooled open

    assert len(counting_connect) == 5
    assert store.connects - base == 5
