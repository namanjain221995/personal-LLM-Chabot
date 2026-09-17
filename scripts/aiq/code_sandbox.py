"""Run the code an answer contains, and say whether it actually works.

The coding cases (coding_cases.py) ask for Python, SQL, TypeScript or bash.
Grading them by reading the text would measure the reader, so this module
EXECUTES what the model wrote:

  * the fenced block of the case's language is lifted out of the answer,
  * it is written into a fresh working directory next to the case's own
    checker (a program the harness wrote, never the model),
  * a BUILD step compiles / type-checks / parses it,
  * a CHECK step runs the checker, which asserts behaviour including the
    edge cases the ask named.

Isolation, because this runs code a language model wrote:

  * a scratch virtualenv created with `--without-pip` (no index, no
    installs) — the Python cases are told to use the standard library only;
  * a private working directory per turn, removed with the run;
  * `ulimit` on CPU seconds, address space, file size and process count,
    plus a wall-clock timeout that kills the process group;
  * no network namespace when `unshare -rn` is available unprivileged, and
    a stripped environment (no proxy, no credentials, HOME inside the
    working directory) in every case;
  * it refuses to run as root.

Nothing here touches the orchestrator, the repository or the network.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

#: fence tags that count as each language
FENCE_TAGS = {
    "python": ("python", "python3", "py"),
    "sql": ("sql", "sqlite", "postgresql", "psql"),
    "typescript": ("typescript", "ts", "tsx"),
    "bash": ("bash", "sh", "shell", "shell-script", "zsh", "console"),
}
SOURCE_NAME = {"python": "solution.py", "sql": "solution.sql", "typescript": "solution.ts", "bash": "solution.sh"}
DEFAULT_TIMEOUT = 60.0
#: ulimit -t (CPU seconds), -v (KiB of address space), -f (KiB per file)
LIMITS = (30, 2_000_000, 65_536)
#: RLIMIT_NPROC counts every task this UID already has, so the cap has to be
#: the current count plus headroom: a fixed small number would refuse to fork
#: at all, and a fixed large one would not stop a runaway.
NPROC_HEADROOM = 1024

_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)(?:```|~~~)[ \t]*$", re.S | re.M)


# ============================================================= extraction ==

def fenced_blocks(answer: str) -> List[Tuple[str, str]]:
    """[(tag, body)] for every fenced block, in order."""
    return [(m.group(1).lower(), m.group(2)) for m in _FENCE_RE.finditer(answer or "")]


def extract_code(answer: str, lang: str) -> Tuple[str, int]:
    """(source, how many blocks of this language). The longest block wins.

    A good answer puts the program in one block. When a model splits it, the
    longest block is still the program and the others are usage examples or
    sample output, which must not be pasted into the file.
    """
    tags = FENCE_TAGS[lang]
    blocks = fenced_blocks(answer)
    mine = [b for t, b in blocks if t in tags]
    if not mine:  # an untagged fence is still a code block
        mine = [b for t, b in blocks if not t]
    if not mine:
        return "", 0
    return max(mine, key=len), len(mine)


# ============================================================== toolchain ==

class Toolchain:
    """Interpreters and compilers, resolved once per run."""

    def __init__(self, root: str, repo: Optional[str] = None):
        self.root = os.path.abspath(root)
        self.repo = repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        os.makedirs(self.root, exist_ok=True)
        self.bash = shutil.which("bash") or "/bin/bash"
        self.node = shutil.which("node")
        self.tsc = self._find_tsc()
        self._venv_python: Optional[str] = None
        self._unshare: Optional[List[str]] = None

    def _find_tsc(self) -> Optional[str]:
        env = os.environ.get("AIQ_TSC")
        if env and os.path.exists(env):
            return env
        local = os.path.join(self.repo, "frontend", "node_modules", ".bin", "tsc")
        return local if os.path.exists(local) else shutil.which("tsc")

    @property
    def venv_python(self) -> str:
        """A scratch virtualenv, created on first use and reused after that."""
        if self._venv_python:
            return self._venv_python
        py = os.path.join(self.root, "venv", "bin", "python")
        if not os.path.exists(py):
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", os.path.join(self.root, "venv")],
                           check=True, capture_output=True, timeout=180)
        self._venv_python = py
        return py

    @property
    def net_prefix(self) -> List[str]:
        """`unshare -rn` when this user may create namespaces, else nothing."""
        if self._unshare is None:
            argv = ["unshare", "-rn", "true"]
            try:
                ok = subprocess.run(argv, capture_output=True, timeout=20).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            self._unshare = ["unshare", "-rn"] if ok else []
        return list(self._unshare)

    def available(self, lang: str) -> Tuple[bool, str]:
        if lang == "typescript":
            if not self.node:
                return False, "node is not installed"
            if not self.tsc:
                return False, "no tsc (set AIQ_TSC, or keep frontend/node_modules in the repo)"
        return True, ""


# ============================================================== execution ==

def _nproc_cap() -> int:
    """Tasks already running, plus headroom: see NPROC_HEADROOM."""
    try:
        running = sum(1 for name in os.listdir("/proc") if name.isdigit())
    except OSError:
        running = 0
    return running + NPROC_HEADROOM


def _env(workdir: str) -> Dict[str, str]:
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": workdir,
            "TMPDIR": workdir, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0", "NO_COLOR": "1", "TERM": "dumb"}


def run_sandboxed(tc: Toolchain, argv: List[str], workdir: str, timeout: float,
                  address_space: bool = True) -> dict:
    """One command under ulimits, without a network, killed with its group.

    `address_space=False` drops `ulimit -v`: V8 reserves a large virtual
    region at startup, so node and tsc cannot run under it. The CPU-second,
    file-size and process limits and the wall-clock kill still apply.
    """
    if os.geteuid() == 0:
        raise SystemExit("refusing to run model-written code as root")
    cpu, mem_kb, file_kb = LIMITS
    vlimit = f"-v {mem_kb} " if address_space else ""
    prologue = f"ulimit -t {cpu} {vlimit}-f {file_kb} -u {_nproc_cap()} 2>/dev/null; exec \"$@\""
    wrapped = tc.net_prefix + [tc.bash, "-c", prologue, "sandbox", *argv]
    started = time.perf_counter()
    try:
        p = subprocess.Popen(wrapped, cwd=workdir, env=_env(workdir), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return {"rc": -1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "seconds": 0.0, "timed_out": False}
    try:
        out, err = p.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        timed_out = True
    return {"rc": p.returncode, "stdout": out.decode("utf-8", "replace")[-8000:],
            "stderr": err.decode("utf-8", "replace")[-8000:], "seconds": round(time.perf_counter() - started, 2),
            "timed_out": timed_out}


def _step(tc: Toolchain, stage: str, name: str, argv: List[str], workdir: str, timeout: float,
          address_space: bool = True) -> dict:
    r = run_sandboxed(tc, argv, workdir, timeout, address_space=address_space)
    tail = (r["stderr"].strip() or r["stdout"].strip() or "").splitlines()
    return {"stage": stage, "name": name, "ok": r["rc"] == 0 and not r["timed_out"], "rc": r["rc"],
            "seconds": r["seconds"], "timed_out": r["timed_out"],
            "tail": ("timed out; " if r["timed_out"] else "") + " | ".join(tail[-6:])[:600],
            "stdout": r["stdout"][-2000:], "stderr": r["stderr"][-2000:]}


# ================================================================ the run ==

#: Shared by the SQL build step and every SQL checker: split a script the way
#: sqlite3 does, so a semicolon inside a string literal does not split it.
SQL_SPLIT = '''\
import sqlite3


def statements(sql):
    out, buf = [], ""
    for line in sql.splitlines(True):
        if not buf and not line.strip().split("--")[0].strip():
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s.strip().strip(";")]
'''

_SQL_BUILD = '''\
import sqlite3, sys
from sqlsplit import statements
con = sqlite3.connect(":memory:")
con.executescript(open("schema.sql").read())
stmts = statements(open("solution.sql").read())
if not stmts:
    sys.exit("the answer holds no SQL statement")
for s in stmts:           # prepare only: the syntax and every name must resolve
    con.execute("EXPLAIN " + s.rstrip(";"))
print(f"{len(stmts)} statement(s) prepared")
'''


def run_code(spec: dict, answer: str, workdir: str, tc: Toolchain) -> dict:
    """Extract, build and check. Returns the record check_turn scores."""
    lang = spec["lang"]
    os.makedirs(workdir, exist_ok=True)
    source, blocks = extract_code(answer, lang)
    rec: dict = {"lang": lang, "blocks": blocks, "source": source, "steps": [],
                 "toolchain": {"tsc": tc.tsc, "node": tc.node, "isolated_network": bool(tc.net_prefix)}}
    ok, why = tc.available(lang)
    if not ok:
        rec["unavailable"] = why
        return rec
    if not source.strip():
        return rec
    timeout = float(spec.get("timeout") or DEFAULT_TIMEOUT)
    with open(os.path.join(workdir, SOURCE_NAME[lang]), "w", encoding="utf-8") as fh:
        fh.write(source if source.endswith("\n") else source + "\n")
    for name, body in (spec.get("files") or {}).items():
        path = os.path.join(workdir, name)
        os.makedirs(os.path.dirname(path) or workdir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    if lang == "sql":
        for name, body in (("schema.sql", spec["schema"]), ("sqlsplit.py", SQL_SPLIT), ("_build.py", _SQL_BUILD)):
            with open(os.path.join(workdir, name), "w", encoding="utf-8") as fh:
                fh.write(body)

    steps: List[dict] = []
    if lang == "python":
        steps.append(_step(tc, "build", "py_compile", [tc.venv_python, "-m", "py_compile", "solution.py"], workdir, timeout))
        if steps[-1]["ok"]:
            steps.append(_step(tc, "check", "check.py", [tc.venv_python, "check.py"], workdir, timeout))
    elif lang == "sql":
        steps.append(_step(tc, "build", "prepare", [tc.venv_python, "_build.py"], workdir, timeout))
        if steps[-1]["ok"]:
            steps.append(_step(tc, "check", "check.py", [tc.venv_python, "check.py"], workdir, timeout))
    elif lang == "typescript":
        assert tc.tsc and tc.node
        steps.append(_step(tc, "build", "tsc", [tc.tsc, "--strict", "--target", "es2020", "--module", "commonjs",
                                                "--moduleResolution", "node", "--lib", "es2020,dom", "--outDir", "out", "--skipLibCheck",
                                                "solution.ts", "check.ts"], workdir, max(timeout, 120.0),
                           address_space=False))
        if steps[-1]["ok"]:
            steps.append(_step(tc, "check", "node", [tc.node, "out/check.js"], workdir, timeout, address_space=False))
    elif lang == "bash":
        steps.append(_step(tc, "build", "bash -n", [tc.bash, "-n", "solution.sh"], workdir, timeout))
        if steps[-1]["ok"]:
            steps.append(_step(tc, "check", "check.sh", [tc.bash, "check.sh"], workdir, timeout))
    else:
        raise ValueError(f"unknown coding language {lang!r}")
    rec["steps"] = steps
    rec["ok"] = bool(steps) and all(s["ok"] for s in steps)
    return rec
