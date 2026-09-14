"""The bounds `/v1/embeddings` and `/v1/rerank` keep once their clock is gone
(review of the no-timeout sidecar routes, 2026-09-14).

* `/tokenize` before a status line goes through ONE process-wide gate per
  engine and stops counting at PUBLIC_API_LENGTH_CHECK_BUDGET_S; what it did
  not count is counted inside the committed response, and the count is
  remembered so an SDK retry gets its real 400 before its status line;
* a 4xx from `/tokenize` is "this engine will not count", not an outage;
* accepted-but-unfinished pooling work is held to a process-wide memory
  budget, refused before the status line;
* the Files API's direct callers of the one-call helpers keep a 60 s read
  bound — only the public route, whose witness decides, gets 600 s;
* an input that crashes the reranker twice is quarantined like an embedding.

The engines are `httpx.MockTransport`s (tests/publicapi_sidecar_support.py).
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.config import settings
from app.publicapi import endpoint_models, endpoints, errors, keepalive, sidecars
from tests.publicapi_sidecar_support import (  # noqa: F401 - fixtures by name
    _pepper,
    api,
    auth,
    daily,
    engines_configured,
    install_embed_engine,
    install_transport,
    no_backoff,
    platform,
    usage_events,
    vllm_embeddings,
)

LONG = "word " * 1000  # 5,000 bytes: over the 4,096 window, so it must be counted


def _long(i: int) -> str:
    return f"text {i} " + LONG


# ------------------------------------------------------------ /tokenize --


def _slow_tokenizer(monkeypatch, *, delay_s: float, count=lambda prompt: 900):
    """`sidecars._transport` = a /tokenize that takes `delay_s` and records
    how many calls were in flight at once."""
    seen = {"live": 0, "peak": 0, "calls": 0}

    async def respond(request):
        body = json.loads(await request.aread())
        seen["calls"] += 1
        seen["live"] += 1
        seen["peak"] = max(seen["peak"], seen["live"])
        try:
            await asyncio.sleep(delay_s)
        finally:
            seen["live"] -= 1
        answer = count(body["prompt"])
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={"count": answer})

    monkeypatch.setattr(sidecars, "_transport", httpx.MockTransport(respond))
    return seen


def test_tokenize_calls_from_concurrent_requests_share_one_gate_per_engine(engines_configured, monkeypatch):
    """Eight per REQUEST used to be the only bound, so M requests sent 8×M
    ungated calls to the API server chat recall embeds through."""
    monkeypatch.setenv("PUBLIC_API_TOKENIZE_CONCURRENCY", "3")
    seen = _slow_tokenizer(monkeypatch, delay_s=0.02)

    async def main():
        return await asyncio.gather(
            *(sidecars.check_embed_lengths([_long(r * 100 + i) for i in range(12)]) for r in range(4))
        )

    checks = asyncio.run(main())

    assert seen["calls"] == 48
    assert seen["peak"] == 3
    assert all(check.unresolved == [] and check.counted == 12 for check in checks)


def test_the_check_before_the_status_line_stops_at_its_budget_and_hands_the_rest_to_the_committed_response(
    engines_configured, monkeypatch
):
    monkeypatch.setenv("PUBLIC_API_TOKENIZE_CONCURRENCY", "2")
    monkeypatch.setenv("PUBLIC_API_LENGTH_CHECK_BUDGET_S", "0.25")
    seen = _slow_tokenizer(monkeypatch, delay_s=0.1)
    inputs = [_long(i) for i in range(20)]

    async def main():
        started = time.monotonic()
        check = await sidecars.check_embed_lengths(inputs)
        elapsed = time.monotonic() - started
        assert 0 < check.counted < 20
        assert sorted(check.unresolved) == sorted(set(range(20)) - {i for i, w in enumerate(check.weights) if w == 900})
        left = list(check.unresolved)
        await sidecars.resolve_lengths(
            check, inputs, window=4096, root="http://embed-engine.internal:30003",
            model=settings.embed_model, field_name="input", single=False,
        )
        return elapsed, left, check

    elapsed, left, check = asyncio.run(main())

    assert elapsed < 0.6  # 20 × 0.1 s at 2 at a time would be 1 s
    assert left and check.unresolved == [] and check.weights == [900] * 20
    assert seen["peak"] == 2


def test_the_length_check_budget_is_clamped_inside_the_byte_invariant(monkeypatch):
    monkeypatch.setenv("PUBLIC_API_LENGTH_CHECK_BUDGET_S", "90")
    assert sidecars.length_check_budget_s() == 15.0
    monkeypatch.setenv("PUBLIC_API_LENGTH_CHECK_BUDGET_S", "-1")
    assert sidecars.length_check_budget_s() == 0.0
    monkeypatch.delenv("PUBLIC_API_LENGTH_CHECK_BUDGET_S")
    assert sidecars.length_check_budget_s() == 8.0


def test_an_over_length_count_that_finished_after_the_commit_is_a_real_400_on_the_retry_without_counting_again(
    engines_configured, monkeypatch
):
    seen = _slow_tokenizer(monkeypatch, delay_s=0.05, count=lambda prompt: 5000)
    inputs = ["short", _long(1)]

    async def main():
        # The first request's check ran out of budget before counting...
        check = await sidecars.check_embed_lengths(inputs, budget_s=0.01)
        assert check.unresolved == [1]
        # ...so its 400 came from inside the committed response.
        with pytest.raises(errors.ApiError) as late:
            await sidecars.resolve_lengths(
                check, inputs, window=4096, root="http://embed-engine.internal:30003",
                model=settings.embed_model, field_name="input", single=False,
            )
        assert late.value.code == "context_length_exceeded"
        calls = seen["calls"]
        # The SDK's retry: the count is remembered, the 400 is before its status line.
        with pytest.raises(errors.ApiError) as retry:
            await sidecars.check_embed_lengths(inputs, budget_s=0)
        return calls, retry.value

    calls, refusal = asyncio.run(main())

    assert (refusal.code, refusal.param) == ("context_length_exceeded", "input.1")
    # The cut-off call and the committed one; none for the retry.
    assert seen["calls"] == calls == 2


def test_a_tokenizer_that_answers_404_is_not_waited_for_and_the_engine_decides(api, monkeypatch):
    """An engine image without /tokenize used to be "unreachable" for the
    whole 1,800 s engine-down grace. Now the input is sent at the window
    weight, and the engine's own 400 names an input that is too long."""
    slept = no_backoff(monkeypatch)
    engine = install_embed_engine(
        monkeypatch,
        lambda request, body: vllm_embeddings(body),
        tokenize=lambda prompt: httpx.Response(404, json={"detail": "Not Found"}),
    )

    response = api.post("/v1/embeddings", json={"model": "techsara-embed", "input": [LONG]}, headers=auth())

    assert response.status_code == 200, response.text
    assert slept == []
    assert len(engine.tokenized) == 1 and engine.calls == 1


def test_an_input_the_engine_refused_as_too_long_without_a_tokenizer_is_a_real_400_before_the_retrys_status_line(
    api, monkeypatch
):
    """No /tokenize, so the engine's own 400 decides — possibly after the
    commit, where it can only drop the connection. The refusal is remembered:
    the retry is refused before its status line without reaching the engine."""

    def engine(request, body):
        if any(len(text) > 4096 for text in json.loads(body)["input"]):
            return httpx.Response(400, json={"object": "error", "message": "maximum context length is 4096 tokens"})
        return vllm_embeddings(body)

    recorded = install_embed_engine(
        monkeypatch, engine, tokenize=lambda prompt: httpx.Response(404, json={"detail": "Not Found"})
    )
    body = {"model": "techsara-embed", "input": ["short", LONG]}

    first = api.post("/v1/embeddings", json=body, headers=auth())
    assert first.status_code == 400 and first.json()["error"]["param"] == "input.1"
    sent = recorded.calls

    async def retry_check():
        with pytest.raises(errors.ApiError) as refused:
            await sidecars.check_embed_lengths(body["input"])
        return refused.value

    refusal = asyncio.run(retry_check())
    assert (refusal.code, refusal.param) == ("context_length_exceeded", "input.1")
    assert api.post("/v1/embeddings", json=body, headers=auth()).status_code == 400
    assert recorded.calls == sent


def test_a_tokenizer_that_answers_429_is_counted_again_rather_than_skipped(engines_configured, monkeypatch):
    answers = iter([httpx.Response(429), 700])
    _slow_tokenizer(monkeypatch, delay_s=0.0, count=lambda prompt: next(answers))

    async def main():
        check = await sidecars.check_embed_lengths([LONG])
        assert check.unresolved == [0] and check.uncountable == []
        return check

    assert asyncio.run(main()).weights == [4096]


def test_the_commit_window_of_a_route_loses_what_its_length_check_spent(monkeypatch):
    monkeypatch.setattr(settings, "public_api_sync_commit_s", 12.0, raising=False)
    monkeypatch.setattr(endpoints, "_request_id", lambda request: "req_x")

    async def work():
        return {}

    assert endpoints._committed(object(), work, spent_s=3.5)._commit_s == pytest.approx(8.5)
    assert endpoints._committed(object(), work, spent_s=40.0)._commit_s == 0.0
    assert endpoints._committed(object(), work)._commit_s == pytest.approx(keepalive.sync_commit_s())


# ------------------------------------------------------- memory budget --


def test_a_request_that_does_not_fit_the_pooling_memory_budget_is_a_503_before_its_body_is_read(
    api, platform, monkeypatch
):
    monkeypatch.setenv("PUBLIC_API_POOLING_MEMORY_BYTES", str(64 * 1024 * 1024))
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))
    other = sidecars.POOLING_MEMORY.reserve(64 * 1024 * 1024 - 1024)  # other requests' vectors

    refused = api.post("/v1/embeddings", json={"model": "techsara-embed", "input": ["x"] * 400}, headers=auth())

    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "model_unavailable"
    assert int(refused.headers["Retry-After"]) == sidecars.MEMORY_RETRY_AFTER_S
    assert refused.content[:1] == b"{"
    assert engine.calls == 0
    assert daily(platform["project"]["id"])["requests"] == 0  # refused before it was counted
    assert (sidecars.POOLING_MEMORY.used, sidecars.POOLING_MEMORY.holders) == (64 * 1024 * 1024 - 1024, 1)

    other.release()
    assert api.post("/v1/embeddings", json={"model": "techsara-embed", "input": ["x"] * 400}, headers=auth()).status_code == 200
    assert (sidecars.POOLING_MEMORY.used, sidecars.POOLING_MEMORY.holders) == (0, 0)


def test_the_charge_is_the_parsed_request_and_every_vector_it_will_hold(api, platform, monkeypatch):
    """Refused AFTER parsing too: the 2,048-vector charge is known only once
    the inputs are parsed, and it is still before the status line — counted,
    like every refusal after the quota gate (CONTRACT §4), and settled as
    nothing ran."""
    monkeypatch.setenv("PUBLIC_API_POOLING_MEMORY_BYTES", str(20 * 1024 * 1024))
    install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))
    small = sidecars.POOLING_MEMORY.reserve(1024)
    inputs = ["tiny"] * 2048
    body = json.dumps({"model": "techsara-embed", "input": inputs})
    expected = sidecars.embed_memory_bytes(len(body), 2048)
    assert expected > 20 * 1024 * 1024 > sidecars.embed_memory_bytes(len(body), 0)

    refused = api.post("/v1/embeddings", content=body, headers={**auth(), "Content-Type": "application/json"})

    assert refused.status_code == 503, refused.text
    assert usage_events("v1_embeddings") == []
    assert daily(platform["project"]["id"]) == {"requests": 1, "input_tokens": 0, "output_tokens": 0, "errors": 0}
    small.release()
    assert (sidecars.POOLING_MEMORY.used, sidecars.POOLING_MEMORY.holders) == (0, 0)


def test_a_request_alone_is_admitted_even_when_it_is_larger_than_the_whole_budget(api, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_POOLING_MEMORY_BYTES", "1")
    install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post("/v1/embeddings", json={"model": "techsara-embed", "input": ["a", "b"]}, headers=auth())

    assert response.status_code == 200, response.text
    assert (sidecars.POOLING_MEMORY.used, sidecars.POOLING_MEMORY.holders) == (0, 0)


@pytest.mark.parametrize(
    "path, body",
    [
        ("/v1/embeddings", {"model": "techsara-embed", "input": ["a"], "dimensions": 3}),
        ("/v1/rerank", {"model": "techsara-rerank", "query": "q", "documents": []}),
    ],
)
def test_a_refused_request_gives_its_memory_back(api, path, body):
    assert api.post(path, json=body, headers=auth()).status_code == 400
    assert (sidecars.POOLING_MEMORY.used, sidecars.POOLING_MEMORY.holders) == (0, 0)


def test_a_rerank_that_does_not_fit_the_budget_is_a_503_and_one_that_does_gives_it_back(api, monkeypatch):
    from tests.test_publicapi_rerank import _body, _scores

    monkeypatch.setenv("PUBLIC_API_POOLING_MEMORY_BYTES", str(1024 * 1024))
    recorded = install_transport(_scores({}))
    other = sidecars.POOLING_MEMORY.reserve(1024 * 1024)

    assert api.post("/v1/rerank", json=_body(), headers=auth()).status_code == 503
    assert recorded.calls == 0
    other.release()
    assert api.post("/v1/rerank", json=_body(), headers=auth()).status_code == 200
    assert (sidecars.POOLING_MEMORY.used, sidecars.POOLING_MEMORY.holders) == (0, 0)


def test_a_reservation_is_given_back_when_its_response_is_never_sent():
    import gc

    budget = sidecars.PoolingMemory()
    reservation = budget.reserve(100)

    class Owner:
        pass

    owner = Owner()
    reservation.bind(owner)
    del owner
    gc.collect()

    assert (budget.used, budget.holders) == (0, 0)
    reservation.release()  # idempotent
    assert (budget.used, budget.holders) == (0, 0)


def test_vectors_are_held_as_doubles_and_rendered_byte_for_byte_as_a_float_list(engines_configured, monkeypatch):
    install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body, dims=5))
    tricky = [0.1, -1e-310, 1.0000000000000002, 123456789.123, -0.0]

    async def main():
        return await sidecars.embed(["a", "b"], wait_s=None)

    outcome = asyncio.run(main())
    assert all(vector.typecode == "d" for vector in outcome.vectors)

    import array

    vectors = [array.array("d", tricky), outcome.vectors[1]]
    for base64_wanted in (False, True):
        reference = json.dumps(
            {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": sidecars.encode_base64(v) if base64_wanted else list(v),
                    }
                    for i, v in enumerate(vectors)
                ],
                "model": "techsara-embed",
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        rendered = endpoints._render_embeddings(
            vectors, base64_wanted=base64_wanted, model_id="techsara-embed", prompt_tokens=4
        )
        assert rendered == reference
    assert endpoints._render_embeddings([], base64_wanted=False, model_id="m", prompt_tokens=None) == (
        b'{"object":"list","data":[],"model":"m","usage":null}'
    )


# ---------------------------------------------------------- read bounds --


def test_the_files_apis_direct_callers_keep_a_sixty_second_read_bound_and_only_the_route_waits_for_its_witness(
    api, monkeypatch
):
    from app.apifiles import vectors
    from app.publicapi import file_inputs

    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    async def files_callers():
        await vectors.engine_embed_documents(["passage"])
        assert await vectors.make_engine_query_embedder(5.0)("question") is not None
        assert await file_inputs._patient_embedder()("question") is not None

    asyncio.run(files_callers())
    assert engine.read_timeouts == [sidecars.EMBED_READ_TIMEOUT_S] * 3 == [60.0] * 3

    assert api.post("/v1/embeddings", json={"model": "techsara-embed", "input": "x"}, headers=auth()).status_code == 200
    assert engine.read_timeouts[3:] == [endpoint_models.pooling_silence_s()] == [600.0]


def test_the_patient_file_reranker_builds_its_client_with_the_sixty_second_bound(engines_configured, monkeypatch):
    from app.publicapi import file_inputs
    from tests.test_publicapi_rerank import _scores

    install_transport(_scores({}))
    bounds = []
    real = sidecars._http_client

    async def recording(timeout_s, **kwargs):
        bounds.append(timeout_s)
        return await real(timeout_s, **kwargs)

    monkeypatch.setattr(sidecars, "_http_client", recording)

    scores = asyncio.run(file_inputs._patient_reranker()("question", ["a document"]))

    assert scores is not None
    assert bounds == [sidecars.RERANK_READ_TIMEOUT_S] == [60.0]


# ----------------------------------------------------- rerank quarantine --


def test_a_document_that_crashes_the_reranker_twice_is_named_and_refused_before_the_gate_next_time(api, monkeypatch):
    from tests.test_publicapi_rerank import _body, _scores

    no_backoff(monkeypatch)
    state = {"down": 0, "crashes": 0}
    scores = _scores({})

    def reranker(request, body):
        if state["down"]:
            state["down"] -= 1
            raise httpx.ConnectError("connection refused")
        if any("POISON" in text for text in json.loads(body)["text_2"]):
            state["crashes"] += 1
            state["down"] = 1
            raise httpx.RemoteProtocolError("server disconnected without sending a response")
        return scores(request, body)

    recorded = install_transport(reranker)
    body = _body(documents=["fine", "POISON", "also fine"])

    first = api.post("/v1/rerank", json=body, headers=auth())

    assert first.status_code == 503 and first.headers["x-should-retry"] == "false"
    assert first.json()["error"]["param"] == "documents.1"
    assert state["crashes"] == 2
    sent = recorded.calls

    again = api.post("/v1/rerank", json=body, headers=auth())
    assert again.status_code == 503 and again.headers["x-should-retry"] == "false"
    assert recorded.calls == sent and state["crashes"] == 2
    assert api.post("/v1/rerank", json=_body(documents=["fine", "also fine"]), headers=auth()).status_code == 200


# ------------------------------------------------------ inline input_audio --


def test_an_inline_mp3_over_the_clip_ceiling_is_refused_before_whisper_decodes_it(engines_configured, monkeypatch):
    from app.apifiles import inline
    from app.publicapi import audio_jobs

    async def long_probe(audio, **_kwargs):
        return 900.0

    class NeverSent:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the clip reached the speech engine")

    monkeypatch.setattr(sidecars, "probe_seconds", long_probe)
    monkeypatch.setattr(audio_jobs, "WhisperDispatcher", NeverSent)
    decoded = inline.DecodedAudio(raw=b"ID3" + b"\x00" * 64, content_type="audio/mpeg", seconds=None, param="input.0.content.1")

    with pytest.raises(errors.ApiError) as refused:
        asyncio.run(inline.transcribe_decoded(decoded))

    assert refused.value.param == "input.0.content.1.data"
    assert "at most" in refused.value.message


def test_a_speech_engine_refusal_in_a_generation_names_the_input_part_not_a_multipart_field(engines_configured):
    from app.apifiles import inline

    async def engine(raw, content_type):
        raise errors.invalid_request("The audio could not be decoded. Send a supported audio file.", param="file")

    decoded = inline.DecodedAudio(raw=b"RIFF", content_type="audio/wav", seconds=None, param="input.2.content.0")

    with pytest.raises(errors.ApiError) as refused:
        asyncio.run(inline.transcribe_decoded(decoded, transcriber=engine))

    assert refused.value.param == "input.2.content.0.data"
