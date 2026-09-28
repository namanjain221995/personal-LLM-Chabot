"""The CI shard split covers the suite exactly once — asserted here, locally.

`.github/workflows/scripts/shard_tests.py --check` runs as the first real step
of every shard in pipeline.yml, so an unassigned file fails the build. That is
the enforcement. This file is the same guarantee where a developer meets it:
`pytest tests/test_ci_shards.py` says whether the split is still whole without
pushing anything, and it fails the moment a file stops being assigned.

The failure this exists to prevent is not a red build. It is a GREEN one. A
split that drops a file leaves all three shards passing while the dropped
file's tests never execute, so the run reports success for work it did not do.
Commit 40bd49d refused to shard the suite until this guard landed with it.

Nothing here needs a database or a network; it is file arithmetic over the
checkout and a read of the workflow.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "workflows" / "scripts" / "shard_tests.py"
PIPELINE = REPO / ".github" / "workflows" / "pipeline.yml"
WEIGHTS = REPO / ".github" / "workflows" / "scripts" / "shard_weights.json"
TESTS_DIR = REPO / "orchestrator" / "tests"


def _load_script():
    """Import shard_tests.py by path: `.github` is not an importable package."""
    spec = importlib.util.spec_from_file_location("shard_tests", SCRIPT)
    assert spec and spec.loader, f"{SCRIPT} is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shard_tests = _load_script()


def _conftest():
    """The tests/conftest.py pytest already loaded, whatever it named it."""
    target = (TESTS_DIR / "conftest.py").resolve()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and pathlib.Path(path).resolve() == target:
            return module
    spec = importlib.util.spec_from_file_location("_shard_conftest", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pipeline() -> dict:
    return yaml.safe_load(PIPELINE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def orchestrator_job(pipeline) -> dict:
    return pipeline["jobs"]["orchestrator"]


def _render(value: str, shard: int, total: int, env: dict | None = None) -> str:
    """Resolve the handful of `${{ }}` expressions this job actually uses."""
    out = value.replace("${{ matrix.shard }}", str(shard))
    out = out.replace("${{ strategy.job-total }}", str(total))
    for key, raw in (env or {}).items():
        out = out.replace("${{ env.%s }}" % key, raw)
    return out


# ------------------------------------------------------- the split is whole
def test_the_union_of_every_shard_is_the_whole_suite():
    files, bins, _ = shard_tests.plan(REPO, shard_tests.SHARDS)
    union: set[str] = set()
    for group in bins:
        union |= set(group)
    assert union == set(files)


def test_no_file_is_in_two_shards():
    _, bins, _ = shard_tests.plan(REPO, shard_tests.SHARDS)
    for i, left in enumerate(bins):
        assert len(left) == len(set(left)), f"shard {i + 1} lists a file twice"
        for j, right in enumerate(bins[i + 1:], start=i + 2):
            overlap = set(left) & set(right)
            assert not overlap, f"shards {i + 1} and {j} share {sorted(overlap)}"


def test_the_parts_add_up_to_the_whole():
    files, bins, _ = shard_tests.plan(REPO, shard_tests.SHARDS)
    assert sum(len(g) for g in bins) == len(files)


def test_no_shard_is_empty():
    _, bins, _ = shard_tests.plan(REPO, shard_tests.SHARDS)
    assert all(bins), "a shard with no files is `pytest -q -rs` with no paths"


def test_check_passes_on_this_checkout():
    ok, report = shard_tests.check(REPO, shard_tests.SHARDS)
    assert ok, "\n".join(report)


def test_discovery_finds_every_file_pytest_would_collect():
    """The split must not be blind to a test file pytest can see.

    A top-level `tests/test_*.py` glob would miss `tests/whatever/test_x.py`,
    and a file that discovery cannot see is a file no shard runs.
    """
    expected = {
        p.relative_to(REPO / "orchestrator").as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for p in TESTS_DIR.rglob(pattern)
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert set(shard_tests.discover(REPO)) == expected
    assert len(expected) >= shard_tests.MIN_FILES


def test_the_assignment_does_not_depend_on_the_run():
    first = shard_tests.plan(REPO, shard_tests.SHARDS)[1]
    second = _load_script().plan(REPO, shard_tests.SHARDS)[1]
    assert first == second


# ------------------------- the timing map BALANCES the split, never selects it
#
# shard_weights.json is why the shards finish together instead of one of them
# carrying 28 minutes while another carries 17. It is also the only part of the
# split that can rot quietly, because a wrong weight produces a GREEN run that
# is merely slower than it needs to be. These tests pin the two things that
# must stay true whatever state the map is in: every discovered file is still
# weighted and still assigned, and a map that is stale, empty or unreadable
# costs balance and nothing else.


def _fake_checkout(tmp_path, names, seconds=None):
    """A throwaway tree shaped like the repo, plus its own weights file.

    shard_tests works off a root path and real file sizes, so a directory is
    all it needs — nothing here imports, collects or runs a test. Returns
    (root, weights_path); pass weights_path=None through monkeypatch to test
    the no-map case.
    """
    tests = tmp_path / "orchestrator" / "tests"
    tests.mkdir(parents=True)
    for i, name in enumerate(names):
        # Sizes differ so the size-based estimate has something to work with.
        (tests / name).write_text("# probe\n" * (i + 1), encoding="utf-8")
    weights_path = tmp_path / "weights.json"
    weights_path.write_text(json.dumps({"seconds": seconds or {}}), encoding="utf-8")
    return tmp_path, weights_path


def test_the_committed_map_is_shaped_the_way_the_script_reads_it():
    """A hand-edit that empties or malforms the map must not pass unnoticed.

    `measured()` swallows a broken map on purpose — losing it may not fail a
    build — so nothing in CI would go red. This test is the thing that does.
    """
    raw = json.loads(WEIGHTS.read_text(encoding="utf-8"))
    assert isinstance(raw.get("seconds"), dict) and raw["seconds"]
    bad = {
        name: value
        for name, value in raw["seconds"].items()
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
    }
    assert not bad, f"shard_weights.json has non-positive weights: {sorted(bad)[:5]}"
    assert shard_tests.measured() == {k: float(v) for k, v in raw["seconds"].items()}


def test_the_map_records_where_its_numbers_came_from():
    """A timing map with no provenance cannot be judged stale or fresh."""
    raw = json.loads(WEIGHTS.read_text(encoding="utf-8"))
    source, seconds = raw["_source"], raw["seconds"]
    assert source["files"] == len(seconds)
    assert len(source["runs"]) >= 2, "one run cannot settle a timing on a hosted runner"
    assert source["commit"] and source["method"] and source["measured"]
    recorded = sum(float(v) for v in seconds.values())
    assert abs(recorded - float(source["total_seconds"])) < 1.0


#: How far the map's sum over a measured shard may sit BELOW that shard's own
#: observed pytest total. It is a deficit, never a surplus: `pytest -q` spends
#: its session start (imports, CREATE DATABASE, the migrations) before the first
#: progress line, so that time is attributable to no file and cannot appear in a
#: per-file map. Measured on runs 259 and 260: 27.91 / 19.78 / 14.44 s, i.e.
#: ratios 0.9709 / 0.9877 / 0.9867. 6% is the nearest round floor under the
#: worst of the three.
RECONCILE_FLOOR = 0.94


def test_the_map_reconciles_with_the_runs_it_was_measured_from():
    """THE ONLY TEST THAT CAN GO RED WHEN THESE PER-FILE SECONDS ARE WRONG.

    Every other check on the map asserts its SHAPE — that `seconds` is an
    object, that every value is positive, that `_source` records a commit and a
    method, that the values sum to `total_seconds`. All of those pass for any
    self-consistent set of numbers, including a set measured from the wrong
    thing. And the balance test cannot help: it plans FROM this map and then
    scores that plan AGAINST the same map, so longest-processing-time makes it
    flat whatever the numbers say. That is exactly how the previous map — the
    one derived from the two UNSHARDED runs — sat in the repository for 259 runs
    printing a confident flat "21.8 min / 21.8 min / 21.8 min" while the shards
    it produced ran 16m55s, 28m13s and 20m52s.

    This test closes that by leaving the map's own frame of reference. It sums
    `seconds` over the file lists the measured runs ACTUALLY ran, recorded in
    `_source.measured_shards`, and compares each sum with that shard's own
    observed pytest total, recorded in `_source.observed_pytest_seconds`. Both
    sides are frozen in the committed file, so the test is deterministic and
    cannot drift with the checkout, the box or the suite's growth: it changes
    only when someone edits the map. Which is the point.

    A refresh that measures the wrong runs, scales onto the wrong totals, or
    silently keeps half the old numbers will land outside the band. If a future
    refresh deliberately changes the derivation — scaling onto the MEAN of the
    two runs rather than the LOWER, say — then this band is what has to be
    edited in the same commit, with the new ratios written down.
    """
    source = json.loads(WEIGHTS.read_text(encoding="utf-8"))["_source"]
    seconds = shard_tests.measured()
    lists = source["measured_shards"]
    observed = source["observed_pytest_seconds"]

    # The recorded lists must be the whole map, exactly once each: a partial
    # record would let half the map drift unreconciled.
    union: set[str] = set()
    for group in lists.values():
        union |= set(group)
    assert union == set(seconds), (
        "_source.measured_shards must name exactly the files in `seconds`; "
        f"only in seconds: {sorted(set(seconds) - union)[:5]}, "
        f"only in measured_shards: {sorted(union - set(seconds))[:5]}"
    )
    assert sum(len(group) for group in lists.values()) == len(union), (
        "the recorded shard lists overlap, so a file's seconds would be "
        "reconciled against two different observed totals"
    )
    # Deliberately NOT `len(lists) == SHARDS`. This record describes the run
    # that was measured, which had however many shards it had; the per-file
    # seconds do not depend on today's shard count. Coupling the two would mean
    # adding a fourth shard could only be done by re-measuring the map, which
    # is a cost with nothing behind it. What must hold is that the record is
    # internally consistent: one file list per observed total, in every run.
    for run, totals in observed.items():
        assert len(totals) == len(lists), (
            f"run {run} records {len(totals)} observed shard total(s) against "
            f"{len(lists)} recorded file list(s)"
        )

    for index in range(len(lists)):
        group = lists[str(index + 1)]
        predicted = sum(seconds[f] for f in group)
        # The LOWER of the runs, which is what the derivation says it scaled
        # onto: contention is one-sided, so the lower total is the closer one to
        # the work itself.
        floor_total = min(float(observed[run][index]) for run in observed)
        ratio = predicted / floor_total
        assert RECONCILE_FLOOR <= ratio <= 1.0, (
            f"shard {index + 1}: the map sums to {predicted:.2f} s over the "
            f"{len(group)} files that run measured, against an observed pytest "
            f"total of {floor_total:.2f} s — a ratio of {ratio:.4f}, outside "
            f"{RECONCILE_FLOOR}..1.0. Above 1.0 the map claims more seconds "
            "than the run took, which no scaling of a real measurement can do; "
            "below the floor it is missing more than pytest's session start. "
            "Either the seconds were not measured from these runs, or "
            "_source no longer describes how they were derived."
        )


def test_every_discovered_file_is_weighted_whatever_the_map_says():
    """No discovered file may end up with a zero or missing weight.

    A weight of zero sorts to the end and packs as free work, which is how a
    heavy file lands on an already-full shard.
    """
    files, _, weight = shard_tests.plan(REPO, shard_tests.SHARDS)
    assert set(weight) == set(files)
    assert all(weight[f] > 0 for f in files), sorted(f for f in files if weight[f] <= 0)


def test_a_file_the_map_has_never_seen_is_still_run(tmp_path, monkeypatch):
    """THE NEW-FILE CONTRACT. Adding a test file must never drop it.

    A new file is not in shard_weights.json, so it cannot be weighted by
    measurement. It is weighted by its SIZE at the map's own measured
    seconds-per-byte, and it is assigned like any other file: exactly one
    shard, and `--check` passes. The estimate is a placeholder for balance; it
    is not allowed to become a question of whether the file runs at all.
    """
    names = [f"test_known_{i}.py" for i in range(9)] + ["test_brand_new.py"]
    root, weights_path = _fake_checkout(
        tmp_path, names, seconds={f"tests/test_known_{i}.py": 10.0 for i in range(9)}
    )
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", weights_path)
    monkeypatch.setattr(shard_tests, "MIN_FILES", len(names))

    files, bins, weight = shard_tests.plan(root, 3)
    assert "tests/test_brand_new.py" in files
    placed = [i for i, group in enumerate(bins) if "tests/test_brand_new.py" in group]
    assert len(placed) == 1, "the unmeasured file must be in exactly one shard"
    assert weight["tests/test_brand_new.py"] > 0, "an unmeasured file must not weigh zero"

    ok, report = shard_tests.check(root, 3)
    assert ok, "\n".join(report)


def test_a_zero_byte_new_file_still_weighs_something(tmp_path, monkeypatch):
    """The size estimate's degenerate case. An empty file is not free work.

    A weight of exactly 0.0 sorts first in the packer's descending order and
    adds nothing to any shard's load, so it is the one estimate that could make
    `assign` treat a file as if it were not there.
    """
    root, weights_path = _fake_checkout(
        tmp_path, [f"test_known_{i}.py" for i in range(4)],
        seconds={f"tests/test_known_{i}.py": 10.0 for i in range(4)},
    )
    (root / "orchestrator" / "tests" / "test_empty.py").write_bytes(b"")
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", weights_path)
    monkeypatch.setattr(shard_tests, "MIN_FILES", 5)

    files, bins, weight = shard_tests.plan(root, 3)
    assert weight["tests/test_empty.py"] >= shard_tests.FLOOR_SECONDS > 0
    assert sum("tests/test_empty.py" in group for group in bins) == 1
    ok, report = shard_tests.check(root, 3)
    assert ok, "\n".join(report)


def test_the_report_names_the_files_it_could_only_estimate(tmp_path, monkeypatch):
    """An estimated file is visible in the log, not only in the clock."""
    names = [f"test_known_{i}.py" for i in range(9)] + ["test_brand_new.py"]
    root, weights_path = _fake_checkout(
        tmp_path, names, seconds={f"tests/test_known_{i}.py": 10.0 for i in range(9)}
    )
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", weights_path)
    monkeypatch.setattr(shard_tests, "MIN_FILES", len(names))

    ok, report = shard_tests.check(root, 3)
    text = "\n".join(report)
    assert ok, text
    assert "estimated from file size" in text
    assert "estimated: tests/test_brand_new.py" in text


def test_a_map_that_is_missing_entirely_costs_balance_and_nothing_else(tmp_path, monkeypatch):
    """The coverage proof must not depend on the timing map in any way."""
    names = [f"test_thing_{i}.py" for i in range(7)]
    root, weights_path = _fake_checkout(tmp_path, names)
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", weights_path.parent / "gone.json")
    monkeypatch.setattr(shard_tests, "MIN_FILES", len(names))

    assert shard_tests.measured() == {}
    files, bins, weight = shard_tests.plan(root, 3)
    union: set[str] = set()
    for group in bins:
        union |= set(group)
    assert union == set(files) and sum(len(g) for g in bins) == len(files)
    assert all(weight[f] > 0 for f in files)

    ok, report = shard_tests.check(root, 3)
    assert ok, "\n".join(report)
    assert "unreadable or empty" in "\n".join(report)


def test_an_unreadable_map_is_not_an_exception(tmp_path, monkeypatch):
    """Garbage in the map file must degrade to "no map", not crash the guard."""
    broken = tmp_path / "broken.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", broken)
    assert shard_tests.measured() == {}
    ok, report = shard_tests.check(REPO, shard_tests.SHARDS)
    assert ok, "\n".join(report)


#: Every way a map file can be present and unusable, as TEXT, because the CLI is
#: what CI runs and a hand-edit is what produces these. The first two reach
#: `measured` through json's own errors; the middle three are a top-level value
#: that is not an object, which makes the `["seconds"]` lookup itself raise
#: TypeError; the last five are the shape that cost the most to find — the key
#: is PRESENT, so nothing raises until the result is iterated.
MALFORMED_MAPS = (
    ("truncated json", "{ this is not json"),
    ("empty file", ""),
    ("top-level list", "[1, 2, 3]"),
    ("top-level number", "7"),
    ("top-level string", '"seconds"'),
    ("seconds is null", '{"seconds": null}'),
    ("seconds is a list", '{"seconds": [1, 2]}'),
    ("seconds is a number", '{"seconds": 3}'),
    ("seconds is a string", '{"seconds": "1423.41"}'),
    ("seconds is a bool", '{"seconds": true}'),
)


@pytest.mark.parametrize("label,text", MALFORMED_MAPS, ids=[m[0] for m in MALFORMED_MAPS])
def test_no_shape_of_broken_map_can_fail_the_build(tmp_path, monkeypatch, label, text):
    """A MAP IS A BALANCE INPUT. Losing it must cost balance and nothing else.

    `measured` is documented as returning {} for anything it cannot read, and
    three separate places — the module docstring, that docstring, and the commit
    that added it — promise a broken map cannot fail a build. The promise was
    only ever tested against invalid JSON. It did not hold for the shapes below
    where the `seconds` KEY EXISTS but does not hold an object: the lookup
    succeeds, the except tuple never fires, and iterating the result raised
    AttributeError straight out of the guard step. `--check` exited 1, which
    fails the shard job, which fails all three — the exact opposite of the
    guarantee, and a REGRESSION against the behaviour before the map was
    rewritten, where `{"seconds": [1, 2]}` merely produced a nonsense number.

    So this is parametrised over every shape rather than over one: the class of
    input is "present but not an object", and one example of it is what the
    previous test proved, which was not the same thing.
    """
    broken = tmp_path / "broken.json"
    broken.write_text(text, encoding="utf-8")
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", broken)

    assert shard_tests.measured() == {}, label
    ok, report = shard_tests.check(REPO, shard_tests.SHARDS)
    text_report = "\n".join(report)
    assert ok, text_report
    # And it must SAY so, rather than printing byte counts wearing a unit.
    assert "EVERY weight above is a file-size estimate" in text_report, text_report
    assert " min" not in text_report.split("\n")[2], (
        "with no usable map the weights are raw byte counts, so a minutes "
        f"column would be fabricated: {text_report.splitlines()[2]!r}"
    )


@pytest.mark.parametrize("bad", [0, 0.0, -0.0, -1, -500.0], ids=lambda v: repr(v))
def test_a_non_positive_map_entry_falls_through_to_the_estimate(tmp_path, monkeypatch, bad):
    """A weight <= 0 in the map must be DROPPED, never carried.

    Zero packs as free work; a NEGATIVE weight is worse than free — it REDUCES
    its shard's apparent load, so the packer keeps filling a shard that is
    already the heaviest. Either one breaks the invariant the rest of the module
    leans on, that every discovered file has a strictly positive weight.

    `measured` filters these out so the file falls through to the size estimate
    in `weights` and is weighted like any other unmeasured file. Nothing tested
    that: the committed map has no non-positive values, so a test that runs
    against the committed map cannot see the filter at all, and removing it left
    the whole suite green.
    """
    names = [f"test_known_{i}.py" for i in range(4)] + ["test_poisoned.py"]
    root, weights_path = _fake_checkout(
        tmp_path,
        names,
        seconds={f"tests/test_known_{i}.py": 10.0 for i in range(4)}
        | {"tests/test_poisoned.py": bad},
    )
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", weights_path)
    monkeypatch.setattr(shard_tests, "MIN_FILES", len(names))

    assert "tests/test_poisoned.py" not in shard_tests.measured()
    files, bins, weight = shard_tests.plan(root, 3)
    assert weight["tests/test_poisoned.py"] >= shard_tests.FLOOR_SECONDS > 0
    assert all(weight[f] > 0 for f in files)
    assert sum("tests/test_poisoned.py" in group for group in bins) == 1
    ok, report = shard_tests.check(root, 3)
    assert ok, "\n".join(report)
    # It is reported as estimated, because that is what it now is.
    assert "estimated: tests/test_poisoned.py" in "\n".join(report)


def test_a_map_that_shares_no_file_with_the_checkout_prints_no_minutes(tmp_path, monkeypatch):
    """A NON-EMPTY map can still be worth nothing, and must say so.

    `weights` derives its seconds-per-byte rate over the files the map and the
    checkout still share, and falls back to a rate of 1.0 when that intersection
    is empty — every estimated weight is then a raw BYTE COUNT. That happens for
    real when the tests directory moves or a batch of files is renamed. The
    report used to decide whether it had minutes to print by asking whether the
    MAP was non-empty, which is a different question: a 425-entry map of paths
    that no longer exist printed 16,800 bytes as "93.3 min".
    """
    names = [f"test_new_{i}.py" for i in range(6)]
    root, weights_path = _fake_checkout(
        tmp_path, names, seconds={f"tests/test_gone_{i}.py": 10.0 * (i + 1) for i in range(6)}
    )
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", weights_path)
    monkeypatch.setattr(shard_tests, "MIN_FILES", len(names))

    assert shard_tests.measured(), "the map itself is non-empty; that is the point"
    ok, report = shard_tests.check(root, 3)
    text = "\n".join(report)
    assert ok, text
    assert "shares no file with this checkout" in text, text
    for line in report[2:2 + 3]:
        assert " min" not in line, f"fabricated a minutes column: {line!r}"
        assert "% wt" in line, line


def test_the_predicted_shards_are_balanced_on_this_checkout():
    """The packer's own output, on the committed map. Not a CI measurement."""
    _, bins, weight = shard_tests.plan(REPO, shard_tests.SHARDS)
    loads = [sum(weight[f] for f in group) for group in bins]
    spread = (max(loads) - min(loads)) / (sum(loads) / len(loads))
    assert spread < 0.05, f"predicted minutes {[round(x / 60, 1) for x in loads]}"


#: The worst runner contention measured, not a margin picked to be comfortable:
#: shard 1 took 1423.41 s of pytest on run 36311316007 and 960.00 s on run
#: 36311424006 over the SAME 141 files, a factor of 1.48.
CONTENDED = 1.5

#: Measured fixed cost of a shard, in minutes: job setup (container, checkout,
#: setup-python, apt, pip, the two psql proofs) plus pytest's session start
#: before its first test. Job log start to the first `pytest -q` progress line,
#: over all SIX measured jobs: 85.2 / 92.3 / 88.2 s on run 36311424006 and
#: 109.8 / 97.2 / 83.0 s on run 36311316007 — 1.38 to 1.83 min, mean 1.54.
#: This is the WORST of the six, not the mean, because it is added to a ceiling
#: projection: a ceiling test that errs should err towards firing early, while
#: there is still room to add a shard.
FIXED_MINUTES = 1.8


def test_the_prediction_survives_the_worst_runner_measured(orchestrator_job):
    """A ceiling is not a balance, so the ceiling must never be the thing that
    notices the suite has grown.

    The check is the one the ceiling is actually for: the slowest shard this
    checkout predicts, run on a runner as contended as the worst one measured,
    must still finish inside `timeout-minutes`. Predicting 20 minutes is not
    enough on its own — the same files took 48% longer on one of the two runs
    the map was measured from.

    When this fails, the answer is another shard (SHARDS, and the matrix with
    it in the same commit) or a suite that got slower on purpose. It is never a
    larger `timeout-minutes`: raising the ceiling is what let one shard drift to
    28 minutes against a flat 21.8-minute prediction in the first place.
    """
    _, bins, weight = shard_tests.plan(REPO, shard_tests.SHARDS)
    worst = max(sum(weight[f] for f in group) for group in bins) / 60
    ceiling = float(orchestrator_job["timeout-minutes"])
    projected = worst * CONTENDED + FIXED_MINUTES
    assert projected <= ceiling, (
        f"the slowest shard is predicted at {worst:.1f} min of tests, which is "
        f"{projected:.1f} min of job on a runner {CONTENDED}x contended, against "
        f"a {ceiling:.0f} min ceiling. Add a shard; do not raise the ceiling."
    )


# ------------------------------------- the guard refuses a split with a hole
def test_a_file_that_no_shard_claims_is_refused(monkeypatch):
    """Remove one file from the assignment; the guard must go red and name it."""
    real = shard_tests.assign
    dropped: list[str] = []

    def drops_one(files, weight, shards):
        bins = real(files, weight, shards)
        dropped.append(bins[0].pop())
        return bins

    monkeypatch.setattr(shard_tests, "assign", drops_one)
    ok, report = shard_tests.check(REPO, shard_tests.SHARDS)
    assert not ok
    assert dropped and dropped[0] in "\n".join(report)
    assert "in NO shard" in "\n".join(report)


def test_a_file_in_two_shards_is_refused(monkeypatch):
    real = shard_tests.assign

    def duplicates_one(files, weight, shards):
        bins = real(files, weight, shards)
        bins[1].append(bins[0][0])
        return bins

    monkeypatch.setattr(shard_tests, "assign", duplicates_one)
    ok, report = shard_tests.check(REPO, shard_tests.SHARDS)
    assert not ok
    assert "MORE than one shard" in "\n".join(report)


def test_an_empty_shard_is_refused(monkeypatch):
    real = shard_tests.assign

    def empties_one(files, weight, shards):
        bins = real(files, weight, shards)
        bins[0].extend(bins[-1])
        bins[-1] = []
        return bins

    monkeypatch.setattr(shard_tests, "assign", empties_one)
    ok, report = shard_tests.check(REPO, shard_tests.SHARDS)
    assert not ok
    assert "would run no files at all" in "\n".join(report)


def test_an_empty_discovery_is_refused(monkeypatch):
    """Zero files satisfies "each file in exactly one shard" vacuously."""
    monkeypatch.setattr(shard_tests, "discover", lambda root: [])
    ok, report = shard_tests.check(REPO, shard_tests.SHARDS)
    assert not ok
    assert "below the floor" in "\n".join(report)


def test_a_path_no_shard_should_have_is_refused_not_a_traceback(monkeypatch, capsys):
    """The fourth refusal must be a MESSAGE, and it was unreachable.

    `check` has four refusals. Three of them print. The fourth — a path in the
    split that discovery never found — could not: `check` builds its report by
    calling `_table` BEFORE it formats its problems, and `_table` looked up
    `weight[f]` for every file in every group, so the invented path raised
    `KeyError` from inside report-building and the prepared message was dead
    code. The exit status was non-zero either way, so this was never a hole in
    CI; it was a traceback where a sentence naming the path belongs, and the
    person reading a red shard is the one who pays for that.

    It is unreachable through the real `assign`, which only ever redistributes
    the list it is handed — which is why it went unnoticed, and why it is
    reached here the same way the other three refusals are.
    """
    real = shard_tests.assign
    monkeypatch.setattr(
        shard_tests,
        "assign",
        lambda f, w, s: [
            g + ["tests/test_a_path_discovery_never_found.py"] if i == 0 else g
            for i, g in enumerate(real(f, w, s))
        ],
    )
    ok, report = shard_tests.check(REPO, shard_tests.SHARDS)
    assert not ok
    text = "\n".join(report)
    assert "path(s) that discovery did not find" in text, text
    assert "tests/test_a_path_discovery_never_found.py" in text, text
    # And through the CLI, which is what CI runs.
    assert shard_tests.main(["--check", "--of", str(shard_tests.SHARDS)]) == 1
    capsys.readouterr()


def test_a_brand_new_nested_test_file_is_discovered_and_cannot_be_dropped(tmp_path, monkeypatch):
    """The whole reason this script exists, exercised on a REAL file on disk.

    Not a monkeypatched list: a file created in a nested directory, the way a
    new test file actually arrives. It must be discovered at depth, weighted,
    placed in exactly one shard and passed by `--check`; and when the split then
    drops that specific file, `--check` must refuse and NAME it, because a file
    no shard claims leaves all three shards GREEN while its tests never run.
    """
    names = [f"test_known_{i}.py" for i in range(8)]
    root, weights_path = _fake_checkout(
        tmp_path, names, seconds={f"tests/test_known_{i}.py": 10.0 for i in range(8)}
    )
    nested = root / "orchestrator" / "tests" / "deeper" / "nested"
    nested.mkdir(parents=True)
    (nested / "thing_test.py").write_text("# probe\n" * 40, encoding="utf-8")
    newcomer = "tests/deeper/nested/thing_test.py"
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", weights_path)
    monkeypatch.setattr(shard_tests, "MIN_FILES", len(names) + 1)

    files, bins, weight = shard_tests.plan(root, 3)
    assert newcomer in files, f"a *_test.py at depth 3 was not discovered: {files}"
    assert weight[newcomer] > 0
    assert sum(newcomer in group for group in bins) == 1
    ok, report = shard_tests.check(root, 3)
    assert ok, "\n".join(report)
    assert f"estimated: {newcomer}" in "\n".join(report)

    # Now lose exactly that file in the split. The guard must go red and say so.
    real = shard_tests.assign
    monkeypatch.setattr(
        shard_tests,
        "assign",
        lambda f, w, s: [[x for x in g if x != newcomer] for g in real(f, w, s)],
    )
    ok, report = shard_tests.check(root, 3)
    text = "\n".join(report)
    assert not ok, text
    assert "are in NO shard" in text and newcomer in text, text


def test_the_cli_reports_a_hole_as_a_non_zero_exit(monkeypatch, capsys):
    real = shard_tests.assign
    monkeypatch.setattr(
        shard_tests,
        "assign",
        lambda f, w, s: [g[:-1] if i == 0 else g for i, g in enumerate(real(f, w, s))],
    )
    assert shard_tests.main(["--check", "--of", str(shard_tests.SHARDS)]) == 1
    capsys.readouterr()


def test_a_shard_count_the_script_does_not_know_is_refused(capsys):
    assert shard_tests.main(["--check", "--of", str(shard_tests.SHARDS + 1)]) == 2
    assert shard_tests.main(["--check", "--of", str(shard_tests.SHARDS - 1)]) == 2
    capsys.readouterr()


def test_a_shard_number_outside_the_range_is_refused(capsys):
    for bad in (0, shard_tests.SHARDS + 1):
        assert shard_tests.main(["--shard", str(bad), "--of", str(shard_tests.SHARDS)]) == 2
    capsys.readouterr()


# --------------------------------------- the workflow and the script agree
def test_the_matrix_is_exactly_one_to_SHARDS(orchestrator_job):
    """The matrix legs and shard_tests.SHARDS are one fact in two files."""
    legs = orchestrator_job["strategy"]["matrix"]["shard"]
    assert legs == list(range(1, shard_tests.SHARDS + 1)), (
        f"pipeline.yml expands {legs} but shard_tests.SHARDS is "
        f"{shard_tests.SHARDS}; some files would run in no job at all"
    )


def test_every_shard_step_passes_the_real_matrix_size(orchestrator_job):
    """`--of` must be `strategy.job-total`, never a literal.

    strategy.job-total is the size GitHub ACTUALLY expanded the matrix to, so
    it catches an edited matrix. A hardcoded 3 would agree with the script
    forever and prove nothing.
    """
    for step in orchestrator_job["steps"]:
        body = step.get("run") or ""
        if "shard_tests.py" not in body:
            continue
        assert "--of \"$SHARD_TOTAL\"" in body
        assert step["env"]["SHARD_TOTAL"] == "${{ strategy.job-total }}"


def test_the_guard_runs_before_anything_is_installed(orchestrator_job):
    """A broken split must be red in seconds, not after apt and pip."""
    names = [s.get("name") or s.get("uses") or "" for s in orchestrator_job["steps"]]
    bodies = [s.get("run") or "" for s in orchestrator_job["steps"]]
    guard = next(i for i, b in enumerate(bodies) if "--check" in b)
    installs = [
        i for i, (n, b) in enumerate(zip(names, bodies))
        if "apt-get" in b or "pip install" in b
    ]
    assert installs and guard < min(installs)
    suite = next(i for i, b in enumerate(bodies) if "-m pytest" in b)
    assert guard < suite


def test_ci_ok_still_requires_the_orchestrator_job(pipeline):
    """A matrix job rolls up to ONE result, and ci_gate.py accepts only
    `success` — so requiring `orchestrator` requires every shard. If the job
    ever stops being a dependency, the shards can fail without blocking."""
    gate = pipeline["jobs"]["ci-ok"]
    assert "orchestrator" in gate["needs"]
    run = "".join(s.get("run", "") for s in gate["steps"])
    required = run.split("--require ", 1)[1].split()[0].split(",")
    assert "orchestrator" in required


def test_the_shards_do_not_stop_at_the_first_failure(orchestrator_job):
    assert orchestrator_job["strategy"]["fail-fast"] is False


# ------------------------------------------- one database per shard, and safe
def test_each_shard_gets_its_own_database(orchestrator_job):
    total = len(orchestrator_job["strategy"]["matrix"]["shard"])
    names = {
        _render(orchestrator_job["env"]["SHARD_DB"], leg, total)
        for leg in orchestrator_job["strategy"]["matrix"]["shard"]
    }
    assert len(names) == total, f"shards would share a database: {sorted(names)}"


def test_every_shard_dsn_passes_conftests_own_guard(orchestrator_job):
    """conftest refuses a database name that is not unmistakably test-only.

    This is not theoretical: `techsara_orchestrator_test_1` — the obvious
    name — neither starts with `test_` nor ends with `_test`, so
    `_assert_safe_test_dsn` would raise before a single test ran. Run the real
    guard over the real rendered DSN for every leg.
    """
    guard = _conftest()._assert_safe_test_dsn
    total = len(orchestrator_job["strategy"]["matrix"]["shard"])
    step = next(s for s in orchestrator_job["steps"] if "-m pytest" in (s.get("run") or ""))
    for leg in orchestrator_job["strategy"]["matrix"]["shard"]:
        env = {"SHARD_DB": _render(orchestrator_job["env"]["SHARD_DB"], leg, total)}
        dsn = _render(step["env"]["TEST_DATABASE_URL"], leg, total, env)
        assert guard(dsn) == dsn
        assert env["SHARD_DB"] in dsn


def test_the_shard_database_is_created_with_productions_collation(orchestrator_job):
    """`CREATE DATABASE` inherits template1. Naming template0 and the locale
    explicitly is what keeps the shard databases on LC_COLLATE=C if the
    service's POSTGRES_INITDB_ARGS is ever edited; conftest re-reads
    datcollate and refuses anything else."""
    body = next(
        s["run"] for s in orchestrator_job["steps"]
        if "CREATE DATABASE" in (s.get("run") or "")
    )
    assert "TEMPLATE template0" in body
    assert "LC_COLLATE 'C'" in body and "LC_CTYPE 'C'" in body
    assert "ENCODING 'UTF8'" in body
    assert "$SHARD_DB" in body


def test_the_service_still_pins_the_c_locale(orchestrator_job):
    env = orchestrator_job["services"]["postgres"]["env"]
    assert env["POSTGRES_INITDB_ARGS"] == "--locale=C --encoding=UTF8"


# ----------------------------------------------- nothing else about the run
def test_the_pytest_options_are_unchanged(orchestrator_job):
    """Only the FILE SELECTION changed. -rs keeps every skip visible."""
    body = next(s["run"] for s in orchestrator_job["steps"] if "-m pytest" in (s.get("run") or ""))
    invocation = next(
        line.strip() for line in body.splitlines() if "-m pytest" in line
    )
    assert invocation == 'python -m pytest "${SHARD_FILES[@]}" -q -rs'


def test_an_empty_file_list_can_never_reach_pytest(orchestrator_job):
    """`pytest -q -rs` with no paths collects the whole repository from the
    rootdir and would read as a pass."""
    body = next(s["run"] for s in orchestrator_job["steps"] if "-m pytest" in (s.get("run") or ""))
    assert 'test "${#SHARD_FILES[@]}" -gt 0' in body
    assert re.search(r"set -euo pipefail", body)


def test_every_shard_proves_its_renderers_import(orchestrator_job):
    """The renderer import check stays in EVERY shard: each one builds its own
    environment from apt and pip, and the PDF and office tests are spread
    across all of them."""
    body = next(
        s["run"] for s in orchestrator_job["steps"]
        if "importlib" in (s.get("run") or "")
    )
    for lib in ("weasyprint", "pypdfium2", "docx", "pptx", "openpyxl", "matplotlib"):
        assert lib in body


def test_the_weight_map_is_advisory_only(monkeypatch):
    """A stale or missing timing map must change BALANCE, never coverage."""
    monkeypatch.setattr(shard_tests, "WEIGHTS_PATH", REPO / "does-not-exist.json")
    files, bins, _ = shard_tests.plan(REPO, shard_tests.SHARDS)
    union: set[str] = set()
    for group in bins:
        union |= set(group)
    assert union == set(files)
    ok, report = shard_tests.check(REPO, shard_tests.SHARDS)
    assert ok, "\n".join(report)
