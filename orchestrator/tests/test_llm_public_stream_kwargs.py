"""llm.stream_chat_events — the no-timeout /v1 keywords (design revision 2, 2026-09-13).

A fake main engine behind the real breaker, the real admission lanes and the
real resilient wrapper; no network. What is pinned:

- `wall_clock_s=None` (or ≤ 0) removes the generation wall clock, while a chat
  caller that passes nothing keeps GEN_WALL_CLOCK_S and its in-band marker;
  `wall_clock_marker=False` stops without the marker;
- `read_timeout_s=None` builds a client with NO read timeout over a TCP
  keepalive transport, cached separately from the default transport;
- `continue_final_message=True` sends vLLM's continuation body, never trims
  the messages, and raises ContinuationRoomExhausted before sending anything
  when an EXACT count leaves the window no room; an inexact count is retried
  with PUBLIC_API_CONTINUATION_TOKENIZE_TIMEOUT_S and never ends the run: the
  request goes out, and a refusal of its guessed size is re-sent once with
  the engine sizing it (T1 review, 2026-09-14);
- `on_dispatch` fires after admission, as the request goes to the engine —
  never while the request waits for a lane;
- `admission_patient=True` waits where a chat request is refused at the door;
- engine_state.note_chunk() runs on every engine chunk;
- with no new keyword the request and the client are exactly chat's.
"""
from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

from app import admission, breaker, context, continuity, engine_state, llm, metrics
from app.config import settings

MAIN_URL = "http://vllm-main.test:8000/v1"
MODEL = "Qwen/Qwen3.6-35B-A3B-NVFP4"
MSGS = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "count"}]


class _Stream:
    def __init__(self, pieces, delay_s: float = 0.0) -> None:
        self.pieces = list(pieces)
        self.delay_s = delay_s
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for i, text in enumerate(self.pieces):
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            last = i == len(self.pieces) - 1
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason="stop" if last else None)],
                usage=None,
            )

    async def close(self) -> None:
        self.closed = True


class _Engine:
    def __init__(self) -> None:
        self.calls: list = []
        self.events: list = []
        self.pieces = ["1", " 2", " 3", " 4"]
        self.delay_s = 0.0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.events.append("create")
        self.calls.append(kwargs)
        return _Stream(self.pieces, self.delay_s)


@pytest.fixture()
def world(monkeypatch):
    metrics.reset()
    breaker.reset()
    engine_state.reset()
    continuity.reset()
    admission.reset()
    monkeypatch.setattr(settings, "openai_base_url", MAIN_URL)
    monkeypatch.setattr(settings, "llm_model", MODEL)
    monkeypatch.setattr(settings, "admission_normal_max", 1)
    monkeypatch.setattr(settings, "admission_max_waiting", 0)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 30.0)
    monkeypatch.setattr(admission, "_POLL_S", 0.02)
    breaker.install("main", breaker.Breaker("main", external_open=engine_state.external_open))
    engine = _Engine()
    built: list = []

    def client(base_url, api_key=None, **options):
        built.append(options)
        return engine

    monkeypatch.setattr(llm, "_client", client)
    fits: list = []

    async def fit(messages, *, base_url, model, requested_max_tokens=None):
        fits.append(list(messages))
        return list(messages), requested_max_tokens or 64

    monkeypatch.setattr(llm.context, "fit_request", fit)

    async def count(base_url, model, messages):
        context._last_count_exact.set(True)
        return 40, 1000

    async def window(base_url, model):
        return 1000

    monkeypatch.setattr(llm.context, "count_tokens", count)
    monkeypatch.setattr(llm.context, "model_window", window)
    monkeypatch.setattr(settings, "context_safety_margin", 100)
    world = SimpleNamespace(engine=engine, built=built, fits=fits)
    yield world
    admission.reset()
    breaker.reset()
    engine_state.reset()
    continuity.reset()
    metrics.reset()


async def _collect(**kwargs):
    out = []
    async for kind, delta in llm.stream_chat_events(MSGS, model_choice="fast", effort="fast", **kwargs):
        out.append((kind, delta))
    return out


def test_no_new_keyword_keeps_the_chat_request_and_client_exactly(world):
    out = asyncio.run(_collect(max_tokens=64))
    assert "".join(d for _k, d in out) == "1 2 3 4"
    assert world.built == [{}], "chat's client: no read-timeout or transport option"
    request = world.engine.calls[0]
    assert set(request) == {"model", "messages", "temperature", "max_tokens", "stream", "stream_options", "extra_body"}
    assert "continue_final_message" not in request["extra_body"]
    assert "add_generation_prompt" not in request["extra_body"]
    assert world.fits, "the ordinary fit (which may trim) sized it"


def test_the_chat_default_still_has_the_wall_clock_and_its_marker(world, monkeypatch):
    monkeypatch.setattr(settings, "gen_wall_clock_s", 0.05)
    world.engine.delay_s = 0.04
    out = asyncio.run(_collect(max_tokens=64))
    text = "".join(d for _k, d in out)
    assert "wall-clock guard" in text


def test_wall_clock_none_or_zero_lets_a_slow_generation_finish(world, monkeypatch):
    monkeypatch.setattr(settings, "gen_wall_clock_s", 0.05)
    world.engine.delay_s = 0.04
    for value in (None, 0, -1):
        world.engine.calls.clear()
        out = asyncio.run(_collect(max_tokens=64, wall_clock_s=value))
        assert "".join(d for _k, d in out) == "1 2 3 4", value


def test_a_positive_wall_clock_without_the_marker_stops_silently(world):
    world.engine.delay_s = 0.04

    async def run():
        out = []
        async for kind, delta in llm.stream_chat_events(
            MSGS, model_choice="fast", effort="fast", max_tokens=64, wall_clock_s=0.05, wall_clock_marker=False,
        ):
            out.append(delta)
        return out, llm.get_finish_reason()

    out, reason = asyncio.run(run())
    assert "wall-clock" not in "".join(out)
    assert len(out) < 4
    assert reason == llm.WALL_CLOCK_FINISH


def test_read_timeout_none_asks_for_the_unbounded_keepalive_client(world):
    asyncio.run(_collect(max_tokens=64, read_timeout_s=None))
    assert world.built == [{"unbounded_read": True}]
    world.built.clear()
    asyncio.run(_collect(max_tokens=64, read_timeout_s=42.0))
    assert world.built == [{"read_timeout": 42.0}]


def test_the_unbounded_client_has_no_read_timeout_and_tcp_keepalive_and_its_own_cache_slot(monkeypatch):
    llm._CLIENTS.clear()

    async def run():
        chat = llm._client(MAIN_URL)
        public = llm._client(MAIN_URL, unbounded_read=True)
        again = llm._client(MAIN_URL, unbounded_read=True)
        assert public is again, "cached"
        assert public is not chat, "the transport kind is part of the cache key"
        assert public.timeout.read is None
        assert chat.timeout.read == float(settings.llm_request_timeout)
        assert public.timeout.connect == float(settings.llm_connect_timeout)
        pool = public._client._transport._pool
        options = set(pool._socket_options)
        assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in options
        assert (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60) in options
        assert (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15) in options
        assert (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4) in options
        chat_pool = chat._client._transport._pool
        assert not chat_pool._socket_options, "chat's transport is unchanged"
        keys = [k for k in llm._CLIENTS if k[1] == MAIN_URL]
        assert {k[-1] for k in keys} == {llm.TRANSPORT_DEFAULT, llm.TRANSPORT_KEEPALIVE}
        await chat.close()
        await public.close()

    asyncio.run(run())
    llm._CLIENTS.clear()


def test_the_keepalive_socket_options_reach_a_real_connected_socket():
    """The options are applied by the transport when it dials: a real local
    listener, a real kept-alive connection, and getsockopt on the socket the
    connection pool actually holds."""
    import httpx

    seen: dict = {}

    async def run():
        async def handle(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
            await writer.drain()
            await asyncio.sleep(0.5)
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = httpx.AsyncHTTPTransport(socket_options=llm.keepalive_socket_options())
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.get(f"http://127.0.0.1:{port}/")
                assert response.status_code == 200
                sock = transport._pool.connections[0]._connection._network_stream.get_extra_info("socket")
                seen["keepalive"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
                seen["idle"] = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)
                seen["interval"] = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
                seen["cnt"] = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
    assert seen == {"keepalive": 1, "idle": 60, "interval": 15, "cnt": 4}


def test_the_unbounded_keepalive_client_completes_a_real_request_over_a_real_socket():
    """Assembler, 2026-09-14: the keepalive transport must be built from the
    httpx package the installed openai client uses (httpx2 for openai 3.x). A
    transport from the other package made every no-read-timeout generation
    fail with APIConnectionError before a byte was sent; only a real request
    through `llm._client(..., unbounded_read=True)` shows it."""
    seen: dict = {}
    body = (
        b'{"id":"c1","object":"chat.completion","created":1,"model":"m",'
        b'"choices":[{"index":0,"finish_reason":"stop",'
        b'"message":{"role":"assistant","content":"pong"}}]}'
    )

    async def run():
        async def handle(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            if length:
                await reader.readexactly(length)
            sock = writer.get_extra_info("socket")
            seen["peer"] = sock is not None
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                + b"content-length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
            await asyncio.sleep(0.3)
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        llm._CLIENTS.clear()
        client = llm._client(f"http://127.0.0.1:{port}/v1", unbounded_read=True)
        try:
            answer = await client.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "ping"}]
            )
            seen["content"] = answer.choices[0].message.content
            pool = client._client._transport._pool
            sock = pool.connections[0]._connection._network_stream.get_extra_info("socket")
            seen["keepalive"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            seen["idle"] = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()
            llm._CLIENTS.clear()

    asyncio.run(run())
    assert seen == {"peer": True, "content": "pong", "keepalive": 1, "idle": 60}


def test_continue_final_message_sends_the_continuation_body_and_never_trims(world):
    msgs = MSGS + [{"role": "assistant", "content": "1 2"}]

    async def run():
        out = []
        async for _kind, delta in llm.stream_chat_events(
            msgs, model_choice="fast", effort="fast", max_tokens=64, continue_final_message=True,
        ):
            out.append(delta)
        return out

    asyncio.run(run())
    request = world.engine.calls[0]
    assert request["extra_body"]["continue_final_message"] is True
    assert request["extra_body"]["add_generation_prompt"] is False
    assert request["messages"][-1] == {"role": "assistant", "content": "1 2"}
    assert world.fits == [], "a continuation is never sent through the trimming fit"
    # window 1000 − prompt 40 − margin 100 = 860 of room; the caller asked 64.
    assert request["max_tokens"] == 64


def test_a_continuation_with_no_room_raises_before_anything_is_sent(world, monkeypatch):
    async def full(base_url, model, messages):
        context._last_count_exact.set(True)
        return 700, 1000  # 1000 − 700 − 100 = 200 < MIN_OUTPUT_TOKENS (256)

    monkeypatch.setattr(llm.context, "count_tokens", full)

    async def run():
        async for _ in llm.stream_chat_events(
            MSGS + [{"role": "assistant", "content": "x"}], model_choice="fast", effort="fast",
            max_tokens=64, continue_final_message=True,
        ):
            pass

    with pytest.raises(llm.ContinuationRoomExhausted) as caught:
        asyncio.run(run())
    assert caught.value.room == 200
    assert world.engine.calls == []


def test_on_dispatch_fires_after_admission_never_while_waiting_for_the_lane(world):
    fired: list = []

    async def run():
        ls = admission.lanes()
        ls.normal.take(admission.ORIGIN_CHAT)  # the one NORMAL seat is taken
        world.engine.events.clear()

        def on_dispatch():
            fired.append(len(world.engine.events))
            world.engine.events.append("dispatch")

        task = asyncio.ensure_future(_collect(max_tokens=64, on_dispatch=on_dispatch, admission_patient=True))
        await asyncio.sleep(0.2)
        assert fired == [], "still waiting for admission: not dispatched"
        assert world.engine.calls == []
        ls.normal.release_nowait(admission.ORIGIN_CHAT)
        out = await asyncio.wait_for(task, 5.0)
        return out

    out = asyncio.run(run())
    assert "".join(d for _k, d in out) == "1 2 3 4"
    assert fired == [0], "fired once, before the engine call"
    assert world.engine.events == ["dispatch", "create"]


def test_a_dispatch_hook_that_raises_does_not_fail_the_request(world):
    def boom():
        raise RuntimeError("observer bug")

    out = asyncio.run(_collect(max_tokens=64, on_dispatch=boom))
    assert "".join(d for _k, d in out) == "1 2 3 4"


def test_a_patient_request_waits_where_a_chat_request_is_refused_at_the_door(world):
    async def run():
        ls = admission.lanes()
        ls.normal.take(admission.ORIGIN_CHAT)
        # ADMISSION_MAX_WAITING=0: a chat waiter is refused at once …
        with pytest.raises(admission.AdmissionRejected) as refused:
            await _collect(max_tokens=64)
        assert refused.value.reason == "capacity"
        # … and a patient one queues.
        task = asyncio.ensure_future(_collect(max_tokens=64, admission_patient=True))
        await asyncio.sleep(0.1)
        assert not task.done()
        ls.normal.release_nowait(admission.ORIGIN_CHAT)
        return await asyncio.wait_for(task, 5.0)

    out = asyncio.run(run())
    assert "".join(d for _k, d in out) == "1 2 3 4"


def test_every_engine_chunk_is_noted_as_serving_evidence(world, monkeypatch):
    noted: list = []
    monkeypatch.setattr(engine_state, "note_chunk", lambda: noted.append(1))
    asyncio.run(_collect(max_tokens=64))
    assert len(noted) == 4


def test_admission_run_id_reaches_the_admission_call(world, monkeypatch):
    seen: dict = {}
    real_run = admission.run

    async def spy(op, **kwargs):
        seen.update(kwargs)
        return await real_run(op, **kwargs)

    monkeypatch.setattr(admission, "run", spy)
    asyncio.run(_collect(max_tokens=64, admission_patient=True, admission_run_id="resp_123"))
    assert seen["run_id"] == "resp_123" and seen["patient"] is True
    seen.clear()
    asyncio.run(_collect(max_tokens=64))
    assert seen["run_id"] is None and seen["patient"] is None, "chat passes neither"


# ---------------------------------------------------------------------------
# ONLY AN EXACT COUNT MAY END A RUN (T1 review, 2026-09-14)
# ---------------------------------------------------------------------------


class _Tokenize:
    """The /tokenize endpoint: `answers` is consumed per POST — an exception to
    raise, or (status, body)."""

    def __init__(self, answers) -> None:
        self.answers = list(answers)
        self.posts: list = []

    async def post(self, url, *, json=None, timeout=None):
        import httpx

        self.posts.append({"url": url, "timeout": timeout})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


@pytest.fixture()
def estimated(world, monkeypatch):
    """count_tokens falls back to the estimate (its 5 s /tokenize timed out),
    and the estimate says the window is full."""
    async def timed_out(base_url, model, messages):
        context._last_count_exact.set(False)
        return 1_050_015, None

    monkeypatch.setattr(llm.context, "count_tokens", timed_out)
    monkeypatch.setattr(llm, "_CONTINUATION_TOKENIZE_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(settings, "public_api_continuation_tokenize_timeout_s", 120.0)

    async def big_window(base_url, model):
        return 1_000_000

    monkeypatch.setattr(llm.context, "model_window", big_window)
    return world


async def _continue(max_tokens=400_000):
    out = []
    async for _kind, delta in llm.stream_chat_events(
        MSGS + [{"role": "assistant", "content": "x" * 64}], model_choice="fast", effort="fast",
        max_tokens=max_tokens, continue_final_message=True,
    ):
        out.append(delta)
    return out


def test_an_estimate_that_leaves_no_room_never_raises_and_the_request_goes_out(estimated, monkeypatch):
    import httpx

    tokenize = _Tokenize([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")])
    monkeypatch.setattr(llm.context, "_tokenize_client", lambda: tokenize)
    assert asyncio.run(_continue()) == ["1", " 2", " 3", " 4"]
    assert len(tokenize.posts) == 2, "retried with the long timeout"
    assert all(post["timeout"].read == 120.0 for post in tokenize.posts)
    assert tokenize.posts[0]["url"] == "http://vllm-main.test:8000/tokenize"
    request = estimated.engine.calls[0]
    assert request["max_tokens"] == 400_000, "the caller's ceiling, not a guess from the estimate"
    assert llm._continuation_estimated.get() is False, "a ContextVar: the task's own copy only"
    assert metrics._counters["llm_continuation_sized_on_estimate_total"][()] == 1.0


def test_a_patient_retry_that_counts_exactly_decides_the_room(estimated, monkeypatch):
    import httpx

    tokenize = _Tokenize([httpx.ReadTimeout("slow"), (200, {"count": 700_001, "max_model_len": 1_000_000})])
    monkeypatch.setattr(llm.context, "_tokenize_client", lambda: tokenize)
    asyncio.run(_continue(max_tokens=400_000))
    # 1,000,000 − 700,001 − 100 = 299,899 of real room: the exact count sizes it.
    assert estimated.engine.calls[0]["max_tokens"] == 299_899

    full = _Tokenize([(200, {"count": 999_800, "max_model_len": 1_000_000})])
    monkeypatch.setattr(llm.context, "_tokenize_client", lambda: full)
    estimated.engine.calls.clear()
    with pytest.raises(llm.ContinuationRoomExhausted) as caught:
        asyncio.run(_continue())
    assert caught.value.room == 100 and estimated.engine.calls == []


def test_a_4xx_from_tokenize_is_conclusive_and_not_retried(estimated, monkeypatch):
    tokenize = _Tokenize([(400, {"error": "multimodal"})])
    monkeypatch.setattr(llm.context, "_tokenize_client", lambda: tokenize)
    asyncio.run(_continue(max_tokens=64))
    assert len(tokenize.posts) == 1
    assert estimated.engine.calls[0]["max_tokens"] == 64


def _bad_request(message: str = "This model's maximum context length is 1000000 tokens. However, you "
                               "requested 400000 output tokens and your prompt contains at least 600001 input tokens"):
    import httpx
    import openai

    response = httpx.Response(400, request=httpx.Request("POST", MAIN_URL + "/chat/completions"))
    return openai.BadRequestError(message, response=response, body=None)


def test_a_refused_guess_is_resent_once_with_the_engine_sizing_it(estimated, monkeypatch):
    import httpx

    tokenize = _Tokenize([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")])
    monkeypatch.setattr(llm.context, "_tokenize_client", lambda: tokenize)
    engine = estimated.engine
    real_create = engine._create

    async def refuse_a_size(**kwargs):
        if kwargs.get("max_tokens") is not None:
            engine.calls.append(kwargs)
            raise _bad_request()
        return await real_create(**kwargs)

    engine.chat.completions.create = refuse_a_size
    assert asyncio.run(_continue()) == ["1", " 2", " 3", " 4"]
    sizes = [call.get("max_tokens") for call in engine.calls]
    assert sizes[-1] is None and all(size == 400_000 for size in sizes[:-1]), sizes
    assert engine.calls[-1]["extra_body"]["continue_final_message"] is True


def test_a_refusal_with_no_room_at_all_propagates_and_an_exact_plain_or_non_size_refusal_is_never_resent(
    estimated, monkeypatch
):
    import httpx
    import openai

    tokenize = _Tokenize([httpx.ReadTimeout("slow")] * 4)
    monkeypatch.setattr(llm.context, "_tokenize_client", lambda: tokenize)
    engine = estimated.engine

    async def always_refuse(**kwargs):
        engine.calls.append(kwargs)
        raise _bad_request()

    engine.chat.completions.create = always_refuse
    with pytest.raises(openai.BadRequestError):
        asyncio.run(_continue())
    assert [call.get("max_tokens") for call in engine.calls][-1] is None, "one engine-sized attempt, refused"

    # Not a continuation: a 400 propagates with no engine-sized retry.
    engine.calls.clear()
    with pytest.raises(openai.BadRequestError):
        asyncio.run(_collect(max_tokens=64))
    assert all(call.get("max_tokens") == 64 for call in engine.calls)

    # A refusal for anything but size (a corrupt image, say) is not re-sent:
    # without max_tokens it could be accepted and write past the ceiling.
    tokenize.answers[:] = [httpx.ReadTimeout("slow")] * 2
    engine.calls.clear()

    async def refuse_the_payload(**kwargs):
        engine.calls.append(kwargs)
        raise _bad_request("Invalid image: cannot identify image file")

    engine.chat.completions.create = refuse_the_payload
    with pytest.raises(openai.BadRequestError):
        asyncio.run(_continue())
    assert engine.calls and all(call.get("max_tokens") == 400_000 for call in engine.calls)
    engine.chat.completions.create = always_refuse

    # An EXACT continuation: its size was counted, so a refusal is the answer.
    exact = _Tokenize([(200, {"count": 700_001, "max_model_len": 1_000_000})])
    monkeypatch.setattr(llm.context, "_tokenize_client", lambda: exact)
    engine.calls.clear()
    with pytest.raises(openai.BadRequestError):
        asyncio.run(_continue())
    assert engine.calls and all(call.get("max_tokens") == 299_899 for call in engine.calls)
