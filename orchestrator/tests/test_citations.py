"""Citation URL building (spec §8)."""
from app.core.citations import (
    DEFAULT_LIGHTNING_BASE_URL,
    build_citation,
    build_citations,
    record_url,
)


def test_default_base_url_is_techsara_lightning():
    assert DEFAULT_LIGHTNING_BASE_URL == "https://techsara.lightning.force.com"


def test_record_url():
    assert (
        record_url("001xx000003DGbY")
        == "https://techsara.lightning.force.com/001xx000003DGbY"
    )


def test_record_url_trailing_slash_base():
    assert (
        record_url("003ABC", "https://techsara.lightning.force.com/")
        == "https://techsara.lightning.force.com/003ABC"
    )


def test_build_citation_shape():
    citation = build_citation("001xx000003DGbY", "Account")
    assert citation == {
        "record_id": "001xx000003DGbY",
        "object": "Account",
        "url": "https://techsara.lightning.force.com/001xx000003DGbY",
    }


def test_build_citations_from_hits_preserves_order_and_dedupes():
    hits = [
        {"record_id": "001A", "object": "Account", "text": "..."},
        {"record_id": "006B", "object": "Opportunity"},
        {"record_id": "001A", "object": "Account"},  # duplicate
        {"object": "Contact"},  # no record_id → skipped
        {"record_id": "", "object": "Case"},  # empty → skipped
    ]
    citations = build_citations(hits)
    assert [c["record_id"] for c in citations] == ["001A", "006B"]
    assert citations[1]["url"] == "https://techsara.lightning.force.com/006B"


def test_build_citations_custom_base():
    citations = build_citations(
        [{"record_id": "500X", "object": "Case"}], base_url="https://example.my.salesforce.com"
    )
    assert citations[0]["url"] == "https://example.my.salesforce.com/500X"
