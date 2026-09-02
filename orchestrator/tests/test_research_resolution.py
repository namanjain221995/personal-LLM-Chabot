"""Deep Research, rebuilt (2026-09-03): time, primaries, duplicates, stopping.

What the first engine could not do, pinned so it cannot regress:

* resolve a subquestion's claims IN CODE into CURRENT / SUPERSEDED /
  CONFLICTING / UNKNOWN by date and authority — not by asking the model to
  pick the newest-looking sentence;
* mark a syndicated copy as a duplicate at registration, so it never counts
  as independent corroboration;
* score the links inside a page so the citation an article gives (the
  official page, the PDF) is opened next;
* stop on EVIDENCE — information gain, duplicate rate, unanswered
  subquestions — and say why;
* run one more targeted round when the verification pass finds a thin
  claim, instead of writing a confident sentence about it;
* persist the resolved claims for the Fast-mode knowledge layer.

Everything is offline: no vLLM, no SearXNG, no network.
"""
import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db
from app.config import settings
from app.engines import deep_research as dr
from app.engines.search import _Source
from app.freshness import Freshness, Verdict
from app.search.base import SearchResult


def _now():
    return datetime.now(timezone.utc)


def _state(question="q", subqs=("who leads it", "when did that change"), sensitive=True):
    st = dr.ResearchState(research_id="abc123def456", conversation_id="c1", question=question)
    st.subquestions = list(subqs)
    st.today = _now().date().isoformat()
    st.now_year = _now().year
    st.temporal = (
        Verdict(Freshness.RECENT, 14 * 86400, "lexical:recent")
        if sensitive
        else Verdict(Freshness.STATIC, 365 * 86400, "lexical:static")
    )
    return st


def _src(st, url, text, *, authority=40, kind="", published=None, primary=None, links=()):
    s = _Source(
        n=0, title=url, url=url, text=text, links=list(links),
        published_at=published, fetched_at=_now(), authority=authority,
        source_type=kind or "unknown",
    )
    rec = dr._register(st, s, "q")
    if primary is not None:
        rec.primary = primary
    return rec


def _claim(st, subq, value, source_n, as_of=None, hint="current"):
    st.claims.append(dr.Claim(subq=subq, text=f"claim {value}", value=value,
                              source_n=source_n, as_of=as_of, hint=hint, iteration=1))


# ---------------------------------------------------------------------------
# Temporal resolution (code, not prompt)
# ---------------------------------------------------------------------------


def test_a_newer_authoritative_value_supersedes_an_older_one():
    st = _state()
    official = _src(st, "https://org.gov.example/leadership", "official page " * 50,
                    authority=100, kind="official", published=_now() - timedelta(days=10))
    blog = _src(st, "https://someone.blogspot.example/post", "blog post " * 50,
                authority=15, kind="blog", published=_now() - timedelta(days=500))
    _claim(st, 1, "Person B", official.n, as_of=_now().date() - timedelta(days=10))
    _claim(st, 1, "Person A", blog.n, as_of=_now().date() - timedelta(days=500))
    dr._resolve(st)
    res = st.resolutions[1]
    assert res.status == dr.STATUS_CURRENT
    assert res.value == "Person B"
    assert res.superseded and res.superseded[0]["value"] == "Person A"
    assert not res.conflicts
    assert st.resolutions[2].status == dr.STATUS_UNKNOWN


def test_comparable_sources_with_comparable_dates_are_a_conflict_not_a_choice():
    st = _state()
    a = _src(st, "https://paper-a.example/news/story", "story a " * 50, authority=70,
             kind="news", published=_now() - timedelta(days=3))
    b = _src(st, "https://paper-b.example/news/story", "story b " * 50, authority=70,
             kind="news", published=_now() - timedelta(days=2))
    _claim(st, 1, "Person X", a.n, as_of=_now().date() - timedelta(days=3))
    _claim(st, 1, "Person Y", b.n, as_of=_now().date() - timedelta(days=2))
    dr._resolve(st)
    res = st.resolutions[1]
    assert res.status == dr.STATUS_CONFLICTING
    assert res.conflicts and {res.value, res.conflicts[0]["value"]} == {"Person X", "Person Y"}
    assert res.confidence < 0.6, "a live disagreement is not a confident answer"


def test_a_claim_the_source_itself_calls_history_never_becomes_current():
    st = _state()
    ref = _src(st, "https://en.wikipedia.org/wiki/Office", "encyclopaedia " * 50, authority=70,
               kind="reference", published=_now() - timedelta(days=1))
    _claim(st, 1, "Person Old", ref.n, as_of=_now().date() - timedelta(days=1), hint="historical")
    _claim(st, 1, "Person New", ref.n, as_of=_now().date() - timedelta(days=1), hint="current")
    dr._resolve(st)
    res = st.resolutions[1]
    assert res.value == "Person New"
    assert any(s["value"] == "Person Old" for s in res.superseded)


def test_independent_corroboration_raises_confidence_but_copies_do_not():
    st = _state()
    story = "the board appointed a new chief executive effective next month " * 20
    a = _src(st, "https://paper-a.example/news/1", story, authority=70, kind="news",
             published=_now() - timedelta(days=1))
    copy = _src(st, "https://paper-b.example/news/1", "Breaking: " + story, authority=70,
                kind="news", published=_now() - timedelta(days=1))
    assert copy.dup_of == a.n, "the syndicated copy is registered as a duplicate"
    _claim(st, 1, "Person Z", a.n, as_of=_now().date() - timedelta(days=1))
    _claim(st, 1, "Person Z", copy.n, as_of=_now().date() - timedelta(days=1))
    dr._resolve(st)
    one_source = st.resolutions[1]
    assert one_source.independent == 1, "a copy is not an independent confirmation"

    st2 = _state()
    a2 = _src(st2, "https://paper-a.example/news/1", story, authority=70, kind="news",
              published=_now() - timedelta(days=1))
    other = _src(st2, "https://wire.example/news/2", "an independently written account " * 25,
                 authority=70, kind="news", published=_now() - timedelta(days=1))
    assert other.dup_of is None
    _claim(st2, 1, "Person Z", a2.n, as_of=_now().date() - timedelta(days=1))
    _claim(st2, 1, "Person Z", other.n, as_of=_now().date() - timedelta(days=1))
    dr._resolve(st2)
    assert st2.resolutions[1].independent == 2
    assert st2.resolutions[1].confidence > one_source.confidence


def test_registration_carries_provenance_and_flags_primaries():
    st = _state()
    rec = _src(st, "https://ministry.gov.example/press/statement", "statement " * 40,
               authority=100, kind="official", published=_now() - timedelta(days=2))
    assert rec.primary
    assert "official" in rec.label() and "published" in rec.label() and "primary source" in rec.label()
    forum = _src(st, "https://forum.example/t/12", "thread " * 40, authority=40, kind="community")
    assert not forum.primary
    assert "undated" in forum.label()


# ---------------------------------------------------------------------------
# Links worth following
# ---------------------------------------------------------------------------


def test_candidate_links_prefer_the_primary_source_an_article_cites():
    st = _state(question="who is the chief executive of Acme Corp", subqs=("who is the chief executive of Acme Corp",))
    st.entities = ["Acme Corp"]
    article = _src(
        st, "https://paper.example/news/acme-ceo", "article " * 50, authority=40, kind="news",
        links=[
            "https://acme.example/press-releases/new-chief-executive",  # the source
            "https://paper.example/login",  # never
            "https://twitter.com/intent/tweet?url=x",  # never
            "https://paper.example/tag/business",  # listing
            "https://acme.example/investors/annual-report.pdf",  # a document
            "https://unrelated.example/weather",  # nothing suggests it matters
        ],
    )
    picks = [link for link, _src_ in dr._candidate_links(st, [article], limit=6)]
    assert "https://acme.example/press-releases/new-chief-executive" in picks
    assert "https://acme.example/investors/annual-report.pdf" in picks
    assert not any("login" in u or "intent/tweet" in u or "/tag/" in u for u in picks)
    assert "https://unrelated.example/weather" not in picks
    assert len(picks) <= 2, "at most two links per page"


# ---------------------------------------------------------------------------
# Stopping on evidence, and saying why
# ---------------------------------------------------------------------------


def test_stops_after_two_rounds_without_information_gain():
    st = _state()
    st.iterations = 2
    st.rounds = [
        dr.RoundStats(1, "search", ["a"], attempted=8, fetched=8, new_sources=0),
        dr.RoundStats(2, "follow-up", ["b"], attempted=8, fetched=8, new_sources=0),
    ]
    st.resolutions = {1: dr.Resolution(1, "x", dr.STATUS_UNKNOWN)}
    assert dr._should_stop(st, {"sufficient": False}, ["c"]) == "no_information_gain"


def test_a_round_of_duplicates_stops_the_loop():
    st = _state()
    st.iterations = 1
    st.rounds = [dr.RoundStats(1, "search", ["a"], attempted=8, fetched=8, new_sources=1, duplicates=7)]
    assert dr._should_stop(st, {"sufficient": False}, ["c"]) == "duplicate_rate"


def test_sufficient_with_an_unanswered_subquestion_keeps_looking_while_it_can():
    st = _state()
    st.iterations = 1
    st.rounds = [dr.RoundStats(1, "search", ["a"], attempted=8, fetched=8, new_sources=8)]
    st.resolutions = {1: dr.Resolution(1, "x", dr.STATUS_CURRENT, confidence=0.8),
                      2: dr.Resolution(2, "y", dr.STATUS_UNKNOWN)}
    # An UNKNOWN subquestion with places left to look is "not found yet".
    assert dr._should_stop(st, {"sufficient": True}, ["site:org.example y"]) == ""
    # ...and "sufficient" once nothing is unresolved.
    st.resolutions[2] = dr.Resolution(2, "y", dr.STATUS_CURRENT, confidence=0.7)
    assert dr._should_stop(st, {"sufficient": True}, ["more"]) == "sufficient"
    # ...or when there is nowhere left to look.
    st.resolutions[2] = dr.Resolution(2, "y", dr.STATUS_UNKNOWN)
    assert dr._should_stop(st, {"sufficient": False}, []) == "no_new_queries"


def test_synthetic_followups_target_the_authoritative_domains_already_found():
    st = _state()
    _src(st, "https://ministry.gov.example/a", "official " * 40, authority=100, kind="official")
    _src(st, "https://blog.example/b", "blog " * 40, authority=15, kind="blog")
    st.resolutions = {1: dr.Resolution(1, "who leads it", dr.STATUS_UNKNOWN),
                      2: dr.Resolution(2, "when did that change", dr.STATUS_CURRENT)}
    qs = dr._synthetic_followups(st, ["who leads it"])
    assert qs and all(q.startswith("site:ministry.gov.example ") for q in qs)
    # Nothing authoritative found → nothing invented.
    st2 = _state()
    _src(st2, "https://blog.example/b", "blog " * 40, authority=15, kind="blog")
    assert dr._synthetic_followups(st2, ["x"]) == []


def test_a_time_sensitive_plan_gets_the_current_year_added_once():
    st = _state(question="who is the current chief of the agency")
    out = dr._augment_queries(st, ["agency chief", "agency leadership"], cap=5)
    assert out[-1].endswith(str(st.now_year))
    assert len(out) == 3
    # Already dated, or timeless: untouched.
    assert dr._augment_queries(st, [f"agency chief {st.now_year}"], cap=5) == [f"agency chief {st.now_year}"]
    assert dr._augment_queries(_state(sensitive=False), ["what is photosynthesis"], cap=5) == ["what is photosynthesis"]


# ---------------------------------------------------------------------------
# The full loop, offline: verification round, claims persisted, meta + logs
# ---------------------------------------------------------------------------


def _emitter():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    return events, emit


def _results(n, host="example.com"):
    return [SearchResult(title=f"Doc {i}", url=f"https://{host}/p{i}", snippet=f"snippet {i}")
            for i in range(1, n + 1)]


def _wire(monkeypatch, *, verify, claims, gap=None):
    plan = {"subquestions": ["who leads it"], "queries": ["q1", "q2"], "entities": ["Acme"]}
    gaps = list(gap or [{"sufficient": True, "missing": [], "followup_queries": []}])
    calls = {"schemas": []}

    async def fake_json_completion(messages, **kw):
        name = kw.get("schema_name")
        calls["schemas"].append(name)
        if name == "research_plan":
            return json.dumps(plan)
        if name == "research_claims":
            return json.dumps(claims)
        if name == "research_verify":
            return json.dumps(verify)
        return json.dumps(gaps.pop(0) if len(gaps) > 1 else gaps[0])

    fetched_batches = []

    async def fake_collect(queries, effort="medium", emit=None, categories="", **kw):
        # Verification queries find a NEW page; ordinary ones the same four.
        if any(q.startswith("verify") for q in queries):
            return [SearchResult(title="Official", url="https://org.gov.example/official", snippet="s")]
        return _results(4)

    async def fake_rerank(message, res, target):
        return res

    async def fake_fetch(res, message=""):
        fetched_batches.append([r.url for r in res])
        out = []
        for i, r in enumerate(res, 1):
            official = "gov.example" in r.url
            out.append(_Source(
                n=i, title=r.title, url=r.url,
                text=(f"official statement about the leader {i} " if official else f"body {i} ") * 30,
                authority=100 if official else 40, source_type="official" if official else "news",
                published_at=_now() - timedelta(days=1), fetched_at=_now(),
            ))
        return out

    async def fake_stream(messages, **kw):
        for piece in ("Report ", "[1]."):
            yield ("token", piece)

    monkeypatch.setattr(dr.llm, "json_completion", fake_json_completion)
    monkeypatch.setattr(dr.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(dr, "_collect_results", fake_collect)
    monkeypatch.setattr(dr, "_rerank_results", fake_rerank)
    monkeypatch.setattr(dr, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(dr, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(dr.db, "create_research_run", lambda *a, **k: 1)
    monkeypatch.setattr(dr.db, "finish_research_run", lambda *a, **k: None)
    monkeypatch.setattr(settings, "deep_research_min_sources", 1)
    return calls, fetched_batches


def test_low_confidence_triggers_one_targeted_verification_round(monkeypatch, caplog):
    claims = {"claims": [
        {"subquestion": 1, "claim": "Person A leads it", "value": "Person A", "source": 1,
         "as_of": "2024-01-01", "status": "current"},
    ]}
    verify = {"verdicts": [{"subquestion": 1, "enough_evidence": False, "primary_source_opened": False,
                            "confidence": 0.3, "verification_queries": ["verify: official leadership page"]}],
              "overall_confidence": 0.3}
    calls, batches = _wire(monkeypatch, verify=verify, claims=claims)
    persisted = []
    monkeypatch.setattr(dr.db, "insert_web_claims", lambda rows: persisted.extend(rows) or len(rows))
    events, emit = _emitter()
    with caplog.at_level(logging.INFO, logger="app.engines.deep_research"):
        out = asyncio.run(dr.run_deep_research_engine("who is the current chief of Acme", [], emit, conversation_id="c1"))
    assert out.startswith("Report")
    meta = [p for k, p in events if k == "meta"][-1]
    run = meta["research_run"]
    assert run["verification_rounds"] == 1
    assert run["iterations"] == 2
    assert any("gov.example" in u for batch in batches for u in batch), "the verification query was searched"
    assert run["stop_reason"]
    assert run["resolutions"] and run["resolutions"][0]["status"] in ("current", "conflicting")
    assert run["claims"] >= 1
    assert run["primary_sources"], "the official page was recognised as primary"
    assert meta["sources"][0]["published_at"]
    # Claims were persisted for the knowledge layer.
    assert persisted and persisted[0]["kind"] in ("current", "conflicting")
    # The decisions are visible in the log.
    text = caplog.text
    assert "opened [" in text and "resolution [" in text and "verify [" in text and "done:" in text
    # Every started step is finished.
    steps = [p for k, p in events if k == "step"]
    assert {s["id"] for s in steps if s["status"] == "running"} == {s["id"] for s in steps if s["status"] == "done"}
    titles = [s["title"] for s in steps]
    assert any("Verifying" in t for t in titles)


def test_confident_evidence_skips_the_verification_round(monkeypatch):
    claims = {"claims": [
        {"subquestion": 1, "claim": "Person A leads it", "value": "Person A", "source": 1, "as_of": "2026-08-01", "status": "current"},
        {"subquestion": 1, "claim": "Person A leads it", "value": "Person A", "source": 2, "as_of": "2026-08-01", "status": "current"},
    ]}
    verify = {"verdicts": [{"subquestion": 1, "enough_evidence": True, "primary_source_opened": True, "confidence": 0.9}]}
    _wire(monkeypatch, verify=verify, claims=claims)
    monkeypatch.setattr(dr.db, "insert_web_claims", lambda rows: len(rows))
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    run = [p for k, p in events if k == "meta"][-1]["research_run"]
    assert run["verification_rounds"] == 0
    assert run["iterations"] == 1
    assert run["stop_reason"] == "sufficient"


def test_the_report_prompt_carries_the_date_and_the_evidence_table():
    st = _state(question="who is the current chief")
    src = _src(st, "https://org.gov.example/a", "official " * 40, authority=100, kind="official",
               published=_now() - timedelta(days=2))
    _claim(st, 1, "Person B", src.n, as_of=_now().date() - timedelta(days=2))
    dr._resolve(st)
    msgs = dr._report_messages(st, [])
    system, user = msgs[0]["content"], msgs[-1]["content"]
    assert f"Current date: {st.today}" in system
    assert "EVIDENCE STATUS" in user and "CURRENT" in user and "Person B" in user
    assert "same text as [n]" in system and "primary sources" in system.lower()
