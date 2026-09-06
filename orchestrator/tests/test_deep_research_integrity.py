"""Deep Research integrity: what the run may claim, and what it must admit.

The engine's own tests pin that the loop terminates and that citations
resolve. These pin the honesty of the record it leaves behind — the failures
that are invisible precisely because the output still looks finished:

* the REPORT writer is fed the chat history, and `recent_turns` keeps every
  pinned system block with it (cross-chat recall, the user's saved facts, a
  shared page's excerpt). A research report is exported to PDF and shared, so
  a saved personal fact reaching the report prompt is a leak with a delivery
  mechanism (R1);
* an auditor OUTAGE used to return "sufficient", so `stop_reason` said the
  evidence had been audited and judged adequate by a check that never ran (R2);
* a resolved value no source states used to be dropped from the shared claim
  store — correctly — while still reaching the report with a citation and a
  confidence number (R3);
* cancelling at minute 9 of a 10-minute run stored 0 sources and no report,
  discarding everything the run had already paid to read (R4);
* a mid-run search outage was a bare `continue`: no log, no counter, and a
  thin report presented as a mined web (R6);
* a disagreement from a much weaker undated source was filed as `superseded`,
  which the report prompt renders as a change over time — a temporal story no
  source stated (R7);
* the report can stop at its token ceiling with nothing saying so (R9);
* gaps found in an early round were overwritten by the next audit and never
  reached "What this report could not establish" (R10);
* the long tail of the evidence was HEAD-sliced into the report prompt, which
  is finding C1 one function further down the pipe: the answer row of a long
  page is dropped, the page is still cited, and the report says the fact is
  absent (C1 / `_trim_evidence`);
* the wall-clock budget was advisory — read between rounds and nowhere else —
  so a 599-second run could still start a whole round, a verification pass and
  a long report while holding the process-wide lock (R8);
* the process-wide lock itself: one person's ten-minute run REFUSED every other
  user's request, org-wide, and the refusal could not say when to come back
  (R13). A shared platform queues within a budget; it does not tell an
  unrelated user no.

Everything is offline: no vLLM, no SearXNG, no network, no database.
"""
import asyncio
import collections
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import settings
from app.core import extract
from app.engines import deep_research as dr
from app.engines.search import _Source
from app.freshness import Freshness, Verdict
from app.search.base import SearchResult, SearchUnavailableError
from app.sse import ALL_EVENTS


# ---------------------------------------------------------------------------
# helpers (the same shapes the sibling suites use)
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc)


def _state(question="q", subqs=("who leads it",), sensitive=True):
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


def _src(st, url, text, *, authority=40, kind="", published=None):
    return dr._register(
        st,
        _Source(
            n=0, title=url, url=url, text=text, published_at=published,
            fetched_at=_now(), authority=authority, source_type=kind or "unknown",
        ),
        "q",
    )


def _claim(st, subq, value, source_n, as_of=None, hint="current"):
    st.claims.append(
        dr.Claim(subq=subq, text=f"claim {value}", value=value, source_n=source_n,
                 as_of=as_of, hint=hint, iteration=1)
    )


def _emitter():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    return events, emit


def _results(n, host="example.com"):
    return [
        SearchResult(title=f"Doc {i}", url=f"https://{host}/p{i}", snippet=f"snippet {i}")
        for i in range(1, n + 1)
    ]


def _wire(monkeypatch, *, plan=None, gap=None, report="Report [1]."):
    """Every outside dependency of the loop, stubbed with canned answers."""
    plan = plan or {"subquestions": ["a"], "queries": ["q1"], "entities": ["Acme"]}
    gaps = list(gap or [{"sufficient": True, "missing": [], "followup_queries": []}])

    async def fake_json_completion(messages, **kw):
        name = kw.get("schema_name")
        if name == "research_plan":
            return json.dumps(plan)
        if name == "research_claims":
            return json.dumps({"claims": []})
        if name == "research_verify":
            return json.dumps({"verdicts": []})
        return json.dumps(gaps.pop(0) if len(gaps) > 1 else gaps[0])

    async def fake_collect(queries, effort="medium", emit=None, categories="", **kw):
        return _results(4)

    async def fake_rerank(message, res, target):
        return res

    async def fake_fetch(res, message=""):
        return [
            _Source(n=i, title=r.title, url=r.url, text=f"body {i} of a page. " * 30,
                    authority=40, source_type="news", fetched_at=_now())
            for i, r in enumerate(res, 1)
        ]

    async def fake_stream(messages, **kw):
        for piece in (report[i:i + 8] for i in range(0, len(report), 8)):
            yield ("token", piece)

    monkeypatch.setattr(dr.llm, "json_completion", fake_json_completion)
    monkeypatch.setattr(dr.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(dr, "_collect_results", fake_collect)
    monkeypatch.setattr(dr, "_rerank_results", fake_rerank)
    monkeypatch.setattr(dr, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(dr, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(dr.db, "create_research_run", lambda *a, **k: 1)
    monkeypatch.setattr(dr.db, "finish_research_run", lambda *a, **k: None)
    monkeypatch.setattr(dr, "_persist_claims", lambda state: asyncio.sleep(0))
    monkeypatch.setattr(settings, "deep_research_min_sources", 1)


def _run_meta(events):
    return [p for k, p in events if k == "meta"][-1]["research_run"]


# ---------------------------------------------------------------------------
# R1 — the report writer must not read the user's pinned memory
# ---------------------------------------------------------------------------


def test_the_report_writer_never_sees_the_memory_blocks():
    """`_conversation_turns` exists for exactly this hazard and was used in
    `_plan` only. The report is the document that gets exported and shared."""
    st = _state(question="who leads Acme")
    _src(st, "https://a.example/x", "a page about Acme " * 20, authority=70)
    history = [
        {"role": "system", "content": "Saved facts about the user: they live at 4 Private Road."},
        {"role": "system", "content": "From an earlier chat: their client is Someone Private."},
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    msgs = dr._report_messages(st, history)
    systems = [m for m in msgs if m["role"] == "system"]
    assert len(systems) == 1, "only the engine's own system prompt may be a system message"
    whole = " ".join(m["content"] for m in msgs)
    assert "4 Private Road" not in whole
    assert "Someone Private" not in whole
    # ...and the real conversation is still context.
    assert any(m["content"] == "earlier question" for m in msgs)


# ---------------------------------------------------------------------------
# R2 — an auditor outage is not "sufficient evidence"
# ---------------------------------------------------------------------------


def test_an_auditor_outage_is_never_reported_as_sufficient(monkeypatch):
    _wire(monkeypatch)

    async def auditor_down(messages, **kw):
        if kw.get("schema_name") == "research_plan":
            return json.dumps({"subquestions": ["a"], "queries": ["q1"]})
        raise RuntimeError("auditor unreachable")

    monkeypatch.setattr(dr.llm, "json_completion", auditor_down)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    run = _run_meta(events)
    assert run["stop_reason"] == "auditor_unavailable"
    assert run["stop_reason"] != "sufficient"
    assert run["evidence_audited"] is False
    # and the timeline says so rather than "no gaps found"
    details = [p.get("detail", "") for k, p in events if k == "step"]
    assert any("NOT audited" in d for d in details)
    assert not any("no gaps found" in d for d in details)


def test_the_auditor_failure_reaches_the_report_prompt():
    st = _state()
    _src(st, "https://a.example/x", "a page " * 30, authority=70)
    st.auditor_failed = True
    user = dr._report_messages(st, [])[-1]["content"]
    assert "AUDIT:" in user and "not audited" in user


# ---------------------------------------------------------------------------
# R3 — a value no source states is not a confident answer
# ---------------------------------------------------------------------------


def test_a_value_no_source_states_is_capped_and_marked():
    st = _state()
    a = _src(st, "https://a.example/x", "The organisation published its annual review today. " * 8,
             authority=70)
    b = _src(st, "https://b.example/y", "A separate account of the same week, in other words. " * 8,
             authority=70)
    _claim(st, 1, "Person Q", a.n)
    _claim(st, 1, "Person Q", b.n)
    dr._resolve(st)
    res = st.resolutions[1]
    assert res.value == "Person Q"
    assert res.stated_verbatim is False
    assert res.confidence <= dr._UNSTATED_CONFIDENCE_CAP
    assert "NOT STATED VERBATIM" in res.line()
    assert res.as_meta()["stated_verbatim"] is False
    # The report prompt carries the marker and the rule for it.
    msgs = dr._report_messages(st, [])
    assert "NOT STATED VERBATIM" in msgs[-1]["content"]
    assert "NOT STATED VERBATIM" in msgs[0]["content"]


def test_a_value_a_source_states_keeps_its_confidence():
    st = _state()
    a = _src(st, "https://a.example/x",
             "The board confirmed that Person Q leads it, effective immediately. " * 8,
             authority=70)
    _claim(st, 1, "Person Q", a.n)
    dr._resolve(st)
    res = st.resolutions[1]
    assert res.stated_verbatim is True
    assert res.confidence > dr._UNSTATED_CONFIDENCE_CAP
    assert "NOT STATED VERBATIM" not in res.line()


def test_corroboration_still_counts_among_unstated_values():
    """The cap must not flatten the evidence: two independent sources for an
    unstated value are still worth more than one."""
    def build(second_domain):
        st = _state()
        a = _src(st, "https://a.example/x", "One account of the week in question. " * 8, authority=70)
        _claim(st, 1, "Person Q", a.n)
        if second_domain:
            b = _src(st, f"https://{second_domain}/y", "A different account, written elsewhere. " * 8,
                     authority=70)
            _claim(st, 1, "Person Q", b.n)
        dr._resolve(st)
        return st.resolutions[1]

    one, two = build(None), build("b.example")
    assert one.stated_verbatim is False and two.stated_verbatim is False
    assert two.confidence > one.confidence


# ---------------------------------------------------------------------------
# R4 — cancellation keeps the record of what the run did
# ---------------------------------------------------------------------------


def test_cancelling_mid_report_keeps_the_partial_report_and_the_sources(monkeypatch):
    closed = {}

    def fake_finish(run_id, status, iterations, queries, sources, cited, report,
                    sources_meta=None, detail=""):
        closed.update(status=status, sources=sources, report=report,
                      sources_meta=list(sources_meta or []), detail=detail)

    _wire(monkeypatch)
    monkeypatch.setattr(dr.db, "finish_research_run", fake_finish)

    async def cancel_midway(messages, **kw):
        yield ("token", "Half a report about [1] ")
        raise asyncio.CancelledError()

    monkeypatch.setattr(dr.llm, "stream_chat_events", cancel_midway)
    events, emit = _emitter()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert closed["status"] == "cancelled"
    assert closed["report"].startswith("Half a report"), "the partial report was discarded"
    assert closed["sources"] > 0, "the run recorded 0 sources it had actually read"
    assert len(closed["sources_meta"]) == closed["sources"]


# ---------------------------------------------------------------------------
# R6 — a search outage is counted, and logged without the upstream detail
# ---------------------------------------------------------------------------


def test_a_mid_run_search_outage_is_counted(monkeypatch, caplog):
    st = _state()
    st.iterations = 2

    async def dead(queries, effort="medium", emit=None, categories="", **kw):
        raise SearchUnavailableError("PRIVATE_UPSTREAM basic-auth=hunter2")

    monkeypatch.setattr(dr, "_collect_results", dead)
    with caplog.at_level(logging.WARNING, logger="app.engines.deep_research"):
        added = asyncio.run(dr._gather(st, ["q1"], "think", None))
    assert added == []
    assert st.search_outages == 1
    assert st.rounds[-1].search_outages == 1
    assert st.rounds[-1].as_meta()["search_outages"] == 1
    # A category, never the exception detail: an upstream error can carry
    # credentials, and this log is shipped.
    assert "unavailable" in caplog.text
    assert "PRIVATE_UPSTREAM" not in caplog.text
    assert "hunter2" not in caplog.text


def test_the_outage_reaches_meta_and_the_report_prompt(monkeypatch):
    st = _state()
    _src(st, "https://a.example/x", "a page " * 30, authority=70)
    st.search_outages = 2
    user = dr._report_messages(st, [])[-1]["content"]
    assert "SEARCH COVERAGE" in user and "2 of this run's query groups" in user

    _wire(monkeypatch)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    run = _run_meta(events)
    assert run["search_outages"] == 0, "a healthy run reports no outage"
    assert run["rounds"][0]["search_outages"] == 0


# ---------------------------------------------------------------------------
# R7 — a weaker disagreeing source is a dispute, not history
# ---------------------------------------------------------------------------


def test_a_weak_undated_dissent_is_a_conflict_not_invented_history():
    st = _state()
    strong = _src(st, "https://org.example/a", "an official statement of the position " * 20,
                  authority=80)
    weak = _src(st, "https://forum.example/b", "someone recounting the same subject elsewhere " * 20,
                authority=40)
    _claim(st, 1, "Person S", strong.n)  # neither side carries a date
    _claim(st, 1, "Person W", weak.n)
    dr._resolve(st)
    res = st.resolutions[1]
    assert res.status == dr.STATUS_CONFLICTING, "an open disagreement was settled by fiat"
    assert not res.superseded, "no source dated either value: there is no change over time"
    assert res.conflicts and res.conflicts[0]["value"] == "Person W"
    assert res.conflicts[0]["weak"] is True
    assert "disputed by a weaker source" in res.line()
    assert "superseded" not in res.line()


def test_a_dated_older_value_is_still_history():
    """The dispute rule must not swallow the real supersession case."""
    st = _state()
    new = _src(st, "https://org.gov.example/leadership", "official page " * 30, authority=100,
               kind="official", published=_now() - timedelta(days=10))
    old = _src(st, "https://someone.example/post", "a much older post " * 30, authority=15,
               published=_now() - timedelta(days=500))
    _claim(st, 1, "Person B", new.n, as_of=_now().date() - timedelta(days=10))
    _claim(st, 1, "Person A", old.n, as_of=_now().date() - timedelta(days=500))
    dr._resolve(st)
    res = st.resolutions[1]
    assert res.status == dr.STATUS_CURRENT
    assert res.superseded and res.superseded[0]["value"] == "Person A"
    assert not res.conflicts


# ---------------------------------------------------------------------------
# R9 — a report cut off at its ceiling says so
# ---------------------------------------------------------------------------


def test_the_report_ceiling_is_the_one_the_call_actually_gets():
    """The ledger's premise (a 6,000-token wall) holds only with thinking OFF:
    `stream_chat_events` floors a thinking request at MAX_OUTPUT_TOKENS."""
    assert dr._report_token_ceiling("fast") == settings.deep_research_report_max_tokens
    assert dr._report_token_ceiling("think") >= settings.max_output_tokens


def _usage_sequence(monkeypatch, *values):
    state = {"i": 0}

    def fake_usage():
        i = min(state["i"], len(values) - 1)
        state["i"] += 1
        return {"prompt_tokens": 0, "completion_tokens": values[i], "calls": 1}

    monkeypatch.setattr(dr.llm, "get_usage", fake_usage)


def test_a_report_cut_off_at_its_ceiling_is_disclosed(monkeypatch):
    _wire(monkeypatch, report="A report that ran out of room [1]")
    _usage_sequence(monkeypatch, 0, dr._report_token_ceiling("think"))
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert "reached its length limit" in out
    assert _run_meta(events)["report_truncated"] is True
    # the disclosure was streamed too, not only stored
    streamed = "".join(p["text"] for k, p in events if k == "token")
    assert "reached its length limit" in streamed


def test_a_report_that_finished_is_not_flagged(monkeypatch):
    _wire(monkeypatch, report="A short, complete report [1].")
    _usage_sequence(monkeypatch, 0, 120)
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert "length limit" not in out
    assert _run_meta(events)["report_truncated"] is False


def test_unmeasured_usage_never_reads_as_truncated(monkeypatch):
    """A runtime that refuses stream_options reports no usage at all: that is
    NOT MEASURED, and must not be rendered as either verdict falsely."""
    _wire(monkeypatch, report="A report [1].")
    monkeypatch.setattr(dr.llm, "get_usage", lambda: None)
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert "length limit" not in out
    assert _run_meta(events)["report_truncated"] is False


# ---------------------------------------------------------------------------
# R10 — gaps accumulate across rounds
# ---------------------------------------------------------------------------


def test_a_gap_from_an_early_round_survives_to_the_report(monkeypatch):
    _wire(
        monkeypatch,
        gap=[
            {"sufficient": False, "missing": ["the founding date"], "followup_queries": ["fq1"]},
            {"sufficient": True, "missing": ["the current headcount"], "followup_queries": []},
        ],
    )
    monkeypatch.setattr(settings, "deep_research_max_iterations", 3)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    run = _run_meta(events)
    assert run["iterations"] >= 2
    assert "the founding date" in run["missing"], "round 1's gap was overwritten"
    assert "the current headcount" in run["missing"]


def test_accumulation_is_deduplicated_and_order_preserving():
    first = dr._accumulate([], ["a gap", "another gap"])
    second = dr._accumulate(first, ["Another Gap", "a third gap", ""])
    assert second == ["a gap", "another gap", "a third gap"]
    assert dr._accumulate(["x"], None) == ["x"]
    assert dr._accumulate([], [f"gap {i}" for i in range(50)], cap=4) == [
        "gap 0", "gap 1", "gap 2", "gap 3"
    ]


# ---------------------------------------------------------------------------
# C1 — the long tail of the evidence must keep the answer, not the lede
#
# The phase's headline finding, in the function the search-path fix did not
# reach. `_trim_evidence` cut every source after the tenth to its first 2,500
# characters; on `leaderboard_long.html` the answer row sits at character
# 19,831 of 20,136, so the report writer got a page that had been fetched, was
# cited in the panel, and no longer contained the answer.
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "web_eval"
LEADERBOARD_Q = "What is GPT-5.2's reasoning score on the BenchLM leaderboard?"


def _page(name="leaderboard_long.html"):
    """The fixture as the pipeline sees it — through the real extractor."""
    ext, _links = extract.extract_readable_and_links(
        "text/html", (FIXTURES / name).read_bytes(), f"https://benchlm.test/{name}"
    )
    return ext.text


def _tail_source(text, n=None):
    """One source BEYOND the tier-A cutoff, where the trim actually bites."""
    return dr.SourceRecord(
        n=n if n is not None else dr._TIER_A_SOURCES + 1,
        title="BenchLM Leaderboard",
        url="https://benchlm.test/leaderboard_long.html",
        text=text,
        query=LEADERBOARD_Q,
        iteration=1,
        fetched_at=_now(),
        source_type="reference",
    )


def test_the_fixture_still_hides_its_answer_where_the_finding_says_it_does():
    """The premise of everything below. If the fixture is edited, this says so
    rather than letting the trim tests pass for the wrong reason."""
    text = _page()
    assert text.index("| 12 | GPT-5.2 | 82.7") > 19_000
    assert 20_000 < len(text) < 21_000


def test_the_old_head_slice_is_what_lost_the_answer():
    """The control. This is exactly what `_trim_evidence` did until now, and
    it is also what it still does for a caller with no question to centre on
    (the engine suite's own trim test passes the list alone)."""
    src = _tail_source(_page())
    dr._trim_evidence([src])
    assert len(src.text) <= dr._TIER_B_CHARS + 10
    assert "82.7" not in src.text, "the head slice kept the answer by accident"
    assert "GPT-5.2" not in src.text


def test_the_answer_row_survives_the_long_tail_trim():
    """C1 acceptance: source 11 of a 24-source run keeps the row it is cited
    for, inside the same character budget."""
    src = _tail_source(_page())
    dr._trim_evidence([src], LEADERBOARD_Q)
    assert len(src.text) <= dr._TIER_B_CHARS, "the trim may reorder its budget, not enlarge it"
    assert "GPT-5.2" in src.text
    assert "82.7" in src.text


def test_the_trim_bites_the_selection_the_fetcher_now_returns():
    """The production shape, and the reason this got WORSE when the search
    path was fixed. `_fetch_sources` now returns 8,000 query-centred
    characters kept in DOCUMENT order, so the passage the question points at
    is the LAST one — measured at character 7,829 of 8,000 on this fixture. A
    head slice of a query-centred selection is therefore close to guaranteed
    to drop the one passage the selection existed to keep."""
    from app.web_memory import select_passages

    fetched = select_passages(_page(), LEADERBOARD_Q, 8000)
    assert "82.7" in fetched and fetched.find("82.7") > 7_000

    head, centred = _tail_source(fetched), _tail_source(fetched)
    dr._trim_evidence([head])                    # today's shape
    dr._trim_evidence([centred], LEADERBOARD_Q)  # the fix
    assert "82.7" not in head.text
    assert "82.7" in centred.text


def test_the_top_tier_is_never_trimmed_at_all():
    """The two-tier shape is the point: the best sources keep their full
    budget, so High is never shallower than Medium where it matters."""
    src = _tail_source(_page(), n=dr._TIER_A_SOURCES)
    before = src.text
    dr._trim_evidence([src], LEADERBOARD_Q)
    assert src.text == before


def test_the_trimmed_answer_reaches_the_report_prompt():
    """End to end through the prompt builder: the row must be in the evidence
    block the model actually reads, not merely in the record."""
    st = _state(question=LEADERBOARD_Q, subqs=["what does GPT-5.2 score"])
    for i in range(1, dr._TIER_A_SOURCES + 1):
        st.sources.append(
            dr.SourceRecord(n=i, title=f"filler {i}", url=f"https://f.example/{i}",
                            text="an unrelated page about something else. " * 40,
                            query=LEADERBOARD_Q, iteration=1, fetched_at=_now())
        )
    st.sources.append(_tail_source(_page()))
    dr._trim_evidence(st.sources, st.question)
    user = dr._report_messages(st, [])[-1]["content"]
    assert "82.7" in user
    assert "[11]" in user, "the source kept its citation number"


def test_the_claim_extractor_reads_the_part_of_the_page_the_question_asks_for():
    """The same head slice, one stage earlier and with more consequence: this
    excerpt is what BECOMES the evidence. A fact outside it is never extracted
    as a claim, never resolved and never verified, however often the page is
    cited."""
    st = _state(question=LEADERBOARD_Q, subqs=["what does GPT-5.2 score"])
    src = _tail_source(_page(), n=1)
    st.sources.append(src)
    seen = {}

    async def capture(messages, **kw):
        seen[kw.get("schema_name")] = messages[-1]["content"]
        return json.dumps({"claims": []})

    import unittest.mock as _mock

    with _mock.patch.object(dr.llm, "json_completion", capture):
        asyncio.run(dr._extract_claims(st, [src], "think", None))
    prompt = seen["research_claims"]
    assert "82.7" in prompt, "the claim extractor read the lede, not the answer"
    assert "GPT-5.2" in prompt


# ---------------------------------------------------------------------------
# R8 — the wall-clock budget bounds the run instead of advising it
# ---------------------------------------------------------------------------


def test_the_budget_reserves_room_for_the_report():
    """Arithmetic, stated once: gathering stops early enough that a run which
    spends its whole budget can still write what it found."""
    st = _state()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "deep_research_timeout_s", 600.0)
        assert st.gather_budget_s == 450.0
        assert st.report_budget_s() >= 150.0
        # A run that stopped early keeps the whole remainder for the report.
        assert st.report_budget_s() <= 600.0
        # …and a run already over its budget still gets a floor, not nothing.
        st.started_at -= 10_000
        assert st.report_budget_s() == 150.0
        assert st.gather_left() < 0
        assert st.budget_reason() == "timeout"


def test_no_configured_budget_keeps_exactly_the_old_behaviour():
    """A non-positive budget has always meant "one round, then stop". Bounding
    the stages must not turn it into a run that gathers nothing at all."""
    st = _state()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "deep_research_timeout_s", 0.0)
        assert st.gather_budget_s == 0.0
        assert st.budget_left() is False
        assert st.gather_left() == float("inf"), "the first round would be skipped"
        # The LLM's own wall clock is then the only bound on the report.
        assert st.report_budget_s() == float(settings.gen_wall_clock_s)


def test_a_zero_budget_still_runs_one_round(monkeypatch):
    _wire(monkeypatch, report="One round is still a run [1].")
    monkeypatch.setattr(settings, "deep_research_timeout_s", 0.0)
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    run = _run_meta(events)
    assert run["iterations"] == 1
    assert run["sources_found"] > 0, "the round was skipped instead of run"
    assert run["stop_reason"] == "timeout"
    assert "One round is still a run" in out


def test_a_round_that_overruns_is_cut_and_the_run_still_reports(monkeypatch):
    """R8 acceptance. Round 2 would take 30 seconds against a sub-second
    budget: without the bound the run holds `_RUN_LOCK` for all of it. With
    it, the round is cut, the sources round 1 read are kept, the report is
    written from them, and meta says the clock is why."""
    calls = {"n": 0}

    async def slow_second_round(queries, effort="medium", emit=None, categories="", **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            await asyncio.sleep(30)
        return _results(4)

    _wire(
        monkeypatch,
        gap=[
            {"sufficient": False, "missing": ["more"], "followup_queries": ["fq1"]},
            {"sufficient": True, "missing": [], "followup_queries": []},
        ],
        report="A report from what was gathered [1].",
    )
    monkeypatch.setattr(dr, "_collect_results", slow_second_round)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 0.6)
    monkeypatch.setattr(settings, "deep_research_max_iterations", 5)

    events, emit = _emitter()
    started = time.monotonic()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    elapsed = time.monotonic() - started

    assert elapsed < 10, f"the 30s round was not bounded ({elapsed:.1f}s)"
    assert "A report from what was gathered" in out, "the run produced no report"
    run = _run_meta(events)
    assert run["stop_reason"] == "timeout"
    assert run["sources_found"] > 0, "the sources round 1 read were thrown away"
    assert any(s.startswith("round 2") for s in run["stages_cut_short"])
    assert run["time_budget_s"] == 0.6
    # The round that was cut is recorded as having taken time, not 0.0s.
    assert run["rounds"][-1]["elapsed_s"] > 0


def test_a_cut_short_run_says_so_in_the_report_prompt():
    st = _state()
    _src(st, "https://a.example/x", "a page " * 30, authority=70)
    st.cut_short = ["round 2 (follow-up)", "verification"]
    user = dr._report_messages(st, [])[-1]["content"]
    assert "TIME BUDGET" in user
    assert "round 2 (follow-up)" in user and "did not finish" in user


def test_verification_is_skipped_rather_than_run_past_the_budget(monkeypatch):
    """The verify pass is deliberately NOT gated on `budget_left()` — it
    matters most for the runs that ended on a cap. It is still not free, so
    when the clock has already run out it is skipped and SAID to be skipped,
    instead of spending another minute of somebody else's turn."""
    calls = {"n": 0}
    verified = {"n": 0}

    async def slow_second_round(queries, effort="medium", emit=None, categories="", **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            await asyncio.sleep(30)
        return _results(4)

    async def counting_verify(state, effort):
        verified["n"] += 1
        return {}, []

    _wire(
        monkeypatch,
        gap=[
            {"sufficient": False, "missing": ["more"], "followup_queries": ["fq1"]},
            {"sufficient": True, "missing": [], "followup_queries": []},
        ],
        report="Report [1].",
    )
    monkeypatch.setattr(dr, "_collect_results", slow_second_round)
    monkeypatch.setattr(dr, "_verify", counting_verify)
    monkeypatch.setattr(settings, "deep_research_verify", True)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 0.6)
    monkeypatch.setattr(settings, "deep_research_max_iterations", 5)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert verified["n"] == 0, "verification ran on a run with no budget left"
    details = [p.get("detail", "") for k, p in events if k == "step"]
    assert any("time budget" in d for d in details)
    assert "verification" in _run_meta(events)["stages_cut_short"]


def test_an_audit_the_clock_cut_short_is_not_a_finished_audit(monkeypatch):
    """The R2 rule, reached by the clock: an audit that did not run is an
    UNAUDITED run, never 'no gaps found'."""
    async def slow_auditor(messages, **kw):
        if kw.get("schema_name") == "research_plan":
            return json.dumps({"subquestions": ["a"], "queries": ["q1"]})
        if kw.get("schema_name") in ("research_claims", "research_verify"):
            return json.dumps({"claims": [], "verdicts": []})
        await asyncio.sleep(30)

    _wire(monkeypatch, report="Report [1].")
    monkeypatch.setattr(dr.llm, "json_completion", slow_auditor)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 0.6)
    events, emit = _emitter()
    started = time.monotonic()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert time.monotonic() - started < 10
    assert "Report" in out
    run = _run_meta(events)
    assert run["stop_reason"] == "timeout"
    assert run["evidence_audited"] is False
    details = [p.get("detail", "") for k, p in events if k == "step"]
    assert any("NOT audited" in d for d in details)
    assert not any("no gaps found" in d for d in details)


def test_a_report_stream_that_hangs_is_cut_and_keeps_what_it_wrote(monkeypatch, caplog):
    """`llm.py`'s only guard is GEN_WALL_CLOCK_S — 1,800 s by default, three
    times a whole research run. The words already streamed to the user must
    survive the cut, and the log must not carry the report's own text."""
    _wire(monkeypatch)

    async def stalls(messages, **kw):
        yield ("token", "The first half of a report [1] PRIVATE_UPSTREAM")
        await asyncio.sleep(30)
        yield ("token", "never arrives")

    monkeypatch.setattr(dr.llm, "stream_chat_events", stalls)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 0.6)
    monkeypatch.setattr(dr, "_REPORT_FLOOR_S", 0.4)

    events, emit = _emitter()
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.engines.deep_research"):
        out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert time.monotonic() - started < 10
    assert "The first half of a report" in out, "the streamed text was discarded"
    assert "reached its time budget" in out, "the cut was silent"
    assert "never arrives" not in out
    run = _run_meta(events)
    assert run["report_cut_short"] is True
    assert "report" in run["stages_cut_short"]
    assert "time budget" in caplog.text
    assert "PRIVATE_UPSTREAM" not in caplog.text


def test_a_healthy_run_is_not_marked_as_cut_short(monkeypatch):
    """The bound must be invisible when nothing overruns."""
    _wire(monkeypatch, report="A complete report [1].")
    monkeypatch.setattr(settings, "deep_research_timeout_s", 600.0)
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    run = _run_meta(events)
    assert run["stages_cut_short"] == []
    assert run["report_cut_short"] is False
    assert run["stop_reason"] == "sufficient"
    assert "time budget" not in out


def test_a_run_cut_before_its_first_source_does_not_blame_the_search_provider(monkeypatch):
    """"The search provider returned nothing usable" is a different failure
    from "the clock ran out before it was asked", and the user acts on them
    differently."""
    async def too_slow(queries, effort="medium", emit=None, categories="", **kw):
        await asyncio.sleep(30)

    closed = {}

    def fake_finish(run_id, status, iterations, queries, sources, cited, report,
                    sources_meta=None, detail=""):
        closed.update(status=status, detail=detail)

    _wire(monkeypatch)
    monkeypatch.setattr(dr, "_collect_results", too_slow)
    monkeypatch.setattr(dr.db, "finish_research_run", fake_finish)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 0.4)
    events, emit = _emitter()
    started = time.monotonic()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert time.monotonic() - started < 10
    assert "time budget" in out
    assert "search provider returned nothing" not in out
    assert closed["status"] == "failed"
    assert "time budget" in closed["detail"]


# ---------------------------------------------------------------------------
# R12 — a page about its own publisher is first-hand, not disinterested
# ---------------------------------------------------------------------------


def _resolved_from(domain, *, entities=("Acme",), kind="docs", authority=80):
    """One claim about Acme, resolved from a single source on `domain`.

    Everything except the host is held constant, so a difference in the
    resolution's confidence can only come from who published the page.
    """
    st = _state(question="what did Acme measure on the benchmark")
    st.entities = list(entities)
    src = _src(
        st,
        f"https://{domain}/benchmarks",
        "Acme measured 82.7 on the benchmark this quarter, up from last. " * 8,
        authority=authority,
        kind=kind,
    )
    _claim(st, 1, "82.7", src.n)
    dr._resolve(st)
    return st.resolutions[1], src


def test_a_vendors_own_page_is_still_primary_but_earns_half_the_bonus():
    """R12 wiring. `provenance.primary_weight` had no caller: `is_primary` is
    True for the vendor's own benchmark page, so a claim about the vendor got
    the full first-hand bonus from the vendor itself."""
    vendor_res, vendor_src = _resolved_from("acme.com")
    other_res, other_src = _resolved_from("mlperf.org")

    # Nothing is taken away from the source: it is first-hand, and it is
    # still recorded and cited as a primary source.
    assert vendor_src.primary is True and other_src.primary is True
    assert vendor_res.primary is True
    # What changes is only how much of the bonus it earned.
    assert vendor_src.primary_weight == 0.5
    assert other_src.primary_weight == 1.0
    assert other_res.confidence - vendor_res.confidence == pytest.approx(0.05)


def test_a_run_whose_planner_named_nobody_is_completely_unchanged():
    """The safety property the provenance author asked for: with no entities
    `primary_weight` is `is_primary` as a float, so an entity-less run cannot
    silently start half-trusting its sources."""
    blind, blind_src = _resolved_from("acme.com", entities=())
    other, _ = _resolved_from("mlperf.org")
    assert blind_src.primary_weight == 1.0
    assert blind.confidence == pytest.approx(other.confidence)


def test_one_independent_primary_restores_the_whole_group():
    """The group takes the BEST first-hand claim, not the average: a vendor
    page sitting beside an independent laboratory must not drag the
    laboratory's evidence down."""
    st = _state(question="what did Acme measure on the benchmark")
    st.entities = ["Acme"]
    text = "Acme measured 82.7 on the benchmark this quarter, up from last. " * 8
    vendor = _src(st, "https://acme.com/benchmarks", text, authority=80, kind="docs")
    lab = _src(st, "https://mlperf.org/results", text, authority=80, kind="docs")
    _claim(st, 1, "82.7", vendor.n)
    _claim(st, 1, "82.7", lab.n)
    dr._resolve(st)
    alone, _ = _resolved_from("acme.com")
    assert st.resolutions[1].confidence > alone.confidence


def test_the_self_published_bonus_is_reduced_not_a_penalty():
    """Half of one term, and only that term: the resolution keeps its status,
    its support and its independence count. A penalty would push a first-hand
    source below a blog."""
    vendor_res, _ = _resolved_from("acme.com")
    assert vendor_res.status == dr.STATUS_CURRENT
    assert vendor_res.support and vendor_res.independent == 1
    assert vendor_res.stated_verbatim is True
    assert vendor_res.confidence > 0.05


# ---------------------------------------------------------------------------
# R13 — admission: per-user fairness under a process-wide ceiling
# ---------------------------------------------------------------------------


def _admission_settings(monkeypatch, *, ceiling=2, per_user=1, wait_s=0.0, budget=600.0):
    monkeypatch.setattr(settings, "deep_research_max_concurrent", ceiling)
    monkeypatch.setattr(settings, "deep_research_max_per_user", per_user)
    monkeypatch.setattr(settings, "deep_research_queue_wait_s", wait_s)
    monkeypatch.setattr(settings, "deep_research_timeout_s", budget)


def _parking_collect(parked, release):
    """A gather that holds its run inside the loop until it is let go, so a
    second request meets a run that is genuinely IN FLIGHT rather than one the
    fakes have already finished."""

    async def collect(queries, effort="medium", emit=None, categories="", **kw):
        parked.set()
        await release.wait()
        return _results(4)

    return collect


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def test_two_people_both_get_their_research_run(monkeypatch):
    """The acceptance case, and the whole point of R13: one user's run must
    not fail another user's request. Overlap is proven by counting runs that
    are inside the gather AT THE SAME TIME, not by both eventually finishing —
    a serialised pair would also both finish."""
    _wire(monkeypatch)
    _admission_settings(monkeypatch, ceiling=2, per_user=1)
    inflight = {"now": 0, "peak": 0}

    async def scenario():
        both = asyncio.Event()

        async def counting_collect(queries, effort="medium", emit=None, categories="", **kw):
            inflight["now"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["now"])
            if inflight["now"] >= 2:
                both.set()
            try:
                await asyncio.wait_for(both.wait(), 5)
            except asyncio.TimeoutError:  # serialised: let the test report it
                pass
            inflight["now"] -= 1
            return _results(4)

        monkeypatch.setattr(dr, "_collect_results", counting_collect)
        ev_a, emit_a = _emitter()
        ev_b, emit_b = _emitter()
        outs = await asyncio.gather(
            dr.run_deep_research_engine("q", [], emit_a, conversation_id="c1", user_id=1),
            dr.run_deep_research_engine("q", [], emit_b, conversation_id="c2", user_id=2),
        )
        return outs, ev_a, ev_b

    (out_a, out_b), ev_a, ev_b = asyncio.run(scenario())
    assert inflight["peak"] == 2, "the two users' runs never overlapped"
    for out, events in ((out_a, ev_a), (out_b, ev_b)):
        assert "Report" in out, out
        assert "already running" not in out and "still going" not in out
        assert _run_meta(events)["sources_found"] > 0


def test_one_person_cannot_take_the_second_slot_as_well(monkeypatch):
    """Fairness. The ceiling is 2 and NOTHING else is running, so the machine
    has room — but the allowance is per person, and a second tab is still a
    second tab."""
    _wire(monkeypatch)
    _admission_settings(monkeypatch, ceiling=2, per_user=1)

    async def scenario():
        parked, release = asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(dr, "_collect_results", _parking_collect(parked, release))
        ev_a, emit_a = _emitter()
        ev_b, emit_b = _emitter()
        first = asyncio.create_task(
            dr.run_deep_research_engine("q", [], emit_a, conversation_id="c1", user_id=7)
        )
        await asyncio.wait_for(parked.wait(), 5)
        out_b = await dr.run_deep_research_engine(
            "q", [], emit_b, conversation_id="c2", user_id=7
        )
        release.set()
        return await first, out_b, ev_b

    out_a, out_b, ev_b = asyncio.run(scenario())
    assert "Report" in out_a, "the run that was already going was disturbed"
    assert "Your own research run is still going" in out_b
    assert "1 run at a time per person" in out_b
    # Truthful, not a bare no: the budget that bounds the other run is quoted.
    assert "about 10 minutes" in out_b
    assert "Web Search" in out_b
    # The refusal is still a well-formed answer on the wire.
    assert [p for k, p in ev_b if k == "meta"][-1] == {
        "route": "deep_research", "sources": []
    }


def test_the_process_ceiling_holds_against_a_third_person(monkeypatch):
    """Per-user fairness must not become a way to buy the whole box by
    bringing more accounts: two runs from two users fill the machine, and the
    third is told so."""
    _wire(monkeypatch)
    _admission_settings(monkeypatch, ceiling=2, per_user=1)

    async def scenario():
        parked, release = asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(dr, "_collect_results", _parking_collect(parked, release))
        running = [
            asyncio.create_task(
                dr.run_deep_research_engine(
                    "q", [], _emitter()[1], conversation_id=f"c{uid}", user_id=uid
                )
            )
            for uid in (1, 2)
        ]
        assert await _until(lambda: dr._admission().total == 2), "both runs did not start"
        ev_c, emit_c = _emitter()
        out_c = await dr.run_deep_research_engine(
            "q", [], emit_c, conversation_id="c3", user_id=3
        )
        release.set()
        await asyncio.gather(*running)
        return out_c

    out_c = asyncio.run(scenario())
    assert "already running 2 research runs" in out_c
    assert "The earliest finishes in about 10 minutes" in out_c


def test_a_queued_request_runs_when_a_slot_frees(monkeypatch):
    """Queue, do not refuse. With room for one run, the second request waits
    and then RUNS — the behaviour the global lock could not produce."""
    _wire(monkeypatch)
    _admission_settings(monkeypatch, ceiling=1, per_user=1, wait_s=10.0)

    async def scenario():
        parked, release = asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(dr, "_collect_results", _parking_collect(parked, release))
        ev_a, emit_a = _emitter()
        ev_b, emit_b = _emitter()
        first = asyncio.create_task(
            dr.run_deep_research_engine("q", [], emit_a, conversation_id="c1", user_id=1)
        )
        await asyncio.wait_for(parked.wait(), 5)
        second = asyncio.create_task(
            dr.run_deep_research_engine("q", [], emit_b, conversation_id="c2", user_id=2)
        )
        assert await _until(lambda: bool(dr._admission().waiters)), "it never queued"
        release.set()
        return await first, await asyncio.wait_for(second, 10), ev_b

    out_a, out_b, ev_b = asyncio.run(scenario())
    assert "Report" in out_a and "Report" in out_b, "the queued request was starved"
    statuses = [p["text"] for k, p in ev_b if k == "status"]
    assert any(t.startswith("Deep Research is busy") for t in statuses)
    assert any("slot came free" in t for t in statuses)


def test_a_queued_request_that_is_abandoned_gives_its_slot_back(monkeypatch):
    """A client that goes away mid-queue must not leave a slot counted against
    everyone else — the failure that turns a fixed lock into a stuck one."""
    # Wired even though the run must never start: if admission ever lets it
    # through, the test must fail on its assertion, not on a live LLM call.
    _wire(monkeypatch)
    _admission_settings(monkeypatch, ceiling=1, per_user=1, wait_s=30.0)

    async def scenario():
        adm = dr._admission()
        held = adm.try_admit("user:1")  # stands in for a run already in flight
        _events, emit = _emitter()
        waiting = asyncio.create_task(
            dr.run_deep_research_engine("q", [], emit, conversation_id="c2", user_id=2)
        )
        assert await _until(lambda: bool(adm.waiters)), "it never queued"
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        adm.release("user:1", held)
        return adm.total, adm.waiters, adm.try_admit("user:3")

    total, waiters, fresh = asyncio.run(scenario())
    assert total == 0 and waiters == []
    assert fresh is not None, "the abandoned request kept the slot"


def test_a_queued_request_whose_stream_dies_leaks_nothing(monkeypatch):
    """The status line announcing the wait is an AWAIT, so a slot can be handed
    over while it is in flight. If that emit then fails — the client hung up —
    the slot has to go back; a leaked one is only recovered by a restart."""
    _wire(monkeypatch)
    _admission_settings(monkeypatch, ceiling=1, per_user=1, wait_s=30.0)

    async def scenario():
        adm = dr._admission()
        held = adm.try_admit("user:1")

        async def hostile_stream(kind, payload):
            # The slot frees WHILE the status line is in flight, so this
            # request is handed one, and only then does the stream fail. That
            # is the ordering that leaks a slot if nothing gives it back.
            adm.release("user:1", held)
            raise RuntimeError("the client hung up")

        with pytest.raises(RuntimeError):
            await dr.run_deep_research_engine(
                "q", [], hostile_stream, conversation_id="c2", user_id=2
            )
        assert adm.waiters == [], "the dead request stayed in the queue"
        return adm.total, adm.try_admit("user:3")

    total, fresh = asyncio.run(scenario())
    assert total == 0
    assert fresh is not None, "a slot was lost to the dead request"


def test_a_run_that_raises_still_releases_its_slot(monkeypatch):
    """`_run` is wrapped in try/finally for this: an engine crash used to
    release the lock the same way, and losing that would wedge the whole
    process until a restart."""
    _admission_settings(monkeypatch, ceiling=1, per_user=1)

    async def boom(*a, **k):
        raise RuntimeError("the engine fell over")

    monkeypatch.setattr(dr, "_run", boom)

    async def scenario():
        adm = dr._admission()
        _events, emit = _emitter()
        with pytest.raises(RuntimeError):
            await dr.run_deep_research_engine("q", [], emit, conversation_id="c1", user_id=1)
        return adm.total, adm.try_admit("user:2")

    total, fresh = asyncio.run(scenario())
    assert total == 0
    assert fresh is not None, "the crashed run kept its slot"


def test_the_refusal_never_promises_a_time_the_budget_cannot_keep(monkeypatch):
    """`frees_in_s` is only truthful because R8 made the wall clock a real
    bound. With no budget configured there is no honest number, so the refusal
    says nothing about time rather than inventing one."""

    async def scenario():
        adm = dr._admission()
        adm.try_admit("user:1")
        adm.try_admit("user:2")
        return adm

    _admission_settings(monkeypatch, ceiling=2, per_user=1, budget=0.0)
    adm = asyncio.run(scenario())
    text = dr._refusal_text(adm, "user:3", waited_s=0.0)
    assert "already running 2 research runs" in text
    # No promise about when, and no dangling "try again THEN" pointing at one.
    assert "finishes in" not in text, text
    assert "Try again once it finishes" in text, text
    assert "Web Search" in text


def test_a_wait_that_actually_happened_is_reported_as_one(monkeypatch):
    _admission_settings(monkeypatch, ceiling=1, per_user=1, budget=600.0)

    async def scenario():
        adm = dr._admission()
        adm.try_admit("user:1")
        return adm

    adm = asyncio.run(scenario())
    assert "I waited 45s for a slot" in dr._refusal_text(adm, "user:2", waited_s=45.0)
    assert "waited" not in dr._refusal_text(adm, "user:2", waited_s=0.0)


def test_a_per_user_allowance_can_never_exceed_the_whole_machine(monkeypatch):
    """A misconfiguration must degrade to the ceiling, not through it."""
    monkeypatch.setattr(settings, "deep_research_max_concurrent", 1)
    monkeypatch.setattr(settings, "deep_research_max_per_user", 9)
    assert dr._Admission.per_user() == 1
    monkeypatch.setattr(settings, "deep_research_max_concurrent", 0)
    assert dr._Admission.ceiling() == 1, "zero must not mean unlimited"


def test_concurrency_did_not_widen_the_model_budget():
    """The safety argument for allowing a second run is that `_LLM_SEM` is
    process-wide and SHARED: two runs interleave inside two generation slots
    rather than adding a third for interactive chat to queue behind. If this
    is ever raised alongside the run ceiling, that argument is gone."""
    assert dr._LLM_SEM._value == 2


# ---------------------------------------------------------------------------
# R14 — two runs at once: nothing crosses between them
#
# R13 made a second concurrent run POSSIBLE; it did not prove that the second
# run is isolated from the first. The admission tests count slots and assert
# that both requests finish, which a pair of runs quietly sharing a citation
# registry would also do. These pin the isolation itself: the sources, the
# claims, the shared-store rows and the SSE events each run produces belong to
# that run and to no other.
# ---------------------------------------------------------------------------


#: The two runs ask questions carrying their own tag, so every stub below can
#: tell which run is asking and hand back that run's data. Anything that
#: crosses then shows up as the OTHER tag in the assertions, instead of having
#: to be inferred from a count that a shared registry would also satisfy.
_TAG_VALUE = {"alpha": "alpha-42", "beta": "beta-77"}


def _tag_of(blob: str) -> str:
    """Which run this call belongs to. Deliberately strict: a stub that cannot
    tell must fail the test rather than pick one and hide a leak."""
    hits = [t for t in _TAG_VALUE if t in blob]
    assert len(hits) == 1, f"a stub could not tell the two runs apart: {hits}"
    return hits[0]


def _tagged_wire(monkeypatch, claim_rows, closed_rows, run_ids):
    """`_wire`, but every fake answers according to WHICH run is asking."""

    async def fake_json_completion(messages, **kw):
        tag = _tag_of(json.dumps(messages))
        name = kw.get("schema_name")
        if name == "research_plan":
            return json.dumps(
                {
                    "subquestions": [f"what is the {tag} value"],
                    "queries": [f"{tag}-q"],
                    "entities": [tag],
                }
            )
        if name == "research_claims":
            return json.dumps(
                {
                    "claims": [
                        {
                            "subquestion": 1,
                            "claim": f"The {tag} value is {_TAG_VALUE[tag]}.",
                            "value": _TAG_VALUE[tag],
                            "source": 1,
                            "as_of": "2026-01-01",
                            "status": "current",
                        }
                    ]
                }
            )
        if name == "research_verify":
            return json.dumps({"verdicts": []})
        return json.dumps({"sufficient": True, "missing": [], "followup_queries": []})

    async def fake_rerank(message, res, target):
        return res

    async def fake_fetch(res, message=""):
        tag = _tag_of(message)
        return [
            _Source(
                n=i,
                title=f"{tag} doc {i}",
                url=r.url,
                text=(
                    f"A page about the {tag} programme. "
                    f"The {tag} value is {_TAG_VALUE[tag]}. " * 12
                ),
                authority=40,
                source_type="news",
                fetched_at=_now(),
            )
            for i, r in enumerate(res, 1)
        ]

    async def fake_stream(messages, **kw):
        tag = _tag_of(json.dumps(messages))
        for piece in (f"Report on {tag} [1].", f" The {tag} value holds."):
            yield ("token", piece)

    def fake_create(conversation_id, user_id, question, research_id):
        run_ids[research_id] = 100 + len(run_ids)
        return run_ids[research_id]

    def fake_finish(run_id, status, iterations, queries, sources, cited, report,
                    sources_meta=None, detail=""):
        closed_rows.append(
            {
                "run_id": run_id,
                "status": status,
                "report": report,
                "sources_meta": list(sources_meta or []),
            }
        )

    monkeypatch.setattr(dr.llm, "json_completion", fake_json_completion)
    monkeypatch.setattr(dr.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(dr, "_rerank_results", fake_rerank)
    monkeypatch.setattr(dr, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(dr, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(dr.db, "create_research_run", fake_create)
    monkeypatch.setattr(dr.db, "finish_research_run", fake_finish)
    # `_persist_claims` is NOT stubbed here: the shared claim store is exactly
    # the place a crossed citation would become another user's grounding.
    monkeypatch.setattr(dr.db, "get_web_pages", lambda keys: [])
    monkeypatch.setattr(
        dr.db, "insert_web_claims", lambda rows: (claim_rows.extend(rows), len(rows))[1]
    )
    monkeypatch.setattr(settings, "deep_research_min_sources", 1)


def test_two_concurrent_runs_never_cross_their_sources_claims_or_events(monkeypatch):
    """The isolation proof. Two runs are held inside the SAME round until both
    are there, so the interleaving is real rather than incidental, and every
    artefact each one produces is then checked to carry only its own tag."""
    _admission_settings(monkeypatch, ceiling=2, per_user=1)
    claim_rows, closed_rows, run_ids = [], [], {}
    _tagged_wire(monkeypatch, claim_rows, closed_rows, run_ids)

    overlapped = {"ok": False}

    async def scenario():
        both = asyncio.Event()
        arrived = {"n": 0}

        async def overlapping_collect(queries, effort="medium", emit=None, categories="", **kw):
            tag = _tag_of(" ".join(queries))
            arrived["n"] += 1
            if arrived["n"] >= 2:
                both.set()
            # Park inside the round so the two runs are genuinely in flight at
            # the same time; a serialised pair would also both finish.
            try:
                await asyncio.wait_for(both.wait(), 5)
                overlapped["ok"] = True
            except asyncio.TimeoutError:  # serialised: let the test report it
                pass
            return _results(3, host=f"{tag}.test")

        monkeypatch.setattr(dr, "_collect_results", overlapping_collect)
        ev_a, emit_a = _emitter()
        ev_b, emit_b = _emitter()
        outs = await asyncio.gather(
            dr.run_deep_research_engine(
                "the alpha programme, in detail", [], emit_a,
                conversation_id="conv-alpha", user_id=1,
            ),
            dr.run_deep_research_engine(
                "the beta programme, in detail", [], emit_b,
                conversation_id="conv-beta", user_id=2,
            ),
        )
        return outs, ev_a, ev_b

    (out_a, out_b), ev_a, ev_b = asyncio.run(scenario())
    assert overlapped["ok"], "the two runs never overlapped — nothing was proven"

    for tag, out, events in (("alpha", out_a, ev_a), ("beta", out_b, ev_b)):
        other = "beta" if tag == "alpha" else "alpha"
        meta = _run_meta(events)
        sources = [p for k, p in events if k == "meta"][-1]["sources"]

        # The citation registry: every source this run cites is its own, and
        # the numbering starts at 1 for each run rather than continuing the
        # other's.
        assert sources, f"{tag} cited nothing"
        assert [s["n"] for s in sources] == list(range(1, len(sources) + 1))
        assert all(f"{tag}.test" in s["url"] for s in sources), sources
        assert not any(f"{other}.test" in s["url"] for s in sources), sources

        # The report and the whole event stream, top to bottom.
        assert tag in out and other not in out, out
        blob = json.dumps(events)
        assert other not in blob, f"{other} leaked onto the {tag} stream"
        # And every frame on it is a type the SSE layer will actually send:
        # an unregistered event is dropped, which under concurrency reads as
        # "the other run stole my progress".
        assert {k for k, _ in events} <= set(ALL_EVENTS), {k for k, _ in events}

        # Step ids are per-run, so the two timelines do not overwrite each
        # other in a UI that keys steps by id.
        step_ids = [p["id"] for k, p in events if k == "step" and p["status"] == "running"]
        assert step_ids == sorted(set(step_ids)) and step_ids[0] == 1

        # The run record and its resolutions.
        assert meta["research_id"] in run_ids
        assert meta["resolutions"][0]["value"] == _TAG_VALUE[tag]

    # Two distinct research ids, two distinct rows, each closed once.
    assert len(run_ids) == 2
    assert sorted(r["run_id"] for r in closed_rows) == sorted(run_ids.values())
    assert {r["status"] for r in closed_rows} == {"done"}

    # The SHARED claim store — the one place where a crossed source number
    # would become the next user's grounding.
    assert len(claim_rows) == 2, claim_rows
    for row in claim_rows:
        tag = _tag_of(row["value"])
        assert row["origin_conversation_id"] == f"conv-{tag}"
        assert f"{tag}.test" in row["url"]
        assert _TAG_VALUE[tag] in row["quote"]
        assert row["research_id"] in run_ids


def test_the_engine_keeps_no_per_run_state_at_module_level():
    """The safety argument for a second concurrent run is written down as a
    claim in `_Admission`: 'everything at module level here is a constant, a
    compiled regex or a semaphore; all run state lives in the per-run
    ResearchState'. A dict or a list added at module scope later would make
    two runs share it silently, and no other test in this suite would notice —
    the concurrency test above only sees the fakes it wired.

    The three names allowed here are shared ON PURPOSE and are each proven
    elsewhere: `_LLM_SEM` (the process-wide generation budget), and the
    admission bookkeeping with the loop it belongs to.
    """
    import inspect as _inspect

    allowed = {"_LLM_SEM", "_ADMISSION", "_ADMISSION_LOOP"}
    shared = sorted(
        name
        for name, value in vars(dr).items()
        if not name.startswith("__")
        and name not in allowed
        and not _inspect.ismodule(value)
        and not _inspect.isclass(value)
        and not _inspect.isroutine(value)
        and isinstance(value, (dict, list, set, bytearray))
    )
    assert shared == [], (
        "these module-level containers are shared by every concurrent research "
        f"run: {shared}. Per-run state belongs on ResearchState."
    )


# ---------------------------------------------------------------------------
# R15 — cancellation: the work stops, the row closes, the slot comes back
#
# R4 proved that a cancelled run KEEPS what it had already read. It proved it
# by raising CancelledError from inside the report stream, which is not the
# shape production produces: /chat/stop cancels the whole worker TASK
# (main.py `gen.task.cancel()`), and an externally cancelled task behaves
# differently at every `await` in the engine — including the shielded write
# that closes the row. These drive the real shape, and check the three things
# a cancelled run must not leave behind: work still running, writes still
# landing, and a research slot nobody can use.
# ---------------------------------------------------------------------------


def _counting_wire(monkeypatch, calls, closed):
    """`_wire`, with a counter on every stage that costs the box something."""
    _wire(monkeypatch)

    async def fetch(res, message=""):
        calls["fetch"] += 1
        return [
            _Source(n=i, title=r.title, url=r.url, text=f"body {i} of a page. " * 30,
                    authority=40, source_type="news", fetched_at=_now())
            for i, r in enumerate(res, 1)
        ]

    async def json_completion(messages, **kw):
        calls[str(kw.get("schema_name") or "json")] += 1
        name = kw.get("schema_name")
        if name == "research_plan":
            return json.dumps({"subquestions": ["a"], "queries": ["q1"], "entities": []})
        if name == "research_claims":
            return json.dumps({"claims": []})
        if name == "research_verify":
            return json.dumps({"verdicts": []})
        return json.dumps({"sufficient": True, "missing": [], "followup_queries": []})

    async def stream(messages, **kw):
        calls["report"] += 1
        yield ("token", "Report [1].")

    async def persist_claims(state):
        calls["persist_claims"] += 1

    async def queue_crawls(state):
        calls["crawl"] += 1

    def finish(run_id, status, iterations, queries, sources, cited, report,
               sources_meta=None, detail=""):
        closed.append({"run_id": run_id, "status": status, "report": report,
                       "sources": sources, "detail": detail})

    monkeypatch.setattr(dr, "_fetch_sources", fetch)
    monkeypatch.setattr(dr.llm, "json_completion", json_completion)
    monkeypatch.setattr(dr.llm, "stream_chat_events", stream)
    monkeypatch.setattr(dr, "_persist_claims", persist_claims)
    monkeypatch.setattr(dr, "_queue_primary_crawls", queue_crawls)
    monkeypatch.setattr(dr.db, "create_research_run", lambda *a, **k: 4242)
    monkeypatch.setattr(dr.db, "finish_research_run", finish)


def test_an_externally_cancelled_run_stops_the_work_and_closes_its_row(monkeypatch):
    """The /chat/stop shape. The run is held inside its first search; the task
    is then cancelled the way main.py cancels it. Nothing the run had not yet
    started may start afterwards — a fetch that keeps going is a page this box
    pays for after the user said stop — and the row must read 'cancelled'
    rather than sit at 'running' until the next restart reconciles it."""
    _admission_settings(monkeypatch, ceiling=2, per_user=1)
    calls, closed = collections.Counter(), []
    _counting_wire(monkeypatch, calls, closed)

    async def scenario():
        parked, release = asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(dr, "_collect_results", _parking_collect(parked, release))
        events, emit = _emitter()
        task = asyncio.create_task(
            dr.run_deep_research_engine("q", [], emit, conversation_id="c1", user_id=1)
        )
        await asyncio.wait_for(parked.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Anything the cancellation failed to stop would land in this window.
        release.set()
        await asyncio.sleep(0.05)
        return events, dr._admission(), asyncio.all_tasks()

    events, adm, tasks = asyncio.run(scenario())

    # 1. The work stopped where it was: the round never reached its fetch, no
    #    claim extraction, no audit, no verification, no report.
    assert calls["fetch"] == 0, "the run fetched pages after it was cancelled"
    assert calls["research_claims"] == 0 and calls["research_gaps"] == 0
    assert calls["research_verify"] == 0 and calls["report"] == 0

    # 2. No writes after cancel: the shared claim store and the background
    #    crawl queue are both on the SUCCESS path and must stay there.
    assert calls["persist_claims"] == 0, "a cancelled run wrote to the shared claim store"
    assert calls["crawl"] == 0, "a cancelled run queued background crawls"

    # 3. The row is closed, not dangling.
    assert [c["status"] for c in closed] == ["cancelled"], closed
    assert closed[0]["run_id"] == 4242
    assert closed[0]["detail"] == "cancelled mid-run"

    # 4. Nothing of this run outlived it, and the slot came back.
    assert adm.total == 0 and adm.waiters == []
    assert adm.try_admit("user:9") is not None, "the cancelled run kept its slot"
    assert [t for t in tasks if not t.done() and "run_deep_research" in repr(t)] == []


def test_a_cancelled_run_keeps_the_evidence_it_had_already_paid_for(monkeypatch):
    """The other half of R4, at the real cancellation shape: a run stopped
    AFTER a round finished must still record the sources it read. Cancelling
    at minute 9 used to store 0 sources and no report."""
    _admission_settings(monkeypatch, ceiling=2, per_user=1)
    calls, closed = collections.Counter(), []
    _counting_wire(monkeypatch, calls, closed)

    async def scenario():
        parked, release = asyncio.Event(), asyncio.Event()

        async def park_in_the_audit(messages, **kw):
            # Round 1 is complete — sources fetched and registered — and the
            # run is now in the evidence audit. This is the minute-9 shape.
            if kw.get("schema_name") == "research_gaps":
                parked.set()
                await release.wait()
            calls[str(kw.get("schema_name") or "json")] += 1
            if kw.get("schema_name") == "research_plan":
                return json.dumps({"subquestions": ["a"], "queries": ["q1"], "entities": []})
            return json.dumps({"claims": []})

        monkeypatch.setattr(dr.llm, "json_completion", park_in_the_audit)
        events, emit = _emitter()
        task = asyncio.create_task(
            dr.run_deep_research_engine("q", [], emit, conversation_id="c1", user_id=1)
        )
        await asyncio.wait_for(parked.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert [c["status"] for c in closed] == ["cancelled"], closed
    assert closed[0]["sources"] > 0, "the run recorded 0 of the sources it read"
    # Still nothing written to the shared store on a cancelled run.
    assert calls["persist_claims"] == 0 and calls["crawl"] == 0


def test_a_run_cancelled_while_its_row_is_being_written_still_closes_it(monkeypatch):
    """The dangling-row window, and it is not theoretical.

    `db.run_in_thread` is anyio's `to_thread.run_sync`: cancelling the await
    raises CancelledError IMMEDIATELY and throws the worker thread's result
    away, while the thread runs the INSERT to completion. So a Stop pressed in
    the first milliseconds of a run — the common case, because that is exactly
    when someone notices they picked the wrong mode — committed a
    `research_runs` row at 'running' whose id nobody held any more. Nothing
    closes it: the engine's cancellation handler needs `run_row`, and
    `db.close_interrupted_research_runs` only runs at process START. Until the
    next restart the admin research analytics count a run that stopped minutes
    ago as still going, and `frees_in_s` quotes its budget to the next person
    who is refused a slot.
    """
    _admission_settings(monkeypatch, ceiling=2, per_user=1)
    _wire(monkeypatch)
    closed, inserting, inserted = [], threading.Event(), threading.Event()

    def slow_create(conversation_id, user_id, question, research_id):
        inserting.set()
        time.sleep(0.3)  # the INSERT, in anyio's worker thread
        inserted.set()
        return 4242

    monkeypatch.setattr(dr.db, "create_research_run", slow_create)
    monkeypatch.setattr(
        dr.db,
        "finish_research_run",
        lambda run_id, status, *a, **k: closed.append((run_id, status)),
    )

    async def scenario():
        events, emit = _emitter()
        task = asyncio.create_task(
            dr.run_deep_research_engine("q", [], emit, conversation_id="c1", user_id=1)
        )
        assert await _until(inserting.is_set), "the row insert never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The worker thread is still committing the row the run can no longer
        # see. Give it the moment it needs before judging what was left behind.
        assert await _until(inserted.is_set), "the INSERT never finished"
        await asyncio.sleep(0.1)
        return dr._admission()

    adm = asyncio.run(scenario())
    assert closed == [(4242, "cancelled")], (
        "the research_runs row was committed and then abandoned at 'running' — "
        "only a process restart will ever close it"
    )
    assert adm.total == 0, "the abandoned run also kept its slot"


def test_the_startup_path_really_closes_runs_a_restart_interrupted(monkeypatch):
    """`db.close_interrupted_research_runs` has its own unit test, but a
    reconciliation that nothing CALLS is a comment. This drives the real
    FastAPI lifespan — its only caller — against the test database and reads
    back the row it leaves behind."""
    from app import db as real_db, main as app_main, web_worker

    async def _noop_stop():
        return None

    monkeypatch.setattr(web_worker, "start", lambda: None)
    monkeypatch.setattr(web_worker, "stop", _noop_stop)
    # The pool is shared with the rest of the session; closing it here would
    # only make the NEXT test pay to reopen it.
    monkeypatch.setattr(real_db, "close_pool", lambda: None)

    run_id = real_db.create_research_run("c-interrupted", None, "a question", "res-1")
    assert real_db.get_research_runs("c-interrupted")[0]["status"] == "running"

    async def drive_startup_and_shutdown():
        async with app_main.lifespan(app_main.app):
            pass

    asyncio.run(drive_startup_and_shutdown())

    row = real_db.get_research_runs("c-interrupted")[0]
    assert row["id"] == run_id
    assert row["status"] == "failed", "the interrupted run is still claiming to run"
    assert row["detail"] == "interrupted by a restart"
    assert row["finished_at"] is not None


def test_the_only_work_that_outlives_a_cancelled_run_is_the_write_it_already_paid_for(monkeypatch):
    """What a cancelled run leaves running, pinned deliberately.

    A finished round fires ONE write-behind (`_spawn(_persist_and_index(...))`)
    so the pages it just read are findable by the next question. That task is
    intentionally not cancelled with the run — the fetches are already paid
    for, and throwing the corpus row away would make the next asker re-fetch
    the same pages — but "intentional" has to be bounded and it has to be
    checked. This pins both halves: exactly one write survives (the round that
    completed), no NEW round's write is started after the cancel, and
    `search._BACKGROUND_TASKS` drains rather than accumulating a task per
    cancelled run.
    """
    from app.engines import search as search_mod

    _admission_settings(monkeypatch, ceiling=2, per_user=1)
    calls, closed = collections.Counter(), []
    _counting_wire(monkeypatch, calls, closed)
    # The REAL fire-and-forget helper, which `_wire` replaces with a close():
    # the whole question here is what its tasks do when the run goes away.
    monkeypatch.setattr(dr, "_spawn", search_mod._spawn)
    writes, let_the_write_finish = [], asyncio.Event()

    async def slow_write_behind(question, queries, results, effort, user_id, conversation_id):
        await let_the_write_finish.wait()
        writes.append({"question": question, "queries": list(queries)})

    monkeypatch.setattr(dr, "_persist_and_index", slow_write_behind)

    async def scenario():
        parked, release = asyncio.Event(), asyncio.Event()

        async def park_in_the_audit(messages, **kw):
            if kw.get("schema_name") == "research_gaps":
                parked.set()
                await release.wait()
            if kw.get("schema_name") == "research_plan":
                return json.dumps({"subquestions": ["a"], "queries": ["q1"], "entities": []})
            return json.dumps({"claims": []})

        monkeypatch.setattr(dr.llm, "json_completion", park_in_the_audit)
        task = asyncio.create_task(
            dr.run_deep_research_engine("q", [], _emitter()[1], conversation_id="c1", user_id=1)
        )
        await asyncio.wait_for(parked.wait(), 5)
        # The round is over and its write-behind is in flight but unfinished.
        assert writes == [] and search_mod._BACKGROUND_TASKS
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.05)
        # The run is gone; the corpus write is still there, not cancelled.
        assert writes == [], "the write finished early — the test proves nothing"
        pending = {t for t in search_mod._BACKGROUND_TASKS if not t.done()}
        assert len(pending) == 1, pending
        let_the_write_finish.set()
        await asyncio.gather(*pending)
        await asyncio.sleep(0)
        return {t for t in search_mod._BACKGROUND_TASKS if not t.done()}

    leftover = asyncio.run(scenario())

    assert len(writes) == 1, f"a cancelled run kept writing rounds to the corpus: {writes}"
    assert writes[0]["queries"] == ["q1"]
    assert leftover == set(), "the background task set grows a task per cancelled run"
    assert [c["status"] for c in closed] == ["cancelled"]
