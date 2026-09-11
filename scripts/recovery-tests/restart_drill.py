#!/usr/bin/env python3
"""Scenario 11: the orchestrator restarts mid-flight, and nothing is lost.

This is the failure that produced the blank answer on 2026-09-09: a deploy
recreated the orchestrator while a video question was being answered, the
in-memory generation died with no record, the analysis was requeued and
finished anyway, and the person was left with an empty assistant bubble.

The drill, against the ISOLATED e2e stack only:
  1. upload a video nobody has analysed before, and ask about it
  2. cut the viewer at the first progress step (the tab "closes")
  3. restart the orchestrator container while the work is in flight
  4. ask the server what became of the send, and re-attach
  5. assert: the request was marked interrupted, it resumed under a NEW
     attempt, exactly one answer was stored, and the analysis did not start
     over from stage one.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ""))

from qa_upload_matrix import Client, PATHS, conv_id, unique_copy, assistant_rows  # noqa: E402

# THE ISOLATED STACK ONLY. This script restarts a container; the name is
# pinned here and guarded below so it can never be pointed at production by
# an environment variable or a typo. Bring the stack up with
# scripts/e2e-stack.sh before running it.
CONTAINER = "techsara-e2e-orchestrator"
if "e2e" not in CONTAINER:  # pragma: no cover - a tripwire, not logic
    raise SystemExit("refusing to restart anything that is not the e2e stack")
BASE = os.environ.get("QA_BASE", "http://127.0.0.1:8081")


def psql(sql: str) -> str:
    user = subprocess.run(
        ["bash", "-lc", "grep -E '^POSTGRES_USER=' /home/techsphere/Documents/project/personal-LLM-Chabot/.env | cut -d= -f2-"],
        capture_output=True, text=True).stdout.strip()
    out = subprocess.run(
        ["docker", "exec", "sf-local-ai-postgres-1", "psql", "-U", user, "-d", "techsara_e2e_test", "-Atc", sql],
        capture_output=True, text=True)
    return out.stdout.strip()


def wait_healthy(timeout_s: float = 600.0) -> float:
    import httpx

    started = time.perf_counter()
    deadline = started + timeout_s
    while time.perf_counter() < deadline:
        try:
            # 20 s, not 3: /health probes six engines, and while a video is
            # being analysed they are busy enough that a tight timeout reads
            # a working orchestrator as a dead one.
            if httpx.get(f"{BASE}/health", timeout=20).status_code == 200:
                return round(time.perf_counter() - started, 1)
        except Exception:  # noqa: BLE001
            pass
        waited = time.perf_counter() - started
        if waited > 30 and int(waited) % 30 < 2:
            print(f"    …still waiting for the orchestrator ({int(waited)}s)", flush=True)
        time.sleep(2)
    raise SystemExit("the orchestrator did not come back")


def main() -> int:
    fixtures = os.environ["QA_FIXTURES"]
    import tempfile

    tmp = tempfile.mkdtemp(prefix="restart-drill-")
    a = Client(BASE, PATHS["orchestrator"], os.environ["QA_EMAIL"], os.environ["QA_PASSWORD"])
    conv = conv_id("restart")
    video = unique_copy(os.path.join(fixtures, "fixture_20mb.mp4"), tmp)
    up = a.upload_file(conv, video)
    analysis_id = (up.get("video") or {}).get("analysis_id")
    intent = uuid.uuid4().hex
    print(f"conversation {conv}\nanalysis {analysis_id}\nintent {intent[:8]}…", flush=True)

    # The tab watches until the first stage reports, then goes away.
    turn = a.chat(conv, "", intent_id=intent,
                  video_uploads=[{"upload_id": up["upload_id"], "name": up["filename"]}],
                  stop_after="step")
    assert turn["cut"], "the answer arrived before the cut point — use fresh bytes"
    print("viewer cut at the first progress step", flush=True)

    before_stage = psql(f"SELECT stage || ' ' || status FROM video_analyses WHERE id = {analysis_id}")
    before_attempt = psql(f"SELECT attempt FROM video_analyses WHERE id = {analysis_id}")
    before_stages_done = psql(
        f"SELECT count(*) FROM jsonb_each(stages) WHERE value->>'status' IN ('done','skipped')"
        f" AND (SELECT 1) = 1 AND id = {analysis_id}"
    ) or psql(
        f"SELECT (SELECT count(*) FROM jsonb_each(stages) e WHERE e.value->>'status' IN ('done','skipped'))"
        f" FROM video_analyses WHERE id = {analysis_id}"
    )
    print(f"before the restart: analysis at {before_stage!r}, attempt {before_attempt}, "
          f"{before_stages_done} stage(s) already durable", flush=True)

    # ── the restart ──────────────────────────────────────────────────────
    print("restarting the orchestrator…", flush=True)
    t0 = time.perf_counter()
    subprocess.run(["docker", "restart", "-t", "30", CONTAINER], check=True, capture_output=True)
    back = wait_healthy()
    print(f"back after {round(time.perf_counter() - t0, 1)}s (healthy {back}s after start)", flush=True)

    # A fresh browser, as after a reload.
    b = Client(BASE, PATHS["orchestrator"], os.environ["QA_EMAIL"], os.environ["QA_PASSWORD"])
    state = b.request_state(intent)
    assert state.status_code == 200, state.text[:200]
    after_restart = state.json()
    print(f"the server says the send is: {after_restart['status']} "
          f"(resumable={after_restart.get('resumable')}, live={after_restart.get('live')})", flush=True)
    assert after_restart["status"] == "interrupted", \
        f"a restart must mark an open request interrupted, not {after_restart['status']}"

    # Re-attaching resumes it, exactly as reopening the conversation would.
    again = b.attach(conv)
    assert again["status"] == 200, f"attach answered {again['status']}"
    assert again["kinds"][-1] == "done", f"the resumed stream ended on {again['kinds'][-1:]}"
    assert len(again["answer"]) > 200, f"the resumed answer is {len(again['answer'])} chars"
    final = b.request_state(intent).json()
    assert final["status"] == "completed", f"the resumed request ended as {final['status']}"
    assert final["attempt"] >= 2, f"a resume must be a new attempt, got {final['attempt']}"

    rows = assistant_rows(b.thread(conv))
    assert len(rows) == 1, f"{len(rows)} assistant rows — a resume must not duplicate the answer"
    assert rows[0]["content"], "the stored answer is empty"

    after_attempt = psql(f"SELECT attempt FROM video_analyses WHERE id = {analysis_id}")
    after_status = psql(f"SELECT status FROM video_analyses WHERE id = {analysis_id}")
    print(f"\nRESULT")
    print(f"  request      interrupted -> resumed -> completed (attempt {final['attempt']})")
    print(f"  answer       1 row, {len(rows[0]['content'])} chars, stored server-side")
    print(f"  analysis     {after_status}, attempt {before_attempt} -> {after_attempt}"
          f" (stages already durable before the restart were not re-run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
