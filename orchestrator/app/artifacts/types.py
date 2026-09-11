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
#: promised that is not in this table.
FileFormat = Literal["pdf", "docx", "pptx", "xlsx"]
FORMATS: Tuple[str, ...] = ("pdf", "docx", "pptx", "xlsx")

MIME_TYPES: Dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "png": "image/png",
    "json": "application/json",
}

#: Which formats each kind may be rendered to. A conversion request outside
#: this table is refused with a plain sentence, not attempted.
FORMATS_FOR_KIND: Dict[str, Tuple[str, ...]] = {
    "document": ("pdf", "docx"),
    "presentation": ("pptx", "pdf"),
    "workbook": ("xlsx",),
}

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
MAX_PREVIEW_PAGES = 40        # pages rasterised for the viewer
PREVIEW_WIDTHS: Tuple[int, ...] = (240, 1400)   # thumbnail, page

# --------------------------------------------------------------- storage --

_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def is_artifact_id(value: str) -> bool:
    """Artifact and job ids are uuid4 hex: 32 lowercase hex characters, and
    nothing else may ever be used to build a path."""
    return bool(_ID_RE.match(value or ""))


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


def download_name(title: str, version: int, fmt: str) -> str:
    """`quarterly-review-v2.pptx` — human, collision-safe within one artifact
    (the version is in the name), and the version directory keeps two
    artifacts with the same title apart."""
    return f"{slug_for(title)}-v{int(version)}.{fmt}"


# ------------------------------------------------------------ references --


@dataclass
class FileRef:
    format: str
    filename: str
    mime_type: str
    size: int
    sha256: str = ""
    pages: Optional[int] = None
    slides: Optional[int] = None
    sheets: Optional[int] = None

    def to_json(self) -> dict:
        out = {"format": self.format, "filename": self.filename, "mime_type": self.mime_type, "size": self.size}
        if self.sha256:
            out["sha256"] = self.sha256
        for key in ("pages", "slides", "sheets"):
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

    def to_json(self) -> dict:
        files = [f.to_json() for f in self.files]
        for f in files:
            f["download_url"] = f"{self.base_path}/file/{f['format']}?disposition=attachment"
            f["inline_url"] = f"{self.base_path}/file/{f['format']}?disposition=inline"
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
        return out
