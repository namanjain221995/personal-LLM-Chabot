"""A routine deploy may not change WHICH model the main engine serves.

2026-09-30, the swap to nvidia/Qwen3.8-27B-NVFP4. Every push to main runs
`TECHSARA_PRESERVE_MAIN_MODEL=1 ./techsara up` (scripts/deploy.sh), which
keeps a serving head. Its readiness probe asks the running engine for the NEW
MAIN_MODEL, so a changed model made a healthy engine look "not serving", the
flag was dropped and the routine deploy reloaded the pair -- with deploy.sh's
automatic rollback (a second reload, back onto the old model) armed behind it.
These tests hold the refusal that replaces that: before anything is
downloaded, written or restarted, and only when the model really changes.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from .support import REPO_ROOT
    from .test_profiles import nvidia
except ImportError:  # `unittest discover -s launcher/tests`
    from support import REPO_ROOT
    from test_profiles import nvidia

from techsara_cli import cli
from techsara_cli.compose import running_head_served_names, served_model_names
from techsara_cli.errors import TechSaraError
from techsara_cli.profiles import select_profile

#: The head's argv as `docker inspect ... {{json .Config.Cmd}}` printed it on
#: 2026-09-30 (the 35B), trimmed after the flags that matter here.
RUNNING_35B_ARGV = [
    "/models/repos/nvidia--Qwen3.6-35B-A3B-NVFP4--491c2f1ea524",
    "--served-model-name", "Qwen/Qwen3.6-35B-A3B-NVFP4",
    "--host", "0.0.0.0", "--port", "8000", "--max-model-len", "1000000",
]


def _docker(container_id: str = "abc123", argv: object = RUNNING_35B_ARGV, *, ps_rc: int = 0, inspect_rc: int = 0):
    """A fake runner answering `docker ps` and `docker inspect` like the daemon."""

    def run(command, **kwargs):
        run.calls.append(list(command))
        if command[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=ps_rc, stdout=f"{container_id}\n" if container_id else "", stderr="")
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=inspect_rc, stdout=json.dumps(argv) + "\n", stderr="")
        raise AssertionError(f"unexpected command {command}")

    run.calls = []
    return run


class ServedModelNamesTests(unittest.TestCase):
    def test_one_name(self) -> None:
        self.assertEqual(served_model_names(RUNNING_35B_ARGV), ("Qwen/Qwen3.6-35B-A3B-NVFP4",))

    def test_every_name_up_to_the_next_option_counts(self) -> None:
        argv = ["/m", "--served-model-name", "nvidia/Qwen3.8-27B-NVFP4", "Qwen/Qwen3.6-35B-A3B-NVFP4", "--host", "x"]
        self.assertEqual(
            served_model_names(argv), ("nvidia/Qwen3.8-27B-NVFP4", "Qwen/Qwen3.6-35B-A3B-NVFP4")
        )

    def test_the_equals_spelling_and_absence(self) -> None:
        self.assertEqual(served_model_names(["/m", "--served-model-name=a/b"]), ("a/b",))
        self.assertEqual(served_model_names(["/m", "--host", "0.0.0.0"]), ())
        self.assertEqual(served_model_names([]), ())


class RunningHeadServedNamesTests(unittest.TestCase):
    def test_read_off_the_running_head_of_this_project(self) -> None:
        runner = _docker()
        self.assertEqual(running_head_served_names(runner=runner), ("Qwen/Qwen3.6-35B-A3B-NVFP4",))
        listed, inspected = runner.calls
        self.assertIn("label=com.docker.compose.project=sf-local-ai", listed)
        self.assertIn("label=com.docker.compose.service=vllm", listed)
        self.assertEqual(inspected[:3], ["docker", "inspect", "abc123"])

    def test_no_head_or_no_docker_answer_is_no_names_never_a_change(self) -> None:
        self.assertEqual(running_head_served_names(runner=_docker(container_id="")), ())
        self.assertEqual(running_head_served_names(runner=_docker(ps_rc=1)), ())
        self.assertEqual(running_head_served_names(runner=_docker(inspect_rc=1)), ())
        self.assertEqual(running_head_served_names(runner=_docker(argv={"not": "a list"})), ())


class RoutineDeployGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        # The swap this guard was written for: the manifest names the 27B.
        self.assertEqual(self.profile.main_model.api_model_id, "nvidia/Qwen3.8-27B-NVFP4")

    def _guard(self, served: tuple[str, ...], preserve: str | None = "1") -> mock.MagicMock:
        environment = {} if preserve is None else {cli.PRESERVE_MAIN_MODEL_ENV: preserve}
        with (
            mock.patch.dict(os.environ, environment, clear=False),
            mock.patch.object(cli, "running_head_served_names", return_value=served) as reader,
        ):
            if preserve is None:
                os.environ.pop(cli.PRESERVE_MAIN_MODEL_ENV, None)
            cli._guard_routine_deploy_model_change(self.profile)
        return reader

    def test_a_routine_deploy_refuses_a_changed_model_and_names_both(self) -> None:
        with self.assertRaises(TechSaraError) as caught:
            self._guard(("Qwen/Qwen3.6-35B-A3B-NVFP4",))
        message = str(caught.exception)
        self.assertIn("from Qwen/Qwen3.6-35B-A3B-NVFP4 to nvidia/Qwen3.8-27B-NVFP4", message)
        self.assertIn("Nothing was downloaded, written or restarted", message)
        self.assertIn("./techsara up (without the flag)", message)
        self.assertIn("scripts/deploy.sh --full", message)

    def test_the_same_model_or_one_of_its_names_is_not_a_change(self) -> None:
        self._guard(("nvidia/Qwen3.8-27B-NVFP4",))
        self._guard(("nvidia/Qwen3.8-27B-NVFP4", "Qwen/Qwen3.6-35B-A3B-NVFP4"))
        self._guard(("Qwen/Qwen3.6-35B-A3B-NVFP4", "nvidia/Qwen3.8-27B-NVFP4"))

    def test_no_running_head_is_not_a_change(self) -> None:
        self._guard(())

    def test_an_explicit_up_never_asks_docker_at_all(self) -> None:
        for flag in (None, "", "0", "false"):
            with self.subTest(flag=flag):
                reader = self._guard(("Qwen/Qwen3.6-35B-A3B-NVFP4",), preserve=flag)
                reader.assert_not_called()


class UpRefusesBeforeTouchingAnythingTests(unittest.TestCase):
    """The refusal comes before the model download and before generated.env."""

    def test_nothing_is_ensured_or_written_when_a_routine_deploy_would_swap(self) -> None:
        profile = select_profile(nvidia(128, dgx=True), REPO_ROOT)
        manager = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = SimpleNamespace(create=mock.Mock(), locks_dir=root / "locks")
            with (
                mock.patch.dict(os.environ, {cli.PRESERVE_MAIN_MODEL_ENV: "1"}, clear=False),
                mock.patch.object(cli, "foreign_checkout_owner", return_value=None),
                mock.patch.object(cli.RuntimeLayout, "for_project", return_value=layout),
                mock.patch.object(cli, "FileLock", return_value=mock.MagicMock()),
                mock.patch.object(cli, "detect_hardware", return_value=nvidia(128, dgx=True)),
                mock.patch.object(cli, "_require_docker"),
                mock.patch.object(cli, "docker_project_has_running_models", return_value=True),
                mock.patch.object(cli, "select_profile", return_value=profile),
                mock.patch.object(
                    cli, "running_head_served_names", return_value=("Qwen/Qwen3.6-35B-A3B-NVFP4",)
                ),
                mock.patch.object(cli, "_model_manager", return_value=manager) as model_manager,
                mock.patch.object(cli, "_write_configuration") as write_configuration,
                mock.patch.object(cli, "_start_compose") as start_compose,
            ):
                args = argparse.Namespace(
                    dry_run=False, profile=None, model=None, skip_ocr=False, offline=False, verbose=False,
                )
                with self.assertRaisesRegex(TechSaraError, "a routine deploy never reloads the engine"):
                    cli._cmd_up(args, root=root)
        model_manager.assert_not_called()
        manager.ensure_all.assert_not_called()
        write_configuration.assert_not_called()
        start_compose.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
