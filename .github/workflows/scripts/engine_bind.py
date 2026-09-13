#!/usr/bin/env python3
"""Where the main vLLM engine was told to listen, and proof it listens nowhere else.

WHY (audit F065, 2026-09-12; this half closed 2026-09-13)
---------------------------------------------------------
The raw vLLM OpenAI server has no authentication. Anyone who can reach its
port bypasses every `/v1` key, quota and usage record the orchestrator
enforces. The audit found it on `0.0.0.0:8000` — every interface of the
production head, the office LAN and the tailnet included — and found the
`verify` job blind to it: the job curled `127.0.0.1:8000`, which answers the
same whether the engine is on loopback or on every interface.

The launcher now generates `CLUSTER_API_BIND_ADDRESS` as the docker bridge
gateway (e.g. 172.17.0.1) for the host-network cluster head. That fixes the
exposure, and it also breaks the old probe: an engine on 172.17.0.1 does not
answer on 127.0.0.1. So this script does two things for `verify`:

  resolve  reads CLUSTER_API_BIND_ADDRESS and VLLM_PORT from the deploy root's
           `.runtime/generated.env` (127.0.0.1 and 8000 when absent — the
           single-node shape, where compose publishes the engine on loopback)
           and hands the probe URL to the later steps;

  check    reads the host's real listening sockets (`ss -ltnH`) and FAILS if
           anything on the engine port is bound to a wildcard (0.0.0.0, ::, *)
           or to any address other than the configured one and loopback. It
           also fails if the CONFIGURATION asks for a wildcard, because that is
           the regression arriving before the container is recreated.

The live check is what matters: generated.env is what the launcher intended,
`ss` is what the kernel is doing. A bind that "returns" through a hand-edited
env file, a compose override or a `docker run -p 8000:8000` is only visible to
the second.

Operator note: until OA-3 in docs/developer-platform/DISPOSITION.md recreates
the engine, production still listens on 0.0.0.0:8000 and `check` is RED on
every deploy. That is the correct reading of the box's state, not a flaky gate.

Usage:
    engine_bind.py resolve --generated-env PATH [--github-output FILE]
    engine_bind.py check   --generated-env PATH [--ss-output FILE]
"""
from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import pathlib
import shutil
import subprocess
import sys

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: Every spelling `ss` and a config file use for "all interfaces".
WILDCARDS = frozenset({"0.0.0.0", "::", "[::]", "*", "::0", "0:0:0:0:0:0:0:0"})


@dataclasses.dataclass(frozen=True)
class Target:
    host: str
    port: int
    configured: bool  # False when the key was absent and the default applied

    @property
    def url(self) -> str:
        """Where to PROBE. A wildcard is not an address to connect to: probe
        loopback so the endpoint evidence is still collected, and let `check`
        be the step that fails on the wildcard itself."""
        host = DEFAULT_HOST if is_wildcard(self.host) else self.host
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{self.port}"


def read_env(path: pathlib.Path) -> dict[str, str]:
    """KEY=VALUE lines; comments, blanks and surrounding quotes stripped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def resolve(env: dict[str, str]) -> Target:
    host = env.get("CLUSTER_API_BIND_ADDRESS", "").strip()
    port_text = env.get("VLLM_PORT", "").strip()
    try:
        port = int(port_text) if port_text else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    return Target(host=host or DEFAULT_HOST, port=port, configured=bool(host))


def _normalise(address: str) -> str:
    address = address.strip()
    if address.startswith("[") and address.endswith("]"):
        address = address[1:-1]
    address = address.split("%", 1)[0]  # `0.0.0.0%eth0` is still every address on eth0
    return address


def is_wildcard(address: str) -> bool:
    bare = _normalise(address)
    if address.strip() in WILDCARDS or bare in WILDCARDS:
        return True
    try:
        return ipaddress.ip_address(bare).is_unspecified
    except ValueError:
        return False


def is_loopback(address: str) -> bool:
    bare = _normalise(address)
    if bare == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    # `::ffff:127.0.0.1` is loopback too, and ipaddress does not say so itself.
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or bool(mapped and mapped.is_loopback)


def listeners(ss_text: str, port: int) -> list[str]:
    """Local addresses listening on `port`, from `ss -ltnH` output."""
    found = []
    for line in ss_text.splitlines():
        fields = line.split()
        # State Recv-Q Send-Q Local:Port Peer:Port [Process]
        if len(fields) < 5:
            continue
        local = fields[3]
        address, sep, local_port = local.rpartition(":")
        if not sep or local_port != str(port):
            continue
        found.append(address)
    return found


def violations(target: Target, ss_text: str) -> list[str]:
    problems = []
    if is_wildcard(target.host):
        problems.append(
            f"CLUSTER_API_BIND_ADDRESS={target.host} asks the engine to listen on EVERY "
            "interface. The raw vLLM API has no authentication; regenerate it "
            "(`./techsara redetect`) so it names the docker bridge gateway."
        )
    bound = listeners(ss_text, target.port)
    if not bound:
        problems.append(
            f"nothing is listening on port {target.port}, so the bind cannot be proved "
            "(the engine is down, or VLLM_PORT does not match what it serves)"
        )
    for address in bound:
        if is_wildcard(address):
            problems.append(
                f"the engine port is bound to {address}:{target.port} — every interface. "
                "Anyone on the LAN or tailnet can call the unauthenticated model API "
                "and bypass every /v1 key and quota (audit F065)."
            )
        elif _normalise(address) != _normalise(target.host) and not is_loopback(address):
            problems.append(
                f"the engine port is bound to {address}:{target.port}, which is neither the "
                f"configured {target.host} nor loopback"
            )
    return problems


def _ss_text(path: str | None) -> str:
    if path:
        return pathlib.Path(path).read_text(encoding="utf-8")
    if shutil.which("ss") is None:
        raise SystemExit("FATAL: `ss` is not on PATH, so the engine's listening address cannot be read")
    proc = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise SystemExit(f"FATAL: `ss -ltnH` failed: {proc.stderr.strip()}")
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("resolve", "check"):
        p = sub.add_parser(name)
        p.add_argument("--generated-env", required=True)
        if name == "resolve":
            p.add_argument("--github-output", default=None)
        else:
            p.add_argument("--ss-output", default=None, help="read `ss -ltnH` output from a file instead")
    args = ap.parse_args(argv)

    env_path = pathlib.Path(args.generated_env)
    target = resolve(read_env(env_path))
    source = (
        f"CLUSTER_API_BIND_ADDRESS from {env_path}"
        if target.configured
        else f"default ({'no CLUSTER_API_BIND_ADDRESS in' if env_path.is_file() else 'no'} {env_path})"
    )

    if args.command == "resolve":
        print(f"main engine: {target.url}  [{source}]")
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as fh:
                fh.write(f"host={target.host}\nport={target.port}\nurl={target.url}\n")
        return 0

    ss_text = _ss_text(args.ss_output)
    problems = violations(target, ss_text)
    bound = listeners(ss_text, target.port)
    if problems:
        print(f"main engine bind check FAILED for {target.url} [{source}]:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"main engine listens only where configured: {target.url} [{source}]; bound to {', '.join(bound)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
