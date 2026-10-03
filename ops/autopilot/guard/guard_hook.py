#!/usr/bin/python3 -I
"""Autopilot PreToolUse guard: layer 3 of the autopilot's guardrails.

Claude Code runs this before every Bash, Write, Edit, MultiEdit, NotebookEdit,
Read and Grep call in an autopilot session (see settings.autopilot.json). It
reads the hook payload on stdin. To block a call it prints the reason on stderr
and exits 2. To let the call continue to the permission rules and the
auto-mode classifier it exits 0.

It enforces the hard limits of docs/ai-platform-upgrade/MASTER_PROMPT.md §3.3
that a tool pattern can express, plus this host's production-isolation rules.
It is a backstop, not a sandbox: it reads shell text best-effort and cannot see
inside programs the agent runs. Layers 1 (permissions.deny) and 2 (the
auto-mode classifier) cover what it misses.

Fails closed: an internal error or an unparseable command blocks the call.
Stays fast (a hook that times out does not block): the only slow path is the
secret scan of outgoing commits on `git push`.
"""

import grp
import json
import os
import pwd
import re
import shlex
import signal
import subprocess
import sys
import time

HOME = pwd.getpwuid(os.getuid()).pw_dir
WORK = os.path.join(HOME, "work")
DEV_WORKTREE = os.path.join(WORK, "llm-dev")
AUTOPILOT_HOME = os.path.join(HOME, ".llm-autopilot")
DEV = "llmdev"  # prefix for every container, image, compose project and unit the autopilot owns
DEV_COMPOSE_DIR = os.path.join(DEV_WORKTREE, "ops/dev")
# The installed release gate: the only way the agent may move `dev`. The copy in
# the repository is its source; the agent cannot edit the installed one.
MERGE_TO_DEV = os.path.join(AUTOPILOT_HOME, "bin", "merge_to_dev.sh")
# Operator-approved tree hashes of .github/ that the gate may release to dev.
CI_APPROVALS = os.path.join(AUTOPILOT_HOME, "approved-ci-trees")
# The runner merges these test-database variables into every cycle's environment.
TEST_DB_VARS = os.path.join(AUTOPILOT_HOME, "agent", "test-db.vars")

# Host details (addresses, ports, names) live in a host-only file so the public
# repository carries none of them (operator decision 6, 2026-10-03). Template:
# ops/autopilot/host.example.json. Missing or unreadable -> every call blocks.
HOST_CONFIG_PATH = os.environ.get("AP_HOST_CONFIG") or os.path.join(AUTOPILOT_HOME, "host.json")
try:
    with open(HOST_CONFIG_PATH, encoding="utf-8") as _fh:
        HOST = json.load(_fh)
    HOST_ERROR = None
except Exception as _exc:  # evaluate() blocks everything
    HOST, HOST_ERROR = {}, f"{type(_exc).__name__}: {_exc}"


def _hp(path):
    return os.path.expanduser(path) if path.startswith("~") else path


PROD_CHECKOUT = _hp(HOST.get("prod_checkout") or os.path.join(HOME, "Documents/project/personal-LLM-Chabot"))
REPO_SLUG = HOST.get("repo_slug") or "namanjain221995/personal-LLM-Chabot"
PROD_STACK = HOST.get("prod_stack") or "sf-local-ai"
PROD_DB_CONTAINERS = set((HOST.get("prod_db") or {}).get("containers") or [f"{PROD_STACK}-postgres-1"])
PROD_DB_PORT = str((HOST.get("prod_db") or {}).get("port") or 5432)
SHARED_TEST_DB_PORTS = {str(p) for p in HOST.get("shared_test_db_ports") or []}
# Production HTTP control planes (frontend, orchestrator, monitoring, admin
# tools). Model ports take inference requests; load tools are refused everywhere.
PROD_CONTROL_PORTS = {str(p) for p in HOST.get("prod_control_ports") or []}
PROD_HOSTS = {h.lower() for h in (HOST.get("prod_hosts") or [])} | {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"}
TRUSTED_HOSTS = {h.lower() for h in (HOST.get("trusted_hosts") or [])}
TEST_DB = HOST.get("test_db") or {}
# Scripts that act on production wherever they are run from.
PROD_SCRIPTS = re.compile(
    r"(^|/)(deploy(-[\w-]+)?\.sh|deploy-rollback\.sh|deploy-preflight\.sh|cluster-[\w-]+\.sh|backup-knowledge\.sh|"
    r"host-guard[\w.-]*|service-reconcile\.sh|e2e-stack\.sh|aiq-stack\.sh|ocr[\w-]*\.sh|whisper[\w-]*\.sh|tunnel[\w-]*\.sh|"
    r"oom-[\w-]+\.sh|engine-[\w-]+\.sh)$"
)
# Harnesses that load the production engines: only inside the measured
# low-traffic window (MASTER_PROMPT §9.3; hours from the host config).
HEAVY_HARNESS = re.compile(r"(^|/)(validate_long_context\.py|api_soak\.py|cluster-ab\.py|evaluation_runner)(\.py)?$|evaluation\.runners\.evaluation_runner")
QUIET_WINDOW_IST = tuple(HOST.get("quiet_window_ist") or (5, 7))  # [start_hour, end_hour) Asia/Kolkata
LOAD_TOOLS = {"ab", "wrk", "wrk2", "hey", "oha", "vegeta", "k6", "locust", "siege", "bombardier", "h2load", "ghz"}
# Relations an agent may SELECT from in the production database. Catalog and
# statistics views hold no user content. Anything read here reaches an external
# model API, so add a table only after checking it holds no user content.
PROD_DB_TABLE_ALLOW = re.compile(r"^(pg_catalog\.)?pg_\w+$|^information_schema\.\w+$", re.I)
MIN_FREE_FRACTION = 0.15

# Latency budget. The installed hook has a 60 s timeout and a hook that TIMES OUT
# does not block (fail-open), so the guard sets its own wall-clock deadline well
# under 60 s and fails CLOSED when it is reached (main() turns the alarm into
# exit 2). The push-path secret scan gets a smaller budget below that.
GUARD_DEADLINE_S = 40
SCAN_DEADLINE_S = 25
# Pathological-size fences. No ordinary hook payload or command reaches these; a
# larger one fails closed rather than being parsed (a parser can be quadratic).
MAX_PAYLOAD_BYTES = 48 * 1024 * 1024
MAX_COMMAND_BYTES = 16 * 1024 * 1024

TAG = "[autopilot-guard]"


def _real(path):
    return os.path.realpath(path)


R_WORK = _real(WORK)
R_DEV_WORKTREE = _real(DEV_WORKTREE)
R_PROD = _real(PROD_CHECKOUT)
R_DOCUMENTS = _real(os.path.join(HOME, "Documents"))
R_DEV_COMPOSE = _real(DEV_COMPOSE_DIR)

# Where the agent may write. Everything else is read-only to it.
WRITE_ALLOW = [
    R_WORK,
    "/tmp",
    "/var/tmp",
    _real(os.path.join(AUTOPILOT_HOME, "agent")),
    _real(os.path.join(AUTOPILOT_HOME, "manifest.jsonl")),
    _real(os.path.join(HOME, ".claude/projects")),
    _real(os.path.join(HOME, ".claude/plans")),
    _real(os.path.join(HOME, ".claude/todos")),
    _real(os.path.join(HOME, ".cache")),
    _real(os.path.join(HOME, ".npm")),
    "/dev/null",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
]
WRITE_DENY_INSIDE_ALLOW = [
    _real(os.path.join(HOME, ".cache/huggingface")),
    _real(os.path.join(HOME, ".cache/torch")),
    _real(os.path.join(HOME, ".cache/vllm")),
]
BROAD_DELETE = {"/tmp", "/var/tmp", R_WORK, R_DEV_WORKTREE, _real(HOME), "/", _real(os.path.join(HOME, ".cache")), _real(os.path.join(HOME, ".claude/projects"))}
GUARD_FILES = [
    _real(os.path.join(AUTOPILOT_HOME, "guard")),
    _real(os.path.join(AUTOPILOT_HOME, "bin")),
    _real(os.path.join(AUTOPILOT_HOME, "settings.autopilot.json")),
    _real(os.path.join(AUTOPILOT_HOME, "MASTER_PROMPT.md")),
    _real(HOST_CONFIG_PATH),
    _real(os.path.join(AUTOPILOT_HOME, "state.json")),
    _real(os.path.join(AUTOPILOT_HOME, "lock")),
    _real(CI_APPROVALS),
    _real(TEST_DB_VARS),
]

SECRET_BASENAME = re.compile(
    r"""^(
        \.env(\.(?!example$|sample$|template$|dist$)[^/]+)? |
        [^/]+\.env |
        secrets?(\.(env|json|ya?ml|toml|txt|ini|conf|cfg))? |
        \.?credentials(\.json)? |
        \.netrc | \.pgpass | \.git-credentials | \.npmrc | \.pypirc |
        id_(rsa|dsa|ecdsa|ed25519)(\.pub)? |
        [^/]+\.(pem|key|p12|pfx|jks|keystore|kdbx) |
        Training_Module_Feature_Map_and_Memory\.txt
    )$""",
    re.X,
)
SECRET_DIRS = [
    _real(os.path.join(HOME, ".ssh")),
    _real(os.path.join(HOME, ".gnupg")),
    _real(os.path.join(HOME, ".config/gh")),
    _real(os.path.join(HOME, ".docker")),
    _real(os.path.join(HOME, ".claude/.credentials.json")),
    _real(os.path.join(AUTOPILOT_HOME, "secrets")),
    "/run/secrets",
    "/etc/shadow",
    "/etc/gshadow",
]
SECRET_PATH_PARTS = re.compile(r"(^|/)(\.runtime/secrets[^/]*|secrets/[^/]+)$")
SECRET_TOKEN_IN_TEXT = re.compile(
    r"""(?<![\w.-])(
        \.env(\.(?!example\b|sample\b|template\b|dist\b)[\w-]+)* |
        [\w-]+\.env |
        \.credentials\.json | \.git-credentials | \.netrc | \.pgpass |
        id_(rsa|dsa|ecdsa|ed25519) |
        secrets\.env |
        Training_Module_Feature_Map_and_Memory\.txt |
        \.config/gh/hosts\.yml
    )(?![\w.-])""",
    re.X,
)
SECRET_VAR = re.compile(r"\$\{?[A-Za-z_]*(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE|CREDENTIAL|COOKIE|AUTH)[A-Za-z_]*\}?", re.I)
# Shapes of real credentials, for the outgoing-commit and PR-body scans.
SECRET_SHAPES = re.compile(
    r"""(
        -----BEGIN\ [A-Z ]*PRIVATE\ KEY----- |
        \bsk-(ant-)?[A-Za-z0-9_-]{20,} |
        \bgh[pousr]_[A-Za-z0-9]{30,} |
        \bgithub_pat_[A-Za-z0-9_]{40,} |
        \bxox[abprs]-[A-Za-z0-9-]{10,} |
        \bAKIA[0-9A-Z]{16}\b |
        \beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,} |
        \b(postgres(ql)?|mysql|mongodb(\+srv)?|redis|amqp)://[^:\s/]+:(?!(postgres|password|passwd|secret|changeme|example|placeholder|test|dummy|x{3,})@)[^@\s]{6,}@ |
        \bhf_[A-Za-z0-9]{30,}
    )""",
    re.X,
)

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
INTERPRETERS = {"python", "python3", "node", "perl", "ruby", "php", "deno", "bun"}
# Argument forms that make an interpreter read its program from stdin.
STDIN_PATH = re.compile(r"^(-|/dev/stdin|/dev/fd/0|/proc/(self|\d+)/fd/0)$")
WRAPPERS_NO_ARG = {"command", "builtin", "exec", "nohup", "time", "setsid", "unbuffer", "!", "then", "do", "else", "elif", "if", "while", "until", "{", "}", "coproc"}
READONLY_CMDS = {
    "cat", "ls", "head", "tail", "less", "more", "wc", "stat", "file", "grep", "egrep", "fgrep", "rg",
    "ag", "find", "fd", "tree", "du", "df", "pwd", "echo", "printf", "true", "false", "test", "[", "[[",
    "readlink", "realpath", "basename", "dirname", "diff", "cmp", "md5sum", "sha1sum", "sha256sum",
    "sort", "uniq", "cut", "tr", "awk", "gawk", "jq", "yq", "column", "nl", "date", "id", "whoami",
    "hostname", "uname", "which", "type", "sleep", "tac", "od", "xxd", "hexdump", "strings",
    "sed", "comm", "join", "paste", "fold", "fmt", "git", "docker", "free", "uptime", "nproc", "lscpu",
    "lsblk", "ps", "pgrep", "nvidia-smi", "journalctl", "systemctl", "loginctl", "ss", "netstat", "ip",
    "curl", "zcat", "zgrep", "xzcat", "bzcat", "timeout", "nice", "ionice", "xargs", "cd", "pushd",
    "popd", "export", "set", "local", "read", "exit", "return", "shift", "unset", "declare", "python3",
    "python", "gh", "ssh",
}
GIT_READONLY = {
    "log", "show", "diff", "status", "rev-parse", "ls-files", "ls-tree", "cat-file", "branch", "tag",
    "worktree", "config", "check-ignore", "merge-base", "describe", "blame", "grep", "for-each-ref",
    "shortlog", "reflog", "rev-list", "name-rev", "remote", "show-ref", "count-objects", "fsck",
    "whatchanged", "range-diff", "var", "help", "version", "symbolic-ref", "ls-remote", "fetch",
    "verify-commit", "verify-tag", "cherry", "difftool", "annotate", "show-branch", "check-ref-format",
}
SYSTEM_DENY = {
    "sudo": "privilege escalation is never allowed (§3.3)",
    "su": "privilege escalation is never allowed (§3.3)",
    "doas": "privilege escalation is never allowed (§3.3)",
    "pkexec": "privilege escalation is never allowed (§3.3)",
    "run0": "privilege escalation is never allowed (§3.3)",
    "shutdown": "power state belongs to the operator",
    "reboot": "power state belongs to the operator",
    "poweroff": "power state belongs to the operator",
    "halt": "power state belongs to the operator",
    "telinit": "power state belongs to the operator",
    "iptables": "firewall/network configuration is off limits (§3.3)",
    "ip6tables": "firewall/network configuration is off limits (§3.3)",
    "nft": "firewall/network configuration is off limits (§3.3)",
    "ufw": "firewall/network configuration is off limits (§3.3)",
    "firewall-cmd": "firewall/network configuration is off limits (§3.3)",
    "nmcli": "network configuration is off limits (§3.3)",
    "tc": "network configuration is off limits (§3.3)",
    "modprobe": "kernel/driver changes are off limits (§3.3)",
    "insmod": "kernel/driver changes are off limits (§3.3)",
    "rmmod": "kernel/driver changes are off limits (§3.3)",
    "mount": "mounts belong to the operator",
    "umount": "mounts belong to the operator",
    "swapon": "swap belongs to the operator",
    "swapoff": "swap belongs to the operator",
    "cpupower": "clocks and power limits are off limits (§3.3)",
    "tuned-adm": "clocks and power limits are off limits (§3.3)",
    "apt": "system packages need root and belong to the operator",
    "apt-get": "system packages need root and belong to the operator",
    "aptitude": "system packages need root and belong to the operator",
    "dpkg": "system packages need root and belong to the operator",
    "snap": "system packages belong to the operator",
    "fwupdmgr": "firmware is off limits (§3.3)",
    "ubuntu-drivers": "drivers are off limits (§3.3)",
    "update-grub": "boot configuration is off limits (§3.3)",
    "grub-install": "boot configuration is off limits (§3.3)",
    "fdisk": "disks belong to the operator",
    "sfdisk": "disks belong to the operator",
    "parted": "disks belong to the operator",
    "wipefs": "disks belong to the operator",
    "passwd": "accounts belong to the operator",
    "chpasswd": "accounts belong to the operator",
    "chsh": "accounts belong to the operator",
    "useradd": "accounts belong to the operator",
    "usermod": "accounts belong to the operator",
    "userdel": "accounts belong to the operator",
    "groupadd": "accounts belong to the operator",
    "visudo": "sudoers is off limits (§3.3)",
    "ssh-copy-id": "new trust relationships are not allowed (§3.3)",
    "ssh-add": "new trust relationships are not allowed (§3.3)",
    "update-alternatives": "system configuration belongs to the operator",
    "ldconfig": "system configuration belongs to the operator",
    "setcap": "capabilities belong to the operator",
    "hostnamectl": "host configuration belongs to the operator",
    "timedatectl": "host configuration belongs to the operator",
    "eval": "eval hides the command from review; run the command directly",
    "crontab": "scheduling belongs to the operator (ops/autopilot/install.sh installs the runner)",
    "at": "scheduling belongs to the operator",
    "batch": "scheduling belongs to the operator",
    "printenv": "printing the environment exposes secrets",
    "chattr": "file attributes belong to the operator",
}


class Block(Exception):
    pass


def block(msg):
    raise Block(msg)


class Ctx:
    def __init__(self, raw, cwd, depth=0, env=None, piped_in=False, redirs=None, top=True, captured=False):
        self.raw = raw
        self.cwd = cwd
        self.depth = depth
        self.env = dict(env or {})
        self.piped_in = piped_in
        self.redirs = list(redirs or [])
        self.top = top
        # captured: this command's output does not reach the transcript (it is
        # captured by $(...), written to a file or piped into a non-reader).
        self.captured = captured
        self.out_ok = True
        # Literal variable values a command substitution inherits from the
        # segment it runs in (see analyze()). Never passed to a child shell,
        # script or handler: those see only exported variables.
        self.known = {}

    def child(self, **kw):
        c = Ctx(self.raw, kw.get("cwd", self.cwd), self.depth + 1, kw.get("env", self.env),
                kw.get("piped_in", False), kw.get("redirs", []), top=False,
                captured=kw.get("captured", self.output_captured()))
        c.known = dict(kw.get("known") or {})
        return c

    def output_captured(self):
        return self.captured or not self.out_ok

    def deny(self, why):
        block(f"{TAG} {self.raw} -> blocked: {why}")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def expand_path(p, cwd):
    p = (p or "").strip()
    if not p:
        return None
    if p.startswith("~/") or p == "~":
        p = HOME + p[1:]
    elif p.startswith("~"):
        m = re.match(r"^~([^/]+)(/.*|$)", p)
        if m:
            try:
                p = pwd.getpwnam(m.group(1)).pw_dir + (m.group(2) or "")
            except KeyError:
                return None
    # Only the plain forms: ${HOME/a/b} or ${HOME:-x} are expansions, not $HOME.
    p = re.sub(r"^(?:\$HOME|\$\{HOME\})(?=/|$)", lambda _m: HOME, p)
    if "$" in p or "`" in p or "__SUBST__" in p:
        return None
    if not os.path.isabs(p):
        p = os.path.join(cwd or HOME, p)
    return os.path.realpath(p)


def under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def is_guard_path(path):
    return any(under(path, g) for g in GUARD_FILES)


def holds_guard_file(path):
    """True when `path` is, or is a directory that contains, a guard file.
    Moving or deleting such a directory would disable the guardrails (P0-17)."""
    return any(g == path or under(g, path) or under(path, g) for g in GUARD_FILES)


def write_allowed(path):
    if path is None or is_guard_path(path):
        return False
    if any(under(path, d) for d in WRITE_DENY_INSIDE_ALLOW):
        return False
    if re.search(r"/finetune(/|$)", path):
        return False
    if re.fullmatch(r"settings(\.local)?\.json", os.path.basename(path)) and "/.claude/" in path:
        return False
    return any(under(path, root) for root in WRITE_ALLOW)


def write_why(path):
    if path and is_guard_path(path):
        return "the autopilot may never modify its own guardrails (guard hook, runner, settings, master prompt, test-db.vars, CI approvals) (§3.3); ask the operator in NEEDS_HUMAN.md"
    if path and under(path, R_PROD):
        return f"the production checkout is read-only; work in {DEV_WORKTREE}"
    if path and under(path, R_DOCUMENTS):
        return "~/Documents (production checkout, operator worktrees, model cache, brain vault) is read-only to the autopilot"
    if path and re.search(r"/finetune(/|$)", path):
        return "finetune/ is out of scope (§3.3)"
    if path and "/.claude/" in path and path.endswith(".json"):
        return "Claude Code settings belong to the operator"
    return f"{path} is outside the autopilot's write zones ({WORK}, /tmp, ~/.llm-autopilot/agent, ~/.claude/projects)"


def is_tracked(path):
    """A tracked file in this public repository is not a secret."""
    if not (under(path, R_WORK) or under(path, R_PROD)):
        return False
    try:
        r = subprocess.run(["git", "-C", os.path.dirname(path), "ls-files", "--error-unmatch", os.path.basename(path)],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


# Tracked in the public repository, yet holds live credentials (memory note
# salesforce-consumer-key-in-repo): secret wherever it appears.
ALWAYS_SECRET = re.compile(r"^Training_Module_Feature_Map_and_Memory\.txt$")


# Any process's environment, this session's included (it holds the test-database URL).
PROC_ENVIRON = re.compile(r"^/proc/[^/]+/(task/[^/]+/)?environ$")


def is_secret_path(path):
    if path is None:
        return False
    base = os.path.basename(path)
    if ALWAYS_SECRET.match(base) or PROC_ENVIRON.match(os.path.normpath(path)):
        return True
    hit = bool(SECRET_BASENAME.match(base)) or any(under(path, d) for d in SECRET_DIRS) \
        or bool(SECRET_PATH_PARTS.search(path))
    return hit and not is_tracked(path)


def free_fraction(path="/"):
    try:
        st = os.statvfs(path)
        return st.f_bavail / st.f_blocks if st.f_blocks else 1.0
    except OSError:
        return 1.0


# --------------------------------------------------------------------------
# Shell text
# --------------------------------------------------------------------------

def find_close(s, i):
    """Index of the ')' closing a '(' that started just before s[i]."""
    depth, q, n = 1, None, len(s)
    while i < n:
        c = s[i]
        if q == "'":
            if c == "'":
                q = None
        elif c == "\\":
            i += 1
        elif q == '"':
            if c == '"':
                q = None
            elif c == "$" and i + 1 < n and s[i + 1] == "(":
                depth += 1
                i += 1
        elif c in ("'", '"'):
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


# A command substitution's place in the text: __SUBST__ plus its index in the
# list extract_substitutions() returns, so analyze() can check the inner command
# with the variables known where it runs. _restore() drops the index again.
SUBST_MARK = re.compile("__SUBST__\x02(\\d+)\x02")


def subst_marker(n):
    return f"__SUBST__\x02{n}\x02"


def extract_substitutions(s):
    """Replace $(...), `...`, <(...) and >(...) by an indexed __SUBST__ marker
    (see SUBST_MARK); return the inner commands. Inners found inside $(( ))
    carry no marker."""
    out, inner, q, i, n = [], [], None, 0, len(s)
    while i < n:
        c = s[i]
        if q == "'":
            out.append(c)
            if c == "'":
                q = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(s[i : i + 2])
            i += 2
            continue
        if q is None and c in ("'", '"'):
            if c == "'" and i > 0 and s[i - 1] == "$":
                out.pop()  # ANSI-C quoting hides text from review
                block(f"{TAG} {short(s)} -> blocked: $'...' quoting hides the command; write it literally.")
            q = c
            out.append(c)
            i += 1
            continue
        if q == '"' and c == '"':
            q = None
            out.append(c)
            i += 1
            continue
        if c == "$" and s.startswith("$((", i):
            j = find_close(s, i + 3)
            j = find_close(s, j + 1) if j is not None and j + 1 < n and s[j + 1] == ")" else j
            if j is None:
                block(f"{TAG} {short(s)} -> blocked: unbalanced $(( (fail-closed).")
            # bash still RUNS a command substitution inside arithmetic, e.g.
            # $(( $(cmd) + 1 )); pull it out so it is analysed, not discarded.
            _arith_rest, arith_inner = extract_substitutions(s[i + 3 : j])
            inner.extend(arith_inner)
            out.append("0")
            i = j + 1
            continue
        if (c == "$" or (q is None and c in "<>")) and i + 1 < n and s[i + 1] == "(":
            j = find_close(s, i + 2)
            if j is None:
                block(f"{TAG} {short(s)} -> blocked: unbalanced $( (fail-closed).")
            inner.append(s[i + 2 : j])
            out.append(subst_marker(len(inner) - 1))
            i = j + 1
            continue
        if c == "`":
            j = i + 1
            while j < n and s[j] != "`":
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                block(f"{TAG} {short(s)} -> blocked: unbalanced backtick (fail-closed).")
            inner.append(s[i + 1 : j])
            out.append(subst_marker(len(inner) - 1))
            i = j + 1
            continue
        out.append(c)
        i += 1
    if q is not None:
        block(f"{TAG} {short(s)} -> blocked: unbalanced quotes (fail-closed).")
    return "".join(out), inner


HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def split_heredocs(cmd):
    """Return (text_without_bodies, [(text_before_operator, body)])."""
    lines = cmd.split("\n")
    out, docs, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        pending = [(m.group(3), m.group(1) == "-", line[: m.start()]) for m in HEREDOC_RE.finditer(line)]
        i += 1
        for delim, strip_tabs, consumer in pending:
            body, closed = [], False
            while i < len(lines):
                cur = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if cur.rstrip() == delim:
                    closed = True
                    break
                body.append(lines[i - 1])
            if not closed:
                block(f"{TAG} {short(cmd)} -> blocked: here-document '{delim}' is never closed (fail-closed).")
            docs.append((consumer, "\n".join(body)))
    return "\n".join(out), docs


# Placeholders for a '$' or '~' that bash reads literally (single-quoted,
# escaped, or a '~' inside double quotes). analyze() never resolves them as a
# variable or a home directory and turns them back into '$' / '~' before any
# check, so they still read as computed text.
LITERAL_DOLLAR, LITERAL_TILDE = "\x00", "\x01"
# protect=True marks an unquoted, unescaped `case`, `esac`, `fi` or `done` word
# with KW_MARK. Only a marked word opens a case statement or closes a compound
# command in segments(): to bash a quoted "case" or "fi" is an ordinary command
# word, so it must neither turn the words after it into (unchecked) patterns nor
# make a body that may not run look like top-level code. _restore() drops the mark.
KW_MARK = "\x04"
CASE_KW = KW_MARK + "case"
_MARKED_KEYWORDS = re.compile(r"(case|esac|fi|done)(?=$|[\s;|&()<>])")


def prepare(text, protect=False):
    """Quote-aware: drop line continuations and comments, turn newlines into ';'.
    With protect=True, a '$' or '~' that bash reads literally becomes a
    placeholder (see LITERAL_DOLLAR) so variable resolution cannot touch it,
    and a bare compound keyword gets KW_MARK."""
    res, q, i, n = [], None, 0, len(text)
    while i < n:
        c = text[i]
        if protect and q is None and c in "cefd" and (i == 0 or text[i - 1] in " \t\n;|&()"):
            m = _MARKED_KEYWORDS.match(text, i)
            if m:
                res.append(KW_MARK + m.group(1))
                i = m.end()
                continue
        if q == "'":
            res.append(LITERAL_DOLLAR if protect and c == "$" else LITERAL_TILDE if protect and c == "~" else c)
            if c == "'":
                q = None
        elif c == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                i += 2
                continue
            if protect and text[i + 1] in "$~":
                res.append(LITERAL_DOLLAR if text[i + 1] == "$" else LITERAL_TILDE)
            else:
                res.append(text[i : i + 2])
            i += 2
            continue
        elif q == '"':
            res.append(LITERAL_TILDE if protect and c == "~" else c)
            if c == '"':
                q = None
        elif c in ("'", '"'):
            q = c
            res.append(c)
        elif c == "\n":
            res.append(" ; ")
        elif c == "#" and (i == 0 or text[i - 1] in " \t;|&("):
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            res.append(c)
        i += 1
    return "".join(res)


SEPARATORS = {";", "&", "&&", "|", "||", "|&", "(", ")", ";;", "&;", ";&", ";;&"}
REDIRECT_RE = re.compile(r"^\d*(>>?|>\||&>>?|<>?|<<<|<<-?|>&|<&)$")
# Separators that start a segment whose assignments run unconditionally. A
# segment reached only through '&&'/'||'/'|' (or inside a subshell / if-for-
# while-case body) may or may not run, so a bare assignment there is NOT a
# value the guard can trust later (it leaves the variable unresolved instead).
UNCONDITIONAL_SEPARATORS = {None, ";", "&", "(", ")", ";;", "&;", ";&", ";;&"}
COMPOUND_OPEN = {"if", "for", "while", "until", "select"}  # `case` is CASE_KW, see segments()
COMPOUND_CLOSE = {KW_MARK + "fi", KW_MARK + "done", KW_MARK + "esac"}  # unquoted only, see prepare()
ESAC_KW = KW_MARK + "esac"
# Reserved words after which bash still reads a command keyword in the same
# position (`then if ...`, `do case ...`, `! while ...`, `{ for ...`).
LEAD_RESERVED = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "time"}
CASE_ARM_ENDS = {";;", ";&", ";;&"}
_PUNCT = set(";&|()<>")


def tokenize(text):
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def writes_output_file(redirs):
    """True when a redirection sends output to a file (not a descriptor or /dev/null)."""
    for op, target in redirs:
        if ">" not in op:
            continue
        if target.startswith("&") or re.fullmatch(r"\d+|-", target or "") or target in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"):
            continue
        return True
    return False


def segments(tokens):
    """Split tokens into simple commands:
    [(words, redirs, piped_in, unconditional, before, after)].

    `unconditional` is True when a bare `V=value` assignment in the segment is
    certain to run in THIS shell: at the top level (not inside a subshell '(...)'
    nor an if/for/while/until/case body), reached by ';'/'&'/newline/start (never
    through '&&'/'||'/'|'), and not itself ended by '&', '|' or '|&' (a
    background job or a pipeline stage runs in a subshell, so its assignments
    never reach the next command). The guard trusts a variable's value only from
    such a segment; everywhere else it leaves `$VAR` unresolved (fail-closed).
    A compound keyword counts after leading reserved words too (`then if`,
    `do case`), so a nested body is never read as top level.

    `before` / `after` are the separators on either side of the segment (None at
    the start / end of the text); analyze() follows '&&' chains with them.

    A case statement opens only on CASE_KW (an unquoted `case` in command
    position, followed by one word and `in`). Its `case WORD in` header is one
    segment and each arm's body is ordinary segments. The words of an arm's
    pattern list (`start)`, `*.py|*.md)`, `"completed "*)`) are patterns bash
    matches and never runs, so they are dropped; a command substitution inside
    one is still analysed (analyze() checks every substitution no segment
    carries)."""
    tokens = list(tokens)
    segs, words, redirs, piped_in, i = [], [], [], False, 0
    sep, paren, compound, pending_open, seg_flag, seg_before = None, 0, 0, 0, None, None
    cases = []  # one entry per open case statement: "pattern" (reading an arm's patterns) or "body"
    pat_started, pat_depth = False, 0

    def start_flag():
        return paren == 0 and compound == 0 and sep in UNCONDITIONAL_SEPARATORS

    def begin():
        nonlocal seg_flag, seg_before
        if seg_flag is None:
            seg_flag, seg_before = start_flag(), sep

    def close(t):
        nonlocal words, redirs, seg_flag, compound, pending_open
        if words or redirs:
            flag = seg_flag if seg_flag is not None else start_flag()
            segs.append((words, redirs, piped_in, flag and t not in ("&", "|", "|&"), seg_before, t))
            compound += pending_open
            pending_open = 0
        words, redirs, seg_flag = [], [], None

    while i < len(tokens):
        t = tokens[i]
        if cases and cases[-1] == "pattern" and t != ESAC_KW:
            # An arm's pattern list, up to the ')' that ends it: '|' separates
            # patterns, a leading '(' is optional, newlines (';') are skipped.
            # shlex glues runs of punctuation ('))', ');;'), so read them by char.
            if set(t) <= _PUNCT:
                rest = None
                for k, ch in enumerate(t):
                    if ch == "(" and pat_started:
                        pat_depth += 1
                    elif ch == ")" and pat_depth:
                        pat_depth -= 1
                    elif ch == ")":
                        rest = t[k + 1 :]
                        break
                if rest is not None:
                    cases[-1] = "body"
                    sep, piped_in, pat_started, pat_depth = ")", False, False, 0
                    if rest:
                        tokens[i] = rest
                        continue
            else:
                pat_started = True
            i += 1
            continue
        if t in SEPARATORS:
            close(t)
            piped_in = t in ("|", "|&")
            sep = t
            if t in CASE_ARM_ENDS and cases:
                cases[-1] = "pattern"
                pat_started, pat_depth = False, 0
            elif t == "(":
                paren += 1
            elif t == ")":
                paren = max(0, paren - 1)
            i += 1
            continue
        if REDIRECT_RE.match(t):
            if words and words[-1].isdigit():
                words.pop()  # file-descriptor number, e.g. 2>
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            redirs.append((t, target))
            begin()
            i += 2
            continue
        lead = all(x in LEAD_RESERVED for x in words)
        if t == CASE_KW and lead:
            j, k = i + 1, i + 2
            while k < len(tokens) and tokens[k] == ";":  # `case WORD` newline `in`
                k += 1
            if j < len(tokens) and not set(tokens[j]) <= _PUNCT and not REDIRECT_RE.match(tokens[j]) \
                    and k < len(tokens) and tokens[k] == "in":
                begin()
                words += [t, tokens[j], "in"]
                pending_open += 1
                close("in")
                sep, piped_in = "in", False
                cases.append("pattern")
                pat_started, pat_depth = False, 0
                i = k + 1
                continue
        if not words and seg_flag is None and t in COMPOUND_CLOSE:
            compound = max(0, compound - 1)
            if t == ESAC_KW and cases:
                cases.pop()
        begin()
        if lead and t in COMPOUND_OPEN:
            pending_open += 1
        words.append(t)
        i += 1
    close(None)
    return segs


def strip_prefix(words):
    """Drop assignments and wrapper commands. Returns (env, words, saw_bare_env)."""
    env, w = {}, list(words)
    while w:
        t = w[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t):
            k, v = t.split("=", 1)
            env[k] = v
            w.pop(0)
            continue
        base = os.path.basename(t)
        if base == "coproc":
            w.pop(0)
            # `coproc NAME { body }`: drop the optional name so the { body } group
            # is analysed (without the name, strip_prefix would treat it as a cmd).
            if len(w) >= 2 and re.fullmatch(r"[A-Za-z_]\w*", w[0]) and w[1] == "{":
                w.pop(0)
            continue
        if base in WRAPPERS_NO_ARG:
            w.pop(0)
            # `command -v/-V NAME` only LOOKS a name up; it never runs it, so it
            # is not the start of that command (command -v sudo is a lookup).
            if base == "command" and w and (w[0] in ("-v", "-V") or re.fullmatch(r"-[a-zA-Z]*[vV][a-zA-Z]*", w[0])):
                return env, [], False
            # Consume this wrapper's own options so '-p'/'-a name'/'--' are not
            # mistaken for the real command (command -p sudo, exec -a x sudo).
            while w and w[0].startswith("-") and w[0] != "-":
                opt = w.pop(0)
                if opt == "--":
                    break
                if base == "exec" and opt == "-a" and w:
                    w.pop(0)
                elif base == "time" and opt in ("-o", "--output", "-f", "--format") and w:
                    w.pop(0)
            continue
        if base == "env":
            rest = w[1:]
            while rest and (rest[0].startswith("-") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", rest[0])):
                if not rest[0].startswith("-"):
                    k, v = rest[0].split("=", 1)
                    env[k] = v
                elif rest[0] in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string") and len(rest) > 1:
                    rest = rest[1:]
                rest = rest[1:]
            if not rest:
                return env, [], True
            w = rest
            continue
        if base == "nice":
            w.pop(0)
            if w and w[0] == "-n":
                w = w[2:]
            elif w and re.fullmatch(r"-\d+|--adjustment=.*|-n\d+", w[0]):
                w.pop(0)
            continue
        if base == "ionice":
            w.pop(0)
            while w and w[0].startswith("-"):
                opt = w.pop(0)
                if opt in ("-c", "-n", "-p", "-P", "-u", "--class", "--classdata") and w:
                    w.pop(0)
            continue
        if base == "timeout":
            w.pop(0)
            while w and w[0].startswith("-"):
                opt = w.pop(0)
                if opt in ("-s", "--signal", "-k", "--kill-after") and w:
                    w.pop(0)
            if w:
                w.pop(0)
            continue
        if base == "stdbuf":
            w.pop(0)
            while w and w[0].startswith("-"):
                opt = w.pop(0)
                if opt in ("-i", "-o", "-e") and w:
                    w.pop(0)
            continue
        if base in ("chrt", "taskset"):
            w.pop(0)
            while w and (w[0].startswith("-") or re.fullmatch(r"[0-9a-fx,\-]+", w[0])):
                w.pop(0)
            continue
        break
    return env, w, False


def short(s, n=140):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def nonopt(args):
    return [a for a in args if not a.startswith("-")]


# Shell specials that resolve to a number at run time; substituting them lets a
# path like /tmp/x.$$ resolve instead of looking "computed".
SHELL_SPECIAL = re.compile(r"\$\$|\$!|\$(?:RANDOM|PPID|BASHPID)\b|\$\{(?:RANDOM|PPID|BASHPID)\}")
# The only values the guard substitutes for $VAR: one word that bash can neither
# split nor glob (no whitespace, glob, quote, brace, '$', '`', '\\' or '~').
LITERAL_VALUE = re.compile(r"[A-Za-z0-9_./:@%+=,-]+")
# Variables bash changes by itself (cd, every command, read, getopts, ...) or
# whose effect reaches past their own value (IFS, PATH, HOME): never resolved.
SHELL_MANAGED = re.compile(
    r"^(_|PWD|OLDPWD|REPLY|OPTARG|OPTIND|OPTERR|RANDOM|SRANDOM|SECONDS|LINENO|EPOCHSECONDS|EPOCHREALTIME|HISTCMD|"
    r"PPID|UID|EUID|SHLVL|DIRSTACK|GROUPS|FUNCNAME|PIPESTATUS|IFS|HOME|PATH|CDPATH|MAPFILE|COPROC\w*|HOSTNAME|"
    r"SHELLOPTS|GLOBIGNORE|BASH\w*|COMP\w*)$"
)
# Text that can change a variable in ways the guard does not follow: ${V:=w} /
# ${V=w}, and arithmetic (( )) / $(( )) / $[ ] (which can assign).
ASSIGNING_EXPANSION = re.compile(r"\$\{[!#]?[A-Za-z_]\w*(\[[^\]]*\])?:?=|\(\(|\$\[")
# `VAR+=x` and `VAR[i]=x` / `VAR[i]+=x`: assignments strip_prefix leaves in place.
APPEND_OR_ELEMENT_ASSIGN = re.compile(r"[A-Za-z_]\w*(\[[^\]]*\])?\+?=.*", re.S)


def _usable(val):
    return val is not None and "`" not in val and "$(" not in val


def _restore(word):
    """Turn the LITERAL_DOLLAR / LITERAL_TILDE placeholders back into '$' / '~'
    and an indexed substitution marker back into plain __SUBST__."""
    word = word.replace(LITERAL_DOLLAR, "$").replace(LITERAL_TILDE, "~").replace(KW_MARK, "")
    return SUBST_MARK.sub("__SUBST__", word) if "\x02" in word else word


def _known_value(name, val):
    """The value `name=val` stores, when the guard can know it exactly; else None.
    `val` still carries the prepare() placeholders, so a quoted or escaped '$'
    or '~' is never read as a variable or a home directory."""
    if SHELL_MANAGED.match(name) or not val or "__SUBST__" in val:
        return None
    val = re.sub(r"^(?:\$HOME|\$\{HOME\})(?=/|$)", lambda _m: HOME, val)
    if val == "~" or val.startswith("~/"):
        val = HOME + val[1:]
    return val if LITERAL_VALUE.fullmatch(val) else None


def _resolve_braced(body, known):
    """Resolve ${V}, ${V:-w} or ${V-w} for a variable in `known` (whose value is a
    non-empty literal, so both defaults keep it). Every other operator (':=' and
    '=' assign; ':offset', '#', '%', '/', '^', ',', '@', ':+', '!', '[...]')
    returns None, so the '$' stays and the computed-at-run-time denials fire."""
    m = re.fullmatch(r"([A-Za-z_]\w*)(?::?-.*)?", body, re.S)
    return known.get(m.group(1)) if m else None


def subst_vars(word, known):
    """Resolve $VAR / ${VAR} / ${VAR:-WORD} from `known` (see analyze()), plus the
    benign numeric specials ($$, $RANDOM, ...), so later checks see the real path
    or refspec. Anything else keeps its '$' on purpose: it then trips the
    computed-command / literal-path / literal-refspec denials (fail-closed)."""
    if "$" not in word:
        return word
    word = SHELL_SPECIAL.sub("0", word)
    out, i, n = [], 0, len(word)
    while i < n:
        c = word[i]
        if c == "$" and i + 1 < n and word[i + 1] == "{":
            j = _brace_close(word, i + 2)
            rep = _resolve_braced(word[i + 2 : j], known) if j is not None else None
            if rep is not None:
                out.append(rep)
                i = j + 1
                continue
            out.append(c)
            i += 1
            continue
        if c == "$" and i + 1 < n and (word[i + 1].isalpha() or word[i + 1] == "_"):
            m = re.match(r"\$([A-Za-z_]\w*)", word[i:])
            val = known.get(m.group(1))
            out.append(val if val is not None else word[i : i + m.end()])
            i += m.end()
            continue
        out.append(c)
        i += 1
    return "".join(out)


def resolution_unsafe(text, tokens, segs, had_function):
    """True when the command can change a variable in a way the guard does not
    follow, so analyze() resolves no $VAR in it at all (fail-closed): a function
    definition or a { } group (bodies run later, or conditionally), sourcing a
    file, a trap handler, printf -v, declare-family options (namerefs, case and
    integer attributes), IFS, ${V:=w} / ${V=w}, or arithmetic that can assign."""
    if had_function or ASSIGNING_EXPANSION.search(text):
        return True
    if any(t in ("{", "}") or re.match(r"IFS(\+?=|$)", t) for t in tokens):
        return True
    for words, *_rest in segs:
        _env, w, _bare = strip_prefix(words)
        if not w:
            continue
        name = os.path.basename(w[0])
        if name in (".", "source", "trap"):
            return True
        if name == "printf" and any(a.startswith("-v") for a in w[1:]):
            return True
        if name in ("declare", "typeset", "local", "readonly", "export") and any(a[:1] in "-+" for a in w[1:]):
            return True
    return False


def _brace_close(s, i):
    """Index just past the '}' matching the '{' before s[i] (i points past '{')."""
    depth, n = 1, len(s)
    while i < n:
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


_FUNC_HEADER = re.compile(r"(?:function\s+)?([A-Za-z_]\w*)\s*\(\s*\)\s*(?=\{)|function\s+([A-Za-z_]\w*)\s+(?=\{)")


def strip_function_headers(text):
    """Quote-aware: drop `name() {` and `function name {` headers (keeping the
    `{ ... }` body) so the body is analysed as ordinary commands, and the body of
    a `coproc name { ... }` is reached too. Returns (text, whether a header was
    dropped): a function body runs later, so its assignments make every $VAR in
    the command uncertain."""
    res, q, i, n, found = [], None, 0, len(text), False
    last_sig = ";"  # the last non-space character emitted (start acts like a separator)
    while i < n:
        c = text[i]
        if q == "'":
            res.append(c)
            if c == "'":
                q = None
            last_sig = c
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            res.append(text[i : i + 2])
            last_sig = text[i + 1]
            i += 2
            continue
        if q == '"':
            res.append(c)
            if c == '"':
                q = None
            last_sig = c
            i += 1
            continue
        if c in ("'", '"'):
            q = c
            res.append(c)
            last_sig = c
            i += 1
            continue
        if c in " \t":
            res.append(c)
            i += 1
            continue
        if last_sig in ";&|({\n" and (text[i].isalpha() or text[i] == "_" or text.startswith("function", i)):
            m = _FUNC_HEADER.match(text, i)
            if m:
                res.append(" ")
                i = m.end()
                last_sig = " "
                found = True
                continue
        res.append(c)
        last_sig = c
        i += 1
    return "".join(res), found


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def analyze(cmd, ctx):
    if ctx.depth > 8:
        ctx.deny("the command nests too deeply for review (fail-closed)")
    text, docs = split_heredocs(cmd)
    for consumer, body in docs:
        check_heredoc(consumer, body, ctx)
    unsafe_text = text  # heredoc bodies are data here; they are analysed above
    text, inners = extract_substitutions(text)
    text, had_function = strip_function_headers(text)
    try:
        tokens = tokenize(prepare(text, protect=True))
    except ValueError as exc:
        ctx.deny(f"could not parse the command ({exc}); simplify quoting (fail-closed)")
    cwd = ctx.cwd
    env = dict(ctx.env)
    segs = segments(tokens)
    # $VAR resolution. `known` holds only literal values assigned earlier in THIS
    # text by a segment certain to run in this shell (plus, inside a command
    # substitution, the values known where it runs). Nothing else is inherited: a
    # child shell, script or handler sees only exported variables, and an outer
    # value may be stale there. The command word is never resolved, and a command
    # that can change variables in ways the guard does not follow resolves nothing.
    resolve = not resolution_unsafe(unsafe_text, tokens, segs, had_function)
    known = dict(ctx.known) if resolve else {}
    analysed = set()
    # `env` is what the checks read (TEST_DATABASE_URL, DOCKER_HOST, ...). It
    # over-approximates: a variable this text changes is never dropped (a check
    # would then fall back to the hook's own environment); a value that is not
    # certain becomes UNCERTAIN (see _note_env). `chain` holds the values set
    # earlier in the current '&&' chain, which are certain whenever the
    # segment runs (`cd x && export V=v && cmd`: cmd runs only after the export).
    chain, prev = {}, None
    for idx, (pwords, predirs, piped_in, unconditional, before, after) in enumerate(segs):
        if prev is not None:
            # The segment after `A && B` runs only if B ran in this shell, unless
            # B came after '||' (`A || B && C`: B may not have run) or is a
            # pipeline stage (a subshell). Any other separator ends the chain.
            p_after, p_before, p_piped, p_new = prev
            if p_after != "&&":
                chain = {}
            elif p_before != "||" and not p_piped:
                chain.update(p_new)
        new = {}  # what this segment assigns, for the chain after it
        prev = (after, before, piped_in, new)
        words = [_restore(x) for x in pwords]
        redirs = [(op, _restore(t)) for op, t in predirs]
        seen = {**env, **chain}
        sctx = Ctx(ctx.raw, cwd, ctx.depth, seen, piped_in, redirs, top=ctx.top, captured=ctx.captured)
        # A command substitution runs (in a subshell) when this segment expands
        # its words: check it with the variables known here, as at the top level.
        for x in list(pwords) + [t for _op, t in predirs]:
            for m in SUBST_MARK.finditer(x):
                n = int(m.group(1))
                if n < len(inners):
                    analysed.add(n)
                    # its output feeds the outer command, not the transcript
                    analyze(inners[n], sctx.child(captured=True, known=known if resolve else {}))
        sctx.out_ok = not writes_output_file(redirs) and pipeline_tail_is_safe(segs, idx)
        check_redirects(sctx)
        raw_env, pw, bare_env = strip_prefix(pwords)
        # `VAR+=suffix` (append) and `VAR[i]=x` (array element) are assignments
        # too, and make the value uncertain: drop the variable (later references
        # stay unresolved) and read on to the real command word.
        while pw and APPEND_OR_ELEMENT_ASSIGN.fullmatch(pw[0]):
            var = re.match(r"[A-Za-z_]\w*", pw[0]).group(0)
            _note_env(env, new, var, None, unconditional)
            known.pop(var, None)
            more_env, pw, more_bare = strip_prefix(pw[1:])
            raw_env.update(more_env)
            bare_env = bare_env or more_bare
        senv = {k: _restore(v) for k, v in raw_env.items()}
        if bare_env:
            sctx.deny("printing the environment exposes secrets")
        check_exec_env(senv, sctx)
        if not pw:
            # A bare `FOO=bar` is trusted for $FOO only when it is certain to run.
            # When it is guarded by &&/||, inside a subshell or compound body, or
            # ended by '&' / '|', later `$FOO` stays unresolved (fail-closed).
            for k, v in senv.items():
                _note_env(env, new, k, v, unconditional)
            for k, v in raw_env.items():
                kv = _known_value(k, v) if unconditional else None
                if kv is None:
                    known.pop(k, None)
                else:
                    known[k] = kv
            continue
        sctx.env = {**seen, **senv}
        if resolve:
            pw = pw[:1] + [subst_vars(x, known) for x in pw[1:]]
        for k in raw_env:
            known.pop(k, None)  # a prefix assignment persists after a special builtin (POSIX sh)
        raw_args = pw[1:]
        w = [_restore(x) for x in pw]
        name = os.path.basename(w[0])
        if name in POSIX_SPECIAL_BUILTINS:
            for k, v in senv.items():
                _note_env(env, new, k, v, False)
        if w[0] in ("cd", "pushd"):
            target = nonopt(w[1:])
            nxt = expand_path(target[0], cwd) if target else HOME
            cwd = nxt or cwd
            continue
        track_var_mutations(name, raw_args, unconditional, env, known, new, sctx)
        if re.search(r"\{[^{}]*,[^{}]*\}", w[0]):
            sctx.deny("brace expansion builds the command name; write the command literally so the guard can read it")
        if "$" in w[0] or "__SUBST__" in w[0] or "`" in w[0]:
            sctx.deny("the command name is computed at run time and cannot be reviewed; write it literally")
        if re.search(r"[*?]", w[0]) or ("[" in w[0] and w[0] not in ("[", "[[")):
            sctx.deny("a glob builds the command name; write the command literally so the guard can read it")
        if cwd and not write_allowed(_real(cwd)) and name not in READONLY_CMDS and name not in SYSTEM_DENY:
            sctx.deny(f"the working directory {cwd} is read-only to the autopilot; cd to {DEV_WORKTREE} first")
        if name == "merge_to_dev.sh":
            check_gate_invocation(words, w, sctx)
        if ("/" in w[0] or w[0].endswith(".sh")) and name not in SHELLS and name not in INTERPRETERS:
            analyze_script_file(w[0], sctx)
        check_command(name, w[1:], sctx)
    # Substitutions with no marker in any segment (inside $(( )), or in a
    # comment) are still checked, with no variable resolved.
    for n, inner in enumerate(inners):
        if n not in analysed:
            analyze(inner, ctx.child(captured=True))


_VARNAME = re.compile(r"[A-Za-z_]\w*")
# A value the guard cannot know: set by a segment that may not run, computed at
# run time, read, looped over, appended to, or changed through declare options.
# The checks that read a variable from ctx.env (TEST_DATABASE_URL, DOCKER_HOST,
# DOCKER_CONTEXT/CONFIG, TECHSARA_STACK and COMPOSE_*, HOME for git push) refuse it.
UNCERTAIN = "__UNCERTAIN__"
# POSIX special builtins: an assignment in front of one persists in sh.
POSIX_SPECIAL_BUILTINS = {":", ".", "source", "break", "continue", "eval", "exec", "exit", "export", "readonly",
                          "return", "set", "shift", "times", "trap", "unset"}


def _note_env(env, new, k, v, certain):
    """Record that a segment sets `k` to `v` (None: a value the guard cannot
    know). `env` over-approximates what later segments may see: a certain
    assignment replaces the value; one that may not run keeps it only when both
    agree (the old value is env's, else the hook's own environment), otherwise
    the variable becomes UNCERTAIN. It is never dropped, so no check falls back
    to the hook's environment for a variable this command changed. `new` gets
    the value for the '&&' chain after the segment (see analyze())."""
    val = v if v is not None and _usable(v) else UNCERTAIN
    new[k] = val
    if not certain and (env[k] if k in env else os.environ.get(k)) != val:
        val = UNCERTAIN
    env[k] = val


def track_var_mutations(name, raw_args, unconditional, env, known, new, ctx):
    """Keep `env` (values handed to the checks, see _note_env) and `known`
    (values $VAR may resolve to) in step with a command that sets, appends,
    reads, loops over, declares or unsets shell variables, so later `$VAR`
    references resolve to a trustworthy value or stay unresolved (fail-closed).
    `raw_args` still carry the prepare() placeholders. A variable is trusted
    for $VAR only after an unconditional literal assignment; read/for/select/
    wait/declare options/printf -v/unset/let/mapfile all drop it instead."""
    args = [_restore(a) for a in raw_args]
    if name in ("export", "declare", "typeset", "readonly", "local"):
        has_opts = any(a[:1] in "-+" for a in args)  # -n nameref, -l/-u case, -i integer, ...
        assigned, raw_assigned, bare = {}, {}, []
        for a, ra in zip(args, raw_args):
            if a[:1] in "-+":
                continue
            if "=" in a:
                k, v = a.split("=", 1)
                assigned[k] = v
                raw_assigned[k] = ra.split("=", 1)[1]
            elif _VARNAME.fullmatch(a):
                bare.append(a)
        check_exec_env(assigned, ctx)
        for k, v in assigned.items():
            # e.g. `declare BR=main` sets BR=main, so a later push to it is seen
            _note_env(env, new, k, None if has_opts else v, unconditional)
            kv = _known_value(k, raw_assigned[k]) if unconditional and not has_opts else None
            if kv is None:
                known.pop(k, None)
            else:
                known[k] = kv
        for k in bare:
            # `export V` / `readonly V` keep V's value; declare/typeset/local may
            # change it (attributes, a new local).
            if name not in ("export", "readonly") or has_opts:
                _note_env(env, new, k, None, unconditional)
            known.pop(k, None)
        return
    targets = []
    if name == "unset":
        if not any(a.startswith("-") and "f" in a for a in args):  # unset -f names functions
            targets = [(a, "") for a in args if _VARNAME.fullmatch(a)]
    elif name in ("for", "select"):
        targets = [(a, None) for a in args[:1]]  # the loop variable
    elif name == "printf":
        for i, a in enumerate(args):
            if a == "-v" and i + 1 < len(args):
                targets.append((args[i + 1], None))
            elif a.startswith("-v"):
                targets.append((a[2:], None))
    elif name in ("read", "mapfile", "readarray", "getopts", "wait", "let"):
        # Every name-shaped argument may be (re)assigned: read/mapfile/getopts/
        # wait -p targets, let NAME=expr.
        targets = [(a, None) for a in args]
    for a, v in targets:
        m = re.match(r"^([A-Za-z_]\w*)", a)
        if m:
            _note_env(env, new, m.group(1), v, unconditional)
            known.pop(m.group(1), None)


# Pipe sinks that only print what they read: a pure reader piped into them still
# shows its output in the transcript and writes nothing.
SAFE_SINKS = {"head", "tail", "wc", "grep", "egrep", "fgrep", "cut", "tr", "nl", "cat", "column"}


def pipeline_tail_is_safe(segs, idx):
    """True when segment idx is not piped, or every later stage of its pipeline is a safe sink."""
    j = idx + 1
    while j < len(segs) and segs[j][2]:  # piped_in
        words, redirs = segs[j][0], segs[j][1]
        _env, w, _bare = strip_prefix(words)
        if not w or os.path.basename(w[0]) not in SAFE_SINKS or writes_output_file(redirs):
            return False
        if any("__SUBST__" in x for x in w):
            return False
        j += 1
    return True


# Variables that make a program run another command (or load code) chosen by
# their value; set inline they hide that command from review.
EXEC_ENV = re.compile(
    r"^(BASH_ENV|ENV|LD_PRELOAD|LD_AUDIT|PROMPT_COMMAND|PERL5OPT|LESSOPEN|LESSCLOSE|PAGER|GIT_PAGER|GIT_EXTERNAL_DIFF|"
    r"GIT_SSH|GIT_SSH_COMMAND|GIT_ASKPASS|SSH_ASKPASS|EDITOR|VISUAL|GIT_EDITOR|GIT_SEQUENCE_EDITOR|GIT_PROXY_COMMAND|"
    r"GIT_EXEC_PATH|GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_CONFIG|GIT_CONFIG_GLOBAL|GIT_CONFIG_SYSTEM|GIT_CONFIG_PARAMETERS|"
    r"GIT_CONFIG_COUNT|GIT_CONFIG_KEY_\d+|GIT_CONFIG_VALUE_\d+|GIT_TEMPLATE_DIR|GH_CONFIG_DIR|GH_HOST|GH_TOKEN|GITHUB_TOKEN|"
    r"GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN|DOCKER_CONFIG|DOCKER_CONTEXT|"
    # interpreters that run code named by an environment variable on start-up.
    r"PYTHONSTARTUP|RUBYOPT|NODE_OPTIONS|"
    # procps prints process environments under a BSD personality (ps -ef).
    r"PS_PERSONALITY|CMD_ENV)$"
)
EXEC_ENV_SAFE_VALUES = {"", "true", ":", "cat", "less", "/bin/true", "/usr/bin/true"}


def check_exec_env(assignments, ctx):
    for k, v in assignments.items():
        if k == "GIT_CONFIG_GLOBAL" and v == "/dev/null":
            continue
        if k == "GH_REPO":
            if v.lower() != REPO_SLUG.lower():
                ctx.deny(f"gh may only act on {REPO_SLUG}")
            continue
        if EXEC_ENV.match(k) and v not in EXEC_ENV_SAFE_VALUES:
            ctx.deny(f"setting {k} makes programs run code or use configuration the guard cannot review; leave it unset")


def check_gate_invocation(words, w, ctx):
    """The installed gate runs only as its own command: no assignments, wrappers or exported variables."""
    if expand_path(w[0], ctx.cwd) != _real(MERGE_TO_DEV):
        ctx.deny(f"run the installed gate {MERGE_TO_DEV}, not a repository copy (the agent cannot weaken the installed one)")
    if words[0] != w[0] or ctx.env:
        ctx.deny(f"run the installed gate {MERGE_TO_DEV} as a plain command: no variable assignments, exports or wrappers in front of it (they would change what it checks)")


def check_heredoc(consumer, body, ctx):
    names = [os.path.basename(x) for x in re.split(r"[\s|;&()]+", consumer.strip()) if x]
    tail = names[-4:]
    if any(n in SHELLS for n in tail) or "ssh" in tail:
        analyze(body, ctx.child())
    elif "psql" in names and is_prod_db_text(consumer):
        check_prod_sql(body, ctx)
    elif any(n in INTERPRETERS or re.fullmatch(r"python3\.\d+", n) for n in tail):
        check_code_text(body, ctx)


def check_redirects(ctx):
    for op, target in ctx.redirs:
        if op.startswith("<<") and op != "<<<":
            continue
        if target.startswith("&") or re.fullmatch(r"\d+|-", target or ""):
            continue
        p = expand_path(target, ctx.cwd)
        if "<" in op and ">" not in op:
            if op == "<" and p and is_secret_path(p):
                ctx.deny("reads a secret file through '<' (§3.3)")
            continue
        if p is None:
            ctx.deny(f"the redirect target '{target}' is computed at run time; use a literal path")
        if not write_allowed(p):
            ctx.deny(write_why(p))


def in_quiet_window():
    import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    return QUIET_WINDOW_IST[0] <= now.hour < QUIET_WINDOW_IST[1]


PURE_READERS = {"cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "diff", "cmp", "stat", "file", "ls",
                "md5sum", "sha1sum", "sha224sum", "sha256sum", "sha384sum", "sha512sum", "b2sum", "cksum"}
GIT_READERS = {"log", "diff", "show", "blame", "annotate"}
# Reader options that run a program they name: rg --pre (per file) and
# rg --hostname-bin (for hyperlinks). ag is not a reader for the same reason
# (ag --pager).
READER_EXEC_OPTIONS = ("--pre", "--hostname-bin")


def is_pure_read(cmd, args):
    """Commands that only print files: they cannot run, write or replace what they read."""
    if cmd in PURE_READERS:
        return not any(a.startswith(READER_EXEC_OPTIONS) for a in args)
    if cmd == "sed":
        scripts, rest, i = [], [], 0
        while i < len(args):
            a = args[i]
            if a in ("-e", "--expression") and i + 1 < len(args):
                scripts.append(args[i + 1])
                i += 2
                continue
            if a.startswith("--expression="):
                scripts.append(a.split("=", 1)[1])
            elif a.startswith("-") and a != "-":
                if a.startswith(("-i", "--in-place", "-f", "--file", "-s", "--separate")) or (not a.startswith("--") and re.search(r"[ifs]", a[1:])):
                    return False
                if a not in ("-n", "--quiet", "--silent", "-E", "-r", "--regexp-extended", "-u", "--unbuffered", "-z", "--null-data", "--debug"):
                    return False
            else:
                rest.append(a)
            i += 1
        if not scripts and rest:
            scripts.append(rest.pop(0))
        # line ranges and p/q/= only: no w (write), e (execute), r/R (read), s///e
        return bool(scripts) and all(re.fullmatch(r"[0-9,$;pqn=\s]+", s) for s in scripts)
    if cmd == "git":
        a = list(args)
        while a and a[0].startswith("-"):
            opt = a.pop(0)
            if opt in ("-C", "-c") and a:
                a.pop(0)
            elif opt not in ("--no-pager", "-P", "--no-replace-objects", "--literal-pathspecs"):
                return False
        if not a or a[0] not in GIT_READERS:
            return False
        return not any(x.startswith(("--output", "--ext-diff", "--textconv", "-O", "--open-files-in-pager")) for x in a[1:])
    return False


# procps-ng ps. A BSD-syntax 'e' (no dash) appends each process's environment.
# When a dash-syntax command line fails to parse, procps parses it again as BSD
# syntax with the dashes dropped, so 'ps -ex' or 'ps -ef -o pid,args' print
# environments as well. The guard therefore checks both readings.
PS_KEYWORDS = frozenset(
    "%cpu %mem _left _left2 _right _right2 _unlimited _unlimited2 ag_id ag_nice args atime blocked bsdstart bsdtime c caught "
    "cgname cgroup cgroupns class cls cmd comm command context cp cpuid cputime cputimes cuc cuu drs dsiz egid egroup eip esp "
    "etime etimes euid euser exe f fgid fgroup flag flags fname fsgid fsgroup fsuid fsuser fuid fuser gid group ignored intpri "
    "ipcns label lastcpu lim longtname lsession lstart luid lwp lxc m_drs m_size m_trs machine maj_flt majflt min_flt minflt "
    "mntns netns ni nice nlwp numa oom oomadj opri ouid pagein pcpu pending pgid pgrp pid pidns pmem policy ppid pri pri_api "
    "pri_bar pri_baz pri_foo priority psr pss rbytes rchars rgid rgroup rops rss rssize rsz rtprio ruid ruser s sched seat sess "
    "session sgi_p sgi_rss sgid sgroup sid sig sig_block sig_catch sig_ignore sig_pend sigcatch sigignore sigmask size slice "
    "spid stackp start start_stack start_time stat state stime suid supgid supgrp suser svgid svgroup svuid svuser sz tgid "
    "thcount tid time timens times tname tpgid trs trss tsig tsiz tt tty tty4 tty8 ucmd ucomm uid uid_hack uname unit user "
    "userns uss util utsns uunit vsize vsz wbytes wcbytes wchan wchars wname wops zone".split()
)
PS_UNIX_VALUE = {"C": "any", "D": "any", "G": "groups", "g": "groups", "O": "format", "o": "format", "p": "numbers",
                 "q": "numbers", "s": "numbers", "U": "users", "u": "users", "t": "unknown", "k": "unknown", "n": "unknown"}
PS_SAFE_FLAGS = set("eAadHwLTm")  # never fail to parse, never conflict with a format
PS_FORMAT_FLAGS = set("fFjl")  # predefined formats: fine together, conflict with -o/-O
PS_BSD_VALUE = set("oOpUtkq")  # BSD options that take the rest of the word or the next word
PS_LONG = {"forest": None, "headers": None, "no-headers": None, "cumulative": None, "format": "format", "sort": "sort",
           "pid": "numbers", "ppid": "numbers", "quick-pid": "numbers", "sid": "numbers", "user": "users", "User": "users",
           "group": "groups", "Group": "groups", "cols": "numbers", "columns": "numbers", "width": "numbers",
           "rows": "numbers", "lines": "numbers", "tty": "unknown"}


def _ps_value_ok(kind, val):
    if val is None or "$" in val or "__SUBST__" in val or "`" in val:
        return False
    items = [x for x in re.split(r"[,\s]+", val) if x]
    if kind == "any":
        return bool(val)
    if kind == "numbers":
        return bool(items) and all(x.isdigit() for x in items)
    if kind == "format":
        names = val.split("=", 1)[0]
        cols = [x for x in re.split(r"[,\s]+", names) if x]
        return bool(cols) and all(re.sub(r":\d+$", "", c) in PS_KEYWORDS for c in cols)
    if kind == "sort":
        return bool(items) and all(x.lstrip("+-") in PS_KEYWORDS for x in items)
    if kind in ("users", "groups"):
        def known(x):
            if x.isdigit():
                return True
            try:
                (pwd.getpwnam if kind == "users" else grp.getgrnam)(x)
                return True
            except KeyError:
                return False
        return bool(items) and all(known(x) for x in items)
    return False


def _ps_bsd_word(word):
    """Read one BSD-syntax word. Returns (shows_environment, takes_next_word)."""
    word = word.replace("__SUBST__", "$")
    if re.match(r"^\d", word):
        return False, False  # a process ID list
    for i, c in enumerate(word):
        if c == "e" or c in "$`*?[":  # computed text could expand to 'e'
            return True, False
        if c in PS_BSD_VALUE:
            return False, i == len(word) - 1
    return False, False


def ps_env_risk(args):
    """Why this ps command line could print process environments, or None."""
    # 1. How procps reads it: dash words as UNIX options, bare words as BSD options.
    provable, fmt_opts, flags, i, n = True, [], set(), 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            provable = False
        elif a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            if name not in PS_LONG:
                provable = False
            elif PS_LONG[name]:
                if not eq:
                    val = args[i + 1] if i + 1 < n else None
                    i += 1
                provable = provable and _ps_value_ok(PS_LONG[name], val)
                if name == "format":
                    fmt_opts.append("o")
        elif a.startswith("-") and len(a) > 1:
            body = a[1:]
            for j, c in enumerate(body):
                if c in PS_UNIX_VALUE:
                    val = body[j + 1 :]
                    if not val:
                        val = args[i + 1] if i + 1 < n else None
                        i += 1
                    provable = provable and _ps_value_ok(PS_UNIX_VALUE[c], val)
                    if c in "oO":
                        fmt_opts.append(c)
                    break
                if c not in PS_SAFE_FLAGS | PS_FORMAT_FLAGS:
                    provable = False
                flags.add(c)
        else:
            if not re.fullmatch(r"\d+(,\d+)*", a):
                provable = False
            env, takes_next = _ps_bsd_word(a)
            if env:
                return f"'{a}' is BSD-syntax ps with 'e', which prints process environments (secrets); use ps -ef or ps -o <columns> -p <pid>"
            if takes_next:
                i += 1
        i += 1
    if fmt_opts and flags & PS_FORMAT_FLAGS or "O" in fmt_opts and len(fmt_opts) > 1:
        provable = False
    # 2. The BSD re-read procps falls back to when step 1 fails to parse.
    i, retry_env = 0, False
    while i < n:
        a = args[i]
        if a.startswith("--"):
            name, eq, _val = a[2:].partition("=")
            if PS_LONG.get(name) and not eq:
                i += 1
        else:
            env, takes_next = _ps_bsd_word(a[1:] if a.startswith("-") and a != "-" else a)
            retry_env = retry_env or env
            if takes_next:
                i += 1
        i += 1
    if retry_env and not provable:
        return ("this ps command line may fail to parse as dash options, and procps then re-reads it as BSD options "
                "where 'e' prints process environments (secrets); use a plain form such as ps -ef, ps -eo pid,etime,args "
                "or ps -o pid,etime -p <pid> (known columns, existing users, numeric IDs)")
    return None


def check_production_reach(cmd, args, ctx):
    """Rules about what a command can reach in production, whatever the command is."""
    text = " ".join([f"{k}={v}" for k, v in ctx.env.items()] + list(args))
    if cmd in LOAD_TOOLS:
        ctx.deny("load generators are not run on these hosts: the main model is shared TP=2 across both DGX nodes (§3.3: no disruptive load against production)")
    if any("@anthropic-ai/claude-code" in a for a in args):
        ctx.deny("the autopilot may not launch or modify Claude Code itself")
    interp = cmd in INTERPRETERS or re.fullmatch(r"python3\.\d+", cmd)
    if cmd == "autopilot.py" or (interp and not {"-m", "-c"} & set(args) and any(os.path.basename(a) == "autopilot.py" for a in nonopt(args))):
        ctx.deny("the autopilot runner belongs to the operator; starting another runner would launch Claude sessions outside these guardrails (run its tests with pytest instead)")
    # Reading a production script or harness (cat, grep, git log, ...) is fine when
    # the output only reaches the transcript; running, copying or editing it is not.
    reading = is_pure_read(cmd, args) and not ctx.output_captured()
    for a in [cmd] + ([] if reading else list(args)):
        v = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a  # --pre=<script>
        if PROD_SCRIPTS.search(v) and not v.startswith("-"):
            ctx.deny(f"{a} acts on PRODUCTION (deploys, cluster, backups, host guard, reconciler, shared e2e stack); the programme never changes production (operator decision 3: prepare it and list it in NEEDS_HUMAN.md)")
        if HEAVY_HARNESS.search(a) and not in_quiet_window():
            ctx.deny(f"{a} loads the production engines; run it only in the measured low-traffic window {QUIET_WINDOW_IST[0]:02d}:00-{QUIET_WINDOW_IST[1]:02d}:00 IST (§9.3)")
    # The production database: 127.0.0.1:5432. Its startup migrations are irreversible.
    prod_db = re.search(rf"(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0):{PROD_DB_PORT}\b|(^|\s)(-p|--port)\s*=?\s*{PROD_DB_PORT}\b|\bPGPORT={PROD_DB_PORT}\b|@(localhost|127\.0\.0\.1)/", text)
    if prod_db:
        if cmd == "psql":
            if ctx.piped_in or any(op in ("<", "<<<") for op, _ in ctx.redirs):
                ctx.deny("SQL piped into production psql cannot be checked; pass one SELECT with -c")
            check_prod_sql(" ".join(args), ctx)
        else:
            ctx.deny("this points at the PRODUCTION database; a dev service there would apply irreversible migrations, and dumps would copy user data. Dev and test databases live elsewhere (§9.3)")
    # Tests TRUNCATE every table: never the shared or default test server.
    runs_pytest = cmd in ("pytest", "py.test") or (cmd.startswith("python") and "pytest" in args[:3])
    if runs_pytest:
        where = (ctx.cwd or "") + " " + " ".join(args)
        if re.search(r"orchestrator|sync-worker", where):
            # inline or set earlier in this command; otherwise the runner sets it for the cycle
            url = ctx.env["TEST_DATABASE_URL"] if "TEST_DATABASE_URL" in ctx.env else os.environ.get("TEST_DATABASE_URL", "")
            if url == UNCERTAIN:
                ctx.deny("TEST_DATABASE_URL is set conditionally or computed earlier in this command, so the guard cannot tell which database the tests would truncate; set it unconditionally (export TEST_DATABASE_URL=...; ...), in the same && chain, or inline in front of pytest")
            if not url:
                ctx.deny(f"orchestrator tests TRUNCATE every table; pass TEST_DATABASE_URL (and TEST_DATABASE_ALLOWED_HOSTS) for the autopilot's dedicated test database inline; the URL is in {TEST_DB.get('url_file', '~/.llm-autopilot/agent/test-db.url')}")
            m = re.match(r"^postgres(?:ql)?(?:\+\w+)?://(?:[^@/]*@)?(\[[^\]]+\]|[^:/?]+)(?::(\d+))?/([^?\s]+)", url)
            host, port, dbname = (m.group(1).lower(), m.group(2) or "5432", m.group(3)) if m else (None, None, "")
            want = (str(TEST_DB.get("host", "")).lower(), str(TEST_DB.get("port", "")))
            if not m or not dbname.endswith("_test") or port in SHARED_TEST_DB_PORTS or (host, port) != want:
                ctx.deny("TEST_DATABASE_URL must point at the autopilot's dedicated test database (host and port from the host config, a database name ending in _test); never production or the shared test server")
    # Production HTTP control planes: read-only.
    if cmd in ("curl", "wget", "http", "https"):
        for a in args:
            m = re.match(r"^(?:https?://)?(\[[^\]]+\]|[^/:\s?#]+)(?::(\d+))?(/\S*)?$", a)
            if not m or m.group(1).lower() not in PROD_HOSTS:
                continue
            port, path = m.group(2) or "80", m.group(3) or "/"
            if path.startswith("/-/"):
                ctx.deny("lifecycle endpoints such as /-/reload or /-/quit change production monitoring")
            if port in PROD_CONTROL_PORTS and NET_UPLOAD.search(" " + " ".join(args)):
                ctx.deny(f"mutating requests to production service port {port} change production state; read-only GETs only")


def check_command(cmd, args, ctx):
    if cmd.startswith("mkfs"):
        ctx.deny("disks belong to the operator")
    if cmd in SYSTEM_DENY:
        if cmd == "crontab" and args == ["-l"]:
            return
        ctx.deny(SYSTEM_DENY[cmd])
    check_disk(cmd, args, ctx)
    check_production_reach(cmd, args, ctx)
    if cmd in ("export", "declare", "typeset") and (not args or (set(args) & {"-p", "-x", "-px", "-xp"} and not any("=" in a for a in args))):
        ctx.deny("printing the environment exposes secrets")
    if cmd == "set" and not args:
        ctx.deny("printing shell variables exposes secrets")
    if cmd in ("echo", "printf", "cat", "tee") and SECRET_VAR.search(" ".join(args)):
        ctx.deny("prints a secret-looking variable")
    if cmd == "ps":
        why = ps_env_risk(list(args))
        if why:
            ctx.deny(why)
    if any("/proc/" in a and "environ" in a for a in args):
        ctx.deny("process environments hold secrets")
    if cmd in SHELLS:
        return check_shell(args, ctx)
    if cmd in ("source", "."):
        if args and ("__SUBST__" in args[0] or args[0].startswith("/dev/fd") or STDIN_PATH.match(args[0]) or args[0].startswith("/proc/")):
            ctx.deny("sourcing generated or stdin input (/dev/stdin, /dev/fd, /proc/self/fd) hides the commands; run them directly")
        if args and (os.path.basename(args[0]) == "merge_to_dev.sh" or expand_path(args[0], ctx.cwd) == _real(MERGE_TO_DEV)):
            ctx.deny(f"run the installed gate {MERGE_TO_DEV} directly; sourcing it runs it with the caller's functions and variables")
        if args:
            analyze_script_file(args[0], ctx, force=True)
        return
    if cmd in INTERPRETERS or re.fullmatch(r"python3\.\d+", cmd):
        return check_interpreter(cmd, args, ctx)
    if cmd in ("awk", "gawk", "mawk") and any(re.search(r"\bsystem\s*\(|\|\s*\"|print[^;}]*>\s*\"", a) for a in args):
        ctx.deny("awk programs that run commands or write files hide them from review")
    if cmd in ("pip", "pip3") or re.fullmatch(r"pip3\.\d+", cmd):
        return check_pip(args, ctx)
    if cmd in ("npm", "pnpm", "yarn", "npx", "bunx", "corepack"):
        if any(a in ("-g", "--global", "--location=global") for a in args) or args[:1] in (["link"], ["unlink"]):
            ctx.deny("global installs change the operator's tools (and could replace the claude binary); install project-locally")
        if any("claude-code" in a for a in args):
            ctx.deny("the autopilot may not launch or modify Claude Code itself")
        return
    if cmd == "claude":
        if args not in (["--version"], ["-v"], ["--help"], ["-h"], ["auth", "status"], ["doctor"]):
            ctx.deny("nested Claude sessions would run outside these guardrails; use the Agent tool for subagents")
        return
    if cmd in ("techsara", "techsara_cli"):
        if not any(a in ("--help", "-h", "--version", "--dry-run") for a in args):
            ctx.deny("the techsara launcher manages PRODUCTION (a second checkout's 'techsara up' degraded production for 20 minutes on 2026-09-15); only --help/--version/--dry-run")
        return
    if cmd == "git":
        return check_git(args, ctx)
    if cmd == "gh":
        return check_gh(args, ctx)
    if cmd in ("docker", "docker-compose", "podman"):
        return check_docker(["compose"] + args if cmd == "docker-compose" else args, ctx)
    if cmd == "systemctl":
        return check_systemctl(args, ctx)
    if cmd == "systemd-run":
        if "--scope" not in args:
            ctx.deny("transient services outlive the cycle; use 'systemd-run --user --scope -p MemoryMax=...' to cap one command")
        rest = list(args)
        while rest and rest[0].startswith("-"):
            opt = rest.pop(0)
            if opt in ("-p", "--property", "--unit", "-u", "--slice", "--description", "-E", "--setenv", "--uid", "--gid", "--working-directory") and rest:
                rest.pop(0)
        if rest:
            analyze_words(rest, ctx)
        return
    if cmd == "loginctl" and args and args[0] not in ("show-user", "show-session", "list-users", "list-sessions", "user-status", "session-status"):
        ctx.deny("login and linger settings belong to the operator")
    if cmd in ("kill", "pkill", "killall"):
        return check_kill(cmd, args, ctx)
    if cmd == "nvidia-smi":
        if any(re.match(r"^(-pl|--power-limit|-lgc|--lock-gpu-clocks|-rgc|--reset-gpu-clocks|-lmc|-rmc|-ac|--applications-clocks|-rac|-pm|--persistence-mode|-r$|--gpu-reset|-c$|--compute-mode|-e$|--ecc-config|-mig|--multi-instance-gpu|-cc|-am|-caa|--auto-boost)", a) for a in args):
            ctx.deny("clocks, power limits and GPU modes are off limits (§3.3)")
        return
    if cmd == "ip":
        if re.search(r"\b(add|del|delete|set|change|replace|flush|append|prepend)\b", " ".join(args)):
            ctx.deny("network configuration is off limits (§3.3)")
        return
    if cmd == "ethtool" and any(a.startswith("-") and a not in ("-i", "-S", "-k", "-g", "-l", "-m", "-T", "-P", "--version", "--show-features", "--statistics", "--driver") for a in args):
        ctx.deny("NIC configuration is off limits (§3.3)")
    if cmd == "sysctl" and any("=" in a or a in ("-w", "-p", "--write", "--load", "--system") for a in args):
        ctx.deny("kernel parameters are off limits (§3.3)")
    if cmd == "ssh-keygen" and not any(a in ("-l", "-F", "-L", "-y", "-Q") for a in args):
        ctx.deny("new keys and known_hosts edits create trust relationships (§3.3)")
    if cmd in ("ssh", "scp", "sftp", "rsync"):
        check_ssh(cmd, args, ctx)
    if cmd in ("curl", "wget", "http", "https", "nc", "ncat", "socat", "telnet"):
        check_network(cmd, args, ctx)
    if cmd == "xargs":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            opt = rest.pop(0)
            if opt in ("-I", "-n", "-P", "-L", "-d", "-a", "-s", "-E", "--max-args", "--max-procs", "--delimiter", "--arg-file") and rest:
                rest.pop(0)
        if rest:
            analyze_words(rest, ctx)
        return
    if cmd == "find":
        for flag in ("-exec", "-execdir", "-ok", "-okdir"):
            if flag in args:
                i = args.index(flag)
                inner = []
                for a in args[i + 1 :]:
                    if a in (";", "+", "\\;"):
                        break
                    inner.append(a)
                inner = [a for a in inner if a != "{}"]
                if inner:
                    analyze_words(inner, ctx)
    if cmd == "watch":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            opt = rest.pop(0)
            if opt in ("-n", "--interval", "-d") and rest and re.fullmatch(r"[\d.]+", rest[0]):
                rest.pop(0)
        if rest:
            analyze(" ".join(rest), ctx.child())
    if cmd == "flock":
        if "-c" in args:
            i = args.index("-c")
            if i + 1 < len(args):
                analyze(args[i + 1], ctx.child())
        else:
            rest = nonopt(args)
            if len(rest) >= 2:
                analyze_words(rest[1:], ctx)
    if cmd == "trap":
        # trap [-lp] ACTION SIGSPEC...: the action is shell run on the signal.
        rest = [a for a in args if a not in ("-l", "-p", "--")]
        if rest and rest[0] not in ("-", ""):
            analyze(rest[0], ctx.child())
        return
    if cmd == "script":
        for i, a in enumerate(args):
            if a in ("-c", "--command") and i + 1 < len(args):
                analyze(args[i + 1], ctx.child())
            elif a.startswith("--command="):
                analyze(a.split("=", 1)[1], ctx.child())
        return
    if cmd == "busybox":
        if args:
            return analyze_words(args, ctx)
        return
    if cmd in ("parallel", "rush"):
        # GNU parallel runs the command before ':::'; analyse it.
        inner = []
        for a in args:
            if a in (":::", "::::", "::::+", ":::+"):
                break
            if a.startswith("-"):
                continue
            inner.append(a)
        if inner:
            analyze_words(inner, ctx)
        return
    if cmd in ("tmux", "screen"):
        if re.search(r"\b(new-session|new|new-window|neww|send-keys|send|split-window|splitw|respawn-pane|respawn-window)\b|(^|\s)-(dm|dmS|S|d)\b", " ".join(args)):
            ctx.deny("detached sessions outlive the cycle and hide their commands; run work in the foreground")
    check_secret_args(cmd, args, ctx)
    check_write_targets(cmd, args, ctx)


def analyze_words(words, ctx):
    env, w, bare = strip_prefix(words)
    if bare:
        ctx.deny("printing the environment exposes secrets")
    check_exec_env(env, ctx)
    if not w:
        return
    c = ctx.child(env={**ctx.env, **env})
    if "$" in w[0] or "__SUBST__" in w[0] or "`" in w[0] or re.search(r"[*?]|\{[^{}]*,[^{}]*\}", w[0]):
        ctx.deny("the command that xargs, find -exec, parallel or another wrapper runs is computed at run time; write it literally")
    name = os.path.basename(w[0])
    if name in ("techsara",) or w[0].endswith("/techsara"):
        name = "techsara"
    if name == "merge_to_dev.sh":
        ctx.deny(f"run the installed gate {MERGE_TO_DEV} as a plain command of its own, not through xargs, find, watch or another wrapper")
    check_command(name, w[1:], c)


def check_shell(args, ctx):
    for i, a in enumerate(args):
        if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a):
            rest = [x for x in args[i + 1 :] if not x.startswith("-")]
            if rest:
                analyze(rest[0], ctx.child())
            return
    here = [t for op, t in ctx.redirs if op == "<<<"]
    if here:
        return analyze(here[0], ctx.child())
    scripts = nonopt(args)
    if scripts:
        if os.path.basename(scripts[0]) == "merge_to_dev.sh" or expand_path(scripts[0], ctx.cwd) == _real(MERGE_TO_DEV):
            ctx.deny(f"run the installed gate {MERGE_TO_DEV} directly, not through a shell (a shell would load BASH_ENV and the caller's functions)")
        return analyze_script_file(scripts[0], ctx, force=True)
    src = [t for op, t in ctx.redirs if op == "<"]
    if src:
        if STDIN_PATH.match(src[0]):
            ctx.deny("feeding a shell from stdin (/dev/stdin, -) hides the commands from review; run them directly")
        return analyze_script_file(src[0], ctx, force=True)
    if ctx.piped_in:
        ctx.deny("piping into a shell hides the commands from review; run them directly")


def check_interpreter(cmd, args, ctx):
    code = None
    for flag in ("-c", "-e", "--eval", "-p", "--print"):
        if flag in args and (flag != "-p" or cmd in ("node", "perl", "ruby")) and (flag not in ("-e", "--eval", "-p", "--print") or cmd != "python3" and not cmd.startswith("python")):
            i = args.index(flag)
            code = args[i + 1] if i + 1 < len(args) else ""
            break
    if code is None:
        here = [t for op, t in ctx.redirs if op == "<<<"]
        if here:
            code = here[0]  # `python3 <<< 'code'` runs the here-string as its program
    if code is not None:
        check_code_text(code, ctx)
    if cmd.startswith("python") and "-m" in args:
        i = args.index("-m")
        mod = args[i + 1] if i + 1 < len(args) else ""
        if mod == "pip":
            check_pip(args[i + 2 :], ctx)
    if code is None and ctx.piped_in and any(STDIN_PATH.match(a) for a in nonopt(args)):
        ctx.deny("piping code into an interpreter through stdin (/dev/stdin, /proc/self/fd/0, -) hides it from review; write a file under ~/work and run it")
    if ctx.piped_in and code is None and not nonopt(args):
        ctx.deny("piping code into an interpreter hides it from review; write a file under ~/work and run it")
    if ctx.cwd and not write_allowed(_real(ctx.cwd)) and code is None and not any(a in ("--version", "-V") for a in args):
        ctx.deny(f"running scripts from a read-only tree can write into it; cd to {DEV_WORKTREE}")
    for a in nonopt(args):
        p = expand_path(a, ctx.cwd)
        if p and is_secret_path(p):
            ctx.deny("reads a secret file (§3.3)")


# Inline code that shells out: the string handed to os.system / subprocess /
# execSync / perl-ruby system is analysed as a shell command.
# A call with an open paren before the string (os.system("..."), execSync('...'),
# perl/ruby system("...")). The '(' is mandatory so prose such as "the system's
# config" is not read as a system() call and analysed as shell.
_CODE_SHELL_STR = re.compile(
    r"(?:os\.system|os\.popen|subprocess\.(?:getoutput|getstatusoutput)|commands\.getoutput|"
    r"(?:child_process\.)?execSync|execFileSync|spawnSync|\bsystem)\s*\(\s*(['\"])(.*?)\1",
    re.S,
)
# perl/ruby `system "cmd"` / `system 'cmd'` without parentheses: whitespace
# before the quote is required, so "system's" (apostrophe, no space) is prose.
_CODE_SYSTEM_BARE = re.compile(r"\bsystem\s+(['\"])(.*?)\1", re.S)
_CODE_SHELL_LIST = re.compile(r"subprocess\.\w+\(\s*\[([^\]]*)\]", re.S)
_CODE_WRITE_OP = re.compile(
    r"open\([^)]*['\"]\s*,\s*['\"][wax+]|\.write_(text|bytes)|shutil\.(rmtree|move|copy\w*)|"
    r"os\.(remove|unlink|rename|replace|rmdir|makedirs|mkdir)|writeFileSync|rmSync|unlinkSync|renameSync|"
    r"pathlib\.Path\([^)]*\)[^\n]*\.(write_|unlink|rename|replace|rmdir|mkdir)"
)
# A secret-shaped environment variable read from inline code. TOKEN(?!S|IZER)
# and AUTH(?!OR) keep MAX_TOKENS / TOKENIZERS_PARALLELISM / GIT_AUTHOR_NAME (all
# benign in this LLM codebase) from being read as secrets.
_CODE_SECRET_ENV = re.compile(
    r"(?:os\.environ(?:\.get)?\s*[\[(]\s*|getenv\s*\(\s*|\bgetenv\s+|process\.env[.\[])\s*['\"]?"
    r"[A-Za-z_]*(TOKEN(?!S|IZER)|SECRET|PASSWORD|PASSWD|API_?KEY|_KEY|PRIVATE|CREDENTIAL|COOKIE|AUTH(?!OR))",
    re.I,
)


def _join_string_concats(code):
    """Join adjacent string literals spliced with + or . ('sud'+'o' -> 'sudo')."""
    prev = None
    for _ in range(8):
        if prev == code:
            break
        prev = code
        code = re.sub(r"(['\"])([^'\"\n]*)\1\s*[.+]\s*(['\"])([^'\"\n]*)\3",
                      lambda m: m.group(1) + m.group(2) + m.group(4) + m.group(1), code)
    return code


def check_code_text(code, ctx):
    if re.search(r"(?<![\w-])sudo\s", code):
        ctx.deny("inline code calls sudo")
    if SECRET_TOKEN_IN_TEXT.search(code) or _CODE_SECRET_ENV.search(code) or re.search(r"/proc/[^\s'\"]*/environ|\benviron\.(items|copy|keys|values)\b|dict\(\s*os\.environ\s*\)|print\(\s*os\.environ\s*\)|process\.env\s*\)|JSON\.stringify\(\s*process\.env", code):
        ctx.deny("inline code reads secrets or dumps the environment")
    if re.search(r"git\s+push[^\n'\"]*\s(-f|--force|--mirror|--delete)|docker\s+(volume\s+(rm|prune)|system\s+prune)|\bsudo\b", code):
        ctx.deny("inline code performs a forbidden git/docker/sudo action")
    joined = _join_string_concats(code)
    for m in _CODE_SHELL_STR.finditer(joined):
        analyze(m.group(2), ctx.child())
    for m in _CODE_SYSTEM_BARE.finditer(joined):
        analyze(m.group(2), ctx.child())
    for m in _CODE_SHELL_LIST.finditer(joined):
        items = re.findall(r"['\"]([^'\"]*)['\"]", m.group(1))
        if items:
            analyze(" ".join(items), ctx.child())
    for root in (PROD_CHECKOUT, R_PROD, os.path.join(HOME, "Documents"), "~/Documents",
                 AUTOPILOT_HOME, "~/.llm-autopilot", "/.llm-autopilot/"):
        if root in joined and _CODE_WRITE_OP.search(joined):  # joined so 'a'+'b' concatenation is seen
            ctx.deny(f"inline code writes or deletes under {root}, which is read-only to the autopilot (guard files and production/model trees)")


# The exact header distlib / pip / uv write on a console-script entry point:
#   #!/bin/sh
#   '''exec' "<python>" "$0" "$@"
#   ' '''
# sh runs only line 2, which replaces sh with python on the same file. <python>
# is a literal path, or the script's own directory plus python[X.Y].
_PY_NAME = r"python[0-9.]*"
_ENTRY_EXEC = re.compile(
    r"'''exec' (?:"
    r'"(?P<q>/[^"$`\\]*/' + _PY_NAME + r')"'
    r"|(?P<u>/[^\s\"'$`\\;&|<>(){}*?\[\]]*/" + _PY_NAME + r")"
    r'|"\$\(dirname (?:-- )?"\$\((?:readlink -f|realpath)(?: --)? "\$0"\)"\)/(?P<rel>' + _PY_NAME + r')"'
    r') "\$0" "\$@"'
)


def is_python_entry_point(text, path):
    """True only for the exact entry-point header above whose interpreter is a
    real binary. Anything else with a `'''exec'` line is analysed as shell, and
    so is an entry point whose "python" is a script (it would run as shell)."""
    lines = text.split("\n", 3)
    if len(lines) < 3 or lines[0].rstrip() != "#!/bin/sh" or lines[2].rstrip() != "' '''":
        return False
    m = _ENTRY_EXEC.fullmatch(lines[1].rstrip())
    if not m:
        return False
    target = m.group("q") or m.group("u") or os.path.join(os.path.dirname(os.path.realpath(path)), m.group("rel"))
    try:
        with open(os.path.realpath(target), "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def analyze_script_file(path, ctx, force=False):
    p = expand_path(path, ctx.cwd)
    if p is None or not os.path.isfile(p):
        return
    if p == _real(MERGE_TO_DEV):
        return  # the installed release gate enforces its own checks
    try:
        if os.path.getsize(p) > 512_000:
            return
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return
    # A pip / distlib console-script entry point (pip, pytest, ...) is a Python
    # program behind a sh/python polyglot header; sh runs only its exec line.
    if is_python_entry_point(text, p):
        return
    if text.startswith("#!") and not re.match(r"#!\s*/(usr/)?bin/(env\s+)?(ba|z|da|k)?sh\b", text):
        return
    if not force and not text.startswith("#!") and not p.endswith(".sh"):
        return
    try:
        analyze(text, Ctx(ctx.raw, ctx.cwd, ctx.depth + 1, ctx.env, top=False))
    except Block as b:
        raise Block(f"{b} (inside {path})")


# A glob whose fixed part targets a secret file (cat .env*, cat .runtime/*).
SECRET_GLOB = re.compile(r"(^|/)(\.env([.*?\[]|$)|secrets?([.*?\[]|$)|\.runtime/(secrets|\*|\.\*)|credentials|id_(rsa|dsa|ecdsa|ed25519)|[^/]*\.(pem|key|p12|pfx))", re.I)
GREP_FAMILY = {"grep", "egrep", "fgrep", "rg", "ag"}
# Options that take a value, per search tool: (short option letters, long
# options). `grep` covers GNU grep and ugrep (Claude Code runs ugrep for
# `grep`); a letter is listed when it takes a value in one and is invalid in the
# other. An option missing here is read as a flag, which can only turn its value
# into an operand the guard checks (fail-closed); a flag listed here would hide
# the next word (the pattern, or a file) from review. Optional values (GNU
# --color[=WHEN], ag -A/-B/-C [N]) are flags: only `--opt=value` attaches them.
_GREP_VALUE = ("efmABCdDgtOMNJK", frozenset({
    "--regexp", "--file", "--max-count", "--min-count", "--after-context", "--before-context", "--context",
    "--directories", "--devices", "--include", "--exclude", "--exclude-dir", "--include-dir", "--exclude-from",
    "--include-from", "--exclude-fs", "--include-fs", "--label", "--binary-files", "--and", "--andnot", "--not",
    "--neg-regexp", "--colors", "--colours", "--delay", "--depth", "--encoding", "--file-type",
    "--file-extension", "--file-magic", "--filter-magic-label", "--format", "--from", "--glob", "--iglob",
    "--jobs", "--range", "--min-line", "--max-line", "--max-files", "--max-size", "--min-size", "--replace",
    "--context-separator", "--zmax"}))
GREP_VALUE_OPTS = {
    "grep": _GREP_VALUE, "egrep": _GREP_VALUE, "fgrep": _GREP_VALUE,
    "rg": ("efmABCgtTEjMrd", frozenset({
        "--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context", "--glob",
        "--iglob", "--type", "--type-not", "--type-add", "--type-clear", "--encoding", "--threads",
        "--max-columns", "--replace", "--max-depth", "--max-filesize", "--color", "--colors", "--path-separator",
        "--pre", "--pre-glob", "--ignore-file", "--sort", "--sortr", "--engine", "--context-separator",
        "--field-context-separator", "--field-match-separator", "--hostname-bin", "--hyperlink-format",
        "--dfa-size-limit", "--regex-size-limit", "--generate"})),
    "ag": ("GgmpW", frozenset({
        "--max-count", "--file-search-regex", "--filename-pattern", "--depth", "--ignore", "--ignore-dir",
        "--path-to-ignore", "--pager", "--width", "--workers", "--color-line-number", "--color-match",
        "--color-path"})),
}
# Options whose value is a file the tool reads (patterns, ignore lists, a file list).
GREP_FILE_OPTS = ("--file", "--exclude-from", "--include-from", "--from", "--ignore-file", "--ignore-files",
                  "--path-to-ignore")
# Options whose value is a pattern or a scoping glob/type, never a file.
GREP_TEXT_OPTS = {"-e", "--regexp", "--and", "--andnot", "--not", "-N", "--neg-regexp", "--include", "--exclude",
                  "--exclude-dir", "--include-dir", "--glob", "--iglob", "--type", "--type-not", "--type-add",
                  "--file-type", "--file-extension", "--file-search-regex", "--filename-pattern", "--pre-glob",
                  "-g", "-G", "-t", "-T", "-O", "-r", "--replace"}
# Secret basenames a recursive search could print. A scoping glob (grep
# --include, ugrep -g/-O, rg -g/--iglob) is trusted to keep a search out of
# secrets only when none of these can match it and the glob itself (and the
# glob with its wildcards filled in) is no secret name; see _glob_can_match_secret.
SECRET_SAMPLES = (".env", ".env.local", ".env.production", ".env.prod", ".env.dev", ".env.development",
                  ".env.staging", ".env.test", ".env.bak", ".env.backup", "prod.env", "secrets.env", "app.env",
                  "secrets.yaml", "secrets.yml", "secrets.json", "secrets.toml", "secrets.txt", "secrets.ini",
                  "secrets.conf", "secrets.cfg", "secret", "secrets", "credentials", "credentials.json",
                  ".credentials.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa.pub", "server.pem",
                  "tls.key", "store.p12", "vault.pfx", "app.jks", "app.keystore", "vault.kdbx", ".netrc",
                  ".pgpass", ".git-credentials", ".npmrc", ".pypirc", "Training_Module_Feature_Map_and_Memory.txt")
# Named file types (rg -t, ugrep -t) whose globs select no secret basename. Any
# other type (txt, json, yaml, toml, config, text, a --type-add one) is not trusted.
SAFE_SEARCH_TYPES = {"py", "python", "Python", "js", "javascript", "ts", "typescript", "md", "markdown", "rust",
                     "go", "html", "css", "c", "cpp", "c++", "java", "rst", "sql", "svelte", "vue", "php",
                     "ruby", "sh", "shell", "Shell", "lua", "kotlin", "swift", "scala"}
_GLOB_WILD = re.compile(r"\[[^\]]*\]|[*?]")


def _glob_alternatives(glob, limit=64):
    """The alternatives of a glob: `{a,b}` groups expanded (nested), then split
    on commas and whitespace (ugrep -g and some tools take a list). None when
    there are more than `limit` (the caller then fails closed)."""
    out, todo = [], [glob]
    while todo:
        cur = todo.pop()
        start = cur.find("{")
        group = None
        while start != -1 and group is None:
            depth, j, cuts = 0, start, []
            while j < len(cur):
                if cur[j] == "{":
                    depth += 1
                elif cur[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif cur[j] == "," and depth == 1:
                    cuts.append(j)
                j += 1
            if j < len(cur) and cuts:
                bounds = [start] + cuts + [j]
                group = (cur[:start], [cur[a + 1 : b] for a, b in zip(bounds, bounds[1:])], cur[j + 1 :])
            else:
                start = cur.find("{", start + 1)
        if group is None:
            out += [p for p in re.split(r"[,\s]+", cur) if p]
        else:
            pre, alts, post = group
            todo += [pre + a + post for a in alts]
        if len(out) + len(todo) > limit:
            return None
    return out


def _glob_can_match_secret(glob):
    """True when a positive include glob could select a secret file, so a
    recursive search restricted to it could still print one: one of its
    alternatives matches a sample secret basename, is (or, with its wildcards
    filled in, becomes) a secret name or path, or names a secret directory."""
    import fnmatch
    g = glob.strip()
    if not g or g.startswith("!"):  # empty or a negated (exclude) glob: not a positive include
        return False
    alts = _glob_alternatives(g)
    if alts is None:
        return True
    for alt in alts:
        if alt.startswith("!"):
            continue
        base = alt.rstrip("/").rsplit("/", 1)[-1] or alt
        if any(fnmatch.fnmatch(s, base) for s in SECRET_SAMPLES):
            return True
        for cand in {alt, _GLOB_WILD.sub("x", alt), _GLOB_WILD.sub("", alt)}:
            cbase = cand.rstrip("/").rsplit("/", 1)[-1]
            if SECRET_BASENAME.match(cbase) or SECRET_TOKEN_IN_TEXT.search(cand) or SECRET_PATH_PARTS.search(cand):
                return True
        for part in alt.split("/")[:-1]:  # a directory part such as secrets/ or .runtime/
            if part not in ("", ".", "*", "**") and any(fnmatch.fnmatch(d, part) for d in ("secrets", "secret", ".runtime")):
                return True
    return False


def parse_search(cmd, args):
    """Read a grep/rg/ag command line into (values, flags, operands): `values`
    is [(option, value)] for each value option (`-e P`, `-eP`, `--file=F`,
    `-rnA3`), `flags` the other options, `operands` the rest. Options end at
    `--`; a short bundle is read letter by letter, a value letter taking the
    rest of the bundle or the next word (see GREP_VALUE_OPTS)."""
    shorts, longs = GREP_VALUE_OPTS.get(cmd, _GREP_VALUE)
    values, flags, operands, i, n = [], [], [], 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            operands += args[i + 1 :]
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if eq:
                values.append((name, val))
            elif name in longs and i + 1 < n:
                values.append((name, args[i + 1]))
                i += 1
            else:
                flags.append(name)
        elif a.startswith("-") and a != "-":
            for j in range(1, len(a)):
                if a[j] in shorts:
                    if j + 1 < len(a):
                        values.append(("-" + a[j], a[j + 1 :]))
                    elif i + 1 < n:
                        values.append(("-" + a[j], args[i + 1]))
                        i += 1
                    else:
                        flags.append("-" + a[j])
                    break
                flags.append("-" + a[j])
        else:
            operands.append(a)
        i += 1
    return values, flags, operands


def _long_prefix(opt, longs, known=frozenset()):
    """True when `opt` is one of `longs`, or an abbreviation of one (GNU
    getopt accepts any unambiguous prefix) that is not itself a `known` option
    (--exclude is not short for --exclude-from)."""
    return opt in longs or (opt.startswith("--") and len(opt) > 3 and opt not in known
                            and any(x.startswith(opt) for x in longs))


def _search_scoped_off_secrets(cmd, values):
    """True when include globs / file types restrict the search to names that
    cannot match a secret basename (so a recursive search reads no secret file
    by name; a secret DIRECTORY under the root is checked per root, see
    _tree_may_hold_secret_dir). False when there is no such restriction, or one
    the guard cannot evaluate (fail-closed)."""
    globs, types = [], []
    for o, v in values:
        if o in ("--include", "--glob", "--iglob") or (o == "-g" and cmd != "ag"):
            globs.append(v)
        elif o in ("-O", "--file-extension") and cmd not in ("rg", "ag"):
            globs += ["*." + e for e in v.split(",")]
        elif o in ("-t", "--type", "--file-type") and cmd != "ag":
            types += v.split(",")
        elif o in ("--type-add", "-M", "--file-magic", "--pre", "--pre-glob"):
            return False  # a custom type, content magic or a preprocessor: not a name restriction
    positives = [g for g in globs if g.strip() and not g.strip().startswith("!")]
    if not positives and not types:
        return False
    if any(_glob_can_match_secret(g) for g in positives):
        return False
    return all(t in SAFE_SEARCH_TYPES for t in types)


# Directories a tree walk never descends: VCS data, dependencies and caches.
_TREE_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "site-packages", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", ".next", ".cache"}


def _tree_may_hold_secret_dir(root, max_depth=4, max_dirs=4000):
    """True when a recursive search rooted at `root` may reach files that are
    secret whatever their names (a SECRET_DIRS entry, a `secrets/` directory,
    `.runtime/secrets*`), so a name glob cannot keep the search off them. Walks
    `max_depth` levels below the root, skipping VCS, dependency and cache trees;
    a walk that reaches `max_dirs` directories counts as True (fail-closed)."""
    if root is None:
        return True
    if any(under(d, root) or under(root, d) for d in SECRET_DIRS):
        return True
    if not os.path.isdir(root):
        return False
    stack, visited = [(root, 0)], 0
    while stack:
        d, depth = stack.pop()
        visited += 1
        if visited > max_dirs:
            return True
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        runtime = os.path.basename(d) == ".runtime"
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                if (e.name == "secrets" and e.is_dir()) or (runtime and e.name.startswith("secrets") and (is_dir or not SECRET_BASENAME.match(e.name))):
                    # every file in it is secret whatever its name (SECRET_PATH_PARTS);
                    # a secret BASENAME elsewhere is _glob_can_match_secret's job
                    return True
            except OSError:
                return True
            if is_dir and depth < max_depth and e.name not in _TREE_SKIP:
                stack.append((e.path, depth + 1))
    return False


def _looks_secret(val, cwd):
    p = expand_path(val, cwd)
    if p and is_secret_path(p):
        return True
    if any(c in val for c in "*?[") and SECRET_GLOB.search(val):
        return True
    if p is None and SECRET_TOKEN_IN_TEXT.search(val):
        return True
    return False


def _names_existing_secret(val, cwd):
    """True when `val`, read as a path (or, with glob characters, as the files
    the shell would expand it to), names a secret file that exists."""
    import glob as globmod
    p = expand_path(val, cwd)
    if not p:
        return False
    if os.path.lexists(p) and is_secret_path(p):
        return True
    if any(c in val for c in "*?["):
        try:
            hits = globmod.glob(p)[:64]
        except Exception:
            return True  # fail-closed
        return any(is_secret_path(h) for h in hits)
    return False


def check_secret_args(cmd, args, ctx):
    """Block reading secret files. Listing, testing, hashing and writing are fine."""
    if cmd in GREP_FAMILY:
        return check_grep_reads(cmd, args, ctx)
    if cmd in {"ls", "stat", "test", "[", "[[", "du", "find", "touch", "chmod", "mkdir", "rm", "file", "md5sum",
               "sha256sum", "sha1sum", "realpath", "readlink", "basename", "dirname", "echo"}:
        return
    if cmd == "git" and args[:1] and args[0] in ("check-ignore", "ls-files", "status", "rm", "add", "commit", "log"):
        return
    if cmd == "docker" and args[:1] == ["compose"] and "config" not in args:
        return  # --env-file is consumed, not printed
    has_t = cmd in ("cp", "install", "mv") and ("-t" in args or any(a.startswith("--target-directory") for a in args))
    for idx, a in enumerate(args):
        val = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
        if cmd in ("cp", "install", "mv") and not has_t and idx == len(args) - 1:
            continue  # destination (with -t the last positional is a source, not the destination)
        if val.startswith("-"):
            continue
        if _looks_secret(val, ctx.cwd):
            ctx.deny(f"reads a secret file ({val}); secrets must never enter the transcript, logs or commits (§3.3)")


def _dir_holds_secret(p):
    """True when directory `p` directly contains an untracked secret file."""
    try:
        for name in os.listdir(p):
            if SECRET_BASENAME.match(name) and not is_tracked(os.path.join(p, name)):
                return True
    except OSError:
        return False
    return False


def recursive_secret_root(p):
    """True when a recursive read rooted at `p` would descend into a tree that
    holds untracked secrets: the production checkout or $HOME (or any ancestor of
    them), the autopilot home outside its agent sandbox, a secret directory (or
    an ancestor of one, e.g. ~/.config), or a production subtree that holds a
    secret basename (e.g. .runtime)."""
    if p is None:
        return False
    agent = _real(os.path.join(AUTOPILOT_HOME, "agent"))
    if under(R_PROD, p) or under(_real(HOME), p):  # p is, or is an ancestor of, prod/$HOME
        return True
    if under(p, AUTOPILOT_HOME) and not under(p, agent):
        return True
    for d in SECRET_DIRS + [_real(os.path.join(HOME, ".config"))]:
        if under(p, d) or under(d, p):  # inside a secret dir, or an ancestor of one
            return True
    if under(p, R_PROD) and (os.path.basename(p) == ".runtime" or SECRET_PATH_PARTS.search(p) or _dir_holds_secret(p)):
        return True
    return False


def check_grep_reads(cmd, args, ctx):
    """grep/rg/ag, read with parse_search(): the first operand is the PATTERN
    unless an option supplies it (-e/-f, rg --files, ag -g); the other operands
    are paths. Reject reading a secret file (an operand, or the file named by
    -f/--exclude-from/--from/--ignore-file), and a RECURSIVE search rooted at a
    tree that holds untracked secrets (the production checkout, $HOME,
    ~/.config, the autopilot home, a secret directory). With NO path operand a
    recursive search covers the cwd (rg/ag always recurse), so check the cwd
    too. An --include / -g / -t that cannot match a secret basename scopes it
    off secrets, unless the root may hold a secret directory. Backstop: the
    pattern word, and any other option value, that names an existing secret
    file is refused too, so a misread option never moves a file out of review."""
    values, flags, operands = parse_search(cmd, args)
    opts = flags + [o for o, _v in values]
    known = GREP_VALUE_OPTS.get(cmd, _GREP_VALUE)[1] | GREP_TEXT_OPTS
    grep_like = cmd not in ("rg", "ag")
    recursive = not grep_like  # ripgrep and the silver searcher recurse by default
    for o in flags:
        if (grep_like and o in ("-r", "-R")) or _long_prefix(o, ("--recursive", "--dereference-recursive")):
            recursive = True
        elif grep_like and re.fullmatch(r"-\d", o):  # ugrep -NUM searches NUM levels deep
            recursive = True
    for o, v in values:
        if grep_like and (o == "-d" or _long_prefix(o, ("--directories",))) and not re.fullmatch(r"rea(d)?|s(k(ip?)?)?", v):
            recursive = True
        elif grep_like and _long_prefix(o, ("--depth", "--recursive", "--dereference-recursive")):
            recursive = True
    pat_from_opt = any((o in ("-e", "-f") and cmd != "ag") or _long_prefix(o, ("--regexp", "--file")) for o in opts) \
        or (cmd == "ag" and any(o == "-g" or _long_prefix(o, ("--filename-pattern",)) for o in opts)) \
        or (cmd == "rg" and any(o in ("--files", "--type-list") for o in opts))
    files = operands if pat_from_opt else operands[1:]
    pattern = None if pat_from_opt or not operands else operands[0]
    for o, v in values:
        if (o == "-f" and cmd != "ag") or (o == "-p" and cmd == "ag") or _long_prefix(o, GREP_FILE_OPTS, known):
            if _looks_secret(v, ctx.cwd):
                ctx.deny(f"reads a secret file ({v}); secrets must never enter the transcript, logs or commits (§3.3)")
        elif o not in GREP_TEXT_OPTS and _names_existing_secret(v, ctx.cwd):
            ctx.deny(f"reads a secret file ({v}); secrets must never enter the transcript, logs or commits (§3.3)")
    if pattern is not None and _names_existing_secret(pattern, ctx.cwd):
        ctx.deny(f"'{pattern}' names a secret file; a search must never read one (§3.3)")
    scoped = _search_scoped_off_secrets(cmd, values)

    def reaches_secrets(root):
        return recursive_secret_root(root) and not (scoped and not _tree_may_hold_secret_dir(root))

    for val in files:
        if _looks_secret(val, ctx.cwd):
            ctx.deny(f"reads a secret file ({val}); secrets must never enter the transcript, logs or commits (§3.3)")
        if recursive and reaches_secrets(expand_path(val, ctx.cwd)):
            ctx.deny(f"a recursive search rooted at {val} would read the untracked .env / .runtime / credential files under it; scope the search to a subdirectory that holds no secrets, or restrict it with --include/-g/-t to names that are not secrets (§3.3)")
    if not files and recursive and reaches_secrets(_real(ctx.cwd or HOME)):
        ctx.deny(f"a recursive search with no path searches the working directory {ctx.cwd}, which would read the untracked .env / .runtime / credential files under it; search a subdirectory that holds no secrets, or restrict it with --include/-g/-t to names that are not secrets (§3.3)")


WRITE_ALL_ARGS = {"rm", "rmdir", "unlink", "shred", "truncate", "touch", "mkdir", "mv", "tee", "setfacl", "mkfifo", "srm"}
WRITE_SKIP_FIRST = {"chmod", "chown", "chgrp"}
WRITE_LAST_ARG = {"cp", "install", "rsync", "scp", "ln"}
DESTRUCTIVE = {"rm", "rmdir", "unlink", "shred", "truncate", "mv", "chmod", "chown", "chgrp", "dd", "ln", "cp", "rsync", "tee", "find"}


LINK_OR_COPY = {"cp", "mv", "install", "ln", "rsync", "link"}


def _tar_extract_info(args):
    """(is_extraction, change-directory) for a tar command, reading -x/--extract/
    --get and the bundled 'x' flag, and -C/--directory, from ANY argument."""
    extract, cdir, i = False, None, 0
    while i < len(args):
        a = args[i]
        if a in ("--extract", "--get"):
            extract = True
        elif a == "--directory" and i + 1 < len(args):
            cdir = args[i + 1]; i += 1
        elif a.startswith("--directory="):
            cdir = a.split("=", 1)[1]
        elif a == "-C" and i + 1 < len(args):
            cdir = args[i + 1]; i += 1
        elif a.startswith("-C") and len(a) > 2 and not a.startswith("--"):
            cdir = a[2:]
        elif not a.startswith("--") and "x" in a.lstrip("-") and (a.startswith("-") or i == 0):
            extract = True  # -x, -xzf, or the classic no-dash first word 'xf'
        i += 1
    return extract, cdir


def check_copy_link_targets(cmd, args, ctx):
    """cp/mv/install/ln/rsync: close two ways a guard or secret file is reached
    that checking only the destination directory misses (P0-17).
      - A DIRECTORY destination: the file actually written is join(dest, basename)
        for each source, so a copy into ~/.llm-autopilot/agent/ would land on
        test-db.vars even though agent/ itself is writable.
      - A HARD link (ln without -s, cp -l, rsync --link-dest) to a guard or secret
        file shares its inode, so later edits through the link change the original."""
    na = nonopt(args)
    hard = cmd == "link" \
        or (cmd == "ln" and not any(a in ("-s", "--symbolic") or (a.startswith("-") and not a.startswith("--") and "s" in a[1:]) for a in args)) \
        or (cmd == "cp" and any(a in ("-l", "--link") or (a.startswith("-") and not a.startswith("--") and "l" in a[1:]) for a in args)) \
        or (cmd == "rsync" and any(a.startswith("--link-dest") for a in args))
    tdir = None
    for i, a in enumerate(args):
        if a in ("-t", "--target-directory") and i + 1 < len(args):
            tdir = args[i + 1]
        elif a.startswith("--target-directory="):
            tdir = a.split("=", 1)[1]
    if tdir is not None:
        dest, sources = tdir, list(na)
    elif len(na) >= 2:
        dest, sources = na[-1], na[:-1]
    else:
        return
    sources = [s for s in sources if s != dest and not re.match(r"^[\w.@-]+:", s)]  # drop remote host:path
    if hard:
        for s in sources:
            sp = expand_path(s, ctx.cwd)
            if sp and (is_guard_path(sp) or is_secret_path(sp)):
                ctx.deny("a hard link to a guard file or a secret file shares its inode, so a later edit through the link would change the protected file; copy the contents into your worktree instead (§3.3/P0-17)")
    if re.match(r"^[\w.@-]+:", dest):  # remote destination: out of scope here
        return
    dp = expand_path(dest, ctx.cwd)
    if dp is None:
        return
    # A CONTENTS copy writes the source directory's own entries into the
    # destination (dest/<name>), not a single dest/<basename-of-src>: an rsync
    # trailing-slash source, cp -T/--no-target-directory, a 'src/.' source, or a
    # glob source. The guard cannot enumerate the incoming names, so if the
    # destination is or holds a guard file it refuses the whole copy (P0-17).
    no_target_dir = any(a in ("-T", "--no-target-directory") or (a.startswith("-") and not a.startswith("--") and "T" in a[1:]) for a in args) and cmd in ("cp", "mv", "install")
    contents_copy = no_target_dir \
        or (cmd == "rsync" and any(s.endswith("/") for s in sources)) \
        or any(s.rstrip("/").endswith("/.") or re.search(r"[*?\[]", s) for s in sources)
    if contents_copy and (is_guard_path(dp) or holds_guard_file(dp)):
        ctx.deny("copying a directory's contents into a directory that holds the autopilot's guard files would overwrite guard/, bin/, agent/test-db.vars or the CI approvals and disable the guardrails (§3.3/P0-17); operate only on files the autopilot created")
    if tdir is not None or dest.endswith("/") or os.path.isdir(dp):
        for s in sources:
            cand = os.path.join(dp, os.path.basename(s.rstrip("/")) or ".")
            if is_guard_path(cand) or holds_guard_file(cand):
                ctx.deny("writing into a directory that holds the autopilot's guard files would overwrite guard/, bin/, agent/test-db.vars or the CI approvals and disable the guardrails (§3.3/P0-17); operate only on files the autopilot created")
            if is_secret_path(cand):
                ctx.deny(f"this would overwrite the secret file {os.path.basename(s)} (§3.3)")


def check_write_targets(cmd, args, ctx):
    if cmd in LINK_OR_COPY:
        check_copy_link_targets(cmd, args, ctx)
    targets = []
    if cmd in WRITE_ALL_ARGS:
        targets = nonopt(args)
        if cmd == "truncate" and "-s" in args:
            i = args.index("-s")
            targets = [t for t in targets if t != (args[i + 1] if i + 1 < len(args) else None)]
    elif cmd in WRITE_SKIP_FIRST:
        targets = nonopt(args)[1:]
    elif cmd in WRITE_LAST_ARG:
        na = nonopt(args)
        if "-t" in args:
            i = args.index("-t")
            targets = args[i + 1 : i + 2]
        elif na:
            targets = na[-1:]
        if cmd == "rsync" and any(a == "--remove-source-files" for a in args):
            # rsync removes the source files too: the sources are delete targets.
            targets = targets + [t for t in na[:-1] if not re.match(r"^[\w.@-]+:", t)]
        if cmd in ("scp", "rsync"):
            targets = [t for t in targets if not re.match(r"^[\w.@-]+:", t)]
    elif cmd == "dd":
        targets = [a[3:] for a in args if a.startswith("of=")]
    elif cmd in ("sed", "perl") and any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") or a == "-pi" for a in args):
        targets = nonopt(args)[1:]
    elif cmd == "tar" and _tar_extract_info(args)[0]:
        cdir = _tar_extract_info(args)[1]
        targets = [cdir] if cdir else ["."]
    elif cmd == "unzip":
        targets = [args[args.index("-d") + 1]] if "-d" in args and args.index("-d") + 1 < len(args) else ["."]
    elif cmd in ("curl", "wget"):
        for flag in ("-o", "--output", "-O", "--output-document", "-P", "--directory-prefix"):
            if flag in args:
                i = args.index(flag)
                if flag == "-O" and cmd == "curl":
                    targets.append(".")
                elif i + 1 < len(args):
                    targets.append(args[i + 1])
        if cmd == "wget" and not targets:
            targets.append(".")
    elif cmd == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args):
        roots = []
        for a in args:
            if a.startswith("-") or a in ("(", "!"):
                break
            roots.append(a)
        targets = roots or ["."]
    elif cmd == "gio" and "trash" in args:
        targets = [a for a in args[args.index("trash") + 1 :] if not a.startswith("-")]
    elif cmd == "patch":
        targets = ["."]
    elif cmd in ("make", "cmake", "ninja", "cargo", "go", "mvn", "gradle"):
        targets = ["."]
    move_or_delete = cmd in ("rm", "rmdir", "unlink", "shred", "srm", "mv", "find", "gio") \
        or (cmd == "rsync" and any(a.startswith("--delete") or a == "--remove-source-files" for a in args))
    # Extracting an archive into a directory that holds a guard file can overwrite
    # it (P0-17), just like a copy into that directory.
    extract = (cmd == "tar" and _tar_extract_info(args)[0]) or cmd == "unzip"
    for t in targets:
        if move_or_delete and re.search(r"[*?\[{]", t):
            gp = _glob_parent(t, ctx.cwd)
            if gp and holds_guard_file(gp):
                ctx.deny("a glob or brace expansion here would delete or move the autopilot's guard files (guard/, bin/, agent/test-db.vars, CI approvals, ...), disabling the guardrails (§3.3/P0-17); name the individual files you created")
        p = expand_path(t, ctx.cwd)
        if p is None:
            if cmd in DESTRUCTIVE:
                ctx.deny(f"'{t}' is computed at run time; destructive commands need literal paths")
            continue
        if (move_or_delete or extract) and holds_guard_file(p):
            ctx.deny("moving into, extracting into, or deleting a directory that holds the autopilot's guard files (guard/, bin/, agent/, test-db.vars, CI approvals, ...) would disable the guardrails (§3.3/P0-17); operate only on individual files the autopilot created")
        if cmd in ("rm", "rmdir", "shred") and (p in BROAD_DELETE or re.fullmatch(r"/(var/)?tmp/\*", t) or t.rstrip("/") in ("/tmp/*", "/tmp/.*", "~/*", "*", ".*", "/*")):
            ctx.deny(f"refuses the broad delete of {t}; delete only paths the autopilot created")
        if not write_allowed(p):
            ctx.deny(write_why(p))


def _glob_parent(t, cwd):
    """The directory a glob/brace target lives in: the literal prefix up to the
    first metacharacter. Used to see whether an expansion would reach a guard file."""
    m = re.search(r"[*?\[{]", t)
    if not m:
        return None
    prefix = t[: m.start()]
    d = prefix if prefix.endswith("/") else os.path.dirname(prefix)
    return expand_path(d or ".", cwd)


def check_disk(cmd, args, ctx):
    heavy = cmd in ("docker", "npm", "pnpm", "yarn", "pip", "pip3", "uv", "tar", "unzip", "rsync", "cp", "wget", "dd", "git") \
        and (cmd != "git" or args[:1] in (["clone"], ["worktree"])) \
        and (cmd != "docker" or bool(set(args[:2]) & {"build", "run", "pull", "compose", "create", "buildx"}))
    if not heavy:
        return
    frac = free_fraction("/")
    if frac < MIN_FREE_FRACTION:
        ctx.deny(f"only {frac:.0%} of / is free; §3.3 keeps at least 15% free. Clean up what the autopilot created (manifest) or add a NEEDS_HUMAN.md item")


# --------------------------------------------------------------------------
# git / gh
# --------------------------------------------------------------------------

GIT_DANGEROUS_C = re.compile(
    r"^(core\.(sshCommand|hooksPath|fsmonitor|pager|editor|askpass|gitProxy|alternateRefsCommand|worktree|bare)|alias\.|url\.|credential|"
    r"http\.|protocol\.|include\.|includeIf\.|filter\.|diff\.external|diff\.\w+\.(textconv|command)|merge\.\w+\.driver|pager\.|"
    r"sequence\.editor|gpg\.|interactive\.diffFilter|uploadpack|receive\.|remote\.|push\.|branch\.|transfer\.|submodule\.)",
    re.I,
)
# Plumbing that can update remote refs without `git push`'s refspec checks.
GIT_TRANSPORT_SUBCOMMANDS = {"send-pack", "http-push", "receive-pack", "upload-pack", "upload-archive", "http-fetch",
                             "remote-http", "remote-https", "remote-ext", "remote-fd", "remote-ftp", "remote-ftps", "subtree"}
# Dangerous `git push` long options, as full names. git accepts any unambiguous
# prefix (--dele -> --delete, --tag -> --tags), so the guard matches prefixes too.
GIT_PUSH_DANGEROUS_LONG = ("delete", "all", "tags", "prune", "mirror", "force", "follow-tags", "force-with-lease", "force-if-includes")
# git read subcommands that can print a file's contents (and so a secret file).
GIT_READ_FILE_SUBS = {"show", "cat-file", "log", "diff", "grep", "blame", "annotate", "difftool", "whatchanged"}


def check_git(args, ctx):
    a = list(args)
    gdir = ctx.cwd
    while a and a[0].startswith("-"):
        opt = a.pop(0)
        if opt == "-C" and a:
            gdir = expand_path(a.pop(0), ctx.cwd) or gdir
        elif opt == "-c" and a:
            kv = a.pop(0)
            if GIT_DANGEROUS_C.match(kv):
                ctx.deny("git -c overrides of hooks, transports, remotes, push mappings, credentials, aliases or filters can run hidden commands or move other branches")
        elif opt == "--config-env" or opt.startswith("--config-env="):
            kv = opt.split("=", 1)[1] if "=" in opt else (a.pop(0) if a else "")
            if GIT_DANGEROUS_C.match(kv):
                ctx.deny("git --config-env overrides of hooks, transports, remotes, push mappings, credentials, aliases or filters can run hidden commands or move other branches")
        elif opt in ("--git-dir", "--work-tree", "--namespace", "--exec-path") and a:
            a.pop(0)
            ctx.deny("redirecting git to another repository or tree is not allowed")
        elif opt.startswith(("--git-dir=", "--work-tree=", "--exec-path=")):
            ctx.deny("redirecting git to another repository or tree is not allowed")
    if not a:
        return
    sub, rest = a[0], a[1:]
    if sub in GIT_TRANSPORT_SUBCOMMANDS or sub.startswith("remote-"):
        ctx.deny(f"'git {sub}' talks to remotes without git push's checks; push one named branch with git push origin <branch>")
    if sub == "grep" and any(x.startswith(("-O", "--open-files-in-pager")) for x in rest):
        ctx.deny("git grep -O runs a program on the matches; print them instead")
    if sub == "grep" and any(x in ("--no-index", "--untracked") for x in rest):
        # git grep --no-index / --untracked searches untracked (and, with
        # --no-index, ignored) files under the pathspec roots, or the cwd when
        # none is given, so it can print a secret file just like grep -r.
        # Only args after '--' are pathspecs; without one, the lone positional is
        # the PATTERN and the search covers the whole working directory.
        pathspecs = rest[rest.index("--") + 1 :] if "--" in rest else []
        roots = pathspecs or [ctx.cwd or HOME]
        for val in roots:
            p = expand_path(val, gdir or ctx.cwd)
            if p and is_secret_path(p):
                ctx.deny(f"reads a secret file ({val}); secrets must never enter the transcript (§3.3)")
            if recursive_secret_root(p if pathspecs else _real(gdir or ctx.cwd or HOME)):
                ctx.deny(f"'git grep {'--untracked' if '--untracked' in rest else '--no-index'}' would read the untracked .env / .runtime / credential files under {val}; search a subdirectory that holds no secrets (§3.3)")
    if sub in GIT_READ_FILE_SUBS:
        # git show HEAD:.env, git cat-file -p HEAD:.env, git log -p -- .runtime/secrets.env,
        # git diff --no-index /dev/null .env: all print a secret file's contents.
        for x in rest:
            if x.startswith("-"):
                continue
            # A `REV:path` argument names committed content, so only the path
            # after the last ':' is a candidate (the whole string is never a
            # worktree file, and testing it would lose the tracked-file exemption
            # for a tracked fixture such as e2e/ci/ci.env).
            cand = x.rsplit(":", 1)[-1] if ":" in x else x
            p = expand_path(cand, gdir or ctx.cwd)
            if p and is_secret_path(p):
                ctx.deny(f"'git {sub}' would print the contents of the secret file {cand}; secrets must never enter the transcript (§3.3)")
    for j, x in enumerate(rest):
        out = x.split("=", 1)[1] if x.startswith("--output=") else (rest[j + 1] if x == "--output" and j + 1 < len(rest) else None)
        if out is not None:
            p = expand_path(out, ctx.cwd)
            if not write_allowed(p):
                ctx.deny(write_why(p))
    gdir_real = _real(gdir) if gdir else None
    protected_tree = gdir_real is not None and not write_allowed(gdir_real)
    readonly = sub in GIT_READONLY
    if sub == "branch":
        mutating = set(rest) & {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force", "--set-upstream-to", "-u", "--unset-upstream", "--edit-description"}
        if mutating:
            readonly = False
            if mutating & {"-d", "-D", "--delete", "-m", "-M", "--move", "-f", "--force"}:
                for nm in nonopt(rest):
                    if not re.match(r"^(upgrade|autopilot|llmdev)/", nm):
                        ctx.deny("may only delete, rename or force-reset branches the autopilot created (upgrade/*, autopilot/*, llmdev/*)")
        elif nonopt(rest):
            readonly = False  # creates a branch (refs are shared but harmless)
    if sub == "tag":
        if set(rest) & {"-d", "--delete", "-f", "--force"}:
            ctx.deny("deleting or moving tags is not allowed; tags are rollback points")
        if nonopt(rest) and not set(rest) & {"-l", "--list", "-n", "--contains", "--points-at", "--merged", "--no-merged", "-v", "--verify"}:
            readonly = False
    if sub == "worktree":
        op = rest[0] if rest else "list"
        if op == "prune":
            ctx.deny("'git worktree prune' acts on every worktree of the shared repository; remove only your own worktree by path")
        if op in ("remove", "move", "add", "lock", "unlock", "repair"):
            readonly = False
            paths = [x for x in rest[1:] if not x.startswith("-")]
            if paths:
                p = expand_path(paths[0], ctx.cwd)
                if op == "add" and (p is None or not under(p, R_WORK)):
                    ctx.deny(f"new worktrees go under {WORK}, outside every production-mounted path")
                if op in ("remove", "move", "lock", "unlock", "repair") and (p is None or not under(p, R_WORK) or p == R_DEV_WORKTREE):
                    ctx.deny(f"may only {op} its own worktrees under {WORK} (never {DEV_WORKTREE} itself)")
    if sub == "config":
        positional = [x for x in rest if not x.startswith("-")]
        setting = bool(set(rest) & {"--unset", "--unset-all", "--add", "--replace-all", "--remove-section", "--rename-section", "-e", "--edit"}) \
            or (len(positional) >= 2 and not set(rest) & {"--get", "--get-all", "--get-regexp", "-l", "--list", "--get-urlmatch"})
        if setting:
            readonly = False
            if set(rest) & {"--global", "--system", "--file", "-f"}:
                ctx.deny("global, system and file git config belong to the operator")
            key = positional[0] if positional else ""
            if not re.match(r"^branch\.(upgrade|autopilot|llmdev)/", key):
                ctx.deny("the repository's git config is shared with the production checkout and the operator's worktrees; only branch.upgrade/*, branch.autopilot/* and branch.llmdev/* keys may change")
    if sub == "remote" and rest and rest[0] in ("add", "remove", "rm", "rename", "set-url", "set-head", "set-branches", "prune", "update"):
        ctx.deny("remotes are fixed; push only to origin")
    if sub == "stash" and (not rest or rest[0] not in ("list", "show")):
        ctx.deny("the stash is shared by every worktree of this repository and agents pop each other's entries; use a WIP commit on a branch or 'git diff > /tmp/x.diff'")
    if sub == "reflog" and rest and rest[0] in ("expire", "delete"):
        ctx.deny("the reflog is the recovery path for every worktree; never expire it")
    if sub == "gc" and any(x.startswith("--prune") for x in rest):
        ctx.deny("pruning objects can destroy other worktrees' recoverable work")
    if sub in ("prune", "filter-branch", "filter-repo", "replace"):
        ctx.deny("rewriting or pruning shared history is not allowed (§3.3)")
    if sub == "update-ref":
        if re.search(r"refs/heads/(main|master|dev)\b|refs/remotes/|refs/tags/|(^|\s)-d\b", " ".join(rest)) or not any(re.match(r"^refs/heads/(upgrade|autopilot|llmdev)/", x) for x in rest):
            ctx.deny("update-ref may only move the autopilot's own branches")
        readonly = False
    if sub == "add" and set(rest) & {"-f", "--force"}:
        ctx.deny("force-adding ignored files risks committing secrets, data or weights; fix .gitignore or rename instead")
    if sub == "symbolic-ref" and len(nonopt(rest)) >= 2:
        readonly = False
    if sub == "fetch" and any(":" in x and not x.startswith("-") for x in rest):
        for x in rest:
            if ":" in x and re.search(r":(refs/heads/)?(main|master|dev)$", x.lstrip("+")):
                ctx.deny("fetching into local main/dev changes refs the production checkout and the operator's worktree use")
    if sub == "push":
        readonly = False
        check_git_push(rest, gdir, ctx)
    if sub == "clone":
        readonly = False
        dest = nonopt(rest)[1:2]
        if dest:
            p = expand_path(dest[0], ctx.cwd)
            if not write_allowed(p):
                ctx.deny(write_why(p))
    if protected_tree and not readonly:
        ctx.deny(write_why(gdir_real))


def check_git_push(rest, gdir, ctx):
    # HOME/XDG_CONFIG_HOME relocate where git reads its global config, which can
    # carry remote.origin.push or url.*.pushInsteadOf and remap the push. (The
    # GIT_CONFIG* variables are refused for every command by EXEC_ENV.)
    for k in ("HOME", "XDG_CONFIG_HOME"):
        v = ctx.env.get(k)
        if not v:
            continue
        # Setting HOME to its own value (env -i HOME="$HOME" ...) does not relocate
        # anything; only a DIFFERENT home redirects git's global config.
        if k == "HOME" and (v in ("$HOME", "${HOME}") or expand_path(v, ctx.cwd) == _real(HOME)):
            continue
        ctx.deny(f"{k} set in front of git push relocates git's global config, which can remap the push to another branch (remote.origin.push, url.*.pushInsteadOf); push with the normal environment")
    for x in rest:
        # git accepts any unambiguous prefix of a long option (--dele -> --delete,
        # --tag -> --tags), so match prefixes of the dangerous ones too.
        long_name = x[2:].split("=", 1)[0] if x.startswith("--") else ""
        abbrev = long_name and any(d.startswith(long_name) for d in GIT_PUSH_DANGEROUS_LONG)
        if x in ("-f", "--force", "--mirror", "--all", "--prune", "--delete", "-d", "--tags", "--follow-tags") \
                or abbrev \
                or x.startswith(("--force", "--mirror", "--receive-pack", "--exec", "--repo")) \
                or (x.startswith("--recurse-submodules") and x.split("=", 1)[-1] in ("on-demand", "only", "--recurse-submodules")) \
                or (re.fullmatch(r"-[a-zA-Z]+", x) and re.search(r"[fd]", x[1:])):
            ctx.deny("force/mirror/all/tags pushes, remote deletes and transport overrides are not allowed (§3.3); push one named branch or tag")
    opts_with_value = {"-o", "--push-option", "--signed"}
    pos, skip = [], False
    for x in rest:
        if skip:
            skip = False
            continue
        if x in opts_with_value:
            skip = True
            continue
        if not x.startswith("-"):
            pos.append(x)
    if not pos:
        ctx.deny("name the remote and the refspec explicitly (git push origin upgrade/<ws>/<slug>, or git push origin HEAD:autopilot/dev)")
    if pos[0] != "origin":
        ctx.deny(f"push only to the 'origin' remote ({REPO_SLUG})")
    refspecs = pos[1:]
    if not refspecs:
        ctx.deny("name the refspec explicitly (git push origin upgrade/<ws>/<slug>, or git push origin HEAD:autopilot/dev)")
    sources, i = [], 0
    while i < len(refspecs):
        rs = refspecs[i]
        i += 1
        if "__SUBST__" in rs or "$" in rs or "`" in rs:
            ctx.deny("refspecs must be literal")
        if rs == "tag":  # `git push origin tag <name>` = refs/tags/<name>:refs/tags/<name>
            name = refspecs[i] if i < len(refspecs) else ""
            i += 1
            check_push_destination(f"refs/tags/{name}", ctx)
            sources.append(f"refs/tags/{name}")
            continue
        if rs.startswith("+"):
            ctx.deny("'+' refspecs force-update; not allowed (§3.3)")
        if any(c in rs for c in "*?[\\") or "@{" in rs:
            ctx.deny("glob, pattern and @{...} refspecs can update branches the command does not name; push one named branch (git push origin <branch> or git push origin HEAD:<branch>)")
        src, colon, dst = rs.partition(":")
        if colon and not src:
            ctx.deny("an empty source deletes the remote ref (or pushes every matching branch); not allowed (§3.3)")
        if colon:
            check_push_destination(remote_destination(dst, ctx), ctx)
        else:
            check_push_destination(resolve_push_source(src, gdir, ctx), ctx)
        sources.append(src)
    scan_outgoing(sources, gdir, ctx)


def remote_destination(dst, ctx):
    """The remote ref a push destination names, the way git expands it."""
    if dst in ("HEAD", "@") or dst.startswith(("remotes/", "refs/remotes/")):
        ctx.deny(f"push to an explicit branch (HEAD:<branch>); '{dst}' is not a branch on the remote")
    if dst.startswith("refs/"):
        if not dst.startswith(("refs/heads/", "refs/tags/")):
            ctx.deny("push only branches (refs/heads/...) and tags (refs/tags/...)")
        return dst
    if dst.startswith(("heads/", "tags/")):
        return "refs/" + dst
    return "refs/heads/" + dst


def resolve_push_source(src, gdir, ctx):
    """Where `git push origin <src>` (no destination) lands: the source's full name,
    unless remote.origin.push or push.default=upstream maps it elsewhere."""
    if src in ("HEAD", "@"):
        ctx.deny("name the destination when pushing HEAD (git push origin HEAD:<branch>); the current branch can change before the push runs")
    full = None
    if src.startswith(("refs/heads/", "refs/tags/")):
        full = src
    else:
        try:
            r = subprocess.run(["git", "-C", gdir or DEV_WORKTREE, "rev-parse", "--symbolic-full-name", src],
                               capture_output=True, text=True, timeout=10)
            out = r.stdout.strip().splitlines()
            if r.returncode == 0 and len(out) == 1 and out[0].startswith(("refs/heads/", "refs/tags/")):
                full = out[0]
            elif r.returncode == 0 and out:
                ctx.deny(f"'{src}' is not a local branch or tag; push with an explicit destination (git push origin {src}:<branch>)")
        except Exception:
            ctx.deny("could not resolve the push source (fail-closed)")
        if full is None:  # not created yet: git will name it the way it is written
            full = remote_destination(src, ctx)
    try:
        r = subprocess.run(["git", "-C", gdir or DEV_WORKTREE, "config", "--get-regexp", r"^(remote\.origin\.push|push\.default|branch\..*\.merge)$"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        ctx.deny("could not read the push configuration (fail-closed)")
    # rc 1: none of the keys is set; rc 128: not a repository or unreadable
    # config, where the push itself fails the same way.
    conf = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        conf.setdefault(k.lower(), []).append(v.strip())
    if conf.get("remote.origin.push"):
        ctx.deny("remote.origin.push is configured and remaps pushes; push with an explicit destination (git push origin <src>:refs/heads/<branch>)")
    if (conf.get("push.default") or [""])[-1].lower() in ("upstream", "tracking") and full.startswith("refs/heads/"):
        merge = conf.get(f"branch.{full[len('refs/heads/'):]}.merge".lower())
        if merge:
            return merge[-1]
    return full


def check_push_destination(ref, ctx):
    if ref.startswith("refs/heads/"):
        name = ref[len("refs/heads/"):]
        if name.lower() in ("main", "master"):
            ctx.deny("the agent never pushes to main; the operator releases dev -> main (operator decisions 2 and 5)")
        if name.lower() == "dev":
            ctx.deny(f"dev moves only through the installed gate {MERGE_TO_DEV}, which refuses unless every check passed for that exact commit and FINAL_REPORT.md exists (operator decision 5)")
        if name.lower() == "head" or not name:
            ctx.deny("push to a named branch")
    elif ref.startswith("refs/tags/"):
        if not re.match(r"^refs/tags/(baseline|release|rc)/[\w./-]+$", ref):
            ctx.deny("push only baseline/*, release/* or rc/* tags")
    else:
        ctx.deny("push only branches (refs/heads/...) and tags (refs/tags/...)")


def load_secret_values():
    """Exact values from known secret files, to catch them in outgoing text. Never printed."""
    vals = set()
    cands = [os.path.join(PROD_CHECKOUT, ".env"), os.path.join(PROD_CHECKOUT, ".runtime/secrets.env"),
             os.path.join(DEV_WORKTREE, ".env"), os.path.join(DEV_COMPOSE_DIR, ".env")]
    for f in cands:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
                    if not m:
                        continue
                    k, v = m.group(1), m.group(2).strip().strip("'\"")
                    if len(v) >= 10 and (re.search(r"KEY|TOKEN|SECRET|PASS|PWD|CREDENTIAL|PRIVATE|AUTH|COOKIE|SALT|SIGN|DSN|DATABASE_URL|CONN", k, re.I)
                                         or (len(v) >= 20 and re.search(r"[A-Z]", v) and re.search(r"[a-z]", v) and re.search(r"\d", v))):
                        vals.add(v)
        except OSError:
            continue
    return vals


def scan_text_for_secrets(text, where, ctx, values=None):
    m = SECRET_SHAPES.search(text)
    if m:
        ctx.deny(f"{where} contains a credential-shaped string ({m.group(0)[:6]}… redacted). The repository is PUBLIC; remove it before pushing")
    for v in values if values is not None else load_secret_values():
        if v in text:
            ctx.deny(f"{where} contains the value of a production secret (redacted). The repository is PUBLIC; remove it before pushing")


def scan_outgoing(sources, gdir, ctx):
    """Scan commits that the push would publish for secrets, weights and dumps.

    One overall wall-clock budget (SCAN_DEADLINE_S) governs the whole scan and
    fails CLOSED when it runs out, so the scan can never outlast the hook timeout
    and let the push through by timing out (P0-17)."""
    deadline = time.monotonic() + SCAN_DEADLINE_S

    def budget(reserve=0.5):
        left = deadline - time.monotonic()
        if left <= reserve:
            ctx.deny("the outgoing-commit secret scan ran out of its time budget; push smaller batches so the scan can finish before the hook deadline (fail-closed)")
        return left

    values = load_secret_values()
    for src in sources:
        ref = src or "HEAD"
        try:
            names = subprocess.run(["git", "-C", gdir or DEV_WORKTREE, "log", "--no-color", "--format=", "--name-status", "--no-renames", ref, "--not", "--remotes=origin"],
                                   capture_output=True, text=True, timeout=min(20, budget()))
            for line in names.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) == 2 and parts[0] in ("A", "M"):
                    f = parts[1]
                    if re.search(r"\.(safetensors|gguf|ckpt|pt|pth|onnx|bin|npz|npy|parquet|sqlite3?|db|dump|sql\.gz|pgdump|tar|tar\.gz|tgz|zip|7z|whl|mp3|wav|mp4|mov|mkv|flac|m4a)$", f, re.I) \
                            or re.match(r"^(data|backups|finetune|\.runtime|models?)/", f) or SECRET_BASENAME.match(os.path.basename(f)) and not re.search(r"\.example$", f):
                        if not re.match(r"^(e2e/ci/ci\.env|launcher/tests/fixtures/.*|\.env\.example)$", f):
                            ctx.deny(f"an outgoing commit adds {f} (weights, data, dumps, archives, media or secret-like files never go to the PUBLIC repo)")
            sizes = subprocess.run(["git", "-C", gdir or DEV_WORKTREE, "log", "--no-color", "-p", "--no-ext-diff", "--no-textconv", "--format=commit %h", ref, "--not", "--remotes=origin"],
                                   capture_output=True, text=True, timeout=min(30, budget()))
        except subprocess.TimeoutExpired:
            ctx.deny("the outgoing-commit secret scan timed out; push smaller batches (fail-closed)")
        budget()
        out = sizes.stdout
        if len(out) > 60_000_000:
            ctx.deny("the outgoing diff is over 60 MB; something large is being published")
        added = "\n".join(l[1:] for l in out.splitlines() if l.startswith("+") and not l.startswith("+++"))
        scan_text_for_secrets(added, "an outgoing commit", ctx, values)


GH_CONFIG_WHY = "repository, account and CLI configuration belong to the operator"
# Top-level gh commands. None = every subcommand is allowed (subject to the
# checks below); a set = only those subcommands; missing = denied, which also
# covers aliases and extensions the guard cannot see into.
GH_ALLOWED = {
    "pr": None, "issue": None, "label": None, "project": None, "org": None, "search": None, "status": None,
    "browse": None, "completion": None, "help": None, "version": None, "api": None,
    "repo": {"view", "list", "ls", "clone"},
    "release": {"view", "list", "ls", "download", "create", "new"},
    "run": {"list", "ls", "view", "watch", "download", "rerun"},
    "workflow": {"list", "ls", "view"},
    "auth": {"status"},
}
GH_DENIED_WHY = {
    "workflow": "workflow_dispatch can deploy production; the agent never triggers, enables or disables workflows",
    "run": "cancelling or deleting runs can cancel a production deploy or erase evidence",
    "repo": "repository settings belong to the operator",
    "release": "existing releases are rollback records",
    "auth": "never print or change GitHub credentials",
    "gist": "gists publish data outside the repository",
}
GH_ROOT_BOOL = {"-h", "--help", "--version"}


def gh_command_words(args):
    """The command path cobra resolves: a flag without '=' takes the next word
    unless it is a known boolean flag ('gh -R x pr merge' is 'pr merge')."""
    words, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("-"):
            if "=" not in a and a not in GH_ROOT_BOOL and (a.startswith("--") or len(a) == 2):
                i += 2
            else:
                i += 1
            continue
        words.append(a)
        i += 1
    return words


def gh_flag_values(args, shorts, longs):
    """Values of the given flags in every spelling: -X v, -Xv, --flag v, --flag=v."""
    vals, i = [], 0
    while i < len(args):
        a = args[i]
        if a in shorts or a in longs:
            vals.append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        for s in shorts:
            if a.startswith(s) and len(a) > len(s) and not a.startswith("--"):
                vals.append(a[len(s):].lstrip("="))
        for lg in longs:
            if a.startswith(lg + "="):
                vals.append(a.split("=", 1)[1])
        i += 1
    return vals


def check_gh(args, ctx):
    for val in gh_flag_values(args, ("-R",), ("--repo",)):
        if val.lower() != REPO_SLUG.lower():
            ctx.deny(f"gh may only act on {REPO_SLUG}")
    words = gh_command_words(args)
    if not words:
        return
    top, sub = words[0], (words[1] if len(words) > 1 else "")
    if top not in GH_ALLOWED:
        ctx.deny(GH_DENIED_WHY.get(top, f"'gh {top}' is not on the guard's allow list ({GH_CONFIG_WHY}; aliases and extensions cannot be reviewed)"))
    allowed = GH_ALLOWED[top]
    if allowed is not None and sub not in allowed:
        ctx.deny(GH_DENIED_WHY.get(top, GH_CONFIG_WHY))
    if top == "auth" and any(a in ("-t", "--show-token") or a.startswith("--show-token=") for a in args):
        ctx.deny(GH_DENIED_WHY["auth"])
    if top == "pr" and sub == "merge":
        ctx.deny("merges happen only inside the installed gate ~/.llm-autopilot/bin/merge_to_dev.sh (dev) or by the operator (main)")
    if top == "pr" and sub == "review" and any(a in ("-a", "--approve") or a.startswith("--approve=") for a in args):
        ctx.deny("self-approval is not a review")
    if top == "issue" and sub in ("delete", "transfer"):
        ctx.deny("issues are records; the agent does not delete or move them")
    if top == "api":
        check_gh_api(args, words, ctx)
    if top == "run" and sub == "rerun":
        check_rerun(args, ctx)
    # PR/issue text is public: scan bodies for secrets.
    if top in ("pr", "issue", "release") and sub in ("create", "new", "edit", "comment"):
        values = load_secret_values()
        for text in gh_flag_values(args, ("-b", "-t", "-n"), ("--body", "--title", "--notes")):
            scan_text_for_secrets(text, "the PR/issue text", ctx, values)
        for f in gh_flag_values(args, ("-F",), ("--body-file", "--notes-file")):
            p = expand_path(f, ctx.cwd)
            if p and os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    scan_text_for_secrets(fh.read(2_000_000), "the PR/issue body file", ctx, values)


def check_gh_api(args, words, ctx):
    endpoint = words[1] if len(words) > 1 else ""
    methods = gh_flag_values(args, ("-X",), ("--method",))
    for m in methods:
        if m.upper() not in ("GET", "HEAD"):
            ctx.deny("mutating REST calls are not allowed; use the typed gh commands")
    explicit_read = bool(methods)  # every -X was GET/HEAD (loop above refused the rest)
    for h in gh_flag_values(args, ("-H",), ("--header",)):
        if re.search(r"method-override", h, re.I):
            ctx.deny("method-override headers turn reads into writes; not allowed")
    if any(h != "github.com" for h in gh_flag_values(args, (), ("--hostname",))):
        ctx.deny(f"gh may only act on github.com/{REPO_SLUG}")
    fields = gh_flag_values(args, ("-f", "-F"), ("--field", "--raw-field"))
    inputs = gh_flag_values(args, (), ("--input",))
    # -F/--field with @file or =@file reads a file (or '-' = stdin) INTO the
    # request, so `gh api -X GET ... -F q=@.env` sends a secret to github.com even
    # on a read. -f/--raw-field send literals and are fine. (The graphql branch
    # already refuses @ below; this covers every REST endpoint too.)
    for f in gh_flag_values(args, ("-F",), ("--field",)):
        if f.startswith("@") or "=@" in f:
            ctx.deny("gh api -F/--field with @file (or =@) reads a file into the request and can exfiltrate a secret to github.com; pass literals with -f/--raw-field")
    if endpoint != "graphql":
        if inputs:
            ctx.deny("gh api --input reads a request body from a file or stdin that the guard cannot check; pass an explicit GET (gh api -X GET ... -f key=value adds query parameters)")
        # Without an explicit method, gh turns -f/-F into a POST body. With an
        # explicit GET/HEAD they are query parameters (gh api -X GET -f q=...).
        if fields and not explicit_read:
            ctx.deny("gh api with fields sends a POST; add -X GET to pass them as query parameters, or use the typed gh commands")
        return
    if inputs or any("=@" in f or f.startswith("@") for f in fields):
        ctx.deny("GraphQL documents read from files or stdin cannot be checked; pass the query inline with -f query='query {...}'")
    if any(re.search(r"mutation|subscription", f, re.I) for f in fields):
        ctx.deny("GraphQL mutations are not allowed; use the typed gh commands")


# --------------------------------------------------------------------------
# docker
# --------------------------------------------------------------------------

def check_rerun(args, ctx):
    """Re-running CI on the autopilot's branches is fine; re-running a main run redeploys production."""
    ids = [a for a in args[2:] if re.fullmatch(r"\d+", a)]
    if len(ids) != 1:
        ctx.deny("name exactly one run id to re-run")
    try:
        r = subprocess.run(["gh", "run", "view", ids[0], "-R", REPO_SLUG, "--json", "headBranch,event,workflowName"],
                           capture_output=True, text=True, timeout=20)
        info = json.loads(r.stdout or "{}")
    except Exception:
        ctx.deny("could not look up the run (fail-closed)")
    if r.returncode != 0 or not info:
        ctx.deny("could not look up the run (fail-closed)")
    if info.get("headBranch") in ("main", "master") or info.get("event") in ("workflow_dispatch", "schedule"):
        ctx.deny("re-running a main, dispatched or scheduled run can redeploy production")


def is_dev(name):
    return bool(name) and name.startswith(DEV)


# Options that take a VALUE, per container-targeting subcommand. Anything not
# listed is treated as a boolean flag (its next word is NOT skipped), so a
# container name can never be dropped by a mis-modelled flag. rm/pause/unpause/
# wait/rename/cp take no value options (rm's -f/-v/-l are boolean).
DOCKER_TARGET_VALUE_OPTS = {
    "stop": {"-t", "--time", "-s", "--signal"},
    "restart": {"-t", "--time", "-s", "--signal"},
    "kill": {"-s", "--signal"},
    "start": {"--detach-keys"},
    "attach": {"--detach-keys"},
    "commit": {"-a", "--author", "-c", "--change", "-m", "--message"},
    "update": {"--cpus", "--memory", "-m", "--memory-swap", "--memory-reservation", "--restart",
               "--cpuset-cpus", "--cpuset-mems", "--cpu-shares", "-c", "--pids-limit", "--blkio-weight",
               "--cpu-period", "--cpu-quota", "--cpu-rt-period", "--cpu-rt-runtime", "--kernel-memory"},
}


def is_prod_db_text(text):
    return bool(re.search(r"sf-local-ai-postgres|(-p|--port)\s*=?\s*5432\b|:5432\b", text))


RUN_VALUE_OPTS = {"-e", "--env", "-v", "--volume", "-p", "--publish", "-w", "--workdir", "--name", "--network", "--net", "-u", "--user",
                  "--entrypoint", "-l", "--label", "--mount", "-m", "--memory", "--cpus", "--env-file", "--restart", "--pull", "--platform",
                  "--hostname", "-h", "--add-host", "--log-driver", "--log-opt", "--tmpfs", "--ulimit", "--shm-size", "--cap-add",
                  "--cap-drop", "--device", "--gpus", "--runtime", "--security-opt", "--stop-signal", "--stop-timeout", "--oom-score-adj",
                  "--memory-swap", "--memory-reservation", "--cpuset-cpus", "--cpu-shares", "--ipc", "--pid", "--uts", "--userns", "--dns",
                  "--ip", "--link", "-a", "--attach", "--cidfile", "--expose", "--group-add", "--health-cmd", "--health-interval",
                  "--pids-limit", "--sysctl", "--storage-opt", "--label-file", "--volumes-from", "--network-alias", "--detach-keys"}


def run_is_detached(rest):
    i = 0
    while i < len(rest):
        x = rest[i]
        if not x.startswith("-"):
            return False  # the image: everything after it is the container's command
        if x in ("-d", "--detach") or x.startswith("--detach="):
            return True
        if re.fullmatch(r"-[a-zA-Z]{2,}", x) and "d" in x[1:]:
            return True
        i += 2 if (x in RUN_VALUE_OPTS and "=" not in x) else 1
    return False


# docker global options (before the subcommand). VALUE options consume the next
# word; BOOL options do not. --context/-c and --config are refused above.
# Anything else fails closed so a hidden value flag cannot fake a subcommand.
DOCKER_GLOBAL_VALUE = {"-l", "--log-level", "--tlscacert", "--tlscert", "--tlskey"}
DOCKER_GLOBAL_BOOL = {"--tls", "--tlsverify", "-D", "--debug"}
# Read-only subcommands the guard lets through after all the state-changing
# branches; any other (unrecognised) subcommand fails closed.
DOCKER_READONLY_SUBS = {"ps", "ls", "logs", "images", "version", "info", "events", "stats", "top",
                        "port", "diff", "history", "search", "inspect", "df", "wait"}


def check_docker(args, ctx):
    a = list(args)
    for k in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
        if ctx.env.get(k) == UNCERTAIN:
            ctx.deny(f"{k} is set conditionally or computed earlier in this command, so the guard cannot tell which docker engine this reaches; set it unconditionally or inline")
    docker_host = ctx.env.get("DOCKER_HOST", "")
    if ctx.env.get("DOCKER_CONTEXT"):
        ctx.deny("DOCKER_CONTEXT selects a docker context; contexts belong to the operator, point DOCKER_HOST at this host's socket or the worker instead")
    if ctx.env.get("DOCKER_CONFIG"):
        ctx.deny("DOCKER_CONFIG relocates docker's config (auth, contexts); it belongs to the operator")
    while a and a[0].startswith("-"):
        opt = a.pop(0)
        if opt in ("-H", "--host") and a:
            docker_host = a.pop(0)
        elif opt.startswith(("--host=", "-H=")):
            docker_host = opt.split("=", 1)[1]
        elif opt.startswith("-H") and len(opt) > 2:
            docker_host = opt[2:]
        elif opt in ("--context", "-c") or opt.startswith(("--context=", "-c=")):
            ctx.deny("docker contexts belong to the operator; point DOCKER_HOST at the worker instead")
        elif opt in ("--config",) or opt.startswith("--config="):
            ctx.deny("docker --config relocates docker's config (auth, contexts); it belongs to the operator")
        elif opt.split("=", 1)[0] in DOCKER_GLOBAL_VALUE:
            if "=" not in opt and a:
                a.pop(0)  # consume the value (--tlscacert FILE, --log-level debug)
        elif opt in DOCKER_GLOBAL_BOOL:
            continue
        else:
            # An unknown global option could consume the next word, so the guard
            # cannot tell which word is the subcommand (a hidden value flag could
            # fake a read-only subcommand). Fail closed rather than guess.
            ctx.deny(f"'docker {opt}' is an unrecognised global option; use only -H/--host and the TLS/--log-level/--debug flags so the guard can tell which word is the subcommand (fail-closed)")
    remote = docker_target_is_worker(docker_host)
    if docker_host and not remote and not re.match(r"^unix://", docker_host):
        ctx.deny("DOCKER_HOST may point only at this host's socket or the worker node")
    if not a:
        return
    sub, rest = a[0], a[1:]
    if sub == "compose":
        return check_compose(rest, ctx, remote)
    if sub in ("run", "create") and not remote and run_is_detached(rest):
        ctx.deny("detached containers are long-lived services; run them on the worker node (owner rule: nothing new on head memory)")
    if sub in ("system", "volume", "image", "container", "network", "builder", "buildx", "context") and rest:
        op = rest[0]
        if op == "prune" or (sub == "volume" and op in ("rm", "remove")) or (sub == "network" and op in ("rm", "remove", "disconnect", "connect")):
            ctx.deny("prunes and volume/network removal can destroy production data or shared caches (§3.3)")
        if sub == "container":
            return check_docker((["--host", docker_host] if docker_host else []) + [{"remove": "rm"}.get(op, op)] + rest[1:], ctx)
        if sub == "image" and op in ("rm", "remove", "tag", "pull", "load", "import", "build", "push", "save"):
            return check_docker((["--host", docker_host] if docker_host else []) + [{"rm": "rmi", "remove": "rmi"}.get(op, op)] + rest[1:], ctx)
        if sub in ("buildx", "builder") and op in ("build", "bake"):
            return check_docker((["--host", docker_host] if docker_host else []) + ["build"] + rest[1:], ctx)
        if sub == "context" and op not in ("ls", "list", "inspect", "show"):
            ctx.deny("docker contexts belong to the operator")
        if sub == "image" and op == "inspect":
            return check_docker(["inspect"] + rest[1:], ctx)
        return
    if sub in ("rm", "stop", "kill", "restart", "update", "pause", "unpause", "rename", "start", "commit", "attach", "wait", "cp"):
        # Drop ONLY the options that take a value for THIS subcommand (docker stop
        # -t 5, docker kill -s TERM), so a value is never mistaken for a container
        # name. For `rm`, -f/-v/-l are boolean: never skip the next word, or a
        # production container named right after `-f` would escape the check.
        value_opts = DOCKER_TARGET_VALUE_OPTS.get(sub, frozenset())
        names, j = [], 0
        while j < len(rest):
            x = rest[j]
            if x.startswith("-") and x != "-":
                if "=" not in x and x in value_opts:
                    j += 2
                else:
                    j += 1
                continue
            names.append(x)
            j += 1
        if sub == "cp":
            names = [x.split(":", 1)[0] for x in names if ":" in x]
        if not names or any("$" in x or "__SUBST__" in x for x in names):
            ctx.deny("name the container literally; the guard cannot verify computed targets")
        for nm in names:
            if not is_dev(nm):
                ctx.deny(f"'{nm}' is not an autopilot dev container ({DEV}*); production changes go only through ops/deploy/deploy_prod.sh")
        return
    if sub == "rmi":
        for nm in nonopt(rest):
            if not is_dev(nm):
                ctx.deny(f"only {DEV}* images the autopilot built may be removed")
        return
    if sub in ("pull", "load", "import", "push", "login", "logout", "swarm", "service", "stack", "node", "secret", "plugin", "trust", "checkpoint", "save"):
        ctx.deny(f"'docker {sub}' can retag images production uses, move data off the host or change the engine")
    if sub == "tag":
        names = nonopt(rest)
        if len(names) >= 2 and not is_dev(names[-1]):
            ctx.deny(f"image tags must start with {DEV}; never overwrite a production tag")
        return
    if sub == "build":
        tags = []
        for i, x in enumerate(rest):
            if x in ("-t", "--tag") and i + 1 < len(rest):
                tags.append(rest[i + 1])
            elif x.startswith("--tag="):
                tags.append(x.split("=", 1)[1])
            if x == "--pull" or x.startswith("--pull="):
                ctx.deny("--pull refreshes shared base images production may rely on")
            if x == "--push" or x.startswith("--output") or x == "-o":
                ctx.deny("build outputs stay local")
        if not tags or not all(is_dev(t) for t in tags):
            ctx.deny(f"tag dev builds {DEV}-<name>; never build onto a production image tag")
        if not remote:
            ctx.deny("image builds run under the Docker daemon, outside the autopilot's memory cap; build on the worker node (DOCKER_HOST=ssh://<worker>, address in the host config)")
        return
    if sub in ("run", "create"):
        if not remote:
            check_head_container_caps(rest, ctx)
        return check_docker_run(rest, ctx)
    if sub == "exec":
        return check_docker_exec(rest, ctx)
    if sub == "inspect":
        fmt = None
        for i, x in enumerate(rest):
            if x in ("-f", "--format") and i + 1 < len(rest):
                fmt = rest[i + 1]
            elif x.startswith(("--format=", "-f=")):
                fmt = x.split("=", 1)[1]
        if fmt is None or re.search(r"\bEnv\b|\bjson\s+\.\s*\}\}|\{\{\s*\.\s*\}\}|\{\{\s*json\s+\.Config\s*\}\}|\.Config\s*\}\}", fmt):
            ctx.deny("docker inspect prints container environments (secrets); use --format with only the fields you need (never Env, never the whole .Config)")
        return
    if sub not in DOCKER_READONLY_SUBS:
        ctx.deny(f"'docker {sub}' is not on the guard's allow list; a subcommand that could change state goes through ops/deploy on production or runs against a {DEV}* dev target (fail-closed)")


WORKER_HOSTS = {h.lower() for h in (HOST.get("worker_hosts") or [])} | ({str(HOST["worker_ssh"]).lower()} if HOST.get("worker_ssh") else set())


def docker_target_is_worker(docker_host):
    """True when DOCKER_HOST / -H points at the worker node (ssh://[user@]host[:port] or tcp://host:port)."""
    m = re.match(r"^(?:ssh|tcp)://(?:[^@/]+@)?(\[[^\]]+\]|[^:/]+)", docker_host or "")
    return bool(m) and m.group(1).lower() in WORKER_HOSTS


def resolve_compose(globals_, ctx):
    """Render the project the command would act on (names only; nothing printed)."""
    env = dict(os.environ)
    env.update(ctx.env)
    try:
        r = subprocess.run(["docker", "compose", *globals_, "config", "--format", "json"], cwd=ctx.cwd or None,
                           env=env, capture_output=True, text=True, timeout=45)
    except Exception as exc:
        ctx.deny(f"could not render the compose project to check where it points ({type(exc).__name__}); fail-closed")
    if r.returncode != 0:
        ctx.deny("could not render the compose project to check where it points (docker compose config failed); fail-closed")
    try:
        return json.loads(r.stdout)
    except ValueError:
        ctx.deny("docker compose config did not print JSON; fail-closed")


# docker compose global options (before the subcommand). VALUE options consume
# the next word (so a hidden one such as the deprecated --workdir cannot fake a
# read-only subcommand); BOOL options do not. Anything else fails closed.
COMPOSE_GLOBAL_VALUE = {"-p", "--project-name", "-f", "--file", "--project-directory", "--workdir",
                        "--env-file", "--profile", "--ansi", "--progress", "--parallel"}
COMPOSE_GLOBAL_BOOL = {"--dry-run", "--compatibility", "--all-resources", "--no-ansi", "--verbose"}


def check_compose(rest, ctx, remote=False):
    """docker compose: read-only subcommands pass; anything else must resolve to a
    dev project (operator decision 7: never a project, volume, network or image
    named after production) and, for long-lived services, run on the worker."""
    globals_, sub, sub_args, i, clean = [], None, [], 0, True
    while i < len(rest):
        x = rest[i]
        if not x.startswith("-"):
            sub, sub_args = x, rest[i + 1 :]
            break
        base = x.split("=", 1)[0]
        if base in COMPOSE_GLOBAL_VALUE:
            if "=" in x:
                globals_.append(x)
                i += 1
            elif i + 1 < len(rest):
                globals_ += [x, rest[i + 1]]
                i += 2
            else:
                clean = False
                i += 1
            continue
        if x in COMPOSE_GLOBAL_BOOL:
            globals_.append(x)
            i += 1
            continue
        if x.startswith("-p") and not x.startswith("--") and len(x) > 2:  # -pNAME attached
            globals_.append(x)
            i += 1
            continue
        # An unknown global option: the guard cannot tell whether it consumes the
        # next word, so it cannot tell which word is the subcommand. Fail closed
        # rather than guess (a hidden value flag could fake a read-only subcommand).
        clean = False
        globals_.append(x)
        i += 1
    # Compose takes the LAST -p / --project-name, then COMPOSE_PROJECT_NAME. A
    # -p that appears AFTER the subcommand still selects the project by name.
    project_flag = ctx.env.get("COMPOSE_PROJECT_NAME")
    for section in (globals_, sub_args):
        j = 0
        while j < len(section):
            g = section[j]
            if g in ("-p", "--project-name") and j + 1 < len(section):
                project_flag = section[j + 1]
                j += 2
                continue
            if g.startswith("--project-name="):
                project_flag = g.split("=", 1)[1]
            elif g.startswith("-p") and not g.startswith("--") and len(g) > 2:
                project_flag = g[2:].lstrip("=")
            j += 1
    if not clean:
        ctx.deny("docker compose has an unrecognised global option, so the guard cannot tell which word is the subcommand or whether this acts on production; use only the known -p/-f/--project-directory/--env-file/--profile/--ansi/--progress/--parallel/--workdir flags and a named subcommand (fail-closed)")
    if sub == "config":
        if not set(sub_args) & {"-q", "--quiet", "--no-interpolate", "--services", "--volumes", "--images", "--profiles", "--hash"}:
            ctx.deny("'docker compose config' prints interpolated values, including secrets; add --no-interpolate or -q")
        return
    # Read-only subcommands are fine even against the production project. This is
    # reached only after the clean-parse check above, so a hidden value flag can
    # no longer shift a mutating subcommand into this allow-list.
    if sub in (None, "ps", "ls", "images", "top", "logs", "version", "port", "events", "stats"):
        return
    uncertain = sorted(k for k, v in ctx.env.items() if v == UNCERTAIN and (k == "TECHSARA_STACK" or k.startswith("COMPOSE_")))
    if uncertain:
        ctx.deny(f"{', '.join(uncertain)} is set conditionally or computed earlier in this command, so the guard cannot tell which compose project this acts on; set it unconditionally or inline")
    if project_flag and project_flag.startswith(PROD_STACK):
        ctx.deny(f"'{project_flag}' is a production compose project")
    if any("$" in g or "__SUBST__" in g for g in globals_):
        ctx.deny("compose flags must be literal so the guard can render the project")
    doc = resolve_compose(globals_, ctx)
    names = [("project", doc.get("name"))]
    for kind in ("volumes", "networks", "secrets", "configs"):
        for key, val in (doc.get(kind) or {}).items():
            names.append((kind[:-1], (val or {}).get("name") or key))
    for svc, val in (doc.get("services") or {}).items():
        val = val or {}
        names += [("image", val.get("image")), ("container", val.get("container_name"))]
        for vol in val.get("volumes") or []:
            if isinstance(vol, dict) and vol.get("type") == "volume":
                names.append(("volume", vol.get("source")))
    hits = sorted({f"{k} {v}" for k, v in names if v and str(v).startswith(PROD_STACK)})
    if hits:
        ctx.deny(f"this compose command resolves to PRODUCTION resources ({', '.join(hits[:4])}); a dev stack sets TECHSARA_STACK={DEV} (--env-file ops/dev/stack.vars)")
    if not str(doc.get("name") or "").startswith(DEV):
        ctx.deny(f"dev compose projects are named {DEV}*; this one resolves to '{doc.get('name')}'")
    if sub in ("up", "create", "run", "start", "restart", "build", "pull", "scale", "watch", "unpause") and not remote:
        ctx.deny("long-lived dev services and builds run on the worker node (owner rule: nothing new on head memory); set DOCKER_HOST=ssh://<worker> (address in the host config); down/stop/rm/kill are fine here")
    if sub == "exec" and not remote:
        ctx.deny("compose exec runs against the worker's dev stack only")


HEAD_CONTAINER_MAX_MEM = 2 * 1024 ** 3
HEAD_CONTAINER_MAX_CPUS = 4.0


def _docker_bytes(v):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([bkmg]?)b?", (v or "").strip().lower())
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)]


def check_head_container_caps(rest, ctx):
    """Containers run under the Docker daemon, outside the autopilot's memory cap.
    On the head only short --rm tool containers with small explicit caps are allowed."""
    opts, i = {}, 0
    while i < len(rest):
        x = rest[i]
        if not x.startswith("-"):
            break  # the image; the rest is the container's command
        if "=" in x and x.startswith("--"):
            k, v = x.split("=", 1)
            opts.setdefault(k, []).append(v)
        elif re.fullmatch(r"-m\S+", x):
            opts.setdefault("-m", []).append(x[2:])
        elif x in RUN_VALUE_OPTS:
            opts.setdefault(x, []).append(rest[i + 1] if i + 1 < len(rest) else "")
            i += 1
        else:
            opts.setdefault(x, []).append("")
        i += 1
    mem = [_docker_bytes(v) for v in opts.get("-m", []) + opts.get("--memory", [])]
    cpus = []
    for v in opts.get("--cpus", []):
        try:
            cpus.append(float(v))
        except ValueError:
            cpus.append(None)
    if "--rm" not in opts or not mem or not cpus or any(m is None or m <= 0 or m > HEAD_CONTAINER_MAX_MEM for m in mem) \
            or any(c is None or c <= 0 or c > HEAD_CONTAINER_MAX_CPUS for c in cpus):
        ctx.deny("containers run under the Docker daemon, outside the autopilot's memory cap; on the head use only "
                 f"'docker run --rm -m <=2g --cpus <={HEAD_CONTAINER_MAX_CPUS:g} ...' tool containers, and run anything bigger on the worker "
                 "(DOCKER_HOST=ssh://<worker>, address in the host config)")


def check_docker_run(rest, ctx):
    name, rm, image_seen = None, False, False
    i = 0
    while i < len(rest):
        x = rest[i]
        nxt = rest[i + 1] if i + 1 < len(rest) else ""
        if x == "--name":
            name = nxt
        elif x.startswith("--name="):
            name = x.split("=", 1)[1]
        elif x == "--rm":
            rm = True
        if x == "--privileged" or x in ("--pid=host", "--ipc=host", "--uts=host", "--userns=host") or (x in ("--pid", "--ipc", "--uts", "--userns") and nxt == "host"):
            ctx.deny("privileged or host-namespace containers can reach production processes")
        if x.startswith(("--cap-add", "--security-opt", "--device", "--gpus", "--runtime")):
            ctx.deny("devices, GPUs, extra capabilities and custom runtimes on the head compete with production inference (owner rule: nothing new on head memory)")
        if (x in ("--network", "--net") and nxt == "host") or x in ("--network=host", "--net=host"):
            ctx.deny("host networking can collide with production ports")
        if x.startswith("--restart"):
            pol = x.split("=", 1)[1] if "=" in x else nxt
            if pol != "no":
                ctx.deny("restart policies create long-running services on the head")
        if x.startswith("--pull"):
            pol = x.split("=", 1)[1] if "=" in x else nxt
            if pol not in ("never", "missing"):
                ctx.deny("--pull can retag images production uses")
        if x in ("-v", "--volume", "--mount"):
            check_mount_spec(nxt, ctx)
        elif x.startswith(("--volume=", "--mount=")) or re.match(r"^-v\S", x):
            check_mount_spec(x.split("=", 1)[1] if "=" in x else x[2:], ctx)
        if x in ("--volumes-from",) or x.startswith("--volumes-from="):
            srcname = (x.split("=", 1)[1] if "=" in x else nxt).split(":", 1)[0]
            if not is_dev(srcname):
                ctx.deny(f"--volumes-from mounts another container's volumes (here '{srcname}'); production and shared volumes must never be mounted, only {DEV}-* dev containers")
        if x in ("-e", "--env", "--env-file") and nxt:
            if x == "--env-file":
                p = expand_path(nxt, ctx.cwd)
                if p and under(p, R_PROD):
                    ctx.deny("production env files must not be passed to containers the autopilot starts")
        i += 1
    if not rm and not is_dev(name):
        ctx.deny(f"containers the autopilot starts must be --rm or named {DEV}-* (and recorded in ~/.llm-autopilot/manifest.jsonl)")


def check_mount_spec(spec, ctx):
    if "docker.sock" in spec:
        ctx.deny("mounting the docker socket hands out root on the host")
    m = re.search(r"(?:^|,)(?:source|src)=([^,]+)", spec)
    src = m.group(1) if m else spec.split(":", 1)[0]
    ro = bool(re.search(r"(:ro\b|,ro\b|readonly|:ro,)", spec))
    if not src.startswith(("/", "~", ".", "$")):
        if src.startswith("sf-local-ai") or "pgdata" in src or "hf-cache" in src:
            ctx.deny(f"production volume {src} must never be mounted by the autopilot")
        return
    p = expand_path(src, ctx.cwd)
    if p is None:
        if not ro:
            ctx.deny("bind-mount sources must be literal paths")
        return
    if p in ("/", _real(HOME)):
        ctx.deny("mounting / or $HOME is not allowed")
    if not ro and not write_allowed(p):
        ctx.deny(f"a read-write mount of {p} would let a container write outside the autopilot's zones; add :ro or use a path under {WORK}")
    if is_secret_path(p) or under(p, _real(os.path.join(HOME, ".ssh"))) or under(p, R_PROD) and not ro:
        ctx.deny("secret files and read-write production paths must not be mounted")


EXEC_READONLY_INNER = {"cat", "ls", "head", "tail", "wc", "stat", "df", "du", "free", "ps", "nvidia-smi", "curl", "wget",
                       "psql", "pip", "vllm", "python", "python3", "uname", "hostname", "date", "id", "grep", "find",
                       "nproc", "pg_isready", "redis-cli", "nc", "echo", "true", "sha256sum", "md5sum"}


def check_docker_exec(rest, ctx):
    a, user = list(rest), None
    while a and a[0].startswith("-"):
        opt = a.pop(0)
        if opt in ("-u", "--user") and a:
            user = a.pop(0)
        elif opt in ("-e", "--env", "-w", "--workdir", "--env-file", "--detach-keys") and a:
            a.pop(0)
        elif opt.startswith("--privileged"):
            ctx.deny("privileged exec is not allowed")
    if not a:
        return
    container, inner = a[0], a[1:]
    if "$" in container or "__SUBST__" in container:
        ctx.deny("name the container literally")
    if is_dev(container):
        if inner:
            analyze_words(inner, ctx)
        return
    if user in ("root", "0") or (user or "").startswith("0:"):
        ctx.deny("root exec into production containers is not allowed")
    if not inner:
        ctx.deny("interactive exec into production containers is not allowed")
    env, w, bare = strip_prefix(inner)
    if bare or not w:
        ctx.deny("printing a production container's environment exposes secrets")
    base = os.path.basename(w[0])
    if base in SHELLS:
        for i, x in enumerate(w[1:], start=1):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", x) and i + 1 < len(w):
                try:
                    toks = tokenize(prepare(w[i + 1]))
                except ValueError:
                    ctx.deny("unparseable nested command (fail-closed)")
                for seg in segments(toks):
                    check_docker_exec([container] + seg[0], ctx)
                return
        ctx.deny("shells in production containers are not allowed; run one read-only command")
    if base in ("env", "printenv", "set", "export", "declare"):
        ctx.deny("printing a production container's environment exposes secrets")
    if base not in EXEC_READONLY_INNER:
        ctx.deny(f"production containers are read-only to the autopilot; '{base}' is not on the read-only list")
    if base in ("python", "python3") and not (w[1:2] in (["--version"], ["-V"]) or (w[1:3] == ["-m", "pip"] and w[3:4] in (["list"], ["show"], ["freeze"]))):
        ctx.deny("running code inside production containers is not allowed")
    if base == "pip" and w[1:2] not in (["list"], ["show"], ["freeze"], ["--version"]):
        ctx.deny("pip in production containers is read-only (list/show/freeze)")
    if base == "psql" or container in PROD_DB_CONTAINERS:
        if ctx.piped_in or any(op in ("<", "<<", "<<-", "<<<") for op, _ in ctx.redirs):
            if not any(op in ("<<", "<<-") for op, _ in ctx.redirs):
                ctx.deny("SQL piped into production psql cannot be checked; pass one SELECT with -c")
        check_prod_sql(" ".join(w), ctx)
    check_command(base, w[1:], Ctx(ctx.raw, "/", ctx.depth + 1, env, top=False))


SQL_MUTATING = re.compile(r"\b(insert\s+into|update\s+\S+\s+set|delete\s+from|drop|truncate|alter|grant|revoke|create|comment\s+on|vacuum|cluster|reindex|copy|lock|refresh|call|do\s+\$|set\s+role|set\s+session|reset|listen|notify|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|lo_export|dblink|pg_sleep|into\s+outfile|select\s+[^;]*\binto\s+\w)", re.I)


def check_prod_sql(text, ctx):
    if re.search(r"(^|\s)(-f|--file)(\s|=)", text):
        ctx.deny("SQL files cannot be checked; pass one SELECT with -c")
    if SQL_MUTATING.search(text):
        ctx.deny("the production database is read-only to the autopilot")
    for r in re.findall(r"\b(?:from|join)\s+([A-Za-z_\"][\w.\"]*)", text, re.I):
        r = r.replace('"', "")
        if not PROD_DB_TABLE_ALLOW.match(r):
            ctx.deny(f"production table '{r}' may hold user content, and anything read here goes to an external model API (§3.3); use catalog/statistics views or Prometheus metrics, or ask the operator (NEEDS_HUMAN.md) to allowlist a metadata-only table")
    if re.search(r"\\(copy|o|w|!|i|ir|g\b|gx|gexec|lo_\w+|setenv|cd)", text):
        ctx.deny("psql meta-commands that read or write files or run shell are not allowed against production")


# --------------------------------------------------------------------------
# Remaining checks
# --------------------------------------------------------------------------

def check_pip(args, ctx):
    if set(args) & {"--user", "--break-system-packages"} or any(a.startswith("--target") or a.startswith("--prefix") for a in args):
        ctx.deny("install into a project venv under ~/work, never the user or system site")


def check_systemctl(args, ctx):
    a = nonopt(args)
    if not a:
        return
    verb = a[0]
    if verb in ("status", "show", "list-units", "list-timers", "list-unit-files", "is-active", "is-enabled", "is-failed", "cat", "list-dependencies", "list-sockets", "list-jobs", "is-system-running", "help"):
        return
    if verb == "show-environment":
        ctx.deny("printing the manager environment can expose secrets")
    if "--user" not in args:
        ctx.deny("system services belong to the operator (and need root)")
    units = a[1:]
    if not units or not all(is_dev(u) for u in units):
        ctx.deny(f"only the autopilot's own {DEV}* user units may change; the runner, GitHub runner and techsara units belong to the operator (ops/autopilot/install.sh)")


PROTECTED_PROC = re.compile(r"vllm|ray::|postgres|Runner\.(Listener|Worker)|actions-runner|github-runner|llm-autopilot|autopilot\.py|cloudflared|dockerd|containerd|claude|code-server|vscode|sshd|systemd|gnome|Xorg|whisper|uvicorn|gunicorn|next-server|grafana|prometheus|searxng|litellm|nccl|techsara", re.I)


def check_kill(cmd, args, ctx):
    if cmd in ("pkill", "killall"):
        pats = nonopt(args)
        if not pats or not all(DEV in p for p in pats):
            ctx.deny(f"pattern kills can hit production or other sessions; kill a PID you started (or match '{DEV}')")
        return
    if any(x.startswith("%") for x in args):
        return
    pids = [x for x in args if re.fullmatch(r"\d+", x)]
    if any(x == "-1" or re.fullmatch(r"-\d{2,}", x) for x in args) or "--" in args and any(re.fullmatch(r"-\d+", x) for x in args[args.index("--") + 1 :]):
        ctx.deny("process-group and broadcast kills are not allowed")
    if any("$" in x or "__SUBST__" in x for x in args):
        ctx.deny("kill a literal PID; the guard cannot verify computed targets")
    for p in pids:
        if int(p) <= 1:
            ctx.deny("PID 0/1 are the init system")
        try:
            st = os.stat(f"/proc/{p}")
            if st.st_uid != os.getuid():
                ctx.deny(f"PID {p} belongs to another user")
            with open(f"/proc/{p}/cgroup") as fh:
                cg = fh.read()
            with open(f"/proc/{p}/cmdline", "rb") as fh:
                cl = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if re.search(r"docker|containerd|system\.slice|init\.scope|github-runner|llm-autopilot|techsara", cg) or PROTECTED_PROC.search(cl):
            ctx.deny(f"PID {p} belongs to production, the runner or another session")


def check_ssh(cmd, args, ctx):
    joined = " ".join(args)
    if re.search(r"StrictHostKeyChecking\s*=?\s*(no|off|accept-new)|UserKnownHostsFile|GlobalKnownHostsFile|ProxyCommand|LocalCommand|PermitLocalCommand|KnownHostsCommand", joined, re.I):
        ctx.deny("changing host-key checking or proxy/local commands creates trust or hides commands (§3.3)")
    if cmd == "ssh":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            opt = rest.pop(0)
            if opt in ("-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w", "-B") and rest:
                rest.pop(0)
        if len(rest) >= 2:
            analyze(" ".join(rest[1:]), Ctx(ctx.raw, "/", ctx.depth + 1, {}, top=False))


NET_UPLOAD = re.compile(r"(^|\s)(-d|--data[\w-]*|-F|--form[\w-]*|-T|--upload-file|--json|--post-data|--post-file|--body-data|--body-file)(\s|=|$)|(^|\s)(-X|--request|--method)\s*=?\s*(POST|PUT|PATCH|DELETE)", re.I)
PRIVATE_NET = re.compile(r"^(localhost|127\.\d+\.\d+\.\d+|\[?::1\]?|0\.0\.0\.0|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|[\w-]+\.internal|host\.docker\.internal)$", re.I)


def is_local_target(text):
    m = re.match(r"^(?:https?://)?(\[[^\]]+\]|[^/:\s?#]+)", text or "")
    host = (m.group(1) if m else "").lower()
    return bool(host) and (bool(PRIVATE_NET.match(host)) or host in TRUSTED_HOSTS or host in PROD_HOSTS)


def check_network(cmd, args, ctx):
    urls = [x for x in args if re.match(r"^(https?://|[\w.-]+:\d+(/|$))", x)]
    if NET_UPLOAD.search(" " + " ".join(args)):
        for u in urls:
            if not is_local_target(u):
                ctx.deny("sending data to an external host is not allowed (§3.3: no private or company data to external services)")
    if cmd in ("nc", "ncat", "socat", "telnet"):
        hosts = nonopt(args)
        if hosts and not is_local_target(hosts[0]):
            ctx.deny("raw sockets to external hosts are not allowed")


# --------------------------------------------------------------------------
# File tools
# --------------------------------------------------------------------------

def check_file_tool(tool, ti, cwd):
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        raw_path = ti.get("file_path") or ti.get("notebook_path") or ""
        p = expand_path(raw_path, cwd) if raw_path else None
        if p is None:
            block(f"{TAG} {tool}: missing or computed file path.")
        if not write_allowed(p):
            block(f"{TAG} {tool} {raw_path} -> blocked: {write_why(p)}")
        content = ti.get("content") or ti.get("new_string") or ti.get("new_source") or ""
        if tool == "MultiEdit":
            content = "\n".join((e or {}).get("new_string", "") for e in ti.get("edits") or [])
        if re.search(r'"disableAllHooks"\s*:\s*true', content):
            block(f"{TAG} {tool} {raw_path} -> blocked: disabling hooks would switch off the autopilot's guardrails.")
        return
    if tool in ("Read", "Grep", "NotebookRead", "Glob"):
        for key in ("file_path", "path", "notebook_path"):
            v = ti.get(key)
            if v:
                p = expand_path(v, cwd)
                if p and is_secret_path(p):
                    block(f"{TAG} {tool} {v} -> blocked: secret files must never enter the transcript (§3.3).")
        # Grep searches INSIDE the files a glob selects, so a glob that reaches a
        # secret file would print its contents. Glob only LISTS names (a path it
        # returns is re-checked when Read/Grep opens it), and .env.example is a
        # public template, so the Glob tool is not subject to this block.
        if tool == "Grep":
            g = ti.get("glob") or ""
            if g and grep_tool_glob_reads_secret(g):
                block(f"{TAG} {tool} glob={g} -> blocked: searching inside secret files is not allowed (§3.3).")


# A Grep-tool glob alternative that reaches secret files.
_GREP_TOOL_SECRET_GLOB = re.compile(r"(^|[/*{,])\.env([.*}]|$)|credentials|secrets?\.env|id_(rsa|ed25519)|\.pem\b|\.key\b")


def grep_tool_glob_reads_secret(g):
    """True when a Grep-tool glob can select a secret file. Brace groups and
    lists are expanded first; an alternative that is exactly a public template
    (.env.example/.sample/.template/.dist, optionally after a directory or `*`)
    is exempt, every other alternative must be no secret glob. Too many
    alternatives fail closed."""
    alts = _glob_alternatives(g)
    if alts is None:
        return True
    for alt in alts:
        if alt.startswith("!"):
            continue  # an exclusion never selects a file
        if _GREP_TOOL_SECRET_GLOB.search(re.sub(r"\.env\.(example|sample|template|dist)$", ".TEMPLATE", alt)):
            return True
    return False


# Tools whose input runs a shell command, analysed exactly like Bash. Monitor
# runs a command and can open a WebSocket; both reach a `-p` auto-mode session.
SHELL_TOOLS = ("Bash", "Monitor")
FILE_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
FILE_READ_TOOLS = ("Read", "Grep", "NotebookRead", "Glob")
# Subagent and workflow tools. The operator REQUIRES multiple agents, and a
# subagent's own tool calls pass back through this same PreToolUse hook, so these
# stay allowed; only a secret-carrying prompt or a request to run outside the
# local guard (remote isolation) is refused.
AGENT_TOOLS = ("Agent", "Task", "Workflow", "Skill")
# A read-only MCP leaf: the tool name AFTER the last '__' must START with a read
# verb, so update_status / resources_delete / delete_resource (a read verb only
# as a trailing noun) are refused, while get_/list_/read_/search_ pass.
MCP_READONLY = re.compile(
    r"^(get|list|read|view|search|fetch|query|describe|show|status|lookup|inspect|retrieve|count|watch|ping|head)s?(_|$|[A-Z0-9])",
    re.I,
)


def _command_too_big(cmd):
    return len(cmd) > MAX_COMMAND_BYTES


def evaluate(payload):
    if HOST_ERROR:
        block(f"{TAG} the host configuration {HOST_CONFIG_PATH} is missing or invalid ({HOST_ERROR}); every call is blocked until the operator reinstalls it (fail-closed).")
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool in SHELL_TOOLS:
        cmd = ti.get("command") or ""
        if _command_too_big(cmd):
            block(f"{TAG} the command is larger than {MAX_COMMAND_BYTES} bytes; blocking (fail-closed).")
        if cmd:
            analyze(cmd, Ctx(short(cmd), cwd))
        check_monitor_channels(ti)
    elif tool in FILE_WRITE_TOOLS or tool in FILE_READ_TOOLS:
        check_file_tool(tool, ti, cwd)
    elif tool in ("WebFetch", "WebSearch"):
        check_web(tool, ti)
    elif tool in AGENT_TOOLS:
        check_agent_tool(tool, ti)
    elif tool in ("CronCreate", "ScheduleWakeup"):
        check_schedule_tool(tool, ti)
    elif tool == "EnterWorktree":
        check_enter_worktree(ti, cwd)
    elif tool.startswith("mcp__"):
        check_mcp_tool(tool, ti)
    else:
        # A tool the guard does not know. If it runs a shell command, analyse it
        # like Bash; if it carries a path, keep secret files out of it; a secret
        # shape anywhere in its input is refused. Otherwise it is let through to
        # layers 1 and 2 (permissions.deny and the auto-mode classifier).
        cmd = ti.get("command")
        if isinstance(cmd, str) and cmd:
            if _command_too_big(cmd):
                block(f"{TAG} the command is larger than {MAX_COMMAND_BYTES} bytes; blocking (fail-closed).")
            analyze(cmd, Ctx(short(cmd), cwd))
        for key in ("file_path", "path", "notebook_path"):
            v = ti.get(key)
            if isinstance(v, str) and v:
                p = expand_path(v, cwd)
                if p and is_secret_path(p):
                    block(f"{TAG} {tool} {v} -> blocked: secret files must never enter the transcript (§3.3).")
        _refuse_secret_shape_in(tool, ti)


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v)


def _refuse_secret_shape_in(tool, ti):
    for s in _iter_strings(ti):
        m = SECRET_SHAPES.search(s)
        if m:
            block(f"{TAG} {tool} -> blocked: its input carries a credential-shaped string ({m.group(0)[:6]}… redacted); secrets never go to a tool, a subagent or an external service (§3.3).")


# Hosts/addresses a WebFetch/WebSearch/WebSocket must never reach: production
# control planes, and any private, link-local or loopback address.
LINK_LOCAL_OR_PRIVATE = re.compile(
    r"^(localhost|127(\.\d+){1,3}|0\.0\.0\.0|0x[0-9a-f]+|\d{8,10}|\[?::1\]?|169\.254\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|"
    r"::ffff:[0-9a-f.:]+|fe80:[0-9a-f:]*|fc[0-9a-f:]+|fd[0-9a-f:]+|[\w-]+\.internal|host\.docker\.internal)$",
    re.I,
)


def _url_host(text):
    m = re.match(r"^\s*[a-z][a-z0-9+.-]*://(?:[^@/\s]+@)?(\[[^\]]+\]|[^/:\s?#]+)", text or "", re.I)
    return (m.group(1) if m else "").strip("[]").lower()


def check_web(tool, ti):
    text = ti.get("url") if tool == "WebFetch" else ti.get("query")
    text = text or ""
    m = SECRET_SHAPES.search(text)
    if m:
        block(f"{TAG} {tool} -> blocked: the {'URL' if tool == 'WebFetch' else 'query'} carries a credential-shaped string ({m.group(0)[:6]}… redacted); secrets never leave the host (§3.3).")
    host = _url_host(text)
    if tool == "WebFetch" and host:
        if host in PROD_HOSTS or LINK_LOCAL_OR_PRIVATE.match(host):
            block(f"{TAG} WebFetch -> blocked: '{host}' is a production, private, link-local or loopback address; WebFetch reaches a hosted model and must not probe internal or production endpoints (§3.3).")
    if tool == "WebSearch":
        for h in PROD_HOSTS:
            if h and h not in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1") and re.search(rf"(?<![\w.-]){re.escape(h)}(?![\w.-])", text, re.I):
                block(f"{TAG} WebSearch -> blocked: the query names a production host; internal host names must not reach an external search service (§3.3).")


def check_monitor_channels(ti):
    ws = ti.get("ws")
    url = ws.get("url") if isinstance(ws, dict) else None
    if url:
        m = SECRET_SHAPES.search(url)
        if m:
            block(f"{TAG} Monitor -> blocked: the WebSocket URL carries a credential-shaped string; secrets never leave the host (§3.3).")
        host = _url_host(url)
        if host and host not in TRUSTED_HOSTS and not LINK_LOCAL_OR_PRIVATE.match(host) and host not in PROD_HOSTS:
            block(f"{TAG} Monitor -> blocked: a WebSocket to '{host}' is a two-way channel to an external host; keep data on the host (§3.3).")


def check_agent_tool(tool, ti):
    iso = (ti.get("isolation") or ti.get("isolationMode") or "")
    if isinstance(iso, str) and iso.lower() in ("remote", "cloud"):
        block(f"{TAG} {tool} -> blocked: remote/cloud isolation runs the work outside this host's guard; keep subagents local so every tool call passes this hook (§3.3).")
    _refuse_secret_shape_in(tool, ti)


def check_schedule_tool(tool, ti):
    # Scheduling itself is a layer-1/2 concern; the guard only refuses a prompt
    # that would carry a secret into a scheduled run.
    _refuse_secret_shape_in(tool, ti)


def check_enter_worktree(ti, cwd):
    raw = ti.get("path") or ""
    if not raw:
        return
    p = expand_path(raw, cwd)
    if p is None:
        return
    if under(p, R_PROD) or under(p, R_DOCUMENTS) or is_guard_path(p) or is_secret_path(p):
        block(f"{TAG} EnterWorktree {raw} -> blocked: the session may not move its write access to the production checkout, ~/Documents, a secret path or the guard files; work in a worktree under {WORK} (§3.3).")


def check_mcp_tool(tool, ti):
    _refuse_secret_shape_in(tool, ti)
    leaf = tool.split("__")[-1]  # the tool name after the server segment(s)
    if not MCP_READONLY.match(leaf):
        block(f"{TAG} {tool} -> blocked: MCP tools reach external services; only clearly read-only MCP calls whose name begins with a read verb (get/list/read/view/search/...) pass this guard (§3.3).")


def _deadline_reached(signum, frame):
    # Terminate hard with exit 2. Raising here could let the alarm fire inside an
    # except/finally and escape main() as exit 1, which Claude Code treats as
    # non-blocking (fail-open). os._exit guarantees a blocking exit code.
    try:
        sys.stderr.write(f"{TAG} the guard did not finish within its {GUARD_DEADLINE_S}s budget; "
                         "blocking (fail-closed) so a slow check can never let a call through by timing out. "
                         "Split or simplify the command.\n")
        sys.stderr.flush()
    finally:
        os._exit(2)


def main():
    try:
        data = sys.stdin.read(MAX_PAYLOAD_BYTES + 1)
        if len(data) > MAX_PAYLOAD_BYTES:
            print(f"{TAG} the hook payload is larger than {MAX_PAYLOAD_BYTES} bytes; blocking (fail-closed).", file=sys.stderr)
            return 2
        payload = json.loads(data)
    except Exception as exc:
        print(f"{TAG} could not read the hook payload ({exc}); blocking (fail-closed).", file=sys.stderr)
        return 2
    armed = hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")
    if armed:
        signal.signal(signal.SIGALRM, _deadline_reached)
        signal.setitimer(signal.ITIMER_REAL, GUARD_DEADLINE_S)
    try:
        evaluate(payload)
    except Block as b:
        if armed:  # stop the clock before reporting so the alarm cannot interrupt the print
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_IGN)
        print(str(b), file=sys.stderr)
        return 2
    except BaseException as exc:  # fail closed on anything else
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_IGN)
        print(f"{TAG} internal error ({type(exc).__name__}: {exc}); blocking (fail-closed). Simplify the command.", file=sys.stderr)
        return 2
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
