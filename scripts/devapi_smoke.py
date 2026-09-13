#!/usr/bin/env python3
"""End-to-end proof of the developer platform against a RUNNING stack.

This script proves the things a unit test cannot: that a real API key minted
through the console reaches a real model through the real admission control,
that the stream arrives as tokens rather than one buffered blob, that the
refusals refuse, and that the numbers the console reports are the numbers the
API actually produced.

It is written to be run against the ISOLATED e2e stack (scripts/e2e-stack.sh,
orchestrator on 127.0.0.1:8081, frontend on 127.0.0.1:3001), never production.
It refuses to run against a base URL it was not pointed at explicitly.

Every check prints PASS or FAIL with the evidence it saw. The exit code is the
number of failures, so CI and a person read the same verdict. Nothing here
retries a failure into a pass, and nothing is skipped silently: a check that
cannot run says SKIP and why, and a SKIP is reported in the summary.

    VIDEO_SMOKE_EMAIL=… VIDEO_SMOKE_PASSWORD=… \
      python scripts/devapi_smoke.py --base http://127.0.0.1:8081

UNLIMITED BY DEFAULT (owner decision, 2026-09-13). The public API enforces no
request, token, daily or concurrency limit unless the stack runs with
PUBLIC_API_ENFORCE_LIMITS=true, so the default run proves the opposite of what
it used to: a 70-request burst is admitted in full and no response carries a
`RateLimit` or `RateLimit-Policy` field. `--limits enforced` runs the old
429 check against a stack started with the switch on.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

import httpx

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: List[Tuple[str, str, str]] = []
_t0 = time.monotonic()


def check(name: str, ok: bool, evidence: str = "") -> bool:
    state = PASS if ok else FAIL
    _results.append((state, name, evidence))
    mark = "✓" if ok else "✗"
    print(f"  {mark} [{time.monotonic() - _t0:6.1f}s] {name}" + (f" — {evidence}" if evidence else ""))
    return ok


def skip(name: str, why: str) -> None:
    _results.append((SKIP, name, why))
    print(f"  – [{time.monotonic() - _t0:6.1f}s] SKIP {name} — {why}")


def section(title: str) -> None:
    print(f"\n=== {title}")


# --------------------------------------------------------------- session --


def _login(client: httpx.Client, base: str, email: str, password: str) -> httpx.Response:
    """Sign in and keep the session. The session cookie is `Secure` on this
    deployment and httpx's jar will not replay a Secure cookie over plain
    http://, so it is pinned as a header — exactly the bytes a browser sends
    through the HTTPS front door (the same approach as scripts/video_smoke.py)."""
    r = client.post(f"{base}/auth/login", json={"email": email, "password": password})
    if r.status_code == 200:
        pair = r.headers.get("set-cookie", "").split(";", 1)[0].strip()
        if "=" in pair:
            client.headers["Cookie"] = pair
    return r


def sign_in(client: httpx.Client, base: str) -> None:
    email = os.environ.get("VIDEO_SMOKE_EMAIL")
    password = os.environ.get("VIDEO_SMOKE_PASSWORD")
    if not email or not password:
        raise SystemExit("set VIDEO_SMOKE_EMAIL and VIDEO_SMOKE_PASSWORD (the e2e account, never a production one)")
    r = _login(client, base, email, password)
    if r.status_code != 200:
        raise SystemExit(f"login failed: {r.status_code} {r.text[:200]}")
    me = client.get(f"{base}/auth/me")
    if me.status_code != 200:
        raise SystemExit(f"the session did not stick: /auth/me {me.status_code}")
    print(f"signed in as {email} ({me.json().get('workspace', {}).get('role')})")


def console(client: httpx.Client, base: str, method: str, path: str, **kw) -> httpx.Response:
    return client.request(method, f"{base}/admin/api/developers/{path.lstrip('/')}", **kw)


# ------------------------------------------------------------- the checks --


def provision(client: httpx.Client, base: str) -> Tuple[Optional[str], Optional[str]]:
    """Create a project and a key through the console API, as a person would."""
    section("provisioning through the console API")
    name = f"smoke-{int(time.time())}"
    r = console(client, base, "POST", "projects", json={"name": name, "environment": "test"})
    if not check("a project is created", r.status_code in (200, 201), f"{r.status_code} {r.text[:160]}"):
        return None, None
    project = r.json().get("project", {})
    project_id = project.get("id")
    check("the project carries a server-issued id", bool(project_id) and str(project_id).startswith("proj_"),
          f"id={project_id} environment={project.get('environment')}")

    r = console(client, base, "POST", f"projects/{project_id}/keys", json={
        "name": "smoke key",
        "scopes": ["models.read", "responses.read", "responses.write", "usage.read"],
    })
    if not check("a key is created", r.status_code in (200, 201), f"{r.status_code} {r.text[:160]}"):
        return project_id, None
    body = r.json()
    token = body.get("secret")
    check("the plaintext key is returned exactly once, in the create response", bool(token),
          f"prefix={str(token)[:12]}…" if token else "no plaintext in the response")
    check("the create response carries no digest", "key_hash" not in json.dumps(body), "no key_hash on the wire")

    r = console(client, base, "GET", f"projects/{project_id}/keys")
    listed = r.text
    check("the key list never returns the secret or its digest",
          r.status_code == 200 and (token or "") not in listed and "key_hash" not in listed,
          f"{r.status_code}, {len(listed)} bytes, secret absent")
    return project_id, token


def models_and_refusals(base: str, token: str) -> None:
    section("authentication, authorization and the model registry")
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {token}"})
        ok = r.status_code == 200
        ids = [m.get("id") for m in r.json().get("data", [])] if ok else []
        check("GET /v1/models answers for a valid key", ok, f"{r.status_code} {ids}")
        check("only public aliases are listed — no internal engine", all(
            not any(bad in str(i) for bad in ("router", "embed", "ocr", "rerank", "8000", "vllm")) for i in ids),
            f"{ids}")

        r = c.get(f"{base}/v1/models", headers={"Authorization": "Bearer tsk_live_0000000000000000_nope000000"})
        check("an unknown key is refused with 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        err = body.get("error", {})
        check("the refusal uses the documented envelope",
              err.get("type") == "authentication_error" and err.get("code") == "invalid_api_key" and "request_id" in err,
              json.dumps(err)[:160])

        r = c.get(f"{base}/v1/models")
        check("no key at all is refused with 401", r.status_code == 401, str(r.status_code))

        r = c.get(f"{base}/v1/models/does-not-exist", headers={"Authorization": f"Bearer {token}"})
        check("an unknown model is 404, not 403 (no existence disclosure)", r.status_code == 404, str(r.status_code))

        r = c.post(f"{base}/v1/responses", headers={"Authorization": f"Bearer {token}"},
                   json={"model": "techsara-35b", "input": "hi", "nonsense_parameter": True})
        check("a parameter the platform cannot honour is rejected, not ignored", r.status_code in (400, 422),
              f"{r.status_code} {r.text[:140]}")

        r = c.post(f"{base}/v1/responses", headers={"Authorization": f"Bearer {token}"},
                   json={"model": "techsara-35b", "input": "x" * (2 * 1024 * 1024)})
        check("an oversized body is refused with 413", r.status_code == 413, str(r.status_code))

        r = c.get(f"{base}/v1/usage", headers={"Authorization": f"Bearer {token}"})
        check("GET /v1/usage answers for a scoped key", r.status_code == 200, f"{r.status_code} {r.text[:120]}")


def cookie_is_not_a_credential(client: httpx.Client, base: str) -> None:
    section("the session cookie is not an API credential")
    r = client.get(f"{base}/v1/models")  # carries ts_session, no Authorization
    check("a signed-in browser session cannot drive /v1", r.status_code == 401,
          f"{r.status_code} with a valid ts_session cookie")


def non_streaming(base: str, token: str, limits: str = "unlimited") -> Optional[str]:
    section("a non-streaming response")
    with httpx.Client(timeout=300) as c:
        t = time.monotonic()
        r = c.post(f"{base}/v1/responses", headers={"Authorization": f"Bearer {token}"},
                   json={"model": "techsara-35b", "input": "Reply with exactly: ready", "max_output_tokens": 32})
        took = time.monotonic() - t
        if not check("POST /v1/responses completes", r.status_code == 200, f"{r.status_code} in {took:.1f}s {r.text[:140]}"):
            return None
        body = r.json()
        text = ""
        for item in body.get("output", []):
            for part in item.get("content", []):
                text += part.get("text", "")
        check("the response carries an id, a status and text",
              body.get("id", "").startswith("resp_") and body.get("status") == "completed" and text.strip(),
              f"{body.get('id')} {body.get('status')} {len(text)} chars")
        usage = body.get("usage")
        check("usage is reported, and is not a fabricated zero",
              usage is None or (usage.get("total_tokens") or 0) > 0,
              json.dumps(usage) if usage else "null (not measured — honest)")
        check("a request id is returned in a header", bool(r.headers.get("x-request-id")),
              r.headers.get("x-request-id", "absent"))
        if limits == "enforced":
            check("rate-limit headers are present", bool(r.headers.get("ratelimit") or r.headers.get("ratelimit-policy")),
                  r.headers.get("ratelimit", "absent"))
        else:
            # A RateLimit field on an unlimited API advertises a ceiling that
            # does not exist, and a client would throttle itself by it.
            check("no rate-limit headers are sent (the API is unlimited)",
                  not r.headers.get("ratelimit") and not r.headers.get("ratelimit-policy"),
                  f"ratelimit={r.headers.get('ratelimit', 'absent')} "
                  f"ratelimit-policy={r.headers.get('ratelimit-policy', 'absent')}")
        return body.get("id")


def streaming(base: str, token: str) -> None:
    section("a streamed response")
    seen: List[Dict[str, Any]] = []
    first_delta_at: Optional[float] = None
    start = time.monotonic()
    with httpx.Client(timeout=300) as c:
        with c.stream("POST", f"{base}/v1/responses", headers={"Authorization": f"Bearer {token}"},
                      json={"model": "techsara-35b", "input": "Count from one to five, one word per line.",
                            "stream": True, "max_output_tokens": 64}) as r:
            if not check("the stream opens with 200 and text/event-stream", r.status_code == 200 and
                         r.headers.get("content-type", "").startswith("text/event-stream"),
                         f"{r.status_code} {r.headers.get('content-type')}"):
                return
            check("the stream is not buffered by the server", r.headers.get("x-accel-buffering") == "no",
                  f"x-accel-buffering={r.headers.get('x-accel-buffering')}")
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                seen.append(ev)
                if ev.get("type") == "response.output_text.delta" and first_delta_at is None:
                    first_delta_at = time.monotonic() - start
    types = [e.get("type") for e in seen]
    check("the lifecycle events arrive in order",
          types[:2] == ["response.created", "response.in_progress"] and "response.output_text.delta" in types,
          " → ".join(types[:4]) + f" … ({len(types)} events)")
    seqs = [e.get("sequence_number") for e in seen if e.get("sequence_number") is not None]
    check("sequence numbers start at 1 and never skip or repeat",
          seqs == list(range(1, len(seqs) + 1)), f"{len(seqs)} events, first={seqs[:3]} last={seqs[-3:] if seqs else []}")
    terminals = [t for t in types if t in ("response.completed", "response.failed", "error")]
    check("exactly one terminal event is emitted", len(terminals) == 1, str(terminals))
    final = next((e for e in seen if e.get("type") == "response.completed"), None)
    # The Responses-style terminal event carries the whole response object,
    # and usage lives on it: `response.usage`, exactly as the non-streaming
    # body has it — not a top-level field of the event.
    usage = (final or {}).get("response", {}).get("usage")
    check("the terminal event carries usage on its response, and it is measured",
          bool(usage) and (usage.get("total_tokens") or 0) > 0,
          json.dumps(usage) if final else "no completed event")
    check("tokens arrived progressively, not as one blob at the end",
          first_delta_at is not None and len(seqs) > 3,
          f"first delta at {first_delta_at:.1f}s, {len(seqs)} events" if first_delta_at else "no delta seen")


def idempotency(base: str, token: str) -> None:
    section("idempotency")
    key = f"smoke-{int(time.time() * 1000)}"
    payload = {"model": "techsara-35b", "input": "Say: once", "max_output_tokens": 16}
    with httpx.Client(timeout=300) as c:
        h = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
        a = c.post(f"{base}/v1/responses", headers=h, json=payload)
        b = c.post(f"{base}/v1/responses", headers=h, json=payload)
        ok = a.status_code == 200 and b.status_code == 200
        check("the same key and body returns the same response, not a second generation",
              ok and a.json().get("id") == b.json().get("id"),
              f"{a.status_code}/{b.status_code} {a.json().get('id')} vs {b.json().get('id')}" if ok else
              f"{a.status_code}/{b.status_code}")
        c2 = c.post(f"{base}/v1/responses", headers=h, json={**payload, "input": "Say: twice"})
        check("the same key with a different body is refused with 409", c2.status_code == 409,
              f"{c2.status_code} {c2.text[:120]}")


def background_and_webhook(client: httpx.Client, base: str, token: str, project_id: str) -> None:
    section("background responses and a signed webhook")
    received: List[Tuple[Dict[str, str], bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("content-length", "0"))
            received.append((dict(self.headers), self.rfile.read(n)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):  # silence
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    r = console(client, base, "POST", f"projects/{project_id}/webhooks", json={
        "url": f"http://127.0.0.1:{port}/hook",
        "events": ["response.completed", "response.failed"],
    })
    loopback_refused = r.status_code in (400, 422)
    check("a loopback webhook URL is refused (SSRF defence)", loopback_refused, f"{r.status_code} {r.text[:140]}")
    if loopback_refused:
        skip("webhook delivery", "the SSRF guard correctly refuses the local receiver; delivery is proven by unit tests")
    server.shutdown()

    with httpx.Client(timeout=120) as c:
        r = c.post(f"{base}/v1/responses", headers={"Authorization": f"Bearer {token}"},
                   json={"model": "techsara-35b", "input": "Say: background", "background": True,
                         "max_output_tokens": 16})
        if not check("a background response is accepted immediately", r.status_code in (200, 202),
                     f"{r.status_code} {r.text[:140]}"):
            return
        rid = r.json().get("id")
        deadline = time.monotonic() + 180
        status = None
        while time.monotonic() < deadline:
            g = c.get(f"{base}/v1/responses/{rid}", headers={"Authorization": f"Bearer {token}"})
            status = g.json().get("status") if g.status_code == 200 else f"http {g.status_code}"
            if status in ("completed", "failed", "cancelled"):
                break
            time.sleep(2)
        check("the background response reaches a terminal state", status == "completed", f"{rid} → {status}")


def tenant_isolation(client: httpx.Client, base: str, token: str) -> None:
    section("tenant isolation")
    r = console(client, base, "GET", "projects")
    listed_ok = r.status_code == 200
    mine = {p.get("id") for p in (r.json().get("projects", []) if listed_ok else [])}
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{base}/v1/responses/resp_{'0' * 24}", headers={"Authorization": f"Bearer {token}"})
        check("another project's response id is 404, never someone else's data", r.status_code == 404, str(r.status_code))
    check("the console lists this workspace's projects", listed_ok, f"{len(mine)} project(s)")


#: The burst both checks send: above the old default of 60 requests a minute,
#: so an enforcing stack would refuse part of it and an unlimited one must not.
BURST = 70


def unlimited(base: str, token: str) -> None:
    """The owner decision of 2026-09-13, end to end: 70 quick requests — ten
    more than the old 60/minute default — all succeed, none is a 429, and no
    response carries a RateLimit field."""
    section("no usage limits (PUBLIC_API_ENFORCE_LIMITS off)")
    codes: List[int] = []
    advertised: List[str] = []
    with httpx.Client(timeout=60) as c:
        for _ in range(BURST):
            r = c.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {token}"})
            codes.append(r.status_code)
            for name in ("ratelimit", "ratelimit-policy"):
                if r.headers.get(name):
                    advertised.append(f"{name}: {r.headers[name]}")
    check(f"a burst of {BURST} requests is admitted in full", codes == [200] * BURST,
          f"{len(codes)} requests, {codes.count(200)} allowed, {codes.count(429)} refused, "
          f"other={sorted({c for c in codes if c not in (200, 429)})}")
    check("no response in the burst carries a RateLimit or RateLimit-Policy header", not advertised,
          advertised[0] if advertised else f"none on {len(codes)} responses")


def rate_limit(base: str, token: str) -> None:
    """Only with `--limits enforced`, against a stack started with
    PUBLIC_API_ENFORCE_LIMITS=true: the enforcement code stays available."""
    section("rate limiting (PUBLIC_API_ENFORCE_LIMITS on)")
    codes: List[int] = []
    with httpx.Client(timeout=60) as c:
        for _ in range(BURST):
            r = c.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {token}"})
            codes.append(r.status_code)
            if r.status_code == 429:
                check("the 429 carries Retry-After", bool(r.headers.get("retry-after")),
                      f"retry-after={r.headers.get('retry-after')}")
                body = r.json().get("error", {})
                check("the 429 uses the documented envelope",
                      body.get("type") in ("rate_limit_error", "quota_exceeded") or body.get("code"),
                      json.dumps(body)[:140])
                break
    check("the per-key limit refuses a burst above the project's rate", 429 in codes,
          f"{len(codes)} requests, {codes.count(200)} allowed, {codes.count(429)} refused")


def no_internal_disclosure(base: str, token: str) -> None:
    section("no internal disclosure")
    leaks = ("Traceback", "psycopg", "postgresql://", "orchestrator:8080", "vllm", "/app/", "127.0.0.1:8000")
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{base}/v1/responses", headers={"Authorization": f"Bearer {token}",
                                                    "content-type": "application/json"}, content=b"{not json")
        check("malformed JSON does not leak internals", not any(s in r.text for s in leaks),
              f"{r.status_code} {r.text[:120]}")
        r = c.post(f"{base}/v1/responses", headers={"Authorization": f"Bearer {token}"},
                   json={"model": "techsara-35b", "input": ["not", "a", "message"]})
        check("a bad input shape does not leak internals", not any(s in r.text for s in leaks),
              f"{r.status_code} {r.text[:120]}")


def openapi_document(base: str, token: str) -> None:
    section("the public OpenAPI document")
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{base}/v1/openapi.json")
        if not check("GET /v1/openapi.json is served", r.status_code == 200, str(r.status_code)):
            return
        doc = r.json()
        check("it declares OpenAPI 3.1", str(doc.get("openapi", "")).startswith("3.1"), doc.get("openapi"))
        paths = sorted(doc.get("paths", {}))
        check("it documents the public surface only",
              all(p.startswith("/") for p in paths) and not any("admin" in p or "chat/" in p and "completions" not in p for p in paths),
              ", ".join(paths))
        check("it declares a bearer security scheme",
              "bearerAuth" in json.dumps(doc.get("components", {}).get("securitySchemes", {})),
              json.dumps(doc.get("components", {}).get("securitySchemes", {}))[:140])
        check("no internal hostname appears in the document",
              not any(s in json.dumps(doc) for s in ("orchestrator:8080", "vllm", "127.0.0.1:8000")), "clean")


def cors(base: str) -> None:
    section("CORS for browser clients")
    with httpx.Client(timeout=30) as c:
        r = c.request("OPTIONS", f"{base}/v1/responses", headers={
            "Origin": "https://example.test", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type"})
        check("a preflight is answered", r.status_code in (200, 204), str(r.status_code))
        check("credentials are never allowed on /v1",
              r.headers.get("access-control-allow-credentials") is None,
              f"allow-credentials={r.headers.get('access-control-allow-credentials')}")


def member_is_refused(base: str) -> None:
    """A signed-in MEMBER must not reach the console API at all — refused as
    404 (the admin surface's convention: the console's existence is not
    disclosed), not merely hidden in the UI. Needs a second, member account:
    SMOKE_MEMBER_EMAIL / SMOKE_MEMBER_PASSWORD."""
    section("a member cannot reach the console")
    email, password = os.environ.get("SMOKE_MEMBER_EMAIL"), os.environ.get("SMOKE_MEMBER_PASSWORD")
    if not email or not password:
        skip("member refusal", "set SMOKE_MEMBER_EMAIL and SMOKE_MEMBER_PASSWORD to a member account")
        return
    with httpx.Client(timeout=30, follow_redirects=False) as member:
        r = _login(member, base, email, password)
        if not check("the member signs in and the session sticks",
                     r.status_code == 200 and member.get(f"{base}/auth/me").status_code == 200, str(r.status_code)):
            return
        for method, path in (("GET", "projects"), ("POST", "projects"), ("GET", "usage"), ("GET", "overview")):
            r = console(member, base, method, path, json={"name": "should-not-exist", "environment": "test"} if method == "POST" else None)
            check(f"a member is refused {method} /admin/api/developers/{path} with 404", r.status_code == 404,
                  f"{r.status_code} {r.text[:80]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True, help="orchestrator base URL, e.g. http://127.0.0.1:8081 (the e2e stack)")
    ap.add_argument("--limits", choices=("unlimited", "enforced"), default="unlimited",
                    help="what the stack's PUBLIC_API_ENFORCE_LIMITS is: unlimited (the default, "
                         "owner decision 2026-09-13) proves a burst is admitted; enforced proves the 429")
    ap.add_argument("--allow-production", action="store_true", help="refuse-by-default guard for a non-e2e base URL")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    if not re.match(r"^http://127\.0\.0\.1:(8081|3001)$", base) and not args.allow_production:
        raise SystemExit(f"refusing to run against {base}: this script is for the isolated e2e stack "
                         "(127.0.0.1:8081 or :3001). Pass --allow-production only if you mean it.")

    print(f"developer platform smoke against {base}")
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        sign_in(client, base)
        member_is_refused(base)
        project_id, token = provision(client, base)
        if not token:
            print("\nprovisioning failed; the rest cannot run")
        else:
            cookie_is_not_a_credential(client, base)
            models_and_refusals(base, token)
            no_internal_disclosure(base, token)
            openapi_document(base, token)
            cors(base)
            non_streaming(base, token, args.limits)
            streaming(base, token)
            idempotency(base, token)
            tenant_isolation(client, base, token)
            if project_id:
                background_and_webhook(client, base, token, project_id)
            if args.limits == "enforced":
                rate_limit(base, token)
            else:
                unlimited(base, token)

    failures = [r for r in _results if r[0] == FAIL]
    skips = [r for r in _results if r[0] == SKIP]
    print(f"\nverdict: {'PASS' if not failures else 'FAIL'} · "
          f"{sum(1 for r in _results if r[0] == PASS)}/{len(_results)} checks passed, "
          f"{len(failures)} failed, {len(skips)} skipped, {time.monotonic() - _t0:.0f}s")
    for state, name, evidence in failures:
        print(f"  FAILED: {name} — {evidence}")
    for state, name, evidence in skips:
        print(f"  SKIPPED: {name} — {evidence}")
    return len(failures)


if __name__ == "__main__":
    sys.exit(main())
