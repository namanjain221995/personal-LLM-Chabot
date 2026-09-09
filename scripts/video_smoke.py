#!/usr/bin/env python3
"""End-to-end smoke for video understanding, against a RUNNING deployment.

    scripts/video_smoke.py --video meeting.mp4 \\
        --ask "what did they decide about the Team tier?" \\
        --ask "read the code on the slide"

It does exactly what the browser does, in order: sign in, upload the video
(purpose=video, chunked past 90 MB like the composer), send a chat turn that
references it, follow the SSE stream — printing every `step` with a clock,
then the answer — and ask the follow-ups in the same conversation. It prints
the analysis status at the end and writes a JSON report next to the video
(`<video>.smoke.json`) with per-stage timings, the answers, and the files.

Credentials come from the environment (VIDEO_SMOKE_EMAIL / _PASSWORD) so a
password is never on a command line. `--base` defaults to the orchestrator
on this box; `--via-frontend http://127.0.0.1:3000` sends the same traffic
through the Next.js route handlers instead — the exact path a browser takes
(/api/upload, /api/chat, /api/reports/…), which is what a deploy is verified
with. The status route has no proxy, so that step is skipped there.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx

CHUNK_THRESHOLD = 90 * 1024 * 1024
CHUNK_PART = 64 * 1024 * 1024


def _clock(t0: float) -> str:
    s = int(time.perf_counter() - t0)
    return f"{s // 60}:{s % 60:02d}"


def login(client: httpx.Client, base: str, email: str, password: str) -> dict:
    r = client.post(f"{base}{PATHS['auth']}/login", json={"email": email, "password": password, "remember": True})
    r.raise_for_status()
    # The session cookie is `Secure` on this deployment; httpx's jar will not
    # replay it over plain http://, so it is pinned as a header — exactly the
    # bytes the browser would send through the HTTPS front door.
    raw = r.headers.get("set-cookie", "")
    pair = raw.split(";", 1)[0].strip()
    if "=" in pair:
        client.headers["Cookie"] = pair
    me = client.get(f"{base}{PATHS['auth']}/me")
    me.raise_for_status()
    return me.json()


#: Route names differ between the orchestrator and the Next.js proxies.
_PATHS = {
    "orchestrator": {"upload": "/uploads", "chunked": "/uploads/chunked", "chat": "/chat", "reports": "/reports", "auth": "/auth", "video": "/video"},
    "frontend": {"upload": "/api/upload", "chunked": "/api/upload/chunked", "chat": "/api/chat", "reports": "/api/reports", "auth": "/api/auth", "video": None},
}
PATHS = _PATHS["orchestrator"]


def upload(client: httpx.Client, base: str, conversation_id: str, path: str) -> dict:
    size = os.path.getsize(path)
    name = os.path.basename(path)
    if size <= CHUNK_THRESHOLD:
        with open(path, "rb") as fh:
            r = client.post(
                f"{base}{PATHS['upload']}",
                files={"file": (name, fh, "video/mp4")},
                data={"conversation_id": conversation_id, "purpose": "video"},
                timeout=600.0,
            )
        r.raise_for_status()
        return r.json()
    r = client.post(
        f"{base}{PATHS['chunked']}/init",
        data={"conversation_id": conversation_id, "filename": name, "purpose": "video"},
    )
    r.raise_for_status()
    upload_id = r.json()["upload_id"]
    parts = (size + CHUNK_PART - 1) // CHUNK_PART
    with open(path, "rb") as fh:
        for i in range(parts):
            chunk = fh.read(CHUNK_PART)
            r = client.put(
                f"{base}{PATHS['chunked']}/{quote(conversation_id)}/{upload_id}/part/{i}",
                content=chunk,
                timeout=600.0,
            )
            r.raise_for_status()
            print(f"  part {i + 1}/{parts} uploaded", flush=True)
    r = client.post(f"{base}{PATHS['chunked']}/{quote(conversation_id)}/{upload_id}/complete", timeout=600.0)
    r.raise_for_status()
    return r.json()


def chat(
    client: httpx.Client,
    base: str,
    *,
    conversation_id: str,
    message: str,
    history: List[dict],
    video_uploads: Optional[List[dict]] = None,
    effort: str = "think",
    t0: float,
) -> dict:
    body = {
        "message": message,
        "messages": [*history, {"role": "user", "content": message}],
        "session_id": conversation_id,
        "conversation_id": conversation_id,
        "mode": "assistant",
        "model": "smart",
        "effort": effort,
        "web_search": "off",
    }
    if video_uploads:
        body["video_uploads"] = video_uploads
    steps: Dict[int, dict] = {}
    tokens: List[str] = []
    meta: dict = {}
    first_token: Optional[float] = None
    started = time.perf_counter()
    with client.stream("POST", f"{base}{PATHS['chat']}", json=body, timeout=httpx.Timeout(10.0, read=7200.0)) as r:
        if r.status_code != 200:
            raise SystemExit(f"/chat -> {r.status_code}: {r.read()[:400]!r}")
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:].strip() or "{}")
            except json.JSONDecodeError:
                continue
            if event == "step":
                sid = int(data.get("id", 0))
                prev = steps.get(sid) or {}
                steps[sid] = {**prev, **data, "seen_at": time.perf_counter() - started}
                if data.get("status") != "running" or prev.get("status") != "running" or data.get("detail", "") != prev.get("detail", ""):
                    print(f"  [{_clock(t0)}] step {sid} {data.get('title')}: {data.get('status')} {data.get('detail', '')}", flush=True)
            elif event == "status":
                pass
            elif event == "token":
                if first_token is None:
                    first_token = time.perf_counter() - started
                tokens.append(data.get("text", ""))
            elif event == "meta":
                meta = data
            elif event == "error":
                raise SystemExit(f"stream error: {data}")
            elif event == "done":
                break
    answer = "".join(tokens)
    return {
        "answer": answer,
        "meta": meta,
        "steps": [steps[k] for k in sorted(steps)],
        "seconds": round(time.perf_counter() - started, 1),
        "first_token_s": round(first_token, 1) if first_token is not None else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("VIDEO_SMOKE_BASE", "http://127.0.0.1:8080"))
    ap.add_argument("--video", required=True)
    ap.add_argument("--ask", action="append", default=[], help="a follow-up question (repeatable)")
    ap.add_argument("--conversation", default=None, help="reuse a conversation id (default: a new one)")
    ap.add_argument("--first", default="", help="the message sent WITH the video (default: none → the overview)")
    ap.add_argument("--effort", default="think", choices=["fast", "think", "max"])
    ap.add_argument("--report", default=None, help="where to write the JSON report")
    ap.add_argument("--via-frontend", default=None, metavar="URL", help="send everything through the Next.js proxies at URL (e.g. http://127.0.0.1:3000)")
    args = ap.parse_args()
    global PATHS
    if args.via_frontend:
        PATHS = _PATHS["frontend"]
        args.base = args.via_frontend

    email = os.environ.get("VIDEO_SMOKE_EMAIL")
    password = os.environ.get("VIDEO_SMOKE_PASSWORD")
    if not email or not password:
        print("set VIDEO_SMOKE_EMAIL and VIDEO_SMOKE_PASSWORD", file=sys.stderr)
        return 2
    base = args.base.rstrip("/")
    t0 = time.perf_counter()
    report: dict = {"video": args.video, "base": base, "turns": []}

    # No Origin header: the orchestrator's CSRF layer refuses a state-changing
    # request whose Origin is not one of the frontend's, and a script is not
    # a browser. The Next.js proxy strips Origin for the same reason.
    with httpx.Client(follow_redirects=True) as client:
        me = login(client, base, email, password)
        print(f"[{_clock(t0)}] signed in as {me.get('user', {}).get('name') or me.get('username')} · video_analysis={me.get('features', {}).get('video_analysis')}")
        conversation_id = args.conversation or f"smoke-{int(time.time())}"
        print(f"[{_clock(t0)}] uploading {args.video} ({os.path.getsize(args.video) / 1e6:.1f} MB) into {conversation_id}")
        up_started = time.perf_counter()
        up = upload(client, base, conversation_id, args.video)
        report["upload"] = {**up, "seconds": round(time.perf_counter() - up_started, 1)}
        print(f"[{_clock(t0)}] upload done in {report['upload']['seconds']}s → {json.dumps(up.get('video'))}")

        history: List[dict] = []
        refs = [{"upload_id": up["upload_id"], "name": up["filename"]}]
        first = args.first or "Analyze the attached video."
        print(f"[{_clock(t0)}] chat: {first!r} (with the video)")
        turn = chat(client, base, conversation_id=conversation_id, message=first, history=history, video_uploads=refs, effort=args.effort, t0=t0)
        print(f"[{_clock(t0)}] answered in {turn['seconds']}s (first token {turn['first_token_s']}s)\n")
        print(turn["answer"][:6000])
        print()
        files = turn["meta"].get("report_files") or []
        print(f"  files: {[f['filename'] for f in files]}")
        history += [{"role": "user", "content": first}, {"role": "assistant", "content": turn["answer"]}]
        report["turns"].append({"message": first, **turn})

        for q in args.ask:
            print(f"\n[{_clock(t0)}] ask: {q!r}")
            turn = chat(client, base, conversation_id=conversation_id, message=q, history=history, effort=args.effort, t0=t0)
            print(f"[{_clock(t0)}] answered in {turn['seconds']}s (first token {turn['first_token_s']}s) · route={turn['meta'].get('route')} · evidence={len((turn['meta'].get('video') or {}).get('evidence') or [])} · frames={(turn['meta'].get('video') or {}).get('frames_shown')}\n")
            print(turn["answer"][:3000])
            history += [{"role": "user", "content": q}, {"role": "assistant", "content": turn["answer"]}]
            report["turns"].append({"message": q, **turn})

        if PATHS["video"]:
            st = client.get(f"{base}{PATHS['video']}/{quote(conversation_id)}/{up['upload_id']}/status")
            report["status"] = st.json() if st.status_code == 200 else {"http": st.status_code}
        else:
            report["status"] = {"skipped": "no status proxy on the frontend"}
        print(f"\n[{_clock(t0)}] status: {report['status'].get('status')} · stages:")
        for s in report["status"].get("stages", []):
            ms = s.get("ms")
            print(f"    {s['stage']:11s} {s['status']:8s} {'' if ms is None else f'{ms / 1000:7.1f}s'}  {s.get('detail', '')}")
        # Download one artifact through the reports route, as the UI would.
        if files:
            name = files[0]["filename"]
            r = client.get(f"{base}{PATHS['reports']}/{quote(name)}")
            print(f"  GET {PATHS['reports']}/{name} -> {r.status_code}, {len(r.content)} bytes")
            report["artifact_probe"] = {"filename": name, "status": r.status_code, "bytes": len(r.content), "head": r.text[:300]}

    out = args.report or (args.video + ".smoke.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
