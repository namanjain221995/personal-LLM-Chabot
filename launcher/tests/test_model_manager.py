from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, patch

try:
    from .support import GIB, REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import GIB, REPO_ROOT

from techsara_cli.errors import OfflineError, TechSaraError
from techsara_cli.model_manager import (
    MODEL_COMPLETION_SCHEMA,
    ModelInstall,
    ModelManager,
)
from techsara_cli.profiles import ModelSpec, load_model_manifest


WEIGHT_BYTES = b"tiny-safe-model-fixture"
WEIGHT_SHA = hashlib.sha256(WEIGHT_BYTES).hexdigest()


def fixture_model(**overrides) -> ModelSpec:
    models, _runtimes = load_model_manifest(REPO_ROOT)
    base = next(iter(models.values()))
    values = {
        "key": "fixture-model",
        "id": "fixture/Tiny-Model",
        "revision": "a" * 40,
        "provider": "fixture",
        "backend": "vllm-metal",
        "quantization": "fixture",
        "approximate_download_bytes": 1024,
        "approximate_loaded_weight_bytes": 2048,
        "context_limit": 4096,
        "tested_context": 2048,
        "minimum_memory_bytes": GIB,
        "recommended_memory_bytes": 2 * GIB,
        "supports_chat": True,
        "supports_reasoning": False,
        "supports_vision": False,
        "supports_audio": False,
        "supports_tool_calling": False,
        "supports_structured_output": False,
        "supports_embeddings": False,
        "supports_reranking": False,
        "supports_ocr": False,
        "supports_streaming": True,
        "requires_trust_remote_code": False,
        "endpoint_type": "openai-chat",
        "served_id": "fixture-served-id",
        "startup_arguments": (),
        "tokenizer_arguments": {},
        "health_probe": {"path": "/v1/models", "method": "GET"},
        "license_metadata": {"spdx": "Apache-2.0", "source": "fixture"},
        "required_files": ("config.json", "*.safetensors"),
        "file_sha256": {"model.safetensors": WEIGHT_SHA},
        "allow_patterns": ("*.json", "*.safetensors"),
        "legacy_directories": (),
    }
    values.update(overrides)
    # replace() ensures this fixture follows future optional ModelSpec fields.
    return replace(base, **values)


def write_required(path: Path, *, weight: bytes = WEIGHT_BYTES) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text('{"fixture": true}\n', encoding="utf-8")
    (path / "model.safetensors").write_bytes(weight)


class ModelManagerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.cache = self.root / "cache"
        self.locks = self.root / "runtime" / "locks"
        self.project.mkdir()
        self.model = fixture_model()

    def manager(self, *, runner=None, environ=None) -> ModelManager:
        return ModelManager(
            self.project,
            self.cache,
            self.locks,
            uv_path=self.root / "bin" / "uv",
            environ={} if environ is None else environ,
            runner=runner or Mock(side_effect=AssertionError("downloader must not run")),
            downloader_version="9.9.9-fixture",
        )

    def complete(self, manager: ModelManager, model: ModelSpec | None = None) -> Path:
        model = model or self.model
        destination = manager.destination(model)
        write_required(destination)
        payload = manager._completion_payload(model, destination)
        (destination / ".complete.json").write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        return destination


class ModelInstallValueTests(unittest.TestCase):
    def test_ready_is_true_only_for_valid_managed_or_legacy_installations(self) -> None:
        for status, expected in (
            ("complete", True),
            ("legacy-complete", True),
            ("missing", False),
            ("partial", False),
            ("invalid", False),
            ("planned", False),
        ):
            with self.subTest(status=status):
                value = ModelInstall(status, "/fixture", "owner/model", "a" * 40, "fixture")
                self.assertEqual(value.ready, expected)
                self.assertEqual(value.to_dict()["status"], status)


class InspectionTests(ModelManagerCase):
    def test_missing_model_has_revision_scoped_managed_destination(self) -> None:
        manager = self.manager()
        result = manager.inspect(self.model)
        self.assertEqual(result.status, "missing")
        self.assertEqual(result.model_id, self.model.id)
        self.assertEqual(result.revision, self.model.revision)
        self.assertEqual(
            Path(result.path).name,
            "fixture--Tiny-Model--aaaaaaaaaaaa",
        )
        self.assertFalse(Path(result.path).exists())

    def test_partial_directory_is_never_treated_as_complete(self) -> None:
        manager = self.manager()
        partial = manager.staging(self.model)
        write_required(partial)
        result = manager.inspect(self.model)
        self.assertEqual(result.status, "partial")
        self.assertEqual(Path(result.path), partial)
        self.assertFalse(result.ready)

    def test_valid_completion_marker_and_required_files_are_complete(self) -> None:
        manager = self.manager()
        destination = self.complete(manager)
        result = manager.inspect(self.model)
        self.assertEqual(result.status, "complete")
        self.assertEqual(Path(result.path), destination)
        self.assertTrue(result.ready)

    def test_bad_completion_marker_is_invalid_and_preserved(self) -> None:
        manager = self.manager()
        destination = self.complete(manager)
        marker = destination / ".complete.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["revision"] = "b" * 40
        marker.write_text(json.dumps(payload), encoding="utf-8")
        result = manager.inspect(self.model)
        self.assertEqual(result.status, "invalid")
        self.assertIn("revision", result.message)
        self.assertTrue(destination.exists())

    def test_missing_required_file_or_bad_checksum_is_invalid(self) -> None:
        manager = self.manager()
        destination = self.complete(manager)
        (destination / "config.json").unlink()
        missing = manager.inspect(self.model)
        self.assertEqual(missing.status, "invalid")
        self.assertIn("missing required files", missing.message)

        (destination / "config.json").write_text("{}", encoding="utf-8")
        (destination / "model.safetensors").write_bytes(b"corrupt")
        checksum = manager.inspect(self.model)
        self.assertEqual(checksum.status, "invalid")
        self.assertIn("checksum mismatch", checksum.message)

    def test_managed_completion_does_not_trust_required_file_symlinks(self) -> None:
        manager = self.manager()
        destination = manager.destination(self.model)
        destination.mkdir(parents=True)
        (destination / "config.json").write_text("{}", encoding="utf-8")
        outside = self.root / "outside-model.safetensors"
        outside.write_bytes(WEIGHT_BYTES)
        try:
            (destination / "model.safetensors").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("test filesystem does not support symlinks")
        payload = manager._completion_payload(self.model, destination)
        (destination / ".complete.json").write_text(json.dumps(payload), encoding="utf-8")
        result = manager.inspect(self.model)
        self.assertEqual(result.status, "invalid")
        self.assertTrue(
            "symlink" in result.message.lower() or "missing required files" in result.message.lower(),
            result.message,
        )
        self.assertEqual(outside.read_bytes(), WEIGHT_BYTES)

    def test_managed_destination_root_symlink_is_never_a_complete_install(self) -> None:
        manager = self.manager()
        outside = self.root / "outside-complete-model"
        write_required(outside)
        payload = manager._completion_payload(self.model, outside)
        (outside / ".complete.json").write_text(json.dumps(payload), encoding="utf-8")
        destination = manager.destination(self.model)
        destination.parent.mkdir(parents=True)
        try:
            destination.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("test filesystem does not support directory symlinks")

        result = manager.inspect(self.model)
        self.assertEqual(result.status, "invalid")
        self.assertFalse(result.ready)
        self.assertTrue(
            "symlink" in result.message.lower()
            or "symbolic link" in result.message.lower(),
            result.message,
        )
        self.assertTrue(outside.exists())

    def test_revision_change_uses_a_new_path_and_preserves_old_revision(self) -> None:
        manager = self.manager()
        old = self.complete(manager)
        changed = replace(self.model, revision="b" * 40)
        result = manager.inspect(changed)
        self.assertEqual(result.status, "missing")
        self.assertNotEqual(Path(result.path), old)
        self.assertTrue(old.exists())

    def test_legacy_cache_requires_revision_evidence_and_required_files(self) -> None:
        model = replace(self.model, legacy_directories=("legacy-model",))
        manager = self.manager()
        legacy = self.cache / "legacy-model"
        write_required(legacy)
        tree = legacy / ".cache" / "huggingface" / "trees" / f"{model.revision}.json"
        tree.parent.mkdir(parents=True)
        tree.write_text("{}", encoding="utf-8")
        result = manager.inspect(model)
        self.assertEqual(result.status, "legacy-complete")
        self.assertEqual(Path(result.path), legacy)
        self.assertEqual(result.source, "legacy-huggingface")

        tree.rename(tree.with_name(f"{'b' * 40}.json"))
        self.assertEqual(manager.inspect(model).status, "missing")

    def test_legacy_manifest_traversal_is_ignored(self) -> None:
        outside = self.root / "outside"
        write_required(outside)
        model = replace(self.model, legacy_directories=("../outside", "/absolute", ".."))
        manager = self.manager()
        self.assertEqual(manager.inspect(model).status, "missing")
        self.assertTrue(outside.exists())

    def test_legacy_cache_root_symlink_outside_cache_is_ignored(self) -> None:
        model = replace(self.model, legacy_directories=("legacy-model",))
        manager = self.manager()
        outside = self.root / "outside-legacy"
        write_required(outside)
        tree = outside / ".cache" / "huggingface" / "trees" / f"{model.revision}.json"
        tree.parent.mkdir(parents=True)
        tree.write_text("{}", encoding="utf-8")
        self.cache.mkdir()
        try:
            (self.cache / "legacy-model").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("test filesystem does not support directory symlinks")

        self.assertEqual(manager.inspect(model).status, "missing")
        self.assertTrue(outside.exists())


class EnsureTests(ModelManagerCase):
    def test_dry_run_plans_without_creating_directories_or_calling_runner(self) -> None:
        runner = Mock(side_effect=AssertionError("runner called during dry-run"))
        manager = self.manager(runner=runner)
        result = manager.ensure(self.model, dry_run=True)
        self.assertEqual(result.status, "planned")
        self.assertIn("download required", result.message)
        self.assertFalse(self.cache.exists())
        runner.assert_not_called()

    def test_offline_complete_is_a_hit_and_offline_missing_is_actionable(self) -> None:
        manager = self.manager()
        self.complete(manager)
        self.assertEqual(manager.ensure(self.model, offline=True).status, "complete")

        missing = replace(self.model, revision="b" * 40)
        with self.assertRaisesRegex(OfflineError, r"offline cache miss.*bbbbbbbbbbbb"):
            manager.ensure(missing, offline=True)

    def test_insufficient_disk_fails_before_lock_staging_or_download(self) -> None:
        runner = Mock(side_effect=AssertionError("runner called with insufficient disk"))
        manager = self.manager(runner=runner)
        with patch("techsara_cli.model_manager.disk_free", return_value=0):
            with self.assertRaisesRegex(TechSaraError, "insufficient disk"):
                manager.ensure(self.model)
        runner.assert_not_called()
        self.assertFalse(manager.staging(self.model).exists())

    def test_staging_root_symlink_is_rejected_before_download(self) -> None:
        runner = Mock(side_effect=AssertionError("symlinked staging must not download"))
        manager = self.manager(runner=runner)
        outside = self.root / "outside-staging"
        outside.mkdir()
        staging = manager.staging(self.model)
        staging.parent.mkdir(parents=True)
        try:
            staging.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("test filesystem does not support directory symlinks")

        with patch("techsara_cli.model_manager.disk_free", return_value=100 * GIB):
            with self.assertRaisesRegex(
                TechSaraError, "symbolic link|symlink|outside|unsafe"
            ):
                manager.ensure(self.model)
        runner.assert_not_called()
        self.assertTrue(outside.exists())

    def test_successful_download_is_validated_published_and_idempotent(self) -> None:
        calls = []

        def runner(args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            staging = Path(args[args.index("--local-dir") + 1])
            write_required(staging)
            return subprocess.CompletedProcess(args, 0, "", "")

        manager = self.manager(runner=runner, environ={})
        with patch("techsara_cli.model_manager.disk_free", return_value=100 * GIB):
            first = manager.ensure(self.model)
            second = manager.ensure(self.model)

        self.assertEqual(first.status, "complete")
        self.assertEqual(second.status, "complete")
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[0], str(self.root / "bin" / "uv"))
        self.assertEqual(args[1:6], ["tool", "run", "--from", "huggingface_hub[hf_xet]==9.9.9-fixture", "hf"])
        self.assertIn("download", args)
        self.assertEqual(args[args.index("--revision") + 1], self.model.revision)
        self.assertEqual(args.count("--include"), len(self.model.allow_patterns))
        self.assertEqual(kwargs["cwd"], self.project.resolve())
        self.assertNotIn("HF_TOKEN", kwargs["env"])
        destination = Path(first.path)
        marker = json.loads((destination / ".complete.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["schema_version"], MODEL_COMPLETION_SCHEMA)
        self.assertEqual(marker["revision"], self.model.revision)
        self.assertEqual(marker["validation_status"], "complete")
        self.assertFalse(manager.staging(self.model).exists())

    def test_existing_partial_is_reused_for_resume(self) -> None:
        manager: ModelManager
        observed_existing = []

        def runner(args, **_kwargs):
            staging = Path(args[args.index("--local-dir") + 1])
            observed_existing.append((staging / "download.part").read_bytes())
            write_required(staging)
            return subprocess.CompletedProcess(args, 0, "", "")

        manager = self.manager(runner=runner)
        staging = manager.staging(self.model)
        staging.mkdir(parents=True)
        (staging / "download.part").write_bytes(b"partial")
        with patch("techsara_cli.model_manager.disk_free", return_value=100 * GIB):
            result = manager.ensure(self.model)
        self.assertEqual(result.status, "complete")
        self.assertEqual(observed_existing, [b"partial"])
        self.assertTrue((Path(result.path) / "download.part").exists())

    def test_interrupted_download_preserves_partial_and_redacts_token(self) -> None:
        token = "hf_fixture_super_secret"
        call_envs = []

        def runner(args, **kwargs):
            call_envs.append(dict(kwargs["env"]))
            staging = Path(args[args.index("--local-dir") + 1])
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "download.part").write_bytes(b"partial")
            return subprocess.CompletedProcess(args, 1, "", f"authorization failed for {token}")

        manager = self.manager(runner=runner, environ={"HF_TOKEN": token})
        with patch("techsara_cli.model_manager.disk_free", return_value=100 * GIB):
            with self.assertRaises(TechSaraError) as caught:
                manager.ensure(self.model)
        self.assertEqual(len(call_envs), 2)
        self.assertNotIn("HF_TOKEN", call_envs[0])
        self.assertEqual(call_envs[1]["HF_TOKEN"], token)
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))
        self.assertTrue(manager.staging(self.model).is_dir())
        self.assertFalse(manager.destination(self.model).exists())

    def test_private_token_is_used_only_after_anonymous_failure(self) -> None:
        token = "hf_fixture_super_secret"
        call_envs = []

        def runner(args, **kwargs):
            call_envs.append(dict(kwargs["env"]))
            if len(call_envs) == 1:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 401")
            staging = Path(args[args.index("--local-dir") + 1])
            write_required(staging)
            return subprocess.CompletedProcess(args, 0, "", "")

        manager = self.manager(runner=runner, environ={"HF_TOKEN": token})
        with patch("techsara_cli.model_manager.disk_free", return_value=100 * GIB):
            result = manager.ensure(self.model)
        self.assertEqual(result.status, "complete")
        self.assertNotIn("HF_TOKEN", call_envs[0])
        self.assertEqual(call_envs[1]["HF_TOKEN"], token)

    def test_incomplete_or_checksum_failed_download_never_gets_completion_marker(self) -> None:
        cases = {
            "missing": lambda staging: (staging.mkdir(parents=True, exist_ok=True), (staging / "config.json").write_text("{}")),
            "checksum": lambda staging: write_required(staging, weight=b"wrong"),
        }
        for name, populate in cases.items():
            with self.subTest(case=name):
                model = replace(self.model, revision=("b" if name == "missing" else "c") * 40)

                def runner(args, **_kwargs):
                    staging = Path(args[args.index("--local-dir") + 1])
                    populate(staging)
                    return subprocess.CompletedProcess(args, 0, "", "")

                manager = self.manager(runner=runner)
                with patch("techsara_cli.model_manager.disk_free", return_value=100 * GIB):
                    with self.assertRaises(TechSaraError):
                        manager.ensure(model)
                self.assertTrue(manager.staging(model).exists())
                self.assertFalse((manager.staging(model) / ".complete.json").exists())
                self.assertFalse(manager.destination(model).exists())

    def test_invalid_managed_directory_is_never_overwritten_or_deleted(self) -> None:
        runner = Mock(side_effect=AssertionError("invalid directory must stop before download"))
        manager = self.manager(runner=runner)
        destination = manager.destination(self.model)
        destination.mkdir(parents=True)
        sentinel = destination / "user-data"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(TechSaraError, r"invalid .*preserved"):
            manager.ensure(self.model)
        runner.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_model_specific_lock_is_acquired_before_downloader(self) -> None:
        runner = Mock(side_effect=AssertionError("contended operation must not download"))
        manager = self.manager(runner=runner)
        recorded = []

        class RejectingLock:
            def __init__(self, path, **kwargs):
                recorded.append((Path(path), kwargs))

            def __enter__(self):
                raise TechSaraError("fixture lock contention")

            def __exit__(self, exc_type, exc, tb):
                return None

        with patch("techsara_cli.model_manager.disk_free", return_value=100 * GIB), patch(
            "techsara_cli.model_manager.FileLock", RejectingLock
        ):
            with self.assertRaisesRegex(TechSaraError, "fixture lock contention"):
                manager.ensure(self.model)
        runner.assert_not_called()
        self.assertEqual(len(recorded), 1)
        lock_path, options = recorded[0]
        self.assertEqual(lock_path.parent, self.locks.resolve())
        self.assertIn(self.model.revision[:12], lock_path.name)
        self.assertEqual(options["stale_after"], 24 * 3600)

    def test_ensure_all_is_sequential_and_status_preserves_input_order(self) -> None:
        manager = self.manager()
        first = self.model
        second = replace(self.model, id="fixture/Second", revision="b" * 40)
        planned = {
            first.id: ModelInstall("planned", "/first", first.id, first.revision, "managed"),
            second.id: ModelInstall("planned", "/second", second.id, second.revision, "managed"),
        }
        with patch.object(manager, "ensure", side_effect=lambda model, **_kwargs: planned[model.id]) as ensure:
            results = manager.ensure_all([first, second], offline=True, dry_run=True)
        self.assertEqual([result.model_id for result in results], [first.id, second.id])
        self.assertEqual(
            ensure.call_args_list,
            [
                call(first, offline=True, dry_run=True, reporter=None),
                call(second, offline=True, dry_run=True, reporter=None),
            ],
        )

        with patch.object(manager, "inspect", side_effect=lambda model: planned[model.id]) as inspect:
            statuses = manager.status([second, first])
        self.assertEqual([result.model_id for result in statuses], [second.id, first.id])
        self.assertEqual(inspect.call_args_list, [call(second), call(first)])

    def test_ensure_all_preflights_aggregate_disk_before_first_download(self) -> None:
        runner = Mock(side_effect=AssertionError("aggregate disk preflight ran too late"))
        manager = self.manager(runner=runner)
        first = replace(
            self.model,
            approximate_download_bytes=2 * GIB,
        )
        second = replace(
            self.model,
            id="fixture/Second",
            revision="b" * 40,
            approximate_download_bytes=2 * GIB,
        )
        # Either model individually fits this fixture disk. Their combined
        # downloads plus staging headroom do not.
        with patch("techsara_cli.model_manager.disk_free", return_value=5 * GIB):
            with self.assertRaisesRegex(TechSaraError, "insufficient disk"):
                manager.ensure_all([first, second])
        runner.assert_not_called()
        self.assertFalse(manager.staging(first).exists())
        self.assertFalse(manager.staging(second).exists())


if __name__ == "__main__":
    unittest.main()
