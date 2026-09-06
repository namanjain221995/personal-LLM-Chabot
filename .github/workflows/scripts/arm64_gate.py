#!/usr/bin/env python3
"""Refuse an image definition that cannot be built for linux/arm64.

Production is an NVIDIA DGX Spark: aarch64. CI runs on GitHub's hosted
runners: x86_64. Every "works on my runner" bug this repository can have in a
Dockerfile is therefore invisible until deploy time, on the one machine that
must not discover it. Two cheap, network-only checks close most of that gap
without emulating a whole build:

  1. every base image referenced by a `FROM` must publish a linux/arm64
     manifest. A digest pin that resolves to a SINGLE-platform manifest is the
     nastiest version of this: it builds on the runner and cannot be pulled on
     the box at all;
  2. no Dockerfile may hardcode an x86 assumption — `--platform=linux/amd64`,
     an `x86_64`/`amd64` download URL, or an `apt` architecture suffix.

Bases that CI cannot reach (the ~20 GB CUDA image, which is built natively on
the DGX and never on a hosted runner) are declared with --skip-base and are
reported rather than silently ignored.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=(?P<platform>\S+)\s+)?(?P<image>\S+)", re.I)
STAGE_RE = re.compile(r"\s+AS\s+(?P<stage>\S+)\s*$", re.I)

#: Literal x86 assumptions. Each pattern is paired with what to do instead, so
#: a failure reads as a fix rather than as a rule.
X86_SMELLS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"--platform\s*=\s*linux/amd64", re.I),
     "pins the build to amd64; drop it, or use $TARGETPLATFORM"),
    (re.compile(r"\bx86[_-]?64\b", re.I),
     "hardcodes an x86_64 artifact; select on $TARGETARCH"),
    (re.compile(r"[-_/]amd64(\b|[._-])", re.I),
     "hardcodes an amd64 artifact; select on $TARGETARCH"),
]


def platforms_of(image: str, timeout: int) -> tuple[set[str], str]:
    """Platforms in an image's manifest, via buildx (no pull of the layers)."""
    try:
        raw = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", image],
            capture_output=True, text=True, timeout=timeout, check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        return set(), (exc.stderr or "").strip().splitlines()[-1:][0] if exc.stderr else "inspect failed"
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return set(), f"{type(exc).__name__}: {exc}"
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return set(), f"manifest is not JSON: {exc}"
    found: set[str] = set()
    for m in doc.get("manifests", []) or []:
        p = m.get("platform") or {}
        os_, arch, variant = p.get("os"), p.get("architecture"), p.get("variant") or ""
        if not os_ or not arch or arch == "unknown":
            continue
        found.add(f"{os_}/{arch}" + (f"/{variant}" if variant else ""))
    if not found and doc.get("config"):
        # A single-platform manifest: no `manifests` array at all. That is
        # exactly the case this check exists to catch.
        found.add("<single-platform manifest>")
    return found, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dockerfiles", nargs="+", type=pathlib.Path)
    ap.add_argument("--skip-base", action="append", default=[],
                    help="base image prefix CI cannot reach (built natively on the DGX)")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--require", default="linux/arm64")
    args = ap.parse_args(argv)

    failures: list[str] = []
    notes: list[str] = []
    checked: set[str] = set()

    for path in args.dockerfiles:
        if not path.is_file():
            failures.append(f"{path}: no such Dockerfile")
            continue
        text = path.read_text(encoding="utf-8")
        stages: set[str] = set()

        for lineno, line in enumerate(text.splitlines(), 1):
            m = FROM_RE.match(line)
            if m:
                image = m.group("image")
                platform = m.group("platform")
                st = STAGE_RE.search(line)
                if st:
                    stages.add(st.group("stage"))
                if platform and "arm64" not in platform:
                    failures.append(
                        f"{path}:{lineno}: FROM --platform={platform} pins a "
                        f"non-arm64 platform"
                    )
                if image in stages or image.startswith("$"):
                    continue  # an earlier stage in this same file, or a build arg
                if any(image.startswith(p) for p in args.skip_base):
                    notes.append(f"{path}:{lineno}: {image} NOT checked (declared unreachable from CI)")
                    continue
                if image in checked:
                    continue
                checked.add(image)
                plats, err = platforms_of(image, args.timeout)
                if err:
                    failures.append(f"{path}:{lineno}: cannot inspect {image}: {err}")
                elif args.require not in plats and not any(
                    p.startswith(args.require + "/") for p in plats
                ):
                    failures.append(
                        f"{path}:{lineno}: {image} does not publish {args.require} "
                        f"(has: {sorted(plats) or 'nothing'}). Production is aarch64."
                    )
                else:
                    print(f"  ok  {path}:{lineno} {image.split('@')[0]} -> {sorted(plats)}")
                continue

            for pattern, advice in X86_SMELLS:
                if pattern.search(line) and not line.lstrip().startswith("#"):
                    failures.append(f"{path}:{lineno}: {line.strip()[:90]}\n        {advice}")

    for note in notes:
        print(f"  note {note}")
    if failures:
        print(f"\narm64 gate: {len(failures)} finding(s)", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("\narm64 gate: OK — every reachable base image publishes linux/arm64 "
          "and no Dockerfile hardcodes an x86 artifact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
