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
import re
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


def scraped_timestamp(text: str) -> float:
    """The check timestamp as a SCRAPER reads it: out of the file's own text.

    The green-channel tests used to hand the readback `(clock(), clock())` --
    the raw clock, never the number `render_textfile` actually writes. That is
    what hid the rounding defect fixed in the same commit as this helper: the
    file carries whole seconds while `written_at` carried the fraction too, and
    `readback` compares one against the other.
    """
    for line in text.splitlines():
        if line.startswith(f"{pt.READBACK_EXPR} "):
            return float(line.split()[-1])
    raise AssertionError(f"{pt.READBACK_EXPR} is not in the textfile at all")


def verdict_series(text: str) -> dict[str, str]:
    """`{label: value}` for every `_verdict{verdict="..."}` series in a textfile.

    Parsed out of the rendered text rather than read off a constant, so a
    series that appears or disappears is visible to the tests.
    """
    pattern = re.compile(
        rf'{re.escape(pt.METRIC_PREFIX)}_verdict\{{verdict="([^"]+)"\}} (\S+)'
    )
    return {m.group(1): m.group(2) for m in (pattern.fullmatch(line) for line in text.splitlines()) if m}


def perfect_prometheus(writer: Recorder):
    """The most favourable scrape that can physically exist.

    It returns, with no delay at all, exactly the value this run's own write
    put in the file. A red verdict under this fake is the script's own
    arithmetic, never a slow, stale or broken Prometheus.
    """

    def samples(clock):
        if not writer.calls:
            return []
        return [(clock(), scraped_timestamp(writer.calls[-1][1]))]

    return samples


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
    seen: dict = {"imported": [], "slept": [], "queried": []}

    def import_module(name: str):
        seen["imported"].append(name)
        if import_error is not None:
            raise import_error
        return probes if probes is not None else fake_probes()

    def query(expr: str):
        seen["queried"].append(expr)
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


class AClearBoxIsGreenAndRefreshesTheMetric(unittest.TestCase):
    """A clean run hands nothing OVER, but it does still WRITE.

    The class was `AClearBoxIsGreenAndHandsOverNothing`, which was accurate
    about the channel and wrong about the file. `ok` needs no readback and no
    proof of receipt -- it has no fault to justify -- and it must still rewrite
    the textfile, because a series nobody rewrites keeps its last value and the
    previous run's fault would stay at 1 in Prometheus for good.
    """

    def test_all_probes_clean_is_green_with_verdict_ok(self):
        runtime, _, writer, _ = make_runtime()
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertIn("verdict", text)
        self.assertRegex(text, r"verdict\s+ok")
        self.assertRegex(text, r"result\s+GREEN")

    def test_a_clean_run_REFRESHES_the_textfile_so_a_cleared_fault_CLEARS(self):
        # THIS TEST WAS `test_a_clean_run_writes_no_textfile_at_all`, and what
        # it pinned was a defect, not a property. Because `ok` wrote nothing,
        # after any fault the textfile kept `verdict{verdict="exposed"} 1` and
        # a frozen check timestamp FOREVER: the Prometheus alert this job hands
        # its faults to could never clear once the box had been fixed, only a
        # root `rm` would clear it, and every run stayed green while it did.
        # `render_textfile`'s own docstring justifies one series per verdict by
        # "a verdict that STOPS being reported leaves its old value at 0
        # instead of leaving a stale 1 behind" -- which is true only if the
        # file is REWRITTEN. This is that rewrite.
        writer = Recorder()
        clock = Clock(1_700_000_000.0)
        faulty, _, _, _ = make_runtime(
            probes=_exposed_probes(),
            writer=writer,
            samples=perfect_prometheus(writer),
            clock=clock,
        )
        code, text = run(faulty)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertEqual(len(writer.calls), 1, text)
        first = writer.calls[-1][1]
        self.assertIn('techsara_production_truth_verdict{verdict="exposed"} 1', first)

        clock.advance(3600.0)  # an hour later, the box has been fixed
        clean, _, _, _ = make_runtime(writer=writer, samples=perfect_prometheus(writer), clock=clock)
        code, text = run(clean)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertRegex(text, r"verdict\s+ok")
        self.assertRegex(text, r"result\s+GREEN")
        self.assertEqual(len(writer.calls), 2, "a clean run must rewrite the file, not skip it")
        second = writer.calls[-1][1]
        self.assertIn('techsara_production_truth_verdict{verdict="exposed"} 0', second)
        self.assertIn('techsara_production_truth_verdict{verdict="ok"} 1', second)
        self.assertGreater(
            scraped_timestamp(second),
            scraped_timestamp(first),
            "the check timestamp must advance, or a staleness alert fires on a healthy box",
        )

    def test_a_clean_run_is_green_even_when_the_refresh_fails(self):
        # The channel requirement stays FAULT-ONLY. `ok` hands nothing over, so
        # a sink that refuses the write must not turn a healthy box into a RED
        # run: a red scheduled run mails, and the owner's standing instruction
        # is no alert mail. The refusal is reported instead.
        writer = Recorder(error=pt.ChannelError("the node-exporter textfile directory does not exist"))
        runtime, _, _, _ = make_runtime(writer=writer)
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertRegex(text, r"verdict\s+ok")
        self.assertRegex(text, r"result\s+GREEN")
        self.assertIn("does not exist", text, "a failed refresh is still reported")

    def test_a_clean_run_does_not_spend_the_readback_deadline(self):
        # `ok` writes, and stops. The readback buys the right to be green OVER
        # A FAULT and there is no fault here to justify; waiting out the
        # Prometheus deadline on every healthy tick would hold the production
        # box's single runner for nothing.
        runtime, _, writer, seen = make_runtime()
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(seen["queried"], [], "a clean run asks Prometheus nothing")
        self.assertEqual(seen["slept"], [])

    def test_a_clean_dry_run_still_writes_nothing(self):
        runtime, _, writer, _ = make_runtime()
        code, text = run(runtime, ARGS + ["--dry-run"])
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertRegex(text, r"result\s+GREEN")
        self.assertIn("predicted", text)
        self.assertEqual(writer.calls, [], "--dry-run writes nothing, ever")


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


class TheReadbackComparesTheNumberThatWasWritten(unittest.TestCase):
    """A PERFECT Prometheus must never produce a red run. It used to, half the time.

    `render_textfile` writes the check timestamp as WHOLE SECONDS, and
    `readback` refuses a sample whose value is below the timestamp this run
    says it wrote. `use_channel` passed the raw `time.time()` float, fraction
    and all, so at every instant whose fraction was below .5 the number in the
    file was strictly SMALLER than the number it was compared against: a
    Prometheus that had scraped this run's own file instantly was declared
    "present but STALE", the run went RED, and a real engine exposure was
    reported as a broken monitor -- on the schedule this job promises will not
    mail. That is 50% of wall-clock instants.

    These tests drive the green channel through `render_textfile`, which is the
    file a scraper actually reads, instead of through the raw clock.
    """

    FRACTIONS = (0.0, 0.2, 0.37, 0.499, 0.5, 0.6, 0.83, 0.999)

    def _run_at(self, fraction: float):
        writer = Recorder()
        clock = Clock(1_700_000_000.0 + fraction)
        runtime, _, _, _ = make_runtime(
            probes=_exposed_probes(),
            writer=writer,
            samples=perfect_prometheus(writer),
            clock=clock,
        )
        code, text = run(runtime)
        return code, text, writer

    def test_a_perfect_scrape_is_green_at_every_fraction_of_a_second(self):
        for fraction in self.FRACTIONS:
            with self.subTest(fraction=fraction):
                code, text, writer = self._run_at(fraction)
                self.assertNotIn("STALE", text, "the scrape returned this run's OWN written value")
                self.assertRegex(text, r"result\s+GREEN")
                self.assertEqual(code, pt.EXIT_OK, text)
                self.assertEqual(len(writer.calls), 1)

    def test_the_value_in_the_file_is_the_value_the_readback_compares(self):
        # The coupling itself, not only its effect: whatever resolution the
        # timestamp is rendered at, the number the file carries must survive
        # being parsed back as a float. Change `render_textfile`'s format
        # without changing `quantise_timestamp` and this fails.
        for fraction in self.FRACTIONS:
            with self.subTest(fraction=fraction):
                now = pt.quantise_timestamp(1_700_000_000.0 + fraction)
                self.assertEqual(scraped_timestamp(pt.render_textfile("exposed", now, [])), now)

    def test_a_genuinely_older_sample_is_still_STALE(self):
        # The fix must not blunt the check it repairs. A value a whole second
        # below this run's write is a PREVIOUS run's file, and stays red.
        writer = Recorder()
        clock = Clock(1_700_000_000.37)
        runtime, _, _, _ = make_runtime(
            probes=_exposed_probes(),
            writer=writer,
            samples=lambda c: [(c(), pt.quantise_timestamp(c()) - 1.0)],
            clock=clock,
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertIn("STALE", text)


# ------------------------------- a run that measured nothing clears nothing


class ARunThatObservedNothingClearsNothing(unittest.TestCase):
    """`deferred` and `unavailable` write NOTHING, and that is deliberate.

    Writing on those two would zero `verdict{verdict="exposed"}` on the
    strength of a run that never looked at the box -- a rollout holding the
    deploy lock, or a probe that could not be performed, would silently clear a
    live exposure alert. Their silence leaves the last real reading in place
    and lets the freshness of `check_timestamp_seconds` say "the watch has not
    looked lately", which is a different statement from "the box is well".
    """

    def _leave_a_fault_in_the_file(self, writer, clock):
        runtime, _, _, _ = make_runtime(
            probes=_exposed_probes(),
            writer=writer,
            samples=perfect_prometheus(writer),
            clock=clock,
        )
        code, text = run(runtime)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertIn('techsara_production_truth_verdict{verdict="exposed"} 1', writer.calls[-1][1])

    def test_a_deferred_run_does_not_clear_a_previous_runs_fault(self):
        writer = Recorder()
        clock = Clock(1_700_000_000.0)
        self._leave_a_fault_in_the_file(writer, clock)
        clock.advance(1800.0)
        deferred, _, _, _ = make_runtime(lock="held", writer=writer, clock=clock)
        code, text = run(deferred)
        self.assertEqual(code, pt.EXIT_OK, text)
        self.assertRegex(text, r"verdict\s+deferred")
        self.assertEqual(len(writer.calls), 1, "a rollout must not clear an exposure alert")
        self.assertIn('techsara_production_truth_verdict{verdict="exposed"} 1', writer.calls[-1][1])

    def test_an_unavailable_run_does_not_clear_a_previous_runs_fault(self):
        writer = Recorder()
        clock = Clock(1_700_000_000.0)
        self._leave_a_fault_in_the_file(writer, clock)
        clock.advance(1800.0)
        blind, _, _, _ = make_runtime(
            probes=fake_probes(
                probe_container_states={"ok": False, "performed": False, "detail": "docker unreachable"}
            ),
            writer=writer,
            clock=clock,
        )
        code, text = run(blind)
        self.assertEqual(code, pt.EXIT_FAIL, text)
        self.assertRegex(text, r"verdict\s+unavailable")
        self.assertEqual(len(writer.calls), 1, "an unmeasured box must not clear an exposure alert")
        self.assertIn('techsara_production_truth_verdict{verdict="exposed"} 1', writer.calls[-1][1])

    def test_the_written_verdicts_are_exactly_the_observed_ones(self):
        self.assertEqual(pt.REPORTED_VERDICTS, frozenset({"ok"}) | pt.FAULT_VERDICTS)
        self.assertNotIn("deferred", pt.REPORTED_VERDICTS)
        self.assertNotIn("unavailable", pt.REPORTED_VERDICTS)
        # `ok` is written, but is green whatever the sink does.
        self.assertIn("ok", pt.NO_CHANNEL_NEEDED)
        self.assertIn("ok", pt.REPORTED_VERDICTS)


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
    """Every series in the file is one a REAL run can set, and no more.

    The contract is pinned in both directions here, because the interesting
    failure is not a wrong number, it is a series that reads plausibly and
    that nothing can ever move. The file used to carry
    `verdict{verdict="unavailable"}`, `verdict{verdict="deferred"}` and a
    `check_ok` whose HELP said "0 means the verdict carries no information" --
    and all three were unreachable, because `main()` only renders the verdicts
    in REPORTED_VERDICTS and a run that observed nothing writes no file at all.
    An alert written against any of them would have waited forever.
    """

    #: One probe set per verdict `main()` can WRITE, so the reachability test
    #: below drives the real path instead of calling the renderer by hand. Each
    #: fault verdict comes from FAULT_FOR_PROBE: engine exposure -> `exposed`,
    #: the real-completion probe -> `wedged`, container states -> `degraded`.
    PROBES_FOR_VERDICT: dict[str, dict] = {
        "ok": {},
        "exposed": {"probe_engine_exposure": {"ok": False}},
        "wedged": {"probe_real_completion": {"ok": False}},
        "degraded": {"probe_container_states": {"ok": False}},
    }

    def test_the_series_labels_are_exactly_the_verdicts_that_can_be_written(self):
        series = verdict_series(pt.render_textfile("ok", 1.0, []))
        self.assertEqual(set(series), set(pt.REPORTED_VERDICTS))
        self.assertEqual(set(pt.VERDICT_SERIES), set(pt.REPORTED_VERDICTS))
        # The two that made this a defect. `unavailable` is carried by the run
        # going RED, `deferred` by the absence of a new write; neither is a
        # label here, because no file this script writes could ever set it to 1.
        self.assertNotIn("unavailable", series)
        self.assertNotIn("deferred", series)

    def test_every_verdict_main_can_reach_writes_the_whole_label_set(self):
        self.assertEqual(
            set(self.PROBES_FOR_VERDICT),
            set(pt.REPORTED_VERDICTS),
            "a verdict became writable (or stopped being writable) and this table did not follow",
        )
        for verdict, overrides in sorted(self.PROBES_FOR_VERDICT.items()):
            with self.subTest(verdict=verdict):
                writer = Recorder()
                runtime, _, _, _ = make_runtime(
                    probes=fake_probes(**overrides),
                    writer=writer,
                    samples=perfect_prometheus(writer),
                )
                code, text = run(runtime)
                self.assertEqual(code, pt.EXIT_OK, text)
                self.assertRegex(text, rf"verdict\s+{verdict}")
                self.assertEqual(len(writer.calls), 1, text)
                written = writer.calls[-1][1]
                series = verdict_series(written)
                self.assertEqual(set(series), set(pt.REPORTED_VERDICTS))
                self.assertEqual(
                    {name for name, value in series.items() if value == "1"},
                    {verdict},
                    written,
                )
                self.assertIn(f"{pt.METRIC_PREFIX}_check_ok 1", written)
                self.assertNotIn(f"{pt.METRIC_PREFIX}_check_ok 0", written)

    def test_no_run_that_writes_a_file_can_mark_the_check_as_blind(self):
        # THIS TEST WAS `test_unavailable_marks_the_check_as_carrying_no_
        # information`, and it proved nothing: it called
        # `pt.render_textfile("unavailable", 1.0, [])` directly, an input
        # `main()` never supplies, so it was green over a branch production
        # could not take -- the same blind-test shape as the clean-run test
        # replaced in commit d7d7485. `check_ok 0` is now gone from the file
        # entirely; what says "the watch could not look" is the run exiting 1
        # and writing nothing, which
        # `ARunThatObservedNothingClearsNothing.test_an_unavailable_run_does_
        # not_clear_a_previous_runs_fault` pins.
        for verdict in sorted(pt.REPORTED_VERDICTS):
            with self.subTest(verdict=verdict):
                text = pt.render_textfile(verdict, 1.0, [])
                self.assertIn(f"{pt.METRIC_PREFIX}_check_ok 1", text)
                self.assertNotIn(f"{pt.METRIC_PREFIX}_check_ok 0", text)

    def test_check_ok_never_branches_on_a_verdict_the_file_cannot_carry(self):
        # The dead branch was `0 if verdict == 'unavailable' else 1`. It is not
        # observable from a written file -- `main()` never renders that verdict
        # -- so it is pinned here at the renderer, over EVERY verdict the script
        # knows about, including the two it never writes.
        def check_ok(verdict: str) -> str:
            prefix = f"{pt.METRIC_PREFIX}_check_ok "
            lines = [
                line for line in pt.render_textfile(verdict, 1.0, []).splitlines()
                if line.startswith(prefix)
            ]
            self.assertEqual(len(lines), 1, lines)
            return lines[0][len(prefix):]

        self.assertEqual(
            {verdict: check_ok(verdict) for verdict in sorted(pt.SEVERITY)},
            {verdict: "1" for verdict in sorted(pt.SEVERITY)},
            "check_ok branched on a verdict, and the only reachable value is 1",
        )

    def test_the_help_text_promises_only_values_the_file_can_carry(self):
        emitted = {
            line.split()[-1]
            for verdict in pt.REPORTED_VERDICTS
            for line in pt.render_textfile(verdict, 1.0, []).splitlines()
            if line.startswith(f"{pt.METRIC_PREFIX}_check_ok ")
        }
        self.assertEqual(emitted, {"1"})
        help_line = next(
            line
            for line in pt.render_textfile("ok", 1.0, []).splitlines()
            if line.startswith(f"# HELP {pt.METRIC_PREFIX}_check_ok")
        )
        promised = set(re.findall(r"(?<![\w.])[01](?![\w.])", help_line))
        self.assertLessEqual(
            promised,
            emitted,
            f"the HELP promises {sorted(promised - emitted)}, which no run can emit: {help_line}",
        )

    def test_one_verdict_series_is_one_and_every_other_is_zero(self):
        text = pt.render_textfile("exposed", 1_700_000_000.0, ["a reason"])
        ones = [line for line in text.splitlines() if line.startswith(f"{pt.METRIC_PREFIX}_verdict") and line.endswith(" 1")]
        self.assertEqual(len(ones), 1)
        self.assertIn('verdict="exposed"', ones[0])
        self.assertIn(f"{pt.METRIC_PREFIX}_check_ok 1", text)
        self.assertIn(f"{pt.READBACK_EXPR} 1700000000", text)

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

    def test_the_step_comment_does_not_call_the_job_purely_read_only(self):
        # The script writes the node-exporter textfile on every observed run,
        # `ok` included. The step comment used to open "READ-ONLY, and it yields
        # to a release" with no caveat, three lines from the text the previous
        # commit corrected, in a PUBLIC file. Match the module docstring.
        self.assertIn("READ-ONLY apart from ONE write", self.text)
        self.assertNotIn("READ-ONLY, and it yields to a release", self.text)

    def test_the_cron_gate_lists_all_three_blockers(self):
        # A cron makes a red run mail, so the things that must be true before
        # the `schedule:` commit are listed in the file, not in a chat message.
        # Blocker 3 was missing: without an alert rule, the readback proves only
        # that Prometheus SCRAPED the file, so "hands the fault to Prometheus,
        # which owns alerting" -- the whole justification for being green over a
        # real fault -- is unbacked at the alert layer.
        self.assertIn("box_probes.py", self.text)
        self.assertIn("WRITABLE BY THE RUNNER", self.text)
        self.assertIn("monitoring/prometheus/rules/", self.text)
        self.assertIn("metrics-contract.json", self.text)
        self.assertIn("absent()", self.text)


class TheDocsDoNotCiteAControlThisRepositoryLacks(unittest.TestCase):
    """The green-over-a-failed-write path may not name an alert that is absent.

    `refresh_channel` used to justify staying green when a CLEAN run's write
    fails with "the staleness of `check_timestamp_seconds` is what says the
    write is not landing, and that belongs to Prometheus like every other alert
    this job hands over." Measured 2026-09-27: no rule mentioning
    `techsara_production_truth_*` exists under monitoring/prometheus/rules/ and
    the metric is not in monitoring/developer-api/metrics-contract.json, so the
    control was imaginary -- and a refresh that has NEVER landed leaves no
    series at all, which no staleness expression fires on without `absent()`.

    This test fails BOTH ways on purpose: if the claim comes back while the rule
    is still missing, and if the rule lands while the docstring still says it
    does not exist.
    """

    REPO = pathlib.Path(__file__).resolve().parents[4]
    SOURCE = pathlib.Path(pt.__file__)
    WITHDRAWN = "belongs to Prometheus like every other alert this job hands over"

    def rule_exists(self) -> bool:
        rules = self.REPO / "monitoring" / "prometheus" / "rules"
        contract = self.REPO / "monitoring" / "developer-api" / "metrics-contract.json"
        files = sorted(rules.glob("*.yml")) + sorted(rules.glob("*.yaml")) if rules.is_dir() else []
        if contract.is_file():
            files.append(contract)
        return any(pt.METRIC_PREFIX in path.read_text(encoding="utf-8") for path in files)

    def test_the_withdrawn_sentence_stays_withdrawn_while_no_rule_exists(self):
        # assertFalse over a membership test, not assertNotIn: the haystack is
        # the whole script, and a failure message carrying it is unreadable.
        source = self.SOURCE.read_text(encoding="utf-8")
        if self.rule_exists():
            self.assertFalse(
                "finds no Prometheus rule" in source,
                "a rule for this metric now exists under monitoring/prometheus/rules/ or in "
                "the metrics contract: update the docstrings that still say it does not",
            )
            return
        self.assertFalse(
            self.WITHDRAWN in source,
            "no rule under monitoring/prometheus/rules/ reads this metric and it is not in "
            "monitoring/developer-api/metrics-contract.json, so this sentence names a "
            f"compensating control that does not exist: {self.WITHDRAWN!r}",
        )
        self.assertIn(
            "absent()",
            pt.refresh_channel.__doc__ or "",
            "the caveat about an absent series must stay with the code that needs it",
        )


if __name__ == "__main__":
    unittest.main()
