"""Env files are data, not shell scripts.

`set -a && . ./.env` is the trap this module exists to document. It is not a
dotenv parser, it is an *interpreter*, so a value holding a space, a
parenthesis or a quote stops being a value and starts being code. The damage is
quiet: bash reports `line N: <word>: command not found` and keeps going, so a
script that loads its configuration this way runs on a partial environment and
blames whatever fails next.

That is not hypothetical. It cost an engineer on this repository a day: they
configured a test run by sourcing `.env`, got five failures, re-ran against a
pristine `git archive HEAD` export, saw the same five, and concluded the
application was broken. The control experiment varied the code and held the
real cause -- the loader -- constant.

Every value below is a placeholder. No real credential appears in this file or
in `fixtures/env_shapes.env`, and the assertions never print a parsed value
from a real env file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from techsara_cli.utils import (  # noqa: E402
    check_env_file,
    parse_env_file,
    quote_env_value,
    render_env,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "env_shapes.env"

# What the fixture means. This is the contract, and it is the contract Docker
# Compose implements -- test_compose_agrees_with_the_parser proves the two
# agree rather than taking this table on faith.
EXPECTED = {
    "BOM_FIRST_KEY": "first",                                   # UTF-8 BOM must not glue to the key
    "BARE_WITH_SPACE": "two words",                             # the .env line 251 shape
    "WITH_PARENS": "node two (auto-detected)",                  # the generated.env line 169 shape
    "HASH_INSIDE": "value#tight",                               # '#' mid-word is not a comment
    "HASH_AFTER_SPACE": "value",                                # ' #' is a comment
    "EMPTY_VALUE": "",
    "DQUOTED": "two words",
    "SQUOTED": "two words",
    "EQUALS_INSIDE": "a=b=c",
    "EXPORTED_KEY": "exported value",                           # `export FOO=bar`
    "DQUOTED_THEN_COMMENT": "quoted",
    "JSON_VALUE": '{"method":"mtp","num_speculative_tokens":1}',  # the .env.example line 214 shape
    "APOSTROPHE_INSIDE": "it's quoted oddly",
    "CRLF_ONE": "crlf one",                                     # CRLF line endings
    "CRLF_TWO": "crlf two",
    "SENTINEL_LAST": "reached-the-end",                         # proves nothing aborted early
}


def _bash(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
    )


class FixtureIntegrityTests(unittest.TestCase):
    """The fixture is byte-sensitive; a normalising checkout would gut it."""

    def test_fixture_keeps_its_bom_and_crlf_bytes(self) -> None:
        raw = FIXTURE.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "fixture lost its UTF-8 BOM")
        self.assertEqual(raw.count(b"\r\n"), 2, "fixture lost its CRLF lines")


class ShellSourcingIsTheDefectTests(unittest.TestCase):
    """Demonstrate the failure mode, so the fix has something to be measured against."""

    def test_a_parenthesis_makes_the_whole_file_unloadable(self) -> None:
        # This is `.runtime/generated.env` line 169: a syntax error, not a
        # runtime error, so the shell abandons the file at that point.
        result = _bash(f"bash -n {FIXTURE}", FIXTURE.parent)
        self.assertNotEqual(result.returncode, 0, "expected bash to reject the fixture")
        self.assertIn("syntax error", result.stderr)

    def test_sourcing_silently_drops_and_corrupts_values(self) -> None:
        # Drop the two lines that abort the parse outright, so what is left is
        # the *quiet* damage -- the part that produces false conclusions.
        with tempfile.TemporaryDirectory() as tmp:
            survivable = Path(tmp) / "survivable.env"
            kept = [
                line
                for line in FIXTURE.read_text(encoding="utf-8-sig").splitlines()
                if not line.startswith(("WITH_PARENS=", "APOSTROPHE_INSIDE="))
            ]
            survivable.write_text("\n".join(kept) + "\n", encoding="utf-8")

            result = _bash(
                "set -a; . ./survivable.env; set +a; "
                'printf "BARE=[%s]\\n" "${BARE_WITH_SPACE-<UNSET>}"; '
                'printf "JSON=[%s]\\n" "${JSON_VALUE-<UNSET>}"; '
                'printf "EXPORTED=[%s]\\n" "${EXPORTED_KEY-<UNSET>}"',
                Path(tmp),
            )

            # 1. The space-bearing value is not merely wrong, it is GONE, and
            #    bash announced it on stderr that nobody reads.
            self.assertIn("BARE=[<UNSET>]", result.stdout)
            self.assertIn("command not found", result.stderr)

            # 2. Worse: the JSON value loads with NO error at all, quietly
            #    stripped of the quotes that made it valid JSON.
            self.assertIn("JSON=[{method:mtp,num_speculative_tokens:1}]", result.stdout)

            # 3. And the shell's exit status is a clean 0. Nothing downstream
            #    has any way to know the environment is half-built.
            self.assertEqual(result.returncode, 0)

    def test_errexit_turns_one_bad_line_into_total_silence(self) -> None:
        # Any script with `set -euo pipefail` -- which is most of them -- stops
        # dead at the first offending line, so every assignment after it is
        # missing. In the real `.env` that is 21 further keys, UPLOAD_MAX_MB
        # among them.
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "e.env"
            env_file.write_text("EARLY=fine\nBROKEN=two words\nLATE=never-loaded\n", encoding="utf-8")
            result = _bash(
                'set -euo pipefail; set -a; . ./e.env; set +a; echo "LATE=[${LATE-<UNSET>}]"',
                Path(tmp),
            )
            self.assertEqual(result.returncode, 127)
            self.assertNotIn("LATE=", result.stdout)


class CanonicalParserTests(unittest.TestCase):
    """...and the fix: read the file as data."""

    def test_parser_reads_every_shape_correctly(self) -> None:
        self.assertEqual(parse_env_file(FIXTURE), EXPECTED)

    def test_parser_never_executes_anything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "must-not-exist"
            env_file = Path(tmp) / "e.env"
            env_file.write_text(
                f"A=$(touch {marker})\nB=`touch {marker}`\nC=${{HOME}}\n", encoding="utf-8"
            )
            values = parse_env_file(env_file)
            self.assertFalse(marker.exists())
            self.assertEqual(values["A"], f"$(touch {marker})")
            self.assertEqual(values["C"], "${HOME}")

    def test_missing_file_is_empty_not_an_error(self) -> None:
        self.assertEqual(parse_env_file(Path("/nonexistent/nope.env")), {})


class CheckerTests(unittest.TestCase):
    """The guard that stops this shape from being reintroduced."""

    def test_checker_flags_the_shell_hostile_lines_and_only_those(self) -> None:
        flagged = {key for _, key, _ in check_env_file(FIXTURE)}
        for key in ("BARE_WITH_SPACE", "WITH_PARENS", "JSON_VALUE", "APOSTROPHE_INSIDE"):
            self.assertIn(key, flagged)
        # A '#' inside a word is safe in a shell; flagging it would be noise.
        self.assertNotIn("HASH_INSIDE", flagged)
        for key in ("DQUOTED", "SQUOTED", "EQUALS_INSIDE", "EMPTY_VALUE", "SENTINEL_LAST"):
            self.assertNotIn(key, flagged)

    def test_checker_reports_the_bom(self) -> None:
        reasons = [reason for _, key, reason in check_env_file(FIXTURE) if key == "<file>"]
        self.assertTrue(any("BOM" in reason for reason in reasons))

    def test_checker_never_returns_a_value(self) -> None:
        # The checker runs over files full of credentials, so its output must
        # be safe to print in CI. Nothing it returns may contain a value.
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "e.env"
            env_file.write_text("SECRET_SHAPED=hunter two (x)\n", encoding="utf-8")
            report = check_env_file(env_file)
            self.assertEqual(len(report), 1)
            blob = " ".join(f"{n} {k} {r}" for n, k, r in report)
            self.assertNotIn("hunter", blob)
            self.assertIn("SECRET_SHAPED", blob)

    def test_checker_catches_an_unterminated_quote(self) -> None:
        # Compose refuses to read the whole file in this case.
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "e.env"
            env_file.write_text('K="unterminated\n', encoding="utf-8")
            self.assertTrue(any("unterminated" in r for _, _, r in check_env_file(env_file)))

    def test_the_repository_template_is_clean(self) -> None:
        # .env.example is copied verbatim by every new install. If it carries a
        # shell-hostile line, every install inherits the trap.
        example = Path(__file__).resolve().parents[2] / ".env.example"
        if not example.exists():
            self.skipTest(".env.example not present")
        problems = check_env_file(example)
        self.assertEqual(
            problems, [], f".env.example has shell-hostile lines: {[(n, k) for n, k, _ in problems]}"
        )


class QuotingRoundTripTests(unittest.TestCase):
    """Quoting must preserve the value exactly, or it is not an option."""

    HARD_VALUES = [
        "two words",
        "node two (auto-detected)",
        '{"method":"mtp","num_speculative_tokens":1}',
        "it's quoted oddly",
        "--flag '{\"nested\":\"json\"}' --other",
        "value # not a comment",
        "trailing space ",
        "$NOT_A_VARIABLE",
        "back\\slash",
        "",
        "plain",
    ]

    def test_quoting_round_trips_through_the_parser(self) -> None:
        for value in self.HARD_VALUES:
            with self.subTest(shape=repr(value)):
                with tempfile.TemporaryDirectory() as tmp:
                    env_file = Path(tmp) / "e.env"
                    env_file.write_text(f"K={quote_env_value(value)}\n", encoding="utf-8")
                    self.assertEqual(parse_env_file(env_file)["K"], value)

    def test_quoted_output_is_also_safe_to_source(self) -> None:
        for value in self.HARD_VALUES:
            with self.subTest(shape=repr(value)):
                with tempfile.TemporaryDirectory() as tmp:
                    env_file = Path(tmp) / "e.env"
                    env_file.write_text(f"K={quote_env_value(value)}\n", encoding="utf-8")
                    result = _bash('set -a; . ./e.env; set +a; printf "[%s]" "$K"', Path(tmp))
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(result.stdout, f"[{value}]")

    def test_render_env_emits_nothing_a_shell_would_mis_parse(self) -> None:
        values = {f"K{i}": v for i, v in enumerate(self.HARD_VALUES)}
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "generated.env"
            env_file.write_text(render_env(values), encoding="utf-8")
            self.assertEqual(check_env_file(env_file), [])
            self.assertEqual(parse_env_file(env_file), values)

    def test_render_env_keeps_simple_values_unquoted(self) -> None:
        # Churn in generated.env is not free; only quote what needs it.
        self.assertEqual(render_env({"ALPHA": "one", "ZED": 2}), "ALPHA=one\nZED=2\n")


class ShippedToolingTests(unittest.TestCase):
    """The supported alternative has to actually work, or people go back to `.`."""

    REPO = Path(__file__).resolve().parents[2]

    def test_env_load_sh_reads_what_source_cannot(self) -> None:
        loader = self.REPO / "scripts" / "lib" / "env-load.sh"
        self.assertTrue(loader.is_file(), "scripts/lib/env-load.sh is missing")
        script = (
            "set -euo pipefail\n"
            f". ./scripts/lib/env-load.sh\n"
            f"load_env_file {FIXTURE}\n"
            + "".join(
                f'printf "{k}=[%s]\\n" "${{{k}-<UNSET>}}"\n' for k in EXPECTED
            )
        )
        result = _bash(script, self.REPO)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        for key, value in EXPECTED.items():
            with self.subTest(key=key):
                self.assertIn(f"{key}=[{value}]", result.stdout)

    def test_checker_script_fails_on_a_hostile_file_and_passes_a_clean_one(self) -> None:
        checker = self.REPO / "scripts" / "check-env-files.sh"
        self.assertTrue(checker.is_file(), "scripts/check-env-files.sh is missing")
        self.assertTrue(os.access(checker, os.X_OK), "checker is not executable")

        bad = _bash(f"./scripts/check-env-files.sh {FIXTURE}", self.REPO)
        self.assertEqual(bad.returncode, 1, "checker passed a deliberately hostile file")
        self.assertIn("WITH_PARENS", bad.stdout)
        # It reports on files full of credentials: keys yes, values never.
        self.assertNotIn("auto-detected", bad.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / "clean.env"
            clean.write_text("A=plain\nB='two words'\n", encoding="utf-8")
            good = _bash(f"./scripts/check-env-files.sh {clean}", self.REPO)
            self.assertEqual(good.returncode, 0, good.stdout + good.stderr)

    def test_the_repositorys_own_env_files_are_clean(self) -> None:
        # The regression guard proper. Runs against whatever is on disk; it
        # asserts on counts and keys, never on a value.
        result = _bash("./scripts/check-env-files.sh", self.REPO)
        self.assertEqual(
            result.returncode, 0, f"env files regressed:\n{result.stdout}\n{result.stderr}"
        )


@unittest.skipUnless(shutil.which("docker"), "docker not available")
class ComposeParityTests(unittest.TestCase):
    """Compose is the authority: it decides what the containers actually get.

    `docker compose config` is read-only -- it renders the merged model and
    touches no container.
    """

    COMPOSE = """services:
  probe:
    image: alpine
    environment:
{lines}
"""

    @classmethod
    def setUpClass(cls) -> None:
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=60
        )
        if probe.returncode != 0:
            raise unittest.SkipTest("docker compose unavailable")

    def test_compose_agrees_with_the_parser(self) -> None:
        import json

        keys = [k for k in EXPECTED if k != "EMPTY_VALUE"]
        lines = "\n".join(f'      {k}: "${{{k}?missing}}"' for k in keys)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "compose.yaml").write_text(self.COMPOSE.format(lines=lines), encoding="utf-8")
            result = subprocess.run(
                ["docker", "compose", "--env-file", str(FIXTURE), "config", "--format", "json"],
                cwd=str(work),
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            got = json.loads(result.stdout)["services"]["probe"]["environment"]
            for key in keys:
                with self.subTest(key=key):
                    self.assertEqual(got[key], EXPECTED[key])


if __name__ == "__main__":
    unittest.main()
