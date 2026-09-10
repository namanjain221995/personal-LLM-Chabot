#!/usr/bin/env python3
"""The upload-reliability QA matrix, driven against a RUNNING deployment.

    scripts/qa_upload_matrix.py --base http://127.0.0.1:8081 --fixtures <dir>

Every scenario here is one a mocked test cannot prove: real bytes over the
real rails, a real analysis on the real engines, and interruptions produced
by actually dropping a connection rather than by a stub pretending to.

WHAT MAKES A SCENARIO DETERMINISTIC. Nothing here sleeps and hopes. An
interruption is produced by closing the socket at a named point (after part
2, after the first token, before the history push), and the assertion is
made against the SERVER's own view afterwards — the upload session, the chat
request, the stored thread. A run either passes at that named point or names
the point it failed at.

CREDENTIALS come from the environment (QA_EMAIL / QA_PASSWORD, and
QA_EMAIL_2 / QA_PASSWORD_2 for the cross-user checks) so nothing sensitive
reaches a command line or this file.

This talks to the ORCHESTRATOR by default. `--via-frontend URL` sends the
same traffic through the Next.js route handlers instead, which is the path a
browser actually takes; the two runs together are what "the proxies do not
change the contract" means.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

CHUNK_THRESHOLD = 90 * 1024 * 1024
PART = 64 * 1024 * 1024

PATHS = {
    "orchestrator": {
        "auth": "/auth", "upload": "/uploads", "chunked": "/uploads/chunked",
        "chat": "/chat", "requests": "/chat/requests", "attach": "/chat/attach",
        "active": "/chat/active", "stop": "/chat/stop", "history": "/history",
        "reports": "/reports", "video": "/video",
    },
    "frontend": {
        "auth": "/api/auth", "upload": "/api/upload", "chunked": "/api/upload/chunked",
        "chat": "/api/chat", "requests": "/api/chat/requests", "attach": "/api/chat/attach",
        "active": "/api/chat/active", "stop": "/api/chat/stop", "history": "/api/history",
        "reports": "/api/reports", "video": None,
    },
}


# --------------------------------------------------------------- results --


@dataclass
class Case:
    name: str
    number: str
    status: str = "not run"      # pass | fail | skipped | blocked
    detail: str = ""
    seconds: float = 0.0
    facts: Dict[str, Any] = field(default_factory=dict)


class Report:
    def __init__(self) -> None:
        self.cases: List[Case] = []

    def run(self, number: str, name: str, fn: Callable[[], Dict[str, Any]]) -> Case:
        case = Case(name=name, number=number)
        started = time.perf_counter()
        try:
            case.facts = fn() or {}
            case.status = "pass"
        except Skip as exc:
            case.status = "skipped"
            case.detail = str(exc)
        except AssertionError as exc:
            case.status = "fail"
            case.detail = str(exc) or "assertion failed"
        except Exception as exc:  # noqa: BLE001 — a driver crash is a result too
            case.status = "fail"
            case.detail = f"{type(exc).__name__}: {exc}"
        case.seconds = round(time.perf_counter() - started, 1)
        self.cases.append(case)
        mark = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}.get(case.status, "????")
        print(f"  {mark}  {number:5s} {name} ({case.seconds}s){(' — ' + case.detail) if case.detail else ''}", flush=True)
        for key, value in case.facts.items():
            print(f"           {key}: {value}", flush=True)
        return case

    def summary(self) -> Tuple[int, int, int]:
        p = sum(1 for c in self.cases if c.status == "pass")
        f = sum(1 for c in self.cases if c.status == "fail")
        s = sum(1 for c in self.cases if c.status == "skipped")
        return p, f, s


class Skip(Exception):
    pass


# ----------------------------------------------------------------- client --


class Client:
    """One signed-in browser, as far as the server can tell."""

    def __init__(self, base: str, paths: Dict[str, Optional[str]], email: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.p = paths
        # No Origin header: the CSRF layer refuses a state-changing request
        # whose Origin is not the frontend's, and a script is not a browser.
        self.http = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=600.0))
        r = self.http.post(f"{self.base}{self.p['auth']}/login", json={"email": email, "password": password, "remember": True})
        r.raise_for_status()
        # The session cookie is Secure, so httpx's jar will not replay it over
        # plain http://; pin the exact bytes the browser would send.
        pair = r.headers.get("set-cookie", "").split(";", 1)[0].strip()
        if "=" in pair:
            self.http.headers["Cookie"] = pair
        me = self.http.get(f"{self.base}{self.p['auth']}/me")
        me.raise_for_status()
        self.me = me.json()

    def close(self) -> None:
        self.http.close()

    # -- uploads ----------------------------------------------------------

    def init_chunked(self, conv: str, filename: str, purpose: str, size: int, parts: int, part_size: int) -> dict:
        r = self.http.post(
            f"{self.base}{self.p['chunked']}/init",
            data={"conversation_id": conv, "filename": filename, "purpose": purpose,
                  "size": str(size), "parts": str(parts), "part_size": str(part_size)},
        )
        r.raise_for_status()
        return r.json()

    def put_part(self, conv: str, upload_id: str, index: int, blob: bytes, *, with_hash: bool = True,
                 truncate_to: Optional[int] = None) -> httpx.Response:
        headers = {}
        if with_hash:
            headers["X-Part-SHA256"] = hashlib.sha256(blob).hexdigest()
        body = blob if truncate_to is None else blob[:truncate_to]
        if truncate_to is not None:
            # A body that stops early WITHOUT closing cleanly is what a closed
            # tab looks like on the wire. httpx cannot half-close, so declare
            # the full length and send less: the server sees the stream end
            # before content-length, which is the same condition.
            headers["Content-Length"] = str(len(blob))
            return self._raw_put(conv, upload_id, index, body, headers)
        return self.http.put(f"{self.base}{self.p['chunked']}/{conv}/{upload_id}/part/{index}", content=body, headers=headers)

    def _raw_put(self, conv: str, upload_id: str, index: int, body: bytes, headers: Dict[str, str]) -> httpx.Response:
        """A PUT whose body is deliberately short of its declared length."""
        def short_body():
            yield body
        try:
            return self.http.put(
                f"{self.base}{self.p['chunked']}/{conv}/{upload_id}/part/{index}",
                content=short_body(), headers=headers,
            )
        except httpx.HTTPError as exc:
            return httpx.Response(599, request=httpx.Request("PUT", "http://x"), text=str(exc))

    def session(self, conv: str, upload_id: str) -> httpx.Response:
        return self.http.get(f"{self.base}{self.p['chunked']}/{conv}/{upload_id}")

    def complete(self, conv: str, upload_id: str) -> httpx.Response:
        return self.http.post(f"{self.base}{self.p['chunked']}/{conv}/{upload_id}/complete", timeout=httpx.Timeout(30.0, read=900.0))

    def cancel(self, conv: str, upload_id: str) -> httpx.Response:
        return self.http.delete(f"{self.base}{self.p['chunked']}/{conv}/{upload_id}")

    def upload_single(self, conv: str, filename: str, blob: bytes, purpose: str) -> httpx.Response:
        return self.http.post(
            f"{self.base}{self.p['upload']}",
            files={"file": (filename, blob, "video/mp4")},
            data={"conversation_id": conv, "purpose": purpose},
            timeout=httpx.Timeout(30.0, read=900.0),
        )

    def upload_file(self, conv: str, path: str, purpose: str = "video") -> dict:
        """The whole file, the way the composer sends it."""
        size = os.path.getsize(path)
        name = os.path.basename(path)
        if size <= CHUNK_THRESHOLD:
            r = self.upload_single(conv, name, open(path, "rb").read(), purpose)
            r.raise_for_status()
            return r.json()
        parts = (size + PART - 1) // PART
        init = self.init_chunked(conv, name, purpose, size, parts, PART)
        with open(path, "rb") as fh:
            for i in range(parts):
                blob = fh.read(PART)
                r = self.put_part(conv, init["upload_id"], i, blob)
                r.raise_for_status()
        r = self.complete(conv, init["upload_id"])
        r.raise_for_status()
        return r.json()

    # -- chat -------------------------------------------------------------

    def chat(self, conv: str, message: str, *, intent_id: Optional[str] = None,
             video_uploads: Optional[List[dict]] = None, history: Optional[List[dict]] = None,
             effort: str = "fast", stop_after: Optional[str] = None,
             stop_after_n: int = 1) -> dict:
        """One POST /chat, read as SSE.

        `stop_after` closes the connection the moment the nth event of that
        kind has been read — the deterministic stand-in for a tab that goes
        away mid-answer.
        """
        body = {
            "message": message,
            "messages": [*(history or []), {"role": "user", "content": message}],
            "session_id": conv, "conversation_id": conv,
            "mode": "assistant", "model": "smart", "effort": effort, "web_search": "off",
        }
        if intent_id:
            body["intent_id"] = intent_id
        if video_uploads:
            body["video_uploads"] = video_uploads
        return self._stream("POST", f"{self.base}{self.p['chat']}", json=body, stop_after=stop_after, stop_after_n=stop_after_n)

    def attach(self, conv: str, *, stop_after: Optional[str] = None) -> dict:
        return self._stream("GET", f"{self.base}{self.p['attach']}/{conv}", stop_after=stop_after)

    def _stream(self, method: str, url: str, *, json: Optional[dict] = None,
                stop_after: Optional[str] = None, stop_after_n: int = 1) -> dict:
        events: List[Tuple[str, dict]] = []
        tokens: List[str] = []
        status = None
        seen = 0
        cut = False
        import json as _json
        kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(30.0, read=1800.0)}
        if json is not None:
            kwargs["json"] = json
        with self.http.stream(method, url, **kwargs) as r:
            status = r.status_code
            if r.status_code != 200:
                return {"status": status, "events": [], "answer": "", "body": r.read()[:400].decode("utf-8", "replace")}
            kind = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    kind = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                try:
                    data = _json.loads(line[5:].strip() or "{}")
                except ValueError:
                    continue
                events.append((kind or "", data))
                if kind == "token":
                    tokens.append(data.get("text", ""))
                if stop_after and kind == stop_after:
                    seen += 1
                    if seen >= stop_after_n:
                        cut = True
                        break
                if kind == "done":
                    break
        return {"status": status, "events": events, "answer": "".join(tokens), "cut": cut,
                "kinds": [k for k, _ in events],
                "meta": next((d for k, d in reversed(events) if k == "meta"), {}),
                "leading": next((d for k, d in events if k == "meta"), {})}

    def request_state(self, intent_id: str) -> httpx.Response:
        return self.http.get(f"{self.base}{self.p['requests']}/{intent_id}")

    def active(self) -> httpx.Response:
        return self.http.get(f"{self.base}{self.p['active']}")

    def thread(self, conv: str) -> dict:
        r = self.http.get(f"{self.base}{self.p['history']}/conversations/{conv}")
        r.raise_for_status()
        return r.json()

    def video_status(self, conv: str, upload_id: str) -> Optional[dict]:
        if not self.p.get("video"):
            return None
        r = self.http.get(f"{self.base}{self.p['video']}/{conv}/{upload_id}/status")
        return r.json() if r.status_code == 200 else {"http": r.status_code}


def conv_id(tag: str) -> str:
    return f"qa-{tag}-{uuid.uuid4().hex[:8]}"


def assistant_rows(thread: dict) -> List[dict]:
    return [m for m in thread.get("messages", []) if m.get("role") == "assistant"]


# -------------------------------------------------------------- scenarios --


def scenarios(a: Client, b: Optional[Client], fixtures: str, report: Report, *, heavy: bool) -> None:
    """`a` is the person under test; `b` is a second account, for isolation."""
    small = os.path.join(fixtures, "fixture_20mb.mp4")
    under = os.path.join(fixtures, "fixture_89mb.mp4")   # below the chunk threshold
    over = os.path.join(fixtures, "fixture_97mb.mp4")    # above it: two parts
    big = os.path.join(fixtures, "fixture_200mb.mp4")    # four parts
    for path in (small, under, over):
        if not os.path.exists(path):
            raise SystemExit(f"missing fixture {path} — build them first")

    # 1 ─ a normal single MP4, selection to a persisted answer.
    def case1() -> dict:
        conv = conv_id("normal")
        up = a.upload_file(conv, small)
        intent = uuid.uuid4().hex
        turn = a.chat(conv, "", intent_id=intent, video_uploads=[{"upload_id": up["upload_id"], "name": up["filename"]}])
        assert turn["status"] == 200, f"chat answered {turn['status']}"
        assert turn["kinds"][-1] == "done", f"stream ended on {turn['kinds'][-1:]}"
        assert len(turn["answer"]) > 200, "the answer is too short to be an understanding"
        rows = assistant_rows(a.thread(conv))
        assert len(rows) == 1, f"{len(rows)} assistant rows persisted, expected 1"
        assert rows[0]["content"], "the persisted answer is empty"
        state = a.request_state(intent)
        assert state.status_code == 200 and state.json()["status"] == "completed", state.text[:200]
        assert state.json()["answer_persisted"] is True
        return {"analysis": up.get("video", {}).get("analysis_id"), "answer chars": len(turn["answer"])}

    report.run("1", "a normal MP4 from selection to a persisted answer", case1)

    # 2a ─ either side of the chunk threshold.
    def case2a() -> dict:
        c1, c2 = conv_id("under"), conv_id("over")
        u1 = a.upload_file(c1, under)
        u2 = a.upload_file(c2, over)
        assert u1["bytes"] == os.path.getsize(under), "single-shot upload lost bytes"
        assert u2["bytes"] == os.path.getsize(over), "chunked upload lost bytes"
        return {"under": f"{u1['bytes']} B single-shot", "over": f"{u2['bytes']} B chunked"}

    report.run("2a", "uploads just below and just above the chunk threshold", case2a)

    # 2b ─ an oversize DECLARATION is refused before any byte moves.
    def case2b() -> dict:
        conv = conv_id("toobig")
        r = a.http.post(
            f"{a.base}{a.p['chunked']}/init",
            data={"conversation_id": conv, "filename": "huge.mp4", "purpose": "video",
                  "size": str(9 * 1024 ** 4), "parts": "128", "part_size": str(PART)},
        )
        assert r.status_code == 413, f"expected 413 before any byte, got {r.status_code}: {r.text[:160]}"
        return {"status": r.status_code}

    report.run("2b", "an oversize upload is refused before any byte moves", case2b)

    # 4b/4c ─ a part cut mid-body is not accepted; resume sends only the rest.
    def case4() -> dict:
        conv = conv_id("resume")
        size = os.path.getsize(over)
        parts = (size + PART - 1) // PART
        init = a.init_chunked(conv, os.path.basename(over), "video", size, parts, PART)
        uid = init["upload_id"]
        with open(over, "rb") as fh:
            blobs = [fh.read(PART) for _ in range(parts)]
        assert a.put_part(conv, uid, 0, blobs[0]).status_code == 200
        # The tab dies in the middle of the last part.
        a.put_part(conv, uid, parts - 1, blobs[-1], truncate_to=len(blobs[-1]) // 3)
        s = a.session(conv, uid)
        assert s.status_code == 200, s.text[:160]
        accepted = s.json()["accepted_parts"]
        assert accepted == [0], f"a cut part was accepted: {accepted}"
        # Completing now must name the hole rather than assembling a short file.
        r = a.complete(conv, uid)
        assert r.status_code == 409, f"complete accepted a missing part: {r.status_code}"
        assert r.json().get("missing_parts") == list(range(1, parts)), r.text[:200]
        # Resume: only what is missing.
        for i in range(1, parts):
            assert a.put_part(conv, uid, i, blobs[i]).status_code == 200
        done = a.complete(conv, uid)
        assert done.status_code == 200, done.text[:200]
        assert done.json()["bytes"] == size, "resumed upload assembled the wrong length"
        # 4d/5a ─ the acknowledgement was lost; the retry replays.
        again = a.complete(conv, uid)
        assert again.status_code == 200 and again.json()["upload_id"] == done.json()["upload_id"]
        assert again.json()["bytes"] == done.json()["bytes"], "a retried complete produced a different result"
        return {"parts": parts, "resent": parts - 1, "replayed": "identical"}

    report.run("4b/4c/4d/5a", "a cut part is not accepted; resume sends only the rest; complete replays", case4)

    # 12f ─ a filename that tries to escape its directory.
    def case12f() -> dict:
        conv = conv_id("path")
        init = a.init_chunked(conv, "../../../../etc/passwd.mp4", "video", 16, 1, PART)
        r = a.put_part(conv, init["upload_id"], 0, b"\x00" * 16)
        assert r.status_code == 200, r.text[:160]
        s = a.session(conv, init["upload_id"]).json()
        assert "/" not in s["filename"] and ".." not in s["filename"], f"path survived: {s['filename']!r}"
        a.cancel(conv, init["upload_id"])
        return {"stored as": s["filename"]}

    report.run("12f", "a traversing filename is reduced to a basename", case12f)

    # 13a ─ another account cannot see, feed, finish or cancel this session.
    def case13a() -> dict:
        if b is None:
            raise Skip("no second account configured (QA_EMAIL_2)")
        conv = conv_id("mine")
        init = a.init_chunked(conv, "private.mp4", "video", 16, 1, PART)
        uid = init["upload_id"]
        codes = {
            "GET": b.session(conv, uid).status_code,
            "PUT": b.put_part(conv, uid, 0, b"\x00" * 16).status_code,
            "COMPLETE": b.complete(conv, uid).status_code,
            "DELETE": b.cancel(conv, uid).status_code,
        }
        a.cancel(conv, uid)
        assert set(codes.values()) == {404}, f"cross-user access leaked: {codes}"
        return codes

    report.run("13a", "another account gets 404 on every operation of this session", case13a)

    # 5b/10a ─ the same intent twice is one generation and one answer.
    def case5b() -> dict:
        conv = conv_id("intent")
        intent = uuid.uuid4().hex
        first = a.chat(conv, "In one short sentence, what is the capital of France?", intent_id=intent)
        assert first["status"] == 200 and first["kinds"][-1] == "done"
        gen = first["leading"].get("generation_id")
        assert gen, f"no leading meta: {first['kinds'][:3]}"
        second = a.chat(conv, "In one short sentence, what is the capital of France?", intent_id=intent)
        assert second["status"] == 200, second.get("body", "")[:200]
        assert second["leading"].get("generation_id") == gen, "a retry started a second generation"
        rows = assistant_rows(a.thread(conv))
        assert len(rows) == 1, f"{len(rows)} answers persisted for one intent"
        return {"generation": gen[:8], "answers": len(rows)}

    report.run("5b/10a", "the same send intent twice is one generation and one stored answer", case5b)

    # 14b ─ a client that knows nothing of intents still works.
    def case14b() -> dict:
        conv = conv_id("legacy")
        turn = a.chat(conv, "Reply with the single word: ok.")
        assert turn["status"] == 200 and turn["kinds"][-1] == "done", turn["kinds"][-3:]
        assert assistant_rows(a.thread(conv)), "no answer persisted for a legacy client"
        return {"events": len(turn["events"])}

    report.run("14b", "an old client that sends no intent_id is unaffected", case14b)

    # 7/8 ─ every viewer leaves; the server still finishes and stores the answer.
    def case7() -> dict:
        conv = conv_id("detach")
        up = a.upload_file(conv, small)
        intent = uuid.uuid4().hex
        # Cut the connection as soon as the first progress step arrives.
        turn = a.chat(conv, "", intent_id=intent,
                      video_uploads=[{"upload_id": up["upload_id"], "name": up["filename"]}],
                      stop_after="step")
        assert turn["cut"], "the stream ended on its own before the cut point"
        deadline = time.time() + 900
        state: dict = {}
        while time.time() < deadline:
            r = a.request_state(intent)
            assert r.status_code == 200, r.text[:160]
            state = r.json()
            if state["status"] in ("completed", "failed"):
                break
            time.sleep(5)
        assert state.get("status") == "completed", f"request ended as {state.get('status')}"
        assert state.get("answer_persisted") is True, "the answer was not stored server-side"
        rows = assistant_rows(a.thread(conv))
        assert len(rows) == 1 and len(rows[0]["content"]) > 200, f"{len(rows)} rows, {len(rows[0]['content']) if rows else 0} chars"
        return {"cut at": "first step event", "stored": f"{len(rows[0]['content'])} chars"}

    report.run("7/8", "every viewer leaves mid-analysis; the answer is still produced and stored", case7)

    # 6 ─ leaving and coming back does not restart the analysis.
    def case6() -> dict:
        conv = conv_id("reattach")
        up = a.upload_file(conv, small)
        intent = uuid.uuid4().hex
        first = a.chat(conv, "", intent_id=intent,
                       video_uploads=[{"upload_id": up["upload_id"], "name": up["filename"]}],
                       stop_after="step", stop_after_n=2)
        assert first["cut"], "the stream finished before the reload point"
        before = a.video_status(conv, up["upload_id"])
        again = a.attach(conv)
        assert again["status"] == 200, f"attach answered {again['status']}"
        assert again["kinds"][-1] == "done", f"re-attached stream ended on {again['kinds'][-1:]}"
        assert len(again["answer"]) > 200, "the re-attached stream carried no answer"
        after = a.video_status(conv, up["upload_id"])
        facts = {"answer chars": len(again["answer"])}
        if before and after and "attempt" in (before or {}) and "attempt" in (after or {}):
            assert after["attempt"] == before["attempt"], "the analysis restarted on re-attach"
            facts["attempt"] = after["attempt"]
        rows = assistant_rows(a.thread(conv))
        assert len(rows) == 1, f"{len(rows)} assistant rows after a re-attach"
        return facts

    report.run("6", "a reload mid-analysis re-attaches without restarting the work", case6)

    # 12e ─ a file that is not a video fails cleanly and says so.
    def case12e() -> dict:
        conv = conv_id("corrupt")
        r = a.upload_single(conv, "corrupt.mp4", os.urandom(300_000), "video")
        assert r.status_code == 200, f"upload refused with {r.status_code}"
        up = r.json()
        intent = uuid.uuid4().hex
        turn = a.chat(conv, "", intent_id=intent, video_uploads=[{"upload_id": up["upload_id"], "name": up["filename"]}])
        assert turn["status"] == 200, turn.get("body", "")[:200]
        text = turn["answer"].lower()
        assert "couldn't analyse" in text or "could not" in text or "invalid" in text, f"unhelpful reply: {turn['answer'][:160]}"
        return {"reply": turn["answer"][:110]}

    report.run("12e", "a corrupt file fails with a sentence a person can act on", case12e)

    if not heavy:
        return

    # 3d ─ a small and a large file in one turn, the 20/400 pairing in miniature.
    def case3d() -> dict:
        conv = conv_id("pair")
        u1 = a.upload_file(conv, small)
        u2 = a.upload_file(conv, big)
        intent = uuid.uuid4().hex
        turn = a.chat(conv, "", intent_id=intent, video_uploads=[
            {"upload_id": u1["upload_id"], "name": u1["filename"]},
            {"upload_id": u2["upload_id"], "name": u2["filename"]},
        ])
        assert turn["status"] == 200 and turn["kinds"][-1] == "done"
        assert len(turn["answer"]) > 200
        rows = assistant_rows(a.thread(conv))
        assert len(rows) == 1
        return {"files": 2, "bytes": u1["bytes"] + u2["bytes"], "answer chars": len(turn["answer"])}

    report.run("3d", "a small and a large video in one turn", case3d)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("QA_BASE", "http://127.0.0.1:8081"))
    ap.add_argument("--via-frontend", default=None, metavar="URL")
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--report", default=None)
    ap.add_argument("--heavy", action="store_true", help="include the 200 MB pairing")
    args = ap.parse_args()

    paths = PATHS["frontend"] if args.via_frontend else PATHS["orchestrator"]
    base = args.via_frontend or args.base
    email, pw = os.environ.get("QA_EMAIL"), os.environ.get("QA_PASSWORD")
    if not email or not pw:
        print("set QA_EMAIL and QA_PASSWORD", file=sys.stderr)
        return 2
    print(f"QA matrix against {base} ({'through the Next proxies' if args.via_frontend else 'orchestrator directly'})", flush=True)
    a = Client(base, paths, email, pw)
    b = None
    if os.environ.get("QA_EMAIL_2") and os.environ.get("QA_PASSWORD_2"):
        b = Client(base, paths, os.environ["QA_EMAIL_2"], os.environ["QA_PASSWORD_2"])
    report = Report()
    try:
        scenarios(a, b, args.fixtures, report, heavy=args.heavy)
    finally:
        a.close()
        if b:
            b.close()
    passed, failed, skipped = report.summary()
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped", flush=True)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"base": base, "cases": [c.__dict__ for c in report.cases],
                       "passed": passed, "failed": failed, "skipped": skipped}, fh, indent=1)
        print(f"report: {args.report}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
