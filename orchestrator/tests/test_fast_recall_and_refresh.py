"""Regressions for the Fast-path scoring cost and the refresh/run lifecycle.

Four defects, all found on 2026-09-06 against dev @ 29aa0ab:

* `recall.cosine` was called once per candidate, recomputing the query norm
  every time and materialising each packed vector into a Python list. That is
  synchronous CPU on the event loop, so it stalls every concurrent request.
* `context.count_tokens` built a fresh `httpx.AsyncClient` per call. Almost all
  of that cost is building the default SSL context (measured 11.7 ms; the same
  client with `verify=False` costs 0.10 ms) — spent on a plain-http call to a
  vLLM sidecar that never completes a TLS handshake.
* `db.upsert_web_page` omitted `next_refresh_at`, so every page stored after
  V13 was written NULL and the refresh scheduler could never see it again.
* `research_runs` had no restart reconciliation, so an interrupted run claimed
  to be 'running' forever.
"""
from __future__ import annotations

import array
import asyncio
import datetime as dt
import random

import pytest

from app import db, recall

_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Batched cosine: same ranking as the per-candidate path it replaced.
# --------------------------------------------------------------------------


def _blob(values):
    return array.array("f", values).tobytes()


@pytest.mark.parametrize("dim,count", [(8, 5), (1024, 64)])
def test_cosine_many_matches_per_candidate_cosine(dim, count):
    rng = random.Random(4242)
    query = [rng.uniform(-1, 1) for _ in range(dim)]
    blobs = [_blob([rng.uniform(-1, 1) for _ in range(dim)]) for _ in range(count)]

    expected = [recall.cosine(query, recall.unpack_vector(b)) for b in blobs]
    actual = recall.cosine_many(query, blobs)

    # float32 accumulation order differs; what must not differ is the ranking.
    assert actual == pytest.approx(expected, abs=1e-5)
    order = lambda scores: sorted(range(len(scores)), key=lambda i: -scores[i])
    assert order(actual) == order(expected)


def test_cosine_many_pure_python_fallback_agrees():
    """The no-numpy path must rank identically — it is what runs if the
    transitive pyarrow/numpy dependency ever goes away."""
    rng = random.Random(7)
    query = [rng.uniform(-1, 1) for _ in range(32)]
    blobs = [_blob([rng.uniform(-1, 1) for _ in range(32)]) for _ in range(20)]

    assert recall._cosine_many_py(query, blobs, 32) == pytest.approx(
        recall.cosine_many(query, blobs), abs=1e-5
    )


def test_cosine_many_edge_cases():
    assert recall.cosine_many([1.0, 2.0], []) == []
    assert recall.cosine_many([], [_blob([1, 0])]) == [0.0]
    assert recall.cosine_many([0.0, 0.0], [_blob([1, 0])]) == [0.0]
    assert recall.cosine_many([1.0, 0.0], [_blob([0, 0])]) == [0.0]


def test_cosine_many_scores_a_wrong_width_vector_zero_not_garbage():
    """A stored vector of another dimension must not be reinterpreted — a
    reshape over mixed widths would silently mis-align every later row."""
    scores = recall.cosine_many([1.0, 0.0], [_blob([1, 0, 1]), _blob([1, 0])])
    assert scores == [0.0, pytest.approx(1.0)]


# --------------------------------------------------------------------------
# The /tokenize client is reused, not rebuilt per call.
# --------------------------------------------------------------------------


def test_tokenize_client_is_reused_within_a_loop():
    from app import context

    async def main():
        return context._tokenize_client(), context._tokenize_client()

    context._TOKENIZE_CLIENTS.clear()
    first, second = asyncio.run(main())
    assert first is second, "count_tokens would rebuild an SSL context per call"


def test_tokenize_client_is_not_shared_across_loops():
    """An httpx pool is bound to the loop that created it; reusing one across
    loops is how a passing test suite starts failing in the second event loop."""
    from app import context

    context._TOKENIZE_CLIENTS.clear()
    a = asyncio.run(_get_client())
    b = asyncio.run(_get_client())
    assert a is not b


async def _get_client():
    from app import context

    return context._tokenize_client()


# --------------------------------------------------------------------------
# Every stored page is reachable by the refresh scheduler.
# --------------------------------------------------------------------------


def _store(url, text, *, origin="search", content_hash="h1"):
    return db.upsert_web_page(
        url, f"https://{url}", f"https://{url}", "T", text,
        "text/html", 200, content_hash, links=[], origin=origin,
    )


def _next_refresh(page_id):
    with db.connection() as con:
        return con.execute(
            "SELECT next_refresh_at FROM web_pages WHERE id = %s", (page_id,)
        ).fetchone()["next_refresh_at"]


@pytest.mark.parametrize("origin", ["search", "crawl", "research", "share"])
def test_a_newly_stored_page_is_scheduled_for_refresh(origin):
    """73% of the live corpus (1602/2208) was stranded with NULL here, so
    `web_worker._due_pages` — which requires NOT NULL — could never see it."""
    row = _store(f"example.test/{origin}", "Real extracted body text.", origin=origin)
    assert _next_refresh(row["id"]) is not None


def test_a_page_with_no_text_is_left_unscheduled():
    """Matches the scheduler's own `text <> ''` filter: scheduling an empty
    page would queue work that can never be selected."""
    row = _store("example.test/empty", "")
    assert _next_refresh(row["id"]) is None


def test_re_storing_a_page_never_overrides_the_workers_deadline():
    row = _store("example.test/keep", "Body one.", content_hash="h1")
    pinned = dt.datetime(2031, 1, 1, tzinfo=dt.timezone.utc)
    with db.connection() as con:
        con.execute(
            "UPDATE web_pages SET next_refresh_at = %s WHERE id = %s", (pinned, row["id"])
        )
    _store("example.test/keep", "Body two, changed.", content_hash="h2")
    assert _next_refresh(row["id"]) == pinned


# --------------------------------------------------------------------------
# A restart-interrupted research run is closed honestly.
# --------------------------------------------------------------------------


def test_close_interrupted_research_runs_closes_only_running_ones():
    running = db.create_research_run(
        conversation_id="c1", user_id=None, question="q1", research_id="r1"
    )
    finished = db.create_research_run(
        conversation_id="c1", user_id=None, question="q2", research_id="r2"
    )
    db.finish_research_run(finished, "done", 3, 5, 12, 4, "a report", [{"n": 1}], "")

    assert db.close_interrupted_research_runs() == 1

    with db.connection() as con:
        rows = {
            r["id"]: r
            for r in con.execute(
                "SELECT id, status, detail, finished_at, report FROM research_runs"
            ).fetchall()
        }
    assert rows[running]["status"] == "failed"
    assert rows[running]["detail"] == "interrupted by a restart"
    assert rows[running]["finished_at"] is not None
    # A completed run must not be touched by the reconciliation.
    assert rows[finished]["status"] == "done"
    assert rows[finished]["report"] == "a report"


def test_close_interrupted_research_runs_is_idempotent():
    db.create_research_run(
        conversation_id="c1", user_id=None, question="q", research_id="r3"
    )
    assert db.close_interrupted_research_runs() == 1
    assert db.close_interrupted_research_runs() == 0


def test_cosine_many_does_not_wake_a_blas_thread_pool():
    """`matrix @ query` would be the obvious spelling and is a trap here.

    OpenBLAS answers a matmul by waking a thread per core and busy-waiting
    afterwards. Measured 2026-09-06 on this 20-core box, over a 2-second
    window: the einsum form runs 23,264 iterations at a 1.00x CPU-to-wall
    ratio; the `@` form runs 24,337 (4.6% more) and bills 31.8 seconds of CPU
    for 2 seconds of wall time — 16x, in a process whose whole problem was CPU
    contention on the event loop, on a box it shares with vLLM.

    Two methodology points, both learned by getting them wrong first:
    * Run in a subprocess. The ratio is only meaningful in a process that has
      not already woken a pool for some other reason, and pytest's has.
    * Quiesce, then measure over seconds. Importing numpy alone wakes a pool
      that spins down over roughly a second; a short window measures that
      decay rather than the code under test, which is how this test first
      "failed" against an implementation that was already correct.
    """
    import subprocess
    import sys
    import textwrap

    probe = textwrap.dedent(
        """
        import array, random, sys, time
        sys.path.insert(0, %r)
        from app import recall
        random.seed(5)
        dim, n = 1024, 400
        q = [random.uniform(-1, 1) for _ in range(dim)]
        blobs = [array.array("f", [random.uniform(-1, 1) for _ in range(dim)]).tobytes()
                 for _ in range(n)]
        recall.cosine_many(q, blobs)
        time.sleep(3.0)
        t0, c0 = time.perf_counter(), time.process_time()
        while time.perf_counter() - t0 < 2.0:
            recall.cosine_many(q, blobs)
        print((time.process_time() - c0) / (time.perf_counter() - t0))
        """
    ) % str(_ROOT)

    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=300
    )
    if out.returncode != 0:  # numpy absent → the pure-python fallback
        pytest.skip(f"probe did not run: {out.stderr[-300:]}")
    ratio = float(out.stdout.strip().splitlines()[-1])
    assert ratio < 3.0, (
        f"cosine_many burned {ratio:.1f}x CPU per wall-clock second — a BLAS "
        "thread pool is spinning. Keep the einsum form; see the docstring."
    )


def test_every_upsert_web_page_caller_records_the_extractor_version():
    """A store path that omits extract_version puts its pages in a refetch loop.

    The refresh worker re-reads any page whose extract_version is below the
    current one. A caller that does not pass it stores 0, is immediately due
    again, re-fetches, stores 0 again — forever. When this was first wired only
    engines/crawl.py passed it, while `search` (1871 of 2208 live rows) and the
    pasted-link path did not, so the dominant store path would have looped.

    Source-level on purpose: the failure is an omission at a call site, and
    only reading the call sites can see an omission.
    """
    import ast
    from pathlib import Path

    offenders = []
    for path in sorted((_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "upsert_web_page":
                continue
            keywords = {k.arg for k in node.keywords}
            # **meta / **kwargs could carry it, so only a call with neither an
            # explicit keyword nor a splat is definitely missing it.
            if "extract_version" in keywords or None in keywords:
                continue
            offenders.append(f"{path.relative_to(_ROOT)}:{node.lineno}")

    assert not offenders, (
        "these upsert_web_page callers do not record extract_version, so the "
        f"pages they store are permanently due for re-extraction: {offenders}"
    )


# --------------------------------------------------------------------------
# A terse follow-up must not silently lose its grounding.
# --------------------------------------------------------------------------


def test_a_terse_follow_up_recovers_its_subject_from_the_turns():
    """Measured 2026-09-06 against a seeded corpus, before this existed:

        "What does an H100 cost per GPU-hour on Orbital Compute?"
            -> 1584 chars of grounding, 2 sources, decision 'local'
        "and the B200?"
            -> 0 chars, 0 sources, decision 'static_model'

    `_topical`'s gate needs a strong dense score AND lexical overlap. A
    follow-up carries one content word, misses on both, and falls through to
    the model's own memory — which in the benchmark invented "$3.50 per
    GPU-hour" for a page that plainly states $6.75.
    """
    from app import living_knowledge as lk

    history = [
        {"role": "user", "content": "What does an H100 cost per GPU-hour on Orbital Compute?"},
        {"role": "assistant", "content": "An H100 80GB on Orbital Compute costs $2.90 per GPU-hour."},
    ]
    resolved = lk.resolve_from_history("and the B200?", history)
    assert resolved.startswith("and the B200?"), "the user's own words must survive"
    assert "orbital" in resolved.lower() and "compute" in resolved.lower()
    # The recovered terms are appended PLAIN. Bracketing them cost 0.185 of
    # dense score (0.493 -> 0.308) and dropped the page below the retrieval
    # gate entirely, so the punctuation is load-bearing.
    assert "(" not in resolved and ")" not in resolved


def test_a_self_standing_question_is_left_exactly_as_asked():
    from app import living_knowledge as lk

    question = "What does an H100 cost per GPU-hour on Orbital Compute?"
    history = [{"role": "user", "content": "Something else entirely about badgers."}]
    assert lk.resolve_from_history(question, history) == question


def test_follow_up_resolution_never_carries_a_pinned_system_block():
    """This string becomes a retrieval query, and on the escalation path a WEB
    SEARCH. A saved fact or a document excerpt reaching it would be that
    content on the wire to a third-party engine."""
    from app import living_knowledge as lk

    history = [
        {"role": "system", "content": "SAVED FACT: the user's home address is 42 Wallaby Way."},
        {"role": "system", "content": "Recall from another chat: PROJECT-ORION ships in March."},
        {"role": "user", "content": "What does an H100 cost on Orbital Compute?"},
        {"role": "assistant", "content": "It costs $2.90 per GPU-hour."},
    ]
    resolved = lk.resolve_from_history("and the B200?", history)
    for leaked in ("wallaby", "orion", "address", "saved fact", "recall"):
        assert leaked not in resolved.lower(), f"{leaked!r} reached the retrieval query"


def test_resolution_is_a_no_op_without_history():
    from app import living_knowledge as lk

    assert lk.resolve_from_history("and the B200?", []) == "and the B200?"
    assert lk.resolve_from_history("", [{"role": "user", "content": "x"}]) == ""


# --------------------------------------------------------------------------
# …and the turns actually REACH the resolver, end to end.
#
# `resolve_from_history` being correct is worth nothing if `prepare` is never
# handed the turns. The parameter did not exist until 2026-09-06 — every
# caller passed a question and nothing else — so the tests below pin the wire
# at each joint: the two `main` call sites, the `main` wrapper, and `prepare`
# itself putting the resolved string in front of BOTH consumers that read the
# question (the freshness classifier and retrieval).
# --------------------------------------------------------------------------


_FOLLOW_UP_HISTORY = [
    {"role": "user", "content": "What does an H100 cost per GPU-hour on Orbital Compute?"},
    {"role": "assistant", "content": "An H100 80GB on Orbital Compute costs $2.90 per GPU-hour."},
]


def _prepare_capturing(monkeypatch, question, history, **kwargs):
    """Run `living_knowledge.prepare` with every service faked, and report the
    question string each downstream consumer was handed."""
    from app import living_knowledge as lk
    from app.config import settings
    from app.freshness import Freshness, Verdict
    from app.web_memory import Retrieval

    seen = {}

    # No router call: `classify` must decide offline, so this test needs no
    # model. What is under test is the STRING it is given, not its verdict.
    monkeypatch.setattr(settings, "freshness_router_enabled", False)

    async def fake_classify(q, *, now_year, allow_router=True):
        seen["classify"] = q
        return Verdict(Freshness.STATIC, 10**9, "test")

    async def fake_retrieve(q, **kw):
        seen["retrieve"] = q
        return Retrieval(query=q, freshness=kw.get("level", Freshness.STATIC))

    def fake_claims(q, limit=3):
        seen["claims"] = q
        return []

    monkeypatch.setattr(lk, "classify", fake_classify)
    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    monkeypatch.setattr(lk, "claims_for", fake_claims)

    call = dict(
        effort="fast", mode="assistant", web_search_pref="auto",
        allow_network=False, history=history,
    )
    call.update(kwargs)
    seen["prepared"] = asyncio.run(lk.prepare(question, **call))
    return seen


def test_prepare_resolves_the_follow_up_before_it_classifies_or_retrieves(monkeypatch):
    """The order matters as much as the call: `prepare` resolves FIRST, so the
    freshness classifier and the retrieval query both see the subject. Resolve
    after classification and a follow-up about a live fact is still classified
    on three content words."""
    seen = _prepare_capturing(monkeypatch, "and the B200?", _FOLLOW_UP_HISTORY)

    for consumer in ("classify", "retrieve"):
        assert "orbital" in seen[consumer].lower(), (
            f"{consumer} was handed {seen[consumer]!r} — the antecedent never "
            "reached it, which is the fabrication bug"
        )
    assert seen["retrieve"].startswith("and the B200?")


def test_prepare_without_history_is_the_old_ungrounded_behaviour(monkeypatch):
    """The control. Same question, no turns: this is exactly what every caller
    produced before `history` existed, and it is why the model invented a
    price for a page that stated one."""
    seen = _prepare_capturing(monkeypatch, "and the B200?", [])
    assert seen["retrieve"] == "and the B200?"


def test_prepare_accepts_history_and_defaults_it_empty():
    """A keyword-only parameter with a default: no caller can pass the turns
    positionally by accident, and an older caller still works."""
    import inspect

    from app import living_knowledge as lk

    param = inspect.signature(lk.prepare).parameters["history"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default == ()


def test_the_main_wrapper_forwards_the_turns_it_was_given():
    """`main._prepare_knowledge` is the only door into the knowledge layer for
    a chat turn. Source-level because the wrapper swallows every exception —
    a dropped argument there fails silently, as an ungrounded answer."""
    import inspect

    from app import main

    assert "history" in inspect.signature(main._prepare_knowledge).parameters
    body = inspect.getsource(main._prepare_knowledge)
    assert "history=history" in body, (
        "_prepare_knowledge does not forward its turns to living_knowledge."
        "prepare; every follow-up would be resolved against nothing"
    )


def test_every_main_call_site_hands_the_knowledge_layer_the_turns():
    """Both of them: the early pre-pass task and the late in-line fallback.

    An omission at ONE call site is the shape of this bug — the answer is
    still produced, still cited, and silently ungrounded on exactly the turns
    that take the other branch. Only reading the call sites can see it.
    """
    import ast

    source = (_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", "")) == "_prepare_knowledge"
    ]
    assert len(calls) == 2, f"expected both known call sites, found {len(calls)}"
    for node in calls:
        keywords = {k.arg for k in node.keywords}
        assert "history" in keywords or None in keywords, (
            f"app/main.py:{node.lineno} calls _prepare_knowledge without the "
            "conversation turns; a terse follow-up there is ungrounded"
        )
