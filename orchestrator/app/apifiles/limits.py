"""Every technical ceiling of the Files API, read at call time (design §8).

NOT USAGE LIMITS. The owner removed every rate, quota and concurrency limit
from `/v1` (CONTRACT §12.1, 2026-09-13). What is here is physics: Cloudflare's
100 MB request wall, the disk the chat app, LanceDB and the model cache share,
and the unified memory the vLLM head lives in. Each default carries the reason
it is that number.

HOW A VALUE IS READ. `getattr(settings, name.lower(), None)` first, so an
operator who adds the setting to `config.py` (integration item) and a test
that monkeypatches `settings` both win; otherwise `os.environ[name]` parsed
with `config.py`'s own rules — blank means the default, a non-number raises
`ValueError` exactly as `config._int` / `config._float` would at import. Read
PER CALL rather than once, because `config.py` is another engineer's file this
wave and a value captured at import could not be changed by a test or by an
operator's `.env` without a restart of this module's importers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..config import settings

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


def _setting(name: str) -> Any:
    return getattr(settings, name.lower(), None)


def _int(name: str, default: int) -> int:
    value = _setting(name)
    if value is not None and value != "":
        return int(value)
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    value = _setting(name)
    if value is not None and value != "":
        return float(value)
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _str(name: str, default: str) -> str:
    value = _setting(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


# ------------------------------------------------------------- storage --


def files_dir() -> str:
    """PUBLIC_API_FILES_DIR, default /data/api-files.

    Same ext4 volume as /data/video (hard links between the two work), and NOT
    under WORKSPACE_DIR, whose 24 h TTL and 20 GB quota (`core/repo.py:305`)
    would delete API files and evict chat uploads (design §1.3)."""
    return _str("PUBLIC_API_FILES_DIR", "/data/api-files")


def video_data_dir() -> str:
    """The video pipeline's root, the second tree `storage.remove_tree` may
    delete under (API audio/video analyses live there, design §7.1)."""
    value = getattr(settings, "video_data_dir", None)
    return str(value or os.environ.get("VIDEO_DATA_DIR") or "/data/video")


def min_free_bytes() -> int:
    """PUBLIC_API_FILES_MIN_FREE_GIB, default 250 GiB (design §7.4).

    One maximum upload's assembly needs 200 GiB transiently; /data/lancedb is
    119 G and growing, Prometheus keeps 20 GiB and the models are 68 G on the
    same filesystem (measured 2026-09-13: 2.7 T free of 3.7 T). Postgres and
    chat uploads must never be the ones to fail."""
    return int(_float("PUBLIC_API_FILES_MIN_FREE_GIB", 250.0) * _GIB)


# ------------------------------------------------------------- uploads --


def part_max_bytes() -> int:
    """PUBLIC_API_FILES_PART_MAX_BYTES, default 64 MiB (67,108,864).

    Cloudflare's request wall is 100,000,000 B; 64 MiB plus ~200 B of
    multipart framing leaves 32.9 MB of headroom. It equals openai-python's
    DEFAULT_PART_SIZE, so `upload_file_chunked` works at its defaults. The
    chat rail's 90 MiB left 5.6 MB and is not used for /v1 (design §3.1)."""
    return _int("PUBLIC_API_FILES_PART_MAX_BYTES", 64 * _MIB)


def single_max_bytes() -> int:
    """PUBLIC_API_FILES_SINGLE_MAX_BYTES, default 64 MiB: the same wall, and
    openai-python reads a whole `Path` into memory for `files.create`."""
    return _int("PUBLIC_API_FILES_SINGLE_MAX_BYTES", 64 * _MIB)


def max_body_bytes() -> int:
    """PUBLIC_API_FILES_MAX_BODY_BYTES, default 68,157,440 (64 MiB + 1 MiB of
    multipart framing): the transport cap on routes 1 and 11."""
    return _int("PUBLIC_API_FILES_MAX_BODY_BYTES", 65 * _MIB)


def upload_max_bytes() -> int:
    """PUBLIC_API_FILES_UPLOAD_MAX_BYTES, default 100 GiB.

    Assembly transiently needs 2x on disk (200 GiB, 7% of today's free); ~7 min
    of fsynced copy at the measured 260 MB/s and ~43 s of sha256 at
    2,365 MiB/s on one core (design §3.2)."""
    return _int("PUBLIC_API_FILES_UPLOAD_MAX_BYTES", 100 * _GIB)


def max_parts() -> int:
    """PUBLIC_API_FILES_MAX_PARTS, default 10,000: a `complete` body naming
    10,000 part ids is ~310 KB of JSON, inside the 1 MiB JSON cap."""
    return _int("PUBLIC_API_FILES_MAX_PARTS", 10_000)


def json_body_max_bytes() -> int:
    """The JSON cap for `POST /v1/uploads` and `complete` (CONTRACT §12: 1 MiB)."""
    return _int("PUBLIC_API_FILES_JSON_MAX_BYTES", 1 * _MIB)


def upload_idle_ttl_s() -> float:
    """PUBLIC_API_UPLOAD_IDLE_TTL_HOURS, default 24: a pending upload lives
    24 h past its last accepted part. A 100 GiB upload at 10 Mbit/s takes
    ~24 h; V29's fixed TTL from creation would expire it mid-way (§7.3)."""
    return _float("PUBLIC_API_UPLOAD_IDLE_TTL_HOURS", 24.0) * 3600.0


def upload_max_ttl_s() -> float:
    """PUBLIC_API_UPLOAD_MAX_TTL_HOURS, default 168 (7 days): the hard cap on
    the sliding expiry."""
    return _float("PUBLIC_API_UPLOAD_MAX_TTL_HOURS", 168.0) * 3600.0


def upload_record_ttl_s() -> float:
    """Completed, cancelled and expired upload ROWS are kept 30 days for
    resume and status reads after the fact, then deleted (§7.3)."""
    return _float("PUBLIC_API_UPLOAD_RECORD_TTL_DAYS", 30.0) * 86400.0


def finalizing_stale_s() -> float:
    """A `finalizing` upload untouched this long returns to `pending` (V29
    pattern). `complete` holds the state only for O(parts) row work, so 600 s
    is never a live request (§2.14 step 6)."""
    return _float("PUBLIC_API_UPLOAD_FINALIZING_STALE_S", 600.0)


def complete_busy_wait_s() -> float:
    """How long a second `complete` waits for the winner to publish its result
    before answering 409 with Retry-After (§2.14 step 1: "up to 2 s")."""
    return _float("PUBLIC_API_UPLOAD_COMPLETE_BUSY_WAIT_S", 2.0)


def purge_wait_s() -> float:
    """How long `POST /v1/files` (and nothing else) waits for the in-flight
    purge of the SAME bytes in the same project before answering a retryable
    503 (review finding, 2026-09-13). A purge of a document blob is one rename
    and one row delete (milliseconds); an audio/video purge first cancels the
    ffmpeg/whisper job, which is seconds. 8 s stays inside the no-timeout
    design's 15 s first-byte invariant with room for the ingest itself."""
    return _float("PUBLIC_API_FILES_PURGE_WAIT_S", 8.0)


def assembly_renew_s() -> float:
    """The `assemble` stage renews its lease on a timer as well as on byte
    progress (review finding, 2026-09-13): a final fsync of a 100 GiB copy can
    flush GiBs of dirty pages with no byte moving, and a lease that lapses
    then lets another worker start a duplicate copy. A third of the lease."""
    return _float("PUBLIC_API_FILES_ASSEMBLY_RENEW_S", max(1.0, assembly_lease_s() / 3.0))


def assembly_lease_s() -> float:
    """The `assemble` stage's lease. Renewed on every progress tick (at most
    every few seconds while bytes move), so a crashed assembler's upload is
    re-claimed ~90 s later — the same lease the processing jobs use (§4.2)."""
    return _float("PUBLIC_API_FILES_ASSEMBLY_LEASE_S", 90.0)


def assembly_max_attempts() -> int:
    """After this many assembly attempts that could not finish (a part file
    missing or short on disk), the file ends `status: error` instead of being
    re-claimed forever. Same count as processing (`VIDEO_MAX_ATTEMPTS` 5)."""
    return _int("PUBLIC_API_FILES_ASSEMBLY_MAX_ATTEMPTS", 5)


def sweep_interval_s() -> float:
    """Upload expiry / finalizing reset / leftovers: every 10 min (§7.3)."""
    return _float("PUBLIC_API_FILES_SWEEP_INTERVAL_S", 600.0)


def leftover_max_age_s() -> float:
    """`_single`, `_inline` and stray `*.tmp` older than 24 h are crash
    leftovers (§7.3). A 64 MiB part at the 1.79 Mbit/s floor takes 5 min."""
    return _float("PUBLIC_API_FILES_LEFTOVER_MAX_AGE_S", 86400.0)


def orphan_dir_min_age_s() -> float:
    """A directory with no row is removed only once older than 1 h (§7.3), so
    a directory created a moment before its row commits is never swept."""
    return _float("PUBLIC_API_FILES_ORPHAN_MIN_AGE_S", 3600.0)


def sweep_batch() -> int:
    """Rows per sweep pass (§7.3: "each pass is bounded, ≤ 200 rows")."""
    return _int("PUBLIC_API_FILES_SWEEP_BATCH", 200)


def list_max_limit() -> int:
    """`GET /v1/files?limit=` ceiling, 10,000 (OpenAI parity, design §2.4)."""
    return _int("PUBLIC_API_FILES_LIST_MAX_LIMIT", 10_000)


def tombstone_days() -> int:
    """PUBLIC_API_FILES_TOMBSTONE_DAYS, default 30: deleted-file rows (audit)."""
    return _int("PUBLIC_API_FILES_TOMBSTONE_DAYS", 30)


# ---------------------------------------------------------- processing --


def sync_ready_wait_s() -> float:
    """PUBLIC_API_FILES_SYNC_READY_WAIT_S, default 30: pre-header silence on a
    sync model request waiting for processing, + a 30 s gate < 100 s."""
    return _float("PUBLIC_API_FILES_SYNC_READY_WAIT_S", 30.0)


def cpu_jobs() -> int:
    """PUBLIC_API_FILES_CPU_JOBS, default 2: each extraction child may take
    8 GiB of address space on 121 GiB shared with the vLLM head (47 GiB
    available at measurement). The `assemble` stage runs in this lane too."""
    return _int("PUBLIC_API_FILES_CPU_JOBS", 2)


def media_jobs() -> int:
    """PUBLIC_API_FILES_MEDIA_JOBS, default 1: the video pipeline's whole-GPU
    cost; chat's lane keeps its own slot."""
    return _int("PUBLIC_API_FILES_MEDIA_JOBS", 1)


def processing_lease_s() -> float:
    """Blob processing lease, renewed every 30 s (§4.2)."""
    return _float("PUBLIC_API_FILES_PROCESSING_LEASE_S", 90.0)


def processing_retry_delay_s() -> float:
    """Deferred blob not due before now + 300 s (§4.2)."""
    return _float("PUBLIC_API_FILES_PROCESSING_RETRY_DELAY_S", 300.0)


def processing_max_attempts() -> int:
    return _int("PUBLIC_API_FILES_PROCESSING_MAX_ATTEMPTS", 5)


def extract_rlimit_as_bytes() -> int:
    """PUBLIC_API_FILES_EXTRACT_RLIMIT_AS_GIB, default 8: bound a hostile
    parser (design §4.4, R5)."""
    return int(_float("PUBLIC_API_FILES_EXTRACT_RLIMIT_AS_GIB", 8.0) * _GIB)


@dataclass(frozen=True)
class KindCaps:
    """The ceilings that apply to one kind (design §8). None = not bounded by
    this dimension (e.g. pages for an image)."""

    bytes: int
    pages: Optional[int] = None
    ocr_pages: Optional[int] = None
    seconds: Optional[float] = None
    pixels: Optional[int] = None
    wall_s: Optional[float] = None
    cpu_s: Optional[float] = None


def kind_caps(kind: str) -> KindCaps:
    """Per-kind ceilings. Wall/CPU seconds are the extraction child's
    deadlines; they are the no-timeout design's R6 residuals and are sized far
    above the measured rates so they only ever stop a hostile input."""
    upload_max = upload_max_bytes()
    text_max = _int("PUBLIC_API_FILES_TEXT_MAX_BYTES", 1 * _GIB)
    table: Dict[str, KindCaps] = {
        # 2,054 pages/s text layer → 10k pages ≈ 5 s; 1 GiB bounds PDFium's
        # working set in the child.
        "pdf": KindCaps(
            bytes=_int("PUBLIC_API_FILES_PDF_MAX_BYTES", 1 * _GIB),
            pages=_int("PUBLIC_API_FILES_PDF_MAX_PAGES", 10_000),
            ocr_pages=_int("PUBLIC_API_FILES_OCR_PAGE_BUDGET", 1_000),
            wall_s=3600.0,
            cpu_s=3600.0,
        ),
        "document": KindCaps(bytes=_int("PUBLIC_API_FILES_OFFICE_MAX_BYTES", 512 * _MIB), wall_s=1800.0, cpu_s=1800.0),
        "presentation": KindCaps(bytes=_int("PUBLIC_API_FILES_OFFICE_MAX_BYTES", 512 * _MIB), wall_s=1800.0, cpu_s=1800.0),
        # measured 26,929 rows/s → ~9 M rows ≈ 6 min single core.
        "spreadsheet": KindCaps(bytes=_int("PUBLIC_API_FILES_XLSX_MAX_BYTES", 256 * _MIB), wall_s=3600.0, cpu_s=3600.0),
        "tabular": KindCaps(bytes=_int("PUBLIC_API_FILES_TABULAR_MAX_BYTES", upload_max), wall_s=7200.0, cpu_s=7200.0),
        "text": KindCaps(bytes=text_max, wall_s=3600.0, cpu_s=3600.0),
        "html": KindCaps(bytes=text_max, wall_s=3600.0, cpu_s=3600.0),
        "image": KindCaps(
            bytes=_int("PUBLIC_API_FILES_IMAGE_MAX_BYTES", 64 * _MIB),
            # Pillow's decompression-bomb default.
            pixels=_int("PUBLIC_API_FILES_IMAGE_MAX_PIXELS", 89_478_485),
            wall_s=300.0,
            cpu_s=300.0,
        ),
        # VIDEO_MAX_DURATION_S; duration, not bytes, drives the cost.
        "audio": KindCaps(bytes=upload_max, seconds=_float("PUBLIC_API_FILES_MEDIA_MAX_SECONDS", 14_400.0)),
        "video": KindCaps(bytes=upload_max, seconds=_float("PUBLIC_API_FILES_MEDIA_MAX_SECONDS", 14_400.0)),
    }
    return table.get(kind, KindCaps(bytes=upload_max))


def index_max_chunks() -> int:
    """PUBLIC_API_FILES_INDEX_MAX_CHUNKS, default 50,000: 195 MiB memmap,
    4.1 ms search measured."""
    return _int("PUBLIC_API_FILES_INDEX_MAX_CHUNKS", 50_000)


def html_readable_max_bytes() -> int:
    return _int("PUBLIC_API_FILES_HTML_READABLE_MAX_BYTES", 64 * _MIB)


# --------------------------------------------------------- model input --


def inline_max_tokens() -> int:
    """PUBLIC_API_FILES_INLINE_MAX_TOKENS, default 100,000: keeps requests in
    the NORMAL admission lanes (long-lane threshold 131,072)."""
    return _int("PUBLIC_API_FILES_INLINE_MAX_TOKENS", 100_000)


def retrieval_tokens() -> int:
    return _int("PUBLIC_API_FILES_RETRIEVAL_TOKENS", 32_000)


def retrieval_max_tokens() -> int:
    return _int("PUBLIC_API_FILES_RETRIEVAL_MAX_TOKENS", 200_000)


def max_file_parts_per_request() -> int:
    return _int("PUBLIC_API_FILES_MAX_PER_REQUEST", 20)


def max_videos_per_request() -> int:
    return _int("PUBLIC_API_FILES_VIDEOS_PER_REQUEST", 3)


def video_frames(detail: str) -> int:
    """Frames per video in a request: 3, or 8 for `detail: high`."""
    if str(detail or "").lower() == "high":
        return _int("PUBLIC_API_FILES_VIDEO_FRAMES_HIGH", 8)
    return _int("PUBLIC_API_FILES_VIDEO_FRAMES", 3)


def pdf_vision_pages(detail: str) -> int:
    if str(detail or "").lower() == "high":
        return _int("PUBLIC_API_FILES_PDF_VISION_PAGES_HIGH", 6)
    return _int("PUBLIC_API_FILES_PDF_VISION_PAGES", 2)


def inline_sync_max_ocr_pages() -> int:
    return _int("PUBLIC_API_INLINE_SYNC_MAX_OCR_PAGES", 8)


def yield_to_chat_video_max_wait_s() -> float:
    return _float("PUBLIC_API_FILES_YIELD_TO_CHAT_VIDEO_MAX_WAIT_S", 120.0)


def api_lane_ocr_concurrency() -> int:
    """Chat job 4 + API 2 ≤ the OCR engine's --max-num-seqs 8."""
    return _int("PUBLIC_API_FILES_API_LANE_OCR_CONCURRENCY", 2)


#: Every setting this module reads, with its type and default — for the
#: integration team's `config.py` block and for the operator reference.
SETTINGS = (
    ("PUBLIC_API_FILES_DIR", "str", "/data/api-files"),
    ("PUBLIC_API_FILES_MIN_FREE_GIB", "float", 250),
    ("PUBLIC_API_FILES_PART_MAX_BYTES", "int", 67_108_864),
    ("PUBLIC_API_FILES_SINGLE_MAX_BYTES", "int", 67_108_864),
    ("PUBLIC_API_FILES_MAX_BODY_BYTES", "int", 68_157_440),
    ("PUBLIC_API_FILES_UPLOAD_MAX_BYTES", "int", 107_374_182_400),
    ("PUBLIC_API_FILES_MAX_PARTS", "int", 10_000),
    ("PUBLIC_API_FILES_JSON_MAX_BYTES", "int", 1_048_576),
    ("PUBLIC_API_UPLOAD_IDLE_TTL_HOURS", "float", 24),
    ("PUBLIC_API_UPLOAD_MAX_TTL_HOURS", "float", 168),
    ("PUBLIC_API_UPLOAD_RECORD_TTL_DAYS", "float", 30),
    ("PUBLIC_API_UPLOAD_FINALIZING_STALE_S", "float", 600),
    ("PUBLIC_API_UPLOAD_COMPLETE_BUSY_WAIT_S", "float", 2),
    ("PUBLIC_API_FILES_ASSEMBLY_LEASE_S", "float", 90),
    ("PUBLIC_API_FILES_ASSEMBLY_RENEW_S", "float", 30),
    ("PUBLIC_API_FILES_PURGE_WAIT_S", "float", 8),
    ("PUBLIC_API_FILES_ASSEMBLY_MAX_ATTEMPTS", "int", 5),
    ("PUBLIC_API_FILES_SWEEP_INTERVAL_S", "float", 600),
    ("PUBLIC_API_FILES_LEFTOVER_MAX_AGE_S", "float", 86_400),
    ("PUBLIC_API_FILES_ORPHAN_MIN_AGE_S", "float", 3_600),
    ("PUBLIC_API_FILES_SWEEP_BATCH", "int", 200),
    ("PUBLIC_API_FILES_LIST_MAX_LIMIT", "int", 10_000),
    ("PUBLIC_API_FILES_TOMBSTONE_DAYS", "int", 30),
    ("PUBLIC_API_FILES_CPU_JOBS", "int", 2),
    ("PUBLIC_API_FILES_MEDIA_JOBS", "int", 1),
    ("PUBLIC_API_FILES_PROCESSING_LEASE_S", "float", 90),
    ("PUBLIC_API_FILES_PROCESSING_RETRY_DELAY_S", "float", 300),
    ("PUBLIC_API_FILES_PROCESSING_MAX_ATTEMPTS", "int", 5),
)
