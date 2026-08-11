"""Project-scoped native runtime, owned-process lifecycle, and API probes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import OfflineError, PrerequisiteError, TechSaraError
from .hardware import HardwareInfo
from .profiles import ModelSpec
from .utils import (
    FileLock,
    atomic_write_json,
    load_json,
    redact,
    run_command,
    verified_download,
)

RUNTIME_STATE_SCHEMA = 1
PROCESS_STATE_SCHEMA = 1
CAPABILITY_SCHEMA = 1


@dataclass(frozen=True)
class RuntimeInstall:
    status: str
    path: str
    version: str
    python_version: str
    message: str = ""

    @property
    def ready(self) -> bool:
        return self.status == "installed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProcessRecord:
    schema_version: int
    service: str
    pid: int
    process_identity: str
    command_fingerprint: str
    project_root: str
    model_id: str
    runtime_version: str
    port: int
    log_path: str
    started_at: str
    health: str = "starting"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProcessRecord | None":
        try:
            allowed = cls.__dataclass_fields__.keys()
            record = cls(**{key: data[key] for key in allowed})
        except (KeyError, TypeError, ValueError):
            return None
        integer_fields = (record.schema_version, record.pid, record.port)
        string_fields = (
            record.service, record.process_identity, record.command_fingerprint,
            record.project_root, record.model_id, record.runtime_version,
            record.log_path, record.started_at, record.health,
        )
        if any(type(value) is not int for value in integer_fields):
            return None
        if any(not isinstance(value, str) for value in string_fields):
            return None
        if not record.service or not record.service.replace("-", "").isalnum():
            return None
        if record.pid <= 0 or not 1024 <= record.port <= 65535:
            return None
        return record

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _command_fingerprint(args: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(str(arg) for arg in args).encode("utf-8")).hexdigest()


def _process_identity(pid: int) -> str:
    if pid <= 0:
        return ""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        value = proc_stat.read_text(encoding="utf-8", errors="replace")
        close = value.rfind(")")
        fields = value[close + 2 :].split()
        # Field 22 is process start time; the slice begins at field 3.
        if close >= 0 and len(fields) > 19:
            return f"linux:{fields[19]}"
    except OSError:
        pass
    try:
        result = run_command(["ps", "-p", str(pid), "-o", "lstart="], timeout=3.0)
        if result.returncode == 0 and result.stdout.strip():
            return f"ps:{result.stdout.strip()}"
    except OSError:
        pass
    return ""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class RuntimeManager:
    """Install the immutable native vLLM-Metal runtime in a user cache."""

    def __init__(
        self,
        project_root: Path,
        runtime_dir: Path,
        shared_root: Path,
        runtime_spec: Mapping[str, Any],
        *,
        uv_path: Path | str | None = None,
        runner: Callable[..., object] = run_command,
    ) -> None:
        self.project_root = project_root.resolve()
        self.runtime_dir = runtime_dir.resolve()
        self.shared_root = shared_root.expanduser().resolve()
        self.spec = dict(runtime_spec)
        suffix = ".exe" if os.name == "nt" else ""
        self.uv_path = str(uv_path or (Path.home() / ".techsara" / "bin" / f"uv{suffix}"))
        self.runner = runner

    @property
    def install_path(self) -> Path:
        return self.shared_root / f"vllm-metal-{self.spec['version']}"

    @staticmethod
    def _python_path(root: Path) -> Path:
        return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def inspect(self) -> RuntimeInstall:
        root = self.install_path
        if root.is_symlink():
            return RuntimeInstall("invalid", str(root), str(self.spec.get("version", "")), str(self.spec.get("python", "3.12")), "runtime install path must not be a symlink")
        marker = load_json(root / "runtime.json", {})
        expected = {
            "schema_version": RUNTIME_STATE_SCHEMA,
            "runtime": "vllm-metal",
            "version": self.spec.get("version"),
            "commit": self.spec.get("commit"),
            "wheel_sha256": self.spec.get("wheel_sha256"),
            "vllm_source_sha256": self.spec.get("vllm_source_sha256"),
            "validation_status": "complete",
        }
        if not root.exists():
            return RuntimeInstall("missing", str(root), str(self.spec.get("version", "")), str(self.spec.get("python", "3.12")))
        if not isinstance(marker, dict) or any(marker.get(k) != v for k, v in expected.items()):
            return RuntimeInstall("invalid", str(root), str(self.spec.get("version", "")), str(self.spec.get("python", "3.12")), "runtime marker does not match the pinned manifest")
        if "python" in marker and str(marker.get("python")) != str(self.spec.get("python", "3.12")):
            return RuntimeInstall("invalid", str(root), str(self.spec.get("version", "")), str(self.spec.get("python", "3.12")), "runtime marker Python does not match the pinned Python version")
        python = self._python_path(root)
        if not python.is_file():
            return RuntimeInstall("invalid", str(root), str(self.spec.get("version", "")), str(self.spec.get("python", "3.12")), "runtime Python is missing")
        check = self.runner(
            [str(python), "-c", "import json,platform,vllm,vllm_metal; print(json.dumps({'arch':platform.machine(),'vllm':vllm.__version__}))"],
            timeout=30.0,
        )
        if getattr(check, "returncode", 1) != 0:
            return RuntimeInstall("invalid", str(root), str(self.spec.get("version", "")), str(self.spec.get("python", "3.12")), "runtime import verification failed")
        try:
            payload = json.loads(str(getattr(check, "stdout", "")).splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            payload = {}
        if payload.get("arch") != "arm64":
            return RuntimeInstall("invalid", str(root), str(self.spec.get("version", "")), str(self.spec.get("python", "3.12")), "runtime Python is not native arm64")
        if str(payload.get("vllm", "")) != str(self.spec.get("vllm_version", "")):
            return RuntimeInstall("invalid", str(root), str(self.spec.get("version", "")), str(self.spec.get("python", "3.12")), "installed vLLM core does not match the pinned runtime version")
        return RuntimeInstall("installed", str(root), str(self.spec.get("version", "")), str(self.spec.get("python", "3.12")))

    def ensure(
        self,
        hardware: HardwareInfo,
        *,
        offline: bool = False,
        dry_run: bool = False,
    ) -> RuntimeInstall:
        current = self.inspect()
        if current.ready:
            return current
        if hardware.operating_system != "darwin" or not hardware.apple_silicon:
            raise PrerequisiteError("native vLLM-Metal requires macOS on Apple Silicon")
        if hardware.running_under_rosetta or hardware.native_architecture != "arm64":
            raise PrerequisiteError("vLLM-Metal requires a native arm64 terminal; Rosetta/x86_64 is unsupported")
        if current.status == "invalid":
            raise TechSaraError(f"pinned runtime is invalid and was preserved at {current.path}; move it aside manually before reinstalling")
        if offline:
            raise OfflineError("pinned vLLM-Metal runtime is not installed and --offline was requested")
        if dry_run:
            return RuntimeInstall("planned", str(self.install_path), str(self.spec["version"]), str(self.spec["python"]), "verified runtime download/install required")

        downloads = self.shared_root.parent / "downloads"
        locks = self.runtime_dir / "locks"
        wheel_name = Path(str(self.spec["wheel_url"])).name
        source_name = Path(str(self.spec["vllm_source_url"])).name
        with FileLock(locks / f"runtime-vllm-metal-{self.spec['version']}.lock", timeout=30.0, stale_after=24 * 3600):
            current = self.inspect()
            if current.ready:
                return current
            wheel = verified_download(str(self.spec["wheel_url"]), downloads / wheel_name, str(self.spec["wheel_sha256"]))
            source = verified_download(str(self.spec["vllm_source_url"]), downloads / source_name, str(self.spec["vllm_source_sha256"]))
            staging = self.install_path.with_name(self.install_path.name + ".partial")
            if staging.is_symlink():
                raise TechSaraError("refusing a runtime staging path that is a symbolic link")
            if self.install_path.exists():
                raise TechSaraError(f"refusing to overwrite an invalid runtime: {self.install_path}")
            staging.parent.mkdir(parents=True, exist_ok=True)
            create = self.runner(
                [self.uv_path, "venv", "--python", str(self.spec["python"]), "--managed-python", str(staging)],
                timeout=900.0,
                cwd=self.project_root,
            )
            if getattr(create, "returncode", 1) != 0:
                raise TechSaraError("could not create the pinned native Python 3.12 runtime")
            python = self._python_path(staging)
            install = self.runner(
                [self.uv_path, "pip", "install", "--python", str(python), "--require-hashes", "-r", str(self.project_root / "config" / "vllm-metal-runtime.txt")],
                timeout=3600.0,
                cwd=self.project_root,
                env={**os.environ, "TECHSARA_VLLM_SOURCE": source.as_uri(), "TECHSARA_VLLM_METAL_WHEEL": wheel.as_uri()},
            )
            if getattr(install, "returncode", 1) != 0:
                # Some uv/pip versions intentionally do not interpolate env in
                # requirements. Fall back to the same two checksum-verified
                # local artifacts; the resolved closure is captured below.
                install = self.runner(
                    [self.uv_path, "pip", "install", "--python", str(python), str(source), str(wheel)],
                    timeout=3600.0,
                    cwd=self.project_root,
                )
            if getattr(install, "returncode", 1) != 0:
                detail = redact(str(getattr(install, "stderr", ""))[-500:], [])
                raise TechSaraError("vLLM-Metal installation failed" + (f": {detail.strip()}" if detail.strip() else ""))
            verify = self.runner(
                [str(python), "-c", "import json,platform,vllm,vllm_metal; assert platform.machine() == 'arm64'; print(json.dumps({'arch':platform.machine(),'vllm':vllm.__version__}))"],
                timeout=60.0,
            )
            if getattr(verify, "returncode", 1) != 0:
                raise TechSaraError("installed vLLM-Metal runtime failed its native import self-test")
            freeze = self.runner([self.uv_path, "pip", "freeze", "--python", str(python)], timeout=60.0)
            marker = {
                "schema_version": RUNTIME_STATE_SCHEMA,
                "runtime": "vllm-metal",
                "version": self.spec["version"],
                "commit": self.spec["commit"],
                "python": self.spec["python"],
                "wheel_sha256": self.spec["wheel_sha256"],
                "vllm_source_sha256": self.spec["vllm_source_sha256"],
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "resolved_packages": str(getattr(freeze, "stdout", "")).splitlines() if getattr(freeze, "returncode", 1) == 0 else [],
                "validation_status": "complete",
            }
            atomic_write_json(staging / "runtime.json", marker)
            os.replace(staging, self.install_path)
            final = self.inspect()
            if not final.ready:
                raise TechSaraError(f"runtime installation did not validate: {final.message}")
            return final


class ProcessManager:
    """Start and stop only model processes whose identity this project owns."""

    def __init__(self, project_root: Path, runtime_dir: Path) -> None:
        self.project_root = project_root.resolve()
        self.runtime_dir = runtime_dir.resolve()
        self.pid_dir = self.runtime_dir / "pids"
        self.log_dir = self.runtime_dir / "logs"

    def record_path(self, service: str) -> Path:
        if not service.replace("-", "").isalnum():
            raise ValueError("invalid native service name")
        return self.pid_dir / f"{service}.json"

    def load(self, service: str) -> ProcessRecord | None:
        raw = load_json(self.record_path(service), {})
        return ProcessRecord.from_dict(raw) if isinstance(raw, dict) else None

    def is_owned_running(self, record: ProcessRecord | None) -> bool:
        return bool(
            record
            and record.schema_version == PROCESS_STATE_SCHEMA
            and Path(record.project_root).resolve() == self.project_root
            and _pid_alive(record.pid)
            and record.process_identity
            and _process_identity(record.pid) == record.process_identity
        )

    @staticmethod
    def port_available(port: int, host: str = "127.0.0.1") -> bool:
        if not 1024 <= int(port) <= 65535:
            raise ValueError("model port must be between 1024 and 65535")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex((host, int(port))) != 0

    def status(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        if not self.pid_dir.exists():
            return records
        for path in sorted(self.pid_dir.glob("*.json")):
            raw = load_json(path, {})
            record = ProcessRecord.from_dict(raw) if isinstance(raw, dict) else None
            records.append({
                "service": path.stem,
                "state": "running" if self.is_owned_running(record) else "stale",
                "record": record.to_dict() if record else {},
            })
        return records

    def is_resumable_start(
        self,
        service: str,
        args: Sequence[str],
        *,
        model_id: str,
        runtime_version: str,
        port: int,
    ) -> bool:
        """Return true only for this project's exact interrupted start.

        ``start`` deliberately refuses to treat a merely-starting process as
        healthy reuse.  The launcher can use this stricter predicate to wait
        for an identical process after Ctrl+C without spawning a duplicate.
        """
        record = self.load(service)
        return bool(
            record
            and self.is_owned_running(record)
            and record.health == "starting"
            and record.command_fingerprint == _command_fingerprint(args)
            and record.model_id == model_id
            and record.runtime_version == runtime_version
            and record.port == port
        )

    def start(
        self,
        service: str,
        args: Sequence[str],
        *,
        model_id: str,
        runtime_version: str,
        port: int,
        env: Mapping[str, str] | None = None,
        dry_run: bool = False,
    ) -> ProcessRecord:
        existing = self.load(service)
        fingerprint = _command_fingerprint(args)
        if self.is_owned_running(existing):
            if (
                existing
                and existing.command_fingerprint == fingerprint
                and existing.model_id == model_id
                and existing.port == port
                and existing.runtime_version == runtime_version
                and existing.health == "healthy"
            ):
                return existing
            raise TechSaraError(f"native service {service} is already running with a different model or command")
        if not self.port_available(port):
            raise TechSaraError(f"port {port} is already in use; refusing to replace an unrelated process")
        log = self.log_dir / f"{service}.log"
        if dry_run:
            return ProcessRecord(PROCESS_STATE_SCHEMA, service, 0, "dry-run", fingerprint, str(self.project_root), model_id, runtime_version, port, str(log), datetime.now(timezone.utc).isoformat(), "planned")
        self.pid_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > 10 * 1024**2:
            rotated = log.with_suffix(".log.1")
            rotated.unlink(missing_ok=True)
            os.replace(log, rotated)
        process_env = dict(os.environ)
        process_env.update(env or {})
        process_env["TECHSARA_PROJECT_OWNER"] = hashlib.sha256(str(self.project_root).encode()).hexdigest()
        with log.open("a", encoding="utf-8") as handle:
            process = subprocess.Popen(
                [str(arg) for arg in args],
                cwd=self.project_root,
                env=process_env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
        time.sleep(0.05)
        identity = _process_identity(process.pid)
        if process.poll() is not None or not identity:
            raise TechSaraError(f"native service {service} exited before its ownership record could be created; see {log}")
        record = ProcessRecord(
            PROCESS_STATE_SCHEMA, service, process.pid, identity, fingerprint,
            str(self.project_root), model_id, runtime_version, int(port), str(log),
            datetime.now(timezone.utc).isoformat(), "starting",
        )
        atomic_write_json(self.record_path(service), record.to_dict(), mode=0o600)
        return record

    def mark_health(self, service: str, health: str) -> None:
        record = self.load(service)
        if record and self.is_owned_running(record):
            data = record.to_dict()
            data["health"] = health
            atomic_write_json(self.record_path(service), data, mode=0o600)

    def stop(self, service: str, *, timeout: float = 20.0, dry_run: bool = False) -> bool:
        path = self.record_path(service)
        record = self.load(service)
        if not record:
            return False
        if not self.is_owned_running(record):
            # A stale ownership file is safe to remove; no process is signalled.
            if not dry_run:
                path.unlink(missing_ok=True)
            return False
        if dry_run:
            return True
        # Close the PID-reuse race between the initial ownership check and the
        # signal. A changed start identity is never signalled.
        if not self.is_owned_running(record):
            path.unlink(missing_ok=True)
            return False
        os.kill(record.pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _pid_alive(record.pid):
            time.sleep(0.1)
        if _pid_alive(record.pid) and _process_identity(record.pid) == record.process_identity:
            os.kill(record.pid, signal.SIGKILL)
        path.unlink(missing_ok=True)
        return True

    def stop_all(self, *, timeout: float = 20.0, dry_run: bool = False) -> list[str]:
        stopped: list[str] = []
        if not self.pid_dir.exists():
            return stopped
        for path in sorted(self.pid_dir.glob("*.json")):
            if self.stop(path.stem, timeout=timeout, dry_run=dry_run):
                stopped.append(path.stem)
        return stopped


class CapabilityProber:
    """Probe OpenAI/vLLM contracts with synthetic, non-user content."""

    def __init__(self, *, timeout: float = 12.0) -> None:
        self.timeout = timeout

    def _request(
        self,
        base_url: str,
        path: str,
        *,
        api_key: str = "",
        payload: Mapping[str, Any] | None = None,
        stream: bool = False,
    ) -> tuple[bool, Any, str]:
        url = base_url.rstrip("/") + path
        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.readline(65536) if stream else response.read(2 * 1024**2)
                text = raw.decode("utf-8", errors="replace")
                if stream:
                    return bool(text.strip()), text[:500], ""
                try:
                    return True, json.loads(text) if text else {}, ""
                except json.JSONDecodeError:
                    return True, text[:500], ""
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            return False, None, type(exc).__name__

    @staticmethod
    def _result(ok: bool, detail: str = "") -> dict[str, object]:
        return {"supported": bool(ok), "detail": detail}

    @staticmethod
    def _message(body: Any) -> dict[str, Any] | None:
        try:
            message = body["choices"][0]["message"]
            return message if isinstance(message, dict) else None
        except (KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _stream_contract(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        for line in value.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("choices"), list):
                return True
        return False

    def probe(
        self,
        *,
        name: str,
        base_url: str,
        model: ModelSpec,
        api_key: str = "",
        selected_context: int = 0,
    ) -> dict[str, object]:
        capabilities: dict[str, object] = {
            "schema_version": CAPABILITY_SCHEMA,
            "name": name,
            "model_id": model.api_model_id,
            "backend": model.backend,
            "endpoint": base_url,
            "probed_at": datetime.now(timezone.utc).isoformat(),
            "configured_context": selected_context or model.tested_context,
        }
        ok, models, error = self._request(base_url, "/v1/models", api_key=api_key)
        model_ids = []
        if ok and isinstance(models, dict) and isinstance(models.get("data"), list):
            model_ids = [item.get("id") for item in models["data"] if isinstance(item, dict)]
        models_ok = ok and model.api_model_id in model_ids
        capabilities["models"] = self._result(models_ok, error or ("configured model not listed" if not models_ok else ""))
        health_ok, _, health_error = self._request(base_url, "/health", api_key=api_key)
        capabilities["health"] = self._result(health_ok, health_error)

        chat_payload = {
            "model": model.api_model_id,
            "messages": [{"role": "user", "content": "Reply with exactly OK."}],
            "max_tokens": 8,
            "temperature": 0,
        }
        if model.supports_chat:
            chat_ok, body, chat_error = self._request(base_url, "/v1/chat/completions", api_key=api_key, payload=chat_payload)
            message = self._message(body) if chat_ok else None
            chat_ok = bool(message and ("content" in message or "tool_calls" in message))
            capabilities["chat"] = self._result(chat_ok, chat_error or ("invalid chat response contract" if not chat_ok else ""))
            stream_body = dict(chat_payload, stream=True)
            stream_ok, stream_payload, stream_error = self._request(base_url, "/v1/chat/completions", api_key=api_key, payload=stream_body, stream=True)
            stream_ok = bool(stream_ok and self._stream_contract(stream_payload))
            capabilities["streaming"] = self._result(stream_ok, stream_error)
            # Closing urllib's streaming response after the first frame is the
            # non-destructive client-disconnect probe; health must remain live.
            post_cancel, _, cancel_error = self._request(base_url, "/health", api_key=api_key)
            capabilities["cancellation"] = self._result(stream_ok and post_cancel, cancel_error)
            reasoning = False
            if message:
                reasoning = "reasoning" in message or "reasoning_content" in message
            capabilities["reasoning"] = self._result(reasoning if model.supports_reasoning else False, "field observed" if reasoning else "reasoning field not observed")
            structured = dict(chat_payload, messages=[{"role": "user", "content": "Return JSON with key ok set to true."}], response_format={"type": "json_object"})
            structured_ok, structured_body, structured_error = self._request(base_url, "/v1/chat/completions", api_key=api_key, payload=structured)
            structured_message = self._message(structured_body) if structured_ok else None
            if structured_message:
                try:
                    structured_ok = isinstance(json.loads(structured_message.get("content", "")), dict)
                except (json.JSONDecodeError, TypeError):
                    structured_ok = False
            else:
                structured_ok = False
            capabilities["structured_output"] = self._result(structured_ok, structured_error)
            tools = dict(chat_payload, messages=[{"role": "user", "content": "Use the ping tool."}], tools=[{"type": "function", "function": {"name": "ping", "description": "test", "parameters": {"type": "object", "properties": {}}}}], tool_choice="auto")
            tool_ok, tool_body, tool_error = self._request(base_url, "/v1/chat/completions", api_key=api_key, payload=tools)
            tool_observed = False
            if tool_ok and isinstance(tool_body, dict):
                try:
                    tool_observed = bool(tool_body["choices"][0]["message"].get("tool_calls"))
                except (KeyError, IndexError, TypeError, AttributeError):
                    pass
            capabilities["tool_calling"] = self._result(tool_observed, tool_error or ("tool response not observed" if not tool_observed else ""))
        else:
            for feature in ("chat", "streaming", "cancellation", "reasoning", "structured_output", "tool_calling"):
                capabilities[feature] = self._result(False, "not declared by pinned model manifest")

        token_ok, token_body, token_error = self._request(base_url, "/tokenize", api_key=api_key, payload={"model": model.api_model_id, "prompt": "TechSara probe"})
        token_ok = bool(
            token_ok
            and isinstance(token_body, dict)
            and (
                isinstance(token_body.get("tokens"), list)
                or (type(token_body.get("count")) is int and token_body["count"] >= 0)
            )
        )
        capabilities["tokenization"] = self._result(token_ok, token_error)
        capabilities["maximum_context"] = {
            "supported": bool(token_ok and (selected_context or model.tested_context) <= model.context_limit),
            "detail": "selected context is manifest-bounded; destructive full-window stress test not run",
            "configured": selected_context or model.tested_context,
            "manifest_limit": model.context_limit,
        }

        if model.supports_embeddings:
            embed_ok, embed_body, embed_error = self._request(base_url, "/v1/embeddings", api_key=api_key, payload={"model": model.api_model_id, "input": ["TechSara capability probe"]})
            dimension = 0
            if embed_ok and isinstance(embed_body, dict):
                try:
                    dimension = len(embed_body["data"][0]["embedding"])
                except (KeyError, IndexError, TypeError):
                    embed_ok = False
            capabilities["embeddings"] = {**self._result(embed_ok, embed_error), "dimension": dimension}
        else:
            capabilities["embeddings"] = self._result(False, "not declared by pinned model manifest")

        if model.supports_reranking:
            score_ok, score_body, score_error = self._request(base_url, "/score", api_key=api_key, payload={"model": model.api_model_id, "text_1": "TechSara", "text_2": "local AI"})
            try:
                score = score_body["data"][0]["score"]
                score_ok = bool(score_ok and isinstance(score, (int, float)) and math.isfinite(float(score)))
            except (KeyError, IndexError, TypeError, ValueError):
                score_ok = False
            capabilities["reranking"] = self._result(score_ok, score_error)
        else:
            capabilities["reranking"] = self._result(False, "not declared by pinned model manifest")

        if model.supports_vision:
            pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            vision_body = dict(chat_payload, messages=[{"role": "user", "content": [{"type": "text", "text": "What color is this pixel?"}, {"type": "image_url", "image_url": {"url": pixel}}]}])
            vision_ok, vision_response, vision_error = self._request(base_url, "/v1/chat/completions", api_key=api_key, payload=vision_body)
            vision_ok = bool(vision_ok and self._message(vision_response))
            capabilities["vision"] = self._result(vision_ok, vision_error)
        else:
            capabilities["vision"] = self._result(False, "not declared by pinned model manifest")
        capabilities["ocr"] = self._result(bool(model.supports_ocr and capabilities.get("vision", {}).get("supported")), "uses multimodal chat contract" if model.supports_ocr else "not declared by pinned model manifest")
        capabilities["model_load_unload"] = self._result(False, "no verified non-destructive load/unload API in this pinned backend")
        return capabilities

    def write_results(self, path: Path, results: Iterable[Mapping[str, Any]]) -> None:
        atomic_write_json(path, {
            "schema_version": CAPABILITY_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "models": list(results),
        })
