"""Where an artifact lives on disk, and the file-level primitives the job
runner and the API share.

ONE DIRECTORY PER VERSION, OWNER-SCOPED AND ID-KEYED (types.version_dir):

    <REPORTS_DIR>/artifacts/<user_id>/<artifact_id>/
        v1/                     PUBLISHED — immutable from the rename on
            manifest.json       {artifact_id, version, files, sha256s, preview, warnings, ...}
            spec.json           the ArtifactSpec the files were rendered from
            validation.json     what the validate stage found
            <slug>-v1.pdf       one file per (role, format, sheet): the kind's
            <slug>-v1.docx      native file, its companions in other formats,
            <slug>-v1-data.csv  and a CSV per sheet of a workbook (CONTRACT-2 §2)
            preview.pdf         the canonical preview (pages for the viewer)
            previews/1-240.png  rasterised pages, cached on first request
        v2.tmp/                 WORKING — a job that has not published yet

A PUBLISHED DIRECTORY HOLDS EXACTLY THAT (CONTRACT §6): manifest.json,
spec.json, validation.json, the rendered files the manifest lists,
preview.pdf when the kind has one, and previews/. Everything else the working directory accumulates
is SCRATCH and is removed before the rename — `material.json` (the
conversation history, uploads text and Salesforce data the turn gathered;
needed only to resume a job BEFORE it publishes, and a copy of chat content
that would otherwise sit outside every retention path, since deleting a
conversation deliberately leaves artifacts alone), `transform.json` (what
code did to a pasted table, written by the composer for the render stage
of a later attempt; it carries a column label from the person's own paste
and is read by nothing after publication — it slipped into v<N>/ for a day
in 2026-09-12 because it was named in the pipeline and not here),
`render-job.json` and `render-report.json` (absolute paths of this
process's volume), the renderer's `preview.json` and matplotlib cache, and
the chart PNGs the renderer embedded. What the row needs from the report
and the preview meta after publication (preview_kind, preview_pages,
warnings) travels in the manifest, so a crash between the rename and the
row update can still be completed from the directory alone.

PUBLICATION IS A RENAME. Every stage writes into `v<N>.tmp/`; `publish()`
fsyncs what is there, writes the manifest, fsyncs the parent, and
os.replace()s the directory to `v<N>/`. A rename within one filesystem is
atomic, so a reader never sees a half-written version: either `v<N>/` does
not exist, or it is complete. A crash before the rename leaves `v<N>.tmp/`
for the sweep; a crash after it leaves a published directory whose row is
not yet marked completed, which the next attempt of the same job detects
(`publish` is idempotent on an existing version directory) and completes.

THE SWEEP NEVER TOUCHES A PUBLISHED DIRECTORY. `sweep_abandoned` walks
`v<N>.tmp` directories only, by name, and removes those older than
ARTIFACT_TMP_TTL_HOURS. A published `v<N>/` is removed by nothing here: the
quota is enforced at acceptance, and deletion is a person's explicit act.

NOT UNDER THE FLAT `/reports/<filename>` NAMESPACE. core/report_paths refuses
nested paths by design and bootstrap.claim_unbound_reports adopts every
unbound top-level file for the first super-admin; the `artifacts/` subtree
is neither listed nor adopted by them, and its files are served only through
the ID-keyed resolver below, which refuses any path that resolves outside
the version directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from typing import Any, List, Optional, Sequence, Tuple

from ..config import settings
from . import spec as S
from . import types as T

_HASH_CHUNK = 4 * 1024 * 1024
_TMP_SUFFIX = ".tmp"
#: Everything the runner writes into the working directory besides the
#: rendered files. Named here so the manifest can list the rendered files
#: without listing these.
RENDER_REPORT_NAME = "render-report.json"
MATERIAL_NAME = "material.json"
PREVIEW_META_NAME = "preview.json"
JOB_NAME = "render-job.json"
#: The composer's account of what code did to the data (rows copied, blanks
#: kept, hosts forward-filled), for the render stage's methodology note.
#: Named HERE, beside the other scratch, so the list below cannot miss it.
TRANSFORM_NAME = "transform.json"
#: The scratch a working directory carries that a published version must
#: not (module docstring). `publish()` removes these before the rename.
SCRATCH_NAMES = (RENDER_REPORT_NAME, MATERIAL_NAME, PREVIEW_META_NAME, JOB_NAME, TRANSFORM_NAME, ".mpl")
#: What a published directory may hold besides the rendered files and the
#: previews directory. The rendered files are named per version (slug +
#: version + an optional sheet part, types.download_name), so they cannot
#: be listed here: `publish()` protects every name the manifest's `files`
#: carries — the per-sheet CSVs of a workbook included — the same way.
PUBLISHED_NAMES = frozenset({T.MANIFEST_NAME, T.SPEC_NAME, T.VALIDATION_NAME, T.PREVIEW_PDF_NAME, T.PREVIEWS_DIR})


class StorageError(Exception):
    """A storage-level failure the job reports as `storage_failure`. The
    message is for a person; the path is in the log, not here."""


# --------------------------------------------------------------- paths --


def reports_dir() -> str:
    return settings.reports_dir


def artifact_dir(user_id: int, artifact_id: str) -> str:
    return T.artifact_dir(reports_dir(), user_id, artifact_id)


def version_dir(user_id: int, artifact_id: str, version: int) -> str:
    return T.version_dir(reports_dir(), user_id, artifact_id, version)


def version_workdir(user_id: int, artifact_id: str, version: int) -> str:
    """`v<N>.tmp` — where a job writes until it publishes."""
    return version_dir(user_id, artifact_id, version) + _TMP_SUFFIX


def is_published(user_id: int, artifact_id: str, version: int) -> bool:
    return os.path.isfile(os.path.join(version_dir(user_id, artifact_id, version), T.MANIFEST_NAME))


def ensure_workdir(user_id: int, artifact_id: str, version: int) -> str:
    path = version_workdir(user_id, artifact_id, version)
    os.makedirs(os.path.join(path, T.PREVIEWS_DIR), exist_ok=True)
    return path


# ----------------------------------------------------------- json files --


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path: str, payload: Any) -> None:
    """Replace `path` with `payload`, or leave whatever was there intact
    (tmp + fsync + rename, the video store's idiom)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".stage-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_spec(directory: str, spec: S.ArtifactSpec) -> str:
    path = os.path.join(directory, T.SPEC_NAME)
    write_json(path, spec.model_dump(mode="json", exclude_none=True))
    return path


def read_spec(directory: str) -> Optional[S.ArtifactSpec]:
    """The stored spec, or None when there is none or it does not parse.
    A NEWER spec_version raises (spec.load) rather than rendering a guess."""
    data = read_json(os.path.join(directory, T.SPEC_NAME))
    if not isinstance(data, dict):
        return None
    return S.load(data)


# ------------------------------------------------------------ checksums --


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# -------------------------------------------------------------- publish --


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_tree(root: str) -> None:
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                fd = os.open(os.path.join(dirpath, name), os.O_RDONLY)
            except OSError:
                continue
            try:
                os.fsync(fd)
            except OSError:
                pass
            finally:
                os.close(fd)
        _fsync_dir(dirpath)


def build_manifest(
    *,
    artifact_id: str,
    version: int,
    files: List[dict],
    spec_version: int = S.SPEC_VERSION,
    template_version: str = T.TEMPLATE_VERSION,
    renderer_version: str = T.RENDERER_VERSION,
    preview: Optional[dict] = None,
    warnings: Optional[Sequence[str]] = None,
) -> dict:
    """`manifest.json`: what the directory holds and what produced it. The
    sha256s are repeated as a map so a reader can verify one file without
    walking the list. `preview` ({preview_kind, preview_pages, thumbnails})
    and `warnings` are what the render report and the preview meta carried
    — those two files are scratch and do not survive publication, so the
    manifest is where a resume after the rename reads them."""
    preview = dict(preview or {})
    seen: List[str] = []
    for text in warnings or []:
        line = str(text or "")[:300]
        if line and line not in seen:
            seen.append(line)
    return {
        "artifact_id": artifact_id,
        "version": int(version),
        "files": [dict(f) for f in files],
        "sha256s": {str(f.get("filename")): str(f.get("sha256") or "") for f in files},
        "spec_version": int(spec_version),
        "template_version": str(template_version),
        "renderer_version": str(renderer_version),
        "preview": {
            "preview_kind": str(preview.get("preview_kind") or "none"),
            "preview_pages": int(preview.get("preview_pages") or 0),
            "thumbnails": [str(t) for t in (preview.get("thumbnails") or [])],
        },
        "warnings": seen,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def published_names(manifest: Optional[dict]) -> frozenset:
    """Every name a published directory keeps: the fixed ones plus the
    rendered files the manifest lists (basenames — a manifest is written
    by build_manifest from the validate stage's list, never by a request).
    A scratch list that names one of these is refused, not followed."""
    names = set(PUBLISHED_NAMES)
    for entry in (manifest or {}).get("files") or []:
        base = os.path.basename(str((entry or {}).get("filename") or "")) if isinstance(entry, dict) else ""
        if base:
            names.add(base)
    return frozenset(names)


def _remove_scratch(work_dir: str, extra: Sequence[str], keep: Optional[frozenset] = None) -> None:
    """Drop the scratch (SCRATCH_NAMES plus `extra` basenames the renderer
    named, e.g. its chart PNGs) from a working directory. Basenames only —
    a name with a path component cannot reach outside the directory. `keep`
    (published_names of the manifest) is never removed whatever the caller
    lists: a per-sheet CSV named as scratch by mistake would otherwise
    publish a version whose download 404s."""
    keep = keep if keep is not None else PUBLISHED_NAMES
    for name in [*SCRATCH_NAMES, *extra]:
        base = os.path.basename(str(name or ""))
        if not base or base in keep:
            continue
        path = os.path.join(work_dir, base)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass


def publish(work_dir: str, manifest: dict, *, scratch: Sequence[str] = ()) -> str:
    """`v<N>.tmp/` → `v<N>/`, atomically. Returns the published directory.

    Idempotent: when `v<N>/` already exists (a previous attempt renamed and
    then died before the row was marked) the working directory is discarded
    and the published one is the answer — a published version is immutable.

    The scratch (SCRATCH_NAMES and any `scratch` basenames the caller adds)
    is removed first, so the published directory holds exactly what the
    module docstring lists.
    """
    if not work_dir.endswith(_TMP_SUFFIX):
        raise StorageError("not a working directory")
    final = work_dir[: -len(_TMP_SUFFIX)]
    if os.path.isfile(os.path.join(final, T.MANIFEST_NAME)):
        shutil.rmtree(work_dir, ignore_errors=True)
        return final
    if not os.path.isdir(work_dir):
        raise StorageError("the working directory is gone")
    _remove_scratch(work_dir, scratch, keep=published_names(manifest))
    write_json(os.path.join(work_dir, T.MANIFEST_NAME), manifest)
    _fsync_tree(work_dir)
    if os.path.isdir(final):
        # A directory with no manifest is not a published version — a
        # crash between mkdir and rename cannot produce one, but a manual
        # copy could. It is in the way, and it is not ours to keep.
        shutil.rmtree(final, ignore_errors=True)
    try:
        os.replace(work_dir, final)
    except OSError as exc:
        raise StorageError("the version could not be published to the reports volume") from exc
    _fsync_dir(os.path.dirname(final))
    return final


def read_manifest(user_id: int, artifact_id: str, version: int) -> Optional[dict]:
    data = read_json(os.path.join(version_dir(user_id, artifact_id, version), T.MANIFEST_NAME))
    return data if isinstance(data, dict) else None


def remove_workdir(work_dir: str) -> None:
    """Drop a working directory (never a published one)."""
    if work_dir.endswith(_TMP_SUFFIX):
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------- space --


def free_space_ok(path: Optional[str] = None) -> bool:
    """True when the reports volume has ARTIFACT_MIN_FREE_MB left. A volume
    that cannot be measured is reported as not ok: a job that starts on a
    full disk fails half-way through a render, which is the worse failure."""
    root = path or os.path.join(reports_dir(), "artifacts")
    probe = root
    while probe and not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    if not probe:
        return False
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return False
    return usage.free >= int(settings.artifact_min_free_mb) * 1024 * 1024


def free_space_mb(path: Optional[str] = None) -> Optional[int]:
    root = path or os.path.join(reports_dir(), "artifacts")
    probe = root
    while probe and not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    if not probe:
        return None
    try:
        return int(shutil.disk_usage(probe).free // (1024 * 1024))
    except OSError:
        return None


def volume_writable() -> bool:
    """Can this process create the artifacts root and write inside it? What
    /health reports as `volume_writable`. Creates the root when it can."""
    root = os.path.join(reports_dir(), "artifacts")
    try:
        os.makedirs(root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".probe-", dir=root)
        os.close(fd)
        os.unlink(tmp)
    except OSError:
        return False
    return True


def quota_ok(user_id: int, published_bytes: Optional[int] = None) -> bool:
    """Is this person under ARTIFACT_USER_QUOTA_MB?

    Measured on DISK, not from the rows: a version holds its files, an
    identical preview.pdf, and up to ~50 MB of page images once a viewer has
    scrolled it, and the rows count only the files (review, 2026-09-11).
    `published_bytes` lets a test (or a caller that already walked the tree)
    hand the number in.
    """
    if published_bytes is None:
        root = os.path.join(reports_dir(), "artifacts", str(int(user_id)))
        published_bytes = dir_bytes(root) if os.path.isdir(root) else 0
    return int(published_bytes) < int(settings.artifact_user_quota_mb) * 1024 * 1024


def dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------- sweep --


def _is_workdir_name(name: str) -> bool:
    return name.startswith("v") and name.endswith(_TMP_SUFFIX) and name[1:-len(_TMP_SUFFIX)].isdigit()


def sweep_abandoned(ttl_hours: int, *, skip: Optional[set] = None) -> int:
    """Remove `v<N>.tmp` directories older than `ttl_hours`. Never a
    published `v<N>/`. `skip` names working directories that belong to jobs
    running in this process (a long job must not lose its files to the
    sweep because it started before the TTL). Returns the count removed.

    Both sides of the `skip` comparison are realpath'd: `skip` holds paths
    from types.artifact_dir, which rstrips a trailing '/' off REPORTS_DIR,
    while os.scandir builds its entries from the raw setting — with a
    REPORTS_DIR ending in '/' the two never matched and a live job older
    than the TTL would have lost its working directory mid-run."""
    root = os.path.join(reports_dir(), "artifacts")
    cutoff = time.time() - max(1, int(ttl_hours)) * 3600
    removed = 0
    skip = {os.path.realpath(p) for p in (skip or set())}
    try:
        users = list(os.scandir(root))
    except OSError:
        return 0
    for user in users:
        if not user.is_dir():
            continue
        try:
            arts = list(os.scandir(user.path))
        except OSError:
            continue
        for art in arts:
            if not art.is_dir() or not T.is_artifact_id(art.name):
                continue
            try:
                entries = list(os.scandir(art.path))
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir() or not _is_workdir_name(entry.name) or os.path.realpath(entry.path) in skip:
                    continue
                try:
                    if entry.stat().st_mtime > cutoff:
                        continue
                except OSError:
                    continue
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
    return removed


# ------------------------------------------------------------- resolve --


class PathRefused(Exception):
    """The requested file is not inside the version directory."""


def _inside(root: str, path: str) -> str:
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise PathRefused("outside the version directory")
    return real


def _safe_published_name(fmt: str, filename: str) -> bool:
    """A name the version row may carry for a rendered file: a bare
    basename (no separator of either kind, no leading dot) whose extension
    is `fmt`, `fmt` a type we serve (every MIME_TYPES key), and never one
    of the directory's fixed names — manifest.json, spec.json,
    validation.json and preview.pdf are the version's bookkeeping and its
    preview, served (or not) by their own routes, and no rendered file is
    ever named like them (types.download_name)."""
    if fmt not in T.MIME_TYPES or not filename:
        return False
    if "/" in filename or "\\" in filename or filename.startswith(".") or filename in ("..", "."):
        return False
    if filename in PUBLISHED_NAMES:
        return False
    return filename.endswith("." + fmt)


def resolve_version_file(user_id: int, artifact_id: str, version: int, fmt: str, filename: str) -> str:
    """The absolute path of one published file, verified to sit inside the
    version directory. `filename` is what the version row recorded (built
    by types.download_name — never request-supplied); `fmt` must be a type
    we serve (a MIME_TYPES key) and match the extension. Raises PathRefused."""
    if not _safe_published_name(fmt, filename):
        raise PathRefused("not a file this version has")
    root = version_dir(user_id, artifact_id, version)
    return _inside(root, os.path.join(root, filename))


def resolve_file_by_id(user_id: int, artifact_id: str, version: int, file_id: str,
                       files: Optional[Sequence[dict]] = None) -> Tuple[str, dict]:
    """`(path, entry)` for the file `file_id` names in this version — the
    entry is the version row's dict for it (file_id, role, format,
    filename, title, size, sha256, …) and the path is verified to sit
    inside the version directory.

    `files` is the version row's list as the pipeline stores it (with a
    legacy row's ids synthesised by pipeline.ref_for, so a version from
    before ids existed resolves too); when the caller has none, the
    manifest on disk is read, which carries the same list. A value that
    is not a file id, an id no entry carries, an entry whose name has a
    separator or whose extension is not its format, and a path outside
    the directory are all PathRefused — the API answers 404 to each."""
    if not T.is_file_id(file_id):
        raise PathRefused("not a file id")
    if files is None:
        manifest = read_manifest(user_id, artifact_id, version) or {}
        files = list(manifest.get("files") or [])
    for entry in files:
        if not isinstance(entry, dict) or str(entry.get("file_id") or "") != file_id:
            continue
        fmt = str(entry.get("format") or "")
        filename = str(entry.get("filename") or "")
        path = resolve_version_file(user_id, artifact_id, version, fmt, filename)
        return path, dict(entry)
    raise PathRefused("not a file this version has")


def resolve_preview_pdf(user_id: int, artifact_id: str, version: int) -> str:
    root = version_dir(user_id, artifact_id, version)
    return _inside(root, os.path.join(root, T.PREVIEW_PDF_NAME))


def preview_png_path(user_id: int, artifact_id: str, version: int, page: int, width: int) -> str:
    """`previews/<page>-<width>.png` inside the version directory. The page
    and width are integers the API validated; anything else cannot build a
    path here."""
    if int(page) < 1 or int(width) not in T.PREVIEW_WIDTHS:
        raise PathRefused("not a preview this version has")
    root = version_dir(user_id, artifact_id, version)
    return _inside(root, os.path.join(root, T.PREVIEWS_DIR, f"{int(page)}-{int(width)}.png"))


def write_bytes(path: str, data: bytes) -> int:
    """tmp + fsync + rename for binary content (a rasterised page)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".png-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return len(data)


__all__ = [
    "StorageError", "PathRefused", "RENDER_REPORT_NAME", "MATERIAL_NAME", "PREVIEW_META_NAME", "JOB_NAME", "TRANSFORM_NAME",
    "SCRATCH_NAMES", "PUBLISHED_NAMES", "published_names",
    "artifact_dir", "version_dir", "version_workdir", "is_published", "ensure_workdir",
    "read_json", "write_json", "write_spec", "read_spec", "sha256_file",
    "build_manifest", "publish", "read_manifest", "remove_workdir",
    "free_space_ok", "free_space_mb", "volume_writable", "quota_ok", "dir_bytes",
    "sweep_abandoned", "resolve_version_file", "resolve_file_by_id", "resolve_preview_pdf", "preview_png_path", "write_bytes",
]
