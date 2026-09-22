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
import pathlib
import re
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "workflows" / "scripts" / "shard_tests.py"
PIPELINE = REPO / ".github" / "workflows" / "pipeline.yml"
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
