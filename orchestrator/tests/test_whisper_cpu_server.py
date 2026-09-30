"""compose/whisper-cpu/server.py speaks compose/whisper/server.py's contract, exactly.

Two halves. The STATIC half reads both files and holds them together — the routes, the form
fields, every response key, the gate's threshold and the limits' variables, the pinned model — so
a change to the GPU replica's contract fails here until the CPU replica follows it. The LIVE half
runs the CPU server in-process against a stand-in decoder that speaks wcpp-worker's pipe protocol,
and checks each answer's shape and each refusal's status.

No model, no GPU and no whisper.cpp: the decoder is a 40-line Python script.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import textwrap
import time
import wave
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GPU_SERVER = REPO / "compose" / "whisper" / "server.py"
CPU_SERVER = REPO / "compose" / "whisper-cpu" / "server.py"


# -- the static half -----------------------------------------------------------------------------

def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


def _dict_keys(node: ast.AST, *, marker: str = "") -> set:
    """The string keys a function builds a reply from: its dict literals (only those holding
    `marker` when one is given, so request-building dicts are left out) and every
    out["key"] / result["key"] = ... assignment."""
    keys = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            found = {k.value for k in sub.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if not marker or marker in found:
                keys |= found
        elif isinstance(sub, ast.Assign):
            for target in sub.targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                        and isinstance(target.value, ast.Name) and target.value.id in ("out", "result")):
                    keys.add(target.slice.value)
    return keys


def _routes(tree: ast.Module) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.args:
                    out.add((deco.func.attr, deco.args[0].value))
    return out


def _constant(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.unparse(node.value)
    raise AssertionError(f"no constant {name}")


def test_the_same_routes():
    assert _routes(_tree(CPU_SERVER)) == _routes(_tree(GPU_SERVER)) == {
        ("get", "/health"), ("get", "/v1/models"), ("post", "/v1/audio/transcriptions"),
    }


def test_the_same_form_fields_with_the_same_defaults():
    def fields(tree):
        fn = _function(tree, "transcriptions")
        return [(a.arg, ast.unparse(d)) for a, d in zip(fn.args.args, fn.args.defaults)]

    assert fields(_tree(CPU_SERVER)) == fields(_tree(GPU_SERVER))


def test_the_same_response_keys_from_every_path():
    gpu, cpu = _tree(GPU_SERVER), _tree(CPU_SERVER)
    assert _dict_keys(_function(cpu, "_run"), marker="duration") == _dict_keys(_function(gpu, "_run"), marker="duration") == {
        "text", "language", "language_code", "duration", "no_speech_prob", "segments",
    }
    assert _dict_keys(_function(cpu, "_segments")) >= {"id", "start", "end", "text", "language"}
    assert _dict_keys(_function(cpu, "transcriptions")) >= _dict_keys(_function(gpu, "transcriptions"))
    # /health may say more on the CPU replica, never less.
    assert _dict_keys(_function(cpu, "health")) >= _dict_keys(_function(gpu, "health"))
    assert _dict_keys(_function(cpu, "models")) == _dict_keys(_function(gpu, "models"))


def test_the_same_model_gate_and_limits():
    gpu, cpu = _tree(GPU_SERVER), _tree(CPU_SERVER)
    for name in ("MODEL_ID", "MODEL_REVISION", "SAMPLE_RATE", "NO_SPEECH_THRESHOLD", "MAX_AUDIO_SECONDS", "BIND_HOST"):
        assert _constant(cpu, name) == _constant(gpu, name), name
    # The port differs by design (30007 GPU, 30008 CPU); the variable that sets it does not.
    assert "WHISPER_PORT" in _constant(cpu, "BIND_PORT") and "30008" in _constant(cpu, "BIND_PORT")
    # ffmpeg's arguments, the language rule and the segment clamping are the GPU replica's.
    for name in ("_decode", "_language_arg"):
        strip = lambda fn: [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        assert ast.dump(ast.Module(strip(_function(cpu, name)), [])) == ast.dump(ast.Module(strip(_function(gpu, name)), [])), name


def test_the_gpu_replicas_decode_settings_are_the_ones_the_cpu_replica_mirrors():
    """A TRIPWIRE, deliberately. wcpp_worker.cpp reproduces the transformers pipeline's defaults one
    by one (greedy, no fallback, no previous-text conditioning, transcribe, timestamps only for
    segments or >= 30 s, sequential long form). If compose/whisper/server.py starts passing
    anything else to the pipeline (a language hint, beam search, a prompt, chunk_length_s), the two
    replicas no longer transcribe alike: make the same change in wcpp_worker.cpp, re-measure both
    on the paired sets (docs/voice/CPU-REPLICA.md), then update the sets below."""
    run = _function(_tree(GPU_SERVER), "_run")
    generate = set()
    pipeline_kwargs = set()
    for node in ast.walk(run):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "generate_kwargs" and isinstance(node.value, ast.Dict):
                    generate |= {k.value for k in node.value.keys}
                if isinstance(target, ast.Name) and target.id == "kwargs" and isinstance(node.value, ast.Dict):
                    pipeline_kwargs |= {k.value for k in node.value.keys}
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                    if target.value.id == "generate_kwargs":
                        generate.add(target.slice.value)
                    elif target.value.id == "kwargs":
                        pipeline_kwargs.add(target.slice.value)
    assert generate == {"task", "language"}, "the GPU replica's generate_kwargs changed: mirror it in wcpp_worker.cpp"
    assert pipeline_kwargs == {"return_language", "generate_kwargs", "return_timestamps"}, (
        "the GPU replica's pipeline arguments changed: mirror them in wcpp_worker.cpp"
    )
    load = GPU_SERVER.read_text(encoding="utf-8")
    assert "chunk_length_s=" not in load.split("def _load", 1)[1].split("def _decode", 1)[0], (
        "the GPU replica switched to chunked long form; wcpp_worker.cpp decodes sequentially"
    )


def test_the_image_lifts_whisper_cpps_220_token_window_cap():
    """Upstream whisper.cpp stops a window at n_text_ctx/2 - 4 = 220 new tokens and then decodes the
    rest of the window again: 5 of 200 FLEURS Hindi clips (17-24 s) came back with a phrase
    repeated. The GPU replica's pipeline allows the model's whole context. The Dockerfile patches
    the one line and fails the build when the patch does not apply; this keeps both steps."""
    dockerfile = (CPU_SERVER.parent / "Dockerfile").read_text(encoding="utf-8")
    assert "sed -i 's|n_max = whisper_n_text_ctx(ctx)/2 - 4;|n_max = whisper_n_text_ctx(ctx) - (int) prompt.size() - 1;|'" in dockerfile
    assert "test \"$(grep -c 'n_max = whisper_n_text_ctx(ctx)/2 - 4;' \"$f\")\" = 1" in dockerfile
    assert "test \"$(grep -c 'n_max = whisper_n_text_ctx(ctx) - (int) prompt.size() - 1;' \"$f\")\" = 1" in dockerfile
    # The patch sits in the stage that compiles wcpp-worker, before the compile.
    build_stage = dockerfile.split("FROM python:3.12-slim AS convert", 1)[0]
    assert build_stage.index("sed -i 's|n_max") < build_stage.index("cmake --build")


# -- the live half -------------------------------------------------------------------------------

FAKE_WORKER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # wcpp-worker's protocol, with canned answers: silence scores 0.71 (the measured digital-silence
    # number), anything else is "hello world" in English; n_samples == 12345 makes it die and
    # n_samples == 23456 makes it hang.
    import json, struct, sys, time
    print(json.dumps({"ready": True, "load_ms": 1.0, "system_info": "fake"}), flush=True)
    for line in sys.stdin.buffer:
        head = json.loads(line)
        n = head["n_samples"]
        raw = sys.stdin.buffer.read(n * 4)
        if n == 12345:
            sys.exit(9)
        if n == 23456:
            time.sleep(120)
        samples = struct.unpack("<%df" % n, raw)
        silent = max(abs(x) for x in samples) == 0.0
        nsp = 0.71 if silent else 0.01
        if head.get("gate") and nsp > head.get("threshold", 0.6):
            print(json.dumps({"ok": True, "gated": True, "no_speech_prob": nsp, "segments": []}), flush=True)
            continue
        lang = head.get("language")
        if lang not in (None, "en", "english"):
            print(json.dumps({"ok": False, "error": "unsupported language '%s'" % lang}), flush=True)
            continue
        seconds = n / 16000.0
        print(json.dumps({"ok": True, "gated": False, "no_speech_prob": nsp, "language": "en",
                          "language_name": "english", "windows": 1,
                          "segments": [{"t0": 0.0, "t1": min(seconds, 1.5), "text": " hello"},
                                       {"t0": 1.5, "t1": seconds + 5.0, "text": " world"}]}), flush=True)
    """
)


def _wav(seconds: float, *, silent: bool = False, samples: int = 0) -> bytes:
    import math

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        frames = bytearray()
        for i in range(samples or round(seconds * 16000)):
            v = 0 if silent else int(8000 * math.sin(i / 10.0))
            frames += int(v).to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))
    return buf.getvalue()


@pytest.fixture()
def server(tmp_path, monkeypatch):
    worker = tmp_path / "wcpp-worker"
    worker.write_text(FAKE_WORKER.replace("/usr/bin/env python3", sys.executable, 1), encoding="utf-8")
    worker.chmod(worker.stat().st_mode | stat.S_IXUSR)
    model = tmp_path / "ggml-large-v3-q8_0.bin"
    model.write_bytes(b"not a model")
    monkeypatch.setenv("WHISPER_CPU_WORKER", str(worker))
    monkeypatch.setenv("WHISPER_CPU_MODEL_FILE", str(model))
    monkeypatch.setenv("WHISPER_CPU_SKIP_SHA256", "1")
    spec = importlib.util.spec_from_file_location("whisper_cpu_server_under_test", CPU_SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def decode_wav(payload: bytes):
        """ffmpeg's job, for a WAV: s16 -> float32 / 32768. The real _decode needs ffmpeg."""
        import numpy as np

        try:
            with wave.open(io.BytesIO(payload), "rb") as w:
                raw = w.readframes(w.getnframes())
        except Exception:  # noqa: BLE001
            raise module.HTTPException(status_code=400, detail="could not decode audio: not a wav") from None
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size == 0:
            raise module.HTTPException(status_code=400, detail="audio decoded to zero samples")
        return audio

    if shutil.which("ffmpeg") is None:
        monkeypatch.setattr(module, "_decode", decode_wav)
    from fastapi.testclient import TestClient

    with TestClient(module.app) as client:
        yield module, client


def _post(client, payload: bytes, **fields):
    return client.post("/v1/audio/transcriptions", files={"file": ("c.wav", payload, "audio/wav")},
                       data={"model": "openai/whisper-large-v3", **fields})


def test_health_is_ready_and_says_what_the_gpu_replica_says(server):
    _module, client = server
    body = client.get("/health").json()
    assert body["ready"] is True, body
    assert body["model"] == "openai/whisper-large-v3"
    assert body["long_form"] == "sequential" and body["task"] == "transcribe"
    assert body["no_speech_threshold"] == 0.6
    assert body["cuda_failures"] == 0 and body["dtype"] == "q8_0" and body["backend"] == "whisper.cpp"
    assert client.get("/v1/models").json()["data"][0]["id"] == "openai/whisper-large-v3"


def test_json_carries_exactly_the_gpu_replicas_fields(server):
    _module, client = server
    r = _post(client, _wav(2.0))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"text", "language", "language_code", "duration", "no_speech_prob", "processing_ms"}
    assert body["text"] == "hello world"
    assert (body["language"], body["language_code"]) == ("english", "en")
    assert body["duration"] == 2.0 and body["no_speech_prob"] == 0.01


def test_verbose_json_adds_clamped_segments_and_the_task(server):
    _module, client = server
    body = _post(client, _wav(2.0), response_format="verbose_json").json()
    assert body["task"] == "transcribe"
    assert body["segments"] == [
        {"id": 0, "start": 0.0, "end": 1.5, "text": "hello", "language": "en"},
        {"id": 1, "start": 1.5, "end": 2.0, "text": "world", "language": "en"},  # end clamped to the clip
    ]


def test_text_is_a_plain_body(server):
    _module, client = server
    r = _post(client, _wav(1.0), response_format="text")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.json() == "hello world"


def test_silence_is_gated_to_the_gpu_replicas_empty_answer(server):
    _module, client = server
    body = _post(client, _wav(3.0, silent=True)).json()
    assert body["text"] == "" and body["language"] is None and body["language_code"] is None
    assert body["segments"] == [] and body["no_speech_prob"] > 0.6


def test_the_gate_off_reports_zero_like_the_gpu_replica(server):
    _module, client = server
    body = _post(client, _wav(3.0, silent=True), no_speech_check="false").json()
    assert body["no_speech_prob"] == 0.0
    assert body["text"] == "hello world"


def test_a_forced_language_is_echoed_as_given(server):
    _module, client = server
    body = _post(client, _wav(1.0), language="EN").json()
    assert (body["language"], body["language_code"]) == ("en", "en")
    assert _post(client, _wav(1.0), language="auto").json()["language"] == "english"


def test_refusals_have_the_gpu_replicas_statuses(server):
    module, client = server
    assert _post(client, _wav(1.0), response_format="srt").status_code == 400
    assert _post(client, b"").status_code == 400
    assert _post(client, _wav(1.0), language="xx").status_code == 500
    module.MAX_AUDIO_SECONDS = 1.0
    r = _post(client, _wav(2.0))
    assert r.status_code == 413 and "the limit is 1s" in r.json()["detail"]


def test_a_dead_decoder_is_a_503_and_the_next_clip_restarts_it(server):
    module, client = server
    dying = _wav(0, samples=12345)  # the fake worker exits on exactly this many samples
    r = _post(client, dying)
    assert r.status_code == 503
    assert _post(client, _wav(1.0)).json()["text"] == "hello world"
    assert client.get("/health").json()["worker_failures"] == 0


def test_a_hung_decoder_is_killed_at_its_bound_and_the_next_clip_gets_a_fresh_one(server):
    module, client = server
    module.HANG_FIXED_S = 1.0
    module.HANG_S_PER_AUDIO_S = 0.0
    started = time.monotonic()
    r = _post(client, _wav(0, samples=23456))  # the fake worker sleeps on exactly this many samples
    assert r.status_code == 503 and "longer than 1s" in r.json()["detail"]
    assert time.monotonic() - started < 30, "the watchdog did not end the hung decode"
    health = client.get("/health").json()
    assert health["busy"] is False and health["worker_failures"] == 1
    assert _post(client, _wav(1.0)).json()["text"] == "hello world"
    assert client.get("/health").json()["worker_failures"] == 0


def test_the_watchdog_leaves_a_decode_inside_its_bound_alone(server):
    module, client = server
    module.HANG_FIXED_S = 30.0
    module.HANG_S_PER_AUDIO_S = 0.0
    for _ in range(3):
        assert _post(client, _wav(1.0)).json()["text"] == "hello world"
    assert client.get("/health").json()["worker_failures"] == 0


def test_a_model_file_that_is_not_the_pinned_one_is_refused(tmp_path, monkeypatch):
    model = tmp_path / "ggml-large-v3-q8_0.bin"
    model.write_bytes(b"something else")
    monkeypatch.setenv("WHISPER_CPU_MODEL_FILE", str(model))
    monkeypatch.delenv("WHISPER_CPU_SKIP_SHA256", raising=False)
    spec = importlib.util.spec_from_file_location("whisper_cpu_server_sha", CPU_SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from fastapi.testclient import TestClient

    with TestClient(module.app) as client:
        health = client.get("/health").json()
        assert health["ready"] is False and "SHA-256" in (health["error"] or "")
        assert _post(client, _wav(1.0)).status_code == 503


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is the decoder; not installed here")
def test_undecodable_bytes_are_a_400_from_ffmpeg(server):
    _module, client = server
    r = _post(client, b"this is not audio at all" * 50)
    assert r.status_code == 400 and r.json()["detail"].startswith("could not decode audio")
