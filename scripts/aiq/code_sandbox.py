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

Isolation, because this runs code a language model wrote on the same machines
as the production model. Measured 2026-09-18: `unshare -rn true` fails on BOTH
Sparks with "write failed /proc/self/uid_map: Operation not permitted"
(apparmor_restrict_unprivileged_userns=1), so the namespace prefix this module
used to rely on was always empty and NOTHING was isolated — model-written code
reached the head's vLLM port, got "HTTP/1.1 200 OK" out of /var/run/docker.sock
(the QA user is in the `docker` group), read the repository and wrote outside
its working directory. Every step still reported ok=True.

So each step now runs in a throwaway container (`CONTAINER` backend):

  * `--network none`                 no network of any kind,
  * no docker socket bind, `--cap-drop ALL`, `--security-opt no-new-privileges`
                                     no reach to the daemon or to privilege,
  * `--read-only` root with only the turn's working directory bound rw and a
    small `--tmpfs /tmp`
                                     nothing of the host filesystem is visible:
                                     not the repository, not $HOME, not /var/run,
  * `--memory` / `--cpus` / `--pids-limit` / `--ulimit cpu,fsize`
                                     a runaway or a fork bomb dies inside its cap,
  * a wall-clock timeout that removes the container and kills the client's
    process group,
  * and it still refuses to run as root.

When docker cannot give that (no docker, or no sandbox image on the host) every
language reports UNAVAILABLE and no model-written code is executed at all. The
`HOST` backend — ulimits plus `unshare -Un`, which does isolate the network but
cannot take away the docker socket or the filesystem — runs only when
AIQ_ALLOW_UNISOLATED_CODE=1 is set deliberately, and says so in every record.

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
import threading
import uuid
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

# ------------------------------------------------------- container settings --
#: A cap has to be small enough to stop a runaway and large enough for an
#: honest answer. Measured on the coding cases: the whole set builds and checks
#: inside 1 GiB, and `tsc` is the only step that needs more than 256 MiB.
SANDBOX_MEMORY = os.environ.get("AIQ_SANDBOX_MEMORY", "1g")
SANDBOX_CPUS = os.environ.get("AIQ_SANDBOX_CPUS", "1.0")
#: --pids-limit is per container, unlike `ulimit -u`, which counts every task
#: this UID already owns and therefore competes with the session running the QA.
SANDBOX_PIDS = int(os.environ.get("AIQ_SANDBOX_PIDS", "128"))
SANDBOX_TMPFS = os.environ.get("AIQ_SANDBOX_TMPFS", "64m")
SANDBOX_FSIZE = int(os.environ.get("AIQ_SANDBOX_FSIZE", str(64 * 1024 * 1024)))
#: Images are never pulled: a QA harness must not reach the network by itself.
#: The first one already on the host wins.
PYTHON_IMAGES = [s for s in (os.environ.get("AIQ_SANDBOX_PYTHON_IMAGE")
                             or "python:3.12-slim,python:3.11-slim").split(",") if s.strip()]
NODE_IMAGES = [s for s in (os.environ.get("AIQ_SANDBOX_NODE_IMAGE")
                           or "node:20-alpine,node:22-alpine,node:20-bookworm-slim").split(",") if s.strip()]
#: where the repository's own typescript is bound, read-only, for the tsc step
TOOLS = "/tools"
WORK = "/work"
CONTAINER, HOST = "container", "host"

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
    """Interpreters and compilers, and the sandbox that holds them."""

    def __init__(self, root: str, repo: Optional[str] = None):
        self.root = os.path.abspath(root)
        self.repo = repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        os.makedirs(self.root, exist_ok=True)
        self.docker = shutil.which("docker")
        self.bash = shutil.which("bash") or "/bin/bash"
        self.node = shutil.which("node")
        self.tsc = self._find_tsc()
        self.typescript_lib = self._find_typescript_lib()
        self._venv_python: Optional[str] = None
        self._venv_lock = threading.Lock()
        self._unshare: Optional[List[str]] = None
        self._images: Dict[str, Optional[str]] = {}
        self._backend: Optional[str] = None

    def _find_tsc(self) -> Optional[str]:
        env = os.environ.get("AIQ_TSC")
        if env and os.path.exists(env):
            return env
        local = os.path.join(self.repo, "frontend", "node_modules", ".bin", "tsc")
        return local if os.path.exists(local) else shutil.which("tsc")

    def _find_typescript_lib(self) -> Optional[str]:
        """The repository's own `typescript` package, bound into the node
        container read-only: tsc is plain JavaScript, so the node image needs
        nothing installed into it and the sandbox stays offline."""
        env = os.environ.get("AIQ_TYPESCRIPT_LIB")
        if env and os.path.exists(os.path.join(env, "bin", "tsc")):
            return os.path.abspath(env)
        local = os.path.join(self.repo, "frontend", "node_modules", "typescript")
        return local if os.path.exists(os.path.join(local, "bin", "tsc")) else None

    @property
    def venv_python(self) -> str:
        """A scratch virtualenv for the HOST backend, created on first use.

        Locked: run.py runs the cases two at a time, and without the lock a
        second thread sees venv/bin/python exist while `python -m venv` is
        still populating venv/lib and returns a half-built interpreter.
        """
        if self._venv_python:
            return self._venv_python
        with self._venv_lock:
            if self._venv_python:
                return self._venv_python
            py = os.path.join(self.root, "venv", "bin", "python")
            if not os.path.exists(py):
                subprocess.run([sys.executable, "-m", "venv", "--without-pip", os.path.join(self.root, "venv")],
                               check=True, capture_output=True, timeout=180)
            self._venv_python = py
            return py

    # --------------------------------------------------------- the sandbox --

    def image(self, key: str) -> Optional[str]:
        """The first candidate image already on this host, or None.

        Never pulls: a QA harness that reaches a registry on its own is a
        network call nobody asked for, and the answer must be reproducible.
        """
        if key in self._images:
            return self._images[key]
        found = None
        if self.docker:
            for name in (PYTHON_IMAGES if key == "python" else NODE_IMAGES):
                name = name.strip()
                if not name:
                    continue
                try:
                    p = subprocess.run([self.docker, "image", "inspect", name],
                                       capture_output=True, timeout=60)
                except (OSError, subprocess.SubprocessError):
                    break
                if p.returncode == 0:
                    found = name
                    break
        self._images[key] = found
        return found

    @property
    def backend(self) -> str:
        """CONTAINER when docker really gives the isolation, else HOST."""
        if self._backend is not None:
            return self._backend
        forced = os.environ.get("AIQ_SANDBOX_BACKEND")
        if forced in (CONTAINER, HOST):
            self._backend = forced
            return self._backend
        self._backend = CONTAINER if self._container_works() else HOST
        return self._backend

    def _container_works(self) -> bool:
        image = self.image("python")
        if not (self.docker and image):
            return False
        argv = [self.docker, "run", "--rm", *_CONTAINER_GUARDS, "--memory", SANDBOX_MEMORY,
                "--entrypoint", "true", image]
        try:
            return subprocess.run(argv, capture_output=True, timeout=120).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @property
    def net_prefix(self) -> List[str]:
        """`unshare -Un` when this user may unshare a network namespace.

        `-r` (map root) is what fails on both Sparks, and it is not needed: the
        unmapped process keeps its real kuid for filesystem checks, so it still
        reads and writes its own working directory while having no network.
        Used only by the HOST backend, which is off unless explicitly allowed.
        """
        if self._unshare is None:
            self._unshare = []
            for argv in (["unshare", "-Un", "true"], ["unshare", "-rn", "true"]):
                try:
                    ok = subprocess.run(argv, capture_output=True, timeout=20).returncode == 0
                except (OSError, subprocess.SubprocessError):
                    ok = False
                if ok:
                    self._unshare = argv[:-1]
                    break
        return list(self._unshare)

    def isolation(self) -> Dict[str, object]:
        """What the sandbox actually gives, recorded with every coding turn."""
        if self.backend == CONTAINER:
            return {"backend": CONTAINER, "image": self.image("python"), "node_image": self.image("node"),
                    "isolated_network": True, "isolated_docker": True, "read_only_root": True,
                    "memory": SANDBOX_MEMORY, "cpus": SANDBOX_CPUS, "pids": SANDBOX_PIDS}
        return {"backend": HOST, "image": None, "node_image": None,
                "isolated_network": bool(self.net_prefix), "isolated_docker": False, "read_only_root": False,
                "memory": f"{LIMITS[1]}KiB address space", "cpus": f"{LIMITS[0]}s CPU", "pids": _nproc_cap()}

    def available(self, lang: str) -> Tuple[bool, str]:
        """(may this language run, why not). Unavailable is a refusal to
        execute, not a warning: run_code returns before writing any source."""
        if self.backend == CONTAINER:
            if lang == "typescript":
                if not self.image("node"):
                    return False, f"no sandbox node image on this host (tried {', '.join(NODE_IMAGES)})"
                if not self.typescript_lib:
                    return False, "no typescript package (set AIQ_TYPESCRIPT_LIB, or keep frontend/node_modules)"
            return True, ""
        if os.environ.get("AIQ_ALLOW_UNISOLATED_CODE") != "1":
            why = ("the code sandbox cannot isolate this host: "
                   + ("docker is not on PATH" if not self.docker else
                      f"no sandbox image (tried {', '.join(PYTHON_IMAGES)})" if not self.image("python") else
                      "docker refused the sandbox container")
                   + ". Model-written code is NOT run; set AIQ_ALLOW_UNISOLATED_CODE=1 to run it on the host anyway")
            return False, why
        if lang == "typescript":
            if not self.node:
                return False, "node is not installed"
            if not self.tsc:
                return False, "no tsc (set AIQ_TSC, or keep frontend/node_modules in the repo)"
        return True, ""


# ============================================================== execution ==

#: Every one of these is load-bearing; see the module docstring for what each
#: hostile snippet did without them.
_CONTAINER_GUARDS = [
    "--network", "none",                       # no network of any kind
    "--cap-drop", "ALL",                       # no capability, so no raw socket, no mount
    "--security-opt", "no-new-privileges",     # a setuid binary cannot buy any back
    "--read-only",                             # the host filesystem is not there at all
]


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


#: the logical programs a step may ask for, and where each one lives
PROGRAMS = ("python", "bash", "node", "tsc")


def _resolve(tc: "Toolchain", prog: str) -> Tuple[str, List[str]]:
    """(which image, the argv prefix) for a logical program."""
    if prog not in PROGRAMS:
        raise ValueError(f"unknown sandbox program {prog!r}")
    if tc.backend == CONTAINER:
        if prog == "python":
            return "python", ["python3"]
        if prog == "bash":
            return "python", ["bash"]
        if prog == "node":
            return "node", ["node"]
        return "node", ["node", f"{TOOLS}/typescript/bin/tsc"]
    if prog == "python":
        return "host", [tc.venv_python]
    if prog == "bash":
        return "host", [tc.bash]
    if prog == "node":
        return "host", [str(tc.node)]
    return "host", [str(tc.tsc)]


#: what the docker CLIENT needs to find the daemon — never given to the code
#: inside the container, which gets _env(WORK) instead
CLIENT_KEYS = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
               "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR")


def _client_env(workdir: str) -> Dict[str, str]:
    env = {k: os.environ[k] for k in CLIENT_KEYS if os.environ.get(k)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("HOME", workdir)
    return env


def _docker_argv(tc: "Toolchain", image_key: str, workdir: str, name: str, argv: List[str]) -> List[str]:
    image = tc.image(image_key)
    # the label is the sweep: `docker rm -f $(docker ps -q --filter label=aiq-sandbox)`
    # finds anything a killed harness left behind, and matches nothing else
    out = [str(tc.docker), "run", "--rm", "--name", name, "--label", "aiq-sandbox", *_CONTAINER_GUARDS,
           "--tmpfs", f"/tmp:rw,nosuid,nodev,size={SANDBOX_TMPFS}",
           "--memory", SANDBOX_MEMORY, "--memory-swap", SANDBOX_MEMORY,
           "--cpus", SANDBOX_CPUS, "--pids-limit", str(SANDBOX_PIDS),
           "--ulimit", f"cpu={LIMITS[0]}:{LIMITS[0]}", "--ulimit", f"fsize={SANDBOX_FSIZE}:{SANDBOX_FSIZE}",
           "--user", f"{os.getuid()}:{os.getgid()}",
           # the ONLY writable path that outlives the container, and the only
           # thing of this machine the code can see
           "--volume", f"{os.path.abspath(workdir)}:{WORK}", "--workdir", WORK]
    if image_key == "node" and tc.typescript_lib:
        out += ["--volume", f"{tc.typescript_lib}:{TOOLS}/typescript:ro"]
    for k, v in _env(WORK).items():
        out += ["--env", f"{k}={v}"]
    return out + ["--entrypoint", argv[0], str(image), *argv[1:]]


def run_sandboxed(tc: Toolchain, prog: str, args: List[str], workdir: str, timeout: float,
                  address_space: bool = True) -> dict:
    """One command inside the sandbox, killed with its container and its group.

    `address_space=False` drops `ulimit -v` on the HOST backend: V8 reserves a
    large virtual region at startup, so node and tsc cannot run under it. It
    means nothing to the CONTAINER backend, whose `--memory` cap is enforced by
    the kernel's memory controller and is not a virtual-size limit.
    """
    if os.geteuid() == 0:
        raise SystemExit("refusing to run model-written code as root")
    image_key, prefix = _resolve(tc, prog)
    argv = prefix + args
    name = ""
    if tc.backend == CONTAINER:
        name = f"aiq-sandbox-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        wrapped = _docker_argv(tc, image_key, workdir, name, argv)
        env = _client_env(workdir)
    else:
        cpu, mem_kb, file_kb = LIMITS
        vlimit = f"-v {mem_kb} " if address_space else ""
        prologue = f"ulimit -t {cpu} {vlimit}-f {file_kb} -u {_nproc_cap()} 2>/dev/null; exec \"$@\""
        wrapped = tc.net_prefix + [tc.bash, "-c", prologue, "sandbox", *argv]
        env = _env(workdir)
    started = time.perf_counter()
    try:
        p = subprocess.Popen(wrapped, cwd=workdir, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return {"rc": -1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "seconds": 0.0, "timed_out": False}
    try:
        out, err = p.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        if name:
            # the client is not the process that is running the code: remove
            # the container first or `docker run` keeps waiting on it
            subprocess.run([str(tc.docker), "rm", "-f", name], capture_output=True, timeout=60)
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        timed_out = True
    return {"rc": p.returncode, "stdout": out.decode("utf-8", "replace")[-8000:],
            "stderr": err.decode("utf-8", "replace")[-8000:], "seconds": round(time.perf_counter() - started, 2),
            "timed_out": timed_out}


def _step(tc: Toolchain, stage: str, name: str, prog: str, args: List[str], workdir: str, timeout: float,
          address_space: bool = True) -> dict:
    r = run_sandboxed(tc, prog, args, workdir, timeout, address_space=address_space)
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
                 "toolchain": {"tsc": tc.tsc, "node": tc.node, **tc.isolation()}}
    ok, why = tc.available(lang)
    if not ok:
        rec["unavailable"] = why
        return rec
    if rec["toolchain"]["backend"] == HOST:
        # AIQ_ALLOW_UNISOLATED_CODE=1 was set: the record has to carry that,
        # because "the code ran" and "the code was contained" stop being the
        # same statement the moment this line is reached.
        rec["warning"] = ("model-written code ran on the host: "
                          f"network isolated={bool(tc.net_prefix)}, docker socket reachable, "
                          "host filesystem readable and writable outside the working directory")
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
        steps.append(_step(tc, "build", "py_compile", "python", ["-m", "py_compile", "solution.py"], workdir, timeout))
        if steps[-1]["ok"]:
            steps.append(_step(tc, "check", "check.py", "python", ["check.py"], workdir, timeout))
    elif lang == "sql":
        steps.append(_step(tc, "build", "prepare", "python", ["_build.py"], workdir, timeout))
        if steps[-1]["ok"]:
            steps.append(_step(tc, "check", "check.py", "python", ["check.py"], workdir, timeout))
    elif lang == "typescript":
        steps.append(_step(tc, "build", "tsc", "tsc", ["--strict", "--target", "es2020", "--module", "commonjs",
                                                       "--moduleResolution", "node", "--lib", "es2020,dom",
                                                       "--outDir", "out", "--skipLibCheck", "solution.ts", "check.ts"],
                           workdir, max(timeout, 120.0), address_space=False))
        if steps[-1]["ok"]:
            steps.append(_step(tc, "check", "node", "node", ["out/check.js"], workdir, timeout, address_space=False))
    elif lang == "bash":
        steps.append(_step(tc, "build", "bash -n", "bash", ["-n", "solution.sh"], workdir, timeout))
        if steps[-1]["ok"]:
            steps.append(_step(tc, "check", "check.sh", "bash", ["check.sh"], workdir, timeout))
    else:
        raise ValueError(f"unknown coding language {lang!r}")
    rec["steps"] = steps
    rec["ok"] = bool(steps) and all(s["ok"] for s in steps)
    return rec
