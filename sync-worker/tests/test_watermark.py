from syncworker.storage import Store


def test_watermark_missing_is_none(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    assert store.get_watermark("Account") is None
    store.close()


def test_watermark_roundtrip_and_update(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.set_watermark("Account", "2026-07-22T00:00:00Z")
    assert store.get_watermark("Account") == "2026-07-22T00:00:00Z"

    # update overwrites, does not duplicate
    store.set_watermark("Account", "2026-07-22T06:30:00Z")
    assert store.get_watermark("Account") == "2026-07-22T06:30:00Z"

    # other objects are independent
    assert store.get_watermark("Contact") is None
    store.set_watermark("Contact", "2026-07-21T12:00:00Z")
    assert store.get_watermark("Account") == "2026-07-22T06:30:00Z"
    assert store.get_watermark("Contact") == "2026-07-21T12:00:00Z"
    store.close()


def test_watermark_persists_across_reopen(tmp_path):
    db = str(tmp_path / "wh.duckdb")
    store = Store(db)
    store.set_watermark("Opportunity", "2026-07-20T00:00:00Z")
    store.close()

    reopened = Store(db)
    assert reopened.get_watermark("Opportunity") == "2026-07-20T00:00:00Z"
    reopened.close()
