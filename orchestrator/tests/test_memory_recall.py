"""Cross-chat memory recall (V9): keyword extraction + recall-block formatting."""
from app.memory_recall import format_recall_block, keywords, recall_block


def test_keywords_drops_stopwords_and_short_words():
    kw = keywords("What did we decide about the ExternalId dedupe plan?")
    assert "externalid" in kw and "dedupe" in kw and "plan" in kw
    # stopwords / short words gone
    assert "the" not in kw and "did" not in kw and "we" not in kw


def test_keywords_dedupes_and_caps():
    kw = keywords("data data data model model schema mapping keys design review", max_keywords=4)
    assert kw == ["data", "model", "schema", "mapping"]


def test_keywords_empty_for_only_stopwords():
    assert keywords("what did you do?") == []


def test_format_recall_block_none_when_empty():
    assert format_recall_block([]) is None


def test_format_recall_block_lists_titles_and_snippets():
    block = format_recall_block(
        [{"title": "Data Model Prep", "snippet": "dedupe ExternalId before go-live"}]
    )
    assert "Data Model Prep" in block
    assert "dedupe ExternalId" in block
    # instructs the model to use-if-relevant, stay quiet otherwise
    assert "ignore" in block.lower()


def test_recall_block_uses_injected_search():
    calls = {}

    def fake_search(user_id, kws, exclude, limit):
        calls["args"] = (user_id, kws, exclude, limit)
        return [{"id": "c2", "title": "Prior chat", "snippet": "we chose merge key A"}]

    block = recall_block(7, "which merge key did we choose?", "c1", search=fake_search)
    assert "Prior chat" in block and "merge key A" in block
    uid, kws, exclude, _ = calls["args"]
    assert uid == 7 and exclude == "c1"
    assert "merge" in kws and "choose" in kws


def test_recall_block_none_when_no_keywords():
    # a query of only stopwords → no search, no block
    assert recall_block(1, "what did you do?", None, search=lambda *a: [{"x": 1}]) is None


def test_recall_block_none_when_no_hits():
    assert recall_block(1, "unrelated topic xyz", "c1", search=lambda *a: []) is None
