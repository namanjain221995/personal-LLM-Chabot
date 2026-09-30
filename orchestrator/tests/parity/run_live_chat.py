"""The same prompt down the CHAT route, with the REAL chat system prompt.

This answers the route question with evidence instead of opinion: the chat
route's prompt (engines/__init__.py FORMAT_INSTRUCTION + DIAGRAM_INSTRUCTION
+ CODE_INSTRUCTION) asks for headings, subheadings, **bold**, tables, code
fences and mermaid diagrams -- every one of which the document schema has no
block for. So the question is not which prompt is better written, it is
whether the model actually delivers them when asked this way.

It builds the system prompt from the engine's own constants rather than
paraphrasing them, and prints exactly which pieces it could assemble, so a
piece that had to be skipped is visible rather than quietly missing.

Usage: run_live_chat.py [fast|think|max] [out.md]
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.request

#: The orchestrator package root, DERIVED from this file's own location:
#: orchestrator/tests/parity/run_live_chat.py -> orchestrator. Never written out as a
#: literal -- a hardcoded path publishes the operator's directory layout in a
#: public repository and breaks this script for everybody else. PARITY_ORCH
#: overrides it for running the harness against a different checkout.
ORCH = os.environ.get("PARITY_ORCH") or str(
    pathlib.Path(__file__).resolve().parents[2])
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ORCH)

BASE = "http://127.0.0.1:8000"
MODEL = "Qwen/Qwen3.6-35B-A3B-NVFP4"


def gpu_idle(timeout_s: float = 900.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        with urllib.request.urlopen(f"{BASE}/metrics", timeout=20) as r:
            body = r.read().decode()
        running = None
        for line in body.splitlines():
            if line.startswith("vllm:num_requests_running{"):
                running = float(line.rsplit(" ", 1)[1])
                break
        if running is None:
            raise SystemExit("cannot read vllm:num_requests_running -- refusing to run")
        if running == 0:
            print(f"[gate] engine idle ({running})", flush=True)
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"engine busy ({running}) after {timeout_s}s")
        print(f"[gate] busy ({running}) waiting", flush=True)
        time.sleep(10)


def build_system() -> str:
    from app.engines import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION, FORMAT_INSTRUCTION
    from app.engines.chat import ASSISTANT_SYSTEM

    parts = {
        "ASSISTANT_SYSTEM": ASSISTANT_SYSTEM,
        "FORMAT_INSTRUCTION": FORMAT_INSTRUCTION,
        "DIAGRAM_INSTRUCTION": DIAGRAM_INSTRUCTION,
        "CODE_INSTRUCTION": CODE_INSTRUCTION,
    }
    try:
        from app.identity import identity_line
        parts["identity_line"] = identity_line()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] identity_line unavailable ({type(exc).__name__}); "
              "system prompt assembled without it", flush=True)
    print("[system] assembled from: " + ", ".join(
        f"{k}({len(v)}c)" for k, v in parts.items()), flush=True)
    return "".join(parts.values())


def main() -> None:
    effort = sys.argv[1] if len(sys.argv) > 1 else "think"
    # Default into runs/candidates/, never into runs/ itself: runs/ holds
    # the frozen baselines and every one of them is pinned by name in
    # BASELINE_SCORES.json. A new recording is a CANDIDATE until it is
    # declared in runs/candidates/INDEX.json with the track that owes it.
    out = (sys.argv[2] if len(sys.argv) > 2
           else f"{HERE}/runs/candidates/live_chat_{effort}.md")

    # Matches engines/chat.py: Fast never thinks; Think and Max do.
    thinking = effort != "fast"
    system = build_system()
    user = open(f"{HERE}/prompt.txt", encoding="utf-8").read().strip()

    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": 32_000,
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})

    gpu_idle()
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=3600) as r:
        data = json.load(r)
    wall = time.perf_counter() - t0

    choice = data["choices"][0]
    text = choice["message"]["content"] or ""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write(text)
    meta = {"effort": effort, "thinking": thinking, "wall_clock_s": round(wall, 2),
            "finish_reason": choice.get("finish_reason"), "usage": data.get("usage"),
            "system_chars": len(system)}
    open(out.replace(".md", ".meta.json"), "w").write(json.dumps(meta, indent=2))
    print("[meta] " + json.dumps(meta), flush=True)
    print(f"[written] {out}", flush=True)


if __name__ == "__main__":
    main()
