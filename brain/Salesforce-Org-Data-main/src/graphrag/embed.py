"""Embed the fact cards so a question can find a component by meaning.

The lexicon resolves vocabulary someone wrote down. It cannot help with
phrasing nobody anticipated: "who can conduct mock interviews" should reach
Active_For_Mock_Interview__c even though no word matches. That is what the
vectors are for, and it is the ONLY thing they are for -- a business fact like
"placed means Offer Received" is not recoverable by similarity, so embeddings
complement the lexicon rather than replacing it.

Only the 4,484 embeddable cards are sent. The rest are standard components the
org never customised, whose cards all read "not customized in this org"; they
would match every query weakly and push the useful result out of the top-K.

No vector database. 4,484 vectors of 1024 float32 is 18 MB, which a SQLite BLOB
column holds comfortably and a brute-force scan searches exactly. An index
would add a dependency, an approximation and a build step to buy nothing at
this size.
"""
from __future__ import annotations

import json
import sqlite3
import struct
import time
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Iterator, Sequence

DEFAULT_ENDPOINT = "http://127.0.0.1:8003/v1"
DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_BATCH = 32

SCHEMA = """
CREATE TABLE vector (
    component_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    dims         INTEGER NOT NULL,
    data         BLOB NOT NULL
);
CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class EmbedError(RuntimeError):
    """Raised when the embedding endpoint is unreachable or answers wrongly."""


@dataclass
class EmbedStats:
    embedded: int = 0
    reused: int = 0
    skipped_not_embeddable: int = 0
    batches: int = 0
    dims: int = 0
    seconds: float = 0.0
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["seconds"] = round(self.seconds, 1)
        return out


def _pack(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _normalise(values: Sequence[float]) -> list[float]:
    """Unit-length, so cosine similarity is a plain dot product later."""
    total = sum(v * v for v in values) ** 0.5
    if total == 0:
        return list(values)
    return [v / total for v in values]


def _embed_batch(client: Any, endpoint: str, model: str,
                 texts: list[str], timeout: float) -> list[list[float]]:
    response = client.post(
        f"{endpoint.rstrip('/')}/embeddings",
        json={"model": model, "input": texts},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise EmbedError(
            f"{endpoint}: HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
        ordered = sorted(payload["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in ordered]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise EmbedError(f"{endpoint}: unexpected response shape: {exc}") from exc


def build_vectors(cards_path: str, output: str, *,
                  endpoint: str = DEFAULT_ENDPOINT,
                  model: str = DEFAULT_MODEL,
                  batch_size: int = DEFAULT_BATCH,
                  timeout: float = 120.0,
                  limit: int | None = None) -> EmbedStats:
    """Embed every embeddable card, reusing vectors whose card has not changed."""
    try:
        import httpx
    except ImportError as exc:
        raise EmbedError(
            "embedding needs httpx: python -m pip install httpx") from exc

    cards_file = Path(cards_path)
    if not cards_file.is_file():
        raise EmbedError(f"cards not found: {cards_path}; run `graphrag cards` first")

    cards = sqlite3.connect(f"file:{cards_file}?mode=ro", uri=True)
    cards.row_factory = sqlite3.Row
    rows = cards.execute(
        "SELECT component_id, compact, keywords, content_hash FROM card"
        " WHERE embeddable = 1 AND redacted = 0 ORDER BY component_id").fetchall()
    total_cards = cards.execute("SELECT count(*) FROM card").fetchone()[0]
    cards.close()
    # Counted before --limit is applied, so a partial run still reports how
    # many cards the embeddability rule excluded rather than how many this
    # invocation happened to look at.
    stats = EmbedStats(model=model,
                       skipped_not_embeddable=total_cards - len(rows))
    if limit:
        rows = rows[:limit]

    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.executescript(
        SCHEMA if not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='vector'").fetchone() else "")
    existing = {r[0]: r[1] for r in connection.execute(
        "SELECT component_id, content_hash FROM vector")}

    pending = [r for r in rows if existing.get(r["component_id"]) != r["content_hash"]]
    stats.reused = len(rows) - len(pending)

    started = time.perf_counter()
    try:
        with httpx.Client() as client:
            for index in range(0, len(pending), batch_size):
                chunk = pending[index:index + batch_size]
                # Keywords carry the org's own vocabulary (picklist values,
                # label words). Including them lets a query match on a term the
                # prose of the card never repeats.
                texts = []
                for row in chunk:
                    words = ", ".join(json.loads(row["keywords"])[:12])
                    texts.append(f"{row['compact']}\nTerms: {words}" if words
                                 else row["compact"])
                vectors = _embed_batch(client, endpoint, model, texts, timeout)
                if len(vectors) != len(chunk):
                    raise EmbedError(
                        f"asked for {len(chunk)} embeddings, got {len(vectors)}")
                stats.dims = len(vectors[0])
                connection.executemany(
                    "INSERT OR REPLACE INTO vector"
                    " (component_id, content_hash, dims, data) VALUES (?,?,?,?)",
                    [(row["component_id"], row["content_hash"], len(vector),
                      _pack(_normalise(vector)))
                     for row, vector in zip(chunk, vectors)])
                connection.commit()
                stats.batches += 1
                stats.embedded += len(chunk)
    finally:
        stats.seconds = time.perf_counter() - started
        if stats.dims == 0 and existing:
            stats.dims = connection.execute(
                "SELECT dims FROM vector LIMIT 1").fetchone()[0]
        connection.executemany(
            "INSERT OR REPLACE INTO manifest (key, value) VALUES (?,?)",
            [("model", model), ("endpoint", endpoint),
             ("dims", str(stats.dims)), ("vectors_version", "1"),
             ("cards_source", str(cards_file.resolve())),
             ("stats", json.dumps(stats.as_dict(), sort_keys=True))])
        connection.commit()
        connection.close()
    return stats


def embed_query(text: str, *, endpoint: str = DEFAULT_ENDPOINT,
                model: str = DEFAULT_MODEL, timeout: float = 30.0) -> list[float]:
    """One unit-length vector for a user's question."""
    try:
        import httpx
    except ImportError as exc:
        raise EmbedError("embedding needs httpx") from exc
    with httpx.Client() as client:
        vectors = _embed_batch(client, endpoint, model, [text], timeout)
    return _normalise(vectors[0])
