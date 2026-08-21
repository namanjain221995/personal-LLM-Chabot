"""Shared, dependency-free helpers for launcher unit tests.

The launcher tests are intentionally runnable with the Python standard library.
They never inspect the real Docker daemon, GPU, network, or process table.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_ROOT = REPO_ROOT / "launcher"
if str(LAUNCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_ROOT))

GIB = 1024**3


class CommandStub:
    """Record argv calls and return responses supplied by a test handler."""

    def __init__(
        self,
        handler: Callable[[tuple[str, ...], float], tuple[int, str, str]] | None = None,
    ) -> None:
        self.handler = handler or (lambda _args, _timeout: (127, "", "not available"))
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, args: Sequence[str], timeout: float) -> tuple[int, str, str]:
        normalized = tuple(str(arg) for arg in args)
        self.calls.append((normalized, timeout))
        return self.handler(normalized, timeout)

    def called(self, *args: str) -> bool:
        expected = tuple(args)
        return any(actual == expected for actual, _timeout in self.calls)


class ReadTextStub:
    """A strict fake filesystem reader for Linux pseudo-files."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)
        self.calls: list[str] = []

    def __call__(self, path: str) -> str:
        self.calls.append(path)
        if path not in self.values:
            raise OSError(f"fixture has no value for {path}")
        return self.values[path]


def linux_files(*, total_gib: int = 128, available_gib: int = 96) -> ReadTextStub:
    return ReadTextStub(
        {
            "/proc/meminfo": (
                f"MemTotal:       {total_gib * GIB // 1024} kB\n"
                f"MemAvailable:   {available_gib * GIB // 1024} kB\n"
            ),
            "/proc/cpuinfo": "model name : Fixture CPU\n",
            "/etc/os-release": 'PRETTY_NAME="Fixture Linux 1"\n',
            "/sys/class/dmi/id/product_name": "Fixture Workstation\n",
            "/sys/class/dmi/id/sys_vendor": "Fixture Vendor\n",
        }
    )


def docker_handler(
    *,
    running: bool = True,
    denied: bool = False,
    linux_containers: bool = True,
    compose: bool = True,
    model_status: str = "",
    gpu_smoke: bool = False,
    cached_images: Sequence[str] | None = None,
    pullable: bool = True,
    extra: Callable[[tuple[str, ...], float], tuple[int, str, str] | None] | None = None,
) -> Callable[[tuple[str, ...], float], tuple[int, str, str]]:
    """Build a Docker command handler without invoking Docker.

    ``cached_images`` is ``None`` for the common case where every candidate
    probe image is already on the host; pass an explicit (possibly empty)
    sequence to model a clean clone that has to pull the pinned tiny probe.

    ``denied`` models a *running* daemon whose socket this account may not open,
    which Docker reports with the same non-zero exit as a stopped daemon.
    """
    pulled: set[str] = set()

    def handle(args: tuple[str, ...], timeout: float) -> tuple[int, str, str]:
        if extra is not None:
            response = extra(args, timeout)
            if response is not None:
                return response
        if args == ("docker", "version", "--format", "{{json .}}"):
            if denied:
                return (
                    1,
                    '{"Client":{"Version":"29.2.1"},"Server":null}',
                    "permission denied while trying to connect to the docker API"
                    " at unix:///var/run/docker.sock",
                )
            if not running:
                return 1, "", "daemon unavailable"
            return (
                0,
                '{"Server":{"Arch":"amd64","Version":"99.0",'
                '"Platform":{"Name":"Docker Desktop"}}}',
                "",
            )
        if args == ("docker", "info", "--format", "{{.OSType}}"):
            if denied:
                return 1, "", "permission denied while trying to connect to the docker API"
            return (0, "linux" if linux_containers else "windows", "") if running else (1, "", "")
        if args == ("docker", "compose", "version"):
            return (0, "Docker Compose fixture", "") if compose else (1, "", "")
        if args == ("docker", "model", "status"):
            return (0, model_status, "") if model_status else (1, "", "not installed")
        if args[:3] == ("docker", "image", "inspect"):
            image = args[-1]
            present = image in pulled or (cached_images is None or image in cached_images)
            return (0, "sha256:fixture", "") if present else (1, "", "No such image")
        if args[:2] == ("docker", "pull"):
            if not pullable:
                return 1, "", "pull failed"
            pulled.add(args[-1])
            return 0, args[-1], ""
        if len(args) > 2 and args[:2] == ("docker", "run"):
            return (0, "TECHSARA_GPU_OK", "") if gpu_smoke else (1, "", "no GPU")
        return 127, "", "fixture command not found"

    return handle

