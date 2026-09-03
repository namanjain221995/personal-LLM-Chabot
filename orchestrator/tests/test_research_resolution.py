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


def test_incidental_links_to_authoritative_but_unrelated_sites_are_not_followed():
    """Live run 2026-09-02: a blog post about a company's chief executive
    linked to a government statistics bureau and a phone maker's press page;
    both were opened because the target was 'more authoritative'. Authority
    alone is not a reason — the link must be tied to the question by its
    words, by being the entity's own site, or by a domain already found."""
    st = _state(question="who is the chief executive of Acme Corp", subqs=("who is the chief executive of Acme Corp",))
    st.entities = ["Acme Corp"]
    post = _src(
        st, "https://someone.blog.example/thoughts", "post " * 50, authority=15, kind="blog",
        links=[
            "https://stats.gov.example/employment/2026/release",  # authoritative, unrelated
            "https://phones.example/newsroom/new-device",  # press path, unrelated
            "https://acme.example/about/leadership",  # the entity's own site, no keyword overlap
        ],
    )
    picks = [link for link, _s in dr._candidate_links(st, [post], limit=6)]
    assert picks == ["https://acme.example/about/leadership"]


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
            # Every page states the value the wired claims assert ("Person A")
            # — persistence now verifies a value against the source text and
            # drops what no page says.
            out.append(_Source(
                n=i, title=r.title, url=r.url,
                text=(f"official statement about the leader {i}. " if official else f"body {i}. ") * 15
                + "The board confirmed that Person A leads it. " * 15,
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
    # Claims were persisted for the knowledge layer — as the source's own
    # sentence, never as the asker's question.
    assert persisted and persisted[0]["kind"] in ("current", "conflicting")
    assert "Person A" in persisted[0]["quote"] and persisted[0]["claim"] == persisted[0]["quote"]
    assert "who is the current chief" not in persisted[0]["claim"].lower()
    assert "who is the current chief" not in persisted[0]["subquestion"].lower()
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


def test_the_planner_never_sees_the_memory_blocks(monkeypatch):
    """The saved-facts / cross-chat blocks ride in history as system
    messages for the chat engine's benefit. The research planner must not
    read them — it listed the signed-in user's name as an entity to research
    on the first live run."""
    seen = {}

    async def fake_json_completion(messages, **kw):
        seen["messages"] = messages
        return json.dumps({"subquestions": ["a"], "queries": ["q"], "entities": ["Acme"]})

    monkeypatch.setattr(dr.llm, "json_completion", fake_json_completion)
    st = _state(question="who leads Acme")
    history = [
        {"role": "system", "content": "Facts about the user: their name is Someone Private."},
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    asyncio.run(dr._plan("who leads Acme", history, "think", st))
    contents = " ".join(m["content"] for m in seen["messages"] if m["role"] != "system" or "research planner" not in m["content"])
    assert "Someone Private" not in contents
    assert any(m["content"] == "earlier question" for m in seen["messages"])


# ---------------------------------------------------------------------------
# Persisting claims: only what a public page states, never the asker's words
# ---------------------------------------------------------------------------

#: A question carrying words that must never reach the shared store.
_PRIVATE_QUESTION = "my client Private Person asked me who is the chief executive of Acme Corp"


def _persist_state(source_text, *, value="Person B", url="https://acme.example/about/leadership",
                   authority=100, kind="official", superseded=()):
    """A state with one CURRENT resolution over one source, ready to persist."""
    st = _state(question=_PRIVATE_QUESTION, subqs=(_PRIVATE_QUESTION,))
    st.user_id = 7
    st.entities = ["Acme Corp", "Private Person"]
    src = _src(st, url, source_text, authority=authority, kind=kind,
               published=_now() - timedelta(days=2))
    st.resolutions = {1: dr.Resolution(
        1, _PRIVATE_QUESTION, dr.STATUS_CURRENT, value=value,
        as_of=_now().date() - timedelta(days=2), support=[src.n], independent=1,
        primary=src.primary, superseded=list(superseded), confidence=0.8,
    )}
    return st, src


def _capture(monkeypatch):
    persisted = []
    monkeypatch.setattr(dr.db, "insert_web_claims", lambda rows: persisted.extend(rows) or len(rows))
    return persisted


def _assert_nothing_private(row):
    """No column of a shared row may carry the question or its private words."""
    for key in ("claim", "subquestion", "quote", "value", "url"):
        text = str(row.get(key) or "").lower()
        assert _PRIVATE_QUESTION.lower() not in text, key
        assert "private person" not in text, key
        assert "my client" not in text, key


def test_a_value_no_source_states_is_not_persisted(monkeypatch, caplog):
    """The report may still say it; the SHARED store does not repeat a value
    the extractor produced but no page contains."""
    st, _ = _persist_state("Acme Corp is a company. Its leadership page lists the board. " * 10,
                           value="Person B")
    persisted = _capture(monkeypatch)
    with caplog.at_level(logging.INFO, logger="app.engines.deep_research"):
        asyncio.run(dr._persist_claims(st))
    assert persisted == []
    assert "not persisted" in caplog.text and "Person B" in caplog.text


def test_a_value_the_source_states_is_persisted_with_its_sentence_and_page():
    """End to end against the database: the quote is the source's sentence,
    the page is linked through the same url_key the store uses, and the
    origin columns say whose run it was."""
    import hashlib

    url = "https://www.acme.example/about/leadership/?utm_source=x"
    text = ("Acme Corp leadership.\nFounder and chief executive: Person B has led "
            "Acme Corp since March 2024.\nChief financial officer: Someone Else.")
    page = db.upsert_web_page(
        url_key=dr._normalize_url(url), url=url, canonical_url=url, title="Leadership",
        text=text, content_type="text/html", fetch_status=200,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    st, _ = _persist_state(text, value="Person B", url=url)
    asyncio.run(dr._persist_claims(st))
    with db.connection() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT page_id, url, subquestion, claim, quote, value, kind, origin_user_id, "
            "origin_conversation_id FROM web_claims ORDER BY id").fetchall()]
    assert len(rows) == 1
    row = rows[0]
    assert row["page_id"] == page["id"]
    assert row["quote"] == "Founder and chief executive: Person B has led Acme Corp since March 2024."
    assert row["claim"] == row["quote"] and len(row["quote"]) <= 400
    assert row["value"] == "Person B" and row["kind"] == "current"
    assert row["subquestion"] == "Acme Corp", "only the entity the source mentions, not the question"
    assert row["origin_user_id"] == 7 and row["origin_conversation_id"] == "c1"
    _assert_nothing_private(row)
    # ...and the Fast-mode lookup finds it by the entity's words.
    found = db.search_web_claims("Acme Corp chief executive", limit=3)
    assert found and found[0]["value"] == "Person B" and found[0]["domain"]


def test_the_asker_s_question_never_reaches_the_shared_store(monkeypatch):
    """Superseded values are verified the same way, and every stored column
    of every row is built from the page, not the subquestion."""
    text = ("Acme Corp announced today that Person B is its new chief executive. "
            "Person A, the previous chief executive, stepped down in 2023.")
    st, src = _persist_state(
        text, value="Person B",
        superseded=[{"value": "Person A", "as_of": "2023-06-01", "sources": [1]},
                    {"value": "Person Never", "as_of": "2020-01-01", "sources": [1]}],
    )
    persisted = _capture(monkeypatch)
    asyncio.run(dr._persist_claims(st))
    kinds = {r["value"]: r["kind"] for r in persisted}
    assert kinds == {"Person B": dr.STATUS_CURRENT, "Person A": dr.STATUS_SUPERSEDED}
    for row in persisted:
        _assert_nothing_private(row)
        assert row["quote"] and row["claim"] == row["quote"]
        assert row["url"] == src.url
        assert row["origin_user_id"] == 7 and row["origin_conversation_id"] == "c1"
    by_value = {r["value"]: r for r in persisted}
    assert by_value["Person A"]["quote"] == "Person A, the previous chief executive, stepped down in 2023."


def test_a_value_is_matched_as_whole_words_with_accents_and_punctuation_folded(monkeypatch):
    """The extractor drops diacritics and the page may hyphenate; neither is
    a different fact. A longer name that merely STARTS with the value is."""
    st, _ = _persist_state("The founder, François Dupont-Martin, chairs the board.",
                           value="Francois Dupont Martin")
    persisted = _capture(monkeypatch)
    asyncio.run(dr._persist_claims(st))
    assert len(persisted) == 1 and "François Dupont-Martin" in persisted[0]["quote"]

    st2, _ = _persist_state("Person Abbott chairs the board.", value="Person A")
    persisted2 = _capture(monkeypatch)
    asyncio.run(dr._persist_claims(st2))
    assert persisted2 == [], "'Person A' is not stated by a page that names Person Abbott"


def test_a_near_identical_sentence_counts_only_for_long_values(monkeypatch):
    """difflib >= 0.85 against the whole sentence: a one-character slip in a
    long statement passes; in a short name or number it is a different
    fact ('Person A' vs 'Person B' would score 0.875) and is refused."""
    long_value = "The board appointed Alexandra Petrov-Smith as chairman"
    st, _ = _persist_state("Notice. The board appointed Alexandra Petrov-Smyth as chairman. End.",
                           value=long_value)
    persisted = _capture(monkeypatch)
    asyncio.run(dr._persist_claims(st))
    assert len(persisted) == 1
    assert persisted[0]["quote"] == "The board appointed Alexandra Petrov-Smyth as chairman."

    st2, _ = _persist_state("Chief executive: Person A.", value="Person B")
    persisted2 = _capture(monkeypatch)
    asyncio.run(dr._persist_claims(st2))
    assert persisted2 == []
    st3, _ = _persist_state("Release: version 3.3 shipped.", value="version 3.2")
    persisted3 = _capture(monkeypatch)
    asyncio.run(dr._persist_claims(st3))
    assert persisted3 == []


def test_a_long_sentence_is_clipped_around_the_value(monkeypatch):
    filler = "the annual report lists many subsidiaries and offices around the world, "
    text = "Overview. " + filler * 12 + "and names Person B as chief executive, " + filler * 12 + "end."
    st, _ = _persist_state(text, value="Person B")
    persisted = _capture(monkeypatch)
    asyncio.run(dr._persist_claims(st))
    assert len(persisted) == 1
    quote = persisted[0]["quote"]
    assert len(quote) <= 400 and "Person B" in quote
    assert quote.startswith("…") and quote.endswith("…")


def test_the_best_supporting_source_supplies_the_quote(monkeypatch):
    """Two sources back the value; the primary/official one is quoted. The
    value is missing from the first-opened blog, so it is skipped, not
    treated as a reason to drop the claim."""
    st = _state(question=_PRIVATE_QUESTION, subqs=(_PRIVATE_QUESTION,))
    st.entities = ["Acme Corp"]
    blog = _src(st, "https://someone.blog.example/post", "A post about Acme Corp. " * 20,
                authority=15, kind="blog")
    official = _src(st, "https://acme.example/press/new-chief", "Acme Corp names Person B chief executive. " * 5,
                    authority=100, kind="official", published=_now() - timedelta(days=1))
    st.resolutions = {1: dr.Resolution(1, _PRIVATE_QUESTION, dr.STATUS_CURRENT, value="Person B",
                                       support=[blog.n, official.n], independent=2, confidence=0.9)}
    persisted = _capture(monkeypatch)
    asyncio.run(dr._persist_claims(st))
    assert len(persisted) == 1 and persisted[0]["url"] == official.url
    assert persisted[0]["quote"] == "Acme Corp names Person B chief executive."


def test_persisting_survives_a_missing_page_store(monkeypatch):
    """No stored page for the URL → page_id NULL, the row is still written;
    a failing page lookup is not a failing persist."""
    st, _ = _persist_state("Acme Corp: Person B is chief executive.", value="Person B")
    persisted = _capture(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(dr.db, "get_web_pages", boom)
    asyncio.run(dr._persist_claims(st))
    assert len(persisted) == 1 and persisted[0]["page_id"] is None
