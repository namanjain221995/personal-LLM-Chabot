"""The CPU speech replica: prefer the GPU replicas, overflow to the CPU only when they are all busy,
and never send the CPU a clip that would miss its deadline at the CPU's measured speed.

app/asr.RoutedProvider, THE CPU REPLICA. Stand-in engines only: nothing here opens a socket.
"""
from __future__ import annotations

import asyncio
import io
import time
import wave

import httpx
import pytest

from app import asr, metrics
from app.config import Settings, settings


def wav_bytes(seconds: float, *, list_chunk: bool = False) -> bytes:
    """A 16 kHz mono PCM16 WAV of `seconds` of silence, as dictation and video build them."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(seconds * 16000))
    data = buf.getvalue()
    if not list_chunk:
        return data
    # ffmpeg writes a LIST chunk before `data`; a 44-byte assumption would misread it.
    riff, rest = data[:12], data[12:]
    fmt_end = 8 + int.from_bytes(rest[4:8], "little")
    info = b"INFOISFT\x0e\x00\x00\x00Lavf62.3.100\x00\x00"
    listing = b"LIST" + len(info).to_bytes(4, "little") + info
    body = rest[:fmt_end] + listing + rest[fmt_end:]
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WAVE" + body


class Engine:
    """A replica stand-in that can be held busy, made to fail, and costed like the real ones."""

    name = "whisper"
    model = "openai/whisper-large-v3"

    def __init__(self, base_url: str, *, tier: str = "gpu", fail: bool = False, timeout_s: float = 600.0,
                 fixed_s: float = 0.0, s_per_audio_s: float = 0.0) -> None:
        self.base_url = base_url
        self.tier = tier
        self.fail = fail
        self.timeout_s = timeout_s
        self.fixed_s = fixed_s
        self.s_per_audio_s = s_per_audio_s
        self.calls = 0
        self.release = asyncio.Event()
        self.release.set()

    def decode_cost_s(self, seconds: float) -> float:
        return asr.VLLMAudioProvider.decode_cost_s(self, seconds)  # the production formula

    async def _answer(self):
        self.calls += 1
        if self.fail:
            raise asr.ASRUnavailable(f"{self.base_url} is down")
        await self.release.wait()
        return asr.Transcript(text=self.base_url, language=None, language_code=None,
                              provider=self.name, model=self.model, engine_ms=1)

    async def transcribe(self, audio, *, filename, content_type, language=""):
        return await self._answer()

    async def transcribe_window(self, audio, *, filename, content_type, timeout_s=None, no_speech_check=False):
        return await self._answer()

    async def health(self) -> bool:
        return not self.fail


def cpu(**kw) -> Engine:
    # The shipped defaults, ASR_CPU_FIXED_S=8.5 + ASR_CPU_S_PER_AUDIO_S=0.45 (docs/voice/CPU-REPLICA.md).
    return Engine("http://cpu/v1", tier="cpu", fixed_s=kw.pop("fixed_s", 8.5),
                  s_per_audio_s=kw.pop("s_per_audio_s", 0.45), **kw)


async def hold(router: asr.RoutedProvider, engine: Engine, clip: bytes) -> asyncio.Task:
    """Park one clip on `engine` (it must be the one the router picks) and return its task."""
    engine.release.clear()
    task = asyncio.create_task(router.transcribe(clip, filename="a.wav", content_type="audio/wav"))
    for _ in range(5):
        await asyncio.sleep(0)
    return task


# -- clip_seconds --------------------------------------------------------------------------------

def test_clip_seconds_reads_a_wav_header_exactly():
    assert asr.clip_seconds(wav_bytes(12.5)) == pytest.approx(12.5)


def test_clip_seconds_walks_past_a_list_chunk():
    assert asr.clip_seconds(wav_bytes(3.0, list_chunk=True)) == pytest.approx(3.0)


def test_clip_seconds_trusts_the_bytes_present_over_a_streamed_header():
    data = bytearray(wav_bytes(2.0))
    data[40:44] = (0xFFFFFFFF).to_bytes(4, "little")  # "unknown" data size, as a pipe writes it
    assert asr.clip_seconds(bytes(data)) == pytest.approx(2.0)
    truncated = wav_bytes(10.0)[: 44 + 32000]  # the header claims 10 s, 1 s is present
    assert asr.clip_seconds(truncated) == pytest.approx(1.0)


def test_clip_seconds_is_none_for_anything_that_is_not_a_wav():
    assert asr.clip_seconds(b"\x1aE\xdf\xa3" + b"\x00" * 100) is None  # WebM
    assert asr.clip_seconds(b"") is None
    assert asr.clip_seconds(b"RIFF\x00\x00\x00\x00WAVE") is None  # no fmt, no data


# -- the cost model ------------------------------------------------------------------------------

def test_a_gpu_replica_is_costed_at_the_loaded_gpu_rate_exactly_as_before():
    gpu = asr.VLLMAudioProvider(base_url="http://g/v1", model="m")
    assert gpu.decode_cost_s(100.0) == pytest.approx(100.0 * asr.LOADED_DECODE_S_PER_AUDIO_S)
    # and the retry rule it feeds is unchanged for a GPU replica
    assert asr._retry_affordable(100.0, 73.0) is True
    assert asr._retry_affordable(100.0, 72.9) is False


def test_the_cpu_replica_is_costed_with_its_own_measured_numbers():
    replica = asr.VLLMAudioProvider(base_url="http://c/v1", model="m", tier="cpu", fixed_s=4.5, s_per_audio_s=0.3)
    assert replica.decode_cost_s(30.0) == pytest.approx(4.5 + 9.0)
    assert asr._retry_affordable(30.0, 13.4, cost_s=replica.decode_cost_s(30.0)) is False
    assert asr._retry_affordable(30.0, 13.6, cost_s=replica.decode_cost_s(30.0)) is True


# -- routing ---------------------------------------------------------------------------------------

def test_without_a_cpu_replica_the_order_is_exactly_what_it_was():
    a, b = Engine("http://a/v1"), Engine("http://b/v1")
    router = asr.RoutedProvider([a, b])
    router._active[0] = 1
    assert router._order(10.0, 600.0) == [1, 0]
    assert router._order() == [1, 0]


def test_a_free_gpu_replica_takes_the_clip_and_the_cpu_is_only_a_backup():
    a, b, c = Engine("http://a/v1"), Engine("http://b/v1"), cpu()
    router = asr.RoutedProvider([a, b], [c])
    router._active[0] = 1  # one GPU busy, the other free
    assert router._order(10.0, 600.0) == [1, 0, 2]

    result = asyncio.run(router.transcribe(wav_bytes(10.0), filename="a.wav", content_type="audio/wav"))
    assert result.text == "http://b/v1"
    assert c.calls == 0


def test_when_every_gpu_replica_is_busy_the_cpu_replica_takes_the_clip():
    a, b, c = Engine("http://a/v1"), Engine("http://b/v1"), cpu()
    router = asr.RoutedProvider([a, b], [c])

    async def drive():
        first = await hold(router, a, wav_bytes(20.0))
        second = await hold(router, b, wav_bytes(20.0))
        assert router._active[0] == 1 and router._active[1] == 1
        third = await router.transcribe(wav_bytes(10.0), filename="c.wav", content_type="audio/wav")
        a.release.set(); b.release.set()
        await first; await second
        return third

    assert asyncio.run(drive()).text == "http://cpu/v1"
    assert c.calls == 1
    assert router._active == {0: 0, 1: 0, 2: 0}


def test_a_busy_cpu_replica_is_not_queued_behind_the_clip_goes_to_the_freest_gpu():
    a, b, c = Engine("http://a/v1"), Engine("http://b/v1"), cpu()
    router = asr.RoutedProvider([a, b], [c])
    router._active.update({0: 2, 1: 1, 2: 1})  # both GPUs busy, the CPU decoding one already
    assert router._order(10.0, 600.0) == [1, 0]


def test_a_clip_that_would_miss_its_deadline_on_the_cpu_is_never_sent_there():
    a, c = Engine("http://a/v1"), cpu()
    router = asr.RoutedProvider([a], [c], cpu_margin=1.5)
    router._active[0] = 1
    # A 30 s session window with a 120 s window timeout: (8.5 + 13.5) x 1.5 = 33 s -> fits.
    assert router._order(30.0, 120.0) == [1, 0]
    # The same window with a 30 s timeout does not fit: it waits for the GPU as it always did.
    assert router._order(30.0, 30.0) == [0]
    # A 300 s clip against a 600 s deadline: (8.5 + 135) x 1.5 = 215.25 -> fits.
    assert router._order(300.0, 600.0) == [1, 0]
    # ...and not at 2.2 s per second of audio: (4.5 + 660) x 1.5 > 600.
    slow = asr.RoutedProvider([a], [cpu(s_per_audio_s=2.2)], cpu_margin=1.5)
    slow._active[0] = 1
    assert slow._order(300.0, 600.0) == [0]


def test_a_clip_of_unknown_length_is_costed_as_the_longest_clip_an_engine_accepts():
    a = Engine("http://a/v1")
    router = asr.RoutedProvider([a], [cpu()], longest_clip_s=600.0, cpu_margin=1.5)
    router._active[0] = 1
    # (8.5 + 270) x 1.5 = 417.75 <= 600: a WebM of any length the engine accepts fits.
    assert router._order(None, 600.0) == [1, 0]
    # With a 120 s deadline it does not, and a WebM is never gambled on.
    assert router._order(None, 120.0) == [0]


def test_a_session_window_is_judged_against_its_own_timeout_not_the_engine_timeout():
    a, c = Engine("http://a/v1"), cpu()
    router = asr.RoutedProvider([a], [c])
    assert router._deadline_s({"timeout_s": 45.0}) == 45.0
    assert router._deadline_s({}) == 600.0  # the CPU replica's ASR_TIMEOUT_S


def test_a_standing_down_gpu_fleet_overflows_to_the_cpu_too():
    """A GPU replica standing down after a failure cannot take the clip either."""
    a, b, c = Engine("http://a/v1"), Engine("http://b/v1"), cpu()
    router = asr.RoutedProvider([a, b], [c])
    import time as _time

    router._down_until[0] = _time.monotonic() + 60
    router._active[1] = 1
    assert router._order(10.0, 600.0) == [2, 1, 0]


def test_when_every_gpu_replica_fails_the_cpu_replica_answers_last():
    """A node rebooting costs dictation speed, not dictation."""
    a, b = Engine("http://a/v1", fail=True), Engine("http://b/v1", fail=True)
    c = cpu()
    router = asr.RoutedProvider([a, b], [c])
    result = asyncio.run(router.transcribe(wav_bytes(5.0), filename="a.wav", content_type="audio/wav"))
    assert result.text == "http://cpu/v1"
    assert (a.calls, b.calls, c.calls) == (1, 1, 1)


def test_a_failing_cpu_replica_falls_back_to_the_gpu_queue():
    a, c = Engine("http://a/v1"), cpu(fail=True)
    router = asr.RoutedProvider([a], [c])

    async def drive():
        parked = await hold(router, a, wav_bytes(20.0))
        follower = asyncio.create_task(
            router.transcribe(wav_bytes(5.0), filename="b.wav", content_type="audio/wav")
        )
        for _ in range(5):
            await asyncio.sleep(0)
        a.release.set()
        await parked
        return await follower

    assert asyncio.run(drive()).text == "http://a/v1"
    assert c.calls == 1
    assert router.stats()[1]["available"] is False, "the failed CPU replica is stood down"


def test_a_timeout_on_the_cpu_replica_is_not_resent_to_a_gpu():
    """As on the GPU replicas: the engine still has the clip, and the caller's deadline has passed."""

    class Slow(Engine):
        async def _answer(self):
            self.calls += 1
            raise asr.ASRTimeout("no answer within 120s")

    a, c = Engine("http://a/v1"), Slow("http://cpu/v1", tier="cpu", fixed_s=8.5, s_per_audio_s=0.45)
    router = asr.RoutedProvider([a], [c])
    router._active[0] = 1
    with pytest.raises(asr.ASRTimeout):
        asyncio.run(router.transcribe(wav_bytes(5.0), filename="a.wav", content_type="audio/wav"))
    assert a.calls == 0


def test_stats_name_each_replicas_tier_and_keep_the_gpu_indices():
    """/v1's counted_in_dictation_routing finds a GPU replica's index by URL through stats()."""
    router = asr.RoutedProvider([Engine("http://a/v1"), Engine("http://b/v1")], [cpu()])
    rows = router.stats()
    assert [(r["endpoint"], r["tier"]) for r in rows] == [
        ("http://a/v1", "gpu"), ("http://b/v1", "gpu"), ("http://cpu/v1", "cpu"),
    ]


def test_the_overflow_outcomes_are_a_closed_metric_vocabulary():
    assert metrics.ASR_CPU_OVERFLOW_OUTCOMES == {"sent", "cpu_busy", "cpu_down", "too_long"}
    assert metrics._clean({"outcome": "sent"}, "asr_cpu_overflow_total") == (("outcome", "sent"),)
    assert metrics._clean({"outcome": "typo"}, "asr_cpu_overflow_total") == (("outcome", "other"),)
    assert metrics._clean({"tier": "cpu"}, "asr_route_total") == (("tier", "cpu"),)


# -- configuration -------------------------------------------------------------------------------

def test_the_provider_builds_the_cpu_replica_from_its_own_key(monkeypatch):
    monkeypatch.setattr(settings, "asr_base_urls", ("http://gpu-a/v1", "http://gpu-b/v1"))
    monkeypatch.setattr(settings, "asr_cpu_base_urls", ("http://cpu/v1", "http://gpu-a/v1"))
    asr.set_provider(None)
    try:
        rows = asr.provider().stats()
        # a URL listed as both is a GPU replica, once
        assert [(r["endpoint"], r["tier"]) for r in rows] == [
            ("http://gpu-a/v1", "gpu"), ("http://gpu-b/v1", "gpu"), ("http://cpu/v1", "cpu"),
        ]
        replica = asr.provider()._engines[2]
        assert replica.tier == "cpu"
        assert replica.fixed_s == settings.asr_cpu_fixed_s
        assert replica.s_per_audio_s == settings.asr_cpu_s_per_audio_s
    finally:
        asr.set_provider(None)


def test_no_cpu_key_means_no_cpu_replica(monkeypatch):
    monkeypatch.setattr(settings, "asr_base_urls", ("http://gpu-a/v1",))
    monkeypatch.setattr(settings, "asr_cpu_base_urls", ())
    asr.set_provider(None)
    try:
        assert [r["tier"] for r in asr.provider().stats()] == ["gpu"]
    finally:
        asr.set_provider(None)


def test_a_cpu_replica_listed_twice_is_one_replica_with_one_router_slot(monkeypatch):
    """Verifier, 2026-09-30 (finding 4): "http://cpu/v1, http://cpu/v1/" gave one replica, which
    decodes one clip at a time, two router slots, so a second clip queued inside it."""
    monkeypatch.setenv("ASR_CPU_BASE_URLS", "http://cpu/v1, http://cpu/v1/ ,, http://cpu-b/v1,http://cpu/v1")
    fresh = Settings()
    assert fresh.asr_cpu_base_urls == ("http://cpu/v1", "http://cpu-b/v1")
    monkeypatch.setattr(settings, "asr_base_urls", ("http://gpu-a/v1",))
    monkeypatch.setattr(settings, "asr_cpu_base_urls", fresh.asr_cpu_base_urls)
    asr.set_provider(None)
    try:
        assert [(r["endpoint"], r["tier"]) for r in asr.provider().stats()] == [
            ("http://gpu-a/v1", "gpu"), ("http://cpu/v1", "cpu"), ("http://cpu-b/v1", "cpu"),
        ]
    finally:
        asr.set_provider(None)


# -- the replica's busy refusal, seen from the router ------------------------------------------------

def test_a_decode_the_router_let_go_of_meets_a_busy_refusal_and_the_clip_takes_the_gpu_queue(monkeypatch):
    """Verifier, 2026-09-30 (finding 1). The router's only picture of the CPU replica is its own
    in-flight count, so after a cancelled call it offers the next overflow clip to a replica whose
    one-clip lock the abandoned decode still holds (live: the next 30 s window timed out at 120 s
    behind it). The replica now answers such a clip 503 at once (compose/whisper-cpu/server.py,
    test_whisper_cpu_server.py), and the router takes it to the GPU queue in the same call."""
    calls = []

    async def replica(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            await asyncio.sleep(3600)  # the long decode its caller lets go of, below
        return httpx.Response(503, json={"detail": "busy: the CPU replica is decoding another clip"})

    transport = httpx.MockTransport(replica)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: real(*args, **{**kwargs, "transport": transport}))
    replica_cpu = asr.VLLMAudioProvider(base_url="http://cpu:30008/v1", model="openai/whisper-large-v3",
                                        timeout_s=600.0, tier="cpu", fixed_s=8.5, s_per_audio_s=0.45)
    a, b = Engine("http://a/v1"), Engine("http://b/v1")
    router = asr.RoutedProvider([a, b], [replica_cpu])

    async def drive():
        first = await hold(router, a, wav_bytes(20.0))
        second = await hold(router, b, wav_bytes(20.0))
        abandoned = asyncio.create_task(router.transcribe(wav_bytes(200.0), filename="l.wav", content_type="audio/wav"))
        while not calls:
            await asyncio.sleep(0.01)
        assert router._active[2] == 1
        abandoned.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned
        # The premise: the router believes the replica is free, and offers it the next clip first.
        assert router._active[2] == 0 and router.stats()[2]["available"] is True
        assert router._order(10.0, 120.0)[0] == 2
        started = time.monotonic()
        window = asyncio.create_task(router.transcribe_window(wav_bytes(10.0), filename="w.wav",
                                                              content_type="audio/wav", timeout_s=120.0))
        while len(calls) < 2:
            await asyncio.sleep(0.01)
        refused_after = time.monotonic() - started
        a.release.set(); b.release.set()
        result = await window
        await first; await second
        return result, refused_after

    result, refused_after = asyncio.run(drive())
    assert result.text == "http://a/v1", "the window finished on a GPU replica, not behind the decode"
    assert refused_after < 5.0
    assert router.stats()[2]["available"] is False, "the refusing replica stands down"
    assert router._active == {0: 0, 1: 0, 2: 0}


# -- the legacy dictation pool ---------------------------------------------------------------------

#: The legacy path's container: WebM, whose length is unknown until it is decoded.
LEGACY_CLIP = b"\x1aE\xdf\xa3" + b"\x00" * 100


def _legacy_burst(monkeypatch, router: asr.RoutedProvider, engines, clips: int):
    """`clips` legacy dictations at once through asr.transcribe (the pool, then the router), each
    engine holding what it is given. Returns the router's in-flight counts once every admitted
    clip is placed, the pool's lent count then, and how many callers were told busy."""
    monkeypatch.setattr(settings, "asr_queue_wait_s", 0.2)
    asr.POOL.reset_for_tests()
    asr.set_provider(router)

    async def drive():
        for engine in engines:
            engine.release.clear()
        tasks = [
            asyncio.create_task(asr.transcribe(LEGACY_CLIP, filename=f"{i}.webm", content_type="audio/webm"))
            for i in range(clips)
        ]
        for _ in range(10):
            await asyncio.sleep(0)
        placed, lent = dict(router._active), getattr(asr.POOL, "lent", 0)
        await asyncio.sleep(0.6)  # past the pool's queue wait: whoever still waits is refused
        for engine in engines:
            engine.release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return placed, lent, sum(isinstance(r, asr.ASRBusy) for r in results)

    try:
        return asyncio.run(drive())
    finally:
        asr.set_provider(None)
        asr.POOL.reset_for_tests()


@pytest.fixture()
def two_gpus_one_cpu(monkeypatch):
    monkeypatch.setattr(settings, "asr_max_concurrent", 2)
    monkeypatch.setattr(settings, "asr_base_urls", ("http://a/v1", "http://b/v1"))
    monkeypatch.setattr(settings, "asr_cpu_base_urls", ("http://cpu/v1",))


def test_the_pools_permits_are_the_gpu_replicas_alone(monkeypatch):
    monkeypatch.setattr(settings, "asr_max_concurrent", 2)
    monkeypatch.setattr(settings, "asr_base_urls", ("http://a/v1", "http://b/v1"))
    monkeypatch.setattr(settings, "asr_cpu_base_urls", ())
    assert asr.POOL._size() == 4
    monkeypatch.setattr(settings, "asr_cpu_base_urls", ("http://cpu/v1",))
    assert asr.POOL._size() == 4, "a CPU replica's slot is lent (below), never a permit"


def test_the_dictation_pool_lends_the_cpu_slot_to_the_clip_the_cpu_takes(monkeypatch, two_gpus_one_cpu):
    """With the CPU replica free, the pool holds what it did with the slot counted: five at once,
    one decoding and one ready per GPU replica, and one on the CPU."""
    a, b, c = Engine("http://a/v1"), Engine("http://b/v1"), cpu()
    placed, lent, busy = _legacy_burst(monkeypatch, asr.RoutedProvider([a, b], [c]), (a, b, c), clips=6)
    assert placed == {0: 2, 1: 2, 2: 1} and lent == 1
    assert busy == 1


def test_the_dictation_pool_admits_no_extra_clip_while_the_cpu_is_standing_down(monkeypatch, two_gpus_one_cpu):
    """Verifier, 2026-09-30 (finding 5): with the CPU slot counted in the semaphore, the fifth
    clip was admitted while the CPU replica stood down and went to a GPU replica as its third in
    flight ({0: 3, 1: 2, 2: 0}), past ASR_MAX_CONCURRENT's one decoding, one ready."""
    a, b, c = Engine("http://a/v1"), Engine("http://b/v1"), cpu(fail=True)
    router = asr.RoutedProvider([a, b], [c])
    router._down_until[2] = time.monotonic() + 600
    placed, lent, busy = _legacy_burst(monkeypatch, router, (a, b), clips=5)
    assert placed == {0: 2, 1: 2, 2: 0} and lent == 0
    assert busy == 1


def test_the_dictation_pool_admits_no_extra_clip_while_the_cpu_decodes_someone_elses(monkeypatch, two_gpus_one_cpu):
    """A video or session window on the CPU replica is not the pool's: its slot is not lent."""
    a, b, c = Engine("http://a/v1"), Engine("http://b/v1"), cpu()
    router = asr.RoutedProvider([a, b], [c])
    router._active[2] = 1  # a window from another path, decoding there
    placed, lent, busy = _legacy_burst(monkeypatch, router, (a, b), clips=5)
    assert placed == {0: 2, 1: 2, 2: 1} and lent == 0
    assert busy == 1


def test_a_clip_admitted_after_a_wait_that_the_cpu_takes_hands_its_permit_on(two_gpus_one_cpu):
    a, b, c = Engine("http://a/v1"), Engine("http://b/v1"), cpu()
    router = asr.RoutedProvider([a, b], [c])
    router._active.update({0: 2, 1: 2, 2: 1})  # every permit's clip placed; the CPU busy with a window
    asr.POOL.reset_for_tests()
    asr.set_provider(router)

    async def drive():
        sem = asr.POOL._semaphore()
        for _ in range(4):
            await sem.acquire()
        entered, leave, seen = asyncio.Event(), asyncio.Event(), {}

        async def dictation():
            async with asr.POOL:
                seen.update(handed_on=sem._value, lent=asr.POOL.lent)
                entered.set()
                await leave.wait()

        task = asyncio.create_task(dictation())
        for _ in range(5):
            await asyncio.sleep(0)
        assert asr.POOL.waiting == 1
        router._active.update({0: 1, 2: 0})  # the window on the CPU ends, and one GPU clip
        sem.release()                       # ...whose permit wakes the waiter
        await entered.wait()
        leave.set()
        await task
        return seen["handed_on"], seen["lent"], asr.POOL.lent, sem._value

    try:
        handed_on, lent, lent_after, value_after = asyncio.run(drive())
    finally:
        asr.set_provider(None)
        asr.POOL.reset_for_tests()
    # Every GPU replica still has a clip and the CPU is free: the router sends this clip there, so
    # it holds the lent slot and its permit went back for the next caller.
    assert (handed_on, lent) == (1, 1)
    assert (lent_after, value_after) == (0, 1)


def test_the_pools_question_to_the_router_is_the_routers_rule_and_counts_nothing():
    a, b, c = Engine("http://a/v1"), Engine("http://b/v1"), cpu()
    router = asr.RoutedProvider([a, b], [c])
    before = dict(metrics._counters.get("asr_cpu_overflow_total", {}))
    assert router.cpu_takes_next() is False  # a GPU replica is free
    router._active.update({0: 1, 1: 1})
    assert router.cpu_takes_next() is True
    router._active[2] = 1
    assert router.cpu_takes_next() is False  # the CPU replica is busy
    router._active[2] = 0
    router._down_until[2] = time.monotonic() + 60
    assert router.cpu_takes_next() is False  # standing down
    slow = asr.RoutedProvider([a], [cpu(s_per_audio_s=2.2)])
    slow._active[0] = 1
    assert slow.cpu_takes_next() is False  # a clip of unknown length would miss the engine timeout
    assert asr.RoutedProvider([a]).cpu_takes_next() is False  # no CPU replica
    assert dict(metrics._counters.get("asr_cpu_overflow_total", {})) == before
