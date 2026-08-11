from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

try:
    from .support import GIB
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import GIB

from techsara_cli.errors import TechSaraError
from techsara_cli.utils import (
    FileLock,
    atomic_write_json,
    atomic_write_text,
    download_with_resume,
    load_json,
    parse_env_file,
    redact,
    render_env,
    run_command,
    safe_extract_tar,
    secure_token,
    sha256_file,
    slug_model,
    validate_model_id,
    validate_profile_name,
    validate_revision,
    verified_download,
)


class ValidationTests(unittest.TestCase):
    def test_profile_names_accept_only_bounded_lowercase_slugs(self) -> None:
        for value in ("dgx-spark", "mac-128gb-plus", "a", "nvidia-80"):
            self.assertEqual(validate_profile_name(value), value)
        for value in ("DGX", "../dgx", "dgx_spark", "dgx;id", "", "a" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_profile_name(value)

    def test_model_ids_cannot_be_paths_urls_revisions_or_shell_fragments(self) -> None:
        for value in (
            "Qwen/Qwen3-Embedding-0.6B",
            "mlx-community/Qwen3.6-35B-A3B-4bit",
            "owner_1/model.v2",
        ):
            self.assertEqual(validate_model_id(value), value)
            self.assertEqual(slug_model(value), value.replace("/", "--"))
        for value in (
            "Qwen3",
            "../Qwen/model",
            "Qwen/../../etc/passwd",
            "https://example/model",
            "Qwen/model@main",
            "Qwen/model;id",
            "Qwen/model name",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_model_id(value)

    def test_revision_requires_a_canonical_full_lowercase_hex_commit(self) -> None:
        lower = "0123456789abcdef" * 2 + "01234567"
        upper = lower.upper()
        self.assertEqual(validate_revision(lower), lower)
        for value in ("main", "latest", lower[:12], upper, "g" * 40, lower + "0", f"{lower};id"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_revision(value)


class EnvironmentFileTests(unittest.TestCase):
    def test_dotenv_parser_never_evaluates_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "must-not-exist"
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                "# comment\n"
                "SAFE=value\n"
                "QUOTED='two words'\n"
                f"LITERAL=$(touch {marker})\n"
                "ALSO_LITERAL=`id`\n"
                "BAD-KEY=ignored\n"
                "NO_ASSIGNMENT\n",
                encoding="utf-8",
            )
            values = parse_env_file(env_file)
            self.assertEqual(values["SAFE"], "value")
            self.assertEqual(values["QUOTED"], "two words")
            self.assertEqual(values["LITERAL"], f"$(touch {marker})")
            self.assertEqual(values["ALSO_LITERAL"], "`id`")
            self.assertNotIn("BAD-KEY", values)
            self.assertFalse(marker.exists())

    def test_missing_dotenv_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(parse_env_file(Path(temporary) / "missing.env"), {})

    def test_generated_env_is_sorted_and_rejects_multiline_or_invalid_keys(self) -> None:
        self.assertEqual(render_env({"ZED": 2, "ALPHA": "one"}), "ALPHA=one\nZED=2\n")
        with self.assertRaises(ValueError):
            render_env({"BAD-KEY": "value"})
        for value in ("first\nsecond", "carriage\rreturn", "nul\x00byte"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                render_env({"SAFE": value})

    def test_secret_redaction_never_echoes_substantial_secret_values(self) -> None:
        text = "token=super-secret password=long-password short=abc"
        safe = redact(text, ["super-secret", "long-password", "abc", ""])
        self.assertNotIn("super-secret", safe)
        self.assertNotIn("long-password", safe)
        self.assertEqual(safe.count("[REDACTED]"), 2)
        self.assertIn("short=abc", safe)

    def test_secure_token_has_expected_entropy_shape(self) -> None:
        first = secure_token(32)
        second = secure_token(32)
        self.assertEqual(len(first), 64)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)


class SubprocessSafetyTests(unittest.TestCase):
    def test_run_command_passes_an_exact_argv_without_a_shell(self) -> None:
        completed = subprocess.CompletedProcess(["fixture"], 0, "out", "")
        with patch("techsara_cli.utils.subprocess.run", return_value=completed) as mocked:
            result = run_command(
                ["fixture", "literal;touch", "$(id)", "value with spaces"],
                timeout=2.5,
                env={"ONLY": "fixture"},
                cwd=Path("/tmp"),
            )
        self.assertIs(result, completed)
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], ["fixture", "literal;touch", "$(id)", "value with spaces"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 2.5)
        self.assertEqual(kwargs["env"], {"ONLY": "fixture"})
        self.assertTrue(kwargs["capture_output"])

    def test_nonchecking_command_failure_is_bounded_and_nonthrowing(self) -> None:
        with patch("techsara_cli.utils.subprocess.run", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            result = run_command(["fixture"], timeout=1, check=False)
        self.assertEqual(result.returncode, 127)
        self.assertIn("TimeoutExpired", result.stderr)

    def test_checking_command_failure_raises_a_safe_launcher_error(self) -> None:
        with patch("techsara_cli.utils.subprocess.run", side_effect=OSError("host detail")):
            with self.assertRaisesRegex(TechSaraError, r"command failed: fixture \(OSError\)"):
                run_command(["fixture", "secret-argument"], check=True)


class AtomicFileAndLockTests(unittest.TestCase):
    def test_atomic_text_and_json_writes_replace_content_and_apply_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text_path = root / "state" / "value.txt"
            atomic_write_text(text_path, "first\n", mode=0o600)
            atomic_write_text(text_path, "second\n", mode=0o600)
            self.assertEqual(text_path.read_text(encoding="utf-8"), "second\n")
            self.assertEqual(stat.S_IMODE(text_path.stat().st_mode), 0o600)

            json_path = root / "state.json"
            atomic_write_json(json_path, {"z": 2, "a": 1}, mode=0o600)
            self.assertEqual(load_json(json_path), {"a": 1, "z": 2})
            self.assertTrue(json_path.read_text(encoding="utf-8").endswith("\n"))
            self.assertEqual(stat.S_IMODE(json_path.stat().st_mode), 0o600)

    def test_invalid_json_returns_the_supplied_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text("not-json", encoding="utf-8")
            marker = object()
            self.assertIs(load_json(path, marker), marker)
            self.assertIs(load_json(Path(temporary) / "missing", marker), marker)

    def test_file_lock_is_exclusive_and_owned_lock_is_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "locks" / "operation.lock"
            with FileLock(lock_path, timeout=0):
                self.assertTrue(lock_path.exists())
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], os.getpid())
                with self.assertRaisesRegex(TechSaraError, "another TechSara operation"):
                    with FileLock(lock_path, timeout=0):
                        self.fail("contended lock must not be acquired")
            self.assertFalse(lock_path.exists())

    def test_stale_lock_is_recovered_without_touching_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "operation.lock"
            neighbor = root / "user-data.txt"
            lock_path.write_text("stale", encoding="utf-8")
            neighbor.write_text("preserve", encoding="utf-8")
            old = time.time() - 100
            os.utime(lock_path, (old, old))
            with FileLock(lock_path, timeout=0, stale_after=1):
                self.assertTrue(lock_path.exists())
            self.assertEqual(neighbor.read_text(encoding="utf-8"), "preserve")

    def test_old_lock_owned_by_a_live_process_is_not_broken(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "operation.lock"
            payload = {"pid": 4242, "created": time.time() - 100}
            lock_path.write_text(json.dumps(payload), encoding="utf-8")
            old = time.time() - 100
            os.utime(lock_path, (old, old))

            with patch("techsara_cli.utils.os.kill", return_value=None) as kill:
                with self.assertRaisesRegex(TechSaraError, "another TechSara operation"):
                    with FileLock(lock_path, timeout=0, stale_after=1):
                        self.fail("a live owner's old lock must remain exclusive")
            kill.assert_called_with(4242, 0)
            self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8")), payload)


class FakeResponse(io.BytesIO):
    def __init__(self, value: bytes, *, status: int = 200) -> None:
        super().__init__(value)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class DownloadTests(unittest.TestCase):
    def test_download_resumes_partial_content_with_range_and_atomic_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "asset.bin"
            partial = destination.with_name("asset.bin.part")
            partial.write_bytes(b"first-")
            seen_requests = []

            def fake_urlopen(request, *, timeout):
                seen_requests.append((request, timeout))
                return FakeResponse(b"second", status=206)

            with patch("techsara_cli.utils.urllib.request.urlopen", side_effect=fake_urlopen):
                result = download_with_resume("https://fixture.invalid/asset", destination, timeout=9)
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"first-second")
            self.assertFalse(partial.exists())
            self.assertEqual(seen_requests[0][0].get_header("Range"), "bytes=6-")
            self.assertEqual(seen_requests[0][1], 9)

    def test_server_ignoring_range_restarts_partial_file_instead_of_corrupting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "asset.bin"
            destination.with_name("asset.bin.part").write_bytes(b"old-partial")
            with patch(
                "techsara_cli.utils.urllib.request.urlopen",
                return_value=FakeResponse(b"complete", status=200),
            ):
                download_with_resume("https://fixture.invalid/asset", destination)
            self.assertEqual(destination.read_bytes(), b"complete")

    def test_http_416_publishes_an_already_complete_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "asset.bin"
            destination.with_name("asset.bin.part").write_bytes(b"complete")
            error = urllib.error.HTTPError(
                "https://fixture.invalid/asset", 416, "range complete", hdrs=None, fp=None
            )
            with patch("techsara_cli.utils.urllib.request.urlopen", side_effect=error):
                download_with_resume("https://fixture.invalid/asset", destination)
            self.assertEqual(destination.read_bytes(), b"complete")

    def test_verified_download_reuses_a_valid_asset_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "asset.bin"
            destination.write_bytes(b"fixture")
            digest = hashlib.sha256(b"fixture").hexdigest()
            with patch("techsara_cli.utils.download_with_resume") as download:
                self.assertEqual(verified_download("https://fixture.invalid", destination, digest), destination)
            download.assert_not_called()
            self.assertEqual(sha256_file(destination), digest)

    def test_checksum_failure_removes_only_the_bad_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "asset.bin"
            neighbor = root / "user-data.bin"
            neighbor.write_bytes(b"preserve")

            def fake_download(_url, target):
                target.write_bytes(b"corrupt")
                return target

            with patch("techsara_cli.utils.download_with_resume", side_effect=fake_download):
                with self.assertRaisesRegex(TechSaraError, "checksum mismatch"):
                    verified_download(
                        "https://fixture.invalid", destination, hashlib.sha256(b"expected").hexdigest()
                    )
            self.assertFalse(destination.exists())
            self.assertEqual(neighbor.read_bytes(), b"preserve")


class ArchiveSafetyTests(unittest.TestCase):
    @staticmethod
    def _write_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        with tarfile.open(path, "w:gz") as archive:
            for info, value in members:
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))

    def test_safe_archive_extracts_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "safe.tar.gz"
            info = tarfile.TarInfo("bin/tool")
            self._write_tar(archive, [(info, b"fixture")])
            destination = root / "out"
            safe_extract_tar(archive, destination)
            self.assertEqual((destination / "bin" / "tool").read_bytes(), b"fixture")

    def test_archive_rejects_traversal_absolute_paths_links_and_devices(self) -> None:
        cases: list[tarfile.TarInfo] = []
        cases.append(tarfile.TarInfo("../escape"))
        cases.append(tarfile.TarInfo("/absolute"))
        symlink = tarfile.TarInfo("link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "target"
        cases.append(symlink)
        device = tarfile.TarInfo("device")
        device.type = tarfile.CHRTYPE
        cases.append(device)

        for index, info in enumerate(cases):
            with self.subTest(member=info.name, type=info.type), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / f"unsafe-{index}.tar.gz"
                self._write_tar(archive, [(info, b"" if info.isreg() else b"")])
                outside = root.parent / f"techsara-escape-{index}"
                with self.assertRaises(TechSaraError):
                    safe_extract_tar(archive, root / "out")
                self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
