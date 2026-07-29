import pytest

from syncworker.chunking import chunk_text


def _words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def test_empty_text_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\t ") == []


def test_short_text_single_chunk():
    text = _words(50)
    chunks = chunk_text(text)
    assert chunks == [text]


def test_exact_chunk_size_single_chunk():
    text = _words(800)
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_one_over_boundary_makes_two_chunks_with_overlap():
    chunks = chunk_text(_words(801))
    assert len(chunks) == 2
    first, second = (c.split() for c in chunks)
    assert len(first) == 800
    # second chunk = 100 overlap tokens + 1 new token
    assert len(second) == 101
    assert first[-100:] == second[:100]
    assert second[-1] == "w800"


def test_long_text_boundaries_and_overlap():
    n = 1750
    chunks = chunk_text(_words(n))
    # step is 700, so chunks start at 0, 700, 1400 -> 3 chunks
    assert len(chunks) == 3
    tokenized = [c.split() for c in chunks]
    assert [len(t) for t in tokenized] == [800, 800, 350]
    for prev, nxt in zip(tokenized, tokenized[1:]):
        assert prev[-100:] == nxt[:100]
    # nothing lost: reassembling without overlaps gives the original sequence
    reassembled = tokenized[0] + [t for chunk in tokenized[1:] for t in chunk[100:]]
    assert reassembled == _words(n).split()


def test_custom_sizes():
    chunks = chunk_text(_words(25), chunk_tokens=10, overlap_tokens=3)
    tokenized = [c.split() for c in chunks]
    assert [t[0] for t in tokenized] == ["w0", "w7", "w14", "w21"]
    for prev, nxt in zip(tokenized, tokenized[1:]):
        assert prev[-3:] == nxt[:3]


def test_invalid_overlap_rejected():
    with pytest.raises(ValueError):
        chunk_text("a b c", chunk_tokens=10, overlap_tokens=10)
    with pytest.raises(ValueError):
        chunk_text("a b c", chunk_tokens=10, overlap_tokens=-1)
    with pytest.raises(ValueError):
        chunk_text("a b c", chunk_tokens=0)
