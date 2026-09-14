#!/usr/bin/env python3
"""devapi_local_stack — the whole /v1 path on one machine, with fake engines.

WHY (2026-09-13, no-timeout design revision 2, team T5). The no-timeout
guarantees are about what happens BETWEEN processes: a gateway holding a client
while the orchestrator restarts, a Next server that drains without cutting an
upload, a stream resumed after its connection dropped. None of that can be
proven inside one process's unit tests, and none of it may be tried on the
production stack. This tool starts, on 127.0.0.1 and ports of your choosing:

  fake engine      an OpenAI-compatible stub of the main model: streamed chat
                   completions whose text is a deterministic numbered sequence
                   (so a continuation or a replay can be checked word for
                   word), /tokenize, /v1/models, and vLLM-shaped /metrics
  fake controller  GET /state in the engine controller's schema (READY by
                   default; POST /state to switch it)
  fake whisper     POST /v1/audio/transcriptions and /health
  orchestrator     the REAL app.main:app from --tree, on your Postgres
  v1-gateway       the REAL gateway/server.cjs from --tree
  frontend         the REAL Next standalone build of --tree/frontend, with
                   server-preload.cjs, relaying /v1 to the gateway

and provisions a workspace, a project and keys in the database, so an SDK can
be pointed at either edge:

  client -> frontend (Next /v1 route) -> v1-gateway -> orchestrator -> fakes
  client -> v1-gateway -> orchestrator -> fakes
  client -> orchestrator -> fakes

Subcommands:

  up                    start everything (and the fakes), provision keys, print
                        the environment to export
  down                  stop everything this run started
  status                what is running, on which port
  env                   print the exports again
  restart-orchestrator  SIGTERM the orchestrator (graceful, like a deploy),
                        wait --gap-s, start a new one on the same port
  restart-frontend      the same for the Next server
  restart-gateway       the same for the gateway
  set-engine            change the fake engine's pacing while it runs
                        (--token-delay-s, --prefill-s)

Nothing here touches compose, a deployed container or a real engine. Postgres
is YOURS: pass --database-url (a throwaway database; the orchestrator runs its
migrations on it), or --start-postgres to launch one throwaway container
(`postgres:18-alpine`, fsync off) that `down` removes.

Run it with the orchestrator's virtualenv, which has uvicorn and starlette:

  orchestrator/.venv/bin/python tools/devapi_local_stack.py up \\
      --run-dir /tmp/devapi-stack --start-postgres 55590

The frontend needs a `npm run build` (output: "standalone") in --tree/frontend,
or in the copy named by --frontend-dir — Turbopack refuses a node_modules that
is a symlink out of the project. Without a build, `up` starts everything else
and says so.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_FILE = "stack.json"
MAIN_MODEL = "Qwen/Qwen3.6-35B-A3B-NVFP4"

# --------------------------------------------------------------------- fakes --


def _fake_engine_app(state_path: Path):
    """The main-model stub. Output is `w1 w2 w3 …` one word per token, so any
    text can be checked for gaps and duplicates, and a continuation
    (continue_final_message) picks the numbering up where the assistant
    message left it."""
    import asyncio

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
    from starlette.routing import Route

    started = time.time()
    counters = {"running": 0, "waiting": 0, "generation_tokens": 0, "prompt_tokens": 0, "requests": 0}
    seen: List[Dict[str, Any]] = []

    def pacing() -> Dict[str, float]:
        try:
            doc = json.loads(state_path.read_text())
        except (OSError, ValueError):
            doc = {}
        return {
            "token_delay_s": float(doc.get("token_delay_s", 0.02)),
            "prefill_s": float(doc.get("prefill_s", 0.0)),
        }

    def text_of(messages: List[Dict[str, Any]]) -> str:
        out = []
        for message in messages or []:
            content = message.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.extend(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return "\n".join(out)

    async def chat(request: Request):
        body = await request.json()
        messages = body.get("messages") or []
        continuing = bool(body.get("continue_final_message"))
        first = 1
        if continuing and messages and messages[-1].get("role") == "assistant":
            numbers = re.findall(r"\bw(\d+)\b", str(messages[-1].get("content") or ""))
            first = int(numbers[-1]) + 1 if numbers else 1
        budget = int(body.get("max_tokens") or body.get("max_completion_tokens") or 16)
        prompt_tokens = max(1, len(text_of(messages)) // 4)
        counters["requests"] += 1
        seen.append({"at": time.time(), "stream": bool(body.get("stream")), "max_tokens": budget,
                     "continue_final_message": continuing, "first_word": first, "messages": len(messages)})
        pace = pacing()

        words = [f"w{n}" for n in range(first, first + budget)]
        pieces = [(" " if i or continuing else "") + word for i, word in enumerate(words)]

        async def produce():
            counters["running"] += 1
            try:
                if pace["prefill_s"] > 0:
                    await asyncio.sleep(pace["prefill_s"])
                counters["prompt_tokens"] += prompt_tokens
                for piece in pieces:
                    counters["generation_tokens"] += 1
                    yield piece
                    if pace["token_delay_s"] > 0:
                        await asyncio.sleep(pace["token_delay_s"])
            finally:
                counters["running"] -= 1

        model = body.get("model")
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": len(pieces), "total_tokens": prompt_tokens + len(pieces)}
        if not body.get("stream"):
            text = "".join([piece async for piece in produce()])
            return JSONResponse({"id": "cmpl-fake", "object": "chat.completion", "created": int(started), "model": model,
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "length"}],
                                 "usage": usage})

        def frame(payload: Dict[str, Any]) -> str:
            return "data: " + json.dumps({"id": "cmpl-fake", "object": "chat.completion.chunk", "created": int(started),
                                          "model": model, **payload}) + "\n\n"

        async def stream():
            async for piece in produce():
                yield frame({"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
            yield frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
            yield frame({"choices": [], "usage": usage})
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def tokenize(request: Request):
        body = await request.json()
        text = text_of(body.get("messages") or []) if "messages" in body else str(body.get("prompt", ""))
        return JSONResponse({"count": max(1, len(text) // 4), "max_model_len": 1_000_000, "tokens": []})

    async def models(_request: Request):
        return JSONResponse({"object": "list", "data": [{"id": MAIN_MODEL, "object": "model", "max_model_len": 1_000_000}]})

    async def metrics(_request: Request):
        lines = [
            f'vllm:num_requests_running{{model_name="{MAIN_MODEL}"}} {counters["running"]}',
            f'vllm:num_requests_waiting{{model_name="{MAIN_MODEL}"}} {counters["waiting"]}',
            f'vllm:generation_tokens_total{{model_name="{MAIN_MODEL}"}} {counters["generation_tokens"]}',
            f'vllm:prompt_tokens_total{{model_name="{MAIN_MODEL}"}} {counters["prompt_tokens"]}',
            f"process_start_time_seconds {started}",
        ]
        return PlainTextResponse("\n".join(lines) + "\n")

    async def health(_request: Request):
        return PlainTextResponse("ok")

    async def seen_view(_request: Request):
        return JSONResponse({"requests": counters["requests"], "running": counters["running"], "seen": seen[-200:]})

    return Starlette(routes=[
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/tokenize", tokenize, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/metrics", metrics, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/seen", seen_view, methods=["GET"]),
    ])


def _fake_controller_app(engine_url: str):
    """GET /state in the controller's schema (orchestrator/app/engine_state.py
    parse_state_document). READY unless POST /state says otherwise."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    codes = {"MONITORING_UNKNOWN": -1, "DOWN": 0, "STARTING": 1, "READY": 2, "BUSY": 3, "DEGRADED": 4,
             "WEDGED": 5, "RECOVERING": 6, "QUEUEING": 7}
    current = {"state": "READY", "reason": "fake controller", "head_started_at": time.time() - 3600}

    def engine_load() -> Dict[str, int]:
        try:
            with urllib.request.urlopen(engine_url.rstrip("/") + "/seen", timeout=1) as response:
                doc = json.loads(response.read())
            return {"requests_running": int(doc.get("running", 0)), "requests_waiting": 0}
        except (OSError, ValueError):
            return {}

    async def state(_request: Request):
        name = current["state"]
        return JSONResponse({
            "state": name,
            "state_code": codes.get(name, -1),
            "reason": current["reason"],
            "primary_ready": name in ("READY", "BUSY", "DEGRADED"),
            "router_available": False,
            "generated_at": time.time(),
            "recovery": {"step": "idle", "budget": 3, "attempts_in_window": 0},
            "signals": {
                "head_container": {"running": True, "engine_process_alive": name != "DOWN",
                                   "started_at": current["head_started_at"]},
                "engine": engine_load(),
            },
        })

    async def set_state(request: Request):
        body = await request.json()
        current["state"] = str(body.get("state", current["state"]))
        current["reason"] = str(body.get("reason", current["reason"]))
        if body.get("restart_head"):
            current["head_started_at"] = time.time()
        return JSONResponse(current)

    return Starlette(routes=[Route("/state", state, methods=["GET"]), Route("/state", set_state, methods=["POST"])])


def _fake_whisper_app():
    """Transcription stub: a sentence per second of WAV audio, so a windowed
    transcript can be checked for order and completeness."""
    import wave
    import io

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    async def transcribe(request: Request):
        form = await request.form()
        upload = form.get("file")
        data = await upload.read() if upload is not None else b""
        seconds = 0.0
        try:
            with wave.open(io.BytesIO(data)) as handle:
                seconds = handle.getnframes() / float(handle.getframerate() or 16000)
        except (wave.Error, EOFError):
            seconds = len(data) / 32000.0
        text = " ".join(f"second {n}." for n in range(int(seconds)))
        return JSONResponse({"text": text, "duration": seconds, "language": "english",
                             "segments": [{"id": 0, "start": 0.0, "end": seconds, "text": text}]})

    async def health(_request: Request):
        return JSONResponse({"status": "ok", "ready": True, "cuda_failures": 0})

    async def root(_request: Request):
        return PlainTextResponse("fake whisper")

    return Starlette(routes=[
        Route("/v1/audio/transcriptions", transcribe, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
        Route("/", root, methods=["GET"]),
    ])


def _serve(app, port: int) -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


# -------------------------------------------------------------------- helpers --


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _load(run_dir: Path) -> Dict[str, Any]:
    path = run_dir / STATE_FILE
    if not path.exists():
        raise SystemExit(f"no stack in {run_dir}: run `up` first")
    return json.loads(path.read_text())


def _save(run_dir: Path, state: Dict[str, Any]) -> None:
    tmp = run_dir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, run_dir / STATE_FILE)


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        # SO_REUSEADDR as uvicorn and node set it: a closed server leaves its
        # accepted connections in TIME_WAIT, which blocks a plain bind for a
        # minute but not the next server's (measured 2026-09-14).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _require_port_free(name: str, port: int, wait_s: float = 10.0) -> None:
    """A component must never be "started" on a port something else still
    answers on: the health wait would pass against the old process and the
    new one would die on bind (seen 2026-09-14 when two restarts ran at once)."""
    deadline = time.monotonic() + wait_s
    while not _port_free(port):
        if time.monotonic() > deadline:
            raise RuntimeError(f"{name}: port {port} is still in use; stop whatever holds it first")
        time.sleep(0.1)


def _confirm_alive(name: str, pid: int, run_dir: Path) -> None:
    if not _alive(pid):
        log = (run_dir / f"{name}.log").read_text(errors="replace")[-1500:]
        raise RuntimeError(f"{name} (pid {pid}) exited during start-up:\n{log}")


def _spawn(name: str, argv: List[str], *, run_dir: Path, env: Dict[str, str], cwd: Optional[Path] = None) -> int:
    log = open(run_dir / f"{name}.log", "ab")
    log.write(f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%S')} start {' '.join(argv)}\n".encode())
    log.flush()
    process = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=env, stdout=log, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, start_new_session=True)
    return process.pid


def _wait_http(url: str, *, ok=lambda status: status < 500, timeout_s: float = 120.0) -> float:
    started = time.monotonic()
    last = "no answer"
    while time.monotonic() - started < timeout_s:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if ok(response.status):
                    return time.monotonic() - started
                last = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            if ok(exc.code):
                return time.monotonic() - started
            last = f"HTTP {exc.code}"
        except (OSError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(0.25)
    raise RuntimeError(f"{url} did not answer within {timeout_s:.0f} s ({last})")


def _stop(pid: Optional[int], *, sig: int = signal.SIGTERM, wait_s: float = 150.0) -> Optional[float]:
    """Signal the process group, wait for it to exit, SIGKILL after wait_s.
    Returns how long the exit took."""
    if not _alive(pid):
        return None
    started = time.monotonic()
    try:
        os.killpg(pid, sig)
    except OSError:
        os.kill(pid, sig)
    while time.monotonic() - started < wait_s:
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return time.monotonic() - started
        except ChildProcessError:
            if not _alive(pid):
                return time.monotonic() - started
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    return time.monotonic() - started


# ------------------------------------------------------------------ components --


def _orchestrator_env(state: Dict[str, Any]) -> Dict[str, str]:
    ports = state["ports"]
    run_dir = Path(state["run_dir"])
    env = dict(os.environ)
    env.update({
        "APP_DATABASE_URL": state["database_url"],
        "OPENAI_BASE_URL": f"http://127.0.0.1:{ports['engine']}/v1",
        "LLM_MODEL": MAIN_MODEL,
        "EMBED_BASE_URL": "",
        "ROUTER_BASE_URL": "http://127.0.0.1:9/v1",
        "OCR_BASE_URL": "http://127.0.0.1:9/v1",
        "OCR_ENABLED": "false",
        "RERANK_BASE_URL": "",
        "ASR_ENABLED": "true",
        "ASR_BASE_URLS": f"http://127.0.0.1:{ports['whisper']}",
        "ENGINE_CONTROLLER_URL": f"http://127.0.0.1:{ports['controller']}/state",
        "ENGINE_STATE_POLL_S": "1",
        "API_KEY_PEPPER": state["pepper"],
        # The gateway and the Next edge both connect from 127.0.0.1 here.
        "PUBLIC_API_TRUSTED_PROXIES": "127.0.0.1",
        # The gateway is the one peer that may attach (T3-wire, 2026-09-14).
        "PUBLIC_API_GATEWAY_PEERS": "127.0.0.1",
        "PUBLIC_API_MIN_FREE_DISK_BYTES": "0",
        "PUBLIC_API_FILES_DIR": str(run_dir / "files"),
        "PUBLIC_API_BLOB_DIR": str(run_dir / "blobs"),
        "PUBLIC_API_ASR_CACHE_DIR": str(run_dir / "asr"),
        "VIDEO_DATA_DIR": str(run_dir / "video"),
        "WORKSPACE_DIR": str(run_dir / "workspaces"),
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "WARNING"),
    })
    env.update(state.get("orchestrator_env", {}))
    return env


def _start_orchestrator(state: Dict[str, Any]) -> None:
    tree = Path(state["tree"])
    port = state["ports"]["orchestrator"]
    python = state["python"]
    argv = [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning", "--timeout-graceful-shutdown", "90"]
    _require_port_free("orchestrator", port)
    state["pids"]["orchestrator"] = _spawn("orchestrator", argv, run_dir=Path(state["run_dir"]),
                                           env=_orchestrator_env(state), cwd=tree / "orchestrator")
    took = _wait_http(f"http://127.0.0.1:{port}/v1/models", ok=lambda s: s in (200, 401), timeout_s=180)
    _confirm_alive("orchestrator", state["pids"]["orchestrator"], Path(state["run_dir"]))
    print(f"orchestrator  :{port}  answered /v1/models after {took:.1f} s")


def _start_gateway(state: Dict[str, Any]) -> None:
    tree = Path(state["tree"])
    run_dir = Path(state["run_dir"])
    spool = run_dir / "spool"
    spool.mkdir(mode=0o700, exist_ok=True)
    port = state["ports"]["gateway"]
    env = dict(os.environ)
    env.update({
        "ORCHESTRATOR_URL": f"http://127.0.0.1:{state['ports']['orchestrator']}",
        "V1_GATEWAY_PORT": str(port),
        "V1_GATEWAY_HOST": "127.0.0.1",
        "V1_GATEWAY_SPOOL_DIR": str(spool),
        "PUBLIC_API_MIN_FREE_DISK_BYTES": "0",
    })
    env.update(state.get("gateway_env", {}))
    _require_port_free("gateway", port)
    state["pids"]["gateway"] = _spawn("gateway", [state["node"], "server.cjs"], run_dir=run_dir, env=env, cwd=tree / "gateway")
    took = _wait_http(f"http://127.0.0.1:{port}/healthz", timeout_s=30)
    _confirm_alive("gateway", state["pids"]["gateway"], run_dir)
    print(f"v1-gateway    :{port}  healthy after {took:.1f} s")


def _standalone_dir(frontend: Path) -> Optional[Path]:
    standalone = frontend / ".next" / "standalone"
    return standalone if (standalone / "server.js").exists() else None


def _start_frontend(state: Dict[str, Any]) -> None:
    frontend = Path(state.get("frontend_dir") or Path(state["tree"]) / "frontend")
    standalone = _standalone_dir(frontend)
    if standalone is None:
        print(f"frontend      skipped: no {frontend}/.next/standalone/server.js — run `npm run build` there first")
        state["pids"]["frontend"] = None
        return
    # Next's standalone output leaves static assets to the deployer (the
    # Dockerfile copies them); /v1 needs none, the pages do.
    static_src = frontend / ".next" / "static"
    static_dst = standalone / ".next" / "static"
    if static_src.exists() and not static_dst.exists():
        shutil.copytree(static_src, static_dst)
    port = state["ports"]["frontend"]
    env = dict(os.environ)
    env.update({
        "PORT": str(port),
        "HOSTNAME": "127.0.0.1",
        "NODE_ENV": "production",
        "ORCHESTRATOR_URL": f"http://127.0.0.1:{state['ports']['orchestrator']}",
        "V1_GATEWAY_URL": "" if state.get("frontend_direct") else f"http://127.0.0.1:{state['ports']['gateway']}",
    })
    env.update(state.get("frontend_env", {}))
    preload = frontend / "server-preload.cjs"
    argv = [state["node"], "--require", str(preload), "server.js"]
    _require_port_free("frontend", port)
    state["pids"]["frontend"] = _spawn("frontend", argv, run_dir=Path(state["run_dir"]), env=env, cwd=standalone)
    took = _wait_http(f"http://127.0.0.1:{port}/v1/models", ok=lambda s: s in (200, 401), timeout_s=60)
    _confirm_alive("frontend", state["pids"]["frontend"], Path(state["run_dir"]))
    print(f"frontend      :{port}  answered /v1/models after {took:.1f} s")


def _provision(state: Dict[str, Any]) -> Dict[str, Any]:
    """A workspace, a project and three keys, through the platform's own code."""
    tree = Path(state["tree"])
    script = r"""
import json, secrets
from app import db
from app.apiplatform import projects
ws = "ws_local_" + secrets.token_hex(4)
with db.connection() as con:
    con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (ws, "devapi local stack"))
project = projects.create_project(ws, "devapi local stack")
main = projects.create_key(project["id"], ws, "local stack default scopes")
usage = projects.create_key(project["id"], ws, "local stack usage", scopes=["usage.read"])
narrow = projects.create_key(project["id"], ws, "local stack models only", scopes=["models.read"])
print(json.dumps({"workspace": ws, "project": project["id"], "api_key": main.token,
                  "usage_api_key": usage.token, "limited_api_key": narrow.token,
                  "scopes": sorted(getattr(main, "scopes", None) or [])}))
"""
    env = _orchestrator_env(state)
    env["PYTHONPATH"] = str(tree / "orchestrator")
    out = subprocess.run([state["python"], "-c", script], cwd=tree / "orchestrator", env=env,
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"provisioning failed:\n{out.stderr[-2000:]}")
    keys = json.loads(out.stdout.strip().splitlines()[-1])
    keys_path = Path(state["run_dir"]) / "keys.json"
    keys_path.write_text(json.dumps({**keys, "base_url": f"http://127.0.0.1:{state['ports']['gateway']}/v1"}, indent=2))
    os.chmod(keys_path, 0o600)
    return keys


def _exports(state: Dict[str, Any]) -> str:
    ports = state["ports"]
    keys = state.get("keys") or {}
    lines = [
        f"export TECHSARA_API_KEY={keys.get('api_key', '')}",
        f"export TECHSARA_LIMITED_API_KEY={keys.get('limited_api_key', '')}",
        f"export TECHSARA_API_KEY_NARROW={keys.get('limited_api_key', '')}",
        f"export TECHSARA_USAGE_API_KEY={keys.get('usage_api_key', '')}",
        f"export TECHSARA_BASE_URL=http://127.0.0.1:{ports['frontend']}/v1      # Next edge -> gateway",
        f"export TECHSARA_GATEWAY_URL=http://127.0.0.1:{ports['gateway']}/v1",
        f"export TECHSARA_ORCHESTRATOR_URL=http://127.0.0.1:{ports['orchestrator']}/v1",
        f"export TECHSARA_FAKE_ENGINE_URL=http://127.0.0.1:{ports['engine']}",
        f"export TECHSARA_FAKE_CONTROLLER_URL=http://127.0.0.1:{ports['controller']}",
        f"export TECHSARA_KEYS_FILE={state['run_dir']}/keys.json",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------- subcommands --


def cmd_up(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    tree = Path(args.tree).resolve()
    if not (tree / "orchestrator" / "app" / "main.py").exists():
        raise SystemExit(f"--tree {tree} has no orchestrator/app/main.py")
    for sub in ("files", "blobs", "asr", "video", "workspaces"):
        (run_dir / sub).mkdir(exist_ok=True)
    ports = {name: getattr(args, f"{name}_port") or _free_port()
             for name in ("engine", "controller", "whisper", "orchestrator", "gateway", "frontend")}
    state: Dict[str, Any] = {
        "run_dir": str(run_dir),
        "tree": str(tree),
        "python": args.python or sys.executable,
        "node": args.node or shutil.which("node") or "node",
        "ports": ports,
        "pids": {},
        "pepper": "local-stack-" + secrets.token_hex(16),
        "frontend_direct": bool(args.frontend_direct),
        "frontend_dir": str(Path(args.frontend_dir).resolve()) if args.frontend_dir else None,
        "postgres_container": None,
    }
    (run_dir / "engine-pacing.json").write_text(json.dumps({"token_delay_s": args.token_delay_s, "prefill_s": args.prefill_s}))

    if args.start_postgres:
        name = f"devapi-local-stack-pg-{args.start_postgres}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{args.start_postgres}:5432",
                        "-e", "POSTGRES_PASSWORD=postgres", "postgres:18-alpine", "-c", "fsync=off",
                        "-c", "synchronous_commit=off", "-c", "full_page_writes=off", "-c", "max_connections=300"],
                       check=True, capture_output=True)
        state["postgres_container"] = name
        state["database_url"] = f"postgresql://postgres:postgres@127.0.0.1:{args.start_postgres}/postgres"
        for _ in range(120):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        subprocess.run(["docker", "exec", name, "psql", "-U", "postgres", "-c",
                        "CREATE DATABASE devapi_local TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C'"],
                       check=True, capture_output=True)
        state["database_url"] = f"postgresql://postgres:postgres@127.0.0.1:{args.start_postgres}/devapi_local"
    elif args.database_url:
        state["database_url"] = args.database_url
    else:
        raise SystemExit("pass --database-url (a throwaway database) or --start-postgres PORT")
    _save(run_dir, state)

    me = str(Path(__file__).resolve())
    python = state["python"]
    base_env = dict(os.environ)
    state["pids"]["engine"] = _spawn("fake-engine", [python, me, "serve-fake-engine", "--port", str(ports["engine"]),
                                                     "--pacing-file", str(run_dir / "engine-pacing.json")],
                                     run_dir=run_dir, env=base_env)
    state["pids"]["controller"] = _spawn("fake-controller", [python, me, "serve-fake-controller", "--port", str(ports["controller"]),
                                                             "--engine-url", f"http://127.0.0.1:{ports['engine']}"],
                                         run_dir=run_dir, env=base_env)
    state["pids"]["whisper"] = _spawn("fake-whisper", [python, me, "serve-fake-whisper", "--port", str(ports["whisper"])],
                                      run_dir=run_dir, env=base_env)
    _save(run_dir, state)
    _wait_http(f"http://127.0.0.1:{ports['engine']}/health", timeout_s=30)
    _wait_http(f"http://127.0.0.1:{ports['controller']}/state", timeout_s=30)
    _wait_http(f"http://127.0.0.1:{ports['whisper']}/health", timeout_s=30)
    print(f"fakes         engine :{ports['engine']}  controller :{ports['controller']}  whisper :{ports['whisper']}")

    _start_orchestrator(state)
    _save(run_dir, state)
    state["keys"] = _provision(state)
    _save(run_dir, state)
    _start_gateway(state)
    _save(run_dir, state)
    _start_frontend(state)
    _save(run_dir, state)
    print()
    print(_exports(state))
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    state = _load(run_dir)
    for name in ("frontend", "gateway", "orchestrator", "whisper", "controller", "engine"):
        took = _stop(state["pids"].get(name), wait_s=30)
        if took is not None:
            print(f"{name:<13} stopped in {took:.1f} s")
        state["pids"][name] = None
    if state.get("postgres_container"):
        subprocess.run(["docker", "stop", state["postgres_container"]], capture_output=True)
        print(f"postgres      container {state['postgres_container']} stopped")
        state["postgres_container"] = None
    _save(run_dir, state)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = _load(Path(args.run_dir).resolve())
    for name, port in state["ports"].items():
        pid = state["pids"].get(name)
        print(f"{name:<13} :{port:<6} pid {pid or '-':<8} {'running' if _alive(pid) else 'stopped'}")
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    print(_exports(_load(Path(args.run_dir).resolve())))
    return 0


def _restart(args: argparse.Namespace, name: str, start) -> int:
    run_dir = Path(args.run_dir).resolve()
    state = _load(run_dir)
    sig = getattr(signal, f"SIG{args.signal.upper()}")
    t0 = time.monotonic()
    took = _stop(state["pids"].get(name), sig=sig, wait_s=args.grace_s)
    print(f"{name}: SIG{args.signal.upper()} at t=0, exited after {took if took is not None else 0:.2f} s")
    if args.gap_s > 0:
        time.sleep(args.gap_s)
    start(state)
    # Merge into the file as it is NOW: another restart may have saved its own
    # pid while this one slept (two restarts at once lost a pid, 2026-09-14).
    latest = _load(run_dir)
    latest["pids"][name] = state["pids"][name]
    _save(run_dir, latest)
    print(f"{name}: replacement answering at t={time.monotonic() - t0:.1f} s")
    return 0


def cmd_restart_orchestrator(args: argparse.Namespace) -> int:
    return _restart(args, "orchestrator", _start_orchestrator)


def cmd_restart_frontend(args: argparse.Namespace) -> int:
    return _restart(args, "frontend", _start_frontend)


def cmd_restart_gateway(args: argparse.Namespace) -> int:
    return _restart(args, "gateway", _start_gateway)


def cmd_set_engine(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    path = run_dir / "engine-pacing.json"
    doc = json.loads(path.read_text()) if path.exists() else {}
    if args.token_delay_s is not None:
        doc["token_delay_s"] = args.token_delay_s
    if args.prefill_s is not None:
        doc["prefill_s"] = args.prefill_s
    path.write_text(json.dumps(doc))
    print(json.dumps(doc))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_run_dir(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--run-dir", required=True, help="where logs, keys and stack.json live")
        return p

    up = with_run_dir(sub.add_parser("up"))
    up.add_argument("--tree", default=str(Path(__file__).resolve().parents[1]), help="the repository checkout to run")
    up.add_argument("--database-url", help="a throwaway Postgres database the orchestrator may migrate")
    up.add_argument("--start-postgres", type=int, metavar="PORT", help="launch a throwaway postgres:18-alpine on PORT")
    up.add_argument("--python", help="the interpreter with the orchestrator's dependencies (default: this one)")
    up.add_argument("--node", help="the node binary for the gateway and Next (default: node on PATH)")
    up.add_argument("--frontend-direct", action="store_true", help="leave V1_GATEWAY_URL unset on the Next server")
    up.add_argument("--frontend-dir", help="a built frontend (default: TREE/frontend); Turbopack refuses a symlinked node_modules, so a build copy is common")
    up.add_argument("--token-delay-s", type=float, default=0.02, help="fake engine: seconds between tokens")
    up.add_argument("--prefill-s", type=float, default=0.0, help="fake engine: silence before the first token")
    for name in ("engine", "controller", "whisper", "orchestrator", "gateway", "frontend"):
        up.add_argument(f"--{name}-port", type=int)
    up.set_defaults(func=cmd_up)

    for name, func in (("down", cmd_down), ("status", cmd_status), ("env", cmd_env)):
        with_run_dir(sub.add_parser(name)).set_defaults(func=func)

    for name, func in (("restart-orchestrator", cmd_restart_orchestrator), ("restart-frontend", cmd_restart_frontend),
                       ("restart-gateway", cmd_restart_gateway)):
        p = with_run_dir(sub.add_parser(name))
        p.add_argument("--signal", default="TERM", help="TERM (a deploy) or KILL (a crash)")
        p.add_argument("--gap-s", type=float, default=0.0, help="seconds between the exit and the new start")
        p.add_argument("--grace-s", type=float, default=150.0, help="SIGKILL after this long")
        p.set_defaults(func=func)

    engine = with_run_dir(sub.add_parser("set-engine"))
    engine.add_argument("--token-delay-s", type=float)
    engine.add_argument("--prefill-s", type=float)
    engine.set_defaults(func=cmd_set_engine)

    fake_engine = sub.add_parser("serve-fake-engine")
    fake_engine.add_argument("--port", type=int, required=True)
    fake_engine.add_argument("--pacing-file", required=True)
    fake_engine.set_defaults(func=lambda a: _serve(_fake_engine_app(Path(a.pacing_file)), a.port) or 0)

    fake_controller = sub.add_parser("serve-fake-controller")
    fake_controller.add_argument("--port", type=int, required=True)
    fake_controller.add_argument("--engine-url", required=True)
    fake_controller.set_defaults(func=lambda a: _serve(_fake_controller_app(a.engine_url), a.port) or 0)

    fake_whisper = sub.add_parser("serve-fake-whisper")
    fake_whisper.add_argument("--port", type=int, required=True)
    fake_whisper.set_defaults(func=lambda a: _serve(_fake_whisper_app(), a.port) or 0)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
