"""The `e2e-hosted` job's own rules, which workflow_policy.py cannot give it.

WHY A SPECIFIC TEST RATHER THAN A NEW P-RULE
workflow_policy.py's P6 injection check walks `run:` BODIES only — it iterates
each job's steps and looks at `step["run"]`, so an `env:` block is invisible to
it. That is fine as a general rule (a value in `env:` is data to the shell),
and it is NOT fine for this one job, because this job's `env:` decides WHICH
DEPLOYMENT a suite that writes is pointed at:

  * e2e/platform/lib/config.js:160 resolves the base from `--base`, then
    `E2E_BASE_URL`, then loopback;
  * :170 reads `allowRemote` from `E2E_ALLOW_REMOTE === '1'`, which is the one
    switch that lets the suite write to a NON-loopback deployment.

And the suite writes: per its README a run creates conversations, uploads
including a 92 MiB one, a public share link, an API project that cannot be
deleted, an API key, and one failed sign-in that counts toward a per-address
lockout which then refuses EVERY sign-in through that frontend, other people's
included. `E2E_BASE_URL: ${{ inputs.target }}` would be a one-line change that
turns a CI stage into a writer against whatever a dispatch names.

So: a cheap, specific rule over this job, with a failing fixture proving each
assertion can fail. Not a broad new P-rule, which would have to be audited
against every existing job in the file first.

The second half of this file pins the two live fixes in scripts/e2e-stack.sh
(the environment ALLOWLIST and the seed password leaving argv), because both
are the kind of change a later edit reverts by accident.
"""
from __future__ import annotations

import ipaddress
import json
import pathlib
import re
import unittest
from urllib.parse import urlparse

import yaml

REPO = pathlib.Path(__file__).resolve().parents[4]
PIPELINE = REPO / ".github" / "workflows" / "pipeline.yml"
E2E_STACK = REPO / "scripts" / "e2e-stack.sh"
CI_STACK = REPO / "e2e" / "ci" / "stack.sh"
CI_ENV = REPO / "e2e" / "ci" / "ci.env"

JOB_ID = "e2e-hosted"

#: Expressions that must never reach this job's `env:`. `github.event` and
#: `github.head_ref` are attacker-controllable; `inputs.` and `vars.` are
#: operator-controllable from outside the reviewed file, which is the same
#: problem for an address a writing suite is aimed at.
FORBIDDEN_IN_ENV = ("${{ inputs.", "${{ github.event", "${{ vars.", "${{ github.head_ref")

#: lib/config.js:123 refuses these on every host: they are the production
#: frontend and orchestrator ports, published on loopback too.
PRODUCTION_PORTS = {3000, 8080}


#: A `docker exec` whose program (or whose program's input) comes from stdin
#: needs `-i`. Without it the container gets an EMPTY stdin: `python3 -` reads
#: an empty program and exits 0, so every assertion in the heredoc is skipped
#: and the step is green. Measured today, 2026-09-27, against a live container
#: with a heredoc whose only statements are `echo` and `exit 7`:
#:
#:     docker exec    c sh - <<EOS ...   -> printed nothing, rc 0
#:     docker exec -i c sh - <<EOS ...   -> printed the line,  rc 7
#:
#: The same no-op was written twice in this branch's own history: it was caught
#: in e2e/ci/stack.sh's assert_orch_health and left in pipeline.yml's "The
#: container reports the capacity values it was given" step, where it made two
#: asserts unreachable behind an empty green step. Hence a rule rather than a
#: fix.
_EXEC_BARE_STDIN_ARG = re.compile(r"(?:^|\s)-(?=\s|$)")


def _exec_flags(argv: str) -> list[str]:
    """The option tokens between `docker exec` and the container name."""
    flags: list[str] = []
    for token in argv.split():
        if token.startswith("-"):
            flags.append(token)
        else:
            break
    return flags


def _carries_interactive(argv: str) -> bool:
    for flag in _exec_flags(argv):
        if flag == "--interactive":
            return True
        if not flag.startswith("--") and "i" in flag[1:]:
            return True
    return False


def docker_exec_stdin_findings(text: str, label: str) -> list[str]:
    """Findings for every `docker exec` in `text` that needs `-i` and lacks it.

    Three shapes need it, and each of the three is a silent no-op without it
    rather than an error:
      * a heredoc (`<<`) on the same line — the program itself is on stdin;
      * a bare `-` program argument (`python3 -`, `sh -`) for the same reason;
      * anything PIPED into `docker exec`, where the input is discarded.
    """
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        # A COMMENT that quotes the broken form is the record of the fix, not
        # the fix being undone.
        if stripped.startswith("#") or "docker exec" not in stripped:
            continue
        before, _, argv = stripped.partition("docker exec")
        needs = []
        if "<<" in stripped:
            needs.append("a heredoc feeds it")
        if _EXEC_BARE_STDIN_ARG.search(argv):
            needs.append("its program argument is a bare `-`")
        if before.rstrip().endswith("|"):
            needs.append("it is on the right of a pipe")
        if not needs or _carries_interactive(argv):
            continue
        findings.append(
            f"{label}:{lineno}: `docker exec` without -i, and {needs[0]}, "
            "so the container gets an empty stdin and the program is a no-op that exits 0"
        )
    return findings


def load_pipeline() -> dict:
    return yaml.safe_load(PIPELINE.read_text(encoding="utf-8"))


def is_literal_loopback(value: str) -> bool:
    """A literal http(s) URL on 127.0.0.0/8, ::1 or `localhost`.

    Deliberately strict about it being a LITERAL: a hostname that merely
    starts with 127 (`127.attacker.example`, `127.0.0.1.nip.io`) is a DNS
    name that can point anywhere, which is the same mistake lib/config.js
    fixed on 2026-09-13 by switching to net.isIP.
    """
    if "${{" in value:
        return False
    try:
        url = urlparse(value)
    except ValueError:
        return False
    if url.scheme not in ("http", "https"):
        return False
    host = (url.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback


def check_job_env(job: dict) -> list[str]:
    """Findings for one job's `env:` block. Empty list means it is compliant."""
    findings: list[str] = []
    env = job.get("env") or {}
    if not isinstance(env, dict):
        return [f"`env:` is {type(env).__name__}, not a mapping"]
    for key, raw in env.items():
        value = "" if raw is None else str(raw)
        for bad in FORBIDDEN_IN_ENV:
            if bad in value:
                findings.append(
                    f"{key} interpolates {bad}…, which is set outside this file; "
                    "this job's environment decides what a WRITING suite is aimed at"
                )
    base = env.get("E2E_BASE_URL")
    if base is None:
        findings.append("E2E_BASE_URL is not pinned in `env:`, so the suite falls back to its own default")
    else:
        base = str(base)
        if not is_literal_loopback(base):
            findings.append(f"E2E_BASE_URL={base!r} is not a literal loopback URL")
        else:
            port = urlparse(base).port or (443 if base.startswith("https") else 80)
            if port in PRODUCTION_PORTS:
                findings.append(f"E2E_BASE_URL uses port {port}, which is a production port")
    allow_remote = env.get("E2E_ALLOW_REMOTE")
    if allow_remote is None:
        findings.append("E2E_ALLOW_REMOTE is not pinned off in `env:`")
    elif str(allow_remote) == "1":
        findings.append("E2E_ALLOW_REMOTE=1 lets the suite write to a non-loopback deployment")
    chat_mode = str(env.get("E2E_CHAT_MODE", ""))
    if chat_mode == "live":
        findings.append("E2E_CHAT_MODE=live dials real engines, which no hosted runner may reach")
    return findings


def job_steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


class TheJobExistsAndRunsOnAHostedRunner(unittest.TestCase):
    def setUp(self):
        self.doc = load_pipeline()
        self.job = self.doc["jobs"][JOB_ID]

    def test_the_job_is_in_the_pipeline(self):
        self.assertIn(JOB_ID, self.doc["jobs"])

    def test_it_runs_on_a_hosted_runner_and_never_the_production_box(self):
        # A stage that runs the whole product must never do it on the box that
        # IS production: it would compete for the same memory, the same ports
        # and the same database server.
        self.assertEqual(self.job["runs-on"], "ubuntu-latest")

    def test_it_declares_a_read_only_token_and_a_ceiling(self):
        self.assertEqual(self.job["permissions"], {"contents": "read"})
        timeout = self.job["timeout-minutes"]
        self.assertIsInstance(timeout, int)
        self.assertNotIsInstance(timeout, bool)
        self.assertGreater(timeout, 0)

    def test_the_display_name_is_plain_ascii(self):
        self.assertTrue(str(self.job["name"]).isascii())


class TheEnvironmentIsPinnedInTheFile(unittest.TestCase):
    def setUp(self):
        self.job = load_pipeline()["jobs"][JOB_ID]

    def test_the_real_job_passes_every_rule(self):
        self.assertEqual(check_job_env(self.job), [])

    def test_the_base_url_is_a_literal_loopback_url(self):
        base = str(self.job["env"]["E2E_BASE_URL"])
        self.assertTrue(is_literal_loopback(base), base)
        self.assertNotIn(urlparse(base).port, PRODUCTION_PORTS)

    def test_no_env_value_comes_from_inputs_or_vars(self):
        for key, value in (self.job.get("env") or {}).items():
            for bad in FORBIDDEN_IN_ENV:
                self.assertNotIn(bad, str(value), f"{key} reads {bad}")

    def test_chat_mode_is_stub_and_the_runner_may_not_dial_an_engine(self):
        self.assertEqual(str(self.job["env"]["E2E_CHAT_MODE"]), "stub")
        for step in job_steps(self.job):
            self.assertNotIn("--chat-mode live", str(step.get("run", "")))


class TheFailingFixtures(unittest.TestCase):
    """Each assertion above is proven ABLE TO FAIL, on a job shaped like ours.

    A guard nobody has watched refuse something is a guard nobody knows works;
    these are the four refusals this rule exists for.
    """

    def test_a_base_url_from_a_dispatch_input_is_refused(self):
        findings = check_job_env({"env": {"E2E_BASE_URL": "${{ inputs.target }}", "E2E_ALLOW_REMOTE": "0"}})
        self.assertTrue(any("inputs." in f for f in findings), findings)

    def test_a_base_url_from_a_repository_variable_is_refused(self):
        findings = check_job_env({"env": {"E2E_BASE_URL": "${{ vars.E2E_TARGET }}", "E2E_ALLOW_REMOTE": "0"}})
        self.assertTrue(any("vars." in f for f in findings), findings)

    def test_a_remote_base_url_is_refused_even_when_it_is_a_literal(self):
        findings = check_job_env({"env": {"E2E_BASE_URL": "https://ai.example.com", "E2E_ALLOW_REMOTE": "0"}})
        self.assertTrue(any("not a literal loopback" in f for f in findings), findings)

    def test_a_hostname_that_merely_starts_with_127_is_not_loopback(self):
        self.assertFalse(is_literal_loopback("http://127.0.0.1.nip.io:3901"))
        self.assertFalse(is_literal_loopback("http://127.attacker.example:3901"))
        self.assertTrue(is_literal_loopback("http://127.0.0.1:3901"))
        self.assertTrue(is_literal_loopback("http://[::1]:3901"))

    def test_a_production_port_on_loopback_is_refused(self):
        findings = check_job_env({"env": {"E2E_BASE_URL": "http://127.0.0.1:3000", "E2E_ALLOW_REMOTE": "0"}})
        self.assertTrue(any("production port" in f for f in findings), findings)

    def test_allow_remote_switched_on_is_refused(self):
        findings = check_job_env({"env": {"E2E_BASE_URL": "http://127.0.0.1:3901", "E2E_ALLOW_REMOTE": "1"}})
        self.assertTrue(any("non-loopback" in f for f in findings), findings)

    def test_a_missing_base_url_is_refused_because_the_default_would_decide(self):
        findings = check_job_env({"env": {"E2E_ALLOW_REMOTE": "0"}})
        self.assertTrue(any("not pinned" in f for f in findings), findings)

    def test_live_chat_mode_is_refused(self):
        findings = check_job_env(
            {"env": {"E2E_BASE_URL": "http://127.0.0.1:3901", "E2E_ALLOW_REMOTE": "0", "E2E_CHAT_MODE": "live"}}
        )
        self.assertTrue(any("live" in f for f in findings), findings)


class NothingLeavesTheRunnerUnredacted(unittest.TestCase):
    """The B5 fix, pinned.

    `::add-mask::` hides a value in the LOG only; it does not touch the bytes
    actions/upload-artifact uploads, and an artifact on a public repository is
    readable by anyone with the run URL. So: the redactor runs, it runs BEFORE
    the upload, and the upload happens on failure only and expires.
    """

    def setUp(self):
        self.steps = job_steps(load_pipeline()["jobs"][JOB_ID])

    def _index_of(self, predicate) -> int:
        for i, step in enumerate(self.steps):
            if predicate(step):
                return i
        return -1

    def test_the_redactor_runs_and_is_given_every_generated_secret(self):
        idx = self._index_of(lambda s: "node e2e/ci/redact.js" in str(s.get("run", "")))
        self.assertGreater(idx, -1, "no step runs e2e/ci/redact.js")
        body = str(self.steps[idx]["run"])
        self.assertIn("--secrets-file", body)
        # The two account passwords, AND the two secrets stack.sh generates into
        # the orchestrator's environment (2026-09-27). A traceback that prints
        # settings, or a log line that echoes the environment, writes the
        # session signing key or the API key pepper into the collected log, and
        # a pattern cannot catch a random hex string -- only the value can.
        for name in ("e2e-admin.pw", "e2e-member.pw", "e2e-ci-session.key", "e2e-ci-pepper.key"):
            self.assertIn(name, body, f"{name} is not passed to the redactor")

    def test_the_redactor_runs_before_the_upload(self):
        redact = self._index_of(lambda s: "node e2e/ci/redact.js" in str(s.get("run", "")))
        upload = self._index_of(lambda s: "actions/upload-artifact@" in str(s.get("uses", "")))
        self.assertGreater(upload, -1, "no upload step")
        self.assertLess(redact, upload, "the redactor must run before anything is uploaded")

    def test_the_upload_is_failure_only_and_expires(self):
        upload = self.steps[self._index_of(lambda s: "actions/upload-artifact@" in str(s.get("uses", "")))]
        self.assertEqual(str(upload.get("if", "")).strip(), "failure()")
        retention = upload["with"]["retention-days"]
        self.assertIsInstance(retention, int)
        self.assertLessEqual(retention, 7)

    def test_the_report_itself_is_never_written_into_the_run_summary(self):
        # Counts and check ids are fine; the report keeps failures verbatim and
        # a run summary on a public repository is readable by anyone with the
        # run URL.
        for step in self.steps:
            body = str(step.get("run", ""))
            if "GITHUB_STEP_SUMMARY" not in body:
                continue
            self.assertNotIn("results.md", body)
            self.assertNotIn("cat ", body)


class TheJobAndTheStackAgreeOnNames(unittest.TestCase):
    """The workflow names containers by hand (`docker exec …`, `stack.sh logs
    …`). If ci.env renames one, those steps fail at run time with "no such
    container" after the expensive half of the job has already run. This finds
    it in the policy job instead."""

    def test_every_container_the_job_names_is_declared_in_ci_env(self):
        values = dict(
            line.split("=", 1)
            for line in CI_ENV.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        declared = {values[k] for k in ("STACK_ENGINE_NAME", "STACK_ORCH_NAME", "STACK_FRONT_NAME")}
        bodies = " ".join(str(s.get("run", "")) for s in job_steps(load_pipeline()["jobs"][JOB_ID]))
        named = set(re.findall(r"\btechsara-e2e-ci-[a-z-]+\b", bodies))
        self.assertTrue(named, "the job names no container at all; this test needs rewriting")
        self.assertLessEqual(named, declared, f"named but not in ci.env: {sorted(named - declared)}")

    def test_the_suite_is_pointed_at_the_frontend_port_ci_env_publishes(self):
        values = dict(
            line.split("=", 1)
            for line in CI_ENV.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        base = str(load_pipeline()["jobs"][JOB_ID]["env"]["E2E_BASE_URL"])
        self.assertEqual(urlparse(base).port, int(values["STACK_FRONT_PORT"]))


class TheGateListsAgree(unittest.TestCase):
    """`ci-ok`'s `needs:` and ci_gate.py's `--require` must name the same jobs.

    This is the invariant, not "e2e-hosted is absent": the day it is promoted
    into the gate, both lists change together or this fails. ci_gate.py itself
    refuses a dependency that nothing required and a requirement that is not a
    dependency, but only at RUN time, on the box, once the run has already
    cost 30 minutes.
    """

    def test_needs_and_require_are_the_same_set(self):
        doc = load_pipeline()
        needs = set(doc["jobs"]["ci-ok"]["needs"])
        bodies = [str(s.get("run", "")) for s in job_steps(doc["jobs"]["ci-ok"])]
        required: set[str] = set()
        for body in bodies:
            match = re.search(r"--require\s+([A-Za-z0-9,_-]+)", body)
            if match:
                required |= {part for part in match.group(1).split(",") if part}
        self.assertTrue(required, "could not find the --require list in ci-ok")
        self.assertEqual(needs, required)

    def test_promoting_this_job_means_editing_both_lists(self):
        doc = load_pipeline()
        needs = set(doc["jobs"]["ci-ok"]["needs"])
        body = " ".join(str(s.get("run", "")) for s in job_steps(doc["jobs"]["ci-ok"]))
        self.assertEqual(JOB_ID in needs, f"{JOB_ID}" in body)


class TheLiveFixesInTheBoxStackScript(unittest.TestCase):
    """scripts/e2e-stack.sh, read as text. Both fixes are real today on a box
    several agent sessions share."""

    def setUp(self):
        self.text = E2E_STACK.read_text(encoding="utf-8")

    def test_the_production_environment_is_copied_through_an_ALLOWLIST(self):
        # The denylist it replaces named nine path-shaped variables and copied
        # EVERYTHING else — session signing key, model and third-party API
        # keys, the tunnel token — into a file handed to a container seeded
        # with test accounts on loopback. A denylist fails OPEN the day
        # production gains a new secret; an allowlist fails closed.
        self.assertFalse(
            "grep -vE '^(APP_DATABASE_URL" in self.text,
            "scripts/e2e-stack.sh still copies the production environment through a DENYLIST",
        )
        self.assertTrue(
            "ENV_ALLOWLIST" in self.text,
            "scripts/e2e-stack.sh no longer names an environment ALLOWLIST",
        )
        inspect_lines = [ln for ln in self.text.splitlines() if "docker inspect" in ln and "Config.Env" in ln]
        self.assertTrue(inspect_lines, "the environment copy is gone entirely; this test needs rewriting")
        window = self.text.split("Config.Env", 1)[1][:400]
        self.assertIn("grep -E", window, "the environment is no longer filtered through an allowlist")

    def test_the_allowlist_cannot_be_widened_from_the_environment(self):
        # It used to be `E2E_ENV_ALLOWLIST="${E2E_ENV_ALLOWLIST:-...}"`, so
        # `E2E_ENV_ALLOWLIST='.*'` outside the reviewed file restored the
        # wholesale copy of production's environment -- session signing key, API
        # key pepper, tunnel token, every model and third-party key -- into a
        # container on a box several sessions share. An allowlist a caller can
        # widen to `.*` is not an allowlist. E2E_EXTRA_ENV_FILE is the reviewed
        # way to add one variable and is unaffected.
        for line in self.text.splitlines():
            if line.lstrip().startswith("#") or "ENV_ALLOWLIST=" not in line:
                continue
            self.assertNotRegex(
                line,
                r"ENV_ALLOWLIST=\"\$\{",
                "the environment allowlist is overridable from outside this file again",
            )
        self.assertIn("E2E_EXTRA_ENV_FILE", self.text, "the reviewed way to add one variable is gone")

    def test_no_docker_exec_line_carries_a_password_argument(self):
        for lineno, line in enumerate(self.text.splitlines(), 1):
            # A COMMENT that quotes the old form is the record of the fix, not
            # the fix being undone. Only real command lines are checked.
            if line.lstrip().startswith("#") or "docker exec" not in line:
                continue
            # Only what docker exec is GIVEN: `printf ... "$pw" | docker exec`
            # is the fix, and the password is on the left of the pipe.
            argv = line.split("docker exec", 1)[1]
            self.assertNotIn('"$pw"', argv, f"scripts/e2e-stack.sh:{lineno} passes the password in argv")
            self.assertNotIn("$PASSWORD", argv, f"scripts/e2e-stack.sh:{lineno} passes a password in argv")
            # `-e NAME=value` would only move it from `ps` into `docker inspect`.
            self.assertNotRegex(argv, r"-e\s+[A-Z_]*PASSWORD", f"scripts/e2e-stack.sh:{lineno}")

    def test_the_seed_reads_the_password_from_stdin(self):
        self.assertTrue(
            "sys.stdin" in self.text,
            "the seed no longer reads the password from stdin, so it is back in argv or the environment",
        )


class TheCheckedInDefaultsAreNonSecret(unittest.TestCase):
    """e2e/ci/ci.env is the job's environment. It must stay boring."""

    def setUp(self):
        self.lines = [
            ln for ln in CI_ENV.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        self.values = dict(ln.split("=", 1) for ln in self.lines)

    def test_every_line_is_a_plain_key_value(self):
        for line in self.lines:
            self.assertRegex(line, r"^[A-Z][A-Z0-9_]*=", line)
            for bad in ("$(", "`", "${{"):
                self.assertNotIn(bad, line, f"shell expansion in ci.env: {line}")

    def test_the_capacity_watermarks_are_set_explicitly(self):
        # The defaults are 250 GiB and 20 GiB; a hosted runner has far less,
        # and every upload check would be refused by a watermark that is about
        # the DGX's filesystem.
        self.assertIn("PUBLIC_API_FILES_MIN_FREE_GIB", self.values)
        self.assertIn("PUBLIC_API_MIN_FREE_DISK_BYTES", self.values)
        self.assertGreater(int(self.values["PUBLIC_API_MIN_FREE_DISK_BYTES"]), 0)

    def test_every_engine_url_points_at_the_in_repo_stub(self):
        # `_BASE_URLS` (plural) as well: ASR takes a comma-separated list, and a
        # rule that only read the singular would have let a real speech endpoint
        # into CI unnoticed.
        for key, value in self.values.items():
            if key.endswith("_BASE_URL") or key.endswith("_BASE_URLS"):
                for one in value.split(","):
                    if one.strip():
                        self.assertIn("e2e-ci-engine", one, f"{key} does not point at the stub engine")

    def test_the_engine_controller_is_pinned_at_the_stub_too(self):
        # UNSET, app/config.py:2182 defaults it to `http://vllm:9838/state` -- a
        # PRODUCTION container name. A hosted runner then looks that name up on
        # every poll for the length of the job (measured 2026-09-27: "controller
        # unreachable: ConnectError: [Errno -3] Temporary failure in name
        # resolution"), and a search domain or a wildcard zone on the runner
        # would turn the lookup into a request to a host nobody chose.
        url = self.values.get("ENGINE_CONTROLLER_URL")
        self.assertIsNotNone(url, "ENGINE_CONTROLLER_URL is not pinned; the default names a production container")
        self.assertIn("e2e-ci-engine", url, url)
        self.assertNotIn("vllm", url, url)


    def test_no_value_looks_like_a_credential(self):
        # `OPENAI_API_KEY=local` is the placeholder a local inference server
        # wants (app/config.py:167); anything longer and random-looking in this
        # file would be a real secret checked into a public repository.
        for key, value in self.values.items():
            if not re.search(r"(KEY|SECRET|TOKEN|PASSWORD|PEPPER)", key):
                continue
            self.assertLess(
                len(value), 16, f"{key} holds a {len(value)}-character value; ci.env must hold no credential"
            )


class EverySixthPublicModelIsReallyConfigured(unittest.TestCase):
    """The defect the first local run of this stage found, pinned.

    app/publicapi/registry.py WITHDRAWS a public model whose engine shares an
    address with the main engine -- `_router_configured` (registry.py:716, the
    test at :723), `_ocr_configured` (:762, :769), `_embed_configured` (:802,
    :807) and `_rerank_configured` (:838, :846) all refuse on
    `not _same_address(url, openai_base_url)`. That guard is correct: a profile
    pointing a sidecar at the main engine would publish the main model a second
    time, around its breaker and its admission lanes.

    With every `*_BASE_URL` in ci.env on ONE address it did exactly that, and
    `/v1/models` listed `techsara-35b` alone, so the suite's
    `v1.models-published` check failed:

        missing from /v1/models: techsara-8b-vision, techsara-ocr,
        techsara-embed, techsara-rerank, techsara-whisper

    The fix is one DNS name per engine key (network aliases on the one stub
    container). These assertions are what stops a later edit collapsing them
    back onto one address and re-breaking the stage 40 minutes into a run.
    """

    #: engine key -> the ci.env variable holding its address. AGENT and VISION
    #: are deliberately absent: in production the agent runs on the router's
    #: engine and vision on the main one, and ci.env mirrors that.
    PUBLIC_ENGINE_URL_KEYS = ("ROUTER_BASE_URL", "OCR_BASE_URL", "EMBED_BASE_URL", "RERANK_BASE_URL")

    def setUp(self):
        self.values = dict(
            line.split("=", 1)
            for line in CI_ENV.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    @staticmethod
    def _address(url: str) -> str:
        parts = urlparse(url.strip())
        return f"{parts.scheme}://{parts.hostname}:{parts.port or ''}"

    def test_no_public_engine_shares_the_main_engine_address(self):
        main = self._address(self.values["OPENAI_BASE_URL"])
        for key in self.PUBLIC_ENGINE_URL_KEYS:
            self.assertNotEqual(
                self._address(self.values[key]),
                main,
                f"{key} is on the main engine's address, so registry.py withdraws its public model",
            )

    def test_each_public_engine_has_its_own_address(self):
        seen: dict[str, str] = {}
        for key in self.PUBLIC_ENGINE_URL_KEYS:
            address = self._address(self.values[key])
            self.assertNotIn(
                address,
                seen,
                f"{key} shares {address} with {seen.get(address)}; one of the two is withdrawn from /v1/models",
            )
            seen[address] = key

    def test_speech_is_configured_so_techsara_whisper_is_published(self):
        # _asr_configured (registry.py:879) wants all three, and does not probe
        # the endpoint. ASR_ENABLED=false withdrew `techsara-whisper`.
        self.assertEqual(self.values.get("ASR_ENABLED"), "true")
        self.assertTrue(self.values.get("ASR_BASE_URLS", "").strip())
        self.assertTrue(self.values.get("ASR_MODEL", "").strip())
        self.assertNotEqual(
            self.values["ASR_MODEL"],
            self.values["LLM_MODEL"],
            "a sidecar naming the MAIN checkpoint withdraws itself (_configured_name)",
        )

    def test_the_stub_publishes_every_model_name_ci_env_names(self):
        published = {m.strip() for m in self.values["STUB_ENGINE_MODELS"].split(",") if m.strip()}
        for key in ("MAIN_MODEL", "LLM_MODEL", "ROUTER_MODEL", "AGENT_MODEL", "VISION_MODEL",
                    "EMBED_MODEL", "OCR_MODEL", "RERANK_MODEL", "ASR_MODEL"):
            self.assertIn(self.values[key], published, f"{key}={self.values[key]} is not in STUB_ENGINE_MODELS")

    def test_every_engine_alias_carries_e2e_ci_and_is_used(self):
        aliases = [a.strip() for a in self.values["STACK_ENGINE_ALIASES"].split(",") if a.strip()]
        self.assertTrue(aliases, "STACK_ENGINE_ALIASES is empty, so every sidecar is back on one address")
        urls = " ".join(
            value for key, value in self.values.items()
            if key.endswith("_BASE_URL") or key.endswith("_BASE_URLS") or key == "ENGINE_CONTROLLER_URL"
        )
        for alias in aliases:
            self.assertIn("e2e-ci", alias, f"alias {alias!r} does not carry 'e2e-ci'")
            self.assertIn(alias, urls, f"alias {alias!r} is declared but no address uses it")


class TheJobAndCiEnvDoNotDriftApart(unittest.TestCase):
    """The account emails live in TWO places and both are load-bearing.

    `stack.sh` SEEDS the accounts and reads them from ci.env (load_ci_env
    exports every value, overriding the process environment -- deliberately, so
    an engine URL cannot be repointed from outside the reviewed file). The
    SUITE runs on the runner and reads them from the job's `env:`. Change one
    and not the other and the stack seeds one pair of accounts while the suite
    signs in as another, which fails as "incorrect email or password" 25 checks
    deep with nothing pointing at the cause.
    """

    def test_the_account_emails_agree(self):
        env = load_pipeline()["jobs"][JOB_ID]["env"]
        values = dict(
            line.split("=", 1)
            for line in CI_ENV.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        for key in ("E2E_ADMIN_EMAIL", "E2E_MEMBER_EMAIL"):
            self.assertEqual(
                str(env[key]),
                values[key],
                f"{key} differs: the job seeds one account and the suite signs in as another",
            )


class TheStepsThatRunAProgramInsideTheContainer(unittest.TestCase):
    """`docker exec` must carry `-i` wherever the program comes from stdin.

    This is not style. The capacity step in this job printed NOTHING and exited
    0 while claiming to prove that two watermarks reached the container: with no
    `-i`, `python3 -` read an empty program. A reader saw an empty green step.
    Measured today on a live container (see `_EXEC_BARE_STDIN_ARG` above):
    without `-i`, a heredoc whose only statements are `echo` and `exit 7`
    printed nothing and returned 0; with `-i` it printed and returned 7.
    """

    def test_no_run_body_in_this_job_execs_without_stdin(self):
        job = load_pipeline()["jobs"][JOB_ID]
        findings: list[str] = []
        for i, step in enumerate(job_steps(job)):
            body = str(step.get("run", ""))
            if "docker exec" not in body:
                continue
            findings += docker_exec_stdin_findings(body, f"pipeline.yml {JOB_ID} step {i} ({step.get('name')})")
        self.assertEqual(findings, [], findings)

    def test_no_run_body_anywhere_in_the_pipeline_execs_without_stdin(self):
        # The rule is cheap enough to hold over the whole file, so the next
        # job that shells into a container inherits it.
        doc = load_pipeline()
        findings: list[str] = []
        for job_id, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for i, step in enumerate(job_steps(job)):
                findings += docker_exec_stdin_findings(
                    str(step.get("run", "")), f"pipeline.yml {job_id} step {i}"
                )
        self.assertEqual(findings, [], findings)

    def test_both_stack_scripts_exec_with_stdin(self):
        for path in (REPO / "e2e" / "ci" / "stack.sh", E2E_STACK):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(docker_exec_stdin_findings(text, path.name), [], path.name)

    def test_the_capacity_step_is_the_one_that_regressed_and_is_pinned(self):
        # Named, because this exact step is where the no-op shipped.
        steps = job_steps(load_pipeline()["jobs"][JOB_ID])
        bodies = [str(s.get("run", "")) for s in steps if "min_free_bytes" in str(s.get("run", ""))]
        self.assertTrue(bodies, "no step reads the capacity watermarks out of the container any more")
        for body in bodies:
            self.assertIn("docker exec -i", body)
            # And the asserts it exists for are still there.
            self.assertIn("assert files ==", body)
            self.assertIn("assert disk ==", body)


class TheDockerExecRuleCanFail(unittest.TestCase):
    """The failing fixtures for the rule above, one per shape it catches."""

    def test_a_heredoc_without_i_is_refused(self):
        findings = docker_exec_stdin_findings("docker exec c python3 - <<'PY'", "fixture")
        self.assertTrue(any("heredoc" in f for f in findings), findings)

    def test_a_bare_dash_program_without_i_is_refused(self):
        findings = docker_exec_stdin_findings('docker exec "$ORCH" python3 -', "fixture")
        self.assertTrue(any("bare" in f for f in findings), findings)

    def test_a_pipe_into_exec_without_i_is_refused(self):
        findings = docker_exec_stdin_findings('printf x | docker exec "$ORCH" psql -f -', "fixture")
        self.assertTrue(findings, findings)

    def test_the_fixed_forms_pass(self):
        for good in (
            "docker exec -i c python3 - <<'PY'",
            'docker exec --interactive "$ORCH" python3 - <<\'PY\'',
            'printf x | docker exec -i "$ORCH" python3 -c "$prog"',
            # No stdin wanted: these are correct WITHOUT -i and must not be flagged.
            'docker exec "$ENGINE" node -e "fetch(1)"',
            "docker exec \"$ORCH\" python3 -c 'from app import db; print(db.LATEST_SCHEMA_VERSION)'",
            '  # It used to: docker exec -i "$ORCH" python3 - "$name" "$pw"',
        ):
            self.assertEqual(docker_exec_stdin_findings(good, "fixture"), [], good)

    def test_a_combined_flag_counts_as_interactive(self):
        self.assertEqual(docker_exec_stdin_findings("docker exec -it c sh - ", "fixture"), [])



class TheThrowawayStackScriptCanBeTornDownAndCannotEatAPeersStack(unittest.TestCase):
    """e2e/ci/stack.sh, read as text. Four defects that shipped once, pinned.

    All four were measured on this branch on 2026-09-27 before the fix:
      * `( unset E2E_CI_ORCH_IMAGE E2E_CI_FRONT_IMAGE; bash stack.sh down )`
        exited 1 with "set E2E_CI_ORCH_IMAGE to the orchestrator-cpu image this
        job built" and removed nothing, because the two image tags were
        top-level `${VAR:?}`. `down` and `logs` use no image tag, the job's
        teardown step is `if: always()`, and its FIRST step is
        `node --test e2e/ci/tests/` -- so any failure before the build step left
        the stack up behind a message about a variable teardown does not need;
      * `wait_for "orchestrator" 300 orch_healthy` was `for i in $(seq 1 300)`
        around a probe that is itself `curl -m 15`, so a hung-but-listening
        orchestrator cost 300 x 16 s = 80 minutes against a 60-minute job
        ceiling: the ceiling killed the job instead of the budget printing
        `docker logs`;
      * up() ran `docker rm -f` and down() ran `docker volume rm` /
        `docker network rm` on FIXED names whether or not that invocation
        created them. A peer session had this exact stack up during
        verification;
      * the header said flatly that a real engine could not be reached, which
        was argued from ci.env rather than measured. On this box the route
        exists (0.0.0.0:8000 is the live engine, and the orchestrator container
        gets `--add-host host.docker.internal:host-gateway` for the database).
    """

    def setUp(self):
        self.text = CI_STACK.read_text(encoding="utf-8")
        self.job = load_pipeline()["jobs"][JOB_ID]

    def _body(self, func: str) -> str:
        start = self.text.index(f"\n{func}() {{")
        end = self.text.index("\n}\n", start)
        return self.text[start:end]

    def test_the_image_tags_are_required_by_up_and_not_at_top_level(self):
        for name in ("E2E_CI_ORCH_IMAGE", "E2E_CI_FRONT_IMAGE"):
            for lineno, line in enumerate(self.text.splitlines(), 1):
                if line.lstrip().startswith("#") or name not in line:
                    continue
                self.assertNotIn(
                    f"${{{name}:?",
                    line,
                    f"stack.sh:{lineno} makes {name} mandatory at top level, so `down` and `logs` exit 1",
                )
            self.assertIn(name, self._body("up"), f"up() no longer requires {name} at all")

    def test_the_teardown_step_still_runs_on_failure_which_is_why_that_matters(self):
        teardown = [s for s in job_steps(self.job) if "stack.sh down" in str(s.get("run", ""))]
        self.assertTrue(teardown, "nothing tears the stack down any more")
        for step in teardown:
            self.assertEqual(str(step.get("if", "")).strip(), "always()")

    def test_every_wait_budget_fits_under_the_jobs_ceiling(self):
        # The arithmetic the old loop got wrong: worst case is the budget plus
        # ONE probe, and only if the budget is a wall-clock deadline. `seq` makes
        # it budget x probe.
        self.assertNotIn(
            'for i in $(seq 1 "$budget")',
            self.text,
            "wait_for counts iterations again, so a blocking probe multiplies the budget by its own timeout",
        )
        self.assertIn("deadline=$(( start + budget ))", self.text, "wait_for no longer uses a wall-clock deadline")
        budgets = [int(m) for m in re.findall(r'wait_for "[^"]+" (\d+) ', self.text)]
        self.assertTrue(budgets, "no wait_for call found; this test needs rewriting")
        probes = [int(m) for m in re.findall(r"curl -fsS -m (\d+)", self.text)]
        self.assertTrue(probes, "no curl probe found; this test needs rewriting")
        worst = max(budgets) + max(probes)
        ceiling = int(self.job["timeout-minutes"]) * 60
        self.assertLess(worst, ceiling, f"worst-case wait {worst}s is not under the job's {ceiling}s ceiling")

    def test_up_refuses_a_stack_something_else_is_already_running(self):
        self.assertIn("refuse_on_collision", self._body("up"), "up() no longer refuses an existing stack")
        self.assertIn("STACK_FORCE", self.text, "the deliberate override is gone, so the refusal cannot be bypassed")

    def test_down_removes_only_what_this_invocation_started(self):
        down = self._body("down")
        for destructive in ('docker volume rm "$DATA_VOL"', 'docker network rm "$NET"'):
            # Allowed ONLY under the explicit STACK_FORCE branch.
            if destructive in down:
                before = down.split(destructive, 1)[0]
                self.assertIn("STACK_FORCE", before, f"{destructive} runs unconditionally again")
        self.assertIn("owned ", down, "down() no longer scopes removal to the objects it labelled")
        self.assertIn("--label", self._body("up"), "up() no longer labels what it creates, so down() cannot scope")

    def test_the_header_does_not_claim_a_real_engine_is_unreachable(self):
        header = self.text.split("set -euo pipefail", 1)[0]
        self.assertNotIn(
            "NOT ALLOWED TO DO: reach a real engine",
            header,
            "the header claims unreachability again; it is argued from ci.env, not measured",
        )
        self.assertIn("No request is made", header, "the header no longer states what is actually true")


class TheJobCommentSaysWhatWasMeasured(unittest.TestCase):
    """Two figures in this job's own prose went stale inside one branch.

    The stub-usage count was measured before the SAME change pinned
    ENGINE_CONTROLLER_URL at the stub, after which the orchestrator polls
    `GET /state` every ENGINE_STATE_POLL_S for the life of the stack; and
    ci.env described a route the stub had stopped answering that way. Prose is
    what a reviewer decides this stack is safe from, so the two specific
    sentences are pinned.
    """

    def test_the_stale_stub_usage_figure_is_not_back(self):
        text = PIPELINE.read_text(encoding="utf-8")
        for stale in ("stub served 13 requests", "reached it 13 times"):
            self.assertNotIn(stale, text, f"the pre-ENGINE_CONTROLLER_URL figure is back: {stale!r}")

    def test_the_job_names_the_route_the_stub_really_serves_most(self):
        text = PIPELINE.read_text(encoding="utf-8")
        job = text[text.index("6b. END TO END") : text.index("7. THE AGGREGATE RELEASE GATE")]
        self.assertIn("GET /state", job, "the job comment does not name the controller poll at all")
        self.assertIn("ENGINE_STATE_POLL_S", job, "the job comment does not say what sets the poll rate")

    def test_ci_env_does_not_claim_the_stub_404s_on_state(self):
        text = CI_ENV.read_text(encoding="utf-8")
        self.assertNotIn(
            "loud 404 for /state",
            text,
            "ci.env claims a 404 on /state again; e2e/ci/engine.js answers 200 MONITORING_UNKNOWN",
        )
        self.assertIn("MONITORING_UNKNOWN", text, "ci.env no longer says what the stub answers on /state")

    def test_the_baseline_note_forbids_a_squash_merge(self):
        # The three 2026-09-27 fingerprints are keyed to one commit. A squash
        # rewrites it, so the target branch gets three STALE entries AND the same
        # three lines as NEW, and secret_gate.py fails on either.
        baseline = json.loads((REPO / ".github" / "workflows" / "gitleaks-baseline.json").read_text(encoding="utf-8"))
        note = " ".join(baseline["_comment"])
        self.assertIn("NEVER SQUASH", note, "the baseline note does not forbid a squash merge")
        fingerprints = {f["fingerprint"].split(":", 1)[0] for f in baseline["findings"]}
        named = {word.strip(".,;") for word in note.split() if len(word.strip(".,;")) == 40}
        self.assertTrue(
            named & fingerprints,
            "the note forbids a squash without naming a commit the fingerprints are keyed to",
        )

    def test_the_baseline_note_names_the_job_that_actually_runs_the_scan(self):
        baseline = json.loads((REPO / ".github" / "workflows" / "gitleaks-baseline.json").read_text(encoding="utf-8"))
        note = " ".join(baseline["_comment"])
        doc = load_pipeline()
        running = {
            job_id
            for job_id, job in (doc.get("jobs") or {}).items()
            if isinstance(job, dict)
            and any("secret_gate.py" in str(s.get("run", "")) for s in job_steps(job))
        }
        self.assertEqual(running, {"security"}, f"secret_gate.py moved job: {sorted(running)}")
        self.assertIn("`security` job", note, "the baseline note names the wrong job for the secret scan")


if __name__ == "__main__":
    unittest.main()
