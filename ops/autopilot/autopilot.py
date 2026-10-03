#!/usr/bin/python3 -I
"""Autopilot runner for the platform upgrade programme.

Runs fresh `claude -p` cycles in the dev worktree, one after another, so the
programme in docs/ai-platform-upgrade/MASTER_PROMPT.md continues without an
operator session. Each cycle is a new Claude Code session that reads the
governing files; nothing important lives in chat history. The operator
approved this runner on 2026-10-03 (MASTER_PROMPT.md section 0, decision 9).

Between cycles it:
  * waits for a usage-limit reset (the reset time in the result text, plus
    2-5 minutes of jitter), or backs off 20 -> 40 -> 60 minutes when no reset
    time is given;
  * backs off exponentially on network, overload and 5xx errors;
  * on an authentication failure, records the fix for the operator in
    ~/.llm-autopilot/NEEDS_HUMAN.runtime.md and retries every 30 minutes;
  * restarts after a crash in 60 s, and sleeps 2 h after 6 crashes in a row;
    a cycle cut off by a signal counts toward that cap like a crash, but a
    lone one never triggers it and resumes after the short pause;
  * treats a cycle that reaches its timeout (4 h; SIGKILL 120 s later if the
    CLI ignores SIGTERM) as a normal end, like one that reaches max turns;
  * honours ~/.llm-autopilot/STOP (finish the cycle, exit), PAUSE and the
    AUTOPILOT_PAUSE_WINDOWS operator setting;
  * stops for good when RESUME.md says STATUS: COMPLETE and FINAL_REPORT.md
    exists, or after MAX_AUTONOMOUS_DAYS (touch RENEW and restart to go on).

State lives in ~/.llm-autopilot (state.json, heartbeat.json, events.jsonl,
logs/). Exit codes: 0 test run finished, 3 another instance holds the lock,
64 stopped on purpose (systemd does not restart on 3 or 64).

ops/autopilot/install.sh installs this file into ~/.llm-autopilot/bin, where
the agent cannot modify it; the copy in the repository is the source.
"""

import datetime
import fcntl
import json
import os
import pwd
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

HOME = pwd.getpwuid(os.getuid()).pw_dir
EXIT_LOCKED = 3
EXIT_STOPPED = 64


def env(name, default):
    return os.environ.get(name, default)


def _tool(name, *candidates):
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    found = shutil.which(name)
    return found or candidates[0]


AP_HOME = env("AP_HOME", os.path.join(HOME, ".llm-autopilot"))
WORKTREE = env("AP_WORKTREE", os.path.join(HOME, "work/llm-dev"))
CLAUDE = env("AP_CLAUDE", os.path.join(HOME, ".npm-global/bin/claude"))
# Launch the wrappers by absolute path, so the cycle environment's PATH cannot
# choose a different `nice`, `ionice` or `timeout` binary.
NICE = _tool("nice", "/usr/bin/nice", "/bin/nice")
IONICE = _tool("ionice", "/usr/bin/ionice", "/bin/ionice")
TIMEOUT = _tool("timeout", "/usr/bin/timeout", "/bin/timeout")
SETTINGS = env("AP_SETTINGS", os.path.join(AP_HOME, "settings.autopilot.json"))
PROMPT_FILE = env("AP_PROMPT_FILE", os.path.join(AP_HOME, "bin/CYCLE_PROMPT.md"))
MASTER = env("AP_MASTER", os.path.join(AP_HOME, "MASTER_PROMPT.md"))
TEST_DB_VARS = env("AP_TEST_DB_VARS", os.path.join(AP_HOME, "agent/test-db.vars"))
DOCS = os.path.join(WORKTREE, "docs/ai-platform-upgrade")
TIME_SCALE = float(env("AP_TIME_SCALE", "1"))
MAX_CYCLES = int(env("AP_MAX_CYCLES", "0"))
CYCLE_TIMEOUT_S = int(env("AP_CYCLE_TIMEOUT_S", str(4 * 3600)))
# A CLI still running KILL_AFTER_S after the cycle timeout's SIGTERM gets SIGKILL.
KILL_AFTER_S = float(env("AP_KILL_AFTER_S", "120"))
MAX_TURNS = int(env("AP_MAX_TURNS", "150"))
HEARTBEAT_S = float(env("AP_HEARTBEAT_S", "60"))
BETWEEN_CYCLES_S = float(env("AP_BETWEEN_CYCLES_S", "30"))
JITTER = (float(env("AP_JITTER_MIN_S", "120")), float(env("AP_JITTER_MAX_S", "300")))
LIMIT_BACKOFF_MIN = [20, 40, 60]
CRASH_RESTART_S = 60
CRASH_CAP = 6
CRASH_CAP_SLEEP_S = 2 * 3600
AUTH_RETRY_S = 30 * 60
DISK_RETRY_S = 30 * 60
FINAL_CHECKPOINT_MAX_ATTEMPTS = 8
NET_BACKOFF_MAX_S = 30 * 60
MIN_FREE_FRACTION = 0.15
LOG_KEEP = int(env("AP_LOG_KEEP", "400"))
LOG_MAX_TOTAL_MB = int(env("AP_LOG_MAX_TOTAL_MB", "4096"))
LOG_MAX_CYCLE_MB = int(env("AP_LOG_MAX_CYCLE_MB", "300"))
SERVICE = env("AP_SERVICE_NAME", "llm-autopilot.service")
NO_SYSTEMCTL = env("AP_NO_SYSTEMCTL", "") == "1"
LOCAL_TZ = ZoneInfo(env("AP_TZ", "Asia/Kolkata"))

STATE_FILE = os.path.join(AP_HOME, "state.json")
HEARTBEAT_FILE = os.path.join(AP_HOME, "heartbeat.json")
LOCK_FILE = os.path.join(AP_HOME, "lock")
LOG_DIR = os.path.join(AP_HOME, "logs")
STOPFAIL_FILE = os.path.join(AP_HOME, "stopfailure.jsonl")
RUNTIME_NEEDS_HUMAN = os.path.join(AP_HOME, "NEEDS_HUMAN.runtime.md")
EVENTS_FILE = os.path.join(AP_HOME, "events.jsonl")


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def nap(seconds):
    """Sleep in scaled time (tests run with AP_TIME_SCALE << 1)."""
    time.sleep(max(0.0, seconds * TIME_SCALE))


# --------------------------------------------------------------------------
# Operator settings (read from the host copy of the master prompt every loop)
# --------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "DEV_BRANCH": "autopilot/dev",
    "MAX_AUTONOMOUS_DAYS": 14,
    "AUTOPILOT_MODEL": "opus",
    "AUTOPILOT_EFFORT": "xhigh",
    "AUTOPILOT_PAUSE_WINDOWS": [],
}


LIST_KEYS = {"AUTOPILOT_PAUSE_WINDOWS"}


def _parse_list(val):
    """Parse a YAML/JSON flow list that may use single or double quotes.
    Raises ValueError when the value cannot be read as a list."""
    try:
        return json.loads(val)
    except ValueError:
        pass
    import ast
    try:
        parsed = ast.literal_eval(val)
    except (ValueError, SyntaxError):
        raise ValueError(f"not a list: {val!r}")
    if isinstance(parsed, (list, tuple)):
        return [str(x) for x in parsed]
    raise ValueError(f"not a list: {val!r}")


def operator_settings(path=None, warn=None):
    """Read the operator settings from the YAML block of the master prompt. A
    value that cannot be parsed is reported through `warn` (an event callback)
    rather than silently dropped (R11)."""
    out = dict(DEFAULT_SETTINGS)
    try:
        with open(path or MASTER, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return out
    m = re.search(r"```yaml\n(.*?)```", text, re.S)
    if not m:
        return out
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        raw = re.sub(r"\s+#.*$", "", lines[i])
        i += 1
        mm = re.match(r"^([A-Z_]+):\s*(.*)$", raw.strip())
        if not mm:
            continue
        key, val = mm.group(1), mm.group(2).strip()
        # Block list: 'KEY:' then indented '- item' lines.
        if val == "":
            items, block = [], False
            while i < len(lines):
                item = re.sub(r"\s+#.*$", "", lines[i])
                bm = re.match(r"^\s+-\s+(.*)$", item)
                if not bm:
                    break
                block = True
                items.append(bm.group(1).strip().strip("'\""))
                i += 1
            if block:
                out[key] = items
                continue
        if val.startswith("["):
            try:
                out[key] = _parse_list(val)
            except ValueError as exc:
                if warn:
                    warn(key, str(exc))
                continue  # leave the default in place rather than guessing []
        elif re.fullmatch(r"-?\d+", val):
            out[key] = int(val)
        elif key in LIST_KEYS:
            if warn:
                warn(key, f"expected a list, got {val!r}")
        elif val != "":
            out[key] = val.strip("'\"")
    # A window string that does not match the expected shape is reported too.
    windows = out.get("AUTOPILOT_PAUSE_WINDOWS")
    if not isinstance(windows, list):
        out["AUTOPILOT_PAUSE_WINDOWS"] = windows = []
    for w in windows:
        if not re.match(r"^\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}", str(w)) and warn:
            warn("AUTOPILOT_PAUSE_WINDOWS", f"ignored window {w!r}: not HH:MM-HH:MM [tz]")
    return out


def in_pause_window(windows, at=None):
    """Windows look like '10:00-18:00 Asia/Kolkata'; overnight windows wrap."""
    at = at or now()
    for w in windows or []:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*(\S+)?\s*$", str(w))
        if not m:
            continue
        try:
            tz = ZoneInfo(m.group(5)) if m.group(5) else LOCAL_TZ
        except Exception:
            tz = LOCAL_TZ
        local = at.astimezone(tz)
        start = int(m.group(1)) * 60 + int(m.group(2))
        end = int(m.group(3)) * 60 + int(m.group(4))
        cur = local.hour * 60 + local.minute
        if (start <= cur < end) if start <= end else (cur >= start or cur < end):
            return True
    return False


# --------------------------------------------------------------------------
# Reset-time parsing
# --------------------------------------------------------------------------

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
LIMIT_RE = re.compile(r"(hit your [\w -]*limit|usage limit|limit reached|rate[ _-]?limit|too many requests|\b429\b|out of (extra )?usage)", re.I)


def _clock(s):
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s.strip(), re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def parse_reset(text, at=None):
    """Return the UTC reset time named in a usage-limit message, or None."""
    at = at or now()
    m = re.search(r"limit reached\|(\d{10})\b", text or "")
    if m:
        return datetime.datetime.fromtimestamp(int(m.group(1)), datetime.timezone.utc)
    # Capture the time phrase, stopping at a timezone in parentheses or at the
    # separators Claude Code appends ('·', '∙', '|', ' /...') (R10).
    m = re.search(r"resets?\s+(?:at\s+|on\s+)?(.+?)(?:\s*\(([A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)+|UTC)\))?\s*(?:[.\n\"\\·∙|]|\s/|$)", text or "", re.I)
    if not m:
        return None
    when, tzname = m.group(1).strip().rstrip(".,"), m.group(2)
    try:
        tz = ZoneInfo(tzname) if tzname else LOCAL_TZ
    except Exception:
        tz = LOCAL_TZ
    local = at.astimezone(tz)
    rel = re.match(r"^in\s+(?:(\d+)\s*d(?:ays?)?)?\s*(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?$", when, re.I)
    if rel and any(rel.groups()):
        d, h, mi = (int(x or 0) for x in rel.groups())
        return at + datetime.timedelta(days=d, hours=h, minutes=mi)
    tom = re.match(r"^tomorrow(?:\s+at)?\s+(.+)$", when, re.I)
    if tom:
        c = _clock(tom.group(1))
        if not c:
            return None
        target = local.replace(hour=c[0], minute=c[1], second=0, microsecond=0) + datetime.timedelta(days=1)
        return target.astimezone(datetime.timezone.utc)
    wd = re.match(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+(.+)$", when, re.I)
    md = re.match(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(?:at\s+)?(.+)$", when, re.I)
    if wd:
        c = _clock(wd.group(2))
        if not c:
            return None
        target = local.replace(hour=c[0], minute=c[1], second=0, microsecond=0)
        target += datetime.timedelta(days=(WEEKDAYS[wd.group(1).lower()[:3]] - local.weekday()) % 7)
        if target <= local:
            target += datetime.timedelta(days=7)
    elif md:
        c = _clock(md.group(3))
        if not c:
            return None
        try:
            target = local.replace(month=MONTHS[md.group(1).lower()[:3]], day=int(md.group(2)), hour=c[0], minute=c[1], second=0, microsecond=0)
        except ValueError:
            return None
        if target <= local - datetime.timedelta(days=1):
            target = target.replace(year=target.year + 1)
    else:
        c = _clock(when)
        if not c:
            return None
        target = local.replace(hour=c[0], minute=c[1], second=0, microsecond=0)
        if target <= local:
            # A bare clock time that has only just passed almost always means
            # "now" (the limit cleared at that minute), not 24 h from now (R10).
            if local - target <= datetime.timedelta(hours=2):
                return at + datetime.timedelta(seconds=1)
            target += datetime.timedelta(days=1)
    return target.astimezone(datetime.timezone.utc)


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

SECRET_SHAPES = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{40,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|\bhf_[A-Za-z0-9]{30,}"
    r"|\btsk_(?:live|test)_[0-9a-f]{16}_[A-Za-z0-9_-]{8,}"       # this platform's API keys
    r"|(?i:\bbearer)\s+[A-Za-z0-9._~+/=-]{8,}"                    # Authorization: Bearer <token>
    r")"
)
URL_CRED = re.compile(r"\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|https?)://[^:/\s\"'@]+:)([^@\s\"']{3,})(@)")
# key=value / key: value secrets. The value stops at the first structural
# character so the surrounding JSON (quotes, commas, braces) stays intact.
KV_SECRET = re.compile(r"(?i)(\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key)[\w.-]*\b\s*[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}\])]{6,})")
# --password <v>, --token=<v>, 'password <v>' and similar CLI forms.
KV_FLAG_SECRET = re.compile(r"(?i)(--?(?:password|passwd|secret|token|api[-_]?key|auth[-_]?token)[= ]\s*[\"']?)([^\s\"',;}\])]{3,})")
KEYISH = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key)")


def secret_files():
    prod = os.path.join(HOME, "Documents/project/personal-LLM-Chabot")
    return [os.path.join(prod, ".env"), os.path.join(prod, ".runtime/secrets.env"),
            os.path.join(prod, ".runtime/generated.env"), os.path.join(WORKTREE, ".env"), TEST_DB_VARS]


class Redactor:
    def __init__(self, files=None):
        self.values = []
        for f in files if files is not None else secret_files():
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
                        if not m:
                            continue
                        k, v = m.group(1), m.group(2).strip().strip("'\"")
                        if len(v) >= 8 and (re.search(r"KEY|TOKEN|SECRET|PASS|PWD|CREDENTIAL|PRIVATE|AUTH|COOKIE|SALT|SIGN|DSN|DATABASE_URL", k, re.I)
                                            or (len(v) >= 20 and re.search(r"[A-Z]", v) and re.search(r"[a-z]", v) and re.search(r"\d", v))):
                            self.values.append(v)
                        for part in re.findall(r"://[^:/\s]+:([^@\s]{8,})@", v):
                            self.values.append(part)
            except OSError:
                continue
        self.values = sorted(set(self.values), key=len, reverse=True)

    def _scrub(self, text):
        """Redact secrets in a run of plain text (no structure to preserve)."""
        for v in self.values:
            if v in text:
                text = text.replace(v, "[REDACTED]")
        text = SECRET_SHAPES.sub("[REDACTED]", text)
        text = URL_CRED.sub(r"\1[REDACTED]\3", text)
        text = KV_FLAG_SECRET.sub(r"\1[REDACTED]", text)
        return KV_SECRET.sub(r"\1[REDACTED]", text)

    def _walk(self, obj):
        """Redact string leaves of a decoded JSON value; keep numbers and structure.
        A string leaf is first decoded again, so escaped inner JSON is scrubbed too."""
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(v, str) and isinstance(k, str) and KEYISH.search(k):
                    out[k] = "[REDACTED]"  # a secret-named field: redact its whole value
                else:
                    out[k] = self._walk(v)
            return out
        if isinstance(obj, list):
            return [self._walk(v) for v in obj]
        if isinstance(obj, str):
            if obj.lstrip()[:1] in ("{", "[") and KEYISH.search(obj):
                try:
                    return json.dumps(self._walk(json.loads(obj)))
                except ValueError:
                    pass
            return self._scrub(obj)
        return obj  # int, float, bool, None: never a secret, never corrupt it

    def __call__(self, text):
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            try:
                obj = json.loads(stripped)
            except ValueError:
                obj = None
            if obj is not None:
                out = json.dumps(self._walk(obj), ensure_ascii=False)
                return out + "\n" if text.endswith("\n") else out
        return self._scrub(text)


# The only keys the runner copies from test-db.vars into a cycle's environment.
# Everything else is dropped: that file sits in the agent-writable zone, and a
# PATH, NODE_OPTIONS, LD_PRELOAD or ANTHROPIC_* line there could otherwise
# choose the binary or configuration that launches the next Claude session.
TEST_DB_ALLOWED_KEYS = {"TEST_DATABASE_URL", "TEST_DATABASE_ALLOWED_HOSTS", "TEST_DATABASE_REMOTE"}


def load_vars(path, allowed=None):
    out, dropped = {}, []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.rstrip("\n"))
                if not m:
                    continue
                k = m.group(1)
                if allowed is not None and k not in allowed:
                    dropped.append(k)
                    continue
                out[k] = m.group(2)
    except OSError:
        pass
    return out, dropped


# --------------------------------------------------------------------------
# Outcome classification
# --------------------------------------------------------------------------

AUTH_RE = re.compile(r"(invalid api key|please run /login|not logged in|authentication[_ ]failed|oauth token (has )?expired|\b401\b|unauthori[sz]ed|credentials? (are |is )?(missing|invalid))", re.I)
CONFIG_RE = re.compile(r"(model[_ ]not[_ ]found|unknown model|invalid model|not available for your account|unknown option|error: option)", re.I)
TRANSIENT_RE = re.compile(r"(overloaded|\b5\d\d\b|econnreset|etimedout|enotfound|eai_again|socket hang up|fetch failed|network error|connection (error|reset|refused)|timed? ?out|api_error|server_error)", re.I)


def _first(rx, blob):
    m = rx.search(blob or "")
    if not m:
        return None
    s = max(0, m.start() - 120)
    return blob[s : m.end() + 160].replace("\n", " ").strip()


# Process exit codes that mean the cycle was cut off rather than finished.
INTERRUPT_RCS = {143, -15, 130, -2, 129, -1}
# Exit codes of a cycle that SIGKILL ended. `timeout` leads the cycle's process
# group, and the SIGKILL it sends to that group after --kill-after reaches
# `timeout` itself, so the runner sees -9 (137 where a shell reports it); the
# same codes come from an outside kill such as the OOM killer. Only the time
# the cycle ran tells the two apart.
KILLED_RCS = {-9, 137}


def rate_event_reset(rate_events):
    """The resetsAt epoch of the most recent rejected rate_limit_event, or None."""
    reset = None
    for ev in rate_events or []:
        info = ev if isinstance(ev, dict) else {}
        status = str(info.get("status") or "").lower()
        if status in ("rejected", "blocked", "exceeded"):
            at = info.get("resetsAt") or info.get("resets_at")
            if isinstance(at, (int, float)):
                reset = datetime.datetime.fromtimestamp(int(at), datetime.timezone.utc)
            elif isinstance(at, str) and at.isdigit():
                reset = datetime.datetime.fromtimestamp(int(at), datetime.timezone.utc)
    return reset


def classify(rc, result, texts, fails, stop_signal=False, rate_events=None, timed_out=False):
    """Map a finished cycle to ok, interrupted, max_turns, timeout, usage_limit,
    auth, config, transient or crash. Returns (outcome, reset_at, detail).

    `timed_out` says the cycle ran for at least the cycle timeout, so a
    SIGKILL ending (KILLED_RCS) was the timeout's kill-after, not a crash.

    `texts` carries only reliable signals (the final result text, stderr and
    system api_error messages), never assistant prose, so a cycle that merely
    discusses rate limits or 429s is not misread as a usage limit.
    `fails` are StopFailure records from the cycle's main session only.
    """
    types = {f.get("error_type") for f in fails or []}
    blob = "\n".join([str((result or {}).get("result") or "")] + [str(x) for x in texts or []])
    subtype = (result or {}).get("subtype") or ""
    is_error = bool((result or {}).get("is_error"))
    clean_success = bool(result) and subtype == "success" and not is_error and rc == 0
    rejected = any(str((e or {}).get("status") or "").lower() in ("rejected", "blocked", "exceeded") for e in rate_events or [])
    # The cycle was cut off (service restart, SIGTERM) rather than finished (R2).
    # This precedes the success branch: a cycle stopped by a signal did not run
    # to its own end even if the last result it managed to emit was a success.
    if rc in INTERRUPT_RCS or stop_signal:
        return "interrupted", None, f"cycle interrupted (rc={rc}{', stop signal' if stop_signal else ''})"
    # A genuine, completed success wins over any earlier StopFailure record (R8).
    if clean_success:
        return "ok", None, None
    # Structured limit/auth/config signals are reliable; take them before text.
    if "rate_limit" in types or rejected:
        return "usage_limit", rate_event_reset(rate_events) or parse_reset(blob), _first(LIMIT_RE, blob) or "rate limit"
    if types & {"authentication_failed", "oauth_org_not_allowed", "account_on_hold", "billing_error"}:
        return "auth", None, ",".join(sorted(t for t in types if t))
    if types & {"model_not_found", "invalid_request"}:
        return "config", None, ",".join(sorted(t for t in types if t))
    # A max-turns or timeout end is a normal cycle end (§5.5); check it before
    # the text heuristics so an answer that mentions a limit does not override it.
    if subtype == "error_max_turns":
        return "max_turns", None, "max turns reached"
    if rc == 124 or (rc in KILLED_RCS and timed_out):
        return "timeout", None, f"cycle timeout (rc={rc})"
    if LIMIT_RE.search(blob) and (is_error or rc != 0 or not result):
        return "usage_limit", rate_event_reset(rate_events) or parse_reset(blob), _first(LIMIT_RE, blob)
    if AUTH_RE.search(blob) and (is_error or rc != 0):
        return "auth", None, _first(AUTH_RE, blob)
    if CONFIG_RE.search(blob) and (is_error or rc != 0) and not result:
        return "config", None, _first(CONFIG_RE, blob)
    if types & {"overloaded", "server_error"} or (TRANSIENT_RE.search(blob) and (is_error or rc != 0)):
        return "transient", None, _first(TRANSIENT_RE, blob) or ",".join(sorted(t for t in types if t))
    if result and subtype.startswith("error") and rc == 0:
        return "crash", None, f"result subtype {subtype}"
    return "crash", None, (blob.strip()[-300:] or f"exit code {rc}")


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

class Runner:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.state = self.load_state()
        self.lock_fh = None
        self.child = None
        self.stop_signal = False
        self.hb_stop = threading.Event()
        self.hb_lock = threading.Lock()
        self.cycles_run = 0
        self.log_path = None
        self.log_bytes = 0
        self.prompt_file = PROMPT_FILE
        self.redact = Redactor()

    # ---- persistence
    def load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        st.setdefault("started_at", iso(now()))
        st.setdefault("cycle", 0)
        st.setdefault("state", "starting")
        st.setdefault("consecutive_failures", 0)
        st.setdefault("consecutive_interrupts", 0)
        st.setdefault("limit_backoff_idx", 0)
        st.setdefault("net_backoff_idx", 0)
        st.setdefault("limit_wait_s_total", 0)
        st.setdefault("history", [])
        return st

    def save_state(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, STATE_FILE)

    def event(self, kind, **kw):
        rec = {"ts": iso(now()), "kind": kind, "cycle": self.state.get("cycle"), **kw}
        with open(EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    def set_state(self, name, **kw):
        self.state["state"] = name
        self.state.update(kw)
        self.save_state()
        self.write_heartbeat()

    # ---- heartbeat
    def current_task(self):
        try:
            with open(os.path.join(DOCS, "TASK_BOARD.md"), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return None
        for status in ("IN_PROGRESS", "READY"):
            for line in text.splitlines():
                if f"| {status} |" in line or line.strip().startswith(f"{status} "):
                    return re.sub(r"\s+", " ", line.strip(" |-*"))[:200]
        return None

    def last_commit(self):
        try:
            r = subprocess.run(["git", "-C", WORKTREE, "log", "-1", "--format=%h %cI %s"], capture_output=True, text=True, timeout=10)
            return r.stdout.strip()[:200] or None
        except Exception:
            return None

    def write_heartbeat(self):
        hb = {
            "ts": iso(now()),
            "pid": os.getpid(),
            "cycle": self.state.get("cycle"),
            "state": self.state.get("state"),
            "current_task": self.current_task(),
            "last_commit": self.last_commit(),
            "next_wake": self.state.get("next_wake"),
            "last_error": self.state.get("last_error"),
            "last_outcome": (self.state.get("last_cycle") or {}).get("outcome"),
            "log_file": self.log_path,
            "log_bytes": self.log_bytes,
            "started_at": self.state.get("started_at"),
            "limit_wait_hours_total": round(self.state.get("limit_wait_s_total", 0) / 3600, 2),
        }
        # Both the heartbeat thread and set_state() write this file; a shared
        # .tmp path would let them publish a half-written heartbeat (R13).
        tmp = f"{HEARTBEAT_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
        with self.hb_lock:
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(hb, fh, indent=2)
                os.replace(tmp, HEARTBEAT_FILE)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def heartbeat_loop(self):
        while not self.hb_stop.wait(max(0.05, HEARTBEAT_S * TIME_SCALE)):
            self.write_heartbeat()

    # ---- logs
    def rotate_logs(self):
        files = sorted((os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR) if f.startswith("cycle-")), key=os.path.getmtime)
        total = sum(os.path.getsize(f) for f in files)
        while files and (len(files) > LOG_KEEP or total > LOG_MAX_TOTAL_MB * 1024 * 1024):
            f = files.pop(0)
            total -= os.path.getsize(f)
            os.remove(f)

    # ---- lock and control files
    def acquire_lock(self):
        self.lock_fh = open(LOCK_FILE, "a+")
        try:
            fcntl.flock(self.lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_fh.seek(0)
            holder = self.lock_fh.read().strip()
            print(f"autopilot: another instance holds {LOCK_FILE} (pid {holder or '?'}); refusing to start", file=sys.stderr)
            return False
        self.lock_fh.seek(0)
        self.lock_fh.truncate()
        self.lock_fh.write(str(os.getpid()))
        self.lock_fh.flush()
        return True

    def flag(self, name):
        return os.path.exists(os.path.join(AP_HOME, name))

    def complete(self):
        try:
            with open(os.path.join(DOCS, "RESUME.md"), encoding="utf-8") as fh:
                resume = fh.read()
        except OSError:
            return False
        return bool(re.search(r"^\s*STATUS:\s*COMPLETE\s*$", resume, re.M)) and os.path.exists(os.path.join(DOCS, "FINAL_REPORT.md"))

    def disable_service(self):
        if NO_SYSTEMCTL:
            self.event("disable-service-skipped", reason="AP_NO_SYSTEMCTL=1")
            return
        subprocess.run(["systemctl", "--user", "disable", SERVICE], capture_output=True, timeout=30)
        self.event("service-disabled", service=SERVICE)

    def needs_human(self, title, body):
        with open(RUNTIME_NEEDS_HUMAN, "a", encoding="utf-8") as fh:
            fh.write(f"\n## {iso(now())} — {title}\n\n{body}\n")
        self.event("needs-human", title=title)

    def free_fraction(self):
        fr = 1.0
        for p in (WORKTREE, AP_HOME):
            try:
                st = os.statvfs(p)
                fr = min(fr, st.f_bavail / st.f_blocks)
            except OSError:
                pass
        return fr

    # ---- sleeping that stays responsive to STOP, PAUSE, WAKE and signals
    def sleep_until(self, wake, state_name):
        # next_wake is already in state; keep it so a restart in the middle of
        # this wait still honours the reset time (R3). Clear it only when the
        # wait is served, WAKE is used, or a cycle starts.
        self.set_state(state_name, next_wake=iso(wake))
        left = (wake - now()).total_seconds()
        while left > 0:
            if self.stop_signal or self.flag("STOP"):
                return  # keep next_wake; a restart or unpause resumes the wait
            if self.flag("WAKE"):
                os.remove(os.path.join(AP_HOME, "WAKE"))
                self.event("woken")
                self.state["next_wake"] = None
                self.save_state()
                return
            if self.flag("PAUSE"):
                return  # a pause during a wait is visible, and the wait is not lost
            step = min(left, 60.0)
            nap(step)
            left -= step
        self.state["next_wake"] = None
        self.save_state()

    # ---- one cycle
    def run_cycle(self, settings):
        self.state["cycle"] = int(self.state.get("cycle", 0)) + 1
        n = self.state["cycle"]
        self.rotate_logs()
        self.log_path = os.path.join(LOG_DIR, f"cycle-{n:05d}.jsonl")
        self.log_bytes = 0
        self.redact = Redactor()
        with open(self.prompt_file, encoding="utf-8") as fh:
            prompt = fh.read().strip()
        cmd = [
            NICE, "-n", "10", IONICE, "-c2", "-n7",
            TIMEOUT, f"--kill-after={KILL_AFTER_S:g}s", f"{CYCLE_TIMEOUT_S}s",
            CLAUDE, "-p", prompt,
            "--permission-mode", "auto",
            "--permission-prompts", "none",
            "--settings", SETTINGS,
            "--model", str(settings.get("AUTOPILOT_MODEL") or "opus"),
            "--effort", str(settings.get("AUTOPILOT_EFFORT") or "xhigh"),
            "--max-turns", str(MAX_TURNS),
            "--output-format", "stream-json",
            "--verbose",
        ]
        child_env = dict(os.environ)
        test_db, dropped = load_vars(TEST_DB_VARS, allowed=TEST_DB_ALLOWED_KEYS)
        if dropped:
            self.event("test-db-vars-ignored", keys=sorted(set(dropped)))
        child_env.update(test_db)
        child_env.update({"LLM_AUTOPILOT": "1", "LLM_AUTOPILOT_CYCLE": str(n),
                          "LLM_AUTOPILOT_PREV_OUTCOME": str(self.state.get("prev_outcome") or ""),
                          # Print mode otherwise terminates background tasks (a
                          # running workflow) 600 s after the main turn ends; wait
                          # for them instead. The cycle timeout still bounds it.
                          "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"})
        started = now()
        started_mono = time.monotonic()
        stopfail_offset = os.path.getsize(STOPFAIL_FILE) if os.path.exists(STOPFAIL_FILE) else 0
        self.set_state("working", next_wake=None, cycle_started=iso(started))
        self.event("cycle-start", log=self.log_path, model=settings.get("AUTOPILOT_MODEL"), effort=settings.get("AUTOPILOT_EFFORT"))
        result, texts, cap = None, [], LOG_MAX_CYCLE_MB * 1024 * 1024
        rate_events, main_session = [], None
        turns_total, cost_total, result_count = 0, 0.0, 0
        with open(self.log_path, "w", encoding="utf-8") as log:
            try:
                self.child = subprocess.Popen(cmd, cwd=WORKTREE, env=child_env, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                              errors="replace", start_new_session=True)
            except OSError as exc:
                self.child = None
                log.write(json.dumps({"type": "runner_error", "error": str(exc)}) + "\n")
                return self.finish_cycle(n, started, 127, None, [str(exc)], stopfail_offset, None)
            err_lines = []

            def pump_err():
                for line in self.child.stderr:
                    err_lines.append(line)

            t = threading.Thread(target=pump_err, daemon=True)
            t.start()
            try:
                for line in self.child.stdout:
                    red = self.redact(line)
                    if self.log_bytes < cap:
                        log.write(red if red.endswith("\n") else red + "\n")
                        log.flush()
                        self.log_bytes += len(red)
                        if self.log_bytes >= cap:
                            log.write(json.dumps({"type": "runner_note", "note": f"log truncated at {LOG_MAX_CYCLE_MB} MB"}) + "\n")
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue  # a bare list/number/null is not a stream event (R12)
                    mtype = msg.get("type")
                    if mtype == "result":
                        result = msg  # keep the last; accumulate the totals
                        result_count += 1
                        if isinstance(msg.get("num_turns"), (int, float)):
                            turns_total += msg["num_turns"]
                        if isinstance(msg.get("total_cost_usd"), (int, float)):
                            cost_total += msg["total_cost_usd"]
                        if bool(msg.get("is_error")):
                            texts.append(str(msg.get("result") or "")[-2000:])
                    elif mtype == "system":
                        if main_session is None and msg.get("subtype") == "init" and msg.get("session_id"):
                            main_session = msg.get("session_id")
                        info = msg.get("rate_limit_info") or msg.get("rate_limit") or (msg.get("message") or {}).get("rate_limit_info")
                        if isinstance(info, dict):
                            rate_events.append(info)
                        if msg.get("subtype") in ("api_error", "error", "api_retry"):
                            texts.append(json.dumps(msg)[-2000:])
                rc = self.child.wait()
            finally:
                self._terminate_child()
            t.join(timeout=10)
            if err_lines:
                tail = self.redact("".join(err_lines))[-20000:]
                log.write(json.dumps({"type": "stderr", "text": tail}) + "\n")
                texts.append(tail[-4000:])
        self.child = None
        return self.finish_cycle(n, started, rc, result, texts, stopfail_offset, main_session,
                                 rate_events=rate_events, turns_total=turns_total, cost_total=cost_total,
                                 result_count=result_count, elapsed_s=time.monotonic() - started_mono)

    def _terminate_child(self):
        child = self.child
        if child and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                child.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except OSError:
                    pass

    def finish_cycle(self, n, started, rc, result, texts, stopfail_offset, main_session,
                     rate_events=None, turns_total=None, cost_total=None, result_count=0, elapsed_s=None):
        fails = []
        try:
            with open(STOPFAIL_FILE, encoding="utf-8") as fh:
                fh.seek(stopfail_offset)
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    # Only the main session's own failed turns classify the cycle;
                    # a subagent's failure or an intermediate turn does not (R8).
                    if rec.get("agent_id"):
                        continue
                    if main_session and rec.get("session_id") and rec.get("session_id") != main_session:
                        continue
                    fails.append(rec)
        except OSError:
            pass
        timed_out = elapsed_s is not None and elapsed_s >= CYCLE_TIMEOUT_S
        outcome, reset_at, detail = classify(rc, result, texts, fails, stop_signal=self.stop_signal,
                                             rate_events=rate_events, timed_out=timed_out)
        rec = {
            "cycle": n,
            "started": iso(started),
            "ended": iso(now()),
            "rc": rc,
            "outcome": outcome,
            "subtype": (result or {}).get("subtype"),
            "num_turns": turns_total if turns_total else (result or {}).get("num_turns"),
            "result_count": result_count,
            "cost_usd": cost_total if cost_total else (result or {}).get("total_cost_usd"),
            "permission_denials": len((result or {}).get("permission_denials") or []),
            "stop_failures": [f.get("error_type") for f in fails],
            "reset_at": iso(reset_at),
            "detail": self.redact(detail)[:500] if detail else None,
            "log": self.log_path,
        }
        self.state["last_cycle"] = rec
        self.state["prev_outcome"] = outcome
        self.state["history"] = (self.state.get("history") or [])[-49:] + [rec]
        self.save_state()
        self.event("cycle-end", **{k: rec[k] for k in ("rc", "outcome", "subtype", "num_turns", "result_count", "permission_denials", "reset_at", "detail")})
        return outcome, reset_at, detail

    # ---- policy after a cycle
    def after(self, outcome, reset_at, detail):
        t = now()
        if outcome != "interrupted":
            self.state["consecutive_interrupts"] = 0  # any other end breaks a run of interruptions
        if outcome in ("ok", "max_turns", "timeout"):
            self.state.update(consecutive_failures=0, limit_backoff_idx=0, net_backoff_idx=0, last_error=None)
            return t + datetime.timedelta(seconds=BETWEEN_CYCLES_S), "idle"
        if outcome == "interrupted":
            # Cut off, not finished (a service restart, a stray SIGTERM). One
            # interruption on its own, such as an operator restart, starts a
            # fresh cycle after the short pause and never the cap. A run of them
            # is a failure like a crash (§5.5): each counts toward the crash cap,
            # so a cycle that is killed every time cannot loop without backoff.
            interrupts = int(self.state.get("consecutive_interrupts", 0)) + 1
            fails = int(self.state.get("consecutive_failures", 0)) + 1
            self.state.update(consecutive_interrupts=interrupts, consecutive_failures=fails,
                              interrupted_cycles=int(self.state.get("interrupted_cycles", 0)) + 1)
            self.state["last_error"] = f"cycle interrupted ({interrupts} in a row): {self.redact(detail or '')[:200]}"
            if interrupts == 1:
                return t + datetime.timedelta(seconds=BETWEEN_CYCLES_S), "resuming"
            return self._failure_wait(t, fails, interrupts)
        if outcome == "usage_limit":
            if reset_at and t < reset_at < t + datetime.timedelta(days=8):
                wake = reset_at + datetime.timedelta(seconds=random.uniform(*JITTER))
                self.state["limit_backoff_idx"] = 0
            else:
                idx = int(self.state.get("limit_backoff_idx", 0))
                wake = t + datetime.timedelta(minutes=LIMIT_BACKOFF_MIN[min(idx, len(LIMIT_BACKOFF_MIN) - 1)])
                self.state["limit_backoff_idx"] = idx + 1
            self.state["limit_wait_s_total"] = self.state.get("limit_wait_s_total", 0) + (wake - t).total_seconds()
            self.state["last_error"] = f"usage limit; next wake {iso(wake)}"
            return wake, "waiting-limit"
        if outcome == "auth":
            if not self.state.get("auth_reported"):
                self.needs_human(
                    "Claude Code authentication failed",
                    "The autopilot's last cycle could not authenticate. Fix once, as the operator, in a terminal on this host:\n\n"
                    "    claude auth status\n"
                    "    claude auth login        # then: touch ~/.llm-autopilot/WAKE\n\n"
                    f"The runner retries every {AUTH_RETRY_S // 60} minutes. Detail: {self.redact(detail or '')[:300]}",
                )
                self.state["auth_reported"] = True
            self.state["last_error"] = "authentication failed"
            return t + datetime.timedelta(seconds=AUTH_RETRY_S), "waiting-auth"
        if outcome == "config":
            if not self.state.get("auth_reported"):
                self.needs_human("Claude Code configuration error", f"The model or request was rejected; check `claude --version` and the model alias in MASTER_PROMPT.md section 0. Detail: {self.redact(detail or '')[:300]}")
                self.state["auth_reported"] = True
            self.state["last_error"] = "configuration error"
            return t + datetime.timedelta(seconds=AUTH_RETRY_S), "waiting-config"
        if outcome == "transient":
            idx = int(self.state.get("net_backoff_idx", 0))
            delay = min(NET_BACKOFF_MAX_S, 60 * (2 ** idx)) * random.uniform(0.8, 1.2)
            self.state["net_backoff_idx"] = idx + 1
            self.state["last_error"] = f"transient error: {self.redact(detail or '')[:200]}"
            return t + datetime.timedelta(seconds=delay), "backoff"
        fails = int(self.state.get("consecutive_failures", 0)) + 1
        self.state["consecutive_failures"] = fails
        self.state["last_error"] = f"crash #{fails}: {self.redact(detail or '')[:200]}"
        return self._failure_wait(t, fails, 0)

    def _failure_wait(self, t, fails, interrupts):
        """Restart after CRASH_RESTART_S, or sleep CRASH_CAP_SLEEP_S and record
        it once `fails` consecutive failures reach CRASH_CAP (§5.5)."""
        if fails >= CRASH_CAP:
            self.state.update(consecutive_failures=0, consecutive_interrupts=0)
            self.event("failure-cap", failures=fails, interrupted_in_a_row=interrupts, sleep_s=CRASH_CAP_SLEEP_S)
            return t + datetime.timedelta(seconds=CRASH_CAP_SLEEP_S), "failure-cap"
        return t + datetime.timedelta(seconds=CRASH_RESTART_S), "restarting"

    # ---- main loop
    def handle_signal(self, signum, _frame):
        self.stop_signal = True
        if self.child and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except OSError:
                pass

    def run(self):
        if not self.acquire_lock():
            return EXIT_LOCKED
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        if self.flag("RENEW"):
            self.state["started_at"] = iso(now())
            self.state["final_checkpoint_done"] = False
            os.remove(os.path.join(AP_HOME, "RENEW"))
            self.event("renewed")
        self.event("runner-start", pid=os.getpid(), claude=CLAUDE, worktree=WORKTREE)
        try:
            return self.loop()
        finally:
            self.hb_stop.set()
            self.write_heartbeat()

    def _settings_warn(self, key, msg):
        seen = self.__dict__.setdefault("_settings_warned", set())
        if (key, msg) not in seen:
            seen.add((key, msg))
            self.event("operator-setting-ignored", key=key, reason=msg)
            self.state["last_error"] = f"operator setting {key} ignored: {msg}"

    def loop(self):
        while True:
            settings = operator_settings(warn=self._settings_warn)
            # STOP always wins; keep next_wake so a later start still honours a
            # pending usage-limit or crash-cap wait (R3).
            if self.stop_signal:
                self.set_state("stopped")
                return 0
            if self.flag("STOP"):
                self.set_state("stopped", last_error=None)
                self.event("stop-file")
                return EXIT_STOPPED
            if self.complete():
                self.set_state("complete", next_wake=None)
                self.event("complete")
                self.disable_service()
                return EXIT_STOPPED
            # PAUSE and the pause windows: sleep without starting cycles, and
            # keep any pending next_wake and its waiting state (R3).
            if self.flag("PAUSE") or in_pause_window(settings.get("AUTOPILOT_PAUSE_WINDOWS")):
                if self.state.get("state") != "paused":
                    self.set_state("paused", paused_from=self.state.get("state"))
                nap(60)
                continue
            if self.state.get("state") == "paused":  # just unpaused: restore the pre-pause state name
                self.set_state(self.state.get("paused_from") or "idle", paused_from=None)
            # A pending wait (usage-limit reset, crash cap, backoff): honour it.
            wake = parse_iso(self.state.get("next_wake"))
            if wake and wake > now():
                waiting = self.state.get("state") if self.state.get("state") in ("waiting-limit", "waiting-auth", "waiting-config", "backoff", "restarting", "resuming", "failure-cap", "idle", "waiting-disk") else "waiting"
                self.sleep_until(wake, waiting)
                continue
            if self.free_fraction() < MIN_FREE_FRACTION:
                self.needs_human("Disk below 15% free", f"The autopilot paused cycles: a volume holding {WORKTREE} or {AP_HOME} has less than 15% free. Free space; nothing else is needed.")
                self.state["last_error"] = "disk below 15% free"
                self.sleep_until(now() + datetime.timedelta(seconds=DISK_RETRY_S), "waiting-disk")
                continue
            # MAX_AUTONOMOUS_DAYS: only after the gates above, so the final
            # checkpoint waits for STOP, PAUSE, a pending reset and disk (R5).
            started = parse_iso(self.state.get("started_at")) or now()
            days = float(settings.get("MAX_AUTONOMOUS_DAYS") or 14)
            if now() - started >= datetime.timedelta(days=days):
                exit_code = self.run_final_checkpoint(settings, days)
                if exit_code is not None:
                    return exit_code
                continue
            if MAX_CYCLES and self.cycles_run >= MAX_CYCLES:
                self.set_state("test-finished", next_wake=None)
                return 0
            outcome, reset_at, detail = self.run_cycle(settings)
            self.cycles_run += 1
            if outcome not in ("auth", "config"):
                self.state["auth_reported"] = False
            wake, name = self.after(outcome, reset_at, detail)
            self.state["next_wake"] = iso(wake)
            self.event("next", state=name, wake=iso(wake), wait_s=round((wake - now()).total_seconds()))
            self.set_state(name)

    def run_final_checkpoint(self, settings, days):
        """Write the final checkpoint, retrying on recoverable outcomes. Returns an
        exit code to stop, or None to let the loop gate the next attempt."""
        if self.state.get("final_checkpoint_done"):
            self.set_state("expired", next_wake=None, last_error=f"MAX_AUTONOMOUS_DAYS={days:g} reached; touch {AP_HOME}/RENEW and restart to continue")
            return EXIT_STOPPED
        self.event("max-days-reached", days=days)
        outcome, reset_at, detail = self.final_checkpoint(settings)
        self.cycles_run += 1
        if outcome in ("ok", "max_turns", "timeout"):
            self.state["final_checkpoint_done"] = True
            self.set_state("expired", next_wake=None, last_error=f"MAX_AUTONOMOUS_DAYS={days:g} reached; final checkpoint written; touch {AP_HOME}/RENEW and restart to continue")
            return EXIT_STOPPED
        attempts = int(self.state.get("final_checkpoint_attempts", 0)) + 1
        self.state["final_checkpoint_attempts"] = attempts
        if attempts >= FINAL_CHECKPOINT_MAX_ATTEMPTS:
            self.needs_human("Final checkpoint could not be written",
                             f"After {attempts} attempts the final checkpoint cycle still ends in '{outcome}'. The runner is stopping at MAX_AUTONOMOUS_DAYS without a fresh checkpoint. Detail: {self.redact(detail or '')[:300]}")
            self.set_state("expired", next_wake=None, last_error=f"final checkpoint failed after {attempts} attempts ({outcome})")
            return EXIT_STOPPED
        # Reschedule through the normal policy (wait for a reset, back off, retry).
        wake, name = self.after(outcome, reset_at, detail)
        self.state["next_wake"] = iso(wake)
        self.event("next", state=name, wake=iso(wake), wait_s=round((wake - now()).total_seconds()), final_checkpoint_attempt=attempts)
        self.set_state(name)
        return None

    def final_checkpoint(self, settings):
        """One cycle that only writes and commits a final checkpoint. Returns the
        cycle's (outcome, reset_at, detail)."""
        # Keep this prompt and its directory out of the agent-writable zone's
        # reach of a pre-placed symlink: write it with O_NOFOLLOW|O_TRUNC.
        path = os.path.join(AP_HOME, "agent", "FINAL_CHECKPOINT_PROMPT.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = (
            "MAX_AUTONOMOUS_DAYS has been reached and the autopilot runner is stopping. Following "
            "docs/ai-platform-upgrade/MASTER_PROMPT.md (read it, then RESUME.md and TASK_BOARD.md), write a final "
            "checkpoint: update RESUME.md (phase, what is finished, what is in progress, the exact next step, open branches, "
            "running dev services and how to stop them, experiments in flight, last known-good production tag, blockers) and "
            "IMPLEMENTATION_STATUS.md truthfully, commit them, and push autopilot/dev. Do not start new work. Never cross a "
            "hard limit in section 3.3."
        )
        try:
            if os.path.islink(path):
                os.unlink(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
        except OSError as exc:
            self.event("final-checkpoint-prompt-error", error=str(exc))
            return "crash", None, f"could not write the final checkpoint prompt ({exc})"
        saved, self.prompt_file = self.prompt_file, path
        try:
            return self.run_cycle(settings)
        finally:
            self.prompt_file = saved


def main():
    os.makedirs(AP_HOME, exist_ok=True)
    return Runner().run()


if __name__ == "__main__":
    sys.exit(main())
