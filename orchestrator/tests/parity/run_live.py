"""Run the REAL composer in-process at one effort and record what came back.

Usage:  run_live.py fast|think|max  [out.json]

This imports the orchestrator's own `app.artifacts.compose` and calls
`compose()` -- the same function the artifact pipeline calls -- against the
live vLLM. Nothing is stubbed: the real prompt, the real JSON schema, the
real repair and review loop.

It does NOT render or store, so it writes nothing to the production database
and creates no artifact in anyone's conversation.

GPU GATE. The engine is shared with live users and a second tenant, so the
run waits for `vllm:num_requests_running` to be 0 before it starts, exactly
as the standing rules require, and refuses to start if it cannot read the
gauge.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
import urllib.request

#: The orchestrator package root, DERIVED from this file's own location:
#: orchestrator/tests/parity/run_live.py -> orchestrator. Never written out as a
#: literal -- a hardcoded path publishes the operator's directory layout in a
#: public repository and breaks this script for everybody else. PARITY_ORCH
#: overrides it for running the harness against a different checkout.
ORCH = os.environ.get("PARITY_ORCH") or str(
    pathlib.Path(__file__).resolve().parents[2])
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ORCH)
sys.path.insert(0, HERE)

os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
os.environ.setdefault("OPENAI_API_KEY", "x")
os.environ.setdefault("MAIN_MODEL", "Qwen/Qwen3.6-35B-A3B-NVFP4")
os.environ.setdefault("LLM_MODEL", "Qwen/Qwen3.6-35B-A3B-NVFP4")


def gpu_idle(timeout_s: float = 900.0) -> None:
    """Wait for the shared engine to be free. Raises rather than guessing."""
    deadline = time.monotonic() + timeout_s
    while True:
        with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=20) as r:
            body = r.read().decode()
        running = None
        for line in body.splitlines():
            if line.startswith("vllm:num_requests_running{"):
                running = float(line.rsplit(" ", 1)[1])
                break
        if running is None:
            raise SystemExit("cannot read vllm:num_requests_running -- refusing to run")
        if running == 0:
            print(f"[gate] engine idle (num_requests_running={running})", flush=True)
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"engine still busy ({running}) after {timeout_s}s")
        print(f"[gate] engine busy ({running}), waiting...", flush=True)
        time.sleep(10)


async def main() -> None:
    effort = sys.argv[1]
    # Default into runs/candidates/, never into runs/ itself: runs/ holds
    # the frozen baselines and every one of them is pinned by name in
    # BASELINE_SCORES.json. A new recording is a CANDIDATE until it is
    # declared in runs/candidates/INDEX.json with the track that owes it.
    out_path = (sys.argv[2] if len(sys.argv) > 2
                else f"{HERE}/runs/candidates/live_{effort}.json")

    from app.artifacts import compose as C, types as T

    instruction = open(f"{HERE}/prompt.txt", encoding="utf-8").read().strip()
    budget = T.EFFORT_BUDGETS[effort]

    req = C.ComposeRequest(
        kind="document",
        formats=["pdf", "docx"],
        template_id="generic",
        effort=effort,
        operation="create",
        material=C.Material(instruction=instruction),
        instruction=instruction,
    )

    # What the composer decided BEFORE it spoke to the model -- the numbers
    # that explain the answer.
    target = C.target_for(req)
    requested = C.requested_sections(instruction)
    caps = C.caps_for(budget, target, requested)
    decided = {
        "effort": effort,
        "budget": {k: getattr(budget, k) for k in
                   ("thinking", "outline_pass", "research", "content_review",
                    "visual_qa", "max_corrections", "max_sections")},
        "target_words": target.words,
        "target_phrase": target.phrase,
        "target_explicit": target.explicit,
        "requested_sections": requested,
        "caps_sections": caps[0],
        "max_tokens_one_call": C._max_tokens_for("document", effort, target),
        "sectioned_writer": bool(target.words > C.SECTIONED_WRITER_WORDS),
        "sectioned_threshold": C.SECTIONED_WRITER_WORDS,
    }
    print("[decided] " + json.dumps(decided, indent=2), flush=True)

    stages = []

    async def say(pct, msg):
        stages.append({"t": round(time.monotonic() - t0, 2), "pct": pct, "msg": msg})
        print(f"  [{time.monotonic()-t0:7.1f}s] {pct:5.1f}% {msg}", flush=True)

    gpu_idle()
    t0 = time.monotonic()
    err = None
    result = None
    try:
        result = await C.compose(req, progress=say)
    except Exception as exc:  # noqa: BLE001 -- a failure IS the measurement
        err = f"{type(exc).__name__}: {exc}"
        print("[FAILED] " + err, flush=True)
    wall = time.monotonic() - t0

    payload = {
        "decided": decided,
        "wall_clock_s": round(wall, 2),
        "stages": stages,
        "error": err,
    }
    if result is not None:
        payload["warnings"] = list(result.warnings)
        payload["corrections"] = result.corrections
        payload["model_calls"] = result.model_calls
        payload["outline"] = result.outline
        payload["review"] = result.review
        payload["spec"] = json.loads(result.spec.model_dump_json(exclude_none=True))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[written] {out_path}  wall={wall:.1f}s calls={payload.get('model_calls')}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
