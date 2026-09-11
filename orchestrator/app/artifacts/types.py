"""The vocabulary every artifact module shares: stages, statuses, formats,
effort budgets, storage paths, and the reference shape the chat and the API
hand to the browser. Constants and pure functions only — no I/O, no settings
reads at import time, no app imports beyond the standard library — so the
renderers, the job runner, the engine and the tests can all import this
without importing each other.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

# ----------------------------------------------------------------- kinds --

#: What a person is asking for, at the level that decides the template family
#: and the renderer set. Everything else (report vs SOP vs memo) is a
#: template_id inside the spec.
ArtifactKind = Literal["document", "presentation", "workbook"]
KINDS: Tuple[str, ...] = ("document", "presentation", "workbook")

#: A file format we can actually write and reopen. A format is never
#: promised that is not in this table. CSV joined on 2026-09-12 (CONTRACT-2
#: §1): a workbook sheet as a portable data file — data only, no styling.
FileFormat = Literal["pdf", "docx", "pptx", "xlsx", "csv"]
FORMATS: Tuple[str, ...] = ("pdf", "docx", "pptx", "xlsx", "csv")

MIME_TYPES: Dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
    #: A ZIP is a ROUTE (`GET …/zip` bundles a version's files), never a
    #: file format: it is in this table for the response header only and
    #: deliberately NOT in FORMATS, so no renderer is ever asked for one.
    "zip": "application/zip",
    "png": "image/png",
    "json": "application/json",
}

#: Which formats each kind may be rendered to. A conversion request outside
#: this table is refused with a plain sentence, not attempted. The first
#: entry is the kind's NATIVE format (the `primary` file role below). A
#: workbook can be delivered as an editable spreadsheet, a portable data
#: file, and a tabular Word/PDF document (landscape, repeating header) from
#: the same spec; a document or a deck can NOT be a csv/xlsx.
FORMATS_FOR_KIND: Dict[str, Tuple[str, ...]] = {
    "document": ("docx", "pdf"),
    "presentation": ("pptx", "pdf"),
    "workbook": ("xlsx", "csv", "docx", "pdf"),
}

#: The role a file plays in a version (CONTRACT-2 §2): `primary` is the
#: kind's native file (docx/pptx/xlsx), `companion` the same content in
#: another format (the PDF of a deck, the Word/PDF of a workbook), `data`
#: a CSV of one sheet.
FileRole = Literal["primary", "companion", "data"]
FILE_ROLES: Tuple[str, ...] = ("primary", "companion", "data")

#: Which formats a page preview (rasterised PDF) can stand for, and which
#: are read as a grid.
PAGE_FORMATS: Tuple[str, ...] = ("pdf", "docx", "pptx")
GRID_FORMATS: Tuple[str, ...] = ("xlsx", "csv")

# ---------------------------------------------------------------- stages --

#: The pipeline's stages, in order, with FIXED ids so the browser's timeline
#: keeps its rows across a reload and a re-attach (the same rule the video
#: pipeline follows: engines/video.py forwards stages as `step` events whose
#: id is the stage's position here).
STAGES: Tuple[str, ...] = (
    "intent",    # what is asked, for whom, in which formats
    "gather",    # conversation, uploads, Salesforce results, web sources
    "outline",   # the structure (think/max only; fast goes straight to compose)
    "compose",   # the ArtifactSpec, from the model, validated
    "render",    # every selected format, by code
    "validate",  # every file reopened and checked
    "preview",   # page images for the viewer
)
STEP_IDS: Dict[str, int] = {name: i + 1 for i, name in enumerate(STAGES)}
STAGE_TITLES: Dict[str, str] = {
    "intent": "Understanding the request",
    "gather": "Gathering sources",
    "outline": "Planning the document",
    "compose": "Writing the content",
    "render": "Building the files",
    "validate": "Checking the files",
    "preview": "Rendering the preview",
}

# -------------------------------------------------------------- statuses --

#: A job's coarse state — the database CHECK constraint and the metrics
#: `state`/`result` vocabularies use exactly these. The fine-grained position
#: is the stage above, kept on the row as `stage`.
JOB_STATUSES: Tuple[str, ...] = (
    "queued",      # accepted and persisted; no worker has it yet
    "running",     # a worker holds the lease
    "completed",
    "completed_with_warnings",
    "failed",
    "cancelled",
)
TERMINAL_STATUSES: Tuple[str, ...] = ("completed", "completed_with_warnings", "failed", "cancelled")

#: Why a job failed, in words a dashboard can group by and a person can read.
FailureCategory = Literal[
    "invalid_request", "source_unavailable", "model_failure", "renderer_failure",
    "validation_failure", "storage_failure", "quota_exceeded", "permission_denied",
    "cancelled", "dependency_unavailable",
]
FAILURE_CATEGORIES: Tuple[str, ...] = (
    "invalid_request", "source_unavailable", "model_failure", "renderer_failure",
    "validation_failure", "storage_failure", "quota_exceeded", "permission_denied",
    "cancelled", "dependency_unavailable",
)

#: What a version is: how it came to exist relative to the one before it.
Operation = Literal["create", "edit", "convert"]

# ------------------------------------------------------------- templates --

DOCUMENT_TEMPLATES: Tuple[str, ...] = (
    "executive_report", "brief", "sop", "technical_report", "research_report",
    "proposal", "meeting_summary", "generic",
)
PRESENTATION_TEMPLATES: Tuple[str, ...] = ("ceo", "training", "quarterly_review", "generic")
WORKBOOK_TEMPLATES: Tuple[str, ...] = ("tracker", "dashboard", "data", "generic")

#: Bumped when a template's rendered output changes meaning (a different
#: cover, a different page grid). Recorded on every version so a later
#: renderer does not silently re-render an old version differently.
TEMPLATE_VERSION = "1"
RENDERER_VERSION = "1"

# ---------------------------------------------------------------- effort --


@dataclass(frozen=True)
class EffortBudget:
    """What an effort level is allowed to spend on one artifact.

    The ONLY place these numbers live. Effort decides depth — research,
    review, visual QA, correction passes — never whether a file is made.
    """

    thinking: bool
    outline_pass: bool
    research: bool
    max_sources: int
    content_review: bool
    visual_qa: bool
    max_corrections: int
    max_sections: int
    max_slides: int
    max_sheets: int
    max_rows_per_sheet: int
    #: What the UI may promise: the product name for the level.
    label: str


EFFORT_BUDGETS: Dict[str, EffortBudget] = {
    "fast": EffortBudget(
        thinking=False, outline_pass=False, research=False, max_sources=0,
        content_review=False, visual_qa=False, max_corrections=1,
        max_sections=8, max_slides=12, max_sheets=3, max_rows_per_sheet=2000,
        label="Fast",
    ),
    "think": EffortBudget(
        thinking=True, outline_pass=True, research=True, max_sources=5,
        content_review=True, visual_qa=False, max_corrections=2,
        max_sections=12, max_slides=20, max_sheets=5, max_rows_per_sheet=5000,
        label="Think",
    ),
    "max": EffortBudget(
        thinking=True, outline_pass=True, research=True, max_sources=10,
        content_review=True, visual_qa=True, max_corrections=2,
        max_sections=16, max_slides=30, max_sheets=8, max_rows_per_sheet=5000,
        label="Max",
    ),
}

#: What the UI says about Max. A product name for the most thorough
#: workflow — orchestrated planning, research, review and visual checks —
#: and NOT a claim about the model. Tests pin the wording.
MAX_EFFORT_DESCRIPTION = "Highest-quality orchestrated workflow: plans, researches, writes, reviews and visually checks the file before releasing it."

# ---------------------------------------------------------------- limits --

#: Hard ceilings the renderers enforce whatever the spec asks for. They are
#: about the box, not the reader: a 60-page PDF is a valid document; a
#: 6,000-page one is a page-count bomb.
MAX_PAGES = 60
MAX_SLIDES = 40
MAX_SHEETS = 10
MAX_ROWS_PER_SHEET = 10_000
MAX_COLUMNS_PER_SHEET = 60
MAX_TABLE_ROWS = 200
MAX_TABLE_COLUMNS = 12
MAX_CHART_POINTS = 200
MAX_TEXT_CHARS = 200_000      # the whole spec's prose, summed
MAX_FILE_BYTES = 50 * 1024 * 1024
#: `GET …/zip` is refused when the version's recorded sizes sum past this:
#: the bundle is streamed, so the bound is on what a person downloads, not
#: on memory.
MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_PREVIEW_PAGES = 40        # pages rasterised for the viewer
PREVIEW_WIDTHS: Tuple[int, ...] = (240, 1400)   # thumbnail, page

# --------------------------------------------------------------- storage --

_ID_RE = re.compile(r"[a-f0-9]{32}")


def is_artifact_id(value: str) -> bool:
    """Artifact and job ids are uuid4 hex: 32 lowercase hex characters, and
    nothing else may ever be used to build a path."""
    return bool(_ID_RE.fullmatch(value or ""))


def artifact_dir(reports_dir: str, user_id: int, artifact_id: str) -> str:
    """`<reports>/artifacts/<user>/<artifact>` — owner-scoped and ID-keyed.

    Never under the flat legacy namespace `GET /reports/{filename}` resolves,
    which refuses nested paths anyway (core/report_paths.py). Raises on an id
    that is not an id, before any path is built from it.
    """
    if not is_artifact_id(artifact_id):
        raise ValueError("not an artifact id")
    return f"{reports_dir.rstrip('/')}/artifacts/{int(user_id)}/{artifact_id}"


def version_dir(reports_dir: str, user_id: int, artifact_id: str, version: int) -> str:
    return f"{artifact_dir(reports_dir, user_id, artifact_id)}/v{int(version)}"


#: File names inside a version directory. The rendered outputs are named by
#: slug + extension; these are the fixed ones.
MANIFEST_NAME = "manifest.json"
SPEC_NAME = "spec.json"
VALIDATION_NAME = "validation.json"
PREVIEW_PDF_NAME = "preview.pdf"
PREVIEWS_DIR = "previews"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug_for(title: str, fallback: str = "document") -> str:
    """A filename-safe stem: lowercase ASCII, hyphens, at most 60 characters,
    never empty. The display name keeps the real title."""
    base = _SLUG_RE.sub("-", (title or "").lower()).strip("-")[:60].strip("-")
    return base or fallback


def download_name(title: str, version: int, fmt: str, part: Optional[str] = None) -> str:
    """`quarterly-review-v2.pptx` — human, collision-safe within one artifact
    (the version is in the name), and the version directory keeps two
    artifacts with the same title apart. `part` names one piece of a
    multi-part delivery — the per-sheet CSV of a multi-sheet workbook —
    and lands as `quarterly-review-v2-pipeline.csv`; an empty part is no
    part, so a one-sheet workbook keeps the plain name."""
    stem = f"{slug_for(title)}-v{int(version)}"
    if part and part.strip():
        stem = f"{stem}-{slug_for(part, fallback='part')}"
    return f"{stem}.{fmt}"


_FILE_ID_RE = re.compile(r"[a-f0-9]{16}")


def file_id_for(artifact_id: str, version: int, role: str, fmt: str, sheet: str = "") -> str:
    """The identity of one file in one version: sixteen hex characters of
    sha1 over (artifact, version, role, format, sheet). Code-minted from
    what the file IS, never from bytes or a clock, so a retry, a re-render
    and a history reload all agree on it, and a legacy row (no id stored)
    can be given the same id it would have had (pipeline.ref_for)."""
    import hashlib

    key = f"{artifact_id}:{int(version)}:{role}:{fmt}:{sheet or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def is_file_id(value: str) -> bool:
    """Sixteen lowercase hex characters, and nothing else may name a file
    in a URL."""
    return bool(_FILE_ID_RE.fullmatch(value or ""))


# ------------------------------------------------------------ references --


@dataclass
class FileRef:
    """One file of one version, as stored in `artifact_versions.files` and
    sent on the wire (CONTRACT-2 §2). The positional fields are the
    original four-format contract; everything after `sheets` was added on
    2026-09-12 with a default, so a stored row from before then still
    loads. `file_id` is minted by the pipeline with `file_id_for`, never
    by a worker; an empty one marks a legacy ref (URLs fall back to the
    `/file/{format}` alias). `rows` counts DATA rows (header excluded) and
    `columns` the header, both validated after render by reopening."""

    format: str
    filename: str
    mime_type: str
    size: int
    sha256: str = ""
    pages: Optional[int] = None
    slides: Optional[int] = None
    sheets: Optional[int] = None
    file_id: str = ""
    role: str = "primary"
    title: str = ""
    rows: Optional[int] = None
    columns: Optional[int] = None

    def to_json(self) -> dict:
        out = {"format": self.format, "filename": self.filename, "mime_type": self.mime_type, "size": self.size, "role": self.role or "primary"}
        if self.file_id:
            out["file_id"] = self.file_id
        if self.title:
            out["title"] = self.title
        if self.sha256:
            out["sha256"] = self.sha256
        for key in ("pages", "slides", "sheets", "rows", "columns"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass
class ArtifactRef:
    """What the chat meta and the API say about one version of one artifact.

    Small on purpose: it rides on every history load. URLs are RELATIVE API
    paths built here, by code, from ids — the model never sees them.
    """

    artifact_id: str
    version: int
    job_id: str
    title: str
    kind: str
    status: str
    files: List[FileRef] = field(default_factory=list)
    preview_kind: str = "pages"      # "pages" (rasterised) | "grid" (workbook) | "none"
    preview_pages: int = 0
    warnings: List[str] = field(default_factory=list)
    created_at: str = ""
    operation: str = "create"
    parent_version: Optional[int] = None

    @property
    def base_path(self) -> str:
        return f"/artifacts/{self.artifact_id}/v/{self.version}"

    def _file_urls(self, f: dict) -> None:
        """Per-file URLs, by id when the file has one and by format (the
        alias route, first file of that format) when it does not — a ref
        persisted before ids existed still downloads. A grid format is
        previewed from its own file; a page format from the version's
        rasterised preview, which exists whenever the version has pages
        (a `pages` version always; a `grid` version when its Word/PDF
        companion was rendered — the pipeline counts that PDF's pages).
        With no pages the URL is "", which the browser reads as
        "download only"."""
        fid = f.get("file_id") or ""
        fmt = f.get("format") or ""
        if fid:
            f["download_url"] = f"{self.base_path}/f/{fid}?disposition=attachment"
            f["inline_url"] = f"{self.base_path}/f/{fid}?disposition=inline"
        else:
            f["download_url"] = f"{self.base_path}/file/{fmt}?disposition=attachment"
            f["inline_url"] = f"{self.base_path}/file/{fmt}?disposition=inline"
        if fmt in GRID_FORMATS:
            f["preview_url"] = f"{self.base_path}/grid?file={fid}" if fid else f"{self.base_path}/sheets"
        elif fmt in PAGE_FORMATS and (self.preview_kind == "pages" or self.preview_pages > 0):
            f["preview_url"] = f"{self.base_path}/preview"
        else:
            f["preview_url"] = ""

    def to_json(self) -> dict:
        files = [f.to_json() for f in self.files]
        for f in files:
            self._file_urls(f)
        out = {
            "artifact_id": self.artifact_id,
            "version": self.version,
            "job_id": self.job_id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "files": files,
            "preview_kind": self.preview_kind,
            "preview_pages": self.preview_pages,
            "preview_url": f"{self.base_path}/preview" if self.preview_kind == "pages" else (
                f"{self.base_path}/sheets" if self.preview_kind == "grid" else ""
            ),
            "thumbnail_url": f"{self.base_path}/preview/1.png?w=240" if self.preview_pages else "",
            "warnings": list(self.warnings),
            "created_at": self.created_at,
            "operation": self.operation,
            "status_url": f"/artifacts/jobs/{self.job_id}",
        }
        if self.parent_version is not None:
            out["parent_version"] = self.parent_version
        if len(files) >= 2:
            # One click for the whole delivery: the zip route bundles every
            # file of the version. A single file needs no bundle.
            out["download_all_url"] = f"{self.base_path}/zip"
            out["package"] = {"count": len(files)}
        return out
