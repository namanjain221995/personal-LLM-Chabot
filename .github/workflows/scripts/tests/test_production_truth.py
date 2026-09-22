"""The standing watch's refusals, pinned.

Three of these tests are the B4 fix and they are the reason this job is
allowed to be green over a real fault at all: a fault verdict is green ONLY
when the verdict reached a sink that reads it. Delete the readback and
`test_exposed_with_the_textfile_directory_missing_is_red`,
`test_exposed_with_the_write_failing_is_red` and
`test_exposed_with_a_stale_metric_on_readback_is_red` all go green over a live
engine exposure -- which is precisely the display this design refuses.

Everything here runs offline. `production_truth.main()` takes a `Runtime`
carrying every side effect it has, so the import of `box_probes`, the deploy
lock, the textfile write, the Prometheus query, the clock and sleep are all
injected. Nothing in this file touches the box.
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import production_truth as pt  # noqa: E402


# --------------------------------------------------------------------- helpers


class Clock:
    """A clock the tests move by hand, so no test waits on anything."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _callable_for(value):
    if isinstance(value, BaseException):
        def raiser():
            raise value
        return raiser
    if callable(value):
        return value
    return lambda: value


def fake_probes(**overrides) -> types.ModuleType:
    """A stand-in for the box-readiness track's module, all probes clean."""
    values = {name: {"ok": True} for name in pt.REQUIRED_PROBES}
    values.update(overrides)
    module = types.ModuleType(pt.PROBE_MODULE)
    for name, value in values.items():
        if value is None:
            continue  # deliberately absent from the module
        setattr(module, name, _callable_for(value))
    return module


class Recorder:
    """Captures whether the sink was used, and with what."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[pathlib.Path, str]] = []
        self.error = error

    def __call__(self, directory: pathlib.Path, text: str) -> None:
        self.calls.append((directory, text))
        if self.error is not None:
            raise self.error


def make_runtime(
    *,
    probes=None,
    import_error: BaseException | None = None,
    lock: str = "free",
    writer: Recorder | None = None,
    blocker: str = "",
    samples=None,
    query_error: BaseException | None = None,
    clock: Clock | None = None,
) -> tuple[pt.Runtime, Clock, Recorder, dict]:
    clock = clock or Clock()
    writer = writer or Recorder()
    seen: dict = {"imported": [], "slept": []}

    def import_module(name: str):
        seen["imported"].append(name)
        if import_error is not None:
            raise import_error
        return probes if probes is not None else fake_probes()

    def query(expr: str):
        if query_error is not None:
            raise query_error
        if samples is None:
            return [(clock(), clock())]
        if callable(samples):
            return samples(clock)
        return samples

    runtime = pt.Runtime(
        import_module=import_module,
        now=clock,
        lock_state=lambda path: lock,
        write_textfile=writer,
        textfile_blocker=lambda path: blocker,
        query=query,
        sleep=lambda seconds: seen["slept"].append(seconds) or clock.advance(seconds),
    )
    return runtime, clock, writer, seen


ARGS = ["--textfile-dir", "/nowhere/textfile_collector", "--readback-deadline-seconds", "0"]


def run(runtime, argv=None) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pt.main(list(argv if argv is not None else ARGS), runtime=runtime)
    return code, out.getvalue() + err.getvalue()


# ------------------------------------------------------------- the happy answer


class AClearBoxIsGreenAndHandsOverNothing(unittest.TestCase):
    def test_all_probes_clean_is_green_with_verdict_ok(self):
        runtime, _, writer, _ = make_runtime()
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertIn("verdict", text)
        self.assertRegex(text, r"verdict\s+ok")
        self.assertRegex(text, r"result\s+GREEN")

    def test_a_clean_run_writes_no_textfile_at_all(self):
        runtime, _, writer, _ = make_runtime()
        run(runtime)
        self.assertEqual(writer.calls, [], "a clean verdict has nothing to hand over")


# ------------------------------------------- the channel rules: the B4 fix
#
# One fault verdict, five channels. The verdict is identical in all five; only
# whether anybody received it changes, and that alone decides the colour.


def _exposed_probes():
    return fake_probes(
        probe_engine_exposure={
            "ok": False,
            "performed": True,
            "detail": "a non-cluster address ACCEPTED the connection",
        }
    )


class AFaultIsGreenOnlyWhenSomebodyReceivesIt(unittest.TestCase):
    def test_exposed_with_a_healthy_fresh_scrape_is_green_and_carries_the_verdict(self):
        clock = Clock()
        runtime, _, writer, _ = make_runtime(
            probes=_exposed_probes(),
            samples=lambda c: [(c(), c())],
            clock=clock,
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertRegex(text, r"verdict\s+exposed")
        self.assertRegex(text, r"result\s+GREEN")
        self.assertIn("Prometheus", text)
        self.assertEqual(len(writer.calls), 1, "the verdict must actually be written")
        self.assertIn('techsara_production_truth_verdict{verdict="exposed"} 1', writer.calls[0][1])

    def test_exposed_with_the_textfile_directory_missing_is_red(self):
        writer = Recorder(error=pt.ChannelError("the node-exporter textfile directory does not exist"))
        runtime, _, _, _ = make_runtime(probes=_exposed_probes(), writer=writer)
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertRegex(text, r"verdict\s+exposed")
        self.assertRegex(text, r"result\s+RED")
        self.assertIn("does not exist", text)

    def test_exposed_with_the_write_failing_is_red(self):
        writer = Recorder(error=PermissionError(13, "Permission denied"))
        runtime, _, _, _ = make_runtime(probes=_exposed_probes(), writer=writer)
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertRegex(text, r"verdict\s+exposed")
        self.assertRegex(text, r"result\s+RED")
        self.assertIn("PermissionError", text)

    def test_exposed_with_a_stale_metric_on_readback_is_red(self):
        # The scraped sample predates this run's write: Prometheus is reading a
        # file somebody left behind, not this verdict.
        clock = Clock()
        runtime, _, writer, _ = make_runtime(
            probes=_exposed_probes(),
            samples=[(clock() - 3600.0, clock() - 3600.0)],
            clock=clock,
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertRegex(text, r"result\s+RED")
        self.assertIn("STALE", text)
        self.assertEqual(len(writer.calls), 1, "the write is attempted; the readback is what fails")

    def test_exposed_with_a_sample_older_than_two_scrape_intervals_is_red(self):
        clock = Clock()
        runtime, _, _, _ = make_runtime(
            probes=_exposed_probes(),
            # Value is fresh (>= written_at) but the sample itself is old:
            # Prometheus has stopped scraping.
            samples=lambda c: [(c() - 500.0, c() + 1.0)],
            clock=clock,
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("two scrape intervals", text)

    def test_exposed_with_the_metric_absent_on_readback_is_red(self):
        runtime, _, _, _ = make_runtime(probes=_exposed_probes(), samples=[])
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("ABSENT", text)

    def test_exposed_with_prometheus_unreachable_is_red(self):
        runtime, _, _, _ = make_runtime(
            probes=_exposed_probes(),
            query_error=pt.ChannelError("Prometheus did not answer (URLError)"),
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("did not answer", text)

    def test_the_verdict_nobody_receives_says_so_in_the_summary(self):
        writer = Recorder(error=pt.ChannelError("the node-exporter textfile directory does not exist"))
        runtime, _, _, _ = make_runtime(probes=_exposed_probes(), writer=writer)
        _, text = run(runtime)
        self.assertIn("A verdict nobody receives is not a verdict", text)


# --------------------------------------------------------------- wedged engines


class AWedgedEngineTravelsUnderTheSameRules(unittest.TestCase):
    def _wedged(self):
        return fake_probes(
            probe_real_completion={
                "ok": False,
                "detail": "the completion returned but the token counter did not advance",
            }
        )

    def test_a_completion_whose_counter_did_not_advance_is_wedged(self):
        clock = Clock()
        runtime, _, writer, _ = make_runtime(
            probes=self._wedged(), samples=lambda c: [(c(), c())], clock=clock
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertRegex(text, r"verdict\s+wedged")
        self.assertIn('techsara_production_truth_verdict{verdict="wedged"} 1', writer.calls[0][1])

    def test_wedged_with_no_sink_is_red_exactly_like_exposed(self):
        writer = Recorder(error=pt.ChannelError("the node-exporter textfile directory does not exist"))
        runtime, _, _, _ = make_runtime(probes=self._wedged(), writer=writer)
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertRegex(text, r"verdict\s+wedged")


class ContainerAndGuardFaultsAreNotMislabelled(unittest.TestCase):
    def test_a_restarting_container_is_degraded_not_exposed_and_not_wedged(self):
        clock = Clock()
        runtime, _, _, _ = make_runtime(
            probes=fake_probes(probe_container_states={"ok": False, "detail": "one container is restarting"}),
            samples=lambda c: [(c(), c())],
            clock=clock,
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertRegex(text, r"verdict\s+degraded")

    def test_an_inactive_host_guard_unit_is_degraded(self):
        clock = Clock()
        runtime, _, _, _ = make_runtime(
            probes=fake_probes(probe_host_guard_unit={"ok": False, "detail": "the boot unit is not active"}),
            samples=lambda c: [(c(), c())],
            clock=clock,
        )
        code, _ = run(runtime)
        self.assertEqual(code, pt.EXIT_OK)


# ----------------------------------------------- a probe that could not be made


class AProbeThatCouldNotBePerformedIsAlwaysRed(unittest.TestCase):
    def test_performed_false_is_red_and_never_consults_the_channel(self):
        runtime, _, writer, _ = make_runtime(
            probes=fake_probes(
                probe_engine_exposure={"ok": False, "performed": False, "detail": "the engine address is unresolvable"}
            )
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertRegex(text, r"verdict\s+unavailable")
        self.assertRegex(text, r"result\s+RED")
        self.assertEqual(writer.calls, [], "an unmeasured box has no verdict to hand over")

    def test_a_probe_that_raises_counts_as_not_performed(self):
        runtime, _, _, _ = make_runtime(
            probes=fake_probes(probe_container_states=RuntimeError("docker is unreachable"))
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("docker is unreachable", text, "an exception's text can carry a path")

    def test_unavailable_outranks_a_fault_even_with_a_healthy_channel(self):
        clock = Clock()
        runtime, _, writer, _ = make_runtime(
            probes=fake_probes(
                probe_engine_exposure={"ok": False, "detail": "accepted"},
                probe_container_states={"ok": False, "performed": False, "detail": "docker unreachable"},
            ),
            samples=lambda c: [(c(), c())],
            clock=clock,
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertRegex(text, r"verdict\s+unavailable")
        self.assertEqual(writer.calls, [])


class TheProbeModuleIsAContractNotAnAssumption(unittest.TestCase):
    def test_box_probes_missing_entirely_is_red(self):
        runtime, _, _, seen = make_runtime(import_error=ImportError("No module named 'box_probes'"))
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertRegex(text, r"verdict\s+unavailable")
        self.assertIn("ImportError", text)
        self.assertIn("box_probes.py could not be imported", text)

    def test_box_probes_missing_one_probe_is_red_and_names_it(self):
        runtime, _, _, _ = make_runtime(probes=fake_probes(probe_host_guard_unit=None))
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("probe_host_guard_unit", text)
        self.assertIn("never edit box_probes.py from here", text)

    def test_a_probe_returning_something_with_no_ok_is_red(self):
        runtime, _, _, _ = make_runtime(probes=fake_probes(probe_real_completion="fine"))
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("carries", text)

    def test_a_truthy_string_ok_is_refused_rather_than_read_as_clean(self):
        runtime, _, _, _ = make_runtime(probes=fake_probes(probe_real_completion={"ok": "false"}))
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("must be a boolean", text)

    def test_a_probe_demanding_an_unknown_parameter_is_red_not_guessed(self):
        module = fake_probes()

        def needs_something(generated_env):  # noqa: ARG001 - the point is the signature
            return {"ok": True}

        module.probe_engine_exposure = needs_something
        runtime, _, _, _ = make_runtime(probes=module)
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("generated_env", text)

    def test_a_probe_declaring_deploy_root_is_given_it(self):
        module = fake_probes()
        captured = {}

        def with_root(deploy_root, timeout=1.0):
            captured["root"] = deploy_root
            captured["timeout"] = timeout
            return {"ok": True}

        module.probe_engine_exposure = with_root
        runtime, _, _, _ = make_runtime(probes=module)
        code, _ = run(runtime, ARGS + ["--deploy-root", "/tmp/somewhere"])
        self.assertEqual(code, pt.EXIT_OK)
        self.assertEqual(captured["root"], pathlib.Path("/tmp/somewhere"))

    def test_an_object_with_attributes_is_accepted_as_well_as_a_mapping(self):
        module = fake_probes()
        module.probe_engine_exposure = lambda: types.SimpleNamespace(ok=True, detail="loopback only")
        runtime, _, _, _ = make_runtime(probes=module)
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)


# --------------------------------------------------- the first probe of all


class TheWatchYieldsToARelease(unittest.TestCase):
    def test_the_deploy_lock_held_defers_green_and_probes_nothing(self):
        runtime, _, writer, seen = make_runtime(lock="held")
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertRegex(text, r"verdict\s+deferred")
        self.assertRegex(text, r"result\s+GREEN")
        self.assertEqual(seen["imported"], [], "a deferred run imports no probe module")
        self.assertEqual(writer.calls, [], "a deferred run writes nothing")

    def test_a_deploy_lock_that_cannot_be_read_is_red_not_assumed_free(self):
        runtime, _, _, seen = make_runtime(lock="unknown")
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertRegex(text, r"verdict\s+unavailable")
        self.assertEqual(seen["imported"], [])


class TheLockProbeMatchesTheDeployPath(unittest.TestCase):
    """`deploy_lock_state` against a real flock, not a stub."""

    def test_a_missing_lock_file_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pt.deploy_lock_state(pathlib.Path(tmp) / "deploy.lock"), "free")

    def test_an_unlocked_file_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = pathlib.Path(tmp) / "deploy.lock"
            lock.touch()
            self.assertEqual(pt.deploy_lock_state(lock), "free")

    def test_a_file_another_process_holds_exclusively_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = pathlib.Path(tmp) / "deploy.lock"
            lock.touch()
            holder = subprocess.Popen(
                [sys.executable, "-c",
                 "import fcntl,os,sys,time\n"
                 "fd=os.open(sys.argv[1], os.O_RDWR|os.O_APPEND)\n"
                 "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                 "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
                 "time.sleep(30)\n",
                 str(lock)],
                stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                self.assertEqual(pt.deploy_lock_state(lock), "held")
            finally:
                holder.kill()
                holder.wait(timeout=10)
                holder.stdout.close()
            self.assertEqual(pt.deploy_lock_state(lock), "free", "the lock is released with the holder")


# ------------------------------------------------------------------- dry run


class DryRunWritesNothingAnywhere(unittest.TestCase):
    def test_dry_run_reaches_the_same_verdict_without_writing(self):
        runtime, _, writer, _ = make_runtime(probes=_exposed_probes(), blocker="")
        code, text = run(runtime, ARGS + ["--dry-run"])
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertRegex(text, r"verdict\s+exposed")
        self.assertIn("predicted", text)
        self.assertEqual(writer.calls, [], "--dry-run writes nothing, ever")

    def test_dry_run_is_red_when_the_sink_would_refuse_the_write(self):
        runtime, _, writer, _ = make_runtime(
            probes=_exposed_probes(),
            blocker="the node-exporter textfile directory does not exist",
        )
        code, text = run(runtime, ARGS + ["--dry-run"])
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("predicted", text)
        self.assertEqual(writer.calls, [])


# ------------------------------------------------------------------- the metric


class TheTextfileSaysExactlyOneThing(unittest.TestCase):
    def test_one_verdict_series_is_one_and_every_other_is_zero(self):
        text = pt.render_textfile("exposed", 1_700_000_000.0, ["a reason"])
        ones = [line for line in text.splitlines() if line.startswith(f"{pt.METRIC_PREFIX}_verdict") and line.endswith(" 1")]
        self.assertEqual(len(ones), 1)
        self.assertIn('verdict="exposed"', ones[0])
        self.assertIn(f"{pt.METRIC_PREFIX}_check_ok 1", text)
        self.assertIn(f"{pt.READBACK_EXPR} 1700000000", text)

    def test_unavailable_marks_the_check_as_carrying_no_information(self):
        text = pt.render_textfile("unavailable", 1.0, [])
        self.assertIn(f"{pt.METRIC_PREFIX}_check_ok 0", text)

    def test_the_directory_is_never_created_by_the_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "textfile_collector"
            with self.assertRaises(pt.ChannelError):
                pt.write_textfile(missing, "x\n")
            self.assertFalse(missing.exists(), "creating the sink would fake the handover")

    def test_a_real_write_lands_atomically_under_the_expected_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            pt.write_textfile(directory, "payload\n")
            self.assertEqual((directory / pt.TEXTFILE_NAME).read_text(), "payload\n")
            self.assertEqual([p.name for p in directory.iterdir()], [pt.TEXTFILE_NAME])


# ------------------------------------------------------------- output safety
#
# Inherited from box_probes.py by construction (this file opens no environment
# file at all), and asserted here on every branch including the exception path.


ADDRESS = "192.168.9.20"
V6 = "fd7a:115c:a1e0::9"
URL = "http://192.168.9.20:8000/v1/models"
SENTINEL = "s3cr3t-sentinel-value"


class NothingSensitiveEverReachesTheLog(unittest.TestCase):
    def _assert_clean(self, text: str) -> None:
        self.assertNotIn(ADDRESS, text)
        self.assertNotIn(V6, text)
        self.assertNotIn("://", text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("TECHSARA_SECRET_ENV", text)
        self.assertNotIn("generated.env", text)

    def test_a_probe_detail_full_of_addresses_is_scrubbed(self):
        runtime, _, _, _ = make_runtime(
            probes=fake_probes(
                probe_engine_exposure={
                    "ok": False,
                    "detail": f"{ADDRESS} and {V6} accepted, probed {URL} per .runtime/generated.env",
                }
            )
        )
        with mock.patch.dict(os.environ, {"TECHSARA_SECRET_ENV": SENTINEL, "CLUSTER_HEAD_IP": ADDRESS}):
            _, text = run(runtime)
        self._assert_clean(text)
        self.assertIn(pt.REDACTED, text)

    def test_the_exception_path_is_scrubbed_too(self):
        runtime, _, _, _ = make_runtime()
        runtime = pt.Runtime(
            import_module=runtime.import_module,
            now=runtime.now,
            lock_state=mock.Mock(side_effect=RuntimeError(f"cannot stat {URL} for {SENTINEL}")),
            write_textfile=runtime.write_textfile,
            textfile_blocker=runtime.textfile_blocker,
            query=runtime.query,
            sleep=runtime.sleep,
        )
        with mock.patch.dict(os.environ, {"TECHSARA_SECRET_ENV": SENTINEL}):
            code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL)
        self.assertIn("FATAL", text)
        self._assert_clean(text)

    def test_a_green_run_carries_no_address_either(self):
        runtime, _, _, _ = make_runtime(
            probes=fake_probes(probe_engine_exposure={"ok": True, "detail": f"only loopback, checked {ADDRESS}"})
        )
        with mock.patch.dict(os.environ, {"TECHSARA_SECRET_ENV": SENTINEL}):
            _, text = run(runtime)
        self._assert_clean(text)

    def test_scrub_redacts_every_shape_it_claims_to(self):
        text = pt.scrub(f"{ADDRESS} {V6} {URL} /x/.runtime/generated.env {SENTINEL}", [SENTINEL])
        self._assert_clean(text)

    def test_scrub_catches_the_compressed_ipv6_form(self):
        # The regression this pins: the first IPV6_RE enumerated
        # `(?:hex:){2,}` and therefore did not match `fd7a:115c:a1e0::9` --
        # the compressed form this cluster's tailnet addresses actually use.
        for address in ("fd7a:115c:a1e0::9", "fe80::1%eth0", "2001:db8::", "::1"):
            self.assertEqual(pt.scrub(address, []), pt.REDACTED, address)

    def test_scrub_does_not_take_the_closing_bracket_with_the_address(self):
        self.assertEqual(pt.scrub("probed (x://y) here", []), f"probed ({pt.REDACTED}) here")

    def test_scrub_leaves_ordinary_words_alone(self):
        self.assertEqual(pt.scrub("the boot unit is not active", []), "the boot unit is not active")

    def test_environment_secrets_reads_only_the_environment(self):
        values = pt.environment_secrets({"CLUSTER_WORKER_IP": ADDRESS, "SOME_OTHER": "keep"})
        self.assertIn(ADDRESS, values)
        self.assertNotIn("keep", values)


# --------------------------------------------------------------- readback logic


class TheReadbackAsksTwoQuestions(unittest.TestCase):
    def _channel(self, samples, *, deadline=0):
        clock = Clock()
        runtime, _, _, _ = make_runtime(samples=samples, clock=clock)
        return pt.readback(runtime, written_at=clock(), scrape_interval=10, deadline_seconds=deadline)

    def test_a_sample_at_the_freshness_boundary_is_accepted(self):
        clock = Clock()
        runtime, _, _, _ = make_runtime(samples=lambda c: [(c() - 20.0, c())], clock=clock)
        channel = pt.readback(runtime, written_at=clock(), scrape_interval=10, deadline_seconds=0)
        self.assertTrue(channel.ok, channel.detail)

    def test_a_sample_one_second_past_the_boundary_is_refused(self):
        clock = Clock()
        runtime, _, _, _ = make_runtime(samples=lambda c: [(c() - 21.0, c())], clock=clock)
        channel = pt.readback(runtime, written_at=clock(), scrape_interval=10, deadline_seconds=0)
        self.assertFalse(channel.ok)

    def test_it_keeps_asking_until_the_deadline_then_gives_up(self):
        clock = Clock()
        runtime, _, _, seen = make_runtime(samples=[], clock=clock)
        channel = pt.readback(runtime, written_at=clock(), scrape_interval=10, deadline_seconds=20)
        self.assertFalse(channel.ok)
        self.assertGreaterEqual(len(seen["slept"]), 1, "it must retry, not ask once")


class ClassificationPicksTheWorst(unittest.TestCase):
    def test_every_probe_clean_is_ok(self):
        verdict, reasons = pt.classify([pt.ProbeOutcome(n, True, True) for n in pt.REQUIRED_PROBES])
        self.assertEqual(verdict, "ok")
        self.assertEqual(reasons, [])

    def test_exposed_outranks_degraded_and_wedged(self):
        verdict, _ = pt.classify([
            pt.ProbeOutcome("probe_engine_exposure", True, False),
            pt.ProbeOutcome("probe_real_completion", True, False),
            pt.ProbeOutcome("probe_container_states", True, False),
        ])
        self.assertEqual(verdict, "exposed")

    def test_wedged_outranks_degraded(self):
        verdict, _ = pt.classify([
            pt.ProbeOutcome("probe_real_completion", True, False),
            pt.ProbeOutcome("probe_host_guard_unit", True, False),
        ])
        self.assertEqual(verdict, "wedged")

    def test_every_fault_verdict_has_a_severity_and_a_probe_that_produces_it(self):
        self.assertEqual(set(pt.FAULT_FOR_PROBE), set(pt.REQUIRED_PROBES))
        for verdict in set(pt.FAULT_FOR_PROBE.values()):
            self.assertIn(verdict, pt.SEVERITY)
            self.assertIn(verdict, pt.FAULT_VERDICTS)


class TheWorkflowFileMatchesTheseRules(unittest.TestCase):
    """The promises the YAML makes are asserted here, not only in review."""

    WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / "production-watch.yml"

    def setUp(self):
        if not self.WORKFLOW.is_file():
            self.fail(f"{self.WORKFLOW.name} is missing")
        self.text = self.WORKFLOW.read_text(encoding="utf-8")

    def test_commit_one_carries_no_schedule_trigger(self):
        # The no-alert-mail promise at merge time. A cron arrives in its own
        # one-line commit, after the textfile exporter exists on the box.
        import yaml

        doc = yaml.safe_load(self.text)
        triggers = doc.get("on", doc.get(True))
        self.assertNotIn("schedule", triggers, "a cron here would mail on every red tick")

    def test_the_watch_never_shares_the_release_paths_concurrency_group(self):
        # Asserted on the PARSED groups, not on the raw text: the header
        # comment names `deploy-dgx-spark` on purpose, to say which group this
        # job must never join and why.
        import yaml

        doc = yaml.safe_load(self.text)
        self.assertEqual(doc["concurrency"], {"group": "production-watch", "cancel-in-progress": False})
        self.assertEqual(
            doc["jobs"]["production-truth"]["concurrency"],
            {"group": "production-truth-watch", "cancel-in-progress": False},
        )

    def test_the_self_hosted_job_carries_the_positive_branch_guard(self):
        import yaml

        job = yaml.safe_load(self.text)["jobs"]["production-truth"]
        self.assertEqual(str(job["if"]).strip(), "github.ref == 'refs/heads/main'")
        self.assertEqual(job["timeout-minutes"], 8)
        self.assertEqual(job["permissions"], {"contents": "read"})

    def test_the_file_does_not_claim_the_release_gate_can_be_retired(self):
        lowered = self.text.lower()
        self.assertIn("does not retire", lowered)


if __name__ == "__main__":
    unittest.main()
