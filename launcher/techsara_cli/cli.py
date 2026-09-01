"""The single cross-platform TechSara launcher implementation."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import environment as environment_module
from .cluster import CLUSTER_COMPOSE_OVERLAY, discover_cluster_peer, resolve_cluster_mode
from .compose import ComposeManager, docker_project_has_running_models
from .environment import (
    DGX_COMPOSE_OVERLAY,
    RuntimeLayout,
    build_generated_environment,
    effective_user_environment,
    has_salesforce_credentials,
    main_context_notices,
    prepare_local_secrets,
    profile_context_length,
)
from .errors import PrerequisiteError, TechSaraError
from .hardware import HardwareInfo, detect_hardware
from .model_manager import ModelInstall, ModelManager
from .profiles import SelectedProfile, load_model_manifest, select_profile
from .runtime import CapabilityProber, ProcessManager, RuntimeManager
from .utils import (
    GIB,
    FileLock,
    atomic_write_json,
    atomic_write_text,
    load_json,
    parse_env_file,
    redact,
    render_env,
    run_command,
    secure_token,
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _human_bytes(value: int) -> str:
    if value <= 0:
        return "unknown"
    return f"{value / GIB:.1f} GiB"


def _yes(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _cluster_mode(generated: Mapping[str, str]) -> str:
    """The generated cluster mode; anything unset means the single-node layout."""
    return (generated.get("TECHSARA_CLUSTER_MODE") or "single").strip().lower()


def _recorded_cluster_mode(layout: RuntimeLayout) -> str:
    """The mode of the deployed stack: state.json first, then generated.env."""
    state = load_json(layout.state_file, {})
    if isinstance(state, dict) and state.get("cluster_mode"):
        return str(state["cluster_mode"]).strip().lower()
    return _cluster_mode(parse_env_file(layout.generated_env))


def _cluster_summary(profile: SelectedProfile, generated: Mapping[str, str]) -> str:
    """One line saying which cluster mode `up` chose and why; empty off DGX Spark."""
    mode = _cluster_mode(generated)
    reason = (generated.get("TECHSARA_CLUSTER_REASON") or "").strip()
    if mode != "dual" and profile.hardware_profile_id != "dgx-spark":
        return ""
    return f"Cluster: {mode} - {reason or 'CLUSTER_MODE=single in .env'}"


def _context_summary(
    profile: SelectedProfile, generated: Mapping[str, str], user_environment: Mapping[str, str]
) -> list[str]:
    """What `up` says about the main-model window; nothing when it is ordinary."""
    return main_context_notices(
        user_environment, generated, profile_context=profile_context_length(profile)
    )


def _served_context(generated: Mapping[str, str], profile: SelectedProfile) -> int:
    """The window the generated environment actually serves, for the summary.

    ``_print_selection`` used to print ``profile.context_length`` unconditionally,
    so an extended window printed two contradictory numbers in one ``up``.
    """
    try:
        return int(str(generated.get("MODEL_MAX_CONTEXT") or "").strip())
    except (TypeError, ValueError):
        return profile.context_length


def _report_script_line(reporter: Callable[[str], None], raw: bytes) -> None:
    text = raw.decode("utf-8", errors="replace").split("\r")[-1].rstrip()
    if text:
        reporter(f"  | {text}")


def _run_cluster_script(
    root: Path,
    script_name: str,
    *args: str,
    reporter: Callable[[str], None] | None = None,
    timeout: float,
) -> None:
    """Run ``scripts/<script_name>`` and narrate its output line by line.

    The cluster helpers stay bash on purpose (they drive ssh, rsync, and
    Docker on the worker host); this is the launcher's only hand-off to them.
    A non-zero exit or the timeout is a ``TechSaraError``.
    """
    report = reporter or _step
    script = root / "scripts" / script_name
    label = " ".join(("scripts/" + script_name,) + tuple(args))
    if not script.is_file():
        raise TechSaraError(f"{label} is missing from {root}")
    try:
        process = subprocess.Popen(
            ["bash", str(script), *args],
            cwd=str(root),
            env=dict(os.environ, NO_COLOR="1"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        raise TechSaraError(f"could not run {label}: {exc}") from exc
    deadline = time.monotonic() + timeout
    pending = b""
    progress_at = 0.0
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector, process.stdout as stream:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise TechSaraError(f"{label} did not finish within {timeout:.0f}s")
            if not selector.select(timeout=min(remaining, 1.0)):
                continue
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                _report_script_line(report, line)
            # rsync-style progress rewrites one line with \r; show it sparingly.
            if b"\r" in pending:
                pending = pending.rsplit(b"\r", 1)[-1]
                if time.monotonic() - progress_at >= 2.0:
                    progress_at = time.monotonic()
                    _report_script_line(report, pending)
    if pending:
        _report_script_line(report, pending)
    try:
        code = process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise TechSaraError(f"{label} did not finish within {timeout:.0f}s") from exc
    if code != 0:
        raise TechSaraError(f"{label} failed with exit status {code}; see its output above")


def _verbose(args: argparse.Namespace, message: str) -> None:
    if bool(getattr(args, "verbose", False)):
        print(f"  [verbose] {message}")


def _secret_values(values: Mapping[str, str]) -> list[str]:
    markers = ("SECRET", "PASSWORD", "TOKEN", "API_KEY", "PRIVATE_KEY")
    return [value for key, value in values.items() if any(marker in key.upper() for marker in markers) and len(value) >= 4]


def _load_hardware(layout: RuntimeLayout) -> HardwareInfo | None:
    data = load_json(layout.hardware_file, {})
    return HardwareInfo.from_dict(data) if isinstance(data, dict) and data else None


def _load_profile(layout: RuntimeLayout) -> SelectedProfile | None:
    data = load_json(layout.profile_file, {})
    try:
        return SelectedProfile.from_dict(data) if isinstance(data, dict) and data else None
    except (TypeError, ValueError, KeyError):
        return None


def _compose_files(
    root: Path,
    hardware: HardwareInfo,
    profile: SelectedProfile,
    *,
    publish_model_ports: bool = False,
    cluster_mode: str = "single",
) -> list[Path]:
    files = [root / "compose.yaml"] + [root / item for item in profile.compose_files]
    if hardware.operating_system == "windows" and profile.family == "nvidia":
        files.append(root / "compose" / "compose.windows-wsl2.yaml")
    if publish_model_ports:
        # One overlay per family: Compose merges a service name it has never
        # seen into a new, imageless service, so a single shared file would
        # break every profile that does not define all of these services.
        published = {
            "dgx-spark": "compose/compose.published-dgx-spark.yaml",
            "nvidia": "compose/compose.published-nvidia.yaml",
            "cpu": "compose/compose.published-cpu.yaml",
        }.get(
            "dgx-spark" if profile.hardware_profile_id == "dgx-spark" else profile.family
        )
        if published:
            files.append(root / published)
    if cluster_mode == "dual":
        # Always last: it resets the `ports`/`networks` the published overlay
        # adds to `vllm` and switches that one service to host networking.
        files.append(root / CLUSTER_COMPOSE_OVERLAY)
    return files


def _has_reranker_service(profile: SelectedProfile) -> bool:
    """Only the DGX overlay declares a standalone ``vllm-reranker`` service.

    Every other NVIDIA profile shares ``compose.nvidia.yaml``, which has no
    such service, and keeps scoring in-process.
    """
    return bool(
        profile.reranker_model
        and profile.features.get("reranker")
        and DGX_COMPOSE_OVERLAY in profile.compose_files
    )


def _compose_profiles(profile: SelectedProfile, user_env: Mapping[str, str], *, skip_ocr: bool) -> list[str]:
    requested = {
        item.strip().lower()
        for item in (user_env.get("COMPOSE_PROFILES") or "").split(",")
        if item.strip()
    }
    profiles: list[str] = []
    if profile.embedding_model and profile.features.get("embeddings"):
        profiles.append("embeddings")
    if _has_reranker_service(profile):
        profiles.append("reranker")
    if profile.ocr_model and profile.features.get("ocr") and not skip_ocr:
        profiles.append("ocr")
    search_provider = (user_env.get("SEARCH_PROVIDER") or "searxng").strip().lower()
    if (_yes(user_env.get("SEARCH_ENABLED")) and search_provider == "searxng") or "search" in requested:
        profiles.append("search")
    if "admin" in requested or "pgadmin" in requested:
        profiles.append("admin")
    return profiles


def _desired_optional_services(
    profile: SelectedProfile,
    compose_profiles: Iterable[str],
    *,
    salesforce_ready: bool,
) -> set[str]:
    desired: set[str] = set()
    if profile.family == "nvidia":
        desired.add("vllm")
        if not profile.router_shared and profile.router_model:
            desired.add("vllm-router")
        if profile.embedding_model and profile.features.get("embeddings"):
            desired.add("vllm-embed")
        if _has_reranker_service(profile):
            desired.add("vllm-reranker")
        if profile.ocr_model and profile.features.get("ocr"):
            desired.add("vllm-ocr")
    elif profile.family == "cpu":
        desired.add("llama-cpp")
    enabled_profiles = set(compose_profiles)
    if "search" in enabled_profiles:
        desired.add("searxng")
    if "admin" in enabled_profiles:
        desired.add("pgadmin")
    if salesforce_ready:
        desired.add("sync-worker")
    return desired


def _reconcile_native_processes(layout: RuntimeLayout, profile: SelectedProfile) -> list[str]:
    desired: set[str] = set()
    if profile.family == "mac":
        desired.update({"main-model", "main-bridge"})
        if profile.embedding_model and profile.features.get("embeddings"):
            desired.update({"embedding-model", "embedding-bridge"})
        if profile.reranker_model and profile.features.get("reranker"):
            desired.update({"reranker-model", "reranker-bridge"})
    manager = ProcessManager(layout.project_root, layout.runtime_dir)
    stopped: list[str] = []
    for item in manager.status():
        service = str(item.get("service", ""))
        if service and service not in desired and manager.stop(service):
            stopped.append(service)
    return stopped


def _project_has_running_models(layout: RuntimeLayout) -> bool:
    """Return true only when this project already owns a healthy model.

    Model selection may use total rather than currently-free memory in that
    case because the apparent pressure is the model that the next launch will
    reuse or replace. This also keeps a second native-Mac launch from
    downgrading merely because its first model is still resident.
    """
    if docker_project_has_running_models():
        return True
    manager = ProcessManager(layout.project_root, layout.runtime_dir)
    record = manager.load("main-model")
    return bool(
        record
        and record.health == "healthy"
        and manager.is_owned_running(record)
    )


def _print_selection(
    hardware: HardwareInfo,
    profile: SelectedProfile,
    installs: Iterable[ModelInstall] = (),
    *,
    context: int | None = None,
) -> None:
    print("\nHost:")
    print(f"  Operating system: {hardware.operating_system} {hardware.operating_system_version}")
    print(f"  Architecture: {hardware.host_architecture} (native {hardware.native_architecture})")
    if hardware.apple_chip_name:
        print(f"  Chip: {hardware.apple_chip_name}")
        print(f"  Unified memory: {_human_bytes(hardware.apple_unified_memory_bytes)}")
    elif hardware.gpu_name:
        print(f"  Accelerator: {hardware.gpu_name} ({hardware.gpu_count})")
        print(f"  Accelerator memory: {_human_bytes(hardware.gpu_total_memory_bytes)}")
    else:
        print(f"  Processor: {hardware.cpu_name}")
    print(f"  System memory: {_human_bytes(hardware.total_system_memory_bytes)}")
    print(f"  Available memory: {_human_bytes(hardware.available_system_memory_bytes)}")
    print(f"  Free disk: {_human_bytes(hardware.free_disk_bytes)}")
    print("\nRuntime:")
    print(f"  Selected profile: {profile.id}")
    print(f"  Backend: {profile.runtime_backend}")
    # The window actually being served, which MAIN_MODEL_MAX_LEN may have
    # moved away from the profile's own pick; falls back to the profile for
    # the callers that have no generated environment in hand.
    served = int(context) if context else profile.context_length
    print(f"  Context / concurrency: {served} / {profile.concurrency}")
    print("\nModels:")
    print(f"  Main: {profile.main_model.id if profile.main_model else 'disabled'}")
    print(f"  Router: {'shared with main' if profile.router_shared else (profile.router_model.id if profile.router_model else 'disabled')}")
    print(f"  Embedding: {profile.embedding_model.id if profile.embedding_model else 'disabled'}")
    print(f"  Reranker: {'enabled/on-demand' if profile.reranker_model else 'disabled'}")
    print(f"  Vision: {'enabled' if profile.features.get('vision') else 'disabled'}")
    print(f"  OCR: {'enabled' if profile.features.get('ocr') else 'disabled/degraded'}")
    states = list(installs)
    if states:
        print("  Cache:")
        for item in states:
            print(f"    {item.model_id}@{item.revision[:12]}: {item.status}")
    for reason in profile.degraded_reasons:
        print(f"  Degraded: {reason}")


def _docker_group_members(group: str = "docker") -> tuple[bool, list[str]] | None:
    """Return whether the ``docker`` group exists and who belongs to it.

    ``None`` means the question is unanswerable on this host (Windows, or a
    system without a ``docker`` group), so the caller must not claim anything
    about group membership.
    """
    try:
        import grp  # Unix only; absent on Windows.
        import pwd
    except ImportError:
        return None
    try:
        entry = grp.getgrnam(group)
    except KeyError:
        return None
    members = list(entry.gr_mem)
    try:
        login = pwd.getpwuid(os.getuid())
        # A user whose *primary* group is `docker` never appears in gr_mem.
        if login.pw_gid == entry.gr_gid and login.pw_name not in members:
            members.append(login.pw_name)
    except KeyError:
        pass
    return entry.gr_gid in os.getgroups(), members


def _docker_permission_diagnosis() -> tuple[str, list[str], str]:
    """Explain, for this exact host, how to make the Docker socket readable.

    Returned as (explanation, commands, closing) so the same diagnosis can be
    rendered as an indented block for an error and as one line for ``doctor``
    without either rendering mangling the commands.
    """
    if os.name == "nt":
        return (
            "Docker refused this account access to its API.",
            [],
            "Add your Windows account to the 'docker-users' local group, then sign out and back in.",
        )
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"
    membership = _docker_group_members()
    header = "Docker is running, but this account is not allowed to use its socket."
    if membership is None:
        return (
            f"{header} No 'docker' group exists on this host. Either run Docker"
            " rootless (see https://docs.docker.com/engine/security/rootless/),"
            " or create the group and grant access:",
            [f"sudo groupadd docker && sudo usermod -aG docker {user}"],
            "Then open a new login session and rerun ./techsara up",
        )
    active, members = membership
    if active:
        # The group is already applied to this process, so the socket itself is
        # the problem -- a custom DOCKER_HOST, a non-default context, or socket
        # permissions that were changed by hand.
        host = os.environ.get("DOCKER_HOST", "")
        endpoint = f" DOCKER_HOST is set to {host!r}." if host else ""
        return (
            f"{header} This account is already in the 'docker' group, so the"
            f" endpoint is at fault.{endpoint}",
            ["docker context ls"],
            "Check that context and the socket's owner/mode, then rerun ./techsara up",
        )
    if user in members:
        # The classic trap: usermod succeeded but this shell predates it.
        return (
            f"{header} '{user}' is in the 'docker' group, but this login session"
            " started before that change, so the kernel has not applied it yet."
            " Refresh the session:",
            ["newgrp docker"],
            "(or log out and back in), then rerun ./techsara up",
        )
    return (
        f"{header} Add '{user}' to the 'docker' group and refresh this session:",
        [f"sudo usermod -aG docker {user}", "newgrp docker"],
        "(`newgrp` applies the group to the current shell; a fresh login works"
        " too.) Then rerun ./techsara up",
    )


def _docker_permission_remedy(*, indent: str = " " * 16) -> str:
    """Render the diagnosis as an indented multi-line block for an error."""
    explanation, commands, closing = _docker_permission_diagnosis()
    # The first line is not indented: it is printed straight after the
    # "TechSara error: " prefix.  Every following line carries its own indent so
    # the block lines up underneath that prefix.
    lines = [explanation]
    lines.extend(f"{indent}    {command}" for command in commands)
    if closing:
        lines.append(f"{indent}{closing}")
    return "\n".join(lines)


def _docker_permission_summary() -> str:
    """Render the same diagnosis as a single line for the doctor report."""
    explanation, commands, closing = _docker_permission_diagnosis()
    parts = [explanation]
    if commands:
        # The explanation already ends in a colon, so the commands read as its
        # continuation; "then" keeps two commands from looking like one.
        parts.append("; then: ".join(commands) + ".")
    if closing:
        parts.append(closing)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _require_docker(hardware: HardwareInfo) -> None:
    if not hardware.docker_installed:
        raise PrerequisiteError("Docker is not installed. Install Docker Engine/Desktop, start it, then rerun ./techsara up")
    if hardware.docker_permission_denied:
        raise PrerequisiteError(_docker_permission_remedy())
    if not hardware.docker_running:
        raise PrerequisiteError("Docker is installed but its daemon is not running. Start Docker, then rerun ./techsara up")
    if not hardware.docker_compose_available:
        raise PrerequisiteError("Docker Compose is unavailable. Install/enable Docker Compose v2.24 or newer")
    if hardware.docker_compose_version:
        parts = tuple(int(part) for part in hardware.docker_compose_version.split(".")[:2])
        if parts < (2, 24):
            raise PrerequisiteError(
                f"Docker Compose {hardware.docker_compose_version} is too old; upgrade to v2.24 or newer"
            )
    if not hardware.docker_linux_containers:
        raise PrerequisiteError("Docker must be running Linux containers; switch Docker Desktop away from Windows containers")


def _model_manager(
    layout: RuntimeLayout,
    hardware: HardwareInfo,
    runtimes: Mapping[str, Any],
    user_env: Mapping[str, str] | None = None,
) -> ModelManager:
    uv = layout.shared_root / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    # A repository .env is configuration, not a process environment.  Copy
    # only HF_TOKEN into the downloader boundary so Salesforce/database
    # credentials are never inherited by the Hugging Face helper.  An
    # explicitly exported token wins over the file value.
    downloader_env = dict(os.environ)
    token = downloader_env.get("HF_TOKEN") or (user_env or {}).get("HF_TOKEN", "")
    if token:
        downloader_env["HF_TOKEN"] = token
    return ModelManager(
        layout.project_root, Path(hardware.selected_cache_path), layout.locks_dir,
        uv_path=uv, downloader_version=str((runtimes.get("huggingface_hub") or {}).get("version", "0.36.0")),
        environ=downloader_env,
    )


def _write_configuration(
    layout: RuntimeLayout,
    hardware: HardwareInfo,
    profile: SelectedProfile,
    installs: list[ModelInstall],
    *,
    skip_ocr: bool,
    search_enabled: bool,
    search_provider: str = "searxng",
    context_override: int | None = None,
    allow_planned: bool = False,
    external_environment: Mapping[str, str] | None = None,
    user_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    generated = build_generated_environment(
        layout, profile, installs, cache_root=Path(hardware.selected_cache_path),
        skip_ocr=skip_ocr, search_enabled=search_enabled, search_provider=search_provider,
        context_override=context_override,
        allow_planned=allow_planned, external_environment=external_environment,
        user_environment=user_environment,
    )
    atomic_write_json(layout.hardware_file, hardware.to_dict())
    atomic_write_json(layout.profile_file, profile.to_dict())
    atomic_write_text(layout.generated_env, render_env(generated), mode=0o644)
    return generated


def _relocated_layout(layout: RuntimeLayout, runtime: Path) -> RuntimeLayout:
    return RuntimeLayout(
        project_root=layout.project_root,
        runtime_dir=runtime,
        hardware_file=runtime / "hardware.json",
        profile_file=runtime / "selected-profile.json",
        generated_env=runtime / "generated.env",
        secrets_env=runtime / "secrets.env",
        state_file=runtime / "state.json",
        capabilities_file=runtime / "capabilities.json",
        locks_dir=runtime / "locks",
        logs_dir=runtime / "logs",
        pids_dir=runtime / "pids",
        shared_root=layout.shared_root,
    )


def _cmd_up_dry(args: argparse.Namespace, *, root: Path) -> int:
    """Validate a complete plan using only automatically removed temp files."""
    persistent = RuntimeLayout.for_project(root)
    hardware = detect_hardware(root, allow_network=not args.offline)
    _verbose(args, "hardware discovery completed")
    _require_docker(hardware)
    profile = select_profile(
        hardware, root, profile_override=args.profile, model_override=args.model,
        skip_ocr=args.skip_ocr,
        reuse_running_models=_project_has_running_models(persistent),
    )
    _verbose(args, f"selected {profile.id} with runtime {profile.runtime_backend}")
    _, runtimes = load_model_manifest(root)
    user_env = parse_env_file(root / ".env")
    secrets = parse_env_file(persistent.secrets_env)
    for key in ("POSTGRES_PASSWORD", "PGADMIN_DEFAULT_PASSWORD", "SEARXNG_SECRET", "SESSION_SECRET", "TECHSARA_MODEL_API_KEY"):
        if not secrets.get(key) and not user_env.get(key):
            secrets[key] = secure_token(24)
    if profile.runtime_backend == "native-vllm-metal":
        key = secrets["TECHSARA_MODEL_API_KEY"]
        secrets.update(OPENAI_API_KEY=key, EMBED_API_KEY=key, RERANK_API_KEY=key)
    effective = dict(user_env)
    effective.update(secrets)
    manager = _model_manager(persistent, hardware, runtimes, user_env)
    installs = manager.ensure_all(
        profile.required_models(skip_ocr=args.skip_ocr), offline=args.offline, dry_run=True,
    )
    _verbose(args, f"validated {len(installs)} model installation plans")
    runtime_install = None
    with tempfile.TemporaryDirectory(prefix="techsara-dry-run-") as temporary:
        layout = _relocated_layout(persistent, Path(temporary) / ".runtime")
        layout.runtime_dir.mkdir(parents=True, exist_ok=True)
        layout.locks_dir.mkdir(parents=True, exist_ok=True)
        if profile.runtime_backend == "native-vllm-metal":
            runtime = RuntimeManager(
                root, layout.runtime_dir, layout.shared_root / "runtimes", runtimes["vllm-metal"],
                uv_path=layout.shared_root / "bin" / "uv",
            )
            runtime_install = runtime.ensure(hardware, offline=args.offline, dry_run=True)
            profile, _ = _start_native_models(
                layout, hardware, profile, installs, runtime_install, secrets, dry_run=True,
            )
        search_enabled = _yes(user_env.get("SEARCH_ENABLED")) or "search" in {
            value.strip().lower() for value in user_env.get("COMPOSE_PROFILES", "").split(",")
        }
        generation_options: dict[str, Any] = {
            "cache_root": Path(hardware.selected_cache_path),
            "skip_ocr": args.skip_ocr,
            "search_enabled": search_enabled,
            "search_provider": (user_env.get("SEARCH_PROVIDER") or "searxng"),
            "allow_planned": True,
            "user_environment": user_env,
        }
        if profile.family == "external":
            generation_options["external_environment"] = user_env
        generated = build_generated_environment(layout, profile, installs, **generation_options)
        publish_model_ports = _yes(generated.get("TECHSARA_PUBLISH_MODEL_PORTS"))
        cluster_mode = _cluster_mode(generated)
        cluster_line = _cluster_summary(profile, generated)
        if cluster_line:
            print(cluster_line)
        for line in _context_summary(profile, generated, user_env):
            print(line)
        atomic_write_text(layout.generated_env, render_env(generated), mode=0o644)
        atomic_write_text(layout.secrets_env, render_env(secrets), mode=0o600)
        profiles = _compose_profiles(profile, user_env, skip_ocr=args.skip_ocr)
        compose = ComposeManager(
            root,
            _compose_files(
                root, hardware, profile,
                publish_model_ports=publish_model_ports, cluster_mode=cluster_mode,
            ),
            layout.generated_env, layout.secrets_env,
            profiles=profiles, secret_values=_secret_values(effective),
        )
        _verbose(args, "validating the resolved Compose overlay and environment chain")
        compose.validate()
        _start_compose(
            compose, profile, generated,
            salesforce_ready=has_salesforce_credentials(effective),
            search_enabled="search" in profiles, dry_run=True,
            endpoints=_local_endpoints(generated, user_env),
        )
    _print_selection(hardware, profile, installs, context=_served_context(generated, profile))
    print("\nPlan validated; no services, persistent runtime state, runtimes, or downloads were changed.")
    print(f"Frontend: planned at {_local_endpoints(generated, user_env)['frontend']}")
    print("Orchestrator: planned")
    print(f"Salesforce sync: {'ready' if has_salesforce_credentials(effective) else 'credentials unavailable (local UI remains available)'}")
    return 0


def _wait_native_models(prober: CapabilityProber, base_url: str, *, api_key: str = "", timeout: float = 1200.0) -> None:
    deadline = time.monotonic() + timeout
    last = "unreachable"
    while time.monotonic() < deadline:
        ok, _, last = prober._request(base_url, "/v1/models", api_key=api_key)
        if ok:
            return
        time.sleep(2.0)
    raise TechSaraError(f"native model server did not become ready ({last})")


def _start_native_component(
    layout: RuntimeLayout,
    manager: ProcessManager,
    prober: CapabilityProber,
    runtime_path: Path,
    model: Any,
    install: ModelInstall,
    *,
    name: str,
    direct_port: int,
    bridge_port: int,
    context: int,
    concurrency: int,
    api_key: str,
    runtime_version: str,
    dry_run: bool,
) -> dict[str, Any]:
    executable = runtime_path / "bin" / "vllm"
    args = [
        str(executable), "serve", install.path, "--served-model-name", model.api_model_id,
        "--host", "127.0.0.1", "--port", str(direct_port),
        "--max-model-len", str(min(context, model.context_limit)),
        "--max-num-seqs", str(max(1, concurrency)), *model.startup_arguments,
    ]
    environment = {
        "HF_HOME": str(Path(layout.shared_root) / "model-cache" / "huggingface"),
        "HF_HUB_OFFLINE": "1",
    }
    model_service = f"{name}-model"
    if dry_run or manager.is_resumable_start(
        model_service,
        args,
        model_id=model.id,
        runtime_version=runtime_version,
        port=direct_port,
    ) is not True:
        manager.start(
            model_service, args, model_id=model.id, runtime_version=runtime_version,
            port=direct_port, env=environment, dry_run=dry_run,
        )
    bridge_args = [
        sys.executable, "-m", "techsara_cli.bridge", "--listen-host", "0.0.0.0",
        "--listen-port", str(bridge_port), "--target", f"http://127.0.0.1:{direct_port}",
    ]
    if dry_run:
        manager.start(
            f"{name}-bridge", bridge_args, model_id=model.id, runtime_version="bridge-1",
            port=bridge_port, env={"TECHSARA_MODEL_API_KEY": api_key}, dry_run=True,
        )
        return {"name": name, "status": "planned", "model_id": model.api_model_id}
    _wait_native_models(prober, f"http://127.0.0.1:{direct_port}")
    bridge_service = f"{name}-bridge"
    if manager.is_resumable_start(
        bridge_service,
        bridge_args,
        model_id=model.id,
        runtime_version="bridge-1",
        port=bridge_port,
    ) is not True:
        manager.start(
            bridge_service, bridge_args, model_id=model.id, runtime_version="bridge-1",
            port=bridge_port, env={"TECHSARA_MODEL_API_KEY": api_key}, dry_run=False,
        )
    _wait_native_models(prober, f"http://127.0.0.1:{bridge_port}", api_key=api_key, timeout=30.0)
    result = prober.probe(
        name=name, base_url=f"http://127.0.0.1:{direct_port}", model=model,
        selected_context=min(context, model.context_limit),
    )
    manager.mark_health(f"{name}-model", "healthy")
    manager.mark_health(f"{name}-bridge", "healthy")
    return result


def _start_native_models(
    layout: RuntimeLayout,
    hardware: HardwareInfo,
    profile: SelectedProfile,
    installs: list[ModelInstall],
    runtime_install: Any,
    secrets: Mapping[str, str],
    *,
    dry_run: bool,
) -> tuple[SelectedProfile, list[dict[str, Any]]]:
    process = ProcessManager(layout.project_root, layout.runtime_dir)
    prober = CapabilityProber(timeout=30.0)
    install_map = {(item.model_id, item.revision): item for item in installs}
    key = secrets.get("TECHSARA_MODEL_API_KEY", "")
    results: list[dict[str, Any]] = []
    runtime_path = Path(runtime_install.path)
    current = profile

    optional = [
        ("embedding", current.embedding_model, 18003, 18103, "embeddings"),
        ("reranker", current.reranker_model, 18005, 18105, "reranker"),
    ]
    for name, model, direct, bridge, feature in optional:
        if not model or not current.features.get(feature):
            continue
        try:
            results.append(_start_native_component(
                layout, process, prober, runtime_path, model,
                install_map[(model.id, model.revision)], name=name, direct_port=direct,
                bridge_port=bridge, context=model.tested_context, concurrency=1,
                api_key=key, runtime_version=runtime_install.version, dry_run=dry_run,
            ))
        except TechSaraError as exc:
            process.stop(f"{name}-bridge")
            process.stop(f"{name}-model")
            features = dict(current.features)
            features[feature] = False
            current = replace(current, features=features, degraded_reasons=current.degraded_reasons + (f"{name} unavailable after capability probe: {exc}",))

    if current.main_model:
        model = current.main_model
        install = install_map[(model.id, model.revision)]
        try:
            results.append(_start_native_component(
                layout, process, prober, runtime_path, model, install, name="main",
                direct_port=18000, bridge_port=18100, context=current.context_length,
                concurrency=current.concurrency, api_key=key,
                runtime_version=runtime_install.version, dry_run=dry_run,
            ))
        except TechSaraError:
            if dry_run or not current.startup_retry_context:
                raise
            process.stop("main-bridge")
            process.stop("main-model")
            results.append(_start_native_component(
                layout, process, prober, runtime_path, model, install, name="main",
                direct_port=18000, bridge_port=18100, context=current.startup_retry_context,
                concurrency=1, api_key=key, runtime_version=runtime_install.version,
                dry_run=False,
            ))
            current = replace(
                current, context_length=current.startup_retry_context, concurrency=1,
                degraded_reasons=current.degraded_reasons + ("main runtime used the single safer startup retry",),
            )
    return current, results


def _port(values: Mapping[str, str], name: str, default: int) -> int:
    raw = str(values.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise TechSaraError(f"{name} must be an integer port; got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise TechSaraError(f"{name} must be between 1 and 65535; got {port}")
    return port


def _local_base_url(bind_address: str, port: int) -> str:
    """A URL this process can reach for a container port published locally.

    A wildcard publish address is never dialled as-is; loopback reaches the
    same listener and does not depend on the host's routable address.
    """
    host = (bind_address or "127.0.0.1").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::", "*", ""}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _local_endpoints(generated: Mapping[str, str], user_env: Mapping[str, str]) -> dict[str, str]:
    bind = str(generated.get("TECHSARA_BIND_ADDRESS", "127.0.0.1"))
    return {
        "orchestrator": _local_base_url(bind, _port(user_env, "ORCHESTRATOR_PORT", 8080)),
        "frontend": _local_base_url(bind, _port(user_env, "FRONTEND_PORT", 3000)),
    }


def _probe_orchestrator(url: str = "http://127.0.0.1:8080/health") -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=15.0) as response:
            payload = json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        raise TechSaraError(f"orchestrator health contract failed ({type(exc).__name__})") from exc
    if not isinstance(payload, dict) or payload.get("status") not in {"ok", "healthy", "degraded"} or not isinstance(payload.get("checks"), dict):
        raise TechSaraError("orchestrator returned an invalid health contract")
    app_db = payload["checks"].get("app_db", {})
    if isinstance(app_db, dict) and app_db.get("status") not in {None, "ok"}:
        raise TechSaraError("orchestrator application database is not ready")
    return payload


def _step(message: str) -> None:
    """Progress narration. Always on: a silent multi-minute wait reads as a hang."""
    print(message, flush=True)


def _start_compose(
    compose: ComposeManager,
    profile: SelectedProfile,
    generated: dict[str, str],
    *,
    salesforce_ready: bool,
    search_enabled: bool,
    dry_run: bool,
    endpoints: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    orchestrator_base = (endpoints or {}).get("orchestrator", "http://127.0.0.1:8080")
    if dry_run:
        return {"status": "planned"}
    project_root = root or _project_root()
    _step("Building application images (orchestrator, sync-worker, frontend)...")
    compose.build()
    _step("Starting core services...")
    compose.up_service("postgres")
    compose.wait_service("postgres", timeout=180.0, reporter=_step)
    if search_enabled:
        compose.up_service("searxng")
        compose.wait_service("searxng", timeout=180.0, reporter=_step)

    disabled: list[str] = []
    router_fallback = False
    retry_context = 0
    capability_results: dict[str, dict[str, Any]] = {}

    def capability_key(kind: str) -> str:
        return {
            "embedding": "embeddings",
            "structured": "structured_output",
            "tools": "tool_calling",
        }.get(kind, kind)

    def capability_record(role: str, base_url: str, model_id: str) -> dict[str, Any]:
        name = f"docker-{role}"
        if name not in capability_results:
            capability_results[name] = {
                "schema_version": 1,
                "name": name,
                "model_id": model_id,
                "backend": profile.runtime_backend,
                "endpoint": base_url,
                "source": "docker-container",
                "probed_at": datetime.now(timezone.utc).isoformat(),
            }
        return capability_results[name]

    def record_probe(
        role: str,
        base_url: str,
        model_id: str,
        *,
        kind: str = "chat",
    ) -> dict[str, object]:
        result = (
            compose.probe_internal_model(base_url, model_id)
            if kind == "chat"
            else compose.probe_internal_model(base_url, model_id, kind=kind)
        )
        evidence = dict(result) if isinstance(result, Mapping) else {}
        evidence.pop("kind", None)
        evidence.setdefault("supported", True)
        capability_record(role, base_url, model_id)[capability_key(kind)] = evidence
        return result

    def record_failed_probe(role: str, base_url: str, model_id: str, *, kind: str) -> None:
        capability_record(role, base_url, model_id)[capability_key(kind)] = {
            "supported": False,
            "detail": "container API probe failed",
        }

    def publish_generated() -> None:
        atomic_write_text(compose.generated_env, render_env(generated), mode=0o644)

    def disable_role(role: str) -> None:
        disabled.append(role)
        prefix = {"embeddings": "EMBED", "ocr": "OCR", "vision": "VISION"}[role]
        generated[f"{prefix}_ENABLED"] = "false"
        for suffix in (
            "SUPPORTS_CHAT", "SUPPORTS_STREAMING", "SUPPORTS_REASONING",
            "SUPPORTS_TOOL_CALLING", "SUPPORTS_STRUCTURED_OUTPUT", "SUPPORTS_VISION",
            "SUPPORTS_EMBEDDINGS", "SUPPORTS_RERANKING", "SUPPORTS_OCR",
            "SUPPORTS_TOKENIZATION", "REQUIRES_AUTHENTICATION",
        ):
            generated[f"{prefix}_{suffix}"] = "false"
        if role == "embeddings":
            generated["EMBED_BASE_URL"] = "http://disabled.invalid/v1"
            generated["EMBED_VIA"] = "http://disabled.invalid/v1"
            generated["EMBED_MODEL"] = "disabled"
        elif role == "ocr":
            generated["OCR_ENABLED"] = "false"
            generated["OCR_BASE_URL"] = "http://disabled.invalid/v1"
            generated["OCR_MODEL"] = "disabled"
        else:
            generated["VISION_BASE_URL"] = "http://disabled.invalid/v1"
            generated["VISION_MODEL"] = "disabled"
        publish_generated()

    if profile.family == "nvidia":
        if profile.embedding_model and profile.features.get("embeddings"):
            try:
                _step("Starting the embedding model (vllm-embed)...")
                compose.up_service("vllm-embed")
                compose.wait_service("vllm-embed", timeout=1800.0, reporter=_step)
                record_probe(
                    "embedding", generated["EMBED_BASE_URL"], generated["EMBED_MODEL"],
                    kind="embedding",
                )
            except TechSaraError:
                record_failed_probe(
                    "embedding", generated["EMBED_BASE_URL"], generated["EMBED_MODEL"],
                    kind="embedding",
                )
                compose.stop_service("vllm-embed")
                disable_role("embeddings")
        if _has_reranker_service(profile):
            try:
                _step("Starting the reranker model (vllm-reranker)...")
                compose.up_service("vllm-reranker")
                compose.wait_service("vllm-reranker", timeout=1800.0, reporter=_step)
            except TechSaraError:
                # The scorer is optional: fall back to the in-process reranker
                # rather than losing reranking altogether.
                compose.stop_service("vllm-reranker")
                generated["RERANK_BACKEND"] = "inprocess"
                generated["RERANK_BASE_URL"] = ""
                publish_generated()
        if not profile.router_shared and profile.router_model:
            try:
                _step("Starting the router model (vllm-router)...")
                compose.up_service("vllm-router")
                compose.wait_service("vllm-router", timeout=1800.0, reporter=_step)
                record_probe("router", generated["ROUTER_BASE_URL"], generated["ROUTER_MODEL"])
            except TechSaraError:
                record_failed_probe(
                    "router", generated["ROUTER_BASE_URL"], generated["ROUTER_MODEL"],
                    kind="chat",
                )
                compose.stop_service("vllm-router")
                router_fallback = True
        if profile.ocr_model and profile.features.get("ocr"):
            try:
                _step("Starting the OCR model (vllm-ocr)...")
                compose.up_service("vllm-ocr")
                compose.wait_service("vllm-ocr", timeout=1800.0, reporter=_step)
                record_probe(
                    "ocr", generated["OCR_BASE_URL"], generated["OCR_MODEL"], kind="ocr"
                )
            except TechSaraError:
                record_failed_probe(
                    "ocr", generated["OCR_BASE_URL"], generated["OCR_MODEL"], kind="ocr"
                )
                compose.stop_service("vllm-ocr")
                disable_role("ocr")
        cluster = _cluster_mode(generated) == "dual"
        if cluster:
            # Node 2 first: the head's rendezvous waits for the worker, and a
            # sync/start failure here must surface as itself, not as a head
            # readiness timeout.
            _step("cluster: preparing the worker host (scripts/cluster-sync.sh)...")
            _run_cluster_script(project_root, "cluster-sync.sh", reporter=_step, timeout=3600.0)
            _step("cluster: starting the vLLM worker (scripts/cluster-worker.sh start)...")
            _run_cluster_script(project_root, "cluster-worker.sh", "start", reporter=_step, timeout=300.0)
        try:
            if cluster:
                _step(
                    "cluster: starting vLLM head (node-rank 0) on "
                    f"{generated.get('CLUSTER_HEAD_IP', '?')}:{generated.get('CLUSTER_MASTER_PORT', '?')}; "
                    f"the worker on {generated.get('CLUSTER_WORKER_IP', '?')} must be running "
                    "(scripts/cluster-worker.sh start)"
                )
            _step("Starting the main model (vllm) - this is the longest step...")
            compose.up_service("vllm")
            compose.wait_service("vllm", timeout=2400.0, reporter=_step)
            record_probe("main", generated["OPENAI_BASE_URL"], generated["MAIN_MODEL"])
        except TechSaraError as exc:
            if cluster:
                # The safer-context retry recreates only the head; it cannot
                # help when the worker is missing, unreachable, or mismatched.
                raise TechSaraError(
                    f"the two-node vLLM head did not become ready ({exc}). Check both nodes with "
                    "scripts/cluster-status.sh and the worker output with scripts/cluster-logs.sh worker"
                ) from exc
            if not profile.startup_retry_context:
                raise
            retry_context = profile.startup_retry_context
            generated["MODEL_MAX_CONTEXT"] = str(retry_context)
            generated["DEFAULT_MAX_CONTEXT"] = str(retry_context)
            generated["REPORT_MAX_CONTEXT"] = str(retry_context)
            generated["MAIN_CONTEXT_LENGTH"] = str(retry_context)
            # The retry window is inside the model's native one, so the YaRN
            # override that belonged to the wider window must go with it.
            generated["MAIN_MODEL_ROPE_OVERRIDE"] = ""
            generated["MODEL_CONCURRENCY"] = "1"
            generated["MAIN_CONCURRENCY"] = "1"
            publish_generated()
            compose.validate()
            compose.up_service("vllm", force_recreate=True)
            compose.wait_service("vllm", timeout=2400.0, reporter=_step)
            record_probe("main", generated["OPENAI_BASE_URL"], generated["MAIN_MODEL"])
        if _yes(generated.get("VISION_ENABLED")):
            try:
                record_probe(
                    "main", generated["OPENAI_BASE_URL"], generated["MAIN_MODEL"], kind="vision"
                )
            except TechSaraError:
                record_failed_probe(
                    "main", generated["OPENAI_BASE_URL"], generated["MAIN_MODEL"], kind="vision"
                )
                disable_role("vision")
        if router_fallback:
            generated["ROUTER_BASE_URL"] = generated["OPENAI_BASE_URL"]
            generated["ROUTER_MODEL"] = generated["MAIN_MODEL"]
            generated["AGENT_BASE_URL"] = generated["OPENAI_BASE_URL"]
            generated["AGENT_MODEL"] = generated["MAIN_MODEL"]
            for prefix in ("ROUTER", "AGENT"):
                for key, value in list(generated.items()):
                    if key.startswith("MAIN_"):
                        generated[prefix + key[len("MAIN"):]] = value
            publish_generated()
    elif profile.family == "cpu":
        compose.up_service("llama-cpp")
        compose.wait_service("llama-cpp", timeout=900.0, reporter=_step)
        record_probe("main", generated["OPENAI_BASE_URL"], generated["MAIN_MODEL"])
    elif profile.family == "mac":
        # This is the required real container-to-host bridge/API probe.
        record_probe("main", generated["OPENAI_BASE_URL"], generated["MAIN_MODEL"])
    elif profile.family == "external":
        # External endpoints must still be reachable through the same container
        # network path that the orchestrator will use.
        record_probe("main", generated["OPENAI_BASE_URL"], generated["MAIN_MODEL"])
        if _yes(generated.get("EMBED_ENABLED")) and generated.get("EMBED_MODEL") != "disabled":
            try:
                record_probe(
                    "embedding", generated["EMBED_BASE_URL"], generated["EMBED_MODEL"],
                    kind="embedding",
                )
            except TechSaraError:
                record_failed_probe(
                    "embedding", generated["EMBED_BASE_URL"], generated["EMBED_MODEL"],
                    kind="embedding",
                )
                disable_role("embeddings")
        if _yes(generated.get("VISION_ENABLED")):
            try:
                record_probe(
                    "main", generated["VISION_BASE_URL"], generated["VISION_MODEL"], kind="vision"
                )
            except TechSaraError:
                record_failed_probe(
                    "main", generated["VISION_BASE_URL"], generated["VISION_MODEL"], kind="vision"
                )
                disable_role("vision")

    _step("Starting the orchestrator...")
    compose.up_service("orchestrator")
    compose.wait_service("orchestrator", timeout=300.0, reporter=_step)
    health = _probe_orchestrator(f"{orchestrator_base}/health")
    if salesforce_ready:
        compose.up_service("sync-worker")
        compose.wait_service("sync-worker", timeout=60.0, require_health=False, reporter=_step)
    _step("Starting the frontend...")
    compose.up_service("frontend")
    compose.wait_service("frontend", timeout=240.0, reporter=_step)
    if "admin" in compose.profiles:
        compose.up_service("pgadmin")
        compose.wait_service("pgadmin", timeout=180.0, require_health=False, reporter=_step)
    return {
        "status": "running", "orchestrator": health,
        "disabled_features": disabled, "router_fallback": router_fallback,
        "startup_retry_context": retry_context,
        "capability_results": list(capability_results.values()),
    }


def _cmd_up(args: argparse.Namespace, *, root: Path) -> int:
    print("TechSara AI platform bootstrap")
    if args.dry_run:
        return _cmd_up_dry(args, root=root)
    layout = RuntimeLayout.for_project(root)
    layout.create()
    with FileLock(layout.locks_dir / "launcher.lock", timeout=3.0, stale_after=6 * 3600):
        _step("Detecting host hardware and Docker capabilities...")
        hardware = detect_hardware(root, allow_network=not args.offline)
        _verbose(args, "hardware discovery completed")
        _require_docker(hardware)
        reuse = _project_has_running_models(layout)
        profile = select_profile(
            hardware, root, profile_override=args.profile, model_override=args.model,
            skip_ocr=args.skip_ocr, reuse_running_models=reuse,
        )
        _verbose(args, f"selected {profile.id} with runtime {profile.runtime_backend}")
        models, runtimes = load_model_manifest(root)
        user_env = parse_env_file(root / ".env")
        secrets, secret_warnings = prepare_local_secrets(layout, profile, user_env)
        effective = dict(user_env)
        effective.update(secrets)
        manager = _model_manager(layout, hardware, runtimes, user_env)
        required = profile.required_models(skip_ocr=args.skip_ocr)
        _step(
            f"Selected {profile.id}; checking {len(required)} pinned model(s) "
            f"in {hardware.selected_cache_path}"
        )
        installs = manager.ensure_all(
            required, offline=args.offline, dry_run=args.dry_run,
            reporter=None if args.dry_run else _step,
        )
        _verbose(args, f"validated {len(installs)} required model installations")

        runtime_install = None
        capability_results: list[dict[str, Any]] = []
        _reconcile_native_processes(layout, profile)
        if profile.runtime_backend == "native-vllm-metal":
            runtime = RuntimeManager(
                root, layout.runtime_dir, layout.shared_root / "runtimes", runtimes["vllm-metal"],
                uv_path=layout.shared_root / "bin" / "uv",
            )
            _step("Preparing the pinned native vLLM-Metal runtime...")
            runtime_install = runtime.ensure(hardware, offline=args.offline, dry_run=args.dry_run)
            profile, capability_results = _start_native_models(
                layout, hardware, profile, installs, runtime_install, secrets, dry_run=args.dry_run,
            )

        search_enabled = _yes(user_env.get("SEARCH_ENABLED")) or "search" in {
            value.strip().lower() for value in user_env.get("COMPOSE_PROFILES", "").split(",")
        }
        configuration_options: dict[str, Any] = {
            "skip_ocr": args.skip_ocr,
            "search_enabled": search_enabled,
            "search_provider": (user_env.get("SEARCH_PROVIDER") or "searxng"),
            "allow_planned": args.dry_run,
            "user_environment": user_env,
        }
        if profile.family == "external":
            configuration_options["external_environment"] = user_env
        generated = _write_configuration(
            layout, hardware, profile, installs, **configuration_options,
        )
        publish_model_ports = _yes(generated.get("TECHSARA_PUBLISH_MODEL_PORTS"))
        cluster_mode = _cluster_mode(generated)
        cluster_line = _cluster_summary(profile, generated)
        if cluster_line:
            _step(cluster_line)
        for line in _context_summary(profile, generated, user_env):
            _step(line)
        compose_files = _compose_files(
            root, hardware, profile,
            publish_model_ports=publish_model_ports, cluster_mode=cluster_mode,
        )
        if cluster_mode == "dual" and not publish_model_ports:
            _step(
                "cluster: PUBLISH_MODEL_PORTS is off, so the vLLM head API binds to the Docker bridge "
                f"gateway {generated.get('CLUSTER_API_BIND_ADDRESS', '?')}:{generated.get('VLLM_PORT', '?')} "
                "(reachable by this host's containers only), not to a loopback-published port"
            )
        profiles = _compose_profiles(profile, user_env, skip_ocr=args.skip_ocr)
        compose = ComposeManager(
            root, compose_files, layout.generated_env, layout.secrets_env,
            profiles=profiles, secret_values=_secret_values(effective),
        )
        _step("Validating the resolved Compose configuration...")
        compose.validate()
        salesforce_ready = has_salesforce_credentials(effective)
        compose.reconcile(
            _desired_optional_services(profile, profiles, salesforce_ready=salesforce_ready),
            allow_published_models=publish_model_ports,
        )
        _verbose(args, "reconciled project-owned optional services without deleting containers or volumes")
        endpoints = _local_endpoints(generated, user_env)
        result = _start_compose(
            compose, profile, generated, salesforce_ready=salesforce_ready,
            search_enabled="search" in profiles, dry_run=args.dry_run,
            endpoints=endpoints, root=root,
        )
        combined_capability_results = list(capability_results)
        docker_capability_results = result.get("capability_results", [])
        if isinstance(docker_capability_results, list):
            combined_capability_results.extend(
                item for item in docker_capability_results if isinstance(item, dict)
            )
        if combined_capability_results:
            CapabilityProber().write_results(
                layout.capabilities_file, combined_capability_results
            )
        if not args.dry_run:
            changed = False
            features = dict(profile.features)
            reasons = list(profile.degraded_reasons)
            for feature in result.get("disabled_features", []):
                features[feature] = False
                reasons.append(f"{feature} disabled after its real startup/API probe failed")
                changed = True
            if result.get("router_fallback") and profile.main_model:
                profile = replace(profile, router_model=profile.main_model, router_shared=True)
                reasons.append("separate router failed readiness and fell back to the healthy main model")
                changed = True
            if result.get("startup_retry_context"):
                profile = replace(
                    profile, context_length=int(result["startup_retry_context"]), concurrency=1,
                )
                reasons.append("main model used the single safer startup retry")
                changed = True
            if changed:
                profile = replace(profile, features=features, degraded_reasons=tuple(dict.fromkeys(reasons)))
                atomic_write_json(layout.profile_file, profile.to_dict())
        state = {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": "planned" if args.dry_run else "running",
            "profile": profile.id,
            "hardware_profile": profile.hardware_profile_id,
            "cluster_mode": cluster_mode,
            "compose_files": [str(path.relative_to(root)) for path in compose_files],
            "compose_profiles": profiles,
            "compose_command": compose.command("up", "-d"),
            "runtime": runtime_install.to_dict() if runtime_install else {"status": profile.runtime_backend},
            "models": [item.to_dict() for item in installs],
            "salesforce_sync": "ready" if salesforce_ready else "credentials unavailable",
            "endpoints": endpoints,
            "result": result,
        }
        atomic_write_json(layout.state_file, state)
        _print_selection(hardware, profile, installs, context=_served_context(generated, profile))
        for warning in secret_warnings:
            print(f"Warning: {warning}")
        print(
            "\nPlan validated; no services, runtimes, or downloads were changed."
            if args.dry_run
            else f"\nFrontend: healthy at {endpoints['frontend']}"
        )
        print(f"Orchestrator: {'planned' if args.dry_run else 'healthy/degraded contract verified'}")
        print(f"Salesforce sync: {'ready' if salesforce_ready else 'credentials unavailable (local UI remains available)'}")
        return 0


def _compose_from_state(layout: RuntimeLayout) -> ComposeManager | None:
    state = load_json(layout.state_file, {})
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("compose_files"), list)
        or not state["compose_files"]
        or not layout.generated_env.is_file()
    ):
        return None
    resolved_files: list[Path] = []
    root = layout.project_root.resolve()
    for item in state["compose_files"]:
        candidate = Path(str(item))
        if candidate.is_absolute():
            raise TechSaraError("saved Compose state contains an absolute path")
        resolved = (root / candidate).resolve()
        if root != resolved and root not in resolved.parents:
            raise TechSaraError("saved Compose state points outside the project")
        if resolved.suffix not in {".yaml", ".yml"}:
            raise TechSaraError("saved Compose state contains a non-YAML path")
        resolved_files.append(resolved)
    secrets = effective_user_environment(layout)
    return ComposeManager(
        layout.project_root, resolved_files,
        layout.generated_env, layout.secrets_env, profiles=state.get("compose_profiles") or (),
        secret_values=_secret_values(secrets),
    )


def _unmanaged_project_containers(root: Path) -> dict[str, set[str]]:
    """Running `sf-local-ai` containers that this launcher did not start.

    Compose records the file(s) a container was created from, so a stack
    brought up with the superseded root `docker-compose.yml` is identifiable
    rather than merely absent from launcher state.
    """
    result = run_command(
        [
            "docker", "ps", "--filter", "label=com.docker.compose.project=sf-local-ai",
            "--format", '{{.Label "com.docker.compose.service"}}\t{{.Label "com.docker.compose.project.config_files"}}',
        ],
        timeout=15.0,
    )
    if result.returncode != 0:
        return {}
    grouped: dict[str, set[str]] = {}
    managed = {str(root / "compose.yaml")}
    for line in result.stdout.splitlines():
        service, _, files = line.partition("\t")
        service, files = service.strip(), files.strip()
        if not service or not files:
            continue
        # A launcher-started container always lists compose.yaml first.
        if files.split(",")[0].strip() in managed:
            continue
        for path in files.split(","):
            grouped.setdefault(path.strip(), set()).add(service)
    return grouped


def _cmd_down(args: argparse.Namespace, *, root: Path) -> int:
    layout = RuntimeLayout.for_project(root)
    compose = _compose_from_state(layout)
    verb = "Would stop" if args.dry_run else "Stopped"
    if compose:
        if args.dry_run:
            print(f"Would run: {compose.display_command('down', '--timeout', '120')}")
        else:
            compose.down()
    if compose and _recorded_cluster_mode(layout) == "dual":
        # The worker on Node 2 belongs to this deployment too. Its host may
        # already be off, so failing to reach it is a warning, not an error.
        if args.dry_run:
            print("Would run: scripts/cluster-worker.sh down")
        else:
            try:
                _run_cluster_script(root, "cluster-worker.sh", "down", reporter=_step, timeout=300.0)
                print("Stopped the vLLM worker on the second DGX Spark.")
            except TechSaraError as exc:
                print(
                    f"Warning: could not stop the vLLM worker on the second DGX Spark ({exc}); "
                    "it may be powered off, or stop it later with scripts/cluster-worker.sh down"
                )
    process = ProcessManager(root, layout.runtime_dir)
    stopped = process.stop_all(dry_run=args.dry_run)

    if compose:
        print(f"{verb} the launcher-managed Compose project.")
    else:
        print(
            "Nothing to stop: this launcher has no recorded stack "
            f"({layout.state_file} does not exist, so `techsara up` has not completed here)."
        )
    print(f"Project-owned native processes: {', '.join(stopped) if stopped else 'none'}")

    # Do not let "nothing to stop" read as "nothing is running".
    unmanaged = _unmanaged_project_containers(root)
    if unmanaged:
        print("\nStill running, started outside this launcher:")
        for config_files, services in sorted(unmanaged.items()):
            names = ", ".join(sorted(services))
            print(f"  {names}")
            print(f"    from: {config_files}")
            relative = config_files
            try:
                relative = str(Path(config_files).relative_to(root))
            except ValueError:
                pass
            print(f"    stop with: docker compose -f {relative} down")
        print(
            "The launcher deliberately does not stop containers it did not create."
        )
    print("\nModels, runtimes, data volumes, reports, and user configuration were preserved.")
    return 0


def _http_json(url: str, *, timeout: float = 8.0) -> tuple[bool, Any, str]:
    """GET a local endpoint without sending credentials or user content."""
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(512 * 1024).decode("utf-8", errors="replace")
        try:
            return True, json.loads(raw), ""
        except json.JSONDecodeError:
            return True, raw[:200], ""
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return False, None, type(exc).__name__


def _state_endpoints(state: Any) -> dict[str, str]:
    endpoints = state.get("endpoints") if isinstance(state, dict) else None
    if not isinstance(endpoints, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in endpoints.items()
        if isinstance(value, str) and value.startswith("http://")
    }


def _capability_summary(layout: RuntimeLayout) -> list[str]:
    payload = load_json(layout.capabilities_file, {})
    if not isinstance(payload, dict):
        return []
    lines: list[str] = []
    for entry in payload.get("models", []):
        if not isinstance(entry, dict):
            continue
        supported = sorted(
            key
            for key, value in entry.items()
            if isinstance(value, dict) and value.get("supported") is True
        )
        unsupported = sorted(
            key
            for key, value in entry.items()
            if isinstance(value, dict) and value.get("supported") is False
        )
        lines.append(
            f"  {entry.get('name', 'model')} ({entry.get('model_id', 'unknown')}): "
            f"supported={', '.join(supported) or 'none'}; "
            f"unsupported={', '.join(unsupported) or 'none'}"
        )
    return lines


def _native_endpoint_health(
    layout: RuntimeLayout, root: Path, records: Sequence[Mapping[str, Any]] | None = None
) -> list[tuple[str, bool, str]]:
    """Probe only project-owned native model listeners, on loopback."""
    results: list[tuple[str, bool, str]] = []
    if records is None:
        records = ProcessManager(root, layout.runtime_dir).status()
    for item in records:
        record = item.get("record")
        if not isinstance(record, dict) or item.get("state") != "running":
            continue
        port = record.get("port")
        if not isinstance(port, int):
            continue
        service = str(item.get("service", ""))
        # The bridge requires a bearer token; only the direct loopback model
        # listener is probed, and it is never sent user content.
        if service.endswith("-bridge"):
            results.append((service, True, "authenticated bridge listening (not probed)"))
            continue
        ok, _, error = _http_json(f"http://127.0.0.1:{port}/v1/models", timeout=5.0)
        results.append((service, ok, "" if ok else f"unreachable ({error})"))
    return results


def _cmd_status(args: argparse.Namespace, *, root: Path) -> int:
    layout = RuntimeLayout.for_project(root)
    hardware = _load_hardware(layout)
    profile = _load_profile(layout)
    state = load_json(layout.state_file, {})
    print("TechSara status")
    if hardware and profile:
        _print_selection(hardware, profile)
    else:
        print("  Bootstrap state: not configured; run ./techsara up --dry-run or ./techsara up")

    print("\nRuntime installation:")
    runtime = state.get("runtime") if isinstance(state, dict) else None
    if isinstance(runtime, dict) and runtime.get("version"):
        print(f"  vLLM-Metal {runtime.get('version')} ({runtime.get('status', 'unknown')}) at {runtime.get('path', 'unknown')}")
    elif isinstance(runtime, dict):
        print(f"  {runtime.get('status', 'not recorded')}")
    else:
        print("  not recorded")

    print("\nModel cache:")
    if hardware and hardware.selected_cache_path:
        print(f"  {hardware.selected_cache_path}")
    else:
        print("  not detected yet; run ./techsara redetect")
    for item in (state.get("models") if isinstance(state, dict) else None) or []:
        if isinstance(item, dict):
            print(f"    {str(item.get('status', '?')):16} {item.get('model_id')}@{str(item.get('revision', ''))[:12]}")

    print("\nNative processes:")
    processes = ProcessManager(root, layout.runtime_dir).status()
    if processes:
        for item in processes:
            record = item.get("record") if isinstance(item.get("record"), dict) else {}
            detail = f" pid={record.get('pid')} port={record.get('port')} health={record.get('health')}" if record else ""
            print(f"  {item['service']}: {item['state']}{detail}")
    else:
        print("  none")

    result = run_command(
        ["docker", "ps", "--filter", "label=com.docker.compose.project=sf-local-ai", "--format", "{{.Names}}\t{{.Status}}"],
        timeout=15.0,
    )
    print("\nDocker services:")
    print(result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "  none/unavailable")

    print("\nEndpoint health:")
    endpoints = _state_endpoints(state)
    if endpoints.get("orchestrator"):
        ok, payload, error = _http_json(f"{endpoints['orchestrator']}/health")
        status = payload.get("status") if isinstance(payload, dict) else None
        print(f"  orchestrator {endpoints['orchestrator']}: {status or ('reachable' if ok else f'unreachable ({error})')}")
    if endpoints.get("frontend"):
        ok, _, error = _http_json(endpoints["frontend"])
        print(f"  frontend {endpoints['frontend']}: {'reachable' if ok else f'unreachable ({error})'}")
    native = _native_endpoint_health(layout, root, processes)
    for service, ok, detail in native:
        print(f"  {service}: {'healthy' if ok else detail}")
    if not endpoints and not native:
        print("  none recorded")

    print("\nFeature capabilities:")
    if profile:
        for feature in sorted(profile.features):
            print(f"  {feature}: {'enabled' if profile.features[feature] else 'disabled'}")
        print(f"  router: {'shared with main' if profile.router_shared else 'separate endpoint'}")
    else:
        print("  unknown")

    cluster_mode = str(state.get("cluster_mode") or "single") if isinstance(state, dict) else "single"
    cluster_env = parse_env_file(layout.generated_env)
    if cluster_mode == "dual":
        print(
            f"\nCluster mode: dual (head {cluster_env.get('CLUSTER_HEAD_IP') or 'unknown'}, "
            f"worker {cluster_env.get('CLUSTER_WORKER_IP') or 'unknown'})"
        )
    else:
        print("\nCluster mode: single")
    cluster_reason = (cluster_env.get("TECHSARA_CLUSTER_REASON") or "").strip()
    if cluster_reason:
        print(f"  {cluster_reason}")

    capability_lines = _capability_summary(layout)
    print("\nRecorded capability probes:")
    for line in capability_lines or ["  none recorded"]:
        print(line)

    print("\nDegraded components:")
    reasons = list(profile.degraded_reasons) if profile else []
    for reason in reasons or ["none"]:
        print(f"  {reason}")

    if isinstance(state, dict) and state:
        print(f"\nSalesforce sync: {state.get('salesforce_sync', 'unknown')}")
        print(f"Last bootstrap: {state.get('status', 'unknown')} at {state.get('updated_at', 'unknown')}")
    return 0


#: Failing any of these means ``up`` cannot succeed; everything else is
#: reported as a degraded capability rather than a blocking prerequisite.
_BLOCKING_CHECKS = frozenset(
    {
        "Docker CLI",
        "Docker daemon",
        # `up` cannot reach Docker without socket access either, so this stands
        # in for the daemon check whenever the daemon answered but refused us.
        "Docker socket access",
        "Linux containers",
        "Docker Compose",
        "Compose env-file support",
        "Free disk",
        "Model cache writable",
        "Compose configuration",
    }
)


def _directory_writable(path: Path) -> bool:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return os.access(probe, os.W_OK | os.X_OK)


def _reachable_host(host: str, port: int = 443, *, timeout: float = 4.0) -> bool:
    """A TCP reachability check only. No request body ever leaves the host."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _doctor_cluster_checks(
    user_env: Mapping[str, str],
    checks: list[tuple[str, bool, str]],
    notes: list[str],
) -> None:
    """Read-only probes of the RoCE links and the worker host on a DGX Spark."""
    try:
        mode = resolve_cluster_mode(user_env)
    except TechSaraError as exc:
        checks.append(("Cluster mode", False, str(exc)))
        return
    if mode == "single":
        notes.append("Cluster: CLUSTER_MODE=single in .env, so the second DGX Spark was not probed.")
        return
    discovery = environment_module.CLUSTER_DISCOVERY
    worker_ip = (user_env.get("CLUSTER_WORKER_IP") or "").strip()
    if worker_ip and (user_env.get("CLUSTER_HEAD_IP") or "").strip():
        notes.append(f"Cluster: CLUSTER_HEAD_IP/CLUSTER_WORKER_IP are set in .env; discovery is skipped for {worker_ip}.")
    else:
        peer = discover_cluster_peer(discovery=discovery)
        checks.append((
            "Cluster RoCE links",
            bool(peer.links),
            "No interface is up with an IPv4 address and an RDMA device; ./techsara up runs single-node.",
        ))
        if peer.links:
            checks.append((
                "Cluster peer discovered",
                peer.found,
                f"{peer.failure}; ./techsara up runs single-node.",
            ))
        worker_ip = peer.worker_ip if peer.found else ""
    if not worker_ip:
        return
    target = (user_env.get("CLUSTER_WORKER_SSH") or "").strip() or discovery.worker_ssh(worker_ip)
    report = discovery.preflight(target)
    checks.append((
        "Cluster worker preflight",
        report.ok,
        f"{report.detail}; set up key auth for {target} or CLUSTER_MODE=single in .env.",
    ))
    if report.ok:
        notes.append(f"Cluster: second DGX Spark {report.hostname} at {worker_ip} ({target}) is ready for dual mode.")


def _cmd_doctor(args: argparse.Namespace, *, root: Path) -> int:
    offline = bool(getattr(args, "offline", False))
    layout = RuntimeLayout.for_project(root)
    hardware = detect_hardware(root, allow_network=not offline)
    checks: list[tuple[str, bool, str]] = []
    notes: list[str] = []

    checks.append(("Docker CLI", hardware.docker_installed, "Install Docker Engine or Docker Desktop, then rerun."))
    if hardware.docker_permission_denied:
        # The daemon answered; it just refused this account.  Reporting that as
        # "start Docker" sends the user to fix something that is not broken.
        checks.append(("Docker socket access", False, _docker_permission_summary()))
    else:
        checks.append(("Docker daemon", hardware.docker_running, "Start Docker Desktop/Engine, then rerun."))
    # Everything below this point is probed *through* the daemon.  While the
    # socket is unreachable those probes cannot distinguish "wrong setting" from
    # "never ran", so reporting them as failures invents problems the user does
    # not have -- a Linux host with no Docker Desktop was being told to switch
    # Docker Desktop to Linux containers.
    docker_reachable = hardware.docker_running and not hardware.docker_permission_denied
    if docker_reachable:
        checks.append(("Linux containers", hardware.docker_linux_containers, "Switch Docker Desktop to Linux containers."))
    else:
        notes.append(
            "Container checks (Linux containers, GPU access) were skipped: they need a"
            " reachable Docker daemon. Fix the Docker check above, then rerun ./techsara doctor."
        )
    checks.append(("Docker Compose", hardware.docker_compose_available, "Install Docker Compose v2.24 or newer."))
    if hardware.docker_compose_version:
        compose_recent = tuple(int(part) for part in hardware.docker_compose_version.split(".")[:2]) >= (2, 24)
        checks.append(("Compose env-file support", compose_recent, "Upgrade Docker Compose to v2.24 or newer."))

    # --- architecture, memory, disk ----------------------------------------
    checks.append((
        "Host architecture",
        hardware.native_architecture in {"arm64", "amd64"},
        f"{hardware.native_architecture} has no tested TechSara runtime profile.",
    ))
    checks.append((
        "System memory",
        hardware.total_system_memory_bytes >= 8 * GIB,
        "At least 8 GiB of system memory is required for the application services.",
    ))
    checks.append((
        "Available memory",
        hardware.available_system_memory_bytes >= 2 * GIB,
        "Free memory before starting; model selection downshifts under pressure.",
    ))
    checks.append((
        "Free disk",
        hardware.free_disk_bytes >= 2 * GIB,
        "Free at least 2 GiB, and more for the selected model set.",
    ))

    # --- accelerator runtime ------------------------------------------------
    if hardware.gpu_vendor == "nvidia" and docker_reachable:
        checks.append((
            "Container GPU",
            hardware.docker_gpu_available,
            "Install/configure the NVIDIA Container Toolkit, or enable WSL2 Docker GPU access.",
        ))
    if hardware.operating_system == "windows":
        checks.append((
            "WSL2 backend",
            hardware.windows_wsl2_available,
            "Enable WSL2 and the Docker Desktop WSL2 backend for NVIDIA acceleration.",
        ))
    if hardware.operating_system == "darwin":
        checks.append((
            "Apple Silicon",
            hardware.apple_silicon,
            "Intel Macs have no Metal inference profile; they run app-only/external modes.",
        ))
        checks.append((
            "Native arm64 shell",
            not hardware.running_under_rosetta and hardware.native_architecture == "arm64",
            "Open a native arm64 terminal; Rosetta cannot run vLLM-Metal.",
        ))

    # --- model cache and pinned runtime ------------------------------------
    cache = Path(hardware.selected_cache_path)
    checks.append((
        "Model cache writable",
        _directory_writable(cache),
        f"Create {cache} or point TECHSARA_MODEL_CACHE at a writable directory.",
    ))
    try:
        _, runtimes = load_model_manifest(root)
        checks.append(("Model manifest", True, ""))
    except (OSError, ValueError) as exc:
        runtimes = {}
        checks.append(("Model manifest", False, f"config/model-manifest.yaml is unreadable: {type(exc).__name__}"))
    if hardware.operating_system == "darwin" and hardware.apple_silicon and runtimes.get("vllm-metal"):
        manager = RuntimeManager(
            root, layout.runtime_dir, layout.shared_root / "runtimes", runtimes["vllm-metal"],
            uv_path=layout.shared_root / "bin" / "uv",
        )
        install = manager.inspect()
        checks.append((
            "Pinned vLLM-Metal runtime",
            install.ready,
            f"{install.status}: run ./techsara up to install the pinned runtime ({install.message or 'not installed yet'}).",
        ))

    # --- configuration and permissions --------------------------------------
    user_env = parse_env_file(root / ".env")
    checks.append((
        "Environment file",
        (root / ".env").is_file(),
        "Copy .env.example to .env; Salesforce fields may stay blank.",
    ))
    effective = dict(user_env)
    effective.update(parse_env_file(layout.secrets_env))
    notes.append(
        "Salesforce sync: "
        + ("credentials configured" if has_salesforce_credentials(effective) else "credentials unavailable (local UI and models still run)")
    )
    if layout.secrets_env.exists():
        private = (layout.secrets_env.stat().st_mode & 0o077) == 0
        checks.append((
            "Secret permissions",
            private,
            f"Restrict {layout.secrets_env} to the current user (chmod 600).",
        ))
    if layout.runtime_dir.exists():
        checks.append((
            "Runtime directory writable",
            _directory_writable(layout.runtime_dir),
            f"Make {layout.runtime_dir} writable by the current user.",
        ))

    # --- network -------------------------------------------------------------
    if offline:
        notes.append("Network checks skipped: --offline was requested.")
    else:
        for host in ("huggingface.co", "github.com"):
            checks.append((
                f"Network reachability ({host})",
                _reachable_host(host),
                f"{host} is unreachable; first-run downloads will fail. Use --offline with a warm cache.",
            ))

    # --- second DGX Spark (non-blocking; single-node always works) ----------
    if hardware.dgx_spark:
        _doctor_cluster_checks(user_env, checks, notes)

    # --- resolved configuration ---------------------------------------------
    profile = _load_profile(layout)
    state = load_json(layout.state_file, {})
    if profile and layout.generated_env.is_file():
        compose = _compose_from_state(layout)
        if compose is None:
            checks.append((
                "Compose configuration",
                False,
                "No recorded Compose state; run ./techsara up --dry-run.",
            ))
        else:
            try:
                compose.validate()
                checks.append(("Compose configuration", True, ""))
            except TechSaraError as exc:
                checks.append((
                    "Compose configuration",
                    False,
                    f"Run ./techsara up --dry-run for the full error ({exc}).",
                ))
    else:
        checks.append((
            "Runtime configuration",
            False,
            "Run ./techsara up --dry-run to select and validate a profile.",
        ))

    # --- live service and endpoint reachability ------------------------------
    endpoints = _state_endpoints(state)
    if endpoints.get("orchestrator"):
        ok, payload, error = _http_json(f"{endpoints['orchestrator']}/health")
        contract = isinstance(payload, dict) and isinstance(payload.get("checks"), dict)
        checks.append((
            "Orchestrator health contract",
            bool(ok and contract),
            f"Not reachable/valid at {endpoints['orchestrator']}/health ({error or 'unexpected payload'}); run ./techsara up.",
        ))
    if endpoints.get("frontend"):
        ok, _, error = _http_json(endpoints["frontend"])
        checks.append((
            "Frontend reachability",
            ok,
            f"Not reachable at {endpoints['frontend']} ({error}); run ./techsara up.",
        ))
    for service, ok, detail in _native_endpoint_health(layout, root):
        checks.append((
            f"Native model listener ({service})",
            ok,
            detail or "Run ./techsara up to restart the project-owned native model server.",
        ))

    # --- container-to-host reachability --------------------------------------
    if (
        profile
        and profile.runtime_backend == "native-vllm-metal"
        and hardware.docker_running
        and layout.generated_env.is_file()
    ):
        compose = _compose_from_state(layout)
        generated = parse_env_file(layout.generated_env)
        base = generated.get("OPENAI_BASE_URL", "")
        model = generated.get("MAIN_MODEL", "")
        if compose and base.startswith("http://host.docker.internal") and model not in {"", "disabled"}:
            try:
                compose.probe_internal_model(base, model)
                checks.append(("Container-to-host model reachability", True, ""))
            except (TechSaraError, ValueError) as exc:
                checks.append((
                    "Container-to-host model reachability",
                    False,
                    f"Containers cannot reach the native model bridge ({exc}); check the host firewall and rerun ./techsara up.",
                ))

    # --- recorded model API contracts ----------------------------------------
    for line in _capability_summary(layout):
        notes.append(line.strip())

    print("TechSara doctor")
    for name, ok, remediation in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok and remediation:
            print(f"        {remediation}")
    if notes:
        print("\nNotes:")
        for note in notes:
            print(f"  {note}")
    failed_blocking = any(not ok for name, ok, _ in checks if name in _BLOCKING_CHECKS)
    return 1 if failed_blocking else 0


def _cmd_models(args: argparse.Namespace, *, root: Path, ensure: bool = False) -> int:
    layout = RuntimeLayout.for_project(root)
    hardware = _load_hardware(layout) or detect_hardware(root)
    profile = _load_profile(layout) or select_profile(
        hardware, root, reuse_running_models=_project_has_running_models(layout)
    )
    _, runtimes = load_model_manifest(root)
    manager = _model_manager(layout, hardware, runtimes, effective_user_environment(layout))
    required = profile.required_models(skip_ocr=False)
    if ensure:
        layout.create()
        results = manager.ensure_all(required, offline=args.offline, dry_run=getattr(args, "dry_run", False))
    else:
        results = manager.status(required)
    print("TechSara models")
    for item in results:
        print(f"  {item.status:16} {item.model_id}@{item.revision[:12]}  {item.path}")
    if not results:
        print("  No local model is selected for the current degraded profile.")
    return 0


def _cmd_redetect(args: argparse.Namespace, *, root: Path) -> int:
    layout = RuntimeLayout.for_project(root)
    layout.create()
    hardware = detect_hardware(root)
    atomic_write_json(layout.hardware_file, hardware.to_dict())
    profile = select_profile(
        hardware, root, reuse_running_models=_project_has_running_models(layout)
    )
    atomic_write_json(layout.profile_file, profile.to_dict())
    _print_selection(hardware, profile)
    return 0


def _cmd_logs(args: argparse.Namespace, *, root: Path) -> int:
    layout = RuntimeLayout.for_project(root)
    secrets = effective_user_environment(layout)
    compose = _compose_from_state(layout)
    if compose:
        command = ["logs", "--no-color", "--tail", str(args.tail)]
        if args.service:
            command.append(args.service)
        result = compose.run(*command, timeout=120.0)
        print(redact(str(getattr(result, "stdout", "")), _secret_values(secrets)), end="")
    for path in sorted(layout.logs_dir.glob("*.log")) if layout.logs_dir.exists() else []:
        if args.service and path.stem != args.service:
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-args.tail :]
        print(f"\n[{path.stem}]")
        print(redact("\n".join(lines), _secret_values(secrets)))
    return 0


def _cmd_auth_bootstrap(args: argparse.Namespace, *, root: Path) -> int:
    """Establish the first SUPER_ADMIN by running the orchestrator's own
    bootstrap module inside its container.

    The password never touches argv or launcher output: an
    AUTH_BOOTSTRAP_PASSWORD the caller exported is forwarded by NAME only
    (``docker compose exec -e KEY`` resolves the value from the launcher's
    process environment), and otherwise the module prompts over the allocated
    TTY with echo off.
    """
    layout = RuntimeLayout.for_project(root)
    compose = _compose_from_state(layout)
    if compose is None:
        raise TechSaraError(
            "no launcher-managed stack is recorded here; run `./techsara up` "
            "first, then retry `./techsara auth bootstrap`"
        )
    try:
        rows = compose.ps("orchestrator")
    except TechSaraError:
        rows = []
    state = (
        str(rows[0].get("State") or rows[0].get("state") or "").lower() if rows else ""
    )
    if state != "running":
        raise TechSaraError(
            "the orchestrator container is not running"
            + (f" (state: {state})" if state else "")
            + "; run `./techsara up` first, then retry `./techsara auth bootstrap`"
        )

    interactive = sys.stdin.isatty()
    password = os.environ.get("AUTH_BOOTSTRAP_PASSWORD", "")
    if not password and not interactive:
        raise TechSaraError(
            "stdin is not a TTY and AUTH_BOOTSTRAP_PASSWORD is not set; export "
            "AUTH_BOOTSTRAP_PASSWORD for unattended use or run from an "
            "interactive terminal so the password can be prompted"
        )
    exec_args = ["exec"]
    if not interactive:
        # Compose allocates a pseudo-TTY by default; it refuses when stdin is
        # not one, so disable it and rely on the exported password instead.
        exec_args.append("-T")
    if password:
        exec_args.extend(["-e", "AUTH_BOOTSTRAP_PASSWORD"])
    exec_args.extend(
        ["orchestrator", "python", "-m", "app.authn.bootstrap", "--email", args.email]
    )
    if args.name:
        exec_args.extend(["--name", args.name])
    if args.no_adopt:
        exec_args.append("--no-adopt")

    # The same environment every other Compose invocation gets, so the
    # ${...} interpolation of the compose files stays consistent.
    env = compose._environment()
    if password:
        env["AUTH_BOOTSTRAP_PASSWORD"] = password
    try:
        # Deliberately not compose.run(): no capture and no timeout, because
        # the container process may be holding a live no-echo password prompt.
        completed = subprocess.run(
            compose.command(*exec_args), cwd=str(compose.project_root), env=env
        )
    except OSError as exc:
        raise TechSaraError(f"could not run docker compose exec: {exc}") from exc
    return completed.returncode


def _cmd_auth_invite(args: argparse.Namespace, *, root: Path) -> int:
    print(
        "Invitations are created in the web UI: sign in as an administrator,\n"
        "open /admin -> Invitations, and create one there. Each invitation\n"
        "produces a one-time accept link to hand to the new member."
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="techsara", description="Portable TechSara local AI platform launcher")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "restart"):
        item = sub.add_parser(name)
        item.add_argument("--dry-run", action="store_true")
        item.add_argument("--profile")
        item.add_argument("--model")
        item.add_argument("--skip-ocr", action="store_true")
        item.add_argument("--offline", action="store_true")
        item.add_argument("--verbose", action="store_true")
    down = sub.add_parser("down")
    down.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--offline", action="store_true")
    logs = sub.add_parser("logs")
    logs.add_argument("--tail", type=int, default=200)
    logs.add_argument("--service")
    sub.add_parser("models")
    sub.add_parser("redetect")
    update = sub.add_parser("update-models")
    update.add_argument("--offline", action="store_true")
    update.add_argument("--dry-run", action="store_true")
    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    bootstrap = auth_sub.add_parser("bootstrap")
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--name", default="")
    bootstrap.add_argument("--no-adopt", action="store_true")
    auth_sub.add_parser("invite")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = _project_root()
    try:
        if args.command == "up":
            return _cmd_up(args, root=root)
        if args.command == "restart":
            _cmd_down(argparse.Namespace(dry_run=args.dry_run), root=root)
            return _cmd_up(args, root=root)
        if args.command == "down":
            return _cmd_down(args, root=root)
        if args.command == "status":
            return _cmd_status(args, root=root)
        if args.command == "doctor":
            return _cmd_doctor(args, root=root)
        if args.command == "logs":
            if not 1 <= args.tail <= 10000:
                raise TechSaraError("--tail must be between 1 and 10000")
            return _cmd_logs(args, root=root)
        if args.command == "models":
            return _cmd_models(args, root=root)
        if args.command == "redetect":
            return _cmd_redetect(args, root=root)
        if args.command == "update-models":
            return _cmd_models(args, root=root, ensure=True)
        if args.command == "auth":
            if args.auth_command == "bootstrap":
                return _cmd_auth_bootstrap(args, root=root)
            return _cmd_auth_invite(args, root=root)
        parser.error("unknown command")
    except KeyboardInterrupt:
        print("\nTechSara operation interrupted; rerun the same command to resume safely.", file=sys.stderr)
        return 130
    except TechSaraError as exc:
        message = re.sub(
            r"(?i)(authorization\s*:\s*bearer\s+|bearer\s+)[^\s,;]+",
            lambda match: match.group(1) + "[REDACTED]",
            str(exc),
        )
        try:
            layout = RuntimeLayout.for_project(root)
            message = redact(message, _secret_values(effective_user_environment(layout)))
        except OSError:
            pass
        print(f"TechSara error: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
