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


def _walk_stats(path: str) -> Tuple[float, int]:
    """(newest mtime anywhere under `path`, total bytes) in ONE pass.

    The NEWEST mtime in the tree, not the directory's own, because a
    directory's mtime moves only when one of its DIRECT children is created
    or removed. An upload directory's parts land in `_parts/`, so
    `uploads/<conversation>/<upload_id>/` itself can look hours old while
    bytes are arriving into it this second. Asking the tree is the only
    reading of "how old is this upload" that matches what is happening to it.
    """
    newest = 0.0
    total = 0
    for root, _dirs, files in os.walk(path):
        try:
            newest = max(newest, os.path.getmtime(root))
        except OSError:
            pass
        for name in files:
            try:
                st = os.stat(os.path.join(root, name), follow_symlinks=False)
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            total += st.st_size
    if newest == 0.0:
        try:
            newest = os.path.getmtime(path)
        except OSError:
            newest = 0.0
    return newest, total


#: The one top-level workspace entry that is not a repository clone.
#: `uploads.upload_root` builds `<WORKSPACE_DIR>/uploads/<conversation>/<id>`.
_UPLOADS_DIR = "uploads"


@dataclass
class _Sweepable:
    """One directory the sweep may consider removing, with what it costs."""

    path: str
    mtime: float
    size: int
    #: Set for `uploads/<conversation>/<upload_id>`; None for a repo clone.
    conversation_id: Optional[str] = None
    upload_id: Optional[str] = None


def _upload_is_live(entry: _Sweepable, created_since: dict) -> bool:
    """True when this upload directory must survive the sweep whatever its
    age or the quota says.

    Two independent reasons, either of which is enough:

    * an `upload_sessions` row in `uploading` or `finalizing` whose expiry has
      not passed — bytes are arriving, or a `complete` is assembling them, and
      removing the parts under either turns the next PUT into a 404 for an
      upload the person is watching succeed (F-01/INF-4);
    * an `uploads` row created within the workspace TTL — the finished upload
      a conversation is about to ask questions of. Its `_original` bytes are
      the answer's input, and `uploads.bytes_available()` reports their
      absence as "expired, re-upload it".

    A database that cannot be reached answers TRUE. The failure mode of
    guessing wrong in that direction is a directory that lives one sweep
    longer than it had to; guessing wrong the other way is the incident this
    function exists to prevent.
    """
    from datetime import datetime, timezone

    from .. import db  # lazy: core/ must stay importable without a pool

    upload_id = entry.upload_id or ""
    try:
        session = db.get_upload_session(upload_id)
    except Exception:  # noqa: BLE001 — see the docstring: unknown means keep
        return True
    if session is not None and session.get("status") in ("uploading", "finalizing"):
        expires_at = session.get("expires_at")
        if not expires_at:
            return True
        try:
            deadline = datetime.fromisoformat(str(expires_at))
        except ValueError:
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline > datetime.now(timezone.utc):
            return True
    return upload_id in created_since


def _recent_upload_ids(conversation_id: str, cutoff: float) -> Optional[set]:
    """Ids of this conversation's `uploads` rows created since `cutoff`, or
    None when the question could not be asked (caller then keeps everything).

    One query per conversation directory that has a candidate in it, not one
    per upload: `get_uploads` is already the per-conversation accessor and the
    isolation boundary the rest of the code uses.
    """
    from datetime import datetime, timezone

    from .. import db  # lazy, as above

    try:
        rows = db.get_uploads(conversation_id)
    except Exception:  # noqa: BLE001
        return None
    fresh = set()
    for row in rows:
        created_at = row.get("created_at")
        if not created_at:
            fresh.add(row.get("id"))
            continue
        try:
            when = datetime.fromisoformat(str(created_at))
        except ValueError:
            fresh.add(row.get("id"))
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when.timestamp() >= cutoff:
            fresh.add(row.get("id"))
    return fresh


def _collect(base: str, protected_roots: set) -> List[_Sweepable]:
    """Every directory the sweep is allowed to judge, at the RIGHT depth.

    A repository clone is one top-level directory and is judged as one. The
    `uploads/` tree is not: it is a single top-level directory holding every
    conversation of every user, so judging it as one entry is what let a sweep
    delete every upload on the box (F-01/INF-4). It is descended two levels —
    conversation, then upload — and each UPLOAD is an entry.
    """
    out: List[_Sweepable] = []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in sorted(names):
        path = os.path.join(base, name)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        if os.path.realpath(path) in protected_roots:
            continue
        if name != _UPLOADS_DIR:
            mtime, size = _walk_stats(path)
            out.append(_Sweepable(path=path, mtime=mtime, size=size))
            continue
        try:
            conversations = sorted(os.listdir(path))
        except OSError:
            continue
        for conversation in conversations:
            conv_dir = os.path.join(path, conversation)
            if not os.path.isdir(conv_dir) or os.path.islink(conv_dir):
                continue
            if os.path.realpath(conv_dir) in protected_roots:
                continue
            try:
                upload_ids = sorted(os.listdir(conv_dir))
            except OSError:
                continue
            for upload_id in upload_ids:
                upload_dir = os.path.join(conv_dir, upload_id)
                if not os.path.isdir(upload_dir) or os.path.islink(upload_dir):
                    continue
                if os.path.realpath(upload_dir) in protected_roots:
                    continue
                mtime, size = _walk_stats(upload_dir)
                out.append(
                    _Sweepable(
                        path=upload_dir,
                        mtime=mtime,
                        size=size,
                        # The directory name IS the conversation id: uploads
                        # .upload_root strips everything outside [A-Za-z0-9_-]
                        # and truncates at 64, and a conversation id is
                        # already drawn from exactly that alphabet at exactly
                        # that length (main._CONVERSATION_ID_RE), so the
                        # sanitisation is a no-op for every id the app mints.
                        conversation_id=conversation,
                        upload_id=upload_id,
                    )
                )
    return out


def _prune_empty_conversation_dirs(base: str) -> None:
    """Remove `uploads/<conversation>/` once its last upload has gone.

    Only ever empty directories, and never `uploads/` itself: the top-level
    entry has to keep existing, both because the next upload expects it and
    because its removal is precisely what the old sweep did.
    """
    root = os.path.join(base, _UPLOADS_DIR)
    try:
        conversations = os.listdir(root)
    except OSError:
        return
    for conversation in conversations:
        conv_dir = os.path.join(root, conversation)
        try:
            if os.path.isdir(conv_dir) and not os.listdir(conv_dir):
                os.rmdir(conv_dir)
        except OSError:
            pass


def enforce_quota_and_ttl() -> None:
    """Delete workspace directories older than the TTL, then, if still over
    the global quota, delete the oldest until under it — PER UPLOAD.

    WHAT CHANGED AND WHY (F-01 / INF-4, 2026-09-09). This used to iterate the
    TOP-LEVEL entries of WORKSPACE_DIR and rmtree each one that was older than
    WORKSPACE_TTL_HOURS or that the quota could not afford. Every upload of
    every conversation of every user lives under ONE of those entries,
    `uploads/`, whose own mtime moves only when a brand-new conversation gets
    its first upload. So a quiet day plus one 20 MB upload — which calls this
    function before writing a byte — was enough to delete every conversation's
    originals, every in-flight `_parts/` directory and every just-landed file
    on the box, and the next part PUT of a 400 MB video answered 404.

    Now each `uploads/<conversation>/<upload_id>` is weighed on its own newest
    mtime and its own size, repository clones keep being weighed as the single
    directories they are, and three things are never removed at all:

    * an upload whose session is still `uploading` or `finalizing` and has not
      expired,
    * an upload with an `uploads` row created inside the TTL,
    * anything under VIDEO_DATA_DIR — which is not under WORKSPACE_DIR by
      design, and is skipped explicitly in case a deployment ever puts it
      there.

    An analysis is safe either way: `video/store.adopt_source` HARD-LINKS the
    source into `<VIDEO_DATA_DIR>/<sha256>/`, so unlinking the workspace copy
    only drops one link and the bytes stay (proved in
    tests/test_workspace_sweep.py on a real filesystem).

    Best-effort by contract: callers treat it as housekeeping and swallow
    failures, so nothing here raises.
    """
    base = settings.workspace_dir
    if not os.path.isdir(base):
        return
    import time

    now = time.time()
    ttl = settings.workspace_ttl_hours * 3600
    cutoff = now - ttl
    protected_roots = {os.path.realpath(settings.video_data_dir)}

    entries = _collect(base, protected_roots)

    # One `uploads` lookup per conversation that actually has a candidate in
    # it. A workspace where nothing is old enough and the quota is fine asks
    # the database nothing at all — this function runs on the upload path,
    # ahead of the first byte, and must not add a query per upload to it.
    quota = settings.workspace_quota_gb * 1024 ** 3
    total = sum(entry.size for entry in entries)
    needs_lookup = {
        entry.conversation_id
        for entry in entries
        if entry.conversation_id
        and (entry.mtime < cutoff or total > quota)
    }
    recent: dict = {}
    for conversation_id in needs_lookup:
        fresh = _recent_upload_ids(conversation_id, cutoff)
        if fresh is None:
            # The database could not answer. Treat every upload of this
            # conversation as live rather than delete on a guess.
            recent[conversation_id] = None
        else:
            recent[conversation_id] = fresh

    def _keep(entry: _Sweepable) -> bool:
        if entry.upload_id is None:
            return False  # a repository clone protects nothing
        fresh = recent.get(entry.conversation_id, set())
        if fresh is None:
            return True
        return _upload_is_live(entry, fresh)

    survivors: List[_Sweepable] = []
    for entry in entries:
        if entry.mtime >= cutoff or _keep(entry):
            survivors.append(entry)
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        total -= entry.size

    for entry in sorted(survivors, key=lambda e: e.mtime):  # oldest first
        if total <= quota:
            break
        if _keep(entry):
            # Still counted against the quota — it is really on the disk —
            # but not removable. If every survivor is live the loop simply
            # ends over quota, which is the truthful outcome: the bytes
            # belong to uploads that are in flight or about to be read.
            continue
        total -= entry.size
        shutil.rmtree(entry.path, ignore_errors=True)

    _prune_empty_conversation_dirs(base)


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
