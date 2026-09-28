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


def refusing_pairs() -> set:
    """Every (probe, verdict) pair that is a REFUSAL and belongs to a probe.

    The universal pair is excluded on purpose: `probe-raised` and
    `probe-timed-out` come from `run_probe`, not from a probe, and cannot have a
    probe-specific command behind them.
    """
    return {
        (probe, verdict)
        for probe, verdicts in box_probes.PROBE_VERDICTS.items()
        for verdict in verdicts - box_probes.PASSING[probe]
    }


class TheVerdictVocabularyIsDeclaredPerProbe(unittest.TestCase):
    """PROBE_VERDICTS is what gives the remedy coverage test teeth, so it has to
    be true. Before it existed the coverage test iterated the FLAT `VERDICTS`
    set for every probe, which asked for a remedy for pairs no probe can report
    (("disk", "wedged") and 200-odd others); every one of them fell through
    `remedy_for`'s catch-all, so the test passed with the entire remedy table
    deleted. Measured on 2026-09-27 by replacing `_remedies()`'s body with
    `return {}`: test_box_probes.py reported "Ran 23 tests ... OK"."""

    def test_the_flat_vocabulary_is_exactly_the_per_probe_one_plus_the_universal_pair(self):
        union = set(box_probes.UNIVERSAL_VERDICTS)
        for verdicts in box_probes.PROBE_VERDICTS.values():
            union |= set(verdicts)
        self.assertEqual(
            union,
            set(box_probes.VERDICTS),
            "a verdict is declared in one list and not the other; the extras are "
            "verdicts no probe can report, which is how `unavailable` survived",
        )

    def test_every_probe_declares_its_verdicts_and_its_passing_set(self):
        for name, _ in box_probes.ALL_PROBES:
            self.assertIn(name, box_probes.PROBE_VERDICTS, name)
            self.assertIn(name, box_probes.PASSING, name)
            self.assertLessEqual(
                set(box_probes.PASSING[name]), set(box_probes.PROBE_VERDICTS[name]), name
            )

    def test_every_verdict_a_probe_reports_against_the_fake_box_is_declared(self):
        """The map against the code, not just against the other map. Every
        scenario the other test files drive, in one loop."""
        scenarios = [
            {}, {"git-inside": box_probes.Completed(128, "")},
            {"git-status": box_probes.Completed(129, "")},
            {"git-status": box_probes.Completed(0, " M x\n")},
            {"git-branch": box_probes.Completed(0, "HEAD\n")},
            {"df": box_probes.Completed(1, "")}, {"df": box_probes.Completed(0, "Avail\n1G\n")},
            {"flock": box_probes.Completed(1, "")}, {"flock": box_probes.Completed(3, "")},
            {"bind-check": box_probes.Completed(1, "nothing proved\n")},
            {"controller": box_probes.Completed(7, "")},
            {"controller": box_probes.Completed(0, "not json")},
            {"completion": box_probes.Completed(7, "")},
            {"metrics": box_probes.Completed(0, "# empty\n")},
            {"schema-live": box_probes.Completed(1, "")},
            {"schema-code": box_probes.Completed(0, "1\n")},
        ]
        seen = set()
        with tempfile.TemporaryDirectory() as tmp:
            root = make_box(tmp)
            for overrides in scenarios:
                for result in box_probes.run_all(make_env(root, FakeRunner(**overrides))):
                    seen.add((result.probe, result.verdict))
        for probe, verdict in sorted(seen):
            self.assertIn(
                verdict, box_probes.PROBE_VERDICTS[probe], f"{probe} reported {verdict}"
            )


class EveryRefusalCarriesACommand(unittest.TestCase):
    def test_the_remedy_table_holds_exactly_the_refusing_pairs(self):
        """Asking `remedy_for` was a test that could not fail: it has a catch-all
        fallback for the universal pair, so `assertTrue(remedy)` was true for
        every input. This asserts the TABLE instead, in both directions - a
        missing entry is a refusal with no command behind it, and a spare entry
        is text that can never print."""
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            table = box_probes._remedies(env)
        self.assertEqual(set(table), refusing_pairs())

    def test_every_remedy_is_a_command_and_never_a_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            table = box_probes._remedies(env)
            for (probe, verdict), remedy in sorted(table.items()):
                self.assertTrue(remedy, f"{probe}/{verdict}")
                for command in remedy:
                    self.assertNotIn("<", command, f"{probe}/{verdict}: {command}")

    def test_the_universal_pair_falls_back_and_nothing_else_does(self):
        """The fallback exists for `probe-raised` and `probe-timed-out` only."""
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            table = box_probes._remedies(env)
            for probe, verdict in sorted(refusing_pairs()):
                self.assertIn((probe, verdict), table, f"{probe}/{verdict}")
            for verdict in sorted(box_probes.UNIVERSAL_VERDICTS):
                result = box_probes.ProbeResult("disk", verdict)
                self.assertNotIn(("disk", verdict), table)
                self.assertIn("box_readiness.py", box_probes.remedy_for(env, result)[0])

    def test_a_note_only_ever_sits_on_a_pair_that_can_print_it(self):
        """A note is only rendered under a refusal, so a note on a passing or
        impossible verdict is text nobody will ever read."""
        self.assertLessEqual(set(box_probes.NOTES), refusing_pairs())

    def test_no_remedy_or_note_leaks_an_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            for probe, verdict in sorted(refusing_pairs() | {
                ("disk", v) for v in box_probes.UNIVERSAL_VERDICTS
            }):
                lines = list(box_probes.remedy_for(env, box_probes.ProbeResult(probe, verdict)))
                lines.append(box_probes.note_for(box_probes.ProbeResult(probe, verdict)))
                for line in lines:
                    self.assertNotIn("://", line, f"{probe}/{verdict}")
                    self.assertEqual([], ip_literals(line), f"{probe}/{verdict}")


class TheAcceptedCountMatchesTheRealReport(unittest.TestCase):
    """The count against engine_bind ITSELF, not against a fixture.

    A fixture is a guess about another program's output, and this is the defect
    that guess hid: `_box_fixtures.BIND_EXPOSED` omitted engine_bind's own
    `- FAIL: ... ACCEPTED the connection: ...` summary line, so an
    `accepted_addresses` that was always one too high asserted clean. The real
    program prints the phrase once per address (engine_bind.py:879) and once more
    through `report.fail` (:896, rendered at :647-649, printed at :988).

    So this drives the real `engine_bind.evaluate`, through the fake prober
    test_engine_bind already owns, and feeds its actual report into the probe.
    Nothing here touches a network, a host or the GPU: every connection attempt
    is the fake prober, and the address list is test_engine_bind's fixture.
    """

    @staticmethod
    def _report_text(accepting):
        import test_engine_bind as eb

        report, _text, _prober, _made = eb.check(
            prober=eb.FakeProber({(address, eb.PORT): "connected" for address in accepting})
        )
        return "\n".join(report.lines)

    def _probe(self, text, rc):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner(**{"bind-check": box_probes.Completed(rc, text)}))
            return box_probes.probe_engine_exposure(env)

    def test_one_accepting_address_is_reported_as_one(self):
        import test_engine_bind as eb

        text = self._report_text([eb.LAN])
        self.assertEqual(text.count("ACCEPTED the connection"), 2, text)
        result = self._probe(text, 1)
        self.assertEqual(result.verdict, "exposed")
        self.assertEqual(result.facts["accepted_addresses"], 1)

    def test_two_accepting_addresses_are_reported_as_two(self):
        import test_engine_bind as eb

        text = self._report_text([eb.LAN, eb.TAILNET6])
        self.assertEqual(text.count("ACCEPTED the connection"), 3, text)
        result = self._probe(text, 1)
        self.assertEqual(result.verdict, "exposed")
        self.assertEqual(result.facts["accepted_addresses"], 2)

    def test_a_closed_report_is_not_counted_as_an_acceptance(self):
        text = self._report_text([])
        self.assertNotIn("ACCEPTED the connection", text)
        result = self._probe(text, 0)
        self.assertEqual(result.verdict, "closed")


# ==========================================================================
# THE OUTPUT-SAFETY CASES (B6). These are the reason this library exists in
# the shape it does, and they are what the second consumer inherits.
# ==========================================================================
class TheSecondConsumerSurfaceIsCallable(unittest.TestCase):
    """The contract mismatch with fix/production-watch-r3, closed on this side.

    That branch imports THIS module (PROBE_MODULE = "box_probes"), ships none of
    its own, and asserts at import that four callables are present. Measured
    against its own loader on 2026-09-28, before this change:

        ProbeContractError: box_probes.py does not expose
        probe_container_states, probe_host_guard_unit, probe_real_completion

    and then, for the ONE name that did exist:

        ProbeContractError: box_probes.probe_engine_exposure() requires a
        parameter this watch cannot supply: 'env'. It offers deploy_root,
        timeout.

    The second half is the part nobody had seen: renaming the completion probe
    alone would have left all four raising at CALL time instead of at import
    time, because production_truth.call_probe passes only the context keys a
    signature declares and refuses a required parameter it cannot supply. That
    is this file's defect - its header has claimed "the shared library behind
    two jobs" since the first commit while nothing in it was callable by a
    consumer without an `Environment`.

    These cases assert the property rather than the sibling's file, which is not
    on this branch: no probe on the surface may have a REQUIRED parameter outside
    the two keys the watch offers.
    """

    #: Exactly the keys production_truth's context carries.
    OFFERED = {"deploy_root", "timeout"}

    def test_the_surface_is_not_empty_so_the_rest_of_this_class_has_teeth(self):
        self.assertTrue(box_probes.SECOND_CONSUMER_PROBES)

    def test_every_name_on_the_surface_is_a_callable_this_module_exports(self):
        for name in box_probes.SECOND_CONSUMER_PROBES:
            self.assertTrue(callable(getattr(box_probes, name, None)), name)

    def test_no_probe_on_the_surface_requires_anything_the_watch_cannot_supply(self):
        import inspect

        for name in box_probes.SECOND_CONSUMER_PROBES:
            signature = inspect.signature(getattr(box_probes, name))
            for param_name, param in signature.parameters.items():
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                if param.default is param.empty:
                    self.assertIn(param_name, self.OFFERED, f"{name}({param_name})")

    def test_every_probe_on_the_surface_accepts_both_offered_keys(self):
        import inspect

        for name in box_probes.SECOND_CONSUMER_PROBES:
            names = set(inspect.signature(getattr(box_probes, name)).parameters)
            self.assertLessEqual(self.OFFERED, names, name)

    def test_a_probe_called_with_a_deploy_root_and_no_environment_still_runs(self):
        """The watch's actual call shape, end to end against the fake box."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_box(tmp)
            result = box_probes.probe_engine_exposure(deploy_root=root, timeout=5)
        self.assertEqual(result.probe, "engine-exposure")
        self.assertIn(result.verdict, box_probes.PROBE_VERDICTS["engine-exposure"])

    def test_a_probe_called_with_neither_refuses_rather_than_guessing_a_root(self):
        with self.assertRaises(box_probes.ProbeCallRefused):
            box_probes.probe_engine_exposure()

    def test_the_two_completion_names_are_one_function(self):
        """Two names, never two implementations of "ask the engine to generate"."""
        self.assertIs(box_probes.probe_completion, box_probes.probe_real_completion)

    def test_the_result_carries_everything_the_watch_reads_off_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = box_probes.probe_engine_exposure(deploy_root=make_box(tmp), timeout=5)
        self.assertIsInstance(result.ok, bool)
        self.assertIsInstance(result.could_run, bool)
        self.assertIsInstance(result.detail, str)

    def test_the_detail_the_watch_renders_is_one_line_and_allowlisted(self):
        """production_truth prints it inside a single table cell. The full
        engine_bind report lives on `report_lines`, which that consumer never
        reads, so twenty lines cannot end up in one row."""
        result = box_probes.ProbeResult(
            "engine-exposure",
            "exposed",
            {"accepted_addresses": 2, "exit_code": 1},
            ("line one", "line two"),
        )
        self.assertNotIn("\n", result.detail)
        self.assertEqual(result.detail, "accepted_addresses 2 | exit_code 1")
        self.assertEqual(list(result.report_lines), ["line one", "line two"])

    def test_a_reading_that_was_never_taken_says_so(self):
        """`could_run` is what stops the watch handing an unmeasured box to
        Prometheus as a real fault and staying green."""
        for verdict in sorted(box_probes.NOT_PERFORMED):
            self.assertFalse(
                box_probes.ProbeResult("completion", verdict).could_run, verdict
            )
        self.assertTrue(box_probes.ProbeResult("completion", "generated").could_run)

    def test_not_performed_only_names_verdicts_a_probe_can_actually_report(self):
        """A spare entry here is the same vacuous-coverage shape that let
        ("engine-exposure", "unavailable") sit in the remedy table."""
        declared = set(box_probes.UNIVERSAL_VERDICTS)
        for verdicts in box_probes.PROBE_VERDICTS.values():
            declared |= set(verdicts)
        self.assertLessEqual(box_probes.NOT_PERFORMED, declared)

    def test_the_only_verdict_that_is_both_passing_and_unperformed_is_the_documented_one(self):
        """("migrations", "unreadable") is deliberately both: the probe could
        not be performed, AND deploy.sh:244-252 proceeds in exactly that case.
        Any OTHER overlap would be a probe that passes while admitting it never
        ran, which is the shape this whole file is built to refuse."""
        overlap = {
            (probe, verdict)
            for probe, verdicts in box_probes.PASSING.items()
            for verdict in verdicts & box_probes.NOT_PERFORMED
        }
        self.assertEqual(overlap, {("migrations", "unreadable")})


class TheTimeoutCapOnlyEverLowersADeadline(unittest.TestCase):
    """A second consumer may not widen a deadline this file chose."""

    def test_no_cap_means_this_files_own_timeouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            for kind, seconds in box_probes.TIMEOUTS.items():
                self.assertEqual(env.budget(kind), float(seconds), kind)

    def test_a_cap_lowers_every_call_that_is_longer_than_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner(), timeout_cap=20)
            self.assertEqual(env.budget("exposure"), 20.0)
            self.assertEqual(env.budget("lock"), 15.0)

    def test_a_cap_can_never_widen_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner(), timeout_cap=10_000)
            for kind, seconds in box_probes.TIMEOUTS.items():
                self.assertEqual(env.budget(kind), float(seconds), kind)

    def test_a_cap_is_never_zero_or_negative_however_it_is_passed(self):
        """A probe with a zero timeout is a probe that cannot be performed, and
        `CommandRunner` asserts a positive one."""
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner(), timeout_cap=0)
            self.assertGreaterEqual(env.budget("git"), 1.0)

    def test_every_probe_asks_for_its_timeout_through_the_cap(self):
        """Not one `TIMEOUTS[...]` read left in a probe: a probe that reads the
        table directly is a probe a cap silently does not reach."""
        source = pathlib.Path(box_probes.__file__).read_text(encoding="utf-8")
        body = source.split("# -------------------------------------------------------------------- probes", 1)[1]
        self.assertNotIn('TIMEOUTS["', body)


class TheTokenCounterIsReadFromTheSameFieldAsVerify(unittest.TestCase):
    """The Prometheus text format allows an optional trailing timestamp:

        vllm:generation_tokens_total{engine="0",model_name="m"} 1000 1759000000000

    Reading the LAST whitespace field takes the TIMESTAMP as the counter, so a
    frozen counter scraped twice looks like a rising one and this probe passes a
    wedged engine -- the single thing it exists to catch, and the opposite of
    verify's `awk /^vllm:generation_tokens_total/ {s+=$2}`.

    Latent on the pinned build, whose prometheus_client emits two fields, so
    nothing but this test holds the two readers together.
    """

    FROZEN = 1000.0

    def _sum(self, body: str):
        return box_probes._generation_tokens(body)

    def test_a_timestamped_sample_reads_its_value_not_its_timestamp(self) -> None:
        body = 'vllm:generation_tokens_total{engine="0",model_name="m"} 1000 1759000000000\n'
        self.assertEqual(self._sum(body), self.FROZEN)

    def test_a_frozen_counter_stays_frozen_when_the_timestamp_moves(self) -> None:
        before = self._sum('vllm:generation_tokens_total{engine="0"} 1000 1759000000000\n')
        after = self._sum('vllm:generation_tokens_total{engine="0"} 1000 1759000030000\n')
        self.assertEqual(before, after, "a moving timestamp must not look like generation")
        self.assertFalse(after > before, "this is the wedge assertion; it must not pass")

    def test_it_agrees_with_verifys_awk_on_both_shapes(self) -> None:
        for body in (
            'vllm:generation_tokens_total{engine="0"} 1000\n',
            'vllm:generation_tokens_total{engine="0"} 1000 1759000000000\n',
        ):
            awk = sum(float(line.split()[1]) for line in body.splitlines() if line.strip())
            self.assertEqual(self._sum(body), awk, body)


class TheCompletionProbeReadsTheWholeReply(unittest.TestCase):
    """A chat template that ignores `enable_thinking` must not fail a healthy box.

    `chat_template_kwargs.enable_thinking` is honoured by the TEMPLATE, not by
    the server. A template that ignores it puts the eight tokens in
    `reasoning_content` and leaves `content` empty: the counter advances, the box
    is healthy, and reading `content` alone reported `empty-reply` and refused
    the deploy.
    """

    def _probe(self, message):
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(
                make_box(tmp),
                FakeRunner(completion=box_probes.Completed(0, _json.dumps({"choices": [{"message": message}]}))),
            )
            return box_probes.probe_real_completion(env)

    def test_content_alone_still_passes(self):
        result = self._probe({"content": "READY."})
        self.assertEqual(result.verdict, "generated")
        self.assertEqual(result.facts["reply_chars"], 6)
        self.assertEqual(result.facts["reasoning_chars"], 0)

    def test_reasoning_content_alone_is_a_generation_and_not_an_empty_reply(self):
        result = self._probe({"content": "", "reasoning_content": "READY"})
        self.assertEqual(result.verdict, "generated")
        self.assertEqual(result.facts["reply_chars"], 0)
        self.assertEqual(result.facts["reasoning_chars"], 5)

    def test_both_fields_null_is_still_an_empty_reply(self):
        result = self._probe({"content": None, "reasoning_content": None})
        self.assertEqual(result.verdict, "empty-reply")

    def test_a_message_that_is_not_an_object_at_all_is_an_empty_reply(self):
        result = self._probe("just a string")
        self.assertEqual(result.verdict, "empty-reply")

    def test_a_reply_with_a_still_counter_is_wedged_whichever_field_carried_it(self):
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(
                make_box(tmp),
                FakeRunner(
                    metrics_counts=[1000, 1000],
                    completion=box_probes.Completed(
                        0, _json.dumps({"choices": [{"message": {"reasoning_content": "READY"}}]})
                    ),
                ),
            )
            self.assertEqual(box_probes.probe_real_completion(env).verdict, "wedged")


class TheOnlyInterpolatedIdentifierIsCheckedWithoutAnAssert(unittest.TestCase):
    """`python -O` strips `assert`, and that was the only check on the one
    identifier `_schema_version` puts into a shell script. The workflow runs
    plain `python3`, so it held; a future caller under -O would have lost it
    with nothing going red.
    """

    def test_a_name_that_is_not_an_identifier_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = make_env(make_box(tmp), FakeRunner())
            with self.assertRaises(box_probes.ProbeCallRefused):
                box_probes._schema_version(env, env.deploy_root, "dr_live; rm -rf /")

    def test_it_is_still_refused_under_dash_O(self):
        """The whole point, so it is proved by running a real -O interpreter."""
        import subprocess

        scripts = str(pathlib.Path(box_probes.__file__).resolve().parent)
        program = (
            "import sys, pathlib; sys.path.insert(0, %r); import box_probes as bp;"
            "env = bp.Environment(deploy_root=pathlib.Path('/'), repo_root=pathlib.Path('/'), ref='');"
            "\ntry:\n bp._schema_version(env, env.deploy_root, 'x; rm -rf /')\n"
            " print('NOT REFUSED')\nexcept bp.ProbeCallRefused:\n print('REFUSED')" % scripts
        )
        out = subprocess.run(
            [sys.executable, "-O", "-c", program], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(out.stdout.strip(), "REFUSED", out.stderr)

    def test_neither_shipped_module_contains_an_assert_statement_at_all(self):
        """The rule, not just the one site. Parsed rather than grepped: the word
        `assert` appears in prose in both files, and a test that greps for it
        either fails on a comment or passes on a docstring that mentions it.

        Both files run on the production box and decide whether a deploy
        proceeds. An `assert` in either is a check that a future `python -O`
        silently deletes, and this file's whole subject is checks that stop
        checking without anything going red.
        """
        import ast

        for module in (box_probes, box_readiness):
            tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
            statements = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
            self.assertEqual(statements, [], f"{module.__name__} lines {statements}")


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
