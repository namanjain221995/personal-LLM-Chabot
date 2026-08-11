"""Revision-pinned, resumable and idempotent local model installation."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .errors import OfflineError, TechSaraError
from .profiles import ModelSpec
from .utils import (
    GIB,
    FileLock,
    atomic_write_json,
    disk_free,
    load_json,
    redact,
    run_command,
    sha256_file,
    slug_model,
    validate_revision,
)

MODEL_COMPLETION_SCHEMA = 1


@dataclass(frozen=True)
class ModelInstall:
    status: str
    path: str
    model_id: str
    revision: str
    source: str
    message: str = ""

    @property
    def ready(self) -> bool:
        return self.status in {"complete", "legacy-complete"}

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


Runner = Callable[..., object]


def _tree_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return total
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _manifest_fingerprint(model: ModelSpec) -> str:
    value = {
        "id": model.id,
        "revision": model.revision,
        "backend": model.backend,
        "required_files": list(model.required_files),
        "file_sha256": dict(sorted(model.file_sha256.items())),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ModelManager:
    """Manage models beneath a portable cache without deleting old revisions."""

    def __init__(
        self,
        project_root: Path,
        cache_root: Path,
        locks_dir: Path,
        *,
        uv_path: Path | str | None = None,
        environ: Mapping[str, str] | None = None,
        runner: Runner = run_command,
        downloader_version: str = "0.36.0",
    ) -> None:
        self.project_root = project_root.resolve()
        self.cache_root = cache_root.expanduser().resolve()
        self.repos_root = self.cache_root / "repos"
        self.locks_dir = locks_dir.resolve()
        self.uv_path = str(uv_path or self._default_uv())
        self.environ = dict(environ or os.environ)
        self.runner = runner
        self.downloader_version = downloader_version

    def _default_uv(self) -> Path:
        suffix = ".exe" if os.name == "nt" else ""
        return Path.home() / ".techsara" / "bin" / f"uv{suffix}"

    def destination(self, model: ModelSpec) -> Path:
        validate_revision(model.revision)
        return self.repos_root / f"{slug_model(model.id)}--{model.revision[:12]}"

    def _managed_path_safe(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.cache_root)
        except ValueError:
            return False
        cursor = self.cache_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return False
        return True

    def staging(self, model: ModelSpec) -> Path:
        return self.destination(model).with_name(self.destination(model).name + ".partial")

    @staticmethod
    def _required_matches(path: Path, model: ModelSpec) -> tuple[bool, list[str]]:
        missing: list[str] = []
        files = [item for item in path.rglob("*") if item.is_file() and not item.is_symlink()]
        relative = [item.relative_to(path).as_posix() for item in files]
        for pattern in model.required_files:
            if not any(fnmatch.fnmatch(name, pattern) for name in relative):
                missing.append(pattern)
        return not missing, missing

    @staticmethod
    def _checksums_valid(path: Path, model: ModelSpec) -> tuple[bool, str]:
        for relative, expected in model.file_sha256.items():
            candidate = path / relative
            if not candidate.is_file() or candidate.is_symlink():
                return False, f"checksum target is missing: {relative}"
            if sha256_file(candidate) != expected.lower():
                return False, f"checksum mismatch: {relative}"
        return True, ""

    def _valid_complete(self, path: Path, model: ModelSpec) -> tuple[bool, str]:
        marker = load_json(path / ".complete.json", {})
        if not isinstance(marker, dict):
            return False, "invalid completion marker"
        expected = {
            "schema_version": MODEL_COMPLETION_SCHEMA,
            "model_id": model.id,
            "revision": model.revision,
            "backend": model.backend,
            "validation_status": "complete",
            "manifest_fingerprint": _manifest_fingerprint(model),
        }
        for key, value in expected.items():
            if marker.get(key) != value:
                return False, f"completion marker {key} does not match"
        valid, missing = self._required_matches(path, model)
        if not valid:
            return False, "missing required files: " + ", ".join(missing)
        return self._checksums_valid(path, model)

    def _legacy_revision_matches(self, path: Path, model: ModelSpec) -> bool:
        tree = path / ".cache" / "huggingface" / "trees" / f"{model.revision}.json"
        if tree.is_file():
            return True
        metadata = path / ".cache" / "huggingface" / "download"
        if metadata.is_dir():
            for item in metadata.glob("*.metadata"):
                try:
                    if item.read_text(encoding="utf-8", errors="replace").splitlines()[0] == model.revision:
                        return True
                except (OSError, IndexError):
                    continue
        return False

    def _legacy_path(self, model: ModelSpec) -> Path | None:
        for relative in model.legacy_directories:
            # Manifest values are names, never paths. This prevents traversal.
            if not relative or Path(relative).name != relative or relative in {".", ".."}:
                continue
            candidate = self.cache_root / relative
            if not self._managed_path_safe(candidate):
                continue
            valid, _ = self._required_matches(candidate, model)
            checksums, _ = self._checksums_valid(candidate, model)
            if candidate.is_dir() and valid and checksums and self._legacy_revision_matches(candidate, model):
                return candidate
        return None

    def inspect(self, model: ModelSpec) -> ModelInstall:
        destination = self.destination(model)
        if (destination.exists() or destination.is_symlink()) and not self._managed_path_safe(destination):
            return ModelInstall("invalid", str(destination), model.id, model.revision, "managed", "managed model path contains a symlink")
        if destination.is_dir():
            valid, reason = self._valid_complete(destination, model)
            if valid:
                return ModelInstall("complete", str(destination), model.id, model.revision, "managed")
            return ModelInstall("invalid", str(destination), model.id, model.revision, "managed", reason)
        legacy = self._legacy_path(model)
        if legacy is not None:
            return ModelInstall(
                "legacy-complete", str(legacy), model.id, model.revision, "legacy-huggingface",
                "revision and required files validated from Hugging Face metadata",
            )
        partial = self.staging(model)
        if (partial.exists() or partial.is_symlink()) and not self._managed_path_safe(partial):
            return ModelInstall("invalid", str(partial), model.id, model.revision, "managed", "staging path contains a symlink")
        if partial.exists():
            return ModelInstall("partial", str(partial), model.id, model.revision, "managed", "resumable staging directory exists")
        return ModelInstall("missing", str(destination), model.id, model.revision, "managed")

    def _completion_payload(self, model: ModelSpec, path: Path) -> dict[str, object]:
        files = sorted(
            item.relative_to(path).as_posix()
            for item in path.rglob("*")
            if item.is_file() and item.name != ".complete.json"
        )
        return {
            "schema_version": MODEL_COMPLETION_SCHEMA,
            "model_id": model.id,
            "revision": model.revision,
            "backend": model.backend,
            "source": "huggingface",
            "expected_files": list(model.required_files),
            "files": files,
            "downloaded_size": _tree_size(path),
            "validation_status": "complete",
            "manifest_fingerprint": _manifest_fingerprint(model),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _download(self, model: ModelSpec, staging: Path, *, use_token: bool) -> object:
        environment = dict(self.environ)
        token = environment.get("HF_TOKEN", "")
        environment.pop("HF_TOKEN", None)
        environment["HF_HOME"] = str(self.cache_root / "huggingface")
        environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
        if use_token and token:
            environment["HF_TOKEN"] = token
        args: list[str] = [
            self.uv_path,
            "tool", "run", "--from", f"huggingface_hub[hf_xet]=={self.downloader_version}",
            "hf", "download", model.id,
            "--revision", model.revision,
            "--local-dir", str(staging),
        ]
        for pattern in model.allow_patterns:
            args.extend(["--include", pattern])
        return self.runner(args, timeout=7 * 24 * 3600, env=environment, cwd=self.project_root)

    def ensure(
        self,
        model: ModelSpec,
        *,
        offline: bool = False,
        dry_run: bool = False,
        reporter: Callable[[str], None] | None = None,
    ) -> ModelInstall:
        note = reporter or (lambda _message: None)
        current = self.inspect(model)
        if current.ready:
            note(f"{model.id}: already complete ({current.status})")
            return current
        if current.status == "invalid":
            raise TechSaraError(
                f"managed model directory is invalid ({current.message}) and was preserved: {current.path}; "
                "move it aside manually, then retry"
            )
        if offline:
            raise OfflineError(f"offline cache miss for {model.id}@{model.revision[:12]}")
        if dry_run:
            return ModelInstall("planned", current.path, model.id, model.revision, current.source, "download required")

        required_free = int(model.approximate_download_bytes * 1.20) + GIB
        if disk_free(self.cache_root) < required_free:
            raise TechSaraError(
                f"insufficient disk for {model.id}: need at least {required_free / GIB:.1f} GiB free including staging overhead"
            )

        lock_name = f"model-{slug_model(model.id)}-{model.revision[:12]}.lock"
        with FileLock(self.locks_dir / lock_name, timeout=30.0, stale_after=24 * 3600):
            current = self.inspect(model)
            if current.ready:
                return current
            destination = self.destination(model)
            staging = self.staging(model)
            if not self._managed_path_safe(destination) or not self._managed_path_safe(staging):
                raise TechSaraError("refusing a model path containing a symbolic link")
            if destination.exists():
                raise TechSaraError(f"refusing to overwrite invalid managed model directory: {destination}")
            staging.mkdir(parents=True, exist_ok=True)

            note(
                f"{model.id}@{model.revision[:12]}: downloading about "
                f"{model.approximate_download_bytes / GIB:.1f} GiB "
                f"({'resuming' if current.status == 'partial' else 'new'})"
            )
            result = self._download(model, staging, use_token=False)
            if getattr(result, "returncode", 1) != 0 and self.environ.get("HF_TOKEN"):
                result = self._download(model, staging, use_token=True)
            if getattr(result, "returncode", 1) != 0:
                secrets = [self.environ.get("HF_TOKEN", "")]
                detail = redact(str(getattr(result, "stderr", ""))[-600:], secrets).strip()
                raise TechSaraError(
                    f"model download failed for {model.id}; the partial download was preserved for resume"
                    + (f": {detail}" if detail else "")
                )

            valid, missing = self._required_matches(staging, model)
            if not valid:
                raise TechSaraError(
                    f"download for {model.id} is incomplete; missing: {', '.join(missing)}"
                )
            checksums, reason = self._checksums_valid(staging, model)
            if not checksums:
                raise TechSaraError(reason)
            atomic_write_json(staging / ".complete.json", self._completion_payload(model, staging))
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
            note(f"{model.id}: downloaded and validated")
            return ModelInstall("complete", str(destination), model.id, model.revision, "managed", "downloaded and validated")

    def ensure_all(
        self,
        models: Iterable[ModelSpec],
        *,
        offline: bool = False,
        dry_run: bool = False,
        reporter: Callable[[str], None] | None = None,
    ) -> list[ModelInstall]:
        items = list(models)
        if not offline and not dry_run:
            missing = [model for model in items if self.inspect(model).status in {"missing", "partial"}]
            aggregate = sum(int(model.approximate_download_bytes * 1.20) for model in missing)
            if missing:
                aggregate += GIB
            if aggregate and disk_free(self.cache_root) < aggregate:
                raise TechSaraError(
                    f"insufficient disk for the selected model set: need at least {aggregate / GIB:.1f} GiB free including aggregate staging overhead"
                )
        # Deliberately sequential: large downloads and filesystem validation do
        # not compete for disk bandwidth or temporary headroom.
        results: list[ModelInstall] = []
        for index, model in enumerate(items, start=1):
            if reporter:
                reporter(f"[{index}/{len(items)}] {model.id}")
            results.append(
                self.ensure(model, offline=offline, dry_run=dry_run, reporter=reporter)
            )
        return results

    def status(self, models: Iterable[ModelSpec]) -> list[ModelInstall]:
        return [self.inspect(model) for model in models]
