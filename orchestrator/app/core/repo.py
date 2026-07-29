"""GitHub repository analysis (Phase 3) — detect, safely clone, summarize.

SECURITY (see the master prompt): public GitHub only; shallow clone into an
isolated per-conversation workspace; hard size / file-count caps; hooks
disabled; the clone is DATA — repository code is NEVER executed and its
dependencies are NEVER installed.

git and filesystem work is done via subprocess/os; heavy nothing imported at
module load.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..config import settings

# github.com/<owner>/<repo>[.git] and .../blob/<ref>/<path>
_REPO_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/(?:tree|blob)/([^/\s]+)(?:/([^\s?#]+))?)?/?(?=[\s?#]|$)",
    re.I,
)

# Directories never worth reading; big/binary/generated content.
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", "target", ".idea", ".vscode", "vendor", ".mypy_cache", ".pytest_cache",
}
_TEXT_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb",
    ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".sh",
    ".sql", ".yaml", ".yml", ".toml", ".json", ".md", ".txt", ".cfg", ".ini",
    ".html", ".css", ".scss", ".vue", ".r", ".m", ".mm", ".gradle", ".dockerfile",
}
_LANG = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
    ".php": "PHP", ".c": "C", ".cpp": "C++", ".cs": "C#", ".swift": "Swift",
    ".kt": "Kotlin", ".scala": "Scala", ".sh": "Shell", ".sql": "SQL", ".vue": "Vue",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS",
}
_MAX_FILE_BYTES = 400_000  # skip files bigger than this when reading/indexing


class RepoError(RuntimeError):
    """User-facing repo failure (bad URL, too big, clone failed)."""


@dataclass
class GithubRef:
    owner: str
    repo: str
    ref: Optional[str] = None
    path: Optional[str] = None  # set for a blob (single-file) URL

    @property
    def key(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"


def detect_github(text: str) -> Optional[GithubRef]:
    """First github.com repo/blob URL in `text`, or None."""
    m = _REPO_RE.search(text or "")
    if not m:
        return None
    owner, repo, ref, path = m.group(1), m.group(2), m.group(3), m.group(4)
    is_blob = "/blob/" in m.group(0).lower()
    return GithubRef(owner=owner, repo=repo, ref=ref, path=path if is_blob else None)


# --------------------------------------------------------------------------
# workspace lifecycle: quota + TTL
# --------------------------------------------------------------------------
def _dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def enforce_quota_and_ttl() -> None:
    """Delete workspaces older than the TTL, then, if still over the global
    quota, delete the oldest until under it."""
    base = settings.workspace_dir
    if not os.path.isdir(base):
        return
    import time

    now = time.time()
    entries = []
    for name in os.listdir(base):
        p = os.path.join(base, name)
        if not os.path.isdir(p):
            continue
        mtime = os.path.getmtime(p)
        if now - mtime > settings.workspace_ttl_hours * 3600:
            shutil.rmtree(p, ignore_errors=True)
        else:
            entries.append((mtime, p))
    quota = settings.workspace_quota_gb * 1024 ** 3
    total = sum(_dir_size_bytes(p) for _m, p in entries)
    for _mtime, p in sorted(entries):  # oldest first
        if total <= quota:
            break
        total -= _dir_size_bytes(p)
        shutil.rmtree(p, ignore_errors=True)


def workspace_path(conversation_id: str, ref: GithubRef) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{conversation_id}__{ref.owner}__{ref.repo}")
    return os.path.join(settings.workspace_dir, safe)


# --------------------------------------------------------------------------
# clone (shallow, capped, hooks disabled, code never executed)
# --------------------------------------------------------------------------
def _github_repo_size_kb(ref: GithubRef) -> Optional[int]:
    """Repo size (KB) from the GitHub API, or None if it can't be determined.
    Lets us reject an oversized repo BEFORE cloning it."""
    import httpx

    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{ref.owner}/{ref.repo}",
            timeout=httpx.Timeout(10.0),
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code == 404:
            raise RepoError(f"Repository {ref.key} was not found (or is private).")
        resp.raise_for_status()
        return int(resp.json().get("size", 0))
    except RepoError:
        raise
    except (httpx.HTTPError, ValueError, KeyError):
        return None


def shallow_clone(ref: GithubRef, dest: str) -> str:
    """Shallow-clone `ref` into `dest`, enforcing the size + file caps and
    disabling hooks. Returns the checked-out commit SHA. Raises RepoError."""
    size_kb = _github_repo_size_kb(ref)
    if size_kb is not None and size_kb > settings.repo_max_mb * 1024:
        raise RepoError(
            f"{ref.key} is ~{size_kb // 1024} MB — over the "
            f"{settings.repo_max_mb} MB limit."
        )

    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",  # never prompt for credentials
            "GIT_ASKPASS": "/bin/true",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    cmd = [
        "git", "-c", "core.hooksPath=/dev/null",  # repo hooks never run
        "-c", "credential.helper=",
        "clone", "--depth", "1", "--no-tags", "--single-branch",
    ]
    if ref.ref:
        cmd += ["--branch", ref.ref]
    cmd += [ref.clone_url, dest]
    try:
        subprocess.run(
            cmd, env=env, check=True, capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise RepoError(f"Cloning {ref.key} timed out.") from exc
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        detail = (exc.stderr or "").strip().splitlines()[-1:] or ["clone failed"]
        raise RepoError(f"Couldn't clone {ref.key}: {detail[0]}") from exc

    # hooks are only sample files after a clone, but remove them anyway.
    shutil.rmtree(os.path.join(dest, ".git", "hooks"), ignore_errors=True)

    # Enforce the file-count and on-disk size caps AFTER clone.
    file_count = sum(len(files) for _r, _d, files in os.walk(dest))
    if file_count > settings.repo_max_files:
        shutil.rmtree(dest, ignore_errors=True)
        raise RepoError(
            f"{ref.key} has {file_count} files — over the "
            f"{settings.repo_max_files} limit."
        )
    if _dir_size_bytes(dest) > settings.repo_max_mb * 1024 ** 2:
        shutil.rmtree(dest, ignore_errors=True)
        raise RepoError(f"{ref.key} is over the {settings.repo_max_mb} MB limit.")

    try:
        sha = subprocess.run(
            ["git", "-C", dest, "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except subprocess.SubprocessError:
        sha = ""
    return sha


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------
def iter_source_files(repo_dir: str):
    """Yield (relative_path, absolute_path) for readable text source files."""
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in _TEXT_EXT and f.lower() not in ("dockerfile", "makefile"):
                continue
            ap = os.path.join(root, f)
            try:
                if os.path.getsize(ap) > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield os.path.relpath(ap, repo_dir), ap


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


_ENTRY_HINTS = (
    "main.py", "app.py", "__main__.py", "manage.py", "index.js", "index.ts",
    "main.go", "main.rs", "server.py", "server.js", "cli.py",
)
_CONFIG_HINTS = (
    "package.json", "pyproject.toml", "requirements.txt", "go.mod", "cargo.toml",
    "dockerfile", "docker-compose.yml", "makefile", "setup.py", "pom.xml",
)


@dataclass
class RepoOverview:
    tree: str
    languages: List[Tuple[str, int]]
    readme: str
    entry_points: List[str]
    key_configs: List[str]
    file_count: int


def build_overview(repo_dir: str, max_tree_entries: int = 200) -> RepoOverview:
    langs: dict = {}
    entries: List[str] = []
    entry_points: List[str] = []
    key_configs: List[str] = []
    total = 0
    for rel, _ap in iter_source_files(repo_dir):
        total += 1
        ext = os.path.splitext(rel)[1].lower()
        lang = _LANG.get(ext)
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
        if len(entries) < max_tree_entries:
            entries.append(rel)
        base = os.path.basename(rel).lower()
        if base in _ENTRY_HINTS:
            entry_points.append(rel)
        if base in _CONFIG_HINTS:
            key_configs.append(rel)

    readme = ""
    for name in ("README.md", "README.rst", "README.txt", "readme.md"):
        p = os.path.join(repo_dir, name)
        if os.path.isfile(p):
            readme = read_text(p)[:8000]
            break

    languages = sorted(langs.items(), key=lambda t: t[1], reverse=True)
    tree = "\n".join(sorted(entries))
    return RepoOverview(
        tree=tree,
        languages=languages,
        readme=readme,
        entry_points=entry_points[:10],
        key_configs=key_configs[:10],
        file_count=total,
    )
