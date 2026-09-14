"""With the shadow knobs off, rerank inputs and sources are what dev gave.

The pre-pass latency round (2026-09-14) added shadow counters to
`web_memory._answerability`, a judged-list attribute to `retrieve`, a salvage
path to `living_knowledge.prepare`, and a source floor that only counts. None
of them may change what the cross-encoder is asked or what a turn cites while
the knobs are at their defaults. This is checked as a DIFF, not by reading
code: the golden file was produced by running this very module against
dev@ca6b6f3 with PREPASS_GOLDEN_WRITE=1, and every run here compares with it.

For every case of tests/fixtures/web_eval/cases.json, over the case's fixture
pages (real extractor, real PostgreSQL lexical half, real merge, rank,
answerability and partition; only the dense half and the cross-encoder are
stubbed, deterministically):
  - `retrieve` at Fast for each freshness level: the passages sent to
    rerank.score, the evidence and the superseded pages;
  - `prepare` at Fast with the router answering RECENT, and with the router
    failing after the speculative run finished (the salvage case): the
    decision, the sources, and the DISTINCT lists sent to rerank.score (dev
    sent the same list twice in the second shape; salvage sends it once).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, freshness, rerank, web_index, web_memory
from app import living_knowledge as lk
from app.config import settings
from app.core import extract
from app.freshness import Freshness, Verdict

FIXTURES = Path(__file__).parent / "fixtures"
WEB_EVAL = FIXTURES / "web_eval"
GOLDEN = FIXTURES / "prepass_rerank_golden.json"
CASES = [c for c in json.loads((WEB_EVAL / "cases.json").read_text())["cases"] if c.get("question")]


def _h(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _unit(*parts: str) -> float:
    return int(_h("\x1f".join(parts))[:8], 16) / 0xFFFFFFFF


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    web_memory.cache_clear()
    lk._page_vocabulary.reset()
    rerank.reset_for_tests()
    for name, value in dict(
        knowledge_evidence_cache_ttl_s=0.0, web_memory_enabled=True, living_knowledge_topical=True,
        freshness_router_enabled=True, freshness_fast_skip_router=False, knowledge_rerank=True,
        knowledge_rerank_candidates=12, knowledge_fast_concurrent_retrieve=True,
    ).items():
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    yield
    rerank.reset_for_tests()


#: Background pages, generated deterministically, so the candidate lists reach
#: today's full 16-passage head. Each draws its sentences from the questions'
#: own vocabulary, so the lexical half returns them too.
_FILLER_WORDS = (
    "gpt reasoning score benchmark leaderboard benchlm ranked model h100 b200 gpu hour cost price "
    "orbital compute instance cloud rental offer table release notes evaluation dataset accuracy "
    "provider region storage network latency throughput tokens context window pricing plan"
).split()


def _filler(j: int) -> str:
    words = [_FILLER_WORDS[int(_h(f"{j}:{k}")[:6], 16) % len(_FILLER_WORDS)] for k in range(400)]
    return " ".join(
        " ".join(words[k: k + 12]).capitalize() + "." for k in range(0, len(words), 12)
    )


def _corpus_order(case):
    """The case's own pages first (for stale-stored-page: the stored copy),
    then every other fixture page as background, so the candidate lists are
    as long as a real corpus makes them (up to today's 16-passage head)."""
    own = list(case.get("fixtures") or [])
    if case.get("stored"):
        own.append(case["stored"])
    rest = sorted(p.name for p in WEB_EVAL.glob("*.html") if p.name not in own)
    return own + rest


def _seed(case, monkeypatch, sent):
    pages = []
    names = _corpus_order(case)
    for i, name in enumerate(names):
        url = f"https://eval{i}.example.org/{name}"
        ext, _links = extract.extract_readable_and_links("text/html", (WEB_EVAL / name).read_bytes(), url)
        row = db.upsert_web_page(url.replace("https://", ""), url, url, ext.title or name, ext.text,
                                 "text/html", 200, _h(ext.text), authority=40 + 10 * (i % 3))
        # Fixed, distinct read dates, 50 days apart, so supersession and the
        # recency blend have something to decide (the stored copy of
        # stale-stored-page is its case's stored_age_days old).
        age = float(case.get("stored_age_days", 2)) if (i == 0 and case.get("stored")) else 2.0 + 50.0 * i
        when = datetime.now(timezone.utc) - timedelta(days=age)
        with db.connection() as con:
            con.execute("UPDATE web_pages SET fetched_at = %s, first_seen_at = %s WHERE id = %s",
                        (when, when, row["id"]))
        pages.append((url, ext.title or name, ext.text, int(row["id"])))
    for j in range(20):
        url = f"https://filler{j}.example.net/notes"
        text, title = _filler(j), f"Background notes {j}"
        row = db.upsert_web_page(url.replace("https://", ""), url, url, title, text,
                                 "text/html", 200, _h(text), authority=30)
        when = datetime.now(timezone.utc) - timedelta(days=5.0 + 17.0 * j)
        with db.connection() as con:
            con.execute("UPDATE web_pages SET fetched_at = %s, first_seen_at = %s WHERE id = %s",
                        (when, when, row["id"]))
        pages.append((url, title, text, int(row["id"])))

    async def dense(query, top_k=6, site_prefix=""):
        hits = []
        for url, title, text, pid in pages:
            for c in range(2):
                chunk = text[c * 1200: c * 1200 + 1400]
                if chunk:
                    hits.append({"url": url, "title": title, "text": chunk, "page_id": pid,
                                 "fetched_at": "", "score": 0.1 + 0.8 * _unit(query, url, str(c))})
        hits.sort(key=lambda h: h["score"])
        return hits[:top_k]

    async def score(query, docs, **kw):
        sent.append([_h(d) for d in docs])
        return [_unit("answer", query, d) for d in docs]

    monkeypatch.setattr(web_index, "retrieve", dense)
    monkeypatch.setattr(rerank, "score", score)


def _src(sources):
    return [(s["n"], s["url"], s["title"], _h(s["snippet"])) for s in sources]


def _distinct(sent):
    out = []
    for docs in sent:
        if docs not in out:
            out.append(docs)
    return out


def _observe(case, monkeypatch):
    sent = []
    _seed(case, monkeypatch, sent)
    q = case["question"]
    record = {"retrieve": {}, "prepare": {}}
    for level in (Freshness.RECENT, Freshness.REALTIME, Freshness.STATIC):
        sent.clear()
        verdict = Verdict(level, freshness._MAX_AGE[level], "router")
        r = asyncio.run(web_memory.retrieve(q, level=level, top_k=5, effort="fast", verdict=verdict, use_cache=False))
        record["retrieve"][level.value] = {
            "rerank": list(sent),
            "evidence": [(e.url, _h(e.text), round(e.answer, 6)) for e in r.evidence],
            "superseded": [e.url for e in r.superseded],
            "conflict": r.conflict,
        }
    for shape in ("router_recent", "router_down"):
        sent.clear()

        async def ask(question, _shape=shape):
            if _shape == "router_recent":
                await asyncio.sleep(0)
                return Verdict(Freshness.RECENT, freshness._MAX_AGE[Freshness.RECENT], "router")
            await asyncio.sleep(0.25)  # after the speculative run finished
            raise RuntimeError("router unavailable")

        monkeypatch.setattr(freshness, "_ask_router", ask)
        p = asyncio.run(lk.prepare(q, effort="fast", mode="assistant", web_search_pref="off", allow_network=False))
        record["prepare"][shape] = {
            "verdict": [p.verdict.requirement.value, p.verdict.reason],
            "decision": p.decision,
            "sources": _src(p.sources),
            "rerank_distinct": _distinct(sent),
        }
    return json.loads(json.dumps(record))


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_rerank_inputs_and_sources_match_dev(case, monkeypatch):
    observed = _observe(case, monkeypatch)
    if os.environ.get("PREPASS_GOLDEN_WRITE"):
        data = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        data[case["id"]] = observed
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True))
        return
    golden = json.loads(GOLDEN.read_text())[case["id"]]
    assert any(v["rerank"] for v in observed["retrieve"].values()), "premise: the cross-encoder ran"
    assert max(len(docs) for v in observed["retrieve"].values() for docs in v["rerank"]) >= 12, "premise: a full head"
    assert observed == golden
