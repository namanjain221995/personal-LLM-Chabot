"""The three guards added after the 2026-09-15 twenty-minute degradation.

1. `techsara up` refuses to reconfigure a stack another checkout created.
2. The Alertmanager config renders with and without mail settings, and the
   SMTP password never leaves .runtime.
3. scripts/service-reconcile.sh starts stopped services, and never the main
   model, another Compose project, or a container on the skip list.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests`
    from support import REPO_ROOT

from techsara_cli.cli import _guard_foreign_checkout
from techsara_cli.compose import foreign_checkout_owner, running_project_checkouts
from techsara_cli.errors import TechSaraError


def _runner(stdout: str, returncode: int = 0):
    def run(argv, **kwargs):
        run.calls.append(list(argv))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    run.calls = []
    return run


class ForeignCheckoutGuardTests(unittest.TestCase):
    def test_the_owning_checkout_is_read_off_the_compose_label(self) -> None:
        runner = _runner("/srv/one\n/srv/one\n/srv/two\n")
        self.assertEqual(running_project_checkouts(runner=runner), ["/srv/one", "/srv/two"])
        argv = runner.calls[0]
        self.assertIn("label=com.docker.compose.project=sf-local-ai", argv)
        self.assertIn('{{.Label "com.docker.compose.project.working_dir"}}', argv)

    def test_no_owner_when_the_running_stack_is_this_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(foreign_checkout_owner(tmp, runner=_runner(f"{tmp}\n")))

    def test_no_owner_when_nothing_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(foreign_checkout_owner(tmp, runner=_runner("")))

    def test_a_second_checkout_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "other"
            other.mkdir()
            here = Path(tmp) / "here"
            here.mkdir()
            self.assertEqual(foreign_checkout_owner(here, runner=_runner(f"{other}\n")), str(other.resolve()))

    def test_a_docker_failure_is_not_an_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(foreign_checkout_owner(tmp, runner=_runner("/srv/other\n", returncode=1)))

    def test_up_refuses_and_names_both_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            here, other = Path(tmp) / "here", Path(tmp) / "other"
            here.mkdir(); other.mkdir()
            args = argparse.Namespace(force_checkout=False)
            with mock.patch("techsara_cli.cli.foreign_checkout_owner", return_value=str(other)):
                with self.assertRaises(TechSaraError) as caught:
                    _guard_foreign_checkout(args, root=here)
            message = str(caught.exception)
            self.assertIn(str(other), message)
            self.assertIn(str(here), message)
            self.assertIn("--force-checkout", message)

    def test_force_checkout_takes_the_stack_over(self) -> None:
        args = argparse.Namespace(force_checkout=True)
        with mock.patch("techsara_cli.cli.foreign_checkout_owner", return_value="/srv/other") as owner:
            _guard_foreign_checkout(args, root=Path("/srv/here"))
        owner.assert_not_called()

    def test_the_environment_switch_also_takes_it_over(self) -> None:
        args = argparse.Namespace(force_checkout=False)
        with mock.patch.dict(os.environ, {"TECHSARA_ALLOW_FOREIGN_CHECKOUT": "1"}):
            with mock.patch("techsara_cli.cli.foreign_checkout_owner", return_value="/srv/other"):
                _guard_foreign_checkout(args, root=Path("/srv/here"))


class AlertmanagerConfigTests(unittest.TestCase):
    """monitoring/alertmanager/render_config.py"""

    @staticmethod
    def _render(env: dict) -> str:
        import importlib.util

        path = REPO_ROOT / "monitoring" / "alertmanager" / "render_config.py"
        spec = importlib.util.spec_from_file_location("render_config", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module.render(env)

    def test_without_mail_settings_the_config_is_still_valid_and_silent(self) -> None:
        text = self._render({})
        data = _yaml(text)
        self.assertEqual(data["route"]["receiver"], "null")
        self.assertEqual([r["name"] for r in data["receivers"]], ["null"])
        self.assertNotIn("smtp_smarthost", text)

    def test_mail_settings_produce_an_email_receiver(self) -> None:
        data = _yaml(self._render({
            "ALERT_EMAIL_TO": "ops@example.com",
            "ALERT_SMTP_HOST": "smtp.example.com",
            "ALERT_SMTP_PORT": "587",
            "ALERT_SMTP_USER": "bot@example.com",
            "ALERT_SMTP_PASSWORD": "secret",
        }))
        self.assertEqual(data["route"]["receiver"], "email")
        email = [r for r in data["receivers"] if r["name"] == "email"][0]
        self.assertEqual(email["email_configs"][0]["to"], "ops@example.com")
        self.assertTrue(email["email_configs"][0]["send_resolved"])
        self.assertEqual(data["global"]["smtp_smarthost"], "smtp.example.com:587")
        self.assertIs(data["global"]["smtp_require_tls"], True)

    def test_several_recipients_become_one_comma_separated_string(self) -> None:
        data = _yaml(self._render({
            "ALERT_EMAIL_TO": "a@example.com, b@example.com",
            "ALERT_SMTP_HOST": "smtp.example.com",
        }))
        email = [r for r in data["receivers"] if r["name"] == "email"][0]
        self.assertEqual(email["email_configs"][0]["to"], "a@example.com, b@example.com")

    def test_implicit_tls_port_does_not_ask_for_starttls(self) -> None:
        data = _yaml(self._render({
            "ALERT_EMAIL_TO": "ops@example.com",
            "ALERT_SMTP_HOST": "smtp.example.com",
            "ALERT_SMTP_PORT": "465",
        }))
        self.assertIs(data["global"]["smtp_require_tls"], False)

    def test_a_quote_in_the_password_cannot_break_out_of_the_scalar(self) -> None:
        data = _yaml(self._render({
            "ALERT_EMAIL_TO": "ops@example.com",
            "ALERT_SMTP_HOST": "smtp.example.com",
            "ALERT_SMTP_PASSWORD": "it's a 'quoted' one",
        }))
        self.assertEqual(data["global"]["smtp_auth_password"], "it's a 'quoted' one")

    def test_the_generated_path_is_inside_the_ignored_runtime_directory(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".runtime", gitignore)
        compose = (REPO_ROOT / "compose" / "compose.monitoring.yaml").read_text(encoding="utf-8")
        self.assertIn("./.runtime/alertmanager:/etc/alertmanager:ro", compose)


class PrometheusAlertingTests(unittest.TestCase):
    def test_prometheus_sends_its_alerts_to_alertmanager(self) -> None:
        data = _yaml((REPO_ROOT / "monitoring" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
        targets = data["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
        self.assertEqual(targets, ["alertmanager:9093"])

    def test_the_store_size_gauge_uses_delta_not_increase(self) -> None:
        rules = (REPO_ROOT / "monitoring" / "prometheus" / "rules" / "alerts.yml").read_text(encoding="utf-8")
        self.assertIn("delta(techsara_store_total_bytes[24h])", rules)
        self.assertNotIn("increase(techsara_store_total_bytes", rules)


class ServiceReconcileScriptTests(unittest.TestCase):
    """scripts/service-reconcile.sh, driven against a fake `docker`."""

    SCRIPT = REPO_ROOT / "scripts" / "service-reconcile.sh"

    def _run(self, containers: str, *, skip: str = "") -> tuple[str, list[str]]:
        """(stdout, names the script started) for a given `docker ps` output."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "service-reconcile.sh").write_text(
                self.SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            (root / "scripts" / "service-reconcile.sh").chmod(0o755)
            (root / ".runtime").mkdir()
            if skip:
                (root / ".runtime" / "reconcile-skip").write_text(skip, encoding="utf-8")
            started = Path(tmp) / "started"
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            (fake_bin / "docker").write_text(
                "#!/usr/bin/env bash\n"
                "case \"$1 $2\" in\n"
                "  'ps -a')\n"
                f"    printf '%s' {shell_quote(containers)}\n"
                "    ;;\n"
                "  'inspect -f')\n"
                "    echo unless-stopped\n"
                "    ;;\n"
                "  'start '*|'start')\n"
                f"    echo \"$2\" >> {started}\n"
                "    ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            (fake_bin / "docker").chmod(0o755)
            env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
            result = subprocess.run(
                ["bash", str(root / "scripts" / "service-reconcile.sh"), "once"],
                capture_output=True, text=True, env=env, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            names = started.read_text(encoding="utf-8").split() if started.exists() else []
            return result.stdout, names

    def test_a_stopped_service_is_started(self) -> None:
        out, started = self._run("sf-local-ai-searxng-1\texited\n")
        self.assertEqual(started, ["sf-local-ai-searxng-1"])
        self.assertIn("started sf-local-ai-searxng-1", out)

    def test_a_running_service_is_left_alone(self) -> None:
        _, started = self._run("sf-local-ai-searxng-1\trunning\n")
        self.assertEqual(started, [])

    def test_the_main_model_is_never_started_from_here(self) -> None:
        out, started = self._run("sf-local-ai-vllm-1\texited\n")
        self.assertEqual(started, [])
        self.assertIn("skip sf-local-ai-vllm-1", out)

    def test_the_skip_list_is_honoured(self) -> None:
        out, started = self._run(
            "sf-local-ai-pgadmin-1\texited\n", skip="# a note\nsf-local-ai-pgadmin-1\n")
        self.assertEqual(started, [])
        self.assertIn("reconcile-skip", out)

    def test_it_only_looks_at_this_projects_containers(self) -> None:
        script = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn('label=com.docker.compose.project=$PROJECT', script)
        # The worker's rank-1 project and the e2e stack are separate projects,
        # so the filter alone keeps this script away from them.
        self.assertNotIn("sf-local-ai-worker", script.replace("sf-local-ai-worker (tensor-parallel rank 1)", ""))


def shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _yaml(text: str):
    import yaml

    return yaml.safe_load(text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
