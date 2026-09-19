"""Deep Research always writes its report, or says plainly that it could not (B3).

WHAT THE AUDIT SAW. Two ten-minute runs whose stored report was the 94-character
note "[the run reached its time budget and the report stops here — the sources
above are complete]" and nothing else, stored with status 'done'.

THE MECHANISM, read at 4810da0:

* the report reserve is min(240 s, 0.25 x DEEP_RESEARCH_TIMEOUT_S) — 150 s at
  the 600 s default — and a run that spent its gathering budget gets exactly
  that;
* the report call ran at the turn's effort, so at Think the model REASONED
  first, over a prompt of up to 36 sources, and a reasoning delta is not report
  text;
* `asyncio.wait_for(_stream_report(), report_budget_s())` cut the stream at
  150 s whether or not it was still producing, and a stream cut before its
  first answer token left nothing but the note — which was then stored as a
  finished report.

WHAT THESE PIN. The report is written with thinking OFF (the answer plan on the
call says so). The deadline measures IDLE time — a stream still producing
report text is not cut at its allowance — under a hard ceiling. A zero-token
deadline is retried once, thinking off, inside the floor. And a run is never
stored as 'done' with zero report words: it says what happened instead.

Everything is offline: no vLLM, no SearXNG, no network, no database.
"""
import asyncio
import json
import re
import time
from datetime import datetime, timezone

import pytest

from app import rerank
from app.config import settings
from app.engines import deep_research as dr
from app.engines.search import _Source
from app.search.base import SearchResult
from tests.test_llm_public_stream_kwargs import world  # noqa: F401 — the fake-engine fixture

#: The real model client, before any test stubs it.
_REAL_STREAM = dr.llm.stream_chat_events
_REAL_FINISH_REASON = dr.llm.get_finish_reason

#: The note a budget cut appends — the whole stored report in the audit's runs.
_NOTE = "[the run reached its time budget and the report stops here"


def _now():
    return datetime.now(timezone.utc)


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


def _plan_is_thinking_off(kw) -> bool:
    plan = kw.get("answer_plan")
    return plan is not None and getattr(plan, "enable_thinking", None) is False


def _words(text: str) -> int:
    return len(re.findall(r"[^\W\d_]+", text or ""))


def _wire(monkeypatch, stream):
    """Every outside dependency of the loop stubbed; `stream` is the report
    model. Returns (calls, closed): the kwargs of every report call, and the
    status/report the run record was closed with."""

    async def fake_json_completion(messages, **kw):
        name = kw.get("schema_name")
        if name == "research_plan":
            return json.dumps({"subquestions": ["a"], "queries": ["q1"], "entities": []})
        if name == "research_claims":
            return json.dumps({"claims": []})
        if name == "research_verify":
            return json.dumps({"verdicts": []})
        return json.dumps({"sufficient": True, "missing": [], "followup_queries": []})

    async def fake_collect(queries, effort="medium", emit=None, categories="", **kw):
        return _results(4)

    async def no_reranker(query, documents, **kw):
        raise rerank.RerankUnavailable("offline test")

    async def fake_fetch(res, message=""):
        return [
            _Source(n=i, title=r.title, url=r.url, text=f"body {i} of a page. " * 30,
                    authority=40, source_type="news", fetched_at=_now())
            for i, r in enumerate(res, 1)
        ]

    calls = []
    closed = {}

    async def recording_stream(messages, **kw):
        calls.append(kw)
        async for item in stream(len(calls), kw):
            yield item

    def fake_finish(run_id, status, iterations, queries, sources, cited, report,
                    sources_meta=None, detail=""):
        closed.update(status=status, report=report, detail=detail, sources=sources)

    monkeypatch.setattr(dr.llm, "get_finish_reason", lambda: None)
    monkeypatch.setattr(dr.llm, "json_completion", fake_json_completion)
    monkeypatch.setattr(dr.llm, "stream_chat_events", recording_stream)
    monkeypatch.setattr(dr, "_collect_results", fake_collect)
    monkeypatch.setattr(rerank, "score", no_reranker)
    monkeypatch.setattr(dr, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(dr, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(dr.db, "create_research_run", lambda *a, **k: 1)
    monkeypatch.setattr(dr.db, "finish_research_run", fake_finish)
    monkeypatch.setattr(dr, "_persist_claims", lambda state: asyncio.sleep(0))
    monkeypatch.setattr(settings, "deep_research_min_sources", 1)
    monkeypatch.setattr(settings, "deep_research_verify", False)
    monkeypatch.setattr(settings, "deep_research_background_crawl", False)
    # A one-second run: the report's allowance is then about a second, and the
    # floor a retry gets is 0.3 s. Every case below runs in a few seconds.
    monkeypatch.setattr(settings, "deep_research_timeout_s", 1.0)
    monkeypatch.setattr(dr, "_REPORT_FLOOR_S", 0.3)
    return calls, closed


def _run(monkeypatch):
    events, emit = _emitter()
    started = time.monotonic()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    return out, events, time.monotonic() - started


def _run_meta(events):
    metas = [p for k, p in events if k == "meta" and "research_run" in p]
    return metas[-1]["research_run"] if metas else {}


REPORT = "The evidence shows the answer plainly [1]. Two sources agree on it [2]."


async def _reasons_unless_told_not_to(n, kw):
    """The audit's run: a model that thinks first. Told not to, it writes."""
    if _plan_is_thinking_off(kw):
        for piece in (REPORT[i:i + 8] for i in range(0, len(REPORT), 8)):
            yield ("token", piece)
        return
    for _ in range(200):  # ten seconds of reasoning, far past the allowance
        yield ("reasoning", "considering the sources ")
        await asyncio.sleep(0.05)


async def _only_ever_reasons(n, kw):
    """A runtime that ignores the switch: reasoning and nothing else."""
    for _ in range(200):
        yield ("reasoning", "considering the sources ")
        await asyncio.sleep(0.05)


def test_the_report_is_written_with_thinking_off(monkeypatch):
    """The acceptance case. At 4810da0 the call carried no answer plan, the
    fake reasoned past the allowance, and the stored report was the note."""
    calls, closed = _wire(monkeypatch, _reasons_unless_told_not_to)
    out, events, elapsed = _run(monkeypatch)
    assert calls, "the report was never requested"
    assert all(_plan_is_thinking_off(kw) for kw in calls), (
        "the report call did not carry a thinking-off answer plan"
    )
    assert "The evidence shows the answer plainly" in out, out
    assert _NOTE not in out
    assert closed["status"] == "done"
    assert _words(closed["report"]) >= 10


def test_a_model_that_never_writes_is_a_failed_run_not_an_empty_done(monkeypatch):
    """Zero report words is never 'done'. At 4810da0 this stored the 94-char
    note as a finished report; now the run is retried once, thinking off, and
    then closed as failed with one sentence that says what happened."""
    calls, closed = _wire(monkeypatch, _only_ever_reasons)
    out, events, elapsed = _run(monkeypatch)
    assert elapsed < 10, f"the report stage was not bounded ({elapsed:.1f}s)"
    assert closed["status"] != "done", f"stored as done with report {closed['report']!r}"
    assert closed["status"] == "failed"
    assert _NOTE not in out, "the note alone was presented as the report"
    # One honest sentence, naming what happened and what the person still has.
    assert out.count(". ") <= 1 and out.rstrip().endswith("."), out
    assert "no report text" in out and "sources" in out, out
    # Retried exactly once, and both calls asked for thinking off.
    assert len(calls) == 2, f"{len(calls)} report call(s)"
    assert all(_plan_is_thinking_off(kw) for kw in calls)
    run = _run_meta(events)
    assert run["report_retried"] is True
    assert run["report_cut_short"] is True
    # The sources the run read are still shown and recorded.
    assert closed["sources"] > 0
    assert [p for k, p in events if k == "meta"][-1]["sources"]


def test_a_stream_still_writing_is_not_cut_at_its_allowance(monkeypatch):
    """IDLE time, not elapsed time. This stream writes a token every 0.1 s
    for about 2.5 s against an allowance of about 1 s: at 4810da0 it was cut
    at the allowance mid-sentence; now it finishes, because it never stopped
    making progress."""

    async def steady(n, kw):
        for i in range(25):
            yield ("token", f"word{i} ")
            await asyncio.sleep(0.1)
        yield ("token", "and the final sentence arrives [1].")

    calls, closed = _wire(monkeypatch, steady)
    out, events, elapsed = _run(monkeypatch)
    assert "the final sentence arrives" in out, "a stream still producing was cut"
    assert _NOTE not in out
    run = _run_meta(events)
    assert run["report_cut_short"] is False
    assert "report" not in run["stages_cut_short"]
    assert closed["status"] == "done"


def test_a_stream_that_never_ends_is_stopped_at_the_hard_ceiling(monkeypatch):
    """Progress buys time, not unlimited time: a stream that keeps writing
    far past the allowance is stopped at the ceiling, keeps every word it
    wrote, and says why.

    "Never ends" is 200 tokens, 10 s, here: the ceiling is about 2 s, and a
    fake that truly never ended made this test HANG with the ceiling removed
    (QA mutation M17, killed after 12 minutes; on CI a 60-minute job timeout,
    not a named failure). Removed, this now fails on the assertions below.
    """

    async def endless(n, kw):
        for i in range(200):
            yield ("token", f"sentence {i} of an endless report [1]. ")
            await asyncio.sleep(0.05)
        yield ("token", "the last sentence nobody should have waited for.")

    calls, closed = _wire(monkeypatch, endless)
    monkeypatch.setattr(dr, "_REPORT_OVERRUN_S", 1.0)
    out, events, elapsed = _run(monkeypatch)
    assert "the last sentence nobody should have waited for" not in out, "the ceiling did not cut the stream"
    assert elapsed < 8, f"the ceiling did not hold ({elapsed:.1f}s)"
    assert "sentence 0 of an endless report" in out
    assert "reached its time budget" in out, "the cut was silent"
    run = _run_meta(events)
    assert run["report_cut_short"] is True
    assert closed["status"] == "done"


def test_a_zero_token_deadline_is_retried_once_with_thinking_off(monkeypatch):
    """The first call never produces a token (an engine that is slow to
    answer); the one retry, inside the floor, does. The report is the retry's
    text, and the record says a retry happened."""

    async def silent_then_writes(n, kw):
        if n == 1:
            await asyncio.sleep(30)
            yield ("token", "never arrives")
            return
        for piece in (REPORT[i:i + 8] for i in range(0, len(REPORT), 8)):
            yield ("token", piece)

    calls, closed = _wire(monkeypatch, silent_then_writes)
    out, events, elapsed = _run(monkeypatch)
    assert elapsed < 10
    assert "The evidence shows the answer plainly" in out, out
    assert "never arrives" not in out
    assert len(calls) == 2
    assert all(_plan_is_thinking_off(kw) for kw in calls)
    run = _run_meta(events)
    assert run["report_retried"] is True
    assert closed["status"] == "done"


def test_an_empty_completion_is_never_stored_as_done(monkeypatch):
    """The guard is on the stored run, not only on the clock: a call that
    finishes with no text at all is a failed report too."""

    async def says_nothing(n, kw):
        return
        yield  # pragma: no cover — makes this an async generator

    calls, closed = _wire(monkeypatch, says_nothing)
    out, events, elapsed = _run(monkeypatch)
    assert closed["status"] == "failed", f"stored as {closed['status']!r} with {closed['report']!r}"
    assert "no report text" in out


def test_the_report_stage_keeps_the_admission_promise_honest():
    """`frees_in_s` quotes a refused user when the earliest run MUST be over.
    With progress allowed to run past the allowance, that bound has to carry
    the ceiling too, or the refusal promises a time the run can overrun."""
    adm = dr._Admission()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "deep_research_timeout_s", 600.0)
        adm.running["user:1"] = [time.monotonic()]
        left = adm.frees_in_s()
    assert left >= 600.0 + dr._REPORT_OVERRUN_S - 1.0


# ---------------------------------------------------------------------------
# QA's B3 edges (2026-09-18), kept as regression pins. Each passed at
# a0a9b6f; every "forever" fake is bounded so a missing guard FAILS by name
# instead of hanging the job.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["think", "max"])
def test_the_thinking_off_switch_reaches_the_wire(world, monkeypatch, effort):  # noqa: F811
    """The tests above read the kwargs of a stubbed stream. This drives the
    REAL llm.stream_chat_events and continuation against the fake engine and
    reads the request body the engine received."""
    world.engine.pieces = ["The", " answer", " is", " written", " here", " [1]."]
    calls, closed = _wire(monkeypatch, None)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 600.0)
    # _wire stubbed the model; the real client goes back for this one.
    monkeypatch.setattr(dr.llm, "stream_chat_events", _REAL_STREAM)
    monkeypatch.setattr(dr.llm, "get_finish_reason", _REAL_FINISH_REASON)
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, effort=effort, conversation_id="c1"))
    assert world.engine.calls, "no report request reached the engine"
    for call in world.engine.calls:
        kw = (call.get("extra_body") or {}).get("chat_template_kwargs") or {}
        assert kw.get("enable_thinking") is False, call.get("extra_body")
    assert "The answer is written here" in out
    assert closed["status"] == "done"


def test_a_stream_of_citation_markers_only_is_failed_not_done(monkeypatch):
    """"[1] " restarts the idle clock but is not a word: the call runs to the
    ceiling, and a report of markers alone is never 'done'."""

    async def only_markers(n, kw):
        for _ in range(500):  # 10 s; the ceiling is about 1.8 s
            yield ("token", "[1] ")
            await asyncio.sleep(0.02)

    calls, closed = _wire(monkeypatch, only_markers)
    monkeypatch.setattr(dr, "_REPORT_OVERRUN_S", 0.8)
    out, events, elapsed = _run(monkeypatch)
    assert elapsed < 8, f"{elapsed:.1f}s"
    assert closed["status"] == "failed", (closed["status"], closed["report"][:80])
    assert "no report text" in out


def test_the_failure_sentence_never_claims_a_retry_that_was_not_made(monkeypatch):
    """Whitespace keeps the idle clock alive to the ceiling; the retry then
    starts past it and is cancelled at once. The sentence must not say "then
    one ... retry" for a call the engine never got (QA2)."""

    async def whitespace(n, kw):
        for _ in range(200):  # 10 s; the ceiling is about 1.5 s
            yield ("token", " ")
            await asyncio.sleep(0.05)

    calls, closed = _wire(monkeypatch, whitespace)
    monkeypatch.setattr(dr, "_REPORT_OVERRUN_S", 0.5)
    out, events, elapsed = _run(monkeypatch)
    assert elapsed < 8, f"{elapsed:.1f}s"
    assert closed["status"] == "failed"
    if "retry" in out:
        assert len(calls) == 2, f"sentence claims a retry, but {len(calls)} call(s) reached the engine: {out!r}"


def test_a_closed_tab_mid_report_is_a_cancel_not_an_idle_cut(monkeypatch):
    """A cancel from outside stays a cancel: the idle deadline must not turn
    it into a TimeoutError the retry path swallows."""

    async def slow_writer(n, kw):
        for i in range(400):
            yield ("token", f"word{i} ")
            await asyncio.sleep(0.01)

    calls, closed = _wire(monkeypatch, slow_writer)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 600.0)
    events, emit = _emitter()

    async def main():
        task = asyncio.create_task(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
        while not any(k == "token" for k, _ in events):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert closed["status"] == "cancelled", closed
    assert "word0" in closed["report"]
    assert len(calls) == 1, "a cancelled report was retried"


def test_an_engine_error_before_any_token_is_a_failure_not_a_retry(monkeypatch):
    async def dies(n, kw):
        raise ConnectionError("engine gone")
        yield  # pragma: no cover

    calls, closed = _wire(monkeypatch, dies)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 600.0)
    out, events, elapsed = _run(monkeypatch)
    assert closed["status"] == "failed"
    assert "The research run failed" in out
    assert len(calls) == 1
