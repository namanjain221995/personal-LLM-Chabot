"""The package as files: what git keeps out, the dependency pins, and an install that is not editable."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

HOME = Path(__file__).resolve().parents[1]          # the sf_assistant folder


def _git_ok() -> bool:
    if shutil.which("git") is None:
        return False
    p = subprocess.run(["git", "-C", str(HOME), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "true"


needs_git = pytest.mark.skipif(not _git_ok(), reason="git or the repository work tree is not there")


def _ignored(paths: list[str]) -> set[str]:
    """The paths (relative to HOME) that the ignore rules match, tracked or not (``--no-index``)."""
    p = subprocess.run(["git", "-C", str(HOME), "check-ignore", "--no-index", "--stdin"], input="\n".join(paths) + "\n",
                       capture_output=True, text=True)
    assert p.returncode in (0, 1), p.stderr
    return set(p.stdout.split())


def _source_files() -> list[str]:
    globs = ("src/**/*.py", "prompts/*.md", "tests/**/*.py", "tests/golden/*.json", "tests/fixtures/*.json",
             "scripts/*.py", "docs/*.md", "web/*.py", "web/static/*")
    out = {"pyproject.toml", "README.md", "ask.sh", ".gitignore"}
    for g in globs:
        out |= {str(p.relative_to(HOME)) for p in HOME.glob(g) if p.is_file() and "__pycache__" not in p.parts
                and not str(p.relative_to(HOME)).startswith("tests/parity/")}
    return sorted(out)


@needs_git
def test_no_source_file_is_git_ignored():
    """An unanchored rule such as ``knowledge/`` would also hide the package source src/sf_assistant/knowledge/."""
    files = _source_files()
    assert "src/sf_assistant/knowledge/store.py" in files and len(files) > 100
    assert sorted(_ignored(files)) == []


@needs_git
def test_private_data_and_outputs_are_git_ignored():
    private = [".private/codes.json", "knowledge/objects/Interview__c.json", "kb_build/skeletons/Interview__c.json",
               "kb_build/curated/x.json", "kb_build/doc_extracts/x.json", "kb_build/sources/x.html", "kb_build/kb.json",
               "kb_build/training_module/facts.json", "kb_build/training_module/features_src.json",
               "runs/ask_records.txt", "okeval_run/lookup_eval/grades.md", "questions/records.txt",
               "reports/summary.md", "x/ask_records_1.jsonl", "ask_records_1.txt", "x/warehouse.duckdb"]
    assert sorted(set(private) - _ignored(private)) == []


def test_pyproject_dependencies_are_the_lock_file():
    """pyproject.toml documents exactly web/requirements.lock (the image and the test venv install the lock)."""
    lock = [ln.strip() for ln in (HOME / "web" / "requirements.lock").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    project = tomllib.loads((HOME / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == lock
    assert all("==" in d for d in project["optional-dependencies"]["test"])


def _import_copy(tmp_path: Path, env_extra: dict) -> subprocess.CompletedProcess:
    """Import the package from a copy of src/sf_assistant in a site-packages-like folder (as a wheel installs it)."""
    site = tmp_path / "lib" / "python3.11" / "site-packages"
    if not site.is_dir():
        shutil.copytree(HOME / "src" / "sf_assistant", site / "sf_assistant",
                        ignore=shutil.ignore_patterns("__pycache__"))
    code = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport sf_assistant\n"
            "assert sf_assistant.__file__.startswith(sys.argv[1]), sf_assistant.__file__\n"
            "from sf_assistant.prompts import PromptError\n"
            "try:\n    import sf_assistant.pipeline\nexcept PromptError as exc:\n    print('PromptError', exc)\n"
            "else:\n    print('imported')\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SFA_", "OKEVAL_"))}
    env.update(env_extra)
    return subprocess.run([sys.executable, "-I", "-B", "-c", code, str(site)], capture_output=True, text=True, env=env,
                          timeout=120)


def test_a_non_editable_install_names_sfa_home(tmp_path):
    """The prompts, knowledge files and .private/ stay in the sf_assistant folder: a plain install fails with a message
    that says so, and works once SFA_HOME points at the folder."""
    p = _import_copy(tmp_path, {})
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.startswith("PromptError ") and "does not exist" in p.stdout and "SFA_HOME" in p.stdout, p.stdout
    p = _import_copy(tmp_path, {"SFA_HOME": str(HOME)})
    assert p.returncode == 0 and p.stdout.strip() == "imported", (p.stdout, p.stderr[-2000:])
