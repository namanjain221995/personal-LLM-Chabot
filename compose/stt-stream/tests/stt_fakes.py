"""A recognizer that behaves like sherpa-onnx's OnlineRecognizer where the
engine depends on it, and understands a toy language instead of speech.

THE TOY LANGUAGE. A token is a run of TOKEN samples (160 ms) that all hold the
same non-zero int16 value, the entry for that value in VOCAB; zeros are
silence. `speech()` builds such audio from words, so a test can say what was
spoken and check exactly what came back. Runs are found wherever they start,
not on a fixed grid, so the silence the engine puts in front of a stream (and
the seed of a renewed stream) cannot shift a word out of recognition.

WHAT IS MODELLED, because the engine's correctness rests on it (each point
was measured on the real models on the worker, 2026-09-29):

* `is_ready` wants a whole chunk PLUS a right context (lookahead), and
  `input_finished` does not relax that: the real model drops the last word at
  a flush unless the engine pads silence after it.
* a token is emitted in the decode step that completes it, stamped with the
  time its run began (seconds since the stream's start);
* endpoint rules 1-3 as sherpa-onnx evaluates them: trailing silence counts
  since the last token, utterance length since the stream's start, and rule 2
  needs some non-silence first;
* text is the tokens joined, stripped; tokens without a leading space continue
  the previous word ("sau" + "ce").

ONE recognizer serves every decode thread of a profile (build spec 8B), so
the fake is safe for concurrent decode_streams calls on disjoint streams and
records which threads decoded each stream. It also records every reset: the
engine must never call one.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

SAMPLE_RATE = 16000
#: Samples per token of the toy language (160 ms).
TOKEN = 2560

#: int16 value -> token. Leading space = a new word; no space = continues one.
VOCAB: Dict[int, str] = {}
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
         "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango",
         "uniform", "victor", "whiskey", "xray", "yankee", "zulu", "secretword"]
for _i, _w in enumerate(WORDS):
    VOCAB[1000 + 16 * _i] = " " + _w
#: A word in two pieces (" sau" starts it, "ce" continues it) and punctuation,
#: which attaches to the word before it.
PIECES = {" sau": 3000, "ce": 3016, ",": 3032, ".": 3048}
for _p, _v in PIECES.items():
    VOCAB[_v] = _p
#: A word spoken so softly (int16 160, -46 dBFS) that no level rule hears its
#: onset: the recognizer still does.
VOCAB[160] = " hush"
VALUE: Dict[str, int] = {tok.strip(): v for v, tok in VOCAB.items()}


def speech(parts: Sequence) -> np.ndarray:
    """int16 PCM from a script: a str is one token lasting TOKEN samples, a
    float is that many seconds of silence (rounded to whole tokens)."""
    pieces: List[np.ndarray] = []
    for part in parts:
        if isinstance(part, str):
            pieces.append(np.full(TOKEN, VALUE[part], dtype=np.int16))
        else:
            n = int(round(float(part) * SAMPLE_RATE / TOKEN))
            pieces.append(np.zeros(n * TOKEN, dtype=np.int16))
    return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.int16)


def positions(parts: Sequence) -> List[Tuple[str, int]]:
    """Client sample position of every word in a speech() script, in order."""
    out, at = [], 0
    for part in parts:
        if isinstance(part, str):
            out.append((part, at))
            at += TOKEN
        else:
            at += int(round(float(part) * SAMPLE_RATE / TOKEN)) * TOKEN
    return out


@dataclass
class FakeResult:
    text: str
    tokens: List[str]
    timestamps: List[float]
    start_time: float


class FakeStream:
    def __init__(self) -> None:
        self.audio = np.zeros(0, dtype=np.int16)
        self.finished = False
        self.options: Dict[str, str] = {}
        self.processed = 0
        self.tokens: List[Tuple[str, float]] = []
        self.run_value = 0       # the value of the run the last step ended inside (0: silence)
        self.run_start = 0       # where that run began
        self.run_end = 0         # where it ends so far
        self.run_emitted = 0     # tokens already emitted from it
        self.voice_end = 0       # one past the last non-zero sample decoded
        self.decoded_by: Set[str] = set()

    def accept_waveform(self, sample_rate: float, samples) -> None:
        assert sample_rate == SAMPLE_RATE
        assert not self.finished, "audio after input_finished"
        pcm = np.round(np.asarray(samples, dtype=np.float64) * 32768.0).astype(np.int32)
        self.audio = np.concatenate([self.audio, np.clip(pcm, -32768, 32767).astype(np.int16)])

    def input_finished(self) -> None:
        self.finished = True

    def set_option(self, key: str, value: str) -> None:
        self.options[key] = value

    def get_option(self, key: str) -> str:
        return self.options.get(key, "")


class FakeRecognizer:
    def __init__(self, chunk_ms: int = 160, lookahead_ms: int = 160, rule1_s: float = 2.4,
                 rule2_s: float = 0.6, rule3_s: float = 3600.0, with_timestamps: bool = True,
                 fail_decode: bool = False):
        self.chunk = SAMPLE_RATE * chunk_ms // 1000
        self.lookahead = SAMPLE_RATE * lookahead_ms // 1000
        self.rule1_s, self.rule2_s, self.rule3_s = rule1_s, rule2_s, rule3_s
        self.fail_decode = fail_decode
        #: When set, decode_streams waits for it: a test holds the decoder
        #: back while audio queues up, to reach a state on purpose.
        self.gate: Optional[threading.Event] = None
        #: When set, every decode_streams call first takes one permit: a test
        #: grants N decode steps and the decoder stops after them, mid-backlog.
        self.permits: Optional[threading.Semaphore] = None
        self.streams: List[FakeStream] = []
        self.batches: List[int] = []
        self.resets = 0
        self._lock = threading.Lock()
        if not with_timestamps:
            # The engine must work from get_result alone, the spec's fallback.
            self.get_result_all = None  # type: ignore[assignment]

    # -- the sherpa-onnx surface the engine uses -------------------------------

    def create_stream(self) -> FakeStream:
        stream = FakeStream()
        with self._lock:
            self.streams.append(stream)
        return stream

    def is_ready(self, s: FakeStream) -> bool:
        return len(s.audio) - s.processed >= self.chunk + self.lookahead

    def decode_streams(self, streams: Sequence[FakeStream]) -> None:
        if self.gate is not None:
            assert self.gate.wait(10), "the test never opened the gate"
        if self.permits is not None:
            assert self.permits.acquire(timeout=10), "the test never granted another decode step"
        if self.fail_decode:
            raise RuntimeError("fake decode failure")
        with self._lock:
            self.batches.append(len(streams))
        name = threading.current_thread().name
        for s in streams:
            assert self.is_ready(s), "decode_streams on a stream that is not ready"
            s.decoded_by.add(name)
            self._step(s)

    def decode_stream(self, s: FakeStream) -> None:
        self.decode_streams([s])

    def get_result(self, s: FakeStream) -> str:
        return "".join(tok for tok, _ in s.tokens).strip()

    def get_result_all(self, s: FakeStream) -> FakeResult:  # noqa: F811 - replaced when disabled
        return FakeResult(text="".join(tok for tok, _ in s.tokens).strip(), tokens=[tok for tok, _ in s.tokens],
                          timestamps=[t for _, t in s.tokens], start_time=0.0)

    def is_endpoint(self, s: FakeStream) -> bool:
        utterance = s.processed / SAMPLE_RATE
        trailing = 0.0 if s.run_value else (s.processed - s.voice_end) / SAMPLE_RATE
        contains_speech = utterance > trailing + 1e-9
        return (
            trailing >= self.rule1_s - 1e-9
            or (contains_speech and trailing >= self.rule2_s - 1e-9)
            or utterance >= self.rule3_s - 1e-9
        )

    def reset(self, s: FakeStream) -> None:
        with self._lock:
            self.resets += 1
        raise AssertionError("the engine must never reset a stream (it renews it instead)")

    # -- the toy decoder ------------------------------------------------------

    def _step(self, s: FakeStream) -> None:
        base = s.processed
        seg = s.audio[base:base + self.chunk]
        s.processed += len(seg)
        cuts = np.flatnonzero(np.diff(seg)) + 1
        for a, b in zip(np.concatenate(([0], cuts)), np.concatenate((cuts, [len(seg)]))):
            value = int(seg[a])
            at = base + int(a)
            if value != s.run_value or at != s.run_end:
                s.run_value, s.run_start, s.run_emitted = value, at, 0
            s.run_end = base + int(b)
            if not value:
                continue
            s.voice_end = s.run_end
            complete = (s.run_end - s.run_start) // TOKEN
            for k in range(s.run_emitted, complete):
                s.tokens.append((VOCAB.get(value, " ?"), (s.run_start + k * TOKEN) / SAMPLE_RATE))
            s.run_emitted = max(s.run_emitted, complete)



def fake_factory(*, lookahead_ms: int = 160, with_timestamps: bool = True, fail_decode: bool = False,
                 rule3_s: float = 3600.0, made: Optional[List[FakeRecognizer]] = None):
    """A recognizer factory for create_app: the engine calls it once per profile."""

    def build(profile, settings) -> FakeRecognizer:
        recognizer = FakeRecognizer(chunk_ms=profile.chunk_ms, lookahead_ms=lookahead_ms,
                                    rule2_s=settings.endpoint_s, rule3_s=rule3_s,
                                    with_timestamps=with_timestamps, fail_decode=fail_decode)
        if made is not None:
            made.append(recognizer)
        return recognizer

    return build
