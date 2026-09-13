#!/usr/bin/env python3
"""Execute every code sample on /docs against a RUNNING stack, and say what happened.

WHY THIS EXISTS (2026-09-13). CONTRACT §17 says every example on the
documentation site is executed against the running API before it ships, and
that an example which cannot be executed is marked as not executed rather than
presented as verified. Until this script, the `EXAMPLES_EXECUTED` switch in
frontend/content/docs/samples.ts had nothing behind it but good intentions:
the samples were checked against the router's CODE on every commit, never run
against a SERVER. A sample can type-check against the models and still send a
header the edge strips, name an event the stream never emits, or rely on a
variable defined three blocks earlier on a different page.

WHAT IT DOES.

  1. Loads the documentation exactly as the site renders it — the page records
     are bundled with the frontend's own esbuild and evaluated in node, so the
     `${API_BASE_URL}` interpolations and the `\\`` escapes are resolved by
     the TypeScript compiler, not re-implemented here — and extracts every
     fenced block (the pages use `~~~lang` fences; see types.ts for why).
  2. Signs in to the ISOLATED e2e stack as the e2e admin, creates a project and
     a real test key through the console API, and never prints the key.
  3. Runs each executable sample against the public edge
     (http://127.0.0.1:3001/v1): bash with bash, python with the orchestrator
     venv, TypeScript stripped by esbuild and run with node.
  4. Judges each run by its real output, writes the per-sample evidence table
     to docs/developer-platform/evidence/docs-examples.md, and exits non-zero
     if any executable sample failed or any documented shape disagrees with
     the real API.

WHAT IT CHANGES IN A SAMPLE, AND NOTHING ELSE. Every change is recorded next to
the sample in the evidence file:

  * the documented base URL becomes the isolated edge;
  * a placeholder key in a bash sample becomes "$TECHSARA_API_KEY", which the
    harness sets to the real key (python and node samples already read the
    variable);
  * the documented example response id becomes a real id this run created;
  * a fragment that calls names defined by an EARLIER block (python.ts's
    `client()`, javascript.ts's `BASE_URL`) has those earlier blocks prepended,
    and a sample that only DEFINES a function gets a one-line driver that
    calls it — otherwise "it ran" would mean "it parsed";
  * `curl` is wrapped to append its HTTP status on a marker line, and python's
    httpx.Client.send / node's fetch are wrapped to print each status to
    stderr, so the verdict is read from the wire rather than inferred.

Run it against the isolated stack only, with the orchestrator venv:

    orchestrator/.venv/bin/python scripts/docs_examples_run.py
    orchestrator/.venv/bin/python scripts/docs_examples_run.py --self-test
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend"
PAGES_DIR = FRONTEND / "content" / "docs" / "pages"
ESBUILD = FRONTEND / "node_modules" / ".bin" / "esbuild"
EVIDENCE = REPO / "docs" / "developer-platform" / "evidence" / "docs-examples.md"

ORCH = "http://127.0.0.1:8081"
EDGE = "http://127.0.0.1:3001/v1"
DOC_BASE = "https://ai.techsarasolutions.com/v1"
MODEL = "techsara-35b"
EMAIL = "e2e-devadmin@test.local"
PW_FILE = Path(
    "/tmp/claude-1000/-home-techsphere-Documents-project-personal-LLM-Chabot/"
    "c633fe6d-2f75-46c5-90e6-6fe41b6142e1/scratchpad/.e2e_devadmin_pw"
)
PY = "/home/techsphere/Documents/project/personal-LLM-Chabot/orchestrator/.venv/bin/python"
EXAMPLE_RESPONSE_ID = "resp_4f2b8c1d9e0a7b6c5d4e3f20"

PASSED, FAILED, NOT_RUN, NOT_EXECUTABLE = "PASSED", "FAILED", "NOT RUN", "NOT EXECUTABLE"
MARK = re.compile(r"@@HTTP (\d{3})@@")


# ================================================================ extraction ==


@dataclasses.dataclass
class Fence:
    lang: str
    code: str
    line: int  # 1-based line of the opening fence inside the markdown body


def extract_fences(markdown: str) -> List[Fence]:
    """Every fenced code block, CommonMark-style: a run of 3+ `~` or 3+ backticks
    indented at most three spaces opens a block; the info string's first word is
    the language; the block closes on a fence of the SAME character at least as
    long, and an unclosed block runs to the end of the document."""
    out: List[Fence] = []
    lines = markdown.split("\n")
    i = 0
    opener = re.compile(r"^ {0,3}(~{3,}|`{3,})[ \t]*([^\s`]*)?.*$")
    while i < len(lines):
        m = opener.match(lines[i])
        if not m:
            i += 1
            continue
        fence, lang = m.group(1), (m.group(2) or "").lower()
        if fence[0] == "`" and "`" in lines[i].strip()[len(fence):]:
            i += 1  # a backtick info string may not contain a backtick
            continue
        start, body = i, []
        i += 1
        closer = re.compile(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$")
        while i < len(lines) and not closer.match(lines[i]):
            body.append(lines[i])
            i += 1
        out.append(Fence(lang=lang, code="\n".join(body), line=start + 1))
        i += 1
    return out


def classify(lang: str) -> str:
    """bash | python | javascript | json | other — by the info string, which is
    what the reader sees highlighted and what they will paste into."""
    lang = lang.lower()
    if lang in ("bash", "sh", "shell", "zsh", "console", "curl"):
        return "bash"
    if lang in ("python", "py", "python3"):
        return "python"
    if lang in ("javascript", "js", "typescript", "ts", "node", "mjs", "tsx", "jsx"):
        return "javascript"
    if lang in ("json", "jsonc"):
        return "json"
    return "other"


def count_raw_openers(pages_dir: Path) -> int:
    """The second, independent route to the same number: opening fences counted
    in the TypeScript SOURCE. If the bundle and the source disagree, the loader
    is wrong, and every verdict below it is about the wrong samples."""
    n = 0
    for f in sorted(pages_dir.glob("*.ts")):
        inside = False
        for line in f.read_text().split("\n"):
            if re.match(r"^~{3,}", line):
                if not inside and re.match(r"^~{3,}[a-z]", line):
                    n += 1
                    inside = True
                elif inside:
                    inside = False
    return n


def load_pages() -> List[Dict[str, str]]:
    """The pages in site order, bodies as RENDERED — bundled by the frontend's
    esbuild and evaluated by node, so the interpolations are the compiler's."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "docs.cjs"
        subprocess.run(
            [str(ESBUILD), "content/docs/index.ts", "--bundle", "--platform=node", "--format=cjs",
             "--log-level=error", f"--outfile={out}"],
            cwd=FRONTEND, check=True, capture_output=True, text=True,
        )
        js = ("const d=require(process.argv[1]);process.stdout.write(JSON.stringify("
              "d.DOC_SECTIONS.flatMap(s=>s.pages.map(p=>({slug:p.slug,title:p.title,body:p.body})))))")
        r = subprocess.run(["node", "-e", js, str(out)], check=True, capture_output=True, text=True)
        return json.loads(r.stdout)


@dataclasses.dataclass
class Sample:
    slug: str
    title: str
    n: int
    lang: str
    kind: str
    code: str

    @property
    def first(self) -> str:
        return next((l for l in self.code.split("\n") if l.strip()), "")


def samples_of(pages: List[Dict[str, str]]) -> List[Sample]:
    out = []
    for p in pages:
        for n, f in enumerate(extract_fences(p["body"]), start=1):
            out.append(Sample(p["slug"], p["title"], n, f.lang, classify(f.lang), f.code))
    return out


# ============================================================= substitution ==

PLACEHOLDER_KEY = re.compile(r"tsk_(?:live|test)_(?:…|[0-9a-f]{16}_EXAMPLE_KEY[A-Za-z0-9_]*)")


def substitute(code: str, kind: str, ids: Dict[str, str]) -> str:
    code = code.replace(DOC_BASE, EDGE)
    if kind == "bash":
        code = PLACEHOLDER_KEY.sub("$TECHSARA_API_KEY", code)
    if ids.get("response_id"):
        code = code.replace(EXAMPLE_RESPONSE_ID, ids["response_id"])
    return code


def statuses(text: str) -> List[int]:
    return [int(s) for s in MARK.findall(text)]


def bash_bodies(stdout: str) -> List[Tuple[int, str]]:
    """(status, body) per curl invocation, split on the wrapper's marker lines.
    With -i the header block is kept in the body string."""
    out, last = [], 0
    for m in MARK.finditer(stdout):
        out.append((int(m.group(1)), stdout[last:m.start()].strip()))
        last = m.end()
    return out


def body_json(body: str) -> Optional[Any]:
    text = body
    if text.startswith("HTTP/"):
        parts = re.split(r"\r?\n\r?\n", text, maxsplit=1)
        text = parts[1] if len(parts) > 1 else ""
    try:
        return json.loads(text)
    except ValueError:
        return None


def sse_events(text: str) -> List[Tuple[str, str]]:
    """(event name, data) per frame. A frame with only data gets the name ''."""
    frames = []
    for raw in re.split(r"\r?\n\r?\n", text):
        name, data = "", ""
        for line in raw.split("\n"):
            line = line.rstrip("\r")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data += line[6:]
        if name or data:
            frames.append((name, data))
    return frames


LITERAL_KEYS = {"object", "type", "code", "param"}


def json_paths(obj: Any, prefix: str = "") -> Tuple[Set[str], Set[Tuple[str, str]]]:
    """(key paths, literal (path, value) pairs for the identifying keys). Lists
    fold into `[]` over every element, so a documented first element is
    compared against any real element."""
    paths: Set[str] = set()
    lits: Set[Tuple[str, str]] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            paths.add(p)
            if k in LITERAL_KEYS and (v is None or isinstance(v, str)) and "…" not in str(v):
                lits.add((p, json.dumps(v)))
            sub_p, sub_l = json_paths(v, p)
            paths |= sub_p
            lits |= sub_l
    elif isinstance(obj, list):
        for v in obj:
            sub_p, sub_l = json_paths(v, f"{prefix}[]")
            paths |= sub_p
            lits |= sub_l
    return paths, lits


def shape_diff(documented: Any, real: Any) -> List[str]:
    """What the documentation shows that the real object does not have."""
    dp, dl = json_paths(documented)
    rp, rl = json_paths(real)
    missing = [f"field {p}" for p in sorted(dp - rp)]
    wrong = [f"{p}={v} (real: {', '.join(rv for rp_, rv in sorted(rl) if rp_ == p) or 'absent'})"
             for p, v in sorted(dl - rl) if p in rp]
    return missing + wrong


# ================================================================== running ==


@dataclasses.dataclass
class Run:
    exit: int
    stdout: str
    stderr: str
    secs: float
    program: str = ""

    @property
    def statuses(self) -> List[int]:
        return statuses(self.stdout) + statuses(self.stderr)


CURL_WRAPPER = "curl() { command curl -w '\\n@@HTTP %{http_code}@@\\n' \"$@\"; }\n"

PY_PRELUDE = '''import sys as _sys
try:
    import httpx as _httpx
    _orig_send = _httpx.Client.send
    def _send(self, request, *a, **kw):
        r = _orig_send(self, request, *a, **kw)
        print(f"@@HTTP {r.status_code}@@", file=_sys.stderr, flush=True)
        return r
    _httpx.Client.send = _send
except ImportError:
    pass
'''

JS_PRELUDE = '''const __fetch = globalThis.fetch;
globalThis.fetch = async (...a) => { const r = await __fetch(...a); process.stderr.write(`@@HTTP ${r.status}@@\\n`); return r; };
'''


def _env(ctx: Dict[str, Any], extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    env["TECHSARA_API_KEY"] = ctx.get("key", "")
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env.update(extra or {})
    return env


def run_bash(program: str, ctx: Dict[str, Any], timeout: int = 900, extra=None) -> Run:
    t = time.monotonic()
    full = CURL_WRAPPER + program
    try:
        p = subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=timeout, env=_env(ctx, extra))
        return Run(p.returncode, p.stdout, p.stderr, time.monotonic() - t, program)
    except subprocess.TimeoutExpired as e:
        return Run(124, str(e.stdout or ""), f"timed out after {timeout}s", time.monotonic() - t, program)


def missing_python_imports(program: str) -> List[str]:
    try:
        tree = ast.parse(program)
    except SyntaxError:
        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    check = ("import sys,json,importlib.util;n=json.loads(sys.argv[1]);"
             "print(json.dumps([x for x in n if x not in sys.stdlib_module_names and importlib.util.find_spec(x) is None]))")
    r = subprocess.run([PY, "-c", check, json.dumps(sorted(names))], capture_output=True, text=True)
    return json.loads(r.stdout or "[]")


def run_python(program: str, ctx: Dict[str, Any], timeout: int = 900, extra=None) -> Run:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(PY_PRELUDE + program)
        path = f.name
    t = time.monotonic()
    try:
        p = subprocess.run([PY, path], capture_output=True, text=True, timeout=timeout, env=_env(ctx, extra))
        return Run(p.returncode, p.stdout, p.stderr, time.monotonic() - t, program)
    except subprocess.TimeoutExpired as e:
        return Run(124, str(e.stdout or ""), f"timed out after {timeout}s", time.monotonic() - t, program)
    finally:
        os.unlink(path)


def ts_to_js(source: str) -> Tuple[Optional[str], str]:
    p = subprocess.run([str(ESBUILD), "--loader=ts", "--format=esm", "--log-level=error"],
                       input=source, capture_output=True, text=True)
    return (p.stdout if p.returncode == 0 else None), p.stderr


def run_js(program: str, ctx: Dict[str, Any], timeout: int = 900, extra=None) -> Run:
    js, err = ts_to_js(JS_PRELUDE + program)
    if js is None:
        return Run(2, "", "esbuild could not compile the sample: " + err, 0.0, program)
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(js)
        path = f.name
    t = time.monotonic()
    try:
        p = subprocess.run(["node", path], capture_output=True, text=True, timeout=timeout, env=_env(ctx, extra))
        return Run(p.returncode, p.stdout, p.stderr, time.monotonic() - t, program)
    except subprocess.TimeoutExpired as e:
        return Run(124, str(e.stdout or ""), f"timed out after {timeout}s", time.monotonic() - t, program)
    finally:
        os.unlink(path)


# =================================================================== judges ==

Verdict = Tuple[bool, str]


def _terminal(names: List[str]) -> List[str]:
    return [n for n in names if n in ("response.completed", "response.failed", "response.cancelled", "error")]


def j_curl(expect: List[int], obj: Optional[str] = None, status: Optional[str] = None,
           headers: Iterable[str] = (), contains: Optional[str] = None,
           absent_headers: Iterable[str] = ()) -> Callable[[Run, Dict], Verdict]:
    def judge(run: Run, ctx: Dict) -> Verdict:
        got = bash_bodies(run.stdout)
        codes = [c for c, _ in got]
        if run.exit != 0:
            return False, f"exit {run.exit}; HTTP {codes}; {run.stderr.strip()[-200:]}"
        if codes != expect:
            last = got[-1][1][-240:] if got else run.stdout[-240:]
            return False, f"HTTP {codes}, expected {expect}; body: {last}"
        facts = [f"exit 0, HTTP {codes}"]
        if obj or status:
            b = body_json(got[-1][1])
            if not isinstance(b, dict):
                return False, f"HTTP {codes} but the last body is not JSON: {got[-1][1][:200]}"
            if obj and b.get("object") != obj:
                return False, f"HTTP {codes}; object={b.get('object')!r}, expected {obj!r}"
            if status and b.get("status") not in status.split("|"):
                return False, f"HTTP {codes}; status={b.get('status')!r}, expected {status}"
            facts.append(f"object={b.get('object')}" + (f" status={b.get('status')}" if "status" in b else "")
                         + (f" id={b.get('id')}" if str(b.get("id", "")).startswith("resp_") else ""))
            if obj == "response" and b.get("status") == "completed":
                text = "".join(c.get("text", "") for it in b.get("output") or [] for c in it.get("content") or [])
                if not text.strip():
                    return False, f"HTTP {codes}; completed with no output text"
                facts.append(f"{len(text)} chars of output text")
        for h in headers:
            if not re.search(rf"^{re.escape(h)}:", got[-1][1], re.I | re.M):
                return False, f"HTTP {codes}; header {h} absent from the -i output"
        if headers:
            facts.append("headers present: " + ", ".join(headers))
        # A header the documentation says is NOT sent is a claim too (the
        # RateLimit pair, 2026-09-13): its presence fails the sample.
        for h in absent_headers:
            if re.search(rf"^{re.escape(h)}:", got[-1][1], re.I | re.M):
                return False, f"HTTP {codes}; header {h} present in the -i output, documented as not sent"
        if absent_headers:
            facts.append("headers absent: " + ", ".join(absent_headers))
        if contains and contains not in got[-1][1]:
            return False, f"HTTP {codes}; {contains!r} not in the body: {got[-1][1][-200:]}"
        if contains:
            facts.append(f"body names {contains}")
        return True, "; ".join(facts)
    return judge


def j_curl_stream(run: Run, ctx: Dict) -> Verdict:
    got = bash_bodies(run.stdout)
    codes = [c for c, _ in got]
    if run.exit != 0 or codes != [200]:
        return False, f"exit {run.exit}, HTTP {codes}; {run.stdout[-200:]}"
    names = [n for n, _ in sse_events(got[0][1])]
    deltas = names.count("response.output_text.delta")
    term = _terminal(names)
    if term != ["response.completed"] or deltas < 1:
        return False, f"HTTP 200; {len(names)} events, {deltas} deltas, terminals {term}"
    return True, f"exit 0, HTTP 200; {len(names)} SSE events received, {deltas} output_text.delta, one terminal response.completed"


def j_prog(expect: List[int], stdout_has: Optional[str] = None, stdout_nonempty: bool = False,
           check: Optional[Callable[[str], Optional[str]]] = None) -> Callable[[Run, Dict], Verdict]:
    def judge(run: Run, ctx: Dict) -> Verdict:
        codes = run.statuses
        if run.exit != 0:
            return False, f"exit {run.exit}, HTTP {codes}; {run.stderr.strip()[-300:]}"
        if codes != expect:
            return False, f"exit 0 but HTTP {codes}, expected {expect}"
        visible = MARK.sub("", run.stdout).strip()
        if stdout_has and stdout_has not in visible:
            return False, f"exit 0, HTTP {codes}; {stdout_has!r} not printed: {visible[-200:]}"
        if stdout_nonempty and not visible:
            return False, f"exit 0, HTTP {codes}; printed nothing"
        if check:
            problem = check(visible)
            if problem:
                return False, f"exit 0, HTTP {codes}; {problem}"
        tail = visible.replace("\n", " ⏎ ")
        return True, f"exit 0, HTTP {codes}; printed {len(visible)} chars: {tail[:120]}{'…' if len(tail) > 120 else ''}"
    return judge


def _line_value(visible: str, tag: str) -> Optional[str]:
    m = re.search(rf"^{tag} (.*)$", visible, re.M)
    return m.group(1).strip() if m else None


def expect_tag(tag: str, allowed: Callable[[str], bool], what: str) -> Callable[[str], Optional[str]]:
    def check(visible: str) -> Optional[str]:
        v = _line_value(visible, tag)
        if v is None:
            return f"the driver line {tag} was not printed"
        return None if allowed(v) else f"{tag} {v} — expected {what}"
    return check


# ==================================================================== specs ==


@dataclasses.dataclass
class Spec:
    first: str
    action: str  # run | post | shape | check | static
    reason: str = ""
    pre: Tuple[Tuple[str, int], ...] = ()
    head: str = ""
    tail: str = ""
    ids: str = ""  # done | fresh
    judge: Optional[Callable[[Run, Dict], Verdict]] = None
    ref: str = ""
    check: Optional[Callable[[Sample, Dict], Verdict]] = None
    post_path: str = "/responses"


PY_API = "api = client()\n"
NE_HTTP = "an HTTP line, not a program — the same line is sent or checked by the executed samples"
NE_NOFAIL = "a failure-path transcript: the stream failure cannot be provoked on demand against a healthy engine"
NE_WEBHOOK = ("webhook verification needs a delivery; the SSRF guard refuses a loopback receiver and the isolated "
              "stack has no public HTTPS endpoint — verified locally against the server's own signer instead")


def _status_is(*ok: str) -> Callable[[str], bool]:
    return lambda v: v in ok


def build_specs() -> Dict[Tuple[str, int], Spec]:
    S: Dict[Tuple[str, int], Spec] = {}
    ok_resp = j_curl([200], obj="response", status="completed")
    stream_prog = j_prog([200], stdout_nonempty=True)

    S["overview", 1] = Spec("export TECHSARA_API_KEY=", "run", judge=j_curl([200], obj="list"))

    S["quickstart", 1] = Spec('export TECHSARA_API_KEY="tsk_live_', "run",
                              tail='\ntest "${#TECHSARA_API_KEY}" -gt 40 && echo KEY_SET\n',
                              judge=j_prog([], stdout_has="KEY_SET"))
    S["quickstart", 2] = Spec("curl ", "run", judge=ok_resp)
    S["quickstart", 3] = Spec("{", "shape", ref="response_completed")
    S["quickstart", 4] = Spec("import os", "run", judge=j_prog([200], stdout_nonempty=True))
    S["quickstart", 5] = Spec("const response = await fetch(", "run", judge=j_prog([200], stdout_nonempty=True))

    S["authentication", 1] = Spec("Authorization: Bearer", "check", reason=NE_HTTP, check=chk_example_key_401)
    S["authentication", 2] = Spec("{", "shape", ref="error_scope")
    S["authentication", 3] = Spec("WWW-Authenticate:", "check", reason="a response header", check=chk_www_authenticate)
    S["authentication", 4] = Spec("export TECHSARA_API_KEY=", "run",
                                  judge=j_curl([200], headers=("x-request-id",)))

    S["key-security", 1] = Spec("tsk_live_", "check", reason="an annotated diagram of the key format",
                                check=chk_key_anatomy)
    S["key-security", 2] = Spec("rotate", "static", reason="a prose diagram of the rotation window")

    S["models", 1] = Spec("export TECHSARA_API_KEY=", "run", judge=j_curl([200], obj="list", contains=MODEL))
    S["models", 2] = Spec("{", "shape", ref="models")
    S["models", 3] = Spec("curl ", "run", judge=j_curl([200], obj="model"))
    S["models", 4] = Spec("{", "static", reason='a request-body fragment whose input is elided ("…")')

    S["responses", 1] = Spec("POST /v1/responses", "static", reason=NE_HTTP)
    S["responses", 2] = Spec("{", "post", reason="a request body — POSTed as documented",
                             judge=lambda r, c: j_curl_stream(r, c))
    S["responses", 3] = Spec("{", "post", reason="a request body — POSTed as documented", judge=ok_resp)
    S["responses", 4] = Spec("{", "shape", ref="error_top_p")
    S["responses", 5] = Spec("{", "shape", ref="response_completed")
    S["responses", 6] = Spec("GET /v1/responses/{id}", "static", reason=NE_HTTP)
    S["responses", 7] = Spec("export TECHSARA_API_KEY=", "run", ids="done",
                             judge=j_curl([200], obj="response", status="completed"))
    S["responses", 8] = Spec("POST /v1/responses/{id}/cancel", "static", reason=NE_HTTP)
    S["responses", 9] = Spec("curl -X POST", "run", ids="fresh",
                             judge=j_curl([200], obj="response", status="cancelled|queued|in_progress|completed"))

    S["chat-completions", 1] = Spec("POST /v1/chat/completions", "static", reason=NE_HTTP)
    S["chat-completions", 2] = Spec("export TECHSARA_API_KEY=", "run", judge=j_curl([200], obj="chat.completion"))
    S["chat-completions", 3] = Spec("{", "shape", ref="chat")
    S["chat-completions", 4] = Spec("data: ", "check", reason="a stream transcript", check=chk_chat_transcript)
    S["chat-completions", 5] = Spec("data: ", "static", reason=NE_NOFAIL)

    S["streaming", 1] = Spec("export TECHSARA_API_KEY=", "run", judge=j_curl_stream)
    S["streaming", 2] = Spec("Content-Type: text/event-stream", "check", reason="response headers",
                             check=chk_stream_headers)
    S["streaming", 3] = Spec("response.created", "check", reason="the event order, as a list",
                             check=chk_event_order)
    S["streaming", 4] = Spec("event: response.created", "check", reason="a stream transcript",
                             check=chk_stream_transcript)
    S["streaming", 5] = Spec(": ping", "static", reason="the heartbeat comment line, sent only on a 15 s idle gap")
    S["streaming", 6] = Spec("event: response.failed", "static", reason=NE_NOFAIL)
    S["streaming", 7] = Spec("event: error", "static", reason=NE_NOFAIL)
    S["streaming", 8] = Spec("import json", "run",
                             judge=j_prog([200], stdout_has="usage:", check=lambda v: None if "input_tokens" in v
                                          else "usage line carries no input_tokens"))
    S["streaming", 9] = Spec("const response = await fetch(", "run",
                             head=f'const BASE_URL = "{EDGE}";\nconst apiKey = process.env.TECHSARA_API_KEY;\n',
                             judge=j_prog([200], stdout_has="input_tokens"))

    S["background", 1] = Spec("export TECHSARA_API_KEY=", "run",
                              judge=j_curl([202], obj="response", status="queued|in_progress"))
    S["background", 2] = Spec("{", "shape", ref="background_created")
    S["background", 3] = Spec("curl ", "run", ids="done", judge=j_curl([200], obj="response", status="completed"))
    S["background", 4] = Spec("import time", "run", pre=(("python", 2),), ids="fresh_done",
                              tail=f'\nwith client() as api:\n    body = wait_for("{{RID}}", api)\nprint("STATUS", body["status"])\n',
                              judge=j_prog_polls("completed"))
    S["background", 5] = Spec("curl -X POST", "run", ids="fresh",
                              judge=j_curl([200], obj="response", status="cancelled|queued|in_progress|completed"))

    S["webhooks", 1] = Spec("{", "static", reason=NE_WEBHOOK.split(" — ")[0])
    S["webhooks", 2] = Spec("TechSara-Signature:", "check", reason="a request header on a delivery",
                            check=chk_signature_header)
    S["webhooks", 3] = Spec("import hmac", "check", reason=NE_WEBHOOK, check=chk_webhook_python)
    S["webhooks", 4] = Spec('import { createHmac', "check", reason=NE_WEBHOOK, check=chk_webhook_node)

    S["errors", 1] = Spec("{", "shape", ref="error_401")
    S["errors", 2] = Spec("import random", "run", pre=(("python", 2),),
                          tail='\nwith client() as api:\n    body = send_with_retry(api, {"model": MODEL, "input": "Name one ocean."})\nprint("STATUS", body["status"])\n',
                          judge=j_prog([200], check=expect_tag("STATUS", _status_is("completed"), "completed")))

    # 2026-09-13, owner decision: the API enforces no usage limits and sends no
    # RateLimit headers, so the rate-limits page has no header sample left to
    # check (it was `S["rate-limits", 1]`, judged by chk_ratelimit). The page
    # is prose now; the self-test's "no spec names a block that no longer
    # exists" is what keeps a stale entry from coming back.

    S["idempotency", 1] = Spec("export TECHSARA_API_KEY=", "run", judge=ok_resp)
    S["idempotency", 2] = Spec("import uuid", "run", pre=(("python", 2),),
                               tail='\nwith client() as api:\n    body = summarise("docs-run-{RUN}-idempotency-page", "The office is closed on Friday for maintenance.", api)\nprint("STATUS", body.get("status"))\n',
                               judge=j_prog([200], check=expect_tag("STATUS", _status_is("completed"), "completed")))

    S["usage", 1] = Spec("GET /v1/usage", "static", reason=NE_HTTP)
    S["usage", 2] = Spec("export TECHSARA_API_KEY=", "run", judge=j_curl([200], obj="list"))
    S["usage", 3] = Spec("{", "shape", ref="usage")

    S["tools", 1] = Spec("{", "shape", ref="model")
    S["tools", 2] = Spec("{", "shape", ref="error_tools")
    S["tools", 3] = Spec("instructions = (", "run", pre=(("python", 2),),
                         head='import json\napi = client()\ncustomer_message = "I was charged twice for order 1182 and want my money back."\n',
                         tail='\nprint("PARSED", parsed)\n',
                         judge=j_prog([200], stdout_has="PARSED"))

    S["python", 1] = Spec("pip install httpx", "run", reason="pip install → the venv's pip with --dry-run",
                          judge=j_prog([], stdout_has="httpx"))
    S["python", 2] = Spec('"""A minimal TechSara client.', "run",
                          tail='\nwith client() as api:\n    r = api.get("/models")\nprint("MODELS", r.status_code, [m["id"] for m in r.json()["data"]])\n',
                          judge=j_prog([200], stdout_has="MODELS 200"))
    S["python", 3] = Spec("def ask(", "run", pre=(("python", 2),),
                          tail='\nprint("ANSWER", ask("Name one planet."))\n', judge=j_prog([200], stdout_has="ANSWER "))
    S["python", 4] = Spec("messages = [", "run", pre=(("python", 2),), tail='\nprint("STATUS", body["status"])\n',
                          judge=j_prog([200], check=expect_tag("STATUS", _status_is("completed"), "completed")))
    S["python", 5] = Spec("import json", "run", pre=(("python", 2),), judge=stream_prog)
    S["python", 6] = Spec("import random", "run", pre=(("python", 2),),
                          tail='\nwith client() as api:\n    body = post_with_retry(api, "/responses", {"model": MODEL, "input": "Name one river."})\nprint("STATUS", body["status"])\n',
                          judge=j_prog([200], check=expect_tag("STATUS", _status_is("completed"), "completed")))
    S["python", 7] = Spec("import uuid", "run", pre=(("python", 2),),
                          tail='\nwith client() as api:\n    rid = summarise_in_background(api, "docs-run-{RUN}-python-page", "Summarise: the office is closed on Friday.")\nprint("ID", rid)\n',
                          judge=j_prog([202], check=expect_tag("ID", lambda v: v.startswith("resp_"), "a resp_ id")))

    S["javascript", 1] = Spec("// NEVER do this.", "static",
                              reason="an anti-pattern (a key in browser code) shown so the reader does not do it")
    S["javascript", 2] = Spec("export interface Usage", "check", reason="type declarations only",
                              check=chk_ts_compiles)
    S["javascript", 3] = Spec('const BASE_URL = "', "run", pre=(("javascript", 2),),
                              tail='\nconsole.log("ANSWER", await ask("Name one planet."));\n',
                              judge=j_prog([200], stdout_has="ANSWER "))
    S["javascript", 4] = Spec("export async function* streamText(", "run",
                              pre=(("javascript", 2), ("javascript", 3)),
                              tail='\nlet n = 0, chars = 0;\nfor await (const piece of streamText("Explain RAG in one sentence.")) { n += 1; chars += piece.length; }\nconsole.log("DELTAS", n, chars);\n',
                              judge=j_prog([200], check=expect_tag("DELTAS", lambda v: int(v.split()[0]) > 0,
                                                                   "at least one delta")))
    S["javascript", 5] = Spec("const RETRYABLE = new Set([", "run", pre=(("javascript", 2), ("javascript", 3)),
                              tail='\nconst b = await postWithRetry("/responses", { model: MODEL, input: "Name one river." });\nconsole.log("STATUS", b.status);\n',
                              judge=j_prog([200], check=expect_tag("STATUS", _status_is("completed"), "completed")))
    S["javascript", 6] = Spec("await fetch(", "run", pre=(("javascript", 2), ("javascript", 3)),
                              head='const ticketId = crypto.randomUUID();\nconst text = "Summarise: the office is closed on Friday.";\n',
                              judge=j_prog([202]))

    S["curl", 1] = Spec('export TECHSARA_API_KEY="tsk_live_', "run",
                        tail='\ntest "$TECHSARA_BASE_URL" = "' + EDGE + '" && test "${#TECHSARA_API_KEY}" -gt 40 && echo ENV_SET\n',
                        judge=j_prog([], stdout_has="ENV_SET"))
    cpre = (("curl", 1),)
    S["curl", 2] = Spec('curl "$TECHSARA_BASE_URL/models"', "run", pre=cpre, judge=j_curl([200, 200], obj="model"))
    S["curl", 3] = Spec('curl "$TECHSARA_BASE_URL/responses"', "run", pre=cpre, judge=ok_resp)
    S["curl", 4] = Spec('curl "$TECHSARA_BASE_URL/responses"', "run", pre=cpre, judge=ok_resp)
    S["curl", 5] = Spec('curl -N "$TECHSARA_BASE_URL/responses"', "run", pre=cpre, judge=j_curl_stream)
    S["curl", 6] = Spec('curl "$TECHSARA_BASE_URL/responses"', "run", pre=cpre, ids="done",
                        judge=j_curl([202, 200], obj="response", status="completed"))
    S["curl", 7] = Spec('curl -X POST "$TECHSARA_BASE_URL/responses/', "run", pre=cpre, ids="fresh",
                        judge=j_curl([200], obj="response", status="cancelled|queued|in_progress|completed"))
    S["curl", 8] = Spec('curl "$TECHSARA_BASE_URL/usage"', "run", pre=cpre, judge=j_curl([200], obj="list"))
    S["curl", 9] = Spec('curl "$TECHSARA_BASE_URL/openapi.json"', "run", pre=cpre, judge=j_curl([200], contains='"openapi"'))
    # 2026-09-13, owner decision: with PUBLIC_API_ENFORCE_LIMITS off (the
    # default) no RateLimit header is sent — one would advertise a limit that
    # does not exist — so the page now says `-i` shows X-Request-Id and NO
    # RateLimit header, and the judge holds the server to both halves. A stack
    # still sending the headers fails this sample rather than passing it.
    S["curl", 10] = Spec('curl -i "$TECHSARA_BASE_URL/models"', "run", pre=cpre,
                         judge=j_curl([200], headers=("x-request-id",),
                                      absent_headers=("ratelimit", "ratelimit-policy")))
    S["curl", 11] = Spec('curl -sS -i "$TECHSARA_BASE_URL/responses"', "run", pre=cpre,
                         reason="demonstrates the documented 400 — judged by that outcome",
                         judge=j_curl([400], contains='"param":"top_p"'))

    S["status", 1] = Spec("export TECHSARA_API_KEY=", "run", judge=j_curl([200], headers=("x-request-id",)))
    return S


def j_prog_polls(want: str) -> Callable[[Run, Dict], Verdict]:
    inner = j_prog([], check=expect_tag("STATUS", _status_is(want), want))

    def judge(run: Run, ctx: Dict) -> Verdict:
        codes = run.statuses
        if codes and (set(codes) != {200}):
            return False, f"exit {run.exit}; poll statuses {sorted(set(codes))}"
        ok, ev = inner(Run(run.exit, run.stdout, MARK.sub("", run.stderr), run.secs), ctx)
        return ok, ev.replace("HTTP []", f"{len(codes)} polls, all HTTP 200")
    return judge


# ============================================================ cross-checks ==


def chk_example_key_401(sample: Sample, ctx: Dict) -> Verdict:
    r = ctx["refs"].get("error_401")
    if not r:
        return False, "no 401 reference was captured"
    ok = r["status"] == 401 and r["body"].get("error", {}).get("code") == "invalid_api_key"
    return ok, (f"the documented example key sent as this header → HTTP {r['status']} "
                f"code={r['body'].get('error', {}).get('code')}")


def chk_www_authenticate(sample: Sample, ctx: Dict) -> Verdict:
    r = ctx["refs"].get("error_scope")
    real = (r or {}).get("headers", {}).get("www-authenticate")
    doc = sample.code.split(":", 1)[1].strip()
    origin = ctx["refs"].get("error_scope_origin", {})
    at_origin = origin.get("headers", {}).get("www-authenticate")
    return real == doc, (f"a models.read-only key POSTing /responses through the edge → HTTP {(r or {}).get('status')}, "
                         f"header {'matches' if real == doc else 'is ' + repr(real)}; the same request at the origin "
                         f"{ORCH} → HTTP {origin.get('status')}, header {'matches' if at_origin == doc else 'is ' + repr(at_origin)}")


def chk_key_anatomy(sample: Sample, ctx: Dict) -> Verdict:
    pat = re.compile(r"^tsk_(live|test)_[0-9a-f]{16}_[A-Za-z0-9_-]{43}[A-Za-z0-9]{6}$")
    doc_ok = bool(pat.match(sample.first.strip()))
    real_ok = bool(pat.match(ctx.get("key", "")))
    return doc_ok and real_ok, f"diagrammed layout (prefix, 16-hex public id, 43-char secret, 6-char checksum) matches the documented example: {doc_ok}; matches the real key minted for this run: {real_ok}"


def chk_chat_transcript(sample: Sample, ctx: Dict) -> Verdict:
    real = ctx["refs"].get("chat_stream")
    if not real:
        return False, "no chat stream reference"
    real_objs = [json.loads(d) for _, d in real["frames"] if d and d != "[DONE]"]
    problems = []
    for _, d in sse_events(sample.code):
        if d == "[DONE]":
            if not any(x == "[DONE]" for _, x in real["frames"]):
                problems.append("real stream has no [DONE]")
            continue
        doc = json.loads(d)
        if not any(not shape_diff(doc, ro) for ro in real_objs):
            best = min((shape_diff(doc, ro) for ro in real_objs), key=len, default=["no chunks"])
            problems.append("; ".join(best))
    return not problems, (f"compared with a real chat stream ({len(real_objs)} chunks, HTTP {real['status']}): "
                          + ("every documented chunk shape occurs" if not problems else "MISMATCH " + " | ".join(problems)))


def header_matches(name: str, documented: str, real: Optional[str]) -> bool:
    """Exact, except Content-Type compares the MEDIA TYPE (RFC 9110 §8.3.1).

    2026-09-13, first run: both the origin and the edge send
    `text/event-stream; charset=utf-8` where the page shows `text/event-stream`.
    A parameter is not a different type — every SSE client matches on the media
    type — so flagging it would bury the real header defect (a missing
    WWW-Authenticate) under a pedantic one. The real value is still printed."""
    if real is None:
        return False
    if name.lower() == "content-type":
        return real.split(";")[0].strip().lower() == documented.split(";")[0].strip().lower()
    return real == documented


def chk_stream_headers(sample: Sample, ctx: Dict) -> Verdict:
    real = ctx["refs"].get("stream", {}).get("headers", {})
    problems = []
    for line in sample.code.strip().split("\n"):
        name, _, value = line.partition(":")
        got = real.get(name.strip().lower())
        if not header_matches(name.strip(), value.strip(), got):
            problems.append(f"{name.strip()}: real {got!r}")
    return not problems, ("every documented header has the documented value on a real stream through the edge ("
                          + "; ".join(f"{k}: {real.get(k)}" for k in ("content-type", "cache-control", "x-accel-buffering", "connection")) + ")"
                          if not problems else "MISMATCH " + "; ".join(problems))


def chk_event_order(sample: Sample, ctx: Dict) -> Verdict:
    doc = [l.split()[0] for l in sample.code.strip().split("\n") if l.strip()]
    names = [n for n, _ in ctx["refs"].get("stream", {}).get("frames", []) if n]
    collapsed = [n for i, n in enumerate(names) if i == 0 or n != names[i - 1]]
    return collapsed == doc, f"real stream order (repeats collapsed): {' → '.join(collapsed)}"


def chk_stream_transcript(sample: Sample, ctx: Dict) -> Verdict:
    frames = ctx["refs"].get("stream", {}).get("frames", [])
    by_name: Dict[str, List[Any]] = {}
    for n, d in frames:
        try:
            by_name.setdefault(n, []).append(json.loads(d))
        except ValueError:
            pass
    problems = []
    for n, d in sse_events(sample.code):
        doc = json.loads(d)
        reals = by_name.get(n)
        if not reals:
            problems.append(f"event {n} not in the real stream")
            continue
        if doc.get("type") != n:
            problems.append(f"event {n} documents type={doc.get('type')}")
        if all(shape_diff(doc, r) for r in reals):
            problems.append(f"{n}: " + "; ".join(shape_diff(doc, reals[0])))
    return not problems, ("every documented frame's fields occur on the same event in a real stream"
                          if not problems else "MISMATCH " + " | ".join(problems))


def chk_signature_header(sample: Sample, ctx: Dict) -> Verdict:
    hdr = ctx.get("webhook", {}).get("header", "")
    ok = bool(re.fullmatch(r"t=\d+(,v1=[0-9a-f]{64})+", hdr)) and sample.first.startswith("TechSara-Signature: t=")
    return ok, f"the server signer (app/apiplatform/webhooks/signer.py) emits t=<unix>,v1=<64 hex>: {ok}"


def _webhook_verdict(run: Run) -> Verdict:
    line = _line_value(MARK.sub("", run.stdout), "WEBHOOK")
    if run.exit != 0 or line is None:
        return False, f"local check exit {run.exit}: {run.stderr.strip()[-200:]}"
    got = json.loads(line)
    want = {"signed": True, "rotated_old_secret": True, "tampered": False, "wrong_secret": False, "stale": False}
    return got == want, f"local check against the server signer: {got}"


def chk_webhook_python(sample: Sample, ctx: Dict) -> Verdict:
    w = ctx["webhook"]
    tail = f'''
import json, os
body = os.environ["WH_BODY"].encode()
h, stale = os.environ["WH_HEADER"], os.environ["WH_STALE"]
print("WEBHOOK", json.dumps({{
  "signed": verify(body, h, "whsec_new"), "rotated_old_secret": verify(body, h, "nope", "whsec_old"),
  "tampered": verify(body + b" ", h, "whsec_new"), "wrong_secret": verify(body, h, "nope"),
  "stale": verify(body, stale, "whsec_new")}}))
'''
    run = run_python(sample.code + tail, ctx, 60, extra=w["env"])
    ctx["runs"][(sample.slug, sample.n)] = run
    return _webhook_verdict(run)


def chk_webhook_node(sample: Sample, ctx: Dict) -> Verdict:
    w = ctx["webhook"]
    tail = '''
const body = Buffer.from(process.env.WH_BODY);
const h = process.env.WH_HEADER, stale = process.env.WH_STALE;
console.log("WEBHOOK", JSON.stringify({
  signed: verify(body, h, "whsec_new"), rotated_old_secret: verify(body, h, "nope", "whsec_old"),
  tampered: verify(Buffer.concat([body, Buffer.from(" ")]), h, "whsec_new"), wrong_secret: verify(body, h, "nope"),
  stale: verify(body, stale, "whsec_new")}));
'''
    run = run_js(sample.code + tail, ctx, 60, extra=w["env"])
    ctx["runs"][(sample.slug, sample.n)] = run
    return _webhook_verdict(run)


def chk_ts_compiles(sample: Sample, ctx: Dict) -> Verdict:
    js, err = ts_to_js(sample.code)
    return js is not None, "esbuild compiles the declarations" if js is not None else f"esbuild: {err[:200]}"


# ================================================================ the stack ==


def scrub(text: str, ctx: Dict) -> str:
    for secret in (ctx.get("key"), ctx.get("key_limited"), ctx.get("password")):
        if secret:
            text = text.replace(secret, "<redacted>")
    return re.sub(r"tsk_(live|test)_[0-9a-f]{16}_[A-Za-z0-9_-]{49}", "<a key>", text)


def provision(ctx: Dict) -> None:
    import httpx

    password = PW_FILE.read_text().strip()
    ctx["password"] = password
    c = httpx.Client(timeout=60)
    r = c.post(f"{ORCH}/auth/login", json={"email": EMAIL, "password": password})
    if r.status_code != 200:
        raise SystemExit(f"login failed: HTTP {r.status_code}")
    # The session cookie is Secure and httpx will not replay it over http://,
    # so it is pinned as a header — exactly as scripts/devapi_smoke.py _login.
    pair = r.headers.get("set-cookie", "").split(";", 1)[0].strip()
    c.headers["Cookie"] = pair
    if c.get(f"{ORCH}/auth/me").status_code != 200:
        raise SystemExit("the session did not stick")
    dev = f"{ORCH}/admin/api/developers"
    r = c.post(f"{dev}/projects", json={"name": f"docs-examples-{ctx['run']}", "environment": "test"})
    if r.status_code not in (200, 201):
        raise SystemExit(f"project create failed: HTTP {r.status_code} {r.text[:200]}")
    pid = r.json()["project"]["id"]
    ctx["project_id"] = pid
    r = c.post(f"{dev}/projects/{pid}/keys", json={
        "name": "docs examples", "scopes": ["models.read", "responses.read", "responses.write", "usage.read"]})
    if r.status_code not in (200, 201) or not r.json().get("secret"):
        raise SystemExit(f"key create failed: HTTP {r.status_code}")
    ctx["key"] = r.json()["secret"]
    r = c.post(f"{dev}/projects/{pid}/keys", json={"name": "docs examples models-only", "scopes": ["models.read"]})
    if r.status_code not in (200, 201) or not r.json().get("secret"):
        raise SystemExit(f"limited key create failed: HTTP {r.status_code}")
    ctx["key_limited"] = r.json()["secret"]
    print(f"signed in as {EMAIL}; project {pid}; two test keys minted (not printed)")


def _ref(ctx: Dict, name: str, method: str, path: str, key: Optional[str] = None, **kw) -> Dict:
    import httpx

    with httpx.Client(timeout=600) as c:
        r = c.request(method, f"{EDGE}{path}", headers={"Authorization": f"Bearer {key or ctx['key']}"}, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {}
    ctx["refs"][name] = {"status": r.status_code, "body": body, "headers": {k.lower(): v for k, v in r.headers.items()}}
    return ctx["refs"][name]


def _ref_stream(ctx: Dict, name: str, path: str, payload: Dict) -> None:
    import httpx

    with httpx.Client(timeout=600) as c:
        with c.stream("POST", f"{EDGE}{path}", headers={"Authorization": f"Bearer {ctx['key']}"}, json=payload) as r:
            text = "".join(r.iter_text())
            ctx["refs"][name] = {"status": r.status_code, "headers": {k.lower(): v for k, v in r.headers.items()},
                                 "frames": sse_events(text)}


def background_id(ctx: Dict, wait: bool, prompt: str) -> str:
    import httpx

    r = _ref(ctx, "_bg", "POST", "/responses", json={"model": MODEL, "input": prompt, "background": True})
    rid = r["body"].get("id", "")
    if wait:
        with httpx.Client(timeout=60) as c:
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                s = c.get(f"{EDGE}/responses/{rid}", headers={"Authorization": f"Bearer {ctx['key']}"}).json().get("status")
                if s in ("completed", "failed", "cancelled"):
                    break
                time.sleep(3)
    return rid


def capture_refs(ctx: Dict) -> None:
    print("capturing reference objects from the real API …")
    _ref(ctx, "models", "GET", "/models")
    _ref(ctx, "model", "GET", f"/models/{MODEL}")
    _ref(ctx, "error_401", "GET", "/models",
         key="tsk_live_0123456789abcdef_EXAMPLE_KEY_DO_NOT_USE_THIS_IS_NOT_A_SECRETEXAMPL")
    _ref(ctx, "error_top_p", "POST", "/responses", json={"model": MODEL, "input": "hello", "top_p": 0.9})
    _ref(ctx, "error_tools", "POST", "/responses",
         json={"model": MODEL, "input": "hello", "tools": [{"type": "function", "name": "lookup"}]})
    _ref(ctx, "error_scope", "POST", "/responses", key=ctx["key_limited"], json={"model": MODEL, "input": "hello"})
    # The same refusal read at the ORIGIN, bypassing the edge: when a header is
    # missing at the edge this says whether the server never sent it or the
    # edge dropped it (2026-09-13: the edge dropped WWW-Authenticate).
    import httpx
    o = httpx.post(f"{ORCH}/v1/responses", headers={"Authorization": f"Bearer {ctx['key_limited']}"},
                   json={"model": MODEL, "input": "hello"}, timeout=60)
    ctx["refs"]["error_scope_origin"] = {"status": o.status_code, "body": {}, "headers": {k.lower(): v for k, v in o.headers.items()}}
    _ref(ctx, "response_completed", "POST", "/responses",
         json={"model": MODEL, "input": "Explain retrieval-augmented generation in two sentences."})
    _ref(ctx, "background_created", "POST", "/responses",
         json={"model": MODEL, "input": "Summarise the attached policy in 300 words.", "background": True})
    ctx["ids"]["done"] = ctx["refs"]["background_created"]["body"].get("id", "")
    _ref(ctx, "chat", "POST", "/chat/completions",
         json={"model": MODEL, "messages": [{"role": "user", "content": "Name one planet."}]})
    _ref_stream(ctx, "chat_stream", "/chat/completions",
                {"model": MODEL, "messages": [{"role": "user", "content": "Name one planet."}], "stream": True})
    _ref_stream(ctx, "stream", "/responses", {"model": MODEL, "input": "Explain RAG.", "stream": True})
    # Wait until the `done` id is terminal: the samples that read it back expect `completed`.
    import httpx
    with httpx.Client(timeout=60) as c:
        for _ in range(300):
            s = c.get(f"{EDGE}/responses/{ctx['ids']['done']}", headers={"Authorization": f"Bearer {ctx['key']}"}).json()
            if s.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(3)
    # The webhook signature, produced by the SERVER's signer module loaded by
    # path (the package __init__ pulls in the database layer; signer.py is
    # stdlib-only), so the documented verifiers are checked against the bytes
    # production would actually send.
    spec = importlib.util.spec_from_file_location(
        "docs_signer", REPO / "orchestrator" / "app" / "apiplatform" / "webhooks" / "signer.py")
    signer = importlib.util.module_from_spec(spec)
    sys.modules["docs_signer"] = signer
    spec.loader.exec_module(signer)  # type: ignore[union-attr]
    body = json.dumps({"id": "evt_docs", "object": "event", "type": "response.completed"})
    header = signer.signature_header(body.encode(), ["whsec_new", "whsec_old"])
    stale = signer.sign(body.encode(), "whsec_new", timestamp=int(time.time()) - 3600)
    ctx["webhook"] = {"header": header, "env": {"WH_BODY": body, "WH_HEADER": header, "WH_STALE": stale}}
    for k, v in ctx["refs"].items():
        print(f"  ref {k}: HTTP {v['status']}")


# ================================================================== the run ==


@dataclasses.dataclass
class Result:
    sample: Sample
    verdict: str
    evidence: str
    changes: List[str]
    mismatch: bool = False
    secs: float = 0.0


def assemble(sample: Sample, spec: Spec, by_key: Dict[Tuple[str, int], Sample], ctx: Dict) -> Tuple[str, List[str]]:
    changes = []
    parts = []
    for key in spec.pre:
        parts.append(by_key[key].code)
        changes.append(f"prepended {key[0]} block {key[1]} (defines what this block calls)")
    if spec.head:
        parts.append(spec.head.rstrip("\n"))
        changes.append("harness setup before the block: " + spec.head.strip().replace("\n", " ⏎ "))
    parts.append(sample.code)
    if spec.tail:
        parts.append(spec.tail.strip("\n"))
        changes.append("harness driver after the block: " + spec.tail.strip().replace("\n", " ⏎ ")
                       .replace("{RID}", "<real id>").replace("{RUN}", ctx["run"]))
    program = "\n".join(parts) + "\n"
    rid = ""
    if spec.ids == "done":
        rid = ctx["ids"]["done"]
        changes.append(f"{EXAMPLE_RESPONSE_ID} → a completed background response created by this run")
    elif spec.ids == "fresh":
        rid = background_id(ctx, False, "Write a 1500-word history of the printing press.")
        changes.append(f"{EXAMPLE_RESPONSE_ID} → a background response created seconds earlier (still running)")
    elif spec.ids == "fresh_done":
        rid = background_id(ctx, False, "Name three rivers.")
        changes.append("{RID} → a background response created seconds earlier")
    program = program.replace("{RID}", rid).replace("{RUN}", ctx["run"])
    kind = sample.kind
    before = program
    program = substitute(program, kind if kind != "json" else "json", {"response_id": rid})
    if DOC_BASE in before:
        changes.append(f"{DOC_BASE} → {EDGE}")
    if kind == "bash" and PLACEHOLDER_KEY.search(before):
        changes.append('placeholder key → "$TECHSARA_API_KEY" (the real key, from the environment)')
    if sample.slug == "python" and sample.n == 1:
        program = program.replace("pip install httpx", f'"{PY}" -m pip install --dry-run httpx')
        changes.append("pip install httpx → the orchestrator venv's pip, --dry-run (nothing installed or changed)")
    return program, changes


def execute(samples: List[Sample], ctx: Dict, every: Optional[List[Sample]] = None) -> List[Result]:
    specs = build_specs()
    # Prepended blocks come from EVERY page, so --only python still finds nothing missing
    # and --only tools can prepend python.ts's client().
    by_key = {(s.slug, s.n): s for s in (every or samples)}
    results: List[Result] = []
    for s in samples:
        spec = specs.get((s.slug, s.n))
        label = f"{s.slug} #{s.n} ({s.lang})"
        if spec is None:
            if s.kind in ("bash", "python", "javascript"):
                results.append(Result(s, FAILED, "no execution spec — this sample is new or moved; add one", []))
            else:
                results.append(Result(s, NOT_EXECUTABLE, f"a {s.lang or 'plain'} block is not a program (no spec)", []))
            print(f"  {results[-1].verdict:15} {label}")
            continue
        if not s.first.startswith(spec.first):
            results.append(Result(s, FAILED, f"spec is stale: expected a block starting {spec.first!r}, found {s.first!r}", []))
            print(f"  {FAILED:15} {label} — stale spec")
            continue
        t = time.monotonic()
        if spec.action in ("static", "shape", "check"):
            reason = spec.reason or "a documented response shape"
            ok, ev = True, ""
            if spec.action == "shape" and spec.ref == "usage":
                ev = "compared at the end of the run, once this run has produced usage"
            elif spec.action == "shape":
                ref = ctx["refs"].get(spec.ref)
                if not ref:
                    ok, ev = False, f"no reference {spec.ref} captured"
                else:
                    diff = shape_diff(json.loads(s.code), ref["body"])
                    ok = not diff
                    ev = (f"every documented field occurs on a real {spec.ref.replace('_', ' ')} (HTTP {ref['status']})"
                          if ok else f"MISMATCH against a real {spec.ref} (HTTP {ref['status']}): " + "; ".join(diff))
            elif spec.action == "check" and spec.check:
                ok, ev = spec.check(s, ctx)
                if not ok and not ev.startswith("MISMATCH"):
                    ev = "MISMATCH " + ev
            evidence = reason + (" — cross-check: " + ev if ev else "")
            results.append(Result(s, NOT_EXECUTABLE, scrub(evidence, ctx), [], mismatch=not ok,
                                  secs=time.monotonic() - t))
            print(f"  {NOT_EXECUTABLE:15} {label}{'  ** MISMATCH' if not ok else ''}")
            continue
        if spec.action == "post":
            program = (f"curl -sS{' -N' if json.loads(s.code).get('stream') else ''} \"{EDGE}{spec.post_path}\" "
                       "-H \"Authorization: Bearer $TECHSARA_API_KEY\" -H \"Content-Type: application/json\" "
                       f"--data-binary @- <<'__JSON__'\n{s.code}\n__JSON__\n")
            changes = [f"POSTed verbatim as the body of {EDGE}{spec.post_path}"]
            run = run_bash(program, ctx)
        else:
            program, changes = assemble(s, spec, by_key, ctx)
            if s.kind == "bash" or s.kind == "other":
                run = run_bash(program, ctx)
            elif s.kind == "python":
                missing = missing_python_imports(program)
                if missing:
                    results.append(Result(s, NOT_RUN, f"imports a package that is not installed: {', '.join(missing)}", changes))
                    print(f"  {NOT_RUN:15} {label}")
                    continue
                run = run_python(program, ctx)
            elif s.kind == "javascript":
                run = run_js(program, ctx)
            else:
                results.append(Result(s, NOT_RUN, f"no runner for {s.lang}", changes))
                continue
        ctx["runs"][(s.slug, s.n)] = run
        ok, ev = spec.judge(run, ctx) if spec.judge else (run.exit == 0, f"exit {run.exit}")
        ev = f"{ev} ({run.secs:.1f}s)"
        if spec.reason and spec.action == "run":
            ev = f"{spec.reason}: {ev}"
        results.append(Result(s, PASSED if ok else FAILED, scrub(ev, ctx), changes, secs=run.secs))
        print(f"  {results[-1].verdict:15} {label} — {scrub(ev, ctx)[:160]}")
    return results


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").replace("`", "'")


def write_evidence(results: List[Result], ctx: Dict, started: dt.datetime, finished: dt.datetime,
                   path: Path = EVIDENCE) -> None:
    counts = {v: sum(1 for r in results if r.verdict == v) for v in (PASSED, FAILED, NOT_RUN, NOT_EXECUTABLE)}
    mismatches = [r for r in results if r.mismatch]
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    # Read-only: the stack is built from the WORKING TREE, so a commit id alone
    # would claim the run proves a tree nobody committed.
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True).stdout.strip())
    L = [
        "# Documentation examples — executed against the isolated stack",
        "",
        "Generated by `scripts/docs_examples_run.py`. Do not edit by hand: re-run the script.",
        "",
        f"* **Run:** {started:%Y-%m-%d %H:%M:%S} → {finished:%H:%M:%S} UTC",
        f"* **Tree:** branch `{branch}` at `{commit}`" + (" plus uncommitted changes in the working tree" if dirty else "") + " (the worktree the isolated stack was built from)",
        f"* **Stack:** the isolated e2e stack — public edge `{EDGE}` (frontend), orchestrator `{ORCH}`. Never production.",
        f"* **Identity:** signed in as `{EMAIL}`; project `{ctx.get('project_id')}` created for this run with one "
        "`tsk_test_` key (models.read, responses.read, responses.write, usage.read) and one models.read-only key. "
        "Neither key is printed anywhere.",
        f"* **Samples found:** {len(results)} fenced blocks across {len({r.sample.slug for r in results})} pages "
        "(loaded as rendered: the page records bundled by esbuild and evaluated in node).",
        f"* **Verdicts:** {counts[PASSED]} PASSED, {counts[FAILED]} FAILED, {counts[NOT_RUN]} NOT RUN, "
        f"{counts[NOT_EXECUTABLE]} NOT EXECUTABLE ({len(mismatches)} of which disagree with the real API).",
        "",
        "## How a sample is run",
        "",
        "* **bash** — `bash -c`, with `curl` wrapped as a shell function that appends `-w '\\n@@HTTP %{http_code}@@\\n'`, "
        "so every curl call's status is read from the wire.",
        f"* **python** — `{PY}`; `httpx.Client.send` is wrapped to print each status to stderr. A sample importing a "
        "package that is not installed is NOT RUN, never passed.",
        "* **typescript** — types stripped by the frontend's esbuild (`--loader=ts --format=esm`), run by node "
        f"{subprocess.run(['node', '--version'], capture_output=True, text=True).stdout.strip()}; `fetch` is wrapped to print each status to stderr.",
        "* **Changes to a sample** are listed per sample below. The documented base URL becomes the isolated edge; "
        "a placeholder key in bash becomes `$TECHSARA_API_KEY`; the example response id becomes a real one; a "
        "fragment gets the earlier blocks it calls prepended and, if it only defines a function, a driver that calls it.",
        "* **NOT EXECUTABLE** blocks are still checked where the real API can check them: every documented JSON "
        "shape is compared field-by-field (and `object`/`type`/`code`/`param` values) with a real object of the same "
        "kind captured in this run, headers and event orders with a real stream, the webhook verifiers with the "
        "server's own signer.",
        "",
        "## Results",
        "",
        "| # | Page | Language | First line | Verdict | Evidence |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(results, 1):
        v = r.verdict + (" (MISMATCH)" if r.mismatch else "")
        L.append(f"| {i} | {r.sample.title} (`{r.sample.slug}` #{r.sample.n}) | {r.sample.lang or '—'} | "
                 f"`{_cell(r.sample.first[:70])}` | **{v}** | {_cell(r.evidence)} |")
    L += ["", "## What the harness changed, per executed sample", ""]
    for i, r in enumerate(results, 1):
        if r.changes:
            L.append(f"* **#{i} {r.sample.slug} #{r.sample.n}** — " + "; ".join(_cell(c) for c in r.changes))
    failed = [r for r in results if r.verdict == FAILED]
    if failed or mismatches:
        L += ["", "## Output of every failure", ""]
        for r in failed + [m for m in mismatches if m not in failed]:
            run = ctx["runs"].get((r.sample.slug, r.sample.n))
            L.append(f"### {r.sample.slug} #{r.sample.n}")
            L.append("")
            L.append(_cell(r.evidence))
            if run:
                L += ["", "~~~text", scrub(MARK.sub("[HTTP \\1]", run.stdout)[-1500:], ctx),
                      "--- stderr ---", scrub(MARK.sub("[HTTP \\1]", run.stderr)[-1500:], ctx), "~~~"]
            L.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n")


# ================================================================ self-test ==


def self_test() -> int:
    fails = 0

    def t(name: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        fails += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))

    md = "\n".join([
        "intro", "~~~bash", "curl x \\", "  -H y", "~~~", "",
        "```python title=demo.py", "print('a')", "~~~", "still python", "```", "",
        "   ~~~~json", "{}", "~~~", "~~~~", "",
        "~~~", "no language", "~~~", "", "``` not`a`fence", "text", "",
        "~~~text", "unterminated",
    ])
    f = extract_fences(md)
    t("a tilde fence is extracted with its language and body",
      f[0].lang == "bash" and f[0].code == "curl x \\\n  -H y", repr(f[0]))
    t("a backtick fence takes the first word of the info string and is not closed by tildes",
      f[1].lang == "python" and f[1].code == "print('a')\n~~~\nstill python", repr(f[1]))
    t("a closing fence must be at least as long as the opening one",
      f[2].lang == "json" and f[2].code == "{}\n~~~", repr(f[2]))
    t("a fence with no language has the empty language", f[3].lang == "" and f[3].code == "no language", repr(f[3]))
    t("a backtick info string containing a backtick is not a fence",
      len(f) == 5, f"{len(f)} fences: {[x.lang for x in f]}")
    t("an unclosed fence runs to the end of the document", f[-1].lang == "text" and f[-1].code == "unterminated")
    t("the opening fence's line number is recorded", [x.line for x in f[:2]] == [2, 7], str([x.line for x in f]))

    t("languages classify into the five kinds",
      [classify(x) for x in ("bash", "sh", "python", "typescript", "js", "json", "http", "text", "")] ==
      ["bash", "bash", "python", "javascript", "javascript", "json", "other", "other", "other"])

    sub = substitute(f'export TECHSARA_API_KEY="tsk_live_…"\ncurl {DOC_BASE}/responses/{EXAMPLE_RESPONSE_ID}',
                     "bash", {"response_id": "resp_real"})
    t("bash substitution swaps the base URL, the placeholder key and the example id",
      sub == f'export TECHSARA_API_KEY="$TECHSARA_API_KEY"\ncurl {EDGE}/responses/resp_real', sub)
    full = "tsk_live_0123456789abcdef_EXAMPLE_KEY_DO_NOT_USE_THIS_IS_NOT_A_SECRETEXAMPL"
    t("the full documented example key is a placeholder too", PLACEHOLDER_KEY.sub("K", f'"{full}"') == '"K"')
    t("python substitution leaves key-like text alone (python reads the environment)",
      substitute('x = "tsk_live_…"', "python", {}) == 'x = "tsk_live_…"')

    out = '{"a":1}\n@@HTTP 200@@\nHTTP/1.1 400 Bad\r\nx-request-id: r\r\n\r\n{"error":{"param":"top_p"}}\n@@HTTP 400@@\n'
    b = bash_bodies(out)
    t("curl output splits into (status, body) per call", [c for c, _ in b] == [200, 400], str(b))
    t("a -i body parses as JSON after its header block", body_json(b[1][1]) == {"error": {"param": "top_p"}})
    ev = sse_events('event: a\ndata: {"x":1}\n\n: ping\n\nevent: b\ndata: 2\n\ndata: [DONE]\n\n')
    t("SSE frames parse, comments drop, data-only frames keep an empty name",
      ev == [("a", '{"x":1}'), ("b", "2"), ("", "[DONE]")], str(ev))

    doc = {"object": "response", "output": [{"type": "message", "content": [{"text": "…"}]}], "usage": None}
    real = {"object": "response", "output": [{"type": "reasoning"}, {"type": "message", "content": [{"text": "hi"}]}],
            "usage": {"total_tokens": 3}, "id": "resp_x"}
    t("a documented shape that is a subset of the real object has no diff", shape_diff(doc, real) == [],
      str(shape_diff(doc, real)))
    t("a documented field the API does not send is reported",
      shape_diff({"object": "response", "choices": []}, real) == ["field choices"])
    t("a documented identifying value the API does not send is reported",
      shape_diff({"object": "chat.completion"}, real) == ['object="chat.completion" (real: "response")'],
      str(shape_diff({"object": "chat.completion"}, real)))

    t("Content-Type compares the media type, so a charset parameter is not a mismatch",
      header_matches("Content-Type", "text/event-stream", "text/event-stream; charset=utf-8"))
    t("Content-Type still catches a different media type",
      not header_matches("Content-Type", "text/event-stream", "application/json"))
    t("any other header must match exactly, and an absent header never matches",
      not header_matches("X-Accel-Buffering", "no", "yes") and not header_matches("WWW-Authenticate", "Bearer", None))
    tickets = re.findall(r'"(docs-run-\{RUN\}[^"]*)"', "".join(sp.tail for sp in build_specs().values()))
    t("no two harness drivers derive an Idempotency-Key from the same ticket id (the first run's python #7 409)",
      len(tickets) == len(set(tickets)) and len(tickets) >= 2, str(tickets))

    j = j_curl([200], obj="response", status="completed")
    good = Run(0, '{"object":"response","status":"completed","output":[{"content":[{"text":"hi"}]}]}\n@@HTTP 200@@\n', "", 1)
    t("the curl judge passes a completed response with text", j(good, {})[0], j(good, {})[1])
    t("the curl judge fails a 401 even though curl exits 0",
      not j(Run(0, '{"error":{}}\n@@HTTP 401@@\n', "", 1), {})[0])
    t("the curl judge fails a completed response with no output text",
      not j(Run(0, '{"object":"response","status":"completed","output":[]}\n@@HTTP 200@@\n', "", 1), {})[0])
    jh = j_curl([200], headers=("x-request-id",), absent_headers=("ratelimit", "ratelimit-policy"))
    bare = Run(0, 'HTTP/1.1 200 OK\r\nx-request-id: r\r\n\r\n{"object":"list"}\n@@HTTP 200@@\n', "", 1)
    limited = Run(0, 'HTTP/1.1 200 OK\r\nx-request-id: r\r\nratelimit: "requests";r=41;t=23\r\n'
                     'ratelimit-policy: "requests";q=60;w=60\r\n\r\n{"object":"list"}\n@@HTTP 200@@\n', "", 1)
    t("the curl headers judge passes a response with X-Request-Id and no RateLimit header (limits off)",
      jh(bare, {})[0], jh(bare, {})[1])
    t("the curl headers judge fails a response that still advertises a RateLimit header",
      not jh(limited, {})[0], jh(limited, {})[1])
    t("the curl headers judge still fails a response missing a documented header",
      not jh(Run(0, 'HTTP/1.1 200 OK\r\n\r\n{}\n@@HTTP 200@@\n', "", 1), {})[0])
    stream_ok = Run(0, 'event: response.created\ndata: {}\n\nevent: response.output_text.delta\ndata: {}\n\n'
                    'event: response.completed\ndata: {}\n\n\n@@HTTP 200@@\n', "", 1)
    t("the stream judge passes events with a delta and one terminal", j_curl_stream(stream_ok, {})[0])
    t("the stream judge fails a 200 that carried no events",
      not j_curl_stream(Run(0, '{"object":"response"}\n@@HTTP 200@@\n', "", 1), {})[0])
    jp = j_prog([200], stdout_nonempty=True)
    t("the program judge fails a non-zero exit", not jp(Run(1, "x", "@@HTTP 200@@", 1), {})[0])
    t("the program judge reads statuses from stderr", jp(Run(0, "answer", "@@HTTP 200@@\n", 1), {})[0])

    # Against the real documentation, if the frontend's toolchain is present.
    if ESBUILD.exists():
        pages = load_pages()
        samples = samples_of(pages)
        raw = count_raw_openers(PAGES_DIR)
        t("the rendered bodies and the TypeScript sources agree on the number of fences",
          len(samples) == raw and raw > 0, f"rendered {len(samples)}, source {raw}")
        t("no rendered sample still carries an unresolved ${…} interpolation",
          not [s for s in samples if re.search(r"\$\{(API_BASE_URL|MODEL_ID|EXAMPLE_[A-Z_]+)\}", s.code)])
        specs = build_specs()
        unspecced = [f"{s.slug}#{s.n}" for s in samples if (s.slug, s.n) not in specs]
        t("every documented block has an execution spec", not unspecced, ", ".join(unspecced))
        stale = [f"{s.slug}#{s.n}" for s in samples if (s.slug, s.n) in specs and not s.first.startswith(specs[s.slug, s.n].first)]
        t("every spec still matches its block's first line", not stale, ", ".join(stale))
        extra = [f"{k[0]}#{k[1]}" for k in specs if k not in {(s.slug, s.n) for s in samples}]
        t("no spec names a block that no longer exists", not extra, ", ".join(extra))
    else:
        print("  SKIP the real-documentation checks — frontend/node_modules/.bin/esbuild is absent")
    print(f"self-test: {'all passed' if not fails else f'{fails} failed'}")
    return 1 if fails else 0


# ===================================================================== main ==


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true", help="unit-check the extractor and judges; no network")
    ap.add_argument("--only", help="comma-separated page slugs to run; needs --evidence, so a partial run can "
                    "never overwrite the full run's evidence file")
    ap.add_argument("--evidence", type=Path, default=None, help=f"where to write the table (default {EVIDENCE.relative_to(REPO)})")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.only and args.evidence is None:
        raise SystemExit("--only needs --evidence <path>: a partial run must not overwrite the full evidence file")
    evidence = args.evidence or EVIDENCE

    started = dt.datetime.now(dt.timezone.utc)
    pages = load_pages()
    samples = samples_of(pages)
    raw = count_raw_openers(PAGES_DIR)
    if len(samples) != raw:
        raise SystemExit(f"extractor disagreement: rendered {len(samples)} fences, source has {raw}")
    every = samples
    if args.only:
        wanted = set(args.only.split(","))
        samples = [s for s in samples if s.slug in wanted]
    print(f"{len(samples)} samples from {len(pages)} pages")
    ctx: Dict[str, Any] = {"run": started.strftime("%Y%m%d%H%M%S"), "refs": {}, "ids": {}, "runs": {}}
    provision(ctx)
    capture_refs(ctx)
    results = execute(samples, ctx, every)
    # The usage shape is compared at the END, once this run has produced usage.
    _ref(ctx, "usage", "GET", "/usage")
    for r in results:
        if (r.sample.slug, r.sample.n) == ("usage", 3):
            diff = shape_diff(json.loads(r.sample.code), ctx["refs"]["usage"]["body"])
            r.mismatch = bool(diff)
            r.evidence = ("a documented response shape — cross-check: " +
                          (f"every documented field occurs on the real /usage (HTTP {ctx['refs']['usage']['status']}, "
                           f"{len(ctx['refs']['usage']['body'].get('data', []))} day rows)" if not diff else "MISMATCH " + "; ".join(diff)))
    finished = dt.datetime.now(dt.timezone.utc)
    write_evidence(results, ctx, started, finished, evidence)
    counts = {v: sum(1 for r in results if r.verdict == v) for v in (PASSED, FAILED, NOT_RUN, NOT_EXECUTABLE)}
    mism = sum(1 for r in results if r.mismatch)
    print(f"\n{counts[PASSED]} PASSED, {counts[FAILED]} FAILED, {counts[NOT_RUN]} NOT RUN, "
          f"{counts[NOT_EXECUTABLE]} NOT EXECUTABLE ({mism} mismatched) — evidence: {evidence}")
    return 1 if counts[FAILED] or mism else 0


if __name__ == "__main__":
    sys.exit(main())
