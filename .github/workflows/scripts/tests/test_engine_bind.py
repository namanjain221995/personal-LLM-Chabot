"""The verify job's engine-bind gate: probe where the engine was told to listen,
and fail when it listens on every interface (audit F065, CI half, 2026-09-13).

Before this gate the verify job curled 127.0.0.1:8000 and nothing else, so a
wildcard bind was invisible to it — and once the launcher moved the engine to
the docker bridge gateway, that loopback probe would have failed on a
CORRECTLY bound engine. Both halves are pinned here against `ss -ltnH` output
captured in the shapes the production head produces.
"""
from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import engine_bind  # noqa: E402

#: `ss -ltnH 'sport = :8000'` on the production head on 2026-09-13, before OA-3.
SS_WILDCARD = "LISTEN 0      2048   0.0.0.0:8000 0.0.0.0:*\n"
SS_BRIDGE = "LISTEN 0      2048   172.17.0.1:8000 0.0.0.0:*\n"
SS_IPV6_WILDCARD = "LISTEN 0      4096   [::]:8000 [::]:*\n"
SS_STAR = "LISTEN 0      4096   *:8000 *:*\n"
SS_LOOPBACK = "LISTEN 0 4096 127.0.0.1:8000 0.0.0.0:* users:((\"docker-proxy\",pid=1,fd=4))\n"
SS_LAN = "LISTEN 0 2048 192.168.1.40:8000 0.0.0.0:*\n"
SS_OTHER_PORTS = "LISTEN 0 4096 0.0.0.0:9100 0.0.0.0:*\nLISTEN 0 4096 0.0.0.0:18000 0.0.0.0:*\n"


def _run(env_text: str | None, *argv: str, ss: str | None = None) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        env_path = root / "generated.env"
        if env_text is not None:
            env_path.write_text(env_text, encoding="utf-8")
        args = [argv[0], "--generated-env", str(env_path), *argv[1:]]
        if ss is not None:
            (root / "ss.txt").write_text(ss, encoding="utf-8")
            args += ["--ss-output", str(root / "ss.txt")]
        if argv[0] == "resolve":
            args += ["--github-output", str(root / "out")]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = engine_bind.main(args)
        if argv[0] == "resolve":
            out.write((root / "out").read_text(encoding="utf-8"))
        return rc, out.getvalue(), err.getvalue()


class TheProbeGoesWhereTheEngineWasToldToListen(unittest.TestCase):
    def test_the_bridge_gateway_bind_is_probed_at_the_bridge_gateway_not_loopback(self):
        rc, out, _ = _run("CLUSTER_API_BIND_ADDRESS=172.17.0.1\nVLLM_PORT=8000\n", "resolve")
        self.assertEqual(rc, 0)
        self.assertIn("url=http://172.17.0.1:8000", out)

    def test_a_missing_key_falls_back_to_loopback_and_the_default_port(self):
        rc, out, _ = _run("MAIN_MODEL=x\n", "resolve")
        self.assertEqual(rc, 0)
        self.assertIn("url=http://127.0.0.1:8000", out)

    def test_a_missing_generated_env_falls_back_to_loopback(self):
        rc, out, _ = _run(None, "resolve")
        self.assertEqual(rc, 0)
        self.assertIn("url=http://127.0.0.1:8000", out)

    def test_the_configured_port_is_used(self):
        rc, out, _ = _run('CLUSTER_API_BIND_ADDRESS="172.17.0.1"\nVLLM_PORT=8011\n', "resolve")
        self.assertIn("url=http://172.17.0.1:8011", out)

    def test_a_wildcard_configuration_is_probed_on_loopback_so_the_evidence_is_still_collected(self):
        rc, out, _ = _run("CLUSTER_API_BIND_ADDRESS=0.0.0.0\n", "resolve")
        self.assertEqual(rc, 0)
        self.assertIn("url=http://127.0.0.1:8000", out)


class AWildcardBindFailsTheVerifyGate(unittest.TestCase):
    def test_the_engine_on_every_ipv4_interface_is_refused(self):
        rc, _, err = _run("CLUSTER_API_BIND_ADDRESS=172.17.0.1\n", "check", ss=SS_WILDCARD)
        self.assertEqual(rc, 1)
        self.assertIn("0.0.0.0:8000", err)
        self.assertIn("every interface", err)

    def test_the_engine_on_every_ipv6_interface_is_refused(self):
        rc, _, _ = _run("CLUSTER_API_BIND_ADDRESS=172.17.0.1\n", "check", ss=SS_IPV6_WILDCARD)
        self.assertEqual(rc, 1)

    def test_a_star_listener_is_refused(self):
        rc, _, _ = _run("CLUSTER_API_BIND_ADDRESS=172.17.0.1\n", "check", ss=SS_STAR)
        self.assertEqual(rc, 1)

    def test_a_wildcard_in_the_configuration_is_refused_even_before_the_engine_is_recreated(self):
        rc, _, err = _run("CLUSTER_API_BIND_ADDRESS=0.0.0.0\n", "check", ss=SS_BRIDGE)
        self.assertEqual(rc, 1)
        self.assertIn("CLUSTER_API_BIND_ADDRESS=0.0.0.0", err)

    def test_the_production_state_before_oa3_is_red(self):
        rc, _, _ = _run("CLUSTER_API_BIND_ADDRESS=0.0.0.0\nVLLM_PORT=8000\n", "check", ss=SS_WILDCARD)
        self.assertEqual(rc, 1)

    def test_a_lan_address_that_is_not_the_configured_one_is_refused(self):
        rc, _, err = _run("CLUSTER_API_BIND_ADDRESS=172.17.0.1\n", "check", ss=SS_LAN)
        self.assertEqual(rc, 1)
        self.assertIn("192.168.1.40", err)

    def test_nothing_listening_is_refused_because_the_bind_cannot_be_proved(self):
        rc, _, err = _run("CLUSTER_API_BIND_ADDRESS=172.17.0.1\n", "check", ss=SS_OTHER_PORTS)
        self.assertEqual(rc, 1)
        self.assertIn("nothing is listening on port 8000", err)


class ACorrectBindPasses(unittest.TestCase):
    def test_the_bridge_gateway_bind_passes(self):
        rc, out, _ = _run("CLUSTER_API_BIND_ADDRESS=172.17.0.1\n", "check", ss=SS_BRIDGE + SS_OTHER_PORTS)
        self.assertEqual(rc, 0)
        self.assertIn("172.17.0.1", out)

    def test_a_single_node_engine_published_on_loopback_passes(self):
        rc, _, _ = _run("MAIN_MODEL=x\n", "check", ss=SS_LOOPBACK)
        self.assertEqual(rc, 0)

    def test_the_configured_address_plus_a_loopback_listener_passes(self):
        rc, _, _ = _run("CLUSTER_API_BIND_ADDRESS=172.17.0.1\n", "check", ss=SS_BRIDGE + SS_LOOPBACK)
        self.assertEqual(rc, 0)

    def test_a_wildcard_on_a_different_port_is_not_this_gates_business(self):
        rc, _, _ = _run("CLUSTER_API_BIND_ADDRESS=172.17.0.1\n", "check", ss=SS_BRIDGE + SS_OTHER_PORTS)
        self.assertEqual(rc, 0)



class TheVerifyJobUsesTheGate(unittest.TestCase):
    """The shape of pipeline.yml's `verify` job, which the gate is useless without."""

    @classmethod
    def setUpClass(cls):
        import yaml

        pipeline = pathlib.Path(__file__).resolve().parents[2] / "pipeline.yml"
        cls.steps = yaml.safe_load(pipeline.read_text(encoding="utf-8"))["jobs"]["verify"]["steps"]
        cls.runs = [step.get("run", "") for step in cls.steps]

    def test_no_verify_step_probes_the_engine_at_a_hardcoded_loopback_address(self):
        for body in self.runs:
            self.assertNotIn("127.0.0.1:8000", body)
            self.assertNotIn("127.0.0.1:${port}", body)

    def test_the_engine_probes_use_the_resolved_url(self):
        joined = "\n".join(self.runs)
        self.assertIn("engine_bind.py resolve", joined)
        self.assertIn("${ENGINE_URL}/v1/models", joined)
        self.assertIn("${ENGINE_URL}/v1/chat/completions", joined)

    def test_the_bind_check_is_the_last_step_and_runs_unless_cancelled(self):
        last = self.steps[-1]
        self.assertIn("engine_bind.py check", last.get("run", ""))
        self.assertIn("!cancelled()", str(last.get("if", "")))
        self.assertIn("pipefail", last.get("run", ""))


if __name__ == "__main__":
    unittest.main()
