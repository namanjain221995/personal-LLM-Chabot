"""RAG evaluation harness — corpus-derived, repeatable, no hand-written entities.

Runs inside the orchestrator container (``/app``):

    python -m tools.rag_eval generate --n 60 --out /data/eval/dataset.json
    python -m tools.rag_eval run --dataset /data/eval/dataset.json \
        --variants legacy,dense,lexical_and,lexical_or,unified \
        --out /data/eval/report-baseline.json

``generate`` samples stored web pages (stratified by domain), cuts one
paragraph-aligned window from each, and asks the router model to write ONE
question that window answers plus the short answer copied from it. Items
whose answer is not literally in the window are dropped, so every gold item
is verifiable. Nothing about the corpus is assumed: whatever the platform has
read is what it is tested on.

``run`` scores retrieval variants on the same dataset:

- Recall@1 / Recall@5   — the gold page (or any page with identical text)
  appears in the top k
- MRR                   — 1 / rank of the first gold hit
- NDCG@5                — single-relevant-item DCG
- answer@5              — the gold ANSWER string is present in the text the
  prompt would actually see (the evidence passage, not the whole page); this
  is the number that predicts a faithful answer
- latency p50 / p95     — wall clock per query, warm

Variants are plain async callables ``question -> [{"page_id", "url",
"text"}, ...]`` ranked best first, so a new pipeline is one more entry in
``VARIANTS``. Reports are JSON plus a Markdown table on stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

sys.path.insert(0, "/app")

from app import db, web_index, web_memory  # noqa: E402
from app.freshness import Freshness  # noqa: E402

Variant = Callable[[str], Awaitable[List[Dict[str, Any]]]]

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

_GEN_SYSTEM = (
    "You write evaluation questions for a search system. Given a passage from "
    "a web page, write ONE question a person might type into an assistant that "
    "this passage answers specifically, and the short answer (at most 12 words) "
    "copied EXACTLY from the passage. The question must be self-contained (name "
    "the subject), must not say 'the passage' or 'this page', and must not be "
    "answerable from general knowledge alone. Return only JSON: "
    '{"question": "...", "answer": "..."}'
)


def _window(text: str, size: int, rng: random.Random) -> tuple[int, str]:
    """A paragraph-aligned window of ~size chars, avoiding the boilerplate head."""
    clean = text.strip()
    if len(clean) <= size:
        return 0, clean
    paras = [m.start() for m in re.finditer(r"\n\s*\n", clean)] or [0]
    starts = [p for p in paras if p + size <= len(clean)] or [0]
    start = rng.choice(starts)
    return start, clean[start : start + size]


async def generate(n: int, out: str, *, seed: int = 7, window: int = 1400, per_domain: int = 2) -> None:
    from app import llm

    rng = random.Random(seed)
    with db.connection() as con:
        rows = con.execute(
            """SELECT id, url, domain, title, text, content_hash
                 FROM web_pages
                WHERE fetch_status = 200 AND length(text) >= 800
                ORDER BY random() LIMIT %s""",
            (n * 8,),
        ).fetchall()
    per: Dict[str, int] = {}
    seen_hash: set = set()
    items: List[dict] = []
    for row in rows:
        if len(items) >= n:
            break
        dom = row["domain"] or ""
        if per.get(dom, 0) >= per_domain or row["content_hash"] in seen_hash:
            continue
        start, passage = _window(row["text"], window, rng)
        prompt = [
            {"role": "system", "content": _GEN_SYSTEM},
            {"role": "user", "content": f"Page title: {row['title']}\nPassage:\n{passage}"},
        ]
        try:
            raw = await llm.router_chat_completion(prompt, max_tokens=200)
            m = re.search(r"\{.*\}", raw or "", re.S)
            data = json.loads(m.group(0)) if m else {}
        except Exception as exc:  # noqa: BLE001
            print(f"  skip page {row['id']}: generation failed ({exc})", file=sys.stderr)
            continue
        q = _norm(str(data.get("question", "")))
        a = str(data.get("answer", "")).strip()
        words = len(q.split())
        if not q or not a or words < 5 or words > 40 or _norm(a) not in _norm(passage):
            continue
        if "passage" in q or "this page" in q:
            continue
        per[dom] = per.get(dom, 0) + 1
        seen_hash.add(row["content_hash"])
        items.append(
            {
                "id": hashlib.sha1(f"{row['id']}:{q}".encode()).hexdigest()[:10],
                "page_id": row["id"],
                "url": row["url"],
                "domain": dom,
                "content_hash": row["content_hash"],
                "window_start": start,
                "question": q,
                "answer": a,
            }
        )
        print(f"  [{len(items):3}] {dom:28} {q[:80]}")
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generator": "router_chat_completion",
        "seed": seed,
        "window_chars": window,
        "items": items,
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    print(f"wrote {len(items)} items → {out}")


# ---------------------------------------------------------------------------
# variants
# ---------------------------------------------------------------------------


def _verdict_level(question: str) -> Freshness:
    try:
        from app.freshness import classify_offline

        v = classify_offline(question, now_year=datetime.now(timezone.utc).year)
        return v.requirement
    except Exception:  # noqa: BLE001
        return Freshness.RECENT


async def v_legacy(question: str) -> List[Dict[str, Any]]:
    """`web_memory.retrieve` exactly as the Fast path used it (top_k=5)."""
    r = await web_memory.retrieve(question, level=_verdict_level(question), top_k=5)
    return [{"page_id": e.page_id, "url": e.url, "text": e.text} for e in r.evidence]


async def v_dense(question: str) -> List[Dict[str, Any]]:
    hits = await web_index.retrieve(question, top_k=10)
    return [{"page_id": None, "url": h.get("url"), "text": h.get("text", "")} for h in hits]


def _lexical_and(question: str, limit: int) -> List[dict]:
    with db.connection() as con:
        return con.execute(
            """SELECT id, url, text
                 FROM web_pages
                WHERE search_tsv @@ websearch_to_tsquery('english', %s) AND text <> ''
                ORDER BY ts_rank_cd(search_tsv, websearch_to_tsquery('english', %s), 1) DESC
                LIMIT %s""",
            (question, question, limit),
        ).fetchall()


async def v_lexical_and(question: str) -> List[Dict[str, Any]]:
    rows = await db.run_in_thread(_lexical_and, question, 10)
    return [{"page_id": r["id"], "url": r["url"], "text": (r["text"] or "")[:3200]} for r in rows]


async def v_lexical_or(question: str) -> List[Dict[str, Any]]:
    rows = await db.run_in_thread(web_memory._lexical_candidates, question, 10)
    return [{"page_id": r["id"], "url": r["url"], "text": (r["text"] or "")[:3200]} for r in rows]


async def v_unified(question: str) -> List[Dict[str, Any]]:
    """The unified knowledge pipeline (ADR-0001), when present."""
    from app.knowledge import retrieve as k_retrieve  # type: ignore

    r = await k_retrieve(question, level=_verdict_level(question), top_k=5)
    return [{"page_id": e.page_id, "url": e.url, "text": e.text} for e in r.evidence]


VARIANTS: Dict[str, Variant] = {
    "legacy": v_legacy,
    "dense": v_dense,
    "lexical_and": v_lexical_and,
    "lexical_or": v_lexical_or,
    "unified": v_unified,
}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _hashes_for(urls: Sequence[str], ids: Sequence[int]) -> Dict[str, str]:
    """url → content_hash and str(id) → content_hash for the retrieved rows."""
    out: Dict[str, str] = {}
    urls = [u for u in urls if u]
    ids = [int(i) for i in ids if i]
    if not urls and not ids:
        return out
    with db.connection() as con:
        rows = con.execute(
            "SELECT id, url, content_hash FROM web_pages WHERE url = ANY(%s) OR id = ANY(%s)",
            (urls, ids),
        ).fetchall()
    for r in rows:
        out[r["url"]] = r["content_hash"]
        out[f"id:{r['id']}"] = r["content_hash"]
    return out


def _rank_of_gold(item: dict, ranked: List[dict], hashes: Dict[str, str]) -> Optional[int]:
    for i, r in enumerate(ranked, 1):
        h = hashes.get(r.get("url") or "") or hashes.get(f"id:{r.get('page_id')}")
        if r.get("url") == item["url"] or r.get("page_id") == item["page_id"] or (h and h == item["content_hash"]):
            return i
    return None


async def run(dataset: str, variants: Sequence[str], out: str, *, k: int = 5, label: str = "") -> None:
    with open(dataset, encoding="utf-8") as fh:
        items = json.load(fh)["items"]
    report: Dict[str, Any] = {
        "dataset": dataset,
        "label": label,
        "n": len(items),
        "run_at": datetime.now(timezone.utc).isoformat(),
        "variants": {},
    }
    for name in variants:
        fn = VARIANTS.get(name)
        if fn is None:
            print(f"unknown variant {name}", file=sys.stderr)
            continue
        ranks: List[Optional[int]] = []
        answer_hits = 0
        lat: List[float] = []
        failures = 0
        per_item: List[dict] = []
        try:
            await fn(items[0]["question"])  # warm (embedding connection, table open)
        except Exception as exc:  # noqa: BLE001
            print(f"variant {name} unavailable: {exc}", file=sys.stderr)
            continue
        for it in items:
            t0 = time.perf_counter()
            try:
                ranked = await fn(it["question"])
            except Exception as exc:  # noqa: BLE001
                failures += 1
                ranked = []
                print(f"  {name}: {it['id']} failed: {exc}", file=sys.stderr)
            lat.append(time.perf_counter() - t0)
            hashes = await db.run_in_thread(
                _hashes_for, [r.get("url") for r in ranked], [r.get("page_id") for r in ranked]
            )
            rank = _rank_of_gold(it, ranked, hashes)
            ranks.append(rank)
            top_text = " ".join(_norm(r.get("text", "")) for r in ranked[:k])
            a_hit = _norm(it["answer"]) in top_text
            answer_hits += int(a_hit)
            per_item.append({"id": it["id"], "rank": rank, "answer_in_top_k": a_hit, "ms": round(1000 * lat[-1])})
        n = len(items)
        lat_sorted = sorted(lat)
        summary = {
            "recall@1": sum(1 for r in ranks if r == 1) / n,
            f"recall@{k}": sum(1 for r in ranks if r and r <= k) / n,
            "mrr": sum((1.0 / r) for r in ranks if r) / n,
            f"ndcg@{k}": sum((1.0 / math.log2(r + 1)) for r in ranks if r and r <= k) / n,
            f"answer@{k}": answer_hits / n,
            "p50_ms": round(1000 * statistics.median(lat_sorted)) if lat_sorted else None,
            "p95_ms": round(1000 * lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * 0.95))]) if lat_sorted else None,
            "failures": failures,
        }
        report["variants"][name] = {"summary": summary, "items": per_item}
        print(f"{name}: " + ", ".join(f"{k_}={v:.3f}" if isinstance(v, float) else f"{k_}={v}" for k_, v in summary.items()))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    print()
    print(markdown(report))
    print(f"wrote {out}")


def markdown(report: Dict[str, Any]) -> str:
    cols = ["recall@1", "recall@5", "mrr", "ndcg@5", "answer@5", "p50_ms", "p95_ms", "failures"]
    lines = [f"| variant | {' | '.join(cols)} |", f"|---|{'---|' * len(cols)}"]
    for name, v in report["variants"].items():
        s = v["summary"]
        cells = [f"{s.get(c):.3f}" if isinstance(s.get(c), float) else str(s.get(c)) for c in cols]
        lines.append(f"| {name} | {' | '.join(cells)} |")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--n", type=int, default=60)
    g.add_argument("--out", required=True)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--window", type=int, default=1400)
    r = sub.add_parser("run")
    r.add_argument("--dataset", required=True)
    r.add_argument("--variants", default="legacy,dense,lexical_and,lexical_or,unified")
    r.add_argument("--out", required=True)
    r.add_argument("--label", default="")
    a = p.parse_args(argv)
    if a.cmd == "generate":
        asyncio.run(generate(a.n, a.out, seed=a.seed, window=a.window))
    else:
        asyncio.run(run(a.dataset, [v.strip() for v in a.variants.split(",") if v.strip()], a.out, label=a.label))


if __name__ == "__main__":
    main()
