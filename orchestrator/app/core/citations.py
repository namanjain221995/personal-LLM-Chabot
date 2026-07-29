"""Citation building for the rag engine (spec §8).

meta.citations = [{record_id, object, url: https://techsara.lightning.force.com/<record_id>}]

Pure module: stdlib only.
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Optional

DEFAULT_LIGHTNING_BASE_URL = "https://techsara.lightning.force.com"


def record_url(record_id: str, base_url: str = DEFAULT_LIGHTNING_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/{record_id}"


def build_citation(
    record_id: str,
    object_name: Optional[str] = None,
    base_url: str = DEFAULT_LIGHTNING_BASE_URL,
) -> dict:
    return {
        "record_id": record_id,
        "object": object_name or "Record",
        "url": record_url(record_id, base_url),
    }


def build_citations(
    hits: Iterable[Mapping],
    base_url: str = DEFAULT_LIGHTNING_BASE_URL,
) -> List[dict]:
    """Build a de-duplicated (by record_id, order-preserving) citation list.

    Each hit is a mapping with at least `record_id` and optionally `object`.
    Hits without a record_id are skipped.
    """
    seen: set = set()
    out: List[dict] = []
    for hit in hits:
        rid = hit.get("record_id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(build_citation(str(rid), hit.get("object"), base_url))
    return out
