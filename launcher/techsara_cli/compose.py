"""Safe Docker Compose command construction and staged readiness checks."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import PrerequisiteError, TechSaraError
from .utils import parse_env_file, redact, run_command


_INTERNAL_PROBE = r"""
import json, os, sys, urllib.request
base, model, kind = sys.argv[1:4]
headers = {'Content-Type': 'application/json'}
key = os.environ.get('EMBED_API_KEY' if kind == 'embedding' else 'OPENAI_API_KEY', '')
if key:
    headers['Authorization'] = 'Bearer ' + key
root = base.rstrip('/')
service_root = root[:-3] if root.endswith('/v1') else root
chat = {'model': model, 'messages': [{'role':'user','content':'Reply OK.'}], 'max_tokens': 8, 'temperature': 0}
path = '/chat/completions'; body = chat
if kind == 'embedding':
    path = '/embeddings'; body = {'model': model, 'input': ['TechSara readiness probe']}
elif kind in {'vision', 'ocr'}:
    pixel = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
    body = dict(chat, messages=[{'role':'user','content':[{'type':'text','text':'Describe this synthetic pixel.'},{'type':'image_url','image_url':{'url':pixel}}]}])
elif kind == 'structured':
    body = dict(chat, messages=[{'role':'user','content':'Return JSON with key ok set to true.'}], response_format={'type':'json_object'})
elif kind == 'tools':
    body = dict(chat, messages=[{'role':'user','content':'Use ping.'}], tools=[{'type':'function','function':{'name':'ping','description':'synthetic readiness tool','parameters':{'type':'object','properties':{}}}}], tool_choice='auto')
elif kind == 'reasoning':
    body = dict(chat, chat_template_kwargs={'enable_thinking':True})
elif kind == 'streaming':
    body = dict(chat, stream=True)
elif kind == 'tokenization':
    path = '/tokenize'; body = {'model':model,'prompt':'TechSara readiness probe'}; root = service_root
request = urllib.request.Request(root + path, data=json.dumps(body).encode(), headers=headers)
with urllib.request.urlopen(request, timeout=90) as response:
    if kind == 'streaming':
        frame = response.readline(65536).decode('utf-8', errors='replace')
        assert frame.startswith('data:') and ('choices' in frame or frame.strip().endswith('[DONE]'))
        payload = {}
    else:
        payload = json.load(response)
if kind == 'embedding':
    assert payload.get('data') and payload['data'][0].get('embedding')
elif kind == 'tokenization':
    assert isinstance(payload.get('tokens'), list) or isinstance(payload.get('count'), int)
else:
    assert payload.get('choices')
    message = payload['choices'][0].get('message', {})
    if kind == 'structured':
        assert isinstance(json.loads(message.get('content', '')), dict)
    elif kind == 'tools':
        assert message.get('tool_calls')
    elif kind == 'reasoning':
        assert 'reasoning' in message or 'reasoning_content' in message
print(json.dumps({'kind':kind,'supported':True}, separators=(',', ':')))
""".strip()


class ComposeManager:
    def __init__(
        self,
        project_root: Path,
        compose_files: Iterable[Path | str],
        generated_env: Path,
        secrets_env: Path,
        *,
        profiles: Iterable[str] = (),
        runner: Callable[..., object] = run_command,
        secret_values: Iterable[str] = (),
    ) -> None:
        self.project_root = project_root.resolve()
        self.compose_files = tuple(
            path if Path(path).is_absolute() else self.project_root / path
            for path in (Path(item) for item in compose_files)
        )
        self.generated_env = generated_env.resolve()
        self.secrets_env = secrets_env.resolve()
        self.user_env = self.project_root / ".env"
        self.profiles = tuple(sorted(set(profiles)))
        self.runner = runner
        self.secret_values = tuple(value for value in secret_values if value)

    def command(self, *args: str) -> list[str]:
        command = ["docker", "compose", "--project-name", "sf-local-ai"]
        if self.user_env.is_file():
            command.extend(["--env-file", str(self.user_env)])
        if self.secrets_env.is_file():
            command.extend(["--env-file", str(self.secrets_env)])
        command.extend(["--env-file", str(self.generated_env)])
        for path in self.compose_files:
            command.extend(["-f", str(path)])
        for profile in self.profiles:
            command.extend(["--profile", profile])
        command.extend(args)
        return command

    def display_command(self, *args: str) -> str:
        # All pieces are launcher-controlled paths/slugs and contain no secret
        # values. JSON form avoids suggesting shell evaluation semantics.
        return json.dumps(self.command(*args))

    def _environment(self) -> dict[str, str]:
        values = dict(os.environ)
        # Compose gives inherited process variables precedence over --env-file.
        # Reapply the declared file chain so an unrelated exported variable
        # cannot silently override the selected profile or local secret.
        if self.user_env.is_file():
            values.update(parse_env_file(self.user_env))
        if self.secrets_env.is_file():
            values.update(parse_env_file(self.secrets_env))
        values.update(parse_env_file(self.generated_env))
        values["TECHSARA_GENERATED_ENV"] = str(self.generated_env)
        values["TECHSARA_SECRET_ENV"] = str(self.secrets_env)
        return values

    def run(self, *args: str, timeout: float = 300.0) -> object:
        result = self.runner(
            self.command(*args), timeout=timeout, cwd=self.project_root, env=self._environment()
        )
        if getattr(result, "returncode", 1) != 0:
            detail = redact(str(getattr(result, "stderr", ""))[-1200:], self.secret_values).strip()
            raise TechSaraError(
                f"Docker Compose {' '.join(args[:2])} failed" + (f": {detail}" if detail else "")
            )
        return result

    def validate(self) -> None:
        for path in self.compose_files:
            if not path.is_file():
                raise TechSaraError(f"Compose file is missing: {path}")
        self.run("config", "--quiet", timeout=60.0)

    def build(self) -> None:
        self.run("build", "orchestrator", "sync-worker", "frontend", timeout=3600.0)

    def up_service(self, service: str, *, force_recreate: bool = False) -> None:
        args = ["up", "-d", "--no-deps"]
        if force_recreate:
            args.append("--force-recreate")
        args.append(service)
        self.run(*args, timeout=600.0)

    def down(self) -> None:
        # Intentionally no -v/--volumes and no cache/model removal.
        self.run("down", "--timeout", "120", timeout=300.0)

    def stop_service(self, service: str) -> None:
        self.run("stop", "--timeout", "60", service, timeout=120.0)

    def reconcile(
        self, desired_services: Iterable[str], *, allow_published_models: bool = False
    ) -> list[str]:
        return reconcile_project_services(
            desired_services, runner=self.runner, allow_published_models=allow_published_models
        )

    @staticmethod
    def _parse_ps(text: str) -> list[dict[str, Any]]:
        stripped = text.strip()
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
            return payload if isinstance(payload, list) else [payload]
        except json.JSONDecodeError:
            rows: list[dict[str, Any]] = []
            for line in stripped.splitlines():
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        rows.append(item)
                except json.JSONDecodeError:
                    continue
            return rows

    def ps(self, *services: str) -> list[dict[str, Any]]:
        result = self.run("ps", "--format", "json", *services, timeout=30.0)
        return self._parse_ps(str(getattr(result, "stdout", "")))

    def wait_service(
        self,
        service: str,
        *,
        timeout: float,
        require_health: bool = True,
        interval: float = 2.0,
        reporter: Callable[[str], None] | None = None,
        report_every: float = 20.0,
        max_restarts: int = 3,
    ) -> dict[str, Any]:
        """Poll until a service is running/healthy, narrating long waits.

        A large model can take many minutes to load. Without a heartbeat the
        launcher looks hung at exactly the moment it is doing the most work.
        """
        started = time.monotonic()
        deadline = started + timeout
        next_report = started + report_every
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            rows = self.ps(service)
            if rows:
                last = rows[0]
                state = str(last.get("State") or last.get("state") or "").lower()
                health = str(last.get("Health") or last.get("health") or "").lower()
                if state in {"exited", "dead", "removing"}:
                    raise TechSaraError(f"service {service} exited before readiness")
                # A `restart: unless-stopped` service that cannot initialise is
                # never observed as "exited": Docker keeps bringing it back, so
                # a plain state/health poll would burn the entire timeout on a
                # container that has already told us why it failed.
                restarts = self._restart_count(last)
                if restarts >= max_restarts:
                    detail = self._failure_detail(service)
                    raise TechSaraError(
                        f"service {service} restarted {restarts} times without becoming ready"
                        + (f"; last error: {detail}" if detail else "")
                    )
                if state == "running" and ((not require_health) or health == "healthy"):
                    if reporter:
                        reporter(f"  {service}: ready after {time.monotonic() - started:.0f}s")
                    return last
            now = time.monotonic()
            if reporter and now >= next_report:
                observed = str(last.get("Health") or last.get("health") or last.get("State") or "starting")
                reporter(
                    f"  {service}: {observed or 'starting'} "
                    f"({now - started:.0f}s elapsed, timeout {timeout:.0f}s)"
                )
                next_report = now + report_every
            time.sleep(interval)
        health = last.get("Health") or last.get("health") or "unknown"
        raise TechSaraError(f"service {service} did not become healthy before timeout (last health: {health})")

    def _restart_count(self, row: Mapping[str, Any]) -> int:
        """Docker's restart counter for the container behind a ps row."""
        container = str(row.get("ID") or row.get("Id") or row.get("Name") or "").strip()
        if not container:
            return 0
        result = self.runner(
            ["docker", "inspect", container, "--format", "{{.RestartCount}}"], timeout=15.0
        )
        if getattr(result, "returncode", 1) != 0:
            return 0
        try:
            return int(str(getattr(result, "stdout", "0")).strip() or 0)
        except ValueError:
            return 0

    def _failure_detail(self, service: str) -> str:
        """The most informative recent log line, redacted, for an error message."""
        result = self.runner(
            self.command("logs", "--no-color", "--tail", "60", service),
            timeout=45.0, cwd=self.project_root, env=self._environment(),
        )
        if getattr(result, "returncode", 1) != 0:
            return ""
        text = redact(str(getattr(result, "stdout", "")), self.secret_values)
        interesting = [
            line.strip()
            for line in text.splitlines()
            if any(
                marker in line
                for marker in ("Error", "error:", "ValueError", "RuntimeError", "AssertionError", "out of memory")
            )
        ]
        return (interesting[-1] if interesting else "")[:400]

    def probe_internal_model(self, base_url: str, model_id: str, *, kind: str = "chat") -> dict[str, object]:
        if kind not in {"chat", "embedding", "vision", "ocr", "structured", "tools", "reasoning", "streaming", "tokenization"}:
            raise ValueError("unsupported internal probe kind")
        result = self.run(
            "run", "--rm", "--no-deps", "--entrypoint", "python3", "orchestrator",
            "-c", _INTERNAL_PROBE, base_url, model_id, kind,
            timeout=180.0,
        )
        for line in reversed(str(getattr(result, "stdout", "")).splitlines()):
            try:
                payload = json.loads(line)
                if isinstance(payload, dict) and payload.get("kind") == kind:
                    return payload
            except json.JSONDecodeError:
                continue
        # Older/fake test runners may return no stdout after a successful
        # exact-argv assertion. The subprocess exit status still proved it.
        return {"kind": kind, "supported": True}


def docker_project_has_running_models(
    *, runner: Callable[..., object] = run_command,
) -> bool:
    result = runner(
        [
            "docker", "ps", "--filter", "label=com.docker.compose.project=sf-local-ai",
            "--filter", "label=com.docker.compose.service=vllm", "--format", "{{.ID}}",
        ],
        timeout=10.0,
    )
    return getattr(result, "returncode", 1) == 0 and bool(str(getattr(result, "stdout", "")).strip())


def reconcile_project_services(
    desired_services: Iterable[str],
    *,
    runner: Callable[..., object] = run_command,
    allow_published_models: bool = False,
) -> list[str]:
    """Stop stale/legacy project services without removing containers or data.

    A model container that publishes a host port is normally a leftover from
    the pre-launcher layout and is stopped so the API returns to the internal
    network. When the operator has explicitly opted into publishing model ports,
    that is the requested configuration, not drift, and only genuinely
    undesired services are stopped.
    """
    known_optional = {
        "vllm", "vllm-router", "vllm-embed", "vllm-ocr", "vllm-vision",
        "llama-cpp", "sync-worker", "searxng", "pgadmin",
    }
    model_services = {
        "vllm", "vllm-router", "vllm-embed", "vllm-ocr", "vllm-vision", "llama-cpp",
    }
    desired = set(desired_services)
    result = runner(
        [
            "docker", "ps", "--filter", "label=com.docker.compose.project=sf-local-ai",
            "--format", '{{.ID}}\t{{.Label "com.docker.compose.service"}}\t{{.Ports}}',
        ],
        timeout=15.0,
    )
    if getattr(result, "returncode", 1) != 0:
        raise TechSaraError("could not inspect existing sf-local-ai services before reconciliation")
    stopped: list[str] = []
    for line in str(getattr(result, "stdout", "")).splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        container_id, service = parts[0].strip(), parts[1].strip()
        ports = parts[2] if len(parts) > 2 else ""
        if not re_full_container_id(container_id) or service not in known_optional:
            continue
        legacy_public_model = (
            not allow_published_models and service in model_services and "->" in ports
        )
        if service not in desired or legacy_public_model:
            stopped_result = runner(["docker", "stop", "--time", "60", container_id], timeout=90.0)
            if getattr(stopped_result, "returncode", 1) != 0:
                raise TechSaraError(f"could not safely stop stale project service {service}")
            stopped.append(service)
    return stopped


def re_full_container_id(value: str) -> bool:
    return 1 <= len(value) <= 64 and all(character in "0123456789abcdefABCDEF" for character in value)
