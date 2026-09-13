"""What unittest_gate.py must refuse, and what it must let through.

Every case below is a shape the launcher job has to survive on BOTH matrix
legs. Plain `unittest discover` passes two of them on Python 3.11.
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import unittest_gate  # noqa: E402


_PASSING = """
import unittest


class {cls}(unittest.TestCase):
{cases}
"""


def _write_suite(root: pathlib.Path, module: str, how_many: int, failing: int = 0) -> None:
    cases = []
    for i in range(how_many):
        cases.append(f"    def test_case_{i}(self):\n        self.assertTrue(True)\n")
    for i in range(failing):
        cases.append(f"    def test_broken_{i}(self):\n        self.fail('deliberate')\n")
    (root / f"test_{module}.py").write_text(
        _PASSING.format(cls=f"Suite{module.title()}", cases="".join(cases)),
        encoding="utf-8",
    )


@contextlib.contextmanager
def _quiet():
    """Swallow the runner's output; the tests assert on exit codes."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class TheGateRefusesASuiteThatIsNotThere(unittest.TestCase):
    def test_a_directory_with_no_tests_is_refused_although_unittest_would_report_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet() as (_, err):
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "1"])
            self.assertEqual(rc, unittest_gate.EXIT_FAIL)
            self.assertIn("only 0 test(s) were collected", err.getvalue())

    def test_a_suite_that_has_shrunk_below_its_floor_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_suite(root, "small", how_many=3)
            with _quiet() as (_, err):
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "10"])
            self.assertEqual(rc, unittest_gate.EXIT_FAIL)
            self.assertIn("at least 10", err.getvalue())

    def test_a_module_that_cannot_be_imported_is_named_rather_than_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_suite(root, "healthy", how_many=2)
            (root / "test_broken_import.py").write_text(
                "import a_module_that_does_not_exist_anywhere  # noqa: F401\n",
                encoding="utf-8",
            )
            with _quiet() as (_, err):
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "1"])
            self.assertEqual(rc, unittest_gate.EXIT_FAIL)
            message = err.getvalue()
            self.assertIn("could not be imported", message)
            self.assertIn("test_broken_import", message)
            # The floor is not what went wrong, so it must not be what is said.
            self.assertNotIn("were collected under", message)


class TheFloorCountsTestsThatRanNotTestsThatWereCollected(unittest.TestCase):
    """2026-09-13 CI review: the floor was checked against countTestCases().

    Every case below cleared that floor and printed `OK (skipped=N)` in green
    before the gate counted executed tests; each one is refused now.
    """

    def test_a_module_that_skips_itself_at_import_does_not_count_towards_the_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "test_needs_a_tool.py").write_text(
                "import unittest\n"
                "raise unittest.SkipTest('the tool this suite needs is not installed')\n"
                "class Never(unittest.TestCase):\n"
                + "".join(f"    def test_{i}(self):\n        pass\n" for i in range(5)),
                encoding="utf-8",
            )
            # Module names are unique per test: discovery imports each file
            # under its bare name, and a second `test_real` from another
            # temporary directory is refused as "incorrectly imported".
            _write_suite(root, "importskipneighbour", how_many=2)
            with _quiet() as (out, err):
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "3"])
            self.assertEqual(rc, unittest_gate.EXIT_FAIL, out.getvalue() + err.getvalue())
            self.assertIn("only 2 test(s) actually executed", err.getvalue())

    def test_a_skipped_class_keeps_its_methods_in_the_collected_count_but_not_in_the_executed_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "test_switched_off.py").write_text(
                "import unittest\n"
                "@unittest.skip('switched off')\n"
                "class Off(unittest.TestCase):\n"
                + "".join(f"    def test_{i}(self):\n        pass\n" for i in range(10)),
                encoding="utf-8",
            )
            with _quiet() as (out, err):
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "5"])
            self.assertEqual(rc, unittest_gate.EXIT_FAIL)
            self.assertIn("collected 10 test(s)", out.getvalue())
            self.assertIn("only 0 test(s) actually executed", err.getvalue())

    def test_a_set_up_class_skip_is_not_counted_as_executed_either(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "test_class_setup_skip.py").write_text(
                "import unittest\n"
                "class NeedsDocker(unittest.TestCase):\n"
                "    @classmethod\n"
                "    def setUpClass(cls):\n"
                "        raise unittest.SkipTest('no docker')\n"
                + "".join(f"    def test_{i}(self):\n        pass\n" for i in range(4)),
                encoding="utf-8",
            )
            _write_suite(root, "classskipneighbour", how_many=3)
            with _quiet() as (_, err):
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "4"])
            self.assertEqual(rc, unittest_gate.EXIT_FAIL)
            self.assertIn("only 3 test(s) actually executed", err.getvalue())

    def test_a_few_skips_inside_a_suite_that_still_executes_its_floor_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _write_suite(root, "mostlygreen", how_many=6)
            (root / "test_one_skip.py").write_text(
                "import unittest\n"
                "class Some(unittest.TestCase):\n"
                "    @unittest.skip('not on this platform')\n"
                "    def test_skipped(self):\n        pass\n",
                encoding="utf-8",
            )
            with _quiet() as (out, _):
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "6"])
            self.assertEqual(rc, unittest_gate.EXIT_OK)
            self.assertIn("6 test(s) executed, 1 skipped, of 7 collected", out.getvalue())


class TheGateStillRunsTheTests(unittest.TestCase):
    def test_a_suite_above_its_floor_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_suite(pathlib.Path(tmp), "green", how_many=6)
            with _quiet():
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "5"])
            self.assertEqual(rc, unittest_gate.EXIT_OK)

    def test_a_failing_test_inside_a_large_enough_suite_still_fails_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_suite(pathlib.Path(tmp), "red", how_many=5, failing=1)
            with _quiet():
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "5"])
            self.assertEqual(rc, unittest_gate.EXIT_FAIL)


class TheFloorItselfIsChecked(unittest.TestCase):
    def test_a_floor_of_zero_is_a_usage_error_because_it_would_assert_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _quiet() as (_, err):
                rc = unittest_gate.main(["--start", tmp, "--min-tests", "0"])
            self.assertEqual(rc, unittest_gate.EXIT_USAGE)
            self.assertIn("must be at least 1", err.getvalue())

    def test_a_start_directory_that_does_not_exist_is_a_usage_error(self):
        with _quiet() as (_, err):
            rc = unittest_gate.main(
                ["--start", "/nowhere/at/all/please", "--min-tests", "1"]
            )
        self.assertEqual(rc, unittest_gate.EXIT_USAGE)
        self.assertIn("cannot discover tests", err.getvalue())


if __name__ == "__main__":
    unittest.main()
