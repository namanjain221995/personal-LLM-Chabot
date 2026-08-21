"""Strongly typed, cross-platform host and accelerator detection."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

from .utils import GIB, disk_free, run_command

HARDWARE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class HardwareInfo:
    schema_version: int = HARDWARE_SCHEMA_VERSION
    operating_system: str = "unknown"
    operating_system_version: str = "unknown"
    host_architecture: str = "unknown"
    native_architecture: str = "unknown"
    docker_server_architecture: str = ""
    cpu_name: str = "unknown"
    cpu_core_count: int = 1
    total_system_memory_bytes: int = 0
    available_system_memory_bytes: int = 0
    gpu_vendor: str = "none"
    gpu_name: str = ""
    gpu_count: int = 0
    gpu_total_memory_bytes: int = 0
    gpu_available_memory_bytes: int = 0
    nvidia_compute_capability: str = ""
    docker_gpu_available: bool = False
    apple_silicon: bool = False
    apple_chip_name: str = ""
    apple_unified_memory_bytes: int = 0
    running_under_rosetta: bool = False
    running_under_wsl2: bool = False
    windows_wsl2_available: bool = False
    docker_linux_containers: bool = False
    docker_installed: bool = False
    docker_running: bool = False
    docker_permission_denied: bool = False
    docker_compose_available: bool = False
    docker_compose_version: str = ""
    docker_desktop_version: str = ""
    docker_model_runner_available: bool = False
    docker_model_runner_vllm_metal_available: bool = False
    nvidia_driver_available: bool = False
    nvidia_container_toolkit_available: bool = False
    dgx_spark: bool = False
    free_disk_bytes: int = 0
    selected_cache_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict) -> "HardwareInfo":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: values[key] for key in allowed if key in values})


Command = Callable[[Sequence[str], float], tuple[int, str, str]]


def _system_command(args: Sequence[str], timeout: float = 15.0) -> tuple[int, str, str]:
    result = run_command(args, timeout=timeout)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _normalize_arch(value: str) -> str:
    arch = value.strip().lower()
    return {"x86_64": "amd64", "x64": "amd64", "aarch64": "arm64"}.get(arch, arch or "unknown")


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _linux_memory(read_text: Callable[[str], str]) -> tuple[int, int]:
    try:
        content = read_text("/proc/meminfo")
    except OSError:
        return 0, 0
    values: dict[str, int] = {}
    for line in content.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)\s+kB", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    return values.get("MemTotal", 0), values.get("MemAvailable", values.get("MemFree", 0))


def _linux_cpu_name(read_text: Callable[[str], str]) -> str:
    try:
        content = read_text("/proc/cpuinfo")
    except OSError:
        return platform.processor() or "unknown"
    for key in ("model name", "Model", "Processor", "Hardware"):
        match = re.search(rf"^{re.escape(key)}\s*:\s*(.+)$", content, re.M)
        if match:
            return match.group(1).strip()
    return platform.processor() or "unknown"


def _parse_nvidia_csv(text: str) -> tuple[str, int, int, int, str]:
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    if not rows:
        return "", 0, 0, 0, ""
    names: list[str] = []
    totals: list[int] = []
    frees: list[int] = []
    capabilities: list[str] = []
    for row in rows:
        parts = [part.strip() for part in row.split(",")]
        names.append(parts[0])
        if len(parts) > 1 and parts[1].isdigit():
            totals.append(int(parts[1]) * 1024**2)
        if len(parts) > 2 and parts[2].isdigit():
            frees.append(int(parts[2]) * 1024**2)
        if len(parts) > 3 and parts[3] not in {"N/A", "[N/A]"}:
            capabilities.append(parts[3])
    unique_names = list(dict.fromkeys(names))
    def capability_key(value: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in value.split("."))
        except ValueError:
            return (0,)

    return ", ".join(unique_names), len(rows), sum(totals), sum(frees), max(capabilities, key=capability_key, default="")


# A stopped daemon and an unreadable socket are different host problems with
# different fixes, but both make `docker version` exit non-zero.  Docker reports
# the second as a permission error on the endpoint it tried ("permission denied"
# on a unix socket, "access is denied" on a Windows named pipe), so the stderr
# text is the only signal that separates "start Docker" from "grant this account
# access to Docker".
_DOCKER_DENIED_PATTERN = re.compile(
    r"permission denied|access is denied|operation not permitted", re.I
)


def _docker_permission_denied(stderr: str) -> bool:
    text = stderr.strip()
    if not text:
        return False
    return bool(_DOCKER_DENIED_PATTERN.search(text))


def _docker_details(command: Command) -> dict:
    details = {
        "installed": shutil.which("docker") is not None,
        "running": False,
        "permission_denied": False,
        "compose": False,
        "compose_version": "",
        "server_arch": "",
        "desktop_version": "",
        "linux_containers": False,
        "model_runner": False,
        "model_runner_vllm_metal": False,
    }
    if not details["installed"]:
        return details
    code, output, error = command(
        ["docker", "version", "--format", "{{json .}}"], 12.0
    )
    if code == 0:
        details["running"] = True
        try:
            payload = json.loads(output)
            server = payload.get("Server") or {}
            details["server_arch"] = _normalize_arch(server.get("Arch", ""))
            platform_name = str((server.get("Platform") or {}).get("Name", ""))
            details["desktop_version"] = str(server.get("Version", "")) if "desktop" in platform_name.lower() else ""
        except (json.JSONDecodeError, AttributeError):
            pass
        code_info, info, _ = command(
            ["docker", "info", "--format", "{{.OSType}}"], 12.0
        )
        details["linux_containers"] = code_info == 0 and info.strip().lower() == "linux"
    else:
        details["permission_denied"] = _docker_permission_denied(error)
    code, compose_output, _ = command(["docker", "compose", "version"], 8.0)
    details["compose"] = code == 0
    if code == 0:
        match = re.search(r"(?:v|version\s+)(\d+\.\d+(?:\.\d+)?)", compose_output, re.I)
        details["compose_version"] = match.group(1) if match else ""
    code, status, _ = command(["docker", "model", "status"], 8.0)
    if code == 0:
        details["model_runner"] = "running" in status.lower()
        # Docker currently documents vLLM only for Linux NVIDIA/Windows WSL2.
        # This remains false unless a future runner explicitly reports both.
        lower = status.lower()
        details["model_runner_vllm_metal"] = "vllm" in lower and "metal" in lower and "running" in lower
    return details


# A revision-pinned ~4 MiB image used only when no larger candidate is already
# cached. Detection must not require a multi-gigabyte runtime image to be
# present before the launcher is allowed to notice that Docker can reach a GPU.
GPU_PROBE_IMAGE = (
    "busybox@sha256:3c6ae8008e2c2eedd141725c30b20d9c36b026eb796688f88205845ef17aa213"
)
_GPU_PROBE_MARKER = "TECHSARA_GPU_OK"
# Any Linux image exposes /bin/sh, so one probe shape covers the tiny image and
# the large CUDA runtimes alike. The NVIDIA container runtime injects the
# control device only when it has really attached the GPU to the container.
_GPU_PROBE_SCRIPT = f"ls /dev/nvidiactl >/dev/null 2>&1 && echo {_GPU_PROBE_MARKER}"


def _image_present(command: Command, image: str) -> bool:
    code, _, _ = command(["docker", "image", "inspect", "--format", "{{.Id}}", image], 20.0)
    return code == 0


def _run_gpu_probe(command: Command, image: str) -> bool:
    code, output, _ = command(
        [
            "docker", "run", "--rm", "--pull", "never", "--gpus", "all",
            "--entrypoint", "/bin/sh", image, "-c", _GPU_PROBE_SCRIPT,
        ],
        60.0,
    )
    return code == 0 and _GPU_PROBE_MARKER in output


def _docker_gpu_smoke(
    command: Command, *, images: Sequence[str], allow_pull: bool = True
) -> bool:
    """Prove Docker really attaches the GPU, preferring already-cached images."""
    candidates = [image for image in images if image]
    for image in candidates:
        if _image_present(command, image) and _run_gpu_probe(command, image):
            return True
    if not allow_pull or not candidates:
        return False
    probe = candidates[-1]
    if _image_present(command, probe):
        return False  # already tried above; a second run would not differ
    code, _, _ = command(["docker", "pull", "--quiet", probe], 300.0)
    if code != 0:
        return False
    return _run_gpu_probe(command, probe)


def _detect_dgx_spark(
    *, arch: str, gpu_name: str, total_memory: int, cpu_name: str, system_metadata: str
) -> bool:
    signals = 0
    if arch == "arm64":
        signals += 1
    # DMI spells this host "NVIDIA_DGX_Spark", so the separator class must allow
    # underscores and hyphens; `\s*` alone silently loses the strongest signal.
    if re.search(r"\bGB10\b|DGX[\s_-]*Spark", gpu_name, re.I):
        signals += 2
    if total_memory >= 96 * GIB:
        signals += 1
    if re.search(r"DGX[\s_-]*Spark|NVIDIA", f"{cpu_name} {system_metadata}", re.I):
        signals += 1
    return signals >= 4


def _mac_data(command: Command) -> dict:
    def sysctl(name: str) -> str:
        code, value, _ = command(["sysctl", "-n", name], 5.0)
        return value.strip() if code == 0 else ""

    chip = ""
    code, payload, _ = command(["system_profiler", "SPHardwareDataType", "-json"], 15.0)
    if code == 0:
        try:
            rows = json.loads(payload).get("SPHardwareDataType", [])
            if rows:
                chip = str(rows[0].get("chip_type") or rows[0].get("cpu_type") or "")
        except (json.JSONDecodeError, AttributeError, IndexError):
            pass
    total_raw = sysctl("hw.memsize")
    page_size_raw = sysctl("hw.pagesize")
    total = _safe_int(total_raw, 0)
    page_size = _safe_int(page_size_raw, 4096 if not page_size_raw else 0)
    code, vm, _ = command(["vm_stat"], 5.0)
    available = 0
    if code == 0:
        pages = {}
        for line in vm.splitlines():
            match = re.match(r"^([^:]+):\s+(\d+)\.?$", line.strip())
            if match:
                pages[match.group(1)] = int(match.group(2))
        available = page_size * sum(
            pages.get(key, 0)
            for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
        )
    translated = sysctl("sysctl.proc_translated") == "1"
    native_arm = sysctl("hw.optional.arm64") == "1"
    return {
        "chip": chip or sysctl("machdep.cpu.brand_string"),
        "total": total,
        "available": available,
        "translated": translated,
        "native_arch": "arm64" if native_arm else _normalize_arch(platform.machine()),
    }


def _windows_data(command: Command) -> dict:
    script = (
        "$ErrorActionPreference='Stop';"
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$cpu=Get-CimInstance Win32_Processor|Select-Object -First 1;"
        "$gpu=Get-CimInstance Win32_VideoController;"
        "[pscustomobject]@{caption=$os.Caption;version=$os.Version;"
        "total=[int64]$os.TotalVisibleMemorySize*1024;"
        "free=[int64]$os.FreePhysicalMemory*1024;cpu=$cpu.Name;"
        "cores=[int]$cpu.NumberOfLogicalProcessors;gpus=@($gpu|ForEach-Object{"
        "[pscustomobject]@{name=$_.Name;ram=[int64]$_.AdapterRAM}})}|ConvertTo-Json -Depth 4 -Compress"
    )
    code, output, _ = command(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], 20.0)
    data: dict = {}
    if code == 0:
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            data = {}
    code, wsl, _ = command(["wsl.exe", "--status"], 8.0)
    data["wsl2"] = code == 0 and bool(
        re.search(r"(?im)^\s*default\s+version\s*:\s*2\s*$", wsl)
        or re.search(r"(?i)\bWSL\s*2\b", wsl)
    )
    return data


def choose_cache_path(project_root: Path, env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    override = env.get("TECHSARA_MODEL_CACHE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    legacy = env.get("VLLM_MODELS_DIR", "").strip()
    candidates = [Path(legacy).expanduser()] if legacy else []
    candidates.append(project_root.parent / "vllm_models")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    system = platform.system().lower()
    if system == "darwin":
        return (Path.home() / "Library" / "Caches" / "TechSara" / "models").resolve()
    if system == "windows":
        base = Path(env.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return (base / "TechSara" / "models").resolve()
    base = Path(env.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return (base / "techsara" / "models").resolve()


def detect_hardware(
    project_root: Path,
    *,
    command: Command = _system_command,
    read_text: Callable[[str], str] | None = None,
    system: str | None = None,
    machine: str | None = None,
    environ: dict[str, str] | None = None,
    allow_network: bool = True,
) -> HardwareInfo:
    """Detect normalized host capabilities without changing the host.

    ``allow_network`` is cleared by ``--offline``; the GPU probe then uses only
    images that are already cached instead of pulling the pinned tiny probe.
    """
    read_text = read_text or (lambda path: Path(path).read_text(encoding="utf-8", errors="replace"))
    os_name = system or platform.system()
    host_arch = _normalize_arch(machine or platform.machine())
    cache = choose_cache_path(project_root, environ)
    docker = _docker_details(command)
    values: dict = {
        "operating_system": os_name.lower(),
        "operating_system_version": platform.version(),
        "host_architecture": host_arch,
        "native_architecture": host_arch,
        "docker_server_architecture": docker["server_arch"],
        "cpu_core_count": os.cpu_count() or 1,
        "docker_installed": docker["installed"],
        "docker_running": docker["running"],
        "docker_permission_denied": docker["permission_denied"],
        "docker_compose_available": docker["compose"],
        "docker_compose_version": docker["compose_version"],
        "docker_desktop_version": docker["desktop_version"],
        "docker_linux_containers": docker["linux_containers"],
        "docker_model_runner_available": docker["model_runner"],
        "docker_model_runner_vllm_metal_available": docker["model_runner_vllm_metal"],
        "selected_cache_path": str(cache),
        "free_disk_bytes": disk_free(cache),
    }

    lower = os_name.lower()
    metadata = ""
    if lower == "darwin":
        mac = _mac_data(command)
        values.update(
            operating_system_version=platform.mac_ver()[0] or platform.release(),
            cpu_name=mac["chip"] or "Apple Silicon",
            total_system_memory_bytes=mac["total"],
            available_system_memory_bytes=mac["available"],
            apple_silicon=mac["native_arch"] == "arm64",
            apple_chip_name=mac["chip"],
            apple_unified_memory_bytes=mac["total"] if mac["native_arch"] == "arm64" else 0,
            running_under_rosetta=mac["translated"],
            native_architecture=mac["native_arch"],
            gpu_vendor="apple" if mac["native_arch"] == "arm64" else "none",
            gpu_name=mac["chip"] if mac["native_arch"] == "arm64" else "",
            gpu_count=1 if mac["native_arch"] == "arm64" else 0,
            gpu_total_memory_bytes=mac["total"] if mac["native_arch"] == "arm64" else 0,
            gpu_available_memory_bytes=mac["available"] if mac["native_arch"] == "arm64" else 0,
        )
        # Official Docker documentation currently supports llama.cpp, not
        # vLLM-Metal, on macOS. Do not infer support merely from DMR presence.
        if not docker["model_runner_vllm_metal"]:
            values["docker_model_runner_vllm_metal_available"] = False
    elif lower == "windows":
        win = _windows_data(command)
        gpus = win.get("gpus") or []
        if isinstance(gpus, dict):
            gpus = [gpus]
        nvidia = [gpu for gpu in gpus if "nvidia" in str(gpu.get("name", "")).lower()]
        code, nvidia_out, _ = command(
            [
                "nvidia-smi", "--query-gpu=name,memory.total,memory.free,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            12.0,
        )
        measured_name, measured_count, measured_total, measured_free, measured_capability = (
            _parse_nvidia_csv(nvidia_out) if code == 0 else ("", 0, 0, 0, "")
        )
        values.update(
            operating_system_version=str(win.get("version") or platform.version()),
            cpu_name=str(win.get("cpu") or platform.processor() or "unknown"),
            cpu_core_count=max(1, _safe_int(win.get("cores"), os.cpu_count() or 1)),
            total_system_memory_bytes=_safe_int(win.get("total"), 0),
            available_system_memory_bytes=_safe_int(win.get("free"), 0),
            gpu_vendor="nvidia" if nvidia or measured_count else "none",
            gpu_name=measured_name or ", ".join(str(g.get("name", "")) for g in nvidia),
            gpu_count=measured_count or len(nvidia),
            gpu_total_memory_bytes=measured_total or sum(_safe_int(g.get("ram"), 0) for g in nvidia),
            gpu_available_memory_bytes=measured_free,
            nvidia_compute_capability=measured_capability,
            nvidia_driver_available=code == 0,
            windows_wsl2_available=bool(win.get("wsl2")),
        )
    else:
        total, available = _linux_memory(read_text)
        try:
            release = read_text("/etc/os-release")
            match = re.search(r'^PRETTY_NAME="?([^"\n]+)', release, re.M)
            os_version = match.group(1) if match else platform.release()
        except OSError:
            os_version = platform.release()
        cpu_name = _linux_cpu_name(read_text)
        kernel = platform.release()
        try:
            metadata = " ".join(
                read_text(path).strip()
                for path in ("/sys/class/dmi/id/product_name", "/sys/class/dmi/id/sys_vendor")
            )
        except OSError:
            metadata = ""
        code, nvidia_out, _ = command(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            12.0,
        )
        gpu_name, gpu_count, gpu_total, gpu_free, capability = (
            _parse_nvidia_csv(nvidia_out) if code == 0 else ("", 0, 0, 0, "")
        )
        values.update(
            operating_system_version=os_version,
            cpu_name=cpu_name,
            total_system_memory_bytes=total,
            available_system_memory_bytes=available,
            running_under_wsl2="microsoft" in kernel.lower(),
            gpu_vendor="nvidia" if gpu_count else "none",
            gpu_name=gpu_name,
            gpu_count=gpu_count,
            gpu_total_memory_bytes=gpu_total,
            gpu_available_memory_bytes=gpu_free,
            nvidia_compute_capability=capability,
            nvidia_driver_available=code == 0,
        )

    if values.get("gpu_vendor") == "nvidia" and docker["running"] and docker["linux_containers"]:
        override = (environ or os.environ).get("TECHSARA_GPU_SMOKE_IMAGE", "").strip()
        smoke_images = [override] if override else [
            # Prefer an image the host already has, so ordinary re-detection
            # never pulls. The pinned tiny probe is the clean-clone fallback.
            "nvcr.io/nvidia/vllm@sha256:654e563e727be1968487d453de0733fb074cb59adf424c779d0f9e7cfcb2b6b6",
            "vllm/vllm-openai@sha256:24f2f8975d011ea7f7066a547886a08a1fd3c4bf0880463487fae4f01ce723c6",
            GPU_PROBE_IMAGE,
        ]
        gpu_ok = _docker_gpu_smoke(command, images=smoke_images, allow_pull=allow_network)
        values["docker_gpu_available"] = gpu_ok
        values["nvidia_container_toolkit_available"] = gpu_ok

    if values.get("gpu_vendor") == "nvidia":
        values["dgx_spark"] = _detect_dgx_spark(
            arch=values["native_architecture"],
            gpu_name=values.get("gpu_name", ""),
            total_memory=values.get("total_system_memory_bytes", 0),
            cpu_name=values.get("cpu_name", ""),
            system_metadata=metadata,
        )
        if values["dgx_spark"] and not values.get("gpu_total_memory_bytes"):
            # GB10 reports N/A VRAM because CPU and GPU share one physical pool.
            values["gpu_total_memory_bytes"] = values.get("total_system_memory_bytes", 0)
            values["gpu_available_memory_bytes"] = values.get("available_system_memory_bytes", 0)

    return HardwareInfo(**values)
