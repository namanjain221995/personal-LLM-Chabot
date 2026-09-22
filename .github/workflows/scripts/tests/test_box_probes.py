"""The probe library, and above all WHAT IT IS ALLOWED TO PRINT.

The output-safety cases at the bottom are the B6 fix and they live here, in
the LIBRARY's test file, rather than beside the CLI: box_probes.py is imported
by more than one job, and a rule that is only tested next to one consumer is a
rule the next consumer has to remember. Tested here, it is inherited.

What they assert, against a `generated.env` fixture that carries a sentinel
secret value, the pointer to the real secrets file and an address-valued key:

  * none of those three strings appears anywhere in the output;
  * no output line contains `://`;
  * no output line contains an IPv4 or IPv6 literal;
  * and all of that still holds on the EXCEPTION path, driven by a probe that
    raises with the secret in its message.

The last one is the one that matters. `str(exc)` is the normal way a value
read out of a file reaches a log, and nobody decides that it should.
"""
from __future__ import annotations

import io
import ipaddress
import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import box_probes  # noqa: E402
import box_readiness  # noqa: E402
from _box_fixtures import (  # noqa: E402
    SENTINEL_ADDRESS,
    SENTINEL_SECRET,
    SENTINEL_SECRET_PATH,
    FakeRunner,
    Raises,
    make_box,
    make_env,
)

#: Candidate substrings that might be an address, deliberately broader than
#: box_probes' own detector: the test must not be able to agree with the code
#: by sharing its blind spot.
_CANDIDATE = re.compile(r"[0-9A-Fa-f][0-9A-Fa-f.:%]{2,}")


def ip_literals(line: str) -> list[str]:
    found = []
    for match in _CANDIDATE.finditer(line):
        token = match.group(0).strip(".:").split("%", 1)[0]
        if not token:
            continue
        try:
            ipaddress.ip_address(token)
        except ValueError:
            continue
        found.append(token)
    return found


def readiness_output(root, runner, **kwargs) -> str:
    out = io.StringIO()
    env = make_env(root, runner, **kwargs)
    box_readiness.run(env, out=out)
    return out.getvalue()


class TheEnvironmentReaderIsAnAllowlist(unittest.TestCase):
    def test_it_refuses_a_key_that_was_never_reviewed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_box(tmp)
            with self.assertRaises(box_probes.EnvReadRefused):
                box_probes.read_env_keys(
                    root / ".runtime" / "generated.env", ["TECHSARA_SECRET_ENV"]
                )

    def test_it_returns_only_the_named_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_box(tmp)
            got = box_probes.read_env_keys(root / ".runtime" / "generated.env", ["MAIN_MODEL"])
            self.assertEqual(list(got), ["MAIN_MODEL"])
            self.assertEqual(got["MAIN_MODEL"], "nvidia/Qwen-Test-Model")

    def test_a_missing_file_refuses_without_naming_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "nowhere" / "generated.env"
            with self.assertRaises(box_probes.EnvReadRefused) as caught:
                box_probes.read_env_keys(missing, ["MAIN_MODEL"])
            self.assertNotIn(str(missing), str(caught.exception))
            self.assertNotIn("generated.env", str(caught.exception))

    def test_the_read_allowlist_holds_exactly_one_key(self):
        # Widening it is a reviewable act, not a refactor: the same file holds
        # the pointer to the secrets file and every address the cluster uses.
        self.assertEqual(box_probes.READABLE_KEYS, frozenset({"MAIN_MODEL"}))


class ThePrintAllowlistDecidesEveryValue(unittest.TestCase):
    def test_a_key_nobody_listed_is_withheld(self):
        self.assertEqual(box_probes.render_fact("owner_password", "hunter2"), box_probes.WITHHELD)

    def test_a_count_must_be_a_non_negative_int_and_not_a_bool(self):
        self.assertEqual(box_probes.render_fact("free_gb", 21), "21")
        self.assertEqual(box_probes.render_fact("free_gb", -1), box_probes.WITHHELD)
        self.assertEqual(box_probes.render_fact("free_gb", True), box_probes.WITHHELD)
        self.assertEqual(box_probes.render_fact("free_gb", "21"), box_probes.WITHHELD)

    def test_a_token_is_charset_checked_because_it_comes_from_outside(self):
        self.assertEqual(box_probes.render_fact("engine_state", "READY"), "READY")
        self.assertEqual(
            box_probes.render_fact("engine_state", "http://1.2.3.4/"), box_probes.WITHHELD
        )
        self.assertEqual(
            box_probes.render_fact("engine_state", "a" * 40), box_probes.WITHHELD
        )

    def test_a_sha_is_shortened_and_a_non_sha_is_withheld(self):
        self.assertEqual(box_probes.render_fact("ref", "bd532e383e5bcafe01"), "bd532e383e5b")
        self.assertEqual(box_probes.render_fact("ref", "not-a-sha"), box_probes.WITHHELD)

    def test_a_path_carrying_a_url_or_an_address_is_withheld(self):
        self.assertEqual(box_probes.render_fact("deploy_root", "/home/x/y"), "/home/x/y")
        self.assertEqual(
            box_probes.render_fact("deploy_root", "http://1.2.3.4/y"), box_probes.WITHHELD
        )
        self.assertEqual(
            box_probes.render_fact("deploy_root", "/srv/10.1.2.3/y"), box_probes.WITHHELD
        )


class TheSanitizerIsTheLastLineOfDefence(unittest.TestCase):
    def test_it_redacts_urls_and_addresses(self):
        for hostile in (
            "http://127.0.0.1:9838/state",
            "10.77.88.99",
            "::1",
            "fe80::1%enp1s0",
            "2001:db8::dead:beef",
        ):
            with self.subTest(hostile=hostile):
                self.assertNotIn(hostile, box_probes.sanitize(f"probe said {hostile} ok"))

    def test_it_leaves_the_allowlisted_shapes_alone(self):
        line = "deploy-root clean-on-default dirty_files 0 | elapsed_s 12.3s | ref bd532e383e5b"
        self.assertEqual(box_probes.sanitize(line), line)


class EveryProbeFailsClosed(unittest.TestCase):
    def test_a_verdict_nobody_listed_as_passing_is_a_refusal(self):
        self.assertFalse(box_probes.ProbeResult("disk", "invented-verdict").ok)

    def test_every_probe_has_a_passing_set(self):
        for name, _ in box_probes.ALL_PROBES:
            self.assertIn(name, box_probes.PASSING, name)

    def test_every_passing_verdict_is_a_known_verdict(self):
        for name, verdicts in box_probes.PASSING.items():
            for verdict in verdicts:
                self.assertIn(verdict, box_probes.VERDICTS, f"{name}/{verdict}")

    def test_a_probe_that_raises_refuses_and_hides_its_message(self):
        def explode(_env):
            raise RuntimeError(f"boom {SENTINEL_SECRET}")

        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            result = box_probes.run_probe(env, "disk", explode)
        self.assertEqual(result.verdict, "probe-raised")
        self.assertFalse(result.ok)
        self.assertEqual(result.facts.get("exception_type"), "RuntimeError")
        self.assertNotIn(SENTINEL_SECRET, box_probes.render_facts(result.facts))

    def test_a_probe_that_returns_the_wrong_type_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            result = box_probes.run_probe(env, "disk", lambda _env: "fine, honest")
        self.assertEqual(result.verdict, "probe-raised")


class EveryRefusalCarriesACommand(unittest.TestCase):
    def test_every_non_passing_verdict_of_every_probe_has_a_remedy(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            for probe, passing in box_probes.PASSING.items():
                for verdict in sorted(box_probes.VERDICTS - passing):
                    remedy = box_probes.remedy_for(env, box_probes.ProbeResult(probe, verdict))
                    self.assertTrue(remedy, f"{probe}/{verdict}")
                    for command in remedy:
                        self.assertNotIn("<", command, f"{probe}/{verdict}: {command}")

    def test_no_remedy_or_note_leaks_an_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            for probe, passing in box_probes.PASSING.items():
                for verdict in sorted(box_probes.VERDICTS - passing):
                    lines = list(box_probes.remedy_for(env, box_probes.ProbeResult(probe, verdict)))
                    lines.append(box_probes.note_for(box_probes.ProbeResult(probe, verdict)))
                    for line in lines:
                        self.assertNotIn("://", line, f"{probe}/{verdict}")
                        self.assertEqual([], ip_literals(line), f"{probe}/{verdict}")


# ==========================================================================
# THE OUTPUT-SAFETY CASES (B6). These are the reason this library exists in
# the shape it does, and they are what the second consumer inherits.
# ==========================================================================
class NothingSecretReachesStdout(unittest.TestCase):
    def _assert_clean(self, output: str) -> None:
        self.assertNotIn(SENTINEL_SECRET, output)
        self.assertNotIn(SENTINEL_SECRET_PATH, output)
        self.assertNotIn("TECHSARA_SECRET_ENV", output)
        self.assertNotIn(SENTINEL_ADDRESS, output)
        for line in output.splitlines():
            self.assertNotIn("://", line, f"a URL reached the log: {line!r}")
            self.assertEqual([], ip_literals(line), f"an address reached the log: {line!r}")

    def test_a_healthy_run_prints_none_of_the_environment_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = readiness_output(make_box(tmp), FakeRunner())
        self.assertIn("VERDICT: READY", output)
        self._assert_clean(output)

    def test_foreign_output_carrying_a_url_and_an_address_is_scrubbed(self):
        # engine_bind.py's report is trusted by contract and sanitized anyway.
        with tempfile.TemporaryDirectory() as tmp:
            output = readiness_output(make_box(tmp), FakeRunner())
        self.assertIn("engine_bind.py printed for itself", output)
        self.assertIn("non-cluster addresses", output)
        self._assert_clean(output)

    def test_the_exception_path_is_just_as_quiet(self):
        hostile = RuntimeError(
            f"cannot read {SENTINEL_SECRET_PATH}: {SENTINEL_SECRET} "
            f"while dialling http://{SENTINEL_ADDRESS}:8000/v1/models"
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = readiness_output(make_box(tmp), FakeRunner(controller=Raises(hostile)))
        self.assertIn("probe-raised", output)
        self.assertIn("VERDICT: NOT READY", output)
        self._assert_clean(output)

    def test_a_probe_that_reports_an_unlisted_fact_prints_the_fixed_label(self):
        leaked = {"owner_password": SENTINEL_SECRET, "free_gb": 21}
        rendered = box_probes.render_facts(leaked)
        self.assertNotIn(SENTINEL_SECRET, rendered)
        self.assertIn(box_probes.WITHHELD, rendered)
        self.assertIn("free_gb 21", rendered)

    def test_every_fact_a_real_scenario_emits_is_on_the_print_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            for result in box_probes.run_all(env):
                for key in result.facts:
                    self.assertIn(key, box_probes.FACT_KINDS, f"{result.probe}: {key}")


if __name__ == "__main__":
    unittest.main()
