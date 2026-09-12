"""Central configuration for the orchestrator service (spec §6, orchestrator env vars).

Every value comes from an environment variable with a sensible default for the
local docker-compose network. Reading configuration never touches the network,
the GPU, or any model runtime.
"""
from __future__ import annotations

import os

from .core.exports import EXPORT_ROW_CAP, PREVIEW_ROW_CAP
from .model_capabilities import (
    CapabilityDefaults,
    ModelCapabilityRegistry,
    ModelRole,
    ReasoningField,
    RerankerBackend,
    capabilities_from_env,
)

_TRUTHY = {"1", "true", "yes", "on"}


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _controller_state_url(value: str) -> str:
    """`http://host:9838` → `http://host:9838/state`; a URL that already names
    a path is kept as given. Blank stays blank (the poller is off)."""
    url = (value or "").strip()
    if not url:
        return ""
    from urllib.parse import urlsplit

    if urlsplit(url).path.strip("/") == "":
        return url.rstrip("/") + "/state"
    return url


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


#: Valid CHART_TRIGGER_MODE values. `automatic` is intentionally not one.
CHART_TRIGGER_MODES = ("explicit", "hybrid")


class Settings:

    #: The warehouse file READERS should open, resolved on every access.
    #:
    #: The sync worker publishes a snapshot each cycle and the orchestrator
    #: reads that, because DuckDB is many-readers-OR-one-writer and reading the
    #: file being written failed 41.4% of the time — every failure falling back
    #: to live Salesforce and its 200-row cap. Resolving PER ACCESS matters: the
    #: first snapshot usually appears minutes after boot, and a value fixed at
    #: start-up would pin this process to the contended file until restart.
    @property
    def duckdb_path(self) -> str:
        if self._prefer_snapshot and os.path.exists(self.duckdb_snapshot_path):
            return self.duckdb_snapshot_path
        return self._duckdb_live_path

    @duckdb_path.setter
    def duckdb_path(self, value: str) -> None:
        # An explicit assignment (tests, or an operator pinning a file) wins
        # outright — otherwise a stale snapshot would silently shadow it.
        self._duckdb_live_path = value
        self._prefer_snapshot = False

    @duckdb_path.deleter
    def duckdb_path(self) -> None:
        # `monkeypatch.setattr(settings, "duckdb_path", ...)` undoes itself with
        # delattr, because the name lives on the CLASS rather than the instance.
        # Without a deleter every test that pins a warehouse file fails in
        # teardown; restoring the environment's own answer is the honest undo.
        self._duckdb_live_path = os.environ.get(
            "DUCKDB_PATH", "/data/warehouse.duckdb"
        )
        self._prefer_snapshot = os.environ.get(
            "DUCKDB_USE_SNAPSHOT", "true"
        ).lower() not in ("0", "false", "no", "off")
    """Orchestrator settings, resolved from the environment at construction."""

    def __init__(self) -> None:
        # --- gpt-oss-120b via vLLM (OpenAI-compatible endpoint) ---
        self.openai_base_url: str = os.environ.get("OPENAI_BASE_URL", "http://vllm:30000/v1")
        # Local inference server: the key is a placeholder, not a real secret.
        self.openai_api_key: str = os.environ.get("OPENAI_API_KEY", "local")
        # docker-compose sets MAIN_MODEL; LLM_MODEL is a secondary fallback for
        # local overrides. The main model is multimodal AND reasoning-capable,
        # so it serves chat, SQL, RAG, reports, vision, PDF and agent
        # planning/synthesis — everything except the cheap classification calls.
        self.llm_model: str = (
            os.environ.get("MAIN_MODEL")
            or os.environ.get("LLM_MODEL")
            or "Qwen/Qwen3.6-35B-A3B-NVFP4"
        )

        # --- Sidecar vLLM services (router / vision / embeddings), all
        # OpenAI-compatible — vLLM-design env vars.
        self.router_base_url: str = os.environ.get(
            "ROUTER_BASE_URL", "http://vllm-router:30002/v1"
        ).rstrip("/")
        self.router_model: str = os.environ.get(
            "ROUTER_MODEL", "Qwen/Qwen3-VL-8B-Instruct-FP8"
        )
        # The agent runs its sub-steps on the small multimodal model so one
        # heavy task cannot monopolise the main model's KV pool; planning and
        # final synthesis still run on the main model.
        self.agent_base_url: str = os.environ.get(
            "AGENT_BASE_URL", "http://vllm-router:30002/v1"
        ).rstrip("/")
        self.agent_model: str = os.environ.get(
            "AGENT_MODEL", "Qwen/Qwen3-VL-8B-Instruct-FP8"
        )
        self.vision_base_url: str = os.environ.get(
            "VISION_BASE_URL", "http://vllm:30000/v1"
        ).rstrip("/")
        self.vision_model: str = os.environ.get("VISION_MODEL", "Qwen/Qwen3.6-35B-A3B-NVFP4")
        self.embed_base_url: str = os.environ.get(
            "EMBED_BASE_URL", "http://vllm-embed:30003/v1"
        ).rstrip("/")
        self.embed_model: str = os.environ.get("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
        # Unlimited-OCR (2026-08-06): baidu's 3.3B document-OCR VLM on its own
        # vLLM service. Uploaded images and rendered PDF pages are transcribed
        # here FIRST; the transcript rides with the pixels to the main model.
        # Disabled or unreachable → the pipeline runs pixels-only as before.
        self.ocr_enabled: bool = _bool("OCR_ENABLED", True)
        self.ocr_base_url: str = os.environ.get(
            "OCR_BASE_URL", "http://vllm-ocr:30004/v1"
        ).rstrip("/")
        self.ocr_model: str = os.environ.get("OCR_MODEL", "baidu/Unlimited-OCR")

        # -- Speech to text (2026-09-04; whisper-large-v3 from 2026-09-08) --
        #
        # The composer's microphone. OFF by default: the engine is a separate
        # service on a separate node, started by scripts/whisper.sh, and a
        # deployment that has not run it must not offer members a button that
        # cannot work. The script writes ASR_ENABLED and ASR_BASE_URLS into
        # .env when it succeeds, which is what turns the feature on — and
        # `whisper.sh down` takes the stopped engine back out of that list,
        # so the two directions stay symmetric.
        self.asr_enabled: bool = _bool("ASR_ENABLED", False)
        self.asr_backend: str = os.environ.get("ASR_BACKEND", "whisper")
        # The speech server on the worker (compose/compose.whisper.yaml),
        # bound to its MANAGEMENT address because the orchestrator reaches it
        # from the other node. Never a 10.100.x RoCE address: that fabric
        # belongs to the main model's tensor-parallel shards.
        self.asr_base_url: str = os.environ.get(
            "ASR_BASE_URL", "http://192.168.9.68:30007/v1"
        ).rstrip("/")
        # More than one engine, comma-separated, when speech runs on both
        # nodes. Requests go to whichever endpoint has the fewest in flight;
        # see app/asr.RoutedProvider. Falls back to the single URL above, so a
        # deployment that never sets this behaves exactly as it did.
        #
        # These are REPLICAS, not shards, and the distinction is the whole
        # answer to "use both GPUs". whisper-large-v3 is 1.55B parameters,
        # 3.1 GB in float16 — it fits on either node several times over.
        # Splitting one across two Sparks would put every layer's activations
        # on the RoCE link the main model's tensor-parallel traffic already
        # uses, to save memory that was never short: slower, and contending
        # with the thing that must not be slowed.
        #
        # SO BE PRECISE ABOUT WHAT A SECOND ENGINE BUYS: a second concurrent
        # speaker, not a faster transcript. One clip is decoded by one engine.
        # Two nodes double how many people can dictate at once; they do not
        # halve the time any one of them waits. Measured 2026-09-08 on 15 s
        # clips — 1 concurrent: 2.12 s → 2.03 s (1.0x, i.e. nothing); 2: 4.00 s
        # → 2.05 s; 4: 7.99 s → 4.16 s; 8: 15.99 s → 8.31 s; 16: 32.02 s →
        # 16.79 s. A clean 1.9x from two clips upward and 1.0x at one, which is
        # exactly what replicas of a serial engine predict.
        self.asr_base_urls: tuple[str, ...] = tuple(
            url.strip().rstrip("/")
            for url in os.environ.get("ASR_BASE_URLS", "").split(",")
            if url.strip()
        ) or (self.asr_base_url,)
        self.asr_model: str = os.environ.get("ASR_MODEL", "openai/whisper-large-v3")
        # Auto-detection is the default and should stay it: a person dictating
        # must not have to declare a language before they speak, and Whisper
        # identifies ninety-nine by itself. It is also the only setting that
        # serves the people on this deployment, who code-switch mid-sentence
        # — forcing one language mistranscribes the other. The engine is not
        # sent a language at all; see app/asr.transcribe.
        self.asr_language: str = os.environ.get("ASR_LANGUAGE", "auto")
        # A stuck-engine guard, not a budget — but it has to clear the worst
        # legitimate case. Measured on this hardware: 11 s of audio in 1.24 s,
        # about 9x realtime. Ten minutes of audio is therefore ~70 s of
        # decoding, and Whisper's sequential long-form pass is slower per
        # second than a short clip, so 60 s was too tight for a long
        # dictation and would have failed it as an engine fault.
        self.asr_timeout_s: float = _float("ASR_TIMEOUT_S", 240.0)
        # Composer dictation, not podcast transcription. Ten minutes is the
        # ceiling; the browser stops recording at it rather than uploading
        # something that will be refused.
        self.asr_max_audio_seconds: int = _int("ASR_MAX_AUDIO_SECONDS", 600)
        # ~24 MB at the browser's Opus bitrate for ten minutes, with room for
        # a codec that compresses less well. Enforced while reading, before
        # anything is held whole in memory.
        self.asr_max_upload_bytes: int = _int(
            "ASR_MAX_UPLOAD_BYTES", 32 * 1024 * 1024
        )
        # PER ENGINE, not in total, and 2 rather than 4 because the engine
        # SERIALISES. compose/whisper/server.py holds a `_gpu_lock` around
        # every transcription, so one engine decodes exactly one clip at a
        # time — measured 2026-09-08, throughput is flat at ~7 s of audio per
        # wall-second from 1 concurrent clip to 16, while latency grows
        # linearly (2.1 s → 17.0 s at 16). Whisper on transformers does not
        # batch the way Qwen3-ASR on vLLM did, and a deep queue therefore buys
        # nothing but waiting.
        #
        # 2 = one decoding, one ready to start, so the GPU never idles between
        # clips and nobody waits behind more than one other person. Anything
        # past that is refused fast (asr_queue_wait_s) instead of silently
        # extending the window during which the CHAT model is degraded — and
        # that window is expensive: a saturated speech engine takes the main
        # model from 71 tok/s to ~24 on EITHER node, because the chat model is
        # tensor-parallel across both and runs at the speed of its slower rank.
        self.asr_max_concurrent: int = _int("ASR_MAX_CONCURRENT", 2)
        # Past this, callers are told to try again instead of queueing behind
        # work they cannot see.
        self.asr_queue_wait_s: float = _float("ASR_QUEUE_WAIT_S", 8.0)
        # A ceiling on the transcript, not on the audio: a runaway decode on
        # a noisy clip must not stream forever.
        self.asr_max_tokens: int = _int("ASR_MAX_TOKENS", 1024)
        # Per person, per minute. Dictation is bursty but not machine-fast.
        self.asr_rate_per_min: int = _int("ASR_RATE_PER_MIN", 20)

        # -- Video understanding (2026-09-09) ----------------------------------
        #
        # A person attaches a video; the assistant transcribes it (Whisper,
        # timestamped), reads what was on screen (OCR + the router's vision
        # model), fuses both with the main model, indexes the evidence and
        # hands back downloadable transcripts. OFF by default: it needs
        # ffmpeg in the image and the speech engine running, and a
        # deployment without either must not show a video picker.
        self.video_analysis_enabled: bool = _bool("VIDEO_ANALYSIS_ENABLED", False)
        # Where analyses live — NOT under WORKSPACE_DIR, whose top-level
        # directories are swept by age (core/repo.enforce_quota_and_ttl); a
        # job that takes an hour must not lose its inputs to somebody else's
        # upload. Keyed by the sha256 of the file so a re-upload reuses it.
        self.video_data_dir: str = os.environ.get("VIDEO_DATA_DIR", "/data/video")
        # Its own LanceDB directory: the sidecar pins one table per directory
        # and the two existing ones are the Salesforce and web corpora.
        self.lancedb_video_dir: str = os.environ.get("LANCEDB_VIDEO_DIR", "/data/lancedb-video")
        # Ceilings. Four hours is a full workshop; past it the transcript
        # alone is an hour of speech-engine time on a GPU the chat model
        # shares.
        self.video_max_duration_s: int = _int("VIDEO_MAX_DURATION_S", 4 * 3600)
        self.video_max_upload_mb: int = _int("VIDEO_MAX_UPLOAD_MB", 4096)
        # ONE JOB AT A TIME. A saturated speech engine on either node takes
        # the chat model from 71 to 24 tok/s (measured 2026-09-08); two
        # videos at once would double that. Raise only on a deployment where
        # nobody chats while videos are processed.
        self.video_max_concurrent_jobs: int = _int("VIDEO_MAX_CONCURRENT_JOBS", 1)
        # THE LEASE (V29). A run holds `video_analyses.lease_owner` for this
        # long and renews it every third of it; startup reconciliation
        # requeues only rows whose lease has lapsed, so a rolling recreate
        # next to a healthy worker cannot start the same video twice.
        self.video_lease_ttl_s: float = _float("VIDEO_LEASE_TTL_S", 90.0)
        # PACING. Before every GPU-heavy unit (an ASR window, an OCR batch, a
        # caption) the job waits — up to this long — while a chat generation
        # is in flight, so the people chatting never pay for a video being
        # processed in the background. 0 disables pacing.
        self.video_pace_max_wait_s: float = _float("VIDEO_PACE_MAX_WAIT_S", 20.0)
        # A stage that runs longer than this is failed rather than left to
        # hold the only job slot forever. Two hours covers a four-hour video's
        # transcript at ~10x realtime with a margin.
        self.video_stage_timeout_s: float = _float("VIDEO_STAGE_TIMEOUT_S", 2 * 3600.0)
        # A stage that could not reach the model even after the recovery
        # window (app/resilience.py) DEFERS the job — back to 'queued' with
        # its finished stages kept — rather than failing it, up to this many
        # attempts in total; the maintenance drain leaves a deferred row
        # alone for VIDEO_RETRY_DELAY_S before trying again.
        self.video_max_attempts: int = _int("VIDEO_MAX_ATTEMPTS", 5)
        self.video_retry_delay_s: float = _float("VIDEO_RETRY_DELAY_S", 300.0)
        # Transcription windows: at most this long (the engine refuses 600 s),
        # never spanning a pause over `max_gap`, overlapping by `overlap`
        # only where continuous speech had to be cut. Batch clips go through
        # their own admission pool of this size so dictation is never starved.
        # The ceiling on one clip sent to the speech engine. It is the length
        # of the longest GPU unit the job cannot yield during: measured at
        # 240 s, a window took ~41 s in the engine and a person chatting
        # meanwhile saw 31-37 tok/s for that long. 90 s keeps the dip near
        # 15 s at the price of a few more calls (the engine's own receptive
        # field is 30 s, so seams are no worse than its internal ones).
        self.video_asr_window_s: float = _float("VIDEO_ASR_WINDOW_S", 90.0)
        self.video_asr_max_gap_s: float = _float("VIDEO_ASR_MAX_GAP_S", 2.0)
        self.video_asr_overlap_s: float = _float("VIDEO_ASR_OVERLAP_S", 3.0)
        # Clips in flight at once. Two = one per Spark: the speech router
        # hands each clip to the engine with the fewest in flight, so both
        # nodes decode side by side. A person dictating meanwhile may wait
        # for one clip (~15 s at the 90-s window) — the price of using both.
        self.video_asr_concurrency: int = _int("VIDEO_ASR_CONCURRENCY", 2)
        # Frames: a scene-change detector plus a periodic floor, then a
        # perceptual-hash dedupe, then a cap of one frame per
        # `per_seconds` clamped to [min, max]. The floor is derived from the
        # cap (see pipeline._stage_frames) within these bounds.
        self.video_frame_scene_threshold: float = _float("VIDEO_FRAME_SCENE_THRESHOLD", 0.30)
        self.video_frame_floor_min_s: float = _float("VIDEO_FRAME_FLOOR_MIN_S", 4.0)
        self.video_frame_floor_max_s: float = _float("VIDEO_FRAME_FLOOR_MAX_S", 30.0)
        self.video_frame_max_width: int = _int("VIDEO_FRAME_MAX_WIDTH", 1280)
        self.video_frame_per_seconds: float = _float("VIDEO_FRAME_PER_SECONDS", 8.0)
        self.video_frame_min: int = _int("VIDEO_FRAME_MIN", 24)
        self.video_frame_max: int = _int("VIDEO_FRAME_MAX", 400)
        self.video_frame_hash_distance: int = _int("VIDEO_FRAME_HASH_DISTANCE", 5)
        # Above this length only keyframes are decoded for frame selection
        # (tens of times faster; a slide change is noticed at the next
        # keyframe rather than the exact frame).
        # Decoder threads for the ffmpeg children (audio, frames, a single
        # frame). h264 frame-threading scales to about 4-6x; the Sparks have
        # 20 cores and nothing else CPU-bound runs during an analysis.
        self.video_ffmpeg_threads: int = max(1, min(_int("VIDEO_FFMPEG_THREADS", 8), os.cpu_count() or 8))
        self.video_keyframes_only_after_s: float = _float("VIDEO_KEYFRAMES_ONLY_AFTER_S", 1200.0)
        # On-screen text via the OCR sidecar, in small batches with a
        # deadline each — a text-dense frame has cost 47 s here.
        self.video_ocr_enabled: bool = _bool("VIDEO_OCR_ENABLED", True)
        self.video_ocr_max_tokens: int = _int("VIDEO_OCR_MAX_TOKENS", 1500)
        self.video_ocr_batch_deadline_s: float = _float("VIDEO_OCR_BATCH_DEADLINE_S", 150.0)
        self.video_ocr_concurrency: int = _int("VIDEO_OCR_CONCURRENCY", 4)
        # OCR costs ~10 s per text-dense frame on this deployment (measured
        # 2026-09-09) — 400 frames of a two-hour recording would be an hour.
        # Past this many kept frames, only the longest-held ones are read
        # (the slides that mattered); every frame still gets a caption.
        self.video_ocr_max_frames: int = _int("VIDEO_OCR_MAX_FRAMES", 120)
        # Frame captions via the ROUTER vision model (Qwen3-VL-8B), not the
        # chat model: measured 2026-09-09 at ~2 s a frame at 896 px, 0.6 s
        # effective at four in flight. Two in flight leaves the router room
        # for the classification calls every chat message makes.
        self.video_captions_enabled: bool = _bool("VIDEO_CAPTIONS_ENABLED", True)
        self.video_caption_concurrency: int = _int("VIDEO_CAPTION_CONCURRENCY", 3)
        self.video_caption_width: int = _int("VIDEO_CAPTION_WIDTH", 896)
        self.video_caption_max_tokens: int = _int("VIDEO_CAPTION_MAX_TOKENS", 160)
        self.video_caption_timeout_s: float = _float("VIDEO_CAPTION_TIMEOUT_S", 90.0)
        # Fusion on the main model: one pass while the evidence pack is under
        # `direct_tokens`, map-reduce in parts of `part_tokens` above it. The
        # window is a million tokens but a prompt that size monopolises the
        # engine (1.49x concurrency at full window), so this is a ceiling on
        # what one video may ask of it at once.
        self.video_fusion_direct_tokens: int = _int("VIDEO_FUSION_DIRECT_TOKENS", 60_000)
        self.video_fusion_part_tokens: int = _int("VIDEO_FUSION_PART_TOKENS", 32_000)
        self.video_fusion_max_tokens: int = _int("VIDEO_FUSION_MAX_TOKENS", 6_000)
        # Question time: evidence chunks retrieved per question (after the
        # reranker), the pinned summary block's size on later turns, and how
        # close the nearest chunk must be for a text-only turn to be treated
        # as being ABOUT the video (L2 distance; see engines/video.py).
        self.video_retrieve_top_k: int = _int("VIDEO_RETRIEVE_TOP_K", 8)
        self.video_context_chars: int = _int("VIDEO_CONTEXT_CHARS", 6_000)
        # Measured 2026-09-09 on a Hindi tutorial asked about in English: the
        # relevant question scored 1.29 (cross-lingual embeddings sit further
        # apart), unrelated ones 1.66. Same-language relevant questions score
        # 0.5-1.1. 1.45 admits the cross-lingual case and refuses the junk.
        self.video_followup_distance: float = _float("VIDEO_FOLLOWUP_DISTANCE", 1.45)
        # The main model may LOOK at up to this many frames when a question
        # is visual ("read the code on the slide"). ~525 tokens per 896 px
        # frame on this model.
        self.video_answer_frames: int = _int("VIDEO_ANSWER_FRAMES", 3)
        # WHICH FILES ARE OFFERED FOR DOWNLOAD. The pipeline keeps writing
        # all seven formats into the analysis directory — they are content-
        # addressed, cost nothing to keep, and widening this setting must not
        # require re-analysing the video — but only these kinds are copied
        # per user and advertised on the answer. The default is one file
        # because seven was not a result, it was a menu: four of the seven
        # are the same words (txt, srt, vtt, json) and every card is named
        # after the source video, so an answer ended in a grid of near-
        # identical rows the reader had to decode by file extension.
        # Kind names are the pipeline's (`transcript_txt`, `transcript_srt`,
        # `transcript_vtt`, `transcript_json`, `screen_text_txt`,
        # `screen_text_json`, `summary_md`); `all` restores every one, an
        # unknown name is ignored, and an empty value means the default.
        self.video_artifact_kinds: tuple[str, ...] = tuple(
            kind.strip()
            for kind in (os.environ.get("VIDEO_ARTIFACT_KINDS", "").strip() or "transcript_vtt").split(",")
            if kind.strip()
        )
        # Housekeeping: analyses no conversation refers to any more are
        # deleted after this grace period (a re-upload inside it is free).
        self.video_orphan_ttl_hours: int = _int("VIDEO_ORPHAN_TTL_HOURS", 72)
        self.video_maintenance_interval_s: float = _float("VIDEO_MAINTENANCE_INTERVAL_S", 1800.0)

        # --- Artifact Studio (documents, decks, workbooks made in chat) ----
        # Off switches the intent gate off: a request for a file is answered
        # in text, as before. Per-member access is the ARTIFACTS feature.
        self.artifacts_enabled: bool = _bool("ARTIFACTS_ENABLED", True)
        # Jobs run one at a time by default: rendering is CPU-bound and the
        # compose stage is a model call that shares the engine with chat.
        self.artifact_max_concurrent_jobs: int = _int("ARTIFACT_MAX_CONCURRENT_JOBS", 1)
        self.artifact_lease_ttl_s: float = _float("ARTIFACT_LEASE_TTL_S", 90.0)
        # A stage that runs longer than this is failed, not waited on.
        # MEASURED 2026-09-11 (Qwen3.6-35B, TP=2): a Think-effort workbook
        # is outline + spec + review with thinking on, 252 s for three
        # rows; two corrections on top approach 450 s, Max adds sources and
        # a visual pass. 900 s is the wall above that, not a target.
        self.artifact_stage_timeout_s: float = _float("ARTIFACT_STAGE_TIMEOUT_S", 900.0)
        # The renderer subprocess: wall clock and address space. A document
        # that needs more than this is a page-count or image bomb.
        self.artifact_render_timeout_s: float = _float("ARTIFACT_RENDER_TIMEOUT_S", 180.0)
        self.artifact_render_memory_mb: int = _int("ARTIFACT_RENDER_MEMORY_MB", 2048)
        # Space a job needs on the reports volume before it starts writing.
        self.artifact_min_free_mb: int = _int("ARTIFACT_MIN_FREE_MB", 512)
        # Per-person ceiling on published artifact bytes; a job past it is
        # refused with quota_exceeded before any model call.
        self.artifact_user_quota_mb: int = _int("ARTIFACT_USER_QUOTA_MB", 2048)
        # Queued + running jobs one person may hold at once. The render slot
        # is single and the engine is shared; fifty queued Max jobs would
        # be everyone else's afternoon.
        self.artifact_max_open_jobs_per_user: int = _int("ARTIFACT_MAX_OPEN_JOBS_PER_USER", 3)
        # Housekeeping: abandoned working directories (v<N>.tmp) older than
        # this are removed; published versions are never removed by the sweep.
        self.artifact_tmp_ttl_hours: int = _int("ARTIFACT_TMP_TTL_HOURS", 24)
        self.artifact_maintenance_interval_s: float = _float("ARTIFACT_MAINTENANCE_INTERVAL_S", 1800.0)
        # Max effort visual QA: pages sent to the vision model, downscaled.
        self.artifact_qa_pages: int = _int("ARTIFACT_QA_PAGES", 4)
        self.artifact_qa_width: int = _int("ARTIFACT_QA_WIDTH", 896)

        # --- Reranker ------------------------------------------------------
        # Backward compatibility: RERANK_ENABLED=false still disables the
        # feature when no backend is named. New profiles select an explicit
        # backend so CPU/Mac deployments never load the in-process torch path.
        _legacy_rerank_enabled = _bool("RERANK_ENABLED", True)
        _rerank_backend_raw = os.environ.get("RERANK_BACKEND", "").strip().lower()
        if not _rerank_backend_raw:
            _rerank_backend_raw = "inprocess" if _legacy_rerank_enabled else "disabled"
        self.rerank_backend: RerankerBackend = RerankerBackend.parse(_rerank_backend_raw)
        if not _legacy_rerank_enabled:
            self.rerank_backend = RerankerBackend.DISABLED
        self.rerank_enabled: bool = self.rerank_backend is not RerankerBackend.DISABLED
        # §6: docker-compose sets RERANKER_MODEL=Qwen/Qwen3-Reranker-0.6B.
        # RERANK_MODEL is kept as a secondary fallback for local overrides.
        # The remote scorer addresses the model by the name the server
        # advertises, while the in-process loader needs the on-disk path.
        # RERANKER_MODEL carries the path and RERANK_MODEL the API name, so
        # the preference order flips with the backend; sending a path to a
        # remote /score endpoint gets an HTTP 404 for an unknown model.
        _rerank_model_order = (
            ("RERANK_MODEL", "RERANKER_MODEL")
            if self.rerank_backend is RerankerBackend.REMOTE
            else ("RERANKER_MODEL", "RERANK_MODEL")
        )
        self.rerank_model: str = (
            os.environ.get(_rerank_model_order[0])
            or os.environ.get(_rerank_model_order[1])
            or "Qwen/Qwen3-Reranker-0.6B"
        )
        self.rerank_base_url: str = os.environ.get("RERANK_BASE_URL", "").rstrip("/")
        self.rerank_api_key: str = os.environ.get("RERANK_API_KEY", "")
        self.rerank_timeout: float = _float("RERANK_TIMEOUT", 60.0)
        # Backpressure for the shared cross-encoder client (app/rerank.py,
        # ADR-0001 D13). The reranker container serves `concurrency=4`; more
        # in-flight scoring calls than that only queue there. A caller waits
        # at most RERANK_WAIT_S for a slot, then keeps its own order.
        # Load-tested 2026-09-03 (25 concurrent Fast questions): with 4 slots
        # and a 0.25 s wait, 16 of 25 answers went unjudged ("degraded_busy")
        # while the main model's own prefill queue put the first token 12 s
        # out anyway. The reranker is a 0.6B model that batches sequences;
        # 8 in flight and a one-second wait keep every answer judged at that
        # load for a cost nobody can see.
        self.rerank_max_inflight: int = _int("RERANK_MAX_INFLIGHT", 8)
        self.rerank_wait_s: float = _float("RERANK_WAIT_S", 1.5)
        # The knowledge pipeline's own waits (a Fast answer must not queue
        # behind a Think search's 48-candidate rerank) and its per-call
        # deadline. Half of the in-flight slots are reserved for it.
        self.rerank_wait_fast_s: float = _float("RERANK_WAIT_FAST_S", 1.0)
        self.rerank_wait_think_s: float = _float("RERANK_WAIT_THINK_S", 2.0)
        self.rerank_stage_timeout_s: float = _float("RERANK_STAGE_TIMEOUT_S", 2.0)
        self.rerank_reserved_slots: int = _int("RERANK_RESERVED_SLOTS", 4)
        # A healthy-looking reranker can still return garbage (a wrong
        # served model, a broken template). A fixed canary triple is scored
        # at first use and every worker cycle; a failure trips a breaker so
        # every caller degrades to its hybrid order instead of amplifying
        # nonsense into confident grounding.
        self.rerank_canary_enabled: bool = _bool("RERANK_CANARY_ENABLED", True)
        self.rerank_breaker_s: float = _float("RERANK_BREAKER_S", 300.0)

        # --- Data stores (defaults match §6/§7 and what the sync-worker writes) ---
        # Readers prefer the PUBLISHED snapshot the sync worker renames into
        # place each cycle. DuckDB is many-readers-OR-one-writer, so reading the
        # file the worker is writing meant 41.4% of read opens failed with
        # "Could not set lock" — and every failure fell back to live Salesforce,
        # whose 200-row cap became the answer to "how many?". The snapshot has
        # no writer, so nothing can lock a reader out of it. The live file stays
        # the fallback for a first boot, before any cycle has published
        # (2026-08-29).
        self._duckdb_live_path: str = os.environ.get(
            "DUCKDB_PATH", "/data/warehouse.duckdb"
        )
        self.duckdb_snapshot_path: str = os.environ.get("DUCKDB_SNAPSHOT_PATH") or (
            self._duckdb_live_path[:-7] + ".read.duckdb"
            if self._duckdb_live_path.endswith(".duckdb")
            else self._duckdb_live_path + ".read.duckdb"
        )
        self._prefer_snapshot: bool = os.environ.get(
            "DUCKDB_USE_SNAPSHOT", "true"
        ).lower() not in ("0", "false", "no", "off")
        self.lancedb_dir: str = os.environ.get("LANCEDB_DIR", "/data/lancedb")
        self.lancedb_table: str = os.environ.get("LANCEDB_TABLE", "chunks")
        self.parquet_dir: str = os.environ.get("PARQUET_DIR", "/data/parquet")
        self.reports_dir: str = os.environ.get("REPORTS_DIR", "/reports")

        # --- The Salesforce brain: knowledge packs the SF team drops in as
        # files (repo brain/packs, mounted read-only in the container). Rules,
        # metrics, glossary and prose knowledge flow into every Salesforce-mode
        # prompt via org_brief; see core/brain.py.
        self.brain_enabled: bool = _bool("BRAIN_ENABLED", True)
        self.brain_dir: str = os.environ.get("BRAIN_DIR", "/data/brain")
        #: Ceiling for the retrieved-knowledge block in one prompt. Prefill on
        #: the 35B model is ~linear in prompt size; knowledge must stay a
        #: garnish, not the meal.
        self.brain_max_chars: int = _int("BRAIN_MAX_CHARS", 4000)

        # --- Learn-from-chat: thumbs-up answers become few-shot SQL examples
        # for similar future questions (core/learned_examples.py).
        self.learned_examples_enabled: bool = _bool("LEARNED_EXAMPLES_ENABLED", True)
        #: How many confirmed examples one prompt may carry.
        self.learned_examples_k: int = _int("LEARNED_EXAMPLES_K", 2)

        # --- Salesforce Lightning links for citations ---
        self.sf_lightning_base_url: str = os.environ.get(
            "SF_LIGHTNING_BASE_URL", "https://techsara.lightning.force.com"
        ).rstrip("/")

        # --- Context windows (§8): report planning may use the large window,
        # everything else is capped at the default.
        self.default_max_context: int = _int("DEFAULT_MAX_CONTEXT", 32768)
        self.report_max_context: int = _int("REPORT_MAX_CONTEXT", 65536)

        # --- Phase 6 token budget (single source of truth for the REAL model
        # window). Must match the vLLM `--max-model-len` the main model serves
        # — it is not a knob that widens the window, only the app's honest view
        # of it. Raising the actual window is a separate serving change.
        # Live Salesforce lookups (read-only) for data newer than the last
        # sync, or on objects the warehouse does not carry.
        self.sf_client_id: str = os.environ.get("SF_CLIENT_ID", "")
        self.sf_client_secret: str = os.environ.get("SF_CLIENT_SECRET", "")
        self.sf_login_url: str = os.environ.get("SF_LOGIN_URL", "")
        self.sf_private_key_b64: str = os.environ.get("SF_PRIVATE_KEY_B64", "")
        self.sf_api_version: str = os.environ.get("SF_API_VERSION", "v61.0")
        # Explicit deployment label for provenance/evaluation. Never infer
        # this from a hostname: aliases and My Domain URLs do not reliably
        # distinguish production, preprod and sandboxes.
        self.sf_environment: str = (
            os.environ.get("SF_ENVIRONMENT", "unknown").strip().lower() or "unknown"
        )
        self.sf_live_timeout: float = float(os.environ.get("SF_LIVE_TIMEOUT", "45"))
        # _bool, not a bespoke not-in list: the old parse treated "off" (and
        # any typo) as true, silently enabling live lookups.
        self.sf_live_enabled: bool = _bool("SF_LIVE_ENABLED", True)
        # MAIN_MODEL_MAX_LEN is the new, explicit spelling and wins when set;
        # MODEL_MAX_CONTEXT remains the historical name so existing .env files
        # keep working. Both describe the SAME number and neither WIDENS the
        # window — this is the app's honest view of what vLLM serves
        # (`--max-model-len`), and lying to it produces 400s, not more context.
        self.model_max_context: int = _int(
            "MAIN_MODEL_MAX_LEN", _int("MODEL_MAX_CONTEXT", 262144)
        )
        self.model_max_output: int = _int(
            "MAIN_MODEL_DEFAULT_MAX_OUTPUT_TOKENS", _int("MODEL_MAX_OUTPUT", 8192)
        )
        # High effort is allowed a bigger answer: a multi-step comparison with
        # its assumptions stated does not fit in the default reservation, and
        # truncating the conclusion is the worst place to run out.
        self.model_high_max_output: int = _int("MAIN_MODEL_HIGH_MAX_OUTPUT_TOKENS", 16384)

        # --- Thinking budget MODE (owner decision 2026-08-19: local
        # deployment, no per-token cost — thinking runs until the model
        # closes it naturally). "off" (default): no client-side cutoff, no
        # forced closure, no thinking-off regeneration; only physical
        # ceilings apply. "client": re-enables the Phase 1 enforcement
        # exactly as built, using the THINKING_BUDGET_* values below.
        _budget_mode = os.environ.get("THINKING_BUDGET_MODE", "off").strip().lower()
        self.thinking_budget_mode: str = _budget_mode if _budget_mode == "client" else "off"
        #: The physical ceiling when thinking is UNBOUNDED: requests with
        #: thinking on are sized to at least this many completion tokens so
        #: thinking + answer always fit (262k window minus prompt applies on
        #: top via fit_request).
        self.max_output_tokens: int = _int("MAX_OUTPUT_TOKENS", 65536)
        #: Hang guard, NOT a budget: a generation stream that exceeds this
        #: wall clock is killed loudly and returns what it produced so far.
        #: It exists to catch degenerate repetition loops only — at the
        #: measured 46.6 tok/s, 1800s ≈ 84k tokens, far past any real answer.
        self.gen_wall_clock_s: float = _float("GEN_WALL_CLOCK_S", 1800.0)

        # --- Thinking token budgets by effort (Phase 1, 2026-08-19) ---
        # ACTIVE ONLY when THINKING_BUDGET_MODE=client (see above). ---
        # Derived from the MEASURED decode rate on this box — 46.6 tok/s with
        # thinking on (see docs/CONFIG.md) — as target_minutes × 60 × tok/s:
        # medium ≈ 1.4 min, high ≈ 4.3 min, extra_high ≈ 8.6 min of thinking.
        # The budget GROWS max_tokens so reasoning can never starve the answer
        # (the documented 121s/zero-output failure), and is enforced
        # CLIENT-SIDE in llm.stream_chat_events.
        self.thinking_budget_medium: int = _int("THINKING_BUDGET_MEDIUM", 4000)
        self.thinking_budget_high: int = _int("THINKING_BUDGET_HIGH", 12000)
        self.thinking_budget_extra_high: int = _int("THINKING_BUDGET_EXTRA_HIGH", 24000)
        #: Overrun slack before the client forces closure: the cap is
        #: budget × grace, so a thought mid-sentence at the nominal budget is
        #: not guillotined for one more clause.
        self.thinking_budget_grace: float = _float("THINKING_BUDGET_GRACE", 1.25)
        #: Server-side thinking budget. Tested empirically 2026-08-19 against
        #: this vLLM build (0.20.1 NGC 26.05) + Qwen3.6: chat_template_kwargs
        #: thinking_token_budget / thinking_budget / max_thinking_tokens were
        #: ALL silently ignored (600/600 reasoning tokens at a budget of 64).
        #: Leave False unless a future build demonstrably honors it; client-
        #: side enforcement runs regardless, and is the ONLY mechanism allowed
        #: when tools are attached (a server-side cut inside a <think> block
        #: can corrupt tool-call arguments).
        self.server_thinking_budget: bool = _bool("SERVER_THINKING_BUDGET", False)
        #: Best-of-N at extra_high: candidates generated CONCURRENTLY, a
        #: thinking-off guided-JSON judge picks the winner (core/best_of.py).
        #: 1 keeps the extra_high thinking budget but skips the sampling.
        self.extra_high_samples: int = _int("EXTRA_HIGH_SAMPLES", 3)
        # Serving parameters, mirrored here so /health can report what the app
        # BELIEVES it is running against and a drift from the actual vLLM flags
        # is visible instead of mysterious.
        self.main_model_max_num_seqs: int = _int("MAIN_MODEL_MAX_NUM_SEQS", 0)
        self.main_model_max_batched_tokens: int = _int("MAIN_MODEL_MAX_BATCHED_TOKENS", 8192)
        self.main_model_kv_cache_dtype: str = os.environ.get(
            "MAIN_MODEL_KV_CACHE_DTYPE", "fp8"
        ).strip()
        self.main_model_enable_prefix_caching: bool = _bool(
            "MAIN_MODEL_ENABLE_PREFIX_CACHING", True
        )
        self.main_model_enable_chunked_prefill: bool = _bool(
            "MAIN_MODEL_ENABLE_CHUNKED_PREFILL", True
        )
        # Whether the served model was launched with --enable-auto-tool-choice
        # and a tool-call parser. The planner tries tool calling first and
        # downgrades to guided JSON on a 400, so this is a hint that saves a
        # failed round trip — never a correctness gate.
        self.main_model_enable_auto_tool_choice: bool = _bool(
            "MAIN_MODEL_ENABLE_AUTO_TOOL_CHOICE", True
        )
        # Headroom left free in every request (chat-template drift, the
        # tokenizer's own overhead, sampling slack).
        self.context_safety_margin: int = _int("CONTEXT_SAFETY_MARGIN", 512)
        # Budget for the /tokenize probe. It is a local call; a slow one must
        # never hold up a chat, so this stays short and falls back to an
        # estimate on timeout.
        self.tokenize_timeout: float = _float("TOKENIZE_TIMEOUT", 5.0)
        # Router/classification calls only need the opening of a message to
        # pick a route, so long inputs are clipped to this many characters.
        self.router_input_char_cap: int = _int("ROUTER_INPUT_CHAR_CAP", 6000)
        # The embedding model has the smallest window of all; queries longer
        # than this are clipped before being embedded.
        self.embed_input_char_cap: int = _int("EMBED_INPUT_CHAR_CAP", 8000)

        # --- Phase A: rolling summary + auto-compaction.
        # Fractions of the USABLE budget (window − this request's output
        # reservation − safety margin), not of the raw window.
        self.context_warn_threshold: float = _float("CONTEXT_WARN_THRESHOLD", 0.60)
        # Background: compact right AFTER a turn, so the user never waits.
        self.context_bg_compact_threshold: float = _float(
            "CONTEXT_BG_COMPACT_THRESHOLD", 0.70
        )
        # Synchronous: the hard guarantee, BEFORE the request goes out.
        self.context_compact_threshold: float = _float(
            "CONTEXT_COMPACT_THRESHOLD", 0.80
        )
        # An ABSOLUTE ceiling on the verbatim prompt, alongside the fractions
        # above. Those fractions were written for a small window and are now
        # mis-tuned in the other direction: against a 1,000,000-token window
        # the usable budget is ~991,000, so 0.80 means compaction first runs
        # at ~793,000 tokens — i.e. never, in any real conversation. A
        # 60-message session measured 5,826 tokens, 0.58% of the window, and
        # had no summary at all.
        #
        # That mattered because the engines then showed the model only the
        # last few turns, so the ONLY memory a long chat had was a slice that
        # ended three exchanges ago. Whichever of the two triggers fires
        # first now wins, so older turns become a summary at a size that
        # actually occurs. 0 disables the absolute one.
        self.context_compact_max_tokens: int = _int(
            "CONTEXT_COMPACT_MAX_TOKENS", 40_000
        )
        self.keep_recent_turns: int = _int("KEEP_RECENT_TURNS", 8)
        self.summary_max_tokens: int = _int("SUMMARY_MAX_TOKENS", 2000)
        # An answer this short is a failure for a thinking model (it burns the
        # budget reasoning and emits nothing), so input is trimmed further
        # rather than letting the output floor drop below this.
        self.min_output_floor: int = _int("MIN_OUTPUT_FLOOR", 1024)

        # --- Phase B: semantic recall over compacted turns.
        self.semantic_recall_enabled: bool = _bool("SEMANTIC_RECALL_ENABLED", True)
        self.retrieve_top_k: int = _int("RETRIEVE_TOP_K", 6)
        # V10 cross-chat memory: semantic recall over the user's other
        # conversations, plus the explicit fact store (ChatGPT-style Memory).
        # Both assistant-mode only; both fail soft.
        self.cross_chat_semantic_enabled: bool = _bool(
            "CROSS_CHAT_SEMANTIC_ENABLED", True
        )
        # Cosine floor below which a candidate is noise, not a memory.
        # A hit must clear BOTH floors: this absolute one, and a fraction of
        # the best hit in the same query (below). The absolute floor alone
        # returns whatever clears it when nothing relevant exists, which on a
        # small corpus is noise presented as memory.
        self.semantic_recall_relative_floor: float = _float(
            "SEMANTIC_RECALL_RELATIVE_FLOOR", 0.75
        )
        self.semantic_recall_min_score: float = _float(
            "SEMANTIC_RECALL_MIN_SCORE", 0.30
        )
        self.fact_extraction_enabled: bool = _bool("FACT_EXTRACTION_ENABLED", True)
        self.memory_max_facts: int = _int("MEMORY_MAX_FACTS", 200)

        # --- Phase C: context meter.
        self.context_meter_enabled: bool = _bool("CONTEXT_METER_ENABLED", True)

        # --- Phase 4: ZIP & dataset uploads. Files are PROFILED, never
        # executed and never sent to the model raw.
        self.dataset_uploads_enabled: bool = _bool("DATASET_UPLOADS_ENABLED", True)
        self.upload_max_mb: int = _int("UPLOAD_MAX_MB", 200)
        # V29 (2026-09-10): how long a chunked upload session stays resumable
        # after `init`. The sweep reclaims the parts of an open session past
        # this and marks it expired; a finished one is never touched.
        self.upload_session_ttl_hours: float = _float("UPLOAD_SESSION_TTL_HOURS", 24.0)
        # Caps applied to any ZIP-shaped container — including .xlsx, which is
        # itself a zip and would otherwise be a bomb path around archive.py.
        self.archive_max_uncompressed_mb: int = _int(
            "ARCHIVE_MAX_UNCOMPRESSED_MB", 2048
        )
        self.archive_max_files: int = _int("ARCHIVE_MAX_FILES", 10000)
        self.archive_max_ratio: int = _int("ARCHIVE_MAX_RATIO", 200)
        # 1 = extract the outer archive only. Recursion is where bomb maths
        # turns exponential, so inner archives are listed, never opened.
        self.archive_max_depth: int = _int("ARCHIVE_MAX_DEPTH", 1)
        # The ONLY raw file content that reaches the model, all truncated.
        self.profile_sample_rows: int = _int("PROFILE_SAMPLE_ROWS", 5)
        self.profile_cell_chars: int = _int("PROFILE_CELL_CHARS", 200)
        self.profile_top_values: int = _int("PROFILE_TOP_VALUES", 5)
        self.profile_max_files: int = _int("PROFILE_MAX_FILES", 40)
        self.profile_max_columns: int = _int("PROFILE_MAX_COLUMNS", 60)
        # Small-file full-content path (2026-08-06, owner request: "the model
        # should get ALL the data, like ChatGPT"): a table at or under this
        # many rows ships to the prompt in FULL (cells still clipped), so the
        # model can compute exact sums/group-bys. Larger files keep the
        # profile-only rule. The char cap bounds the worst case (many wide
        # columns) — over it, the file falls back to profile-only.
        self.profile_full_rows_max: int = _int("PROFILE_FULL_ROWS_MAX", 200)
        self.profile_full_chars: int = _int("PROFILE_FULL_CHARS", 60000)

        # --- Phase 1: web search (all off by default; SearXNG is the free,
        # self-hosted default provider once SEARXNG_URL is set). ---
        self.search_enabled: bool = _bool("SEARCH_ENABLED", False)
        self.search_provider: str = os.environ.get("SEARCH_PROVIDER", "searxng")
        self.searxng_url: str = os.environ.get("SEARXNG_URL", "").rstrip("/")
        self.tavily_api_key: str = os.environ.get("TAVILY_API_KEY", "")
        self.brave_api_key: str = os.environ.get("BRAVE_API_KEY", "")
        self.search_max_results: int = _int("SEARCH_MAX_RESULTS", 100)
        self.fetch_timeout_ms: int = _int("FETCH_TIMEOUT_MS", 8000)
        self.fetch_max_bytes: int = _int("FETCH_MAX_BYTES", 5_000_000)
        # Per-source extraction budget (~2k tokens ≈ 8k chars) and per-user
        # search rate limit (searches per minute).
        self.search_source_char_budget: int = _int("SEARCH_SOURCE_CHAR_BUDGET", 8000)
        self.search_rate_per_min: int = _int("SEARCH_RATE_PER_MIN", 10)
        self.search_cache_ttl: float = _float("SEARCH_CACHE_TTL", 900.0)  # 15 min
        # V8 web-search memory (2026-08-30): persist every search + fetched
        # page in PostgreSQL and keep an embedded-chunk index over them, so
        # repeat and related questions reuse pages already read. The vector
        # index is DERIVED — deleting the directory is safe, PostgreSQL
        # rebuilds it.
        self.web_memory_enabled: bool = _bool("WEB_MEMORY_ENABLED", True)
        # --- Vector index policy (ADR-0001 D8) ------------------------------
        # Flat scan below this many chunks (measured 19 ms at 9k rows); an
        # IVF_FLAT index above it (measured on a 90k-row copy: 9 ms at
        # recall@10 = 0.995 with 50 probes, versus 150 ms flat). Built by the
        # background worker, never on a request. The table is compacted and
        # old versions pruned every WEB_INDEX_OPTIMIZE_EVERY worker cycles.
        self.web_index_ann_min_rows: int = _int("WEB_INDEX_ANN_MIN_ROWS", 50_000)
        self.web_index_nprobes: int = _int("WEB_INDEX_NPROBES", 50)
        self.web_index_optimize_every: int = _int("WEB_INDEX_OPTIMIZE_EVERY", 12)
        # Embedding calls sit on every assistant turn's critical path; this is
        # their own read timeout (the generation wall clock is far too long).
        self.embed_timeout_s: float = _float("EMBED_TIMEOUT_S", 4.0)
        self.embed_batch_timeout_s: float = _float("EMBED_BATCH_TIMEOUT_S", 90.0)
        # Query embeddings are bounded the way reranking is (ADR-0001 D13):
        # this many in flight, and a caller waits at most EMBED_WAIT_S for a
        # slot before retrieving lexical-only.
        self.embed_max_inflight: int = _int("EMBED_MAX_INFLIGHT", 8)
        self.embed_wait_s: float = _float("EMBED_WAIT_S", 1.0)
        # Reader-side escape hatch for an ANN index: bypass it (flat scan)
        # without touching data — the instant rollback for D8.
        self.knowledge_ann_bypass: bool = _bool("KNOWLEDGE_ANN_BYPASS", False)
        # STALE (design critique 2026-09-03): the best ANSWERING passage's
        # own content date is older than this for a RECENT question → the
        # store does not count as sufficient even though the copy is fresh.
        # A REALTIME question uses its max age; STATIC never goes stale.
        self.knowledge_stale_after_recent_s: int = _int("KNOWLEDGE_STALE_AFTER_RECENT_S", 120 * 86400)
        # --- Living knowledge layer (2026-09-01) -------------------------
        # Web memory used to be reachable only from inside the search engine,
        # so a Fast/no-search chat answered from frozen weights while the
        # corpus already held the current answer. These govern the pre-answer
        # stage that closes that gap (app/living_knowledge.py).
        self.living_knowledge_enabled: bool = _bool("LIVING_KNOWLEDGE_ENABLED", True)
        # Ask the 8B router when the lexical freshness rules are ambiguous.
        # Off => ambiguous questions default to RECENT, which costs a local
        # lookup rather than a wrong answer.
        self.freshness_router_enabled: bool = _bool("FRESHNESS_ROUTER_ENABLED", True)
        # Fast mode may spend THIS much network, and only when the question is
        # time-sensitive and the corpus cannot answer it. A full search reads
        # 5-8 pages; this reads two.
        self.freshness_fast_lookup: bool = _bool("FRESHNESS_FAST_LOOKUP", True)
        self.freshness_fast_sources: int = _int("FRESHNESS_FAST_SOURCES", 2)
        # 12 → 8 s (2026-09-03): the deadline is the worst case a Fast answer
        # waits before it starts; a lookup that has not landed two pages in
        # eight seconds is not going to make the answer better.
        self.freshness_fast_deadline_s: float = _float("FRESHNESS_FAST_DEADLINE_S", 8.0)
        # How much locally-read evidence a grounded answer may carry. 900
        # characters was one paragraph — "a large amount of information from
        # the site" (owner, 2026-09-03) needs several passages, and ~1k
        # tokens of prefill costs the Fast answer well under half a second.
        # 6000, not 3600. Measured 2026-09-04 on the 60-item eval set, paired
        # on identical retrievals: at 3,600 the prompt contained the answer for
        # 33/60 questions, at 6,000 for 43/60. The retrieval had already found
        # it in 44 of them — the budget, not the search, was the ceiling. The
        # extra 2,400 characters are ~600 tokens of prefill against a 1M
        # window, and 10,000 measured no better than 6,000, so this is the knee.
        self.living_knowledge_evidence_chars: int = _int("LIVING_KNOWLEDGE_EVIDENCE_CHARS", 6000)
        # Topical grounding: a TIMELESS question is also answered from the
        # corpus when a strongly matching passage exists (a site the user
        # indexed, a doc a research run read) — the knowledge base a shared
        # site is supposed to become. Gated on a strong match so ordinary
        # chat never drags in loosely related pages.
        self.living_knowledge_topical: bool = _bool("LIVING_KNOWLEDGE_TOPICAL", True)
        # --- The unified evidence pipeline (ADR-0001, 2026-09-03) -----------
        # Every candidate passage is scored by the templated cross-encoder
        # for "does this answer the question" (app/rerank.py); that
        # probability — not entity-word overlap — decides relevance,
        # sufficiency and the order the model sees. Off => the hybrid score
        # alone, as before.
        self.knowledge_rerank: bool = _bool("KNOWLEDGE_RERANK", True)
        # How many hybrid candidates are sent to the reranker per question
        # (after duplicate collapse). Measured: 8 → 82 ms, 15 → 105 ms,
        # 30 → 172 ms. A STATIC question judges at most 8.
        self.knowledge_rerank_candidates: int = _int("KNOWLEDGE_RERANK_CANDIDATES", 12)
        # A passage is RELEVANT (may be cited, may supersede) above this
        # answer probability, and the corpus is SUFFICIENT (no live lookup)
        # when the best passage clears the higher bar, or two clear the
        # lower one.
        self.knowledge_relevant_threshold: float = _float("KNOWLEDGE_RELEVANT_THRESHOLD", 0.30)
        self.knowledge_answer_threshold: float = _float("KNOWLEDGE_ANSWER_THRESHOLD", 0.70)
        # Local first at every effort: when the stored corpus answers with at
        # least this probability and is fresh enough for the question, an
        # AUTO-decided web search is skipped and the answer is grounded on
        # the store (a forced search still runs, merged with stored evidence).
        self.knowledge_local_first: bool = _bool("KNOWLEDGE_LOCAL_FIRST", True)
        # The whole pre-answer stage (classify, retrieve, judge, and at Fast
        # the tiny live lookup with its own 8 s cap) may take at most this
        # long before the answer proceeds without it. A wedged sidecar costs
        # a request this budget, never the generation wall clock.
        self.knowledge_prepare_deadline_s: float = _float("KNOWLEDGE_PREPARE_DEADLINE_S", 12.0)
        self.knowledge_local_first_confidence: float = _float("KNOWLEDGE_LOCAL_FIRST_CONFIDENCE", 0.85)
        # Public-scope evidence cache: normalised question -> ranked evidence,
        # for a few seconds. Holds ONLY public web evidence (no user or
        # conversation data can enter it), so it is safe to share between
        # users; the TTL is far below every freshness window.
        self.knowledge_evidence_cache_ttl_s: float = _float("KNOWLEDGE_EVIDENCE_CACHE_TTL_S", 60.0)
        self.knowledge_evidence_cache_size: int = _int("KNOWLEDGE_EVIDENCE_CACHE_SIZE", 256)
        # Cross-chat recall of the assistant's OWN earlier answers for a
        # question that needs evidence (an office holder, a price, a release).
        # Off (default): only what the USER said in earlier chats is recalled
        # for such questions — the forensic audit found an earlier answer
        # being repeated and cited against sources that did not contain it.
        self.recall_assistant_answers_for_facts: bool = _bool("RECALL_ASSISTANT_ANSWERS_FOR_FACTS", False)
        # The FLOOR on the blended score; the real gate is vector agreement
        # AND lexical overlap together (living_knowledge._topical). Measured
        # 2026-09-02: a true documentation match scores 0.44-0.61 here.
        self.living_knowledge_topical_min_score: float = _float(
            "LIVING_KNOWLEDGE_TOPICAL_MIN_SCORE", 0.4
        )
        # --- Attachment latency (2026-09-03) ------------------------------
        # The OCR sidecar transcribes BEFORE the main model can start. On a
        # text-dense screenshot at Think that measured 47 s to the first
        # visible token (the 3.3B model decoding a long transcript), against
        # 2.3 s at Fast with no OCR. OCR is an enhancer, never a gate: the
        # image route now caps what it asks for and waits at most this long,
        # then proceeds pixels-only.
        self.ocr_vision_deadline_s: float = _float("OCR_VISION_DEADLINE_S", 10.0)
        self.ocr_vision_max_tokens: int = _int("OCR_VISION_MAX_TOKENS", 1500)
        # Extract a document the moment it finishes uploading (text layer,
        # page renders, OCR of scanned pages) so the send that follows reads
        # a cache instead of paying for extraction on the answer's critical
        # path — the way ChatGPT processes a file while you type.
        self.document_prewarm_enabled: bool = _bool("DOCUMENT_PREWARM_ENABLED", True)
        self.document_prewarm_max_mb: int = _int("DOCUMENT_PREWARM_MAX_MB", 64)
        # Background keeper: drains the embedding backlog and re-reads pages
        # past their TTL, newest-demand first. Runs inside the orchestrator as
        # an asyncio task — no extra container, and the queue is a PostgreSQL
        # column so a restart resumes rather than forgets.
        self.web_knowledge_worker_enabled: bool = _bool("WEB_KNOWLEDGE_WORKER_ENABLED", True)
        self.web_worker_interval_s: int = _int("WEB_WORKER_INTERVAL_S", 300)
        self.web_refresh_max_pages_per_cycle: int = _int("WEB_REFRESH_MAX_PAGES_PER_CYCLE", 8)
        self.web_refresh_concurrency: int = _int("WEB_REFRESH_CONCURRENCY", 2)
        # Per-level re-read deadlines. A page about an office holder is worth
        # re-reading daily; documentation is not.
        self.web_refresh_realtime_ttl_s: int = _int("WEB_REFRESH_REALTIME_TTL_S", 6 * 3600)
        self.web_refresh_recent_ttl_s: int = _int("WEB_REFRESH_RECENT_TTL_S", 24 * 3600)
        self.web_refresh_static_ttl_s: int = _int("WEB_REFRESH_STATIC_TTL_S", 21 * 24 * 3600)
        self.lancedb_web_dir: str = os.environ.get(
            "LANCEDB_WEB_DIR", "/data/lancedb-web"
        )
        # A stored page younger than this is served without refetching. Fresh-
        # intent questions (the _FRESH_RE shapes) use the short TTL: "latest
        # release" must hit the network even when a day-old copy exists.
        self.web_page_ttl_s: int = _int("WEB_PAGE_TTL_S", 24 * 3600)
        self.web_page_fresh_ttl_s: int = _int("WEB_PAGE_FRESH_TTL_S", 3600)
        # V9 site crawler (2026-08-30): "index this site" crawls one site,
        # sitemap-first, robots-respecting, into the same web store. The caps
        # are the difference between a librarian and a vacuum cleaner.
        self.web_crawl_enabled: bool = _bool("WEB_CRAWL_ENABLED", True)
        self.web_crawl_max_pages: int = _int("WEB_CRAWL_MAX_PAGES", 1000)
        self.web_crawl_max_minutes: float = _float("WEB_CRAWL_MAX_MINUTES", 15.0)
        self.web_crawl_max_depth: int = _int("WEB_CRAWL_MAX_DEPTH", 4)
        self.web_crawl_concurrency: int = _int("WEB_CRAWL_CONCURRENCY", 3)
        self.web_crawl_delay_ms: int = _int("WEB_CRAWL_DELAY_MS", 400)
        # Background expansion after a search (owner idea): follow a few
        # in-site links from the pages a search just read, so the next related
        # question hits warm content. One hop, few pages, few domains.
        self.web_expand_after_search: bool = _bool("WEB_EXPAND_AFTER_SEARCH", True)
        self.web_expand_pages_per_domain: int = _int("WEB_EXPAND_PAGES_PER_DOMAIN", 8)
        self.web_expand_max_domains: int = _int("WEB_EXPAND_MAX_DOMAINS", 3)
        # --- Background crawl queue (2026-09-03) -------------------------
        # Sharing a URL used to read ONE page. Now the page is answered from
        # immediately AND the site it lives on is queued for a bounded
        # background crawl (engines/crawl.py:enqueue_site_crawl), drained by
        # the knowledge worker one job at a time. The caps are per job; a
        # site bigger than them resumes for free on the next share because
        # stored pages cost nothing.
        self.web_background_crawl_enabled: bool = _bool("WEB_BACKGROUND_CRAWL_ENABLED", True)
        self.web_share_crawl_enabled: bool = _bool("WEB_SHARE_CRAWL_ENABLED", True)
        self.web_share_crawl_max_pages: int = _int("WEB_SHARE_CRAWL_MAX_PAGES", 150)
        self.web_share_crawl_max_minutes: float = _float("WEB_SHARE_CRAWL_MAX_MINUTES", 8.0)

        # --- Deep Research (2026-08-30): the iterative mode. Every default
        # below is derived from measurement on this deployment, not taste:
        # the main model decodes ~70 tok/s (1136 tokens in 16.1 s) and a
        # guided-JSON planning call costs ~2 s, so three iterations plus a
        # ~1500-token report is ~45 s of GPU time; SearXNG answers six
        # parallel queries in ~2.2 s and page extraction is serialised on one
        # worker, so the network side dominates. 24 sources at 8000 chars is
        # ~48k tokens of evidence — comfortable in a 1M window, and small
        # enough that the report call is not itself the bottleneck.
        self.deep_research_enabled: bool = _bool("DEEP_RESEARCH_ENABLED", True)
        # 2026-09-03: 3 → 5 rounds and 24 → 36 sources. The loop no longer
        # stops on a fixed count — it stops on evidence (see
        # engines/deep_research.py: sufficiency, information gain, duplicate
        # rate, budget) — so the caps are a ceiling for the hard question,
        # not the typical run. The wall-clock budget is unchanged.
        self.deep_research_max_iterations: int = _int("DEEP_RESEARCH_MAX_ITERATIONS", 5)
        self.deep_research_max_queries_per_iteration: int = _int(
            "DEEP_RESEARCH_MAX_QUERIES_PER_ITERATION", 5
        )
        self.deep_research_sources_per_iteration: int = _int(
            "DEEP_RESEARCH_SOURCES_PER_ITERATION", 10
        )
        self.deep_research_max_sources: int = _int("DEEP_RESEARCH_MAX_SOURCES", 36)
        # Below this the loop searches the plan's remaining angles instead of
        # asking the auditor whether three pages are enough.
        self.deep_research_min_sources: int = _int("DEEP_RESEARCH_MIN_SOURCES", 6)
        self.deep_research_timeout_s: float = _float("DEEP_RESEARCH_TIMEOUT_S", 600.0)
        # --- Admission control (R13). ---------------------------------
        # Deep Research used to take a process-wide lock and REFUSE anyone
        # else while it was held, so one person's ten-minute run failed every
        # other user's request org-wide. These three bound it properly
        # instead. The scarce resource is MODEL time, not CPU: the engine's
        # `_LLM_SEM` already caps concurrent generations at 2 process-wide and
        # is shared by every run, so a second run interleaves inside the same
        # two model slots rather than adding a third stream for interactive
        # chat to queue behind.
        #
        # 2 concurrent runs: what actually doubles is SearXNG queries and
        # outbound fetches (16 per gather), not GPU streams. Raising it does
        # not buy more research throughput — the model slots are the
        # bottleneck — it only makes each run slower and more likely to hit
        # its wall clock, so the honest ceiling is low.
        self.deep_research_max_concurrent: int = _int("DEEP_RESEARCH_MAX_CONCURRENT", 2)
        # 1 per person, so a single user (or a reloaded browser tab) can never
        # occupy the whole machine. This is the fairness half: the ceiling
        # above bounds the box, this bounds any one requester.
        self.deep_research_max_per_user: int = _int("DEEP_RESEARCH_MAX_PER_USER", 1)
        # How long a request may WAIT for a slot before it is refused with a
        # reason. Queueing beats refusing, but only up to a point: the user is
        # watching a stream that carries nothing but heartbeats while it
        # waits, and a run holds its slot for minutes. 45 s absorbs the real
        # case (a run finishing, two requests arriving together) and then
        # hands back a truthful "try again in about N minutes" instead of a
        # spinner. 0 disables queueing (refuse immediately).
        self.deep_research_queue_wait_s: float = _float("DEEP_RESEARCH_QUEUE_WAIT_S", 45.0)
        self.deep_research_report_max_tokens: int = _int(
            "DEEP_RESEARCH_REPORT_MAX_TOKENS", 6000
        )
        # Links followed FROM the pages a round read (the citation an article
        # gives for its claim, the official page a summary links to, the PDF
        # behind a news story). Per round, on top of search results.
        self.deep_research_links_per_round: int = _int("DEEP_RESEARCH_LINKS_PER_ROUND", 6)
        # The self-correction pass: before the report, review each
        # subquestion's evidence and run ONE more targeted round when a
        # key claim is thin, unverified against a primary source, or the
        # sources disagree.
        self.deep_research_verify: bool = _bool("DEEP_RESEARCH_VERIFY", True)
        self.deep_research_min_confidence: float = _float("DEEP_RESEARCH_MIN_CONFIDENCE", 0.6)
        # Near-duplicate threshold (word-shingle Jaccard). Ten syndicated
        # copies of one report count as ONE independent source.
        #
        # R11's rewrite rule (a large absolute volume of shared text that is
        # also at least half the shorter page) is deliberately NOT scaled by
        # this, and after checking, it deliberately gets NO knob of its own:
        #
        #   * its floor is a MEASUREMENT, not a taste — 200 shared 6-grams
        #     against 31 for two independent reports of one event — and a
        #     knob invites retuning it without the fixtures that produced it;
        #   * the two directions are not symmetrical. Set it too low and
        #     corroboration is merely withheld; set it too high and the rule
        #     stops firing, syndicated copies count as independent voices
        #     again, and confidence goes UP — the exact defect R11 exists to
        #     stop, reintroduced silently by a configuration change;
        #   * `provenance.near_duplicate` has a second caller
        #     (`web_memory._collapse_duplicates`, on the retrieval path) that
        #     passes the DEFAULT threshold, so a `DEEP_RESEARCH_*` variable
        #     would name a rule it only half governs. If it ever becomes
        #     configurable it belongs to `core/provenance`, not here.
        #
        # The operational escape hatch already exists and is honest about its
        # scope: 0 disables duplicate detection for this engine entirely.
        self.deep_research_duplicate_threshold: float = _float(
            "DEEP_RESEARCH_DUPLICATE_THRESHOLD", 0.6
        )
        # Two consecutive rounds adding fewer than this share of new,
        # non-duplicate sources (and no new claims) means the web has been
        # mined for this question: stop, whatever the iteration cap says.
        self.deep_research_min_gain: float = _float("DEEP_RESEARCH_MIN_GAIN", 0.15)
        # After the report: queue the top primary domains for a bounded
        # background crawl so the NEXT question about them answers locally.
        self.deep_research_background_crawl: bool = _bool("DEEP_RESEARCH_BACKGROUND_CRAWL", True)
        self.deep_research_crawl_pages_per_domain: int = _int(
            "DEEP_RESEARCH_CRAWL_PAGES_PER_DOMAIN", 40
        )
        self.deep_research_crawl_max_domains: int = _int("DEEP_RESEARCH_CRAWL_MAX_DOMAINS", 3)

        # --- Phase 2: URL / website analysis (fetches pasted links). ---
        self.url_analysis_enabled: bool = _bool("URL_ANALYSIS_ENABLED", True)
        self.url_max_pages: int = _int("URL_MAX_PAGES", 5)

        # --- Phase 3: GitHub repository analysis (public repos only). ---
        self.repo_analysis_enabled: bool = _bool("REPO_ANALYSIS_ENABLED", True)
        self.repo_max_mb: int = _int("REPO_MAX_MB", 300)
        self.repo_max_files: int = _int("REPO_MAX_FILES", 20000)
        self.workspace_dir: str = os.environ.get("WORKSPACE_DIR", "/data/workspaces")
        self.workspace_ttl_hours: int = _int("WORKSPACE_TTL_HOURS", 24)
        self.workspace_quota_gb: int = _int("WORKSPACE_QUOTA_GB", 20)
        self.repo_final_chunks: int = _int("REPO_FINAL_CHUNKS", 12)

        # --- Charts (§8) ---
        # explicit — a chart appears only when the user asked for one, in
        #            words. This is the historical behaviour and the default.
        # hybrid   — explicit requests still work, and a small set of
        #            deterministic, high-confidence result shapes (time
        #            series, category comparison, trusted Salesforce stage
        #            funnel, small part-to-whole) chart themselves.
        # There is no `automatic` mode: everything else stays text + table.
        # An unrecognised value falls back to `explicit` rather than
        # guessing, because the failure mode of guessing is charts appearing
        # where nobody wanted them.
        _mode = os.environ.get("CHART_TRIGGER_MODE", "explicit").strip().lower()
        self.chart_trigger_mode: str = _mode if _mode in CHART_TRIGGER_MODES else "explicit"

        # When to ask before answering a Salesforce question.
        #   ambiguous — the default: only when the question has more than one
        #               honest reading, e.g. "failed the mock" in a slot, which
        #               returned 7, 20 and 0 on three runs of one sentence
        #   always    — LOWER THE BAR, as a bias on the planner. It used to bolt
        #               a content-free "have I read this right?" card onto every
        #               Salesforce question; that card could not be answered
        #               without re-reading your own sentence, and because its
        #               own option labels contained the words its detectors
        #               matched on, answering it asked it again. Expressed as a
        #               bias it says the same thing where it can act on it, and
        #               the planner still needs two genuinely different readings
        #               to offer — so the mode can no longer produce a question
        #               with nothing in it.
        #   off       — never ask. Context resolution still runs, so follow-ups
        #               keep working; the assistant states its assumptions.
        _clar = os.environ.get("CLARIFY_MODE", "ambiguous").strip().lower()
        self.clarify_mode: str = (
            _clar if _clar in ("always", "ambiguous", "off") else "ambiguous"
        )
        self.clarify_before_answering: bool = self.clarify_mode != "off"

        # --- Salesforce Intelligence Mode (2026-08-11) ----------------------
        # The Salesforce pill stops being a retrieval filter and becomes a
        # context-aware agent: it resolves "what about EMEA?" against this
        # conversation, asks ONE targeted question when a missing detail would
        # change the answer, and resumes the original request afterwards.
        #
        # Off → the previous behaviour, exactly: the deterministic detectors in
        # core/clarify.py and the existing router → engine chain. Every flag
        # here is a kill switch that leaves a working product behind it.
        self.salesforce_intelligence_enabled: bool = _bool(
            "SALESFORCE_INTELLIGENCE_MODE_ENABLED", True
        )
        self.salesforce_contextual_clarification_enabled: bool = _bool(
            "SALESFORCE_CONTEXTUAL_CLARIFICATION_ENABLED", True
        )
        self.salesforce_starter_card_enabled: bool = _bool(
            "SALESFORCE_STARTER_CARD_ENABLED", True
        )
        # Two rounds, then answer with the safest reading and SAY so. A third
        # round has never once been the difference between a right and a wrong
        # answer; it is just an interrogation.
        self.salesforce_max_clarification_rounds: int = _int(
            "SALESFORCE_MAX_CLARIFICATION_ROUNDS", 2
        )
        self.salesforce_allow_custom_clarification: bool = _bool(
            "SALESFORCE_ALLOW_CUSTOM_CLARIFICATION", True
        )
        # Let a clarification card take SEVERAL answers (owner request
        # 2026-08-11). Asked which object holds payment and invoice data,
        # "Invoice__c" and "Payment__c" is the honest answer, and a radio group
        # forced a choice between two things the user needed together.
        # `EXCLUSIVE_SLOTS` in core/sf_intel/state.py still pins the slots where
        # two answers make no sense. False restores single-answer cards unless
        # the planner explicitly asks for multi.
        self.salesforce_multi_select_clarification: bool = _bool(
            "SALESFORCE_MULTI_SELECT_CLARIFICATION", True
        )
        # The planner prefers the served model's own tool parser over guided
        # JSON when the runtime has one; see llm.chat_with_tools.
        self.salesforce_planner_tool_calling: bool = _bool(
            "SALESFORCE_PLANNER_TOOL_CALLING",
            _bool("MAIN_MODEL_ENABLE_AUTO_TOOL_CHOICE", True),
        )
        # Headroom reserved on a FULL-WINDOW request, distinct from the
        # per-call `context_safety_margin` above (which is the small slack every
        # request already leaves for chat-template drift). At 262144 the
        # documented budget is: 262144 − 16384 reserved output − 8192 margin
        # = 237568 tokens of constructed input.
        self.main_model_context_safety_margin: int = _int(
            "MAIN_MODEL_CONTEXT_SAFETY_MARGIN", 8192
        )

        # --- Row caps (§8) ---
        self.sql_preview_row_cap: int = _int("SQL_PREVIEW_ROW_CAP", PREVIEW_ROW_CAP)  # 500
        self.export_row_cap: int = _int("EXPORT_ROW_CAP", EXPORT_ROW_CAP)  # 100_000
        # How many rows the deterministic summary — the "authoritative,
        # computed over every row" block the model must quote from — is
        # actually computed over. It used to inherit the 500-row PREVIEW cap,
        # so a query matching 33,000 interviews had its "totals" computed over
        # 1.5% of the result while the prompt called them exact. The preview
        # cap is a UI concern (rows on meta.data); this one is a truth concern,
        # so it matches the export cap: if we will write 100k rows to a CSV,
        # we can count 100k rows in memory.
        self.sql_summary_row_cap: int = _int("SQL_SUMMARY_ROW_CAP", EXPORT_ROW_CAP)

        # --- RAG (§8): LanceDB top-30 → rerank → top-8 ---
        self.rag_top_k: int = _int("RAG_TOP_K", 30)
        self.rag_final_k: int = _int("RAG_FINAL_K", 8)

        # --- CORS: only the local Next.js frontend origins may call the
        # orchestrator from a browser (the frontend proxies /chat and /reports
        # through its own API routes, so nothing wider is ever needed).
        self.cors_allow_origins: list = [
            origin.strip()
            for origin in os.environ.get(
                "CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
            ).split(",")
            if origin.strip()
        ]

        # --- App state (V2-DESIGN §3c): auth + server-side history.
        # PostgreSQL since 2026-08-10 (was stdlib sqlite3 at /data/app.sqlite3).
        # This is APP state only — the analytics data plane stays
        # DuckDB/LanceDB/Parquet per SPEC §4, and DuckDB is deliberately NOT
        # migrated: it is a columnar engine and beats PostgreSQL at the SQL
        # engine's scan-and-aggregate queries.
        #
        # No default DSN on purpose. A silent fallback to some localhost
        # database is how a deploy ends up writing history nobody can find;
        # missing configuration should fail loudly at startup instead.
        self.app_database_url: str = os.environ.get("APP_DATABASE_URL", "").strip()

        # --- Identity (2026-09-01): opaque server-side sessions, workspaces,
        # RBAC. Lifetimes are the persistent-login contract: a session rolls
        # forward with activity up to AUTH_SESSION_DAYS of idleness, and dies
        # unconditionally AUTH_SESSION_ABSOLUTE_DAYS after it was created.
        # "Stay signed in" unticked shortens the rolling window to hours and
        # drops the cookie's Max-Age (browser-session cookie).
        self.auth_session_days: int = _int("AUTH_SESSION_DAYS", 30)
        self.auth_session_absolute_days: int = _int("AUTH_SESSION_ABSOLUTE_DAYS", 90)
        self.auth_session_unremembered_hours: int = _int(
            "AUTH_SESSION_UNREMEMBERED_HOURS", 24
        )
        self.auth_cookie_name: str = os.environ.get("AUTH_COOKIE_NAME", "ts_session")
        # "auto": Secure when the request arrived over https (directly or via a
        # TRUSTED forwarded proto — see auth_trust_proxy_headers). localhost
        # development stays plain http and still gets a cookie.
        self.auth_cookie_secure: str = (
            os.environ.get("AUTH_COOKIE_SECURE", "auto").strip().lower()
        )
        # Login throttling: N failures inside the window locks that email (and
        # separately that source address) out for a few minutes. Short on
        # purpose — brute force is made impractical without handing an
        # attacker a permanent denial-of-service button for any email.
        self.auth_login_max_fails: int = _int("AUTH_LOGIN_MAX_FAILS", 8)
        self.auth_login_window_seconds: int = _int("AUTH_LOGIN_WINDOW_SECONDS", 900)
        self.auth_login_lock_seconds: int = _int("AUTH_LOGIN_LOCK_SECONDS", 300)
        self.auth_invitation_ttl_days: int = _int("AUTH_INVITATION_TTL_DAYS", 7)
        # X-Forwarded-For / X-Forwarded-Proto are LIES unless a proxy this
        # deployment controls sets them. Off by default; the compose frontend
        # proxy does not need it (audit rows then record the proxy's address,
        # which is honest about what is actually known).
        self.auth_trust_proxy_headers: bool = _bool("AUTH_TRUST_PROXY_HEADERS", False)
        # The workspace every account belongs to. One workspace per
        # deployment today; the data model supports more.
        self.workspace_name: str = os.environ.get(
            "WORKSPACE_NAME", "TechSara's Workspace"
        )
        # Pool bounds. max is per orchestrator process and must stay well under
        # the server's max_connections (60 in compose); 16 is far more than a
        # single-user box needs and leaves room for psql and the migration tool.
        self.app_db_pool_min: int = _int("APP_DB_POOL_MIN", 2)
        self.app_db_pool_max: int = _int("APP_DB_POOL_MAX", 16)
        # Seconds a caller waits for a free connection before giving up, and
        # the server-side statement timeout. Both exist so a pathological query
        # or an exhausted pool surfaces as an error instead of a hung request.
        self.app_db_pool_timeout: float = _float("APP_DB_POOL_TIMEOUT", 10.0)
        self.app_db_statement_timeout_ms: int = _int("APP_DB_STATEMENT_TIMEOUT_MS", 15_000)
        # How long startup waits for PostgreSQL to accept a connection before
        # giving up. This is a REBOOT concern, not a tuning knob: after a power
        # cut the Docker daemon starts every container at once and honours no
        # depends_on, so the orchestrator routinely wins the race against its
        # own database.
        self.app_db_startup_timeout: float = _float("APP_DB_STARTUP_TIMEOUT", 180.0)
        # Session-cookie signing secret: SESSION_SECRET env wins; otherwise a
        # secret is generated once and persisted here (chmod 600).
        self.session_secret_file: str = os.environ.get(
            "SESSION_SECRET_FILE", "/data/.session_secret"
        )

        # --- Conversation sharing (V20) ---
        # Off-by-default at the RISKY end only: sharing inside the workspace
        # is on, sharing to the open internet is a decision a super admin
        # makes for the workspace rather than one this file makes for them.
        self.conversation_sharing_enabled: bool = _bool("CONVERSATION_SHARING_ENABLED", True)
        self.public_sharing_enabled: bool = _bool("PUBLIC_CONVERSATION_SHARING_ENABLED", True)
        #: Where a share link points. Empty means "same origin as the request",
        #: which is right for every deployment that is not behind a tunnel with
        #: a different public name.
        self.public_share_base_url: str = os.environ.get("PUBLIC_SHARE_BASE_URL", "").rstrip("/")
        self.public_share_default_days: int = _int("PUBLIC_SHARE_DEFAULT_EXPIRY_DAYS", 30)
        self.public_share_max_days: int = _int("PUBLIC_SHARE_MAX_EXPIRY_DAYS", 365)
        #: A link that never expires is a link nobody remembers exists.
        self.public_share_allow_never: bool = _bool("PUBLIC_SHARE_ALLOW_NEVER_EXPIRE", False)
        # --- Long-form output: many bounded calls, one text -----------------
        #
        # MODEL_MAX_OUTPUT above is the ceiling on ONE call. It is not the
        # ceiling on an answer: `continuation.py` runs as many calls as the
        # budget allows and stitches them. These settings govern that loop.
        #
        # The numbers below are grounded in what this hardware actually does,
        # measured over 141 real requests: ~46 output tokens/second. So
        # 1,000,000 tokens is about six hours of continuous decoding, and
        # GEN_WALL_CLOCK_S (4,200 s) bounds any SINGLE call at ~190,000. The
        # million is reachable only across segments, and only deliberately.
        self.continuation_enabled: bool = _bool("CONTINUATION_ENABLED", True)
        #: The system-level ceiling. No request may exceed it whatever it asks.
        self.max_logical_output_tokens: int = _int(
            "MAX_LOGICAL_OUTPUT_TOKENS", 1_000_000
        )
        #: Per-effort budgets. ALL THREE default to the ceiling: an answer
        #: runs until the MODEL says it is finished, at every effort.
        #:
        #: These were tiered once (Fast one segment, Think 64k, Max the
        #: ceiling) to stop a passing question costing hours of GPU. Two
        #: things were wrong with that. The reasoning was backwards — a
        #: budget does not stop an expensive question being asked, it stops a
        #: needed answer being finished, and the guards that actually matter
        #: (the model saying it is done, repetition, no forward progress) are
        #: about QUALITY and are always on. And the Fast value was
        #: pathological: 8,192 sits 192 tokens above the chat engine's 8,000
        #: per-call ceiling, so every truncated Fast answer wrote exactly one
        #: segment, found 192 tokens left against a 512 minimum, and stopped
        #: with "this answer reached its length limit" on the FIRST call —
        #: the old silent truncation, now with a notice attached.
        #:
        #: What this costs is real and worth knowing: ~46 output tokens/second
        #: means 8k is about 3 minutes and 50k about 18. Almost nothing
        #: reaches the ceiling, because almost everything stops at `complete`
        #: — a 40,000-token budget measured against the live model produced
        #: 2,011 tokens and stopped, because the model was done.
        #:
        #: Set any of these lower to put the tiering back.
        self.continuation_budget_fast: int = _int(
            "CONTINUATION_BUDGET_FAST", self.max_logical_output_tokens
        )
        self.continuation_budget_think: int = _int(
            "CONTINUATION_BUDGET_THINK", self.max_logical_output_tokens
        )
        self.continuation_budget_max: int = _int(
            "CONTINUATION_BUDGET_MAX", self.max_logical_output_tokens
        )
        #: A backstop against a loop that makes tiny forward progress forever.
        #: At the default segment size this permits far more than any budget.
        self.continuation_max_segments: int = _int("CONTINUATION_MAX_SEGMENTS", 400)
        #: How much of the text so far each continuation sees verbatim. Big
        #: enough to finish an interrupted sentence and hold the voice; small
        #: enough that segment 200's prefill is the same cost as segment 2's.
        self.continuation_tail_chars: int = _int("CONTINUATION_TAIL_CHARS", 6_000)
        #: Never ask for a segment smaller than this — it would be all seam
        #: and no text.
        self.continuation_min_segment_tokens: int = _int(
            "CONTINUATION_MIN_SEGMENT_TOKENS", 512
        )
        #: Wall-clock ceiling for a whole long run, independent of the
        #: per-call GEN_WALL_CLOCK_S. 0 disables it.
        self.continuation_deadline_s: float = _float(
            "CONTINUATION_DEADLINE_S", 21_600.0
        )
        #: A research report gets its OWN total, well under the chat budget.
        #: DEEP_RESEARCH_REPORT_MAX_TOKENS is now the size of one segment;
        #: this is how long the whole report may run to. Nobody asking a
        #: research question wants a book back, so the default is four
        #: segments — enough that the report stops because it is finished
        #: rather than because it ran out of room, which is the thing that
        #: was actually wrong.
        self.deep_research_report_total_tokens: int = _int(
            "DEEP_RESEARCH_REPORT_TOTAL_TOKENS", 24_000
        )

        #: Per user, per hour. Creating a share writes a snapshot, so this
        #: bounds the write amplification of a script as much as the abuse.
        self.share_create_rate_per_hour: int = _int("SHARE_CREATE_RATE_PER_HOUR", 30)
        #: Per public id, per minute — the anonymous read path.
        self.share_view_rate_per_minute: int = _int("SHARE_VIEW_RATE_PER_MINUTE", 120)

        # --- Misc ---
        self.session_max_turns: int = _int("SESSION_MAX_TURNS", 20)
        # How many conversational turns the ASSISTANT sees. Was hardcoded at
        # 6 in engines/chat.py — three exchanges — which is why a 60-message
        # French lesson answered "how to translate" with a Python tutorial:
        # the last six turns were a goodnight exchange and the entire lesson
        # was outside the window.
        #
        # Six was a defence against a small context window. It is the wrong
        # layer for that now: `compaction.prepare` decides what the model
        # sees (a rolling summary plus recent turns, bounded by
        # CONTEXT_COMPACT_MAX_TOKENS above) and `context.fit_request`
        # guarantees the result physically fits. An engine truncating on top
        # of both only discards what those two chose to keep. This is a
        # backstop against a pathological thread, not a memory policy.
        self.chat_history_turns: int = _int("CHAT_HISTORY_TURNS", 400)
        # Read timeout for model calls. For a NON-streaming completion (agent
        # planning, synthesis, classification) this covers the WHOLE
        # generation, not an inter-byte gap, so it must not expire before the
        # generation the app itself permits: at 300s against a 1800s
        # GEN_WALL_CLOCK_S, a long planning call died inside the HTTP client
        # and the SDK silently re-ran it — burning the GPU and never
        # finishing. Defaulting to the wall clock keeps transport and
        # application consistent, leaving GEN_WALL_CLOCK_S the single place
        # that decides what "too long" means.
        self.llm_request_timeout: float = _float(
            "LLM_REQUEST_TIMEOUT", self.gen_wall_clock_s
        )
        #: vLLM is one hop away on the compose network, so a slow CONNECT means
        #: a dead service rather than a busy one — this stays short however
        #: long a generation is allowed to run.
        self.llm_connect_timeout: float = _float("LLM_CONNECT_TIMEOUT", 10.0)
        #: Sending the prompt (a large paste can be megabytes) and pool checkout.
        self.llm_write_timeout: float = _float("LLM_WRITE_TIMEOUT", 60.0)
        #: Automatic SDK retries are OFF by default: the SDK treats a read
        #: timeout as retryable, so one slow call silently became three, each
        #: re-running minutes of local GPU work. Callers that can retry
        #: meaningfully (the agent planner) already do so on parse failures.
        self.llm_max_retries: int = _int("LLM_MAX_RETRIES", 0)
        # --- Surviving an engine outage (app/resilience.py) -----------------
        # The SDK retries above stay OFF; this is the ONE retry layer, and it
        # retries only what a restart can fix (connection refused/reset, a
        # 5xx from a dying engine), never a 4xx or a read timeout. A reload of
        # the TP=2 pair measured 13 min on 2026-09-10, so a background job
        # waits up to LLM_RECOVERY_WINDOW_S for /health to come back; a
        # person watching a chat waits only LLM_INTERACTIVE_RECOVERY_S before
        # the MODEL_UNAVAILABLE sentence, which the client already handles.
        self.llm_recovery_window_s: float = _float("LLM_RECOVERY_WINDOW_S", 1200.0)
        self.llm_interactive_recovery_s: float = _float("LLM_INTERACTIVE_RECOVERY_S", 120.0)
        self.llm_retry_base_s: float = _float("LLM_RETRY_BASE_S", 2.0)
        self.llm_retry_cap_s: float = _float("LLM_RETRY_CAP_S", 30.0)
        self.llm_health_poll_s: float = _float("LLM_HEALTH_POLL_S", 5.0)
        # --- The circuit breaker in front of each engine (app/breaker.py) ---
        # 2026-09-11 22:15Z: a worker rank died and the head kept answering
        # /health, /v1/models and /metrics with 200 for five minutes while
        # every completion hung, so the wait-and-retry layer above — which
        # trusts /health — waited on a port that was never going to answer.
        # The breaker counts what actually happened to real calls: after
        # LLM_BREAKER_FAILURES counted failures inside LLM_BREAKER_WINDOW_S
        # it OPENS and nothing polls the engine but one canary call every
        # LLM_BREAKER_COOLDOWN_S (docs/availability/CONTRACT.md §8.2). It
        # also opens at once when the engine controller reports the engine
        # WEDGED / RECOVERING / DOWN / STARTING — see ENGINE_CONTROLLER_URL.
        self.llm_breaker_failures: int = _int("LLM_BREAKER_FAILURES", 3)
        self.llm_breaker_window_s: float = _float("LLM_BREAKER_WINDOW_S", 30.0)
        self.llm_breaker_cooldown_s: float = _float("LLM_BREAKER_COOLDOWN_S", 10.0)
        # --- The engine controller's verdict (app/engine_state.py) ---------
        # The controller (compose service `engine-controller`, CONTRACT §6)
        # runs the real canary against the head and publishes one of nine
        # states at GET /state. The orchestrator polls it every
        # ENGINE_STATE_POLL_S and treats a snapshot older than 15 s, or an
        # unreachable controller, as UNKNOWN — which never opens the
        # breaker: only observed failures and observed bad states do. An
        # empty URL (or a poll interval of 0) switches the poller off.
        # The same variable name serves scripts/cluster-recover.sh and
        # scripts/cluster-status.sh as a BASE URL (`http://127.0.0.1:9838`),
        # so a value without a path is completed to its /state document here
        # rather than making the two shapes fight (review finding 2026-09-12).
        self.engine_controller_url: str = _controller_state_url(
            os.environ.get("ENGINE_CONTROLLER_URL", "http://vllm:9838/state")
        )
        self.engine_state_poll_s: float = _float("ENGINE_STATE_POLL_S", 5.0)
        # The breaker's single HALF_OPEN canary is whichever real call
        # arrives first, and a non-streaming call can run for minutes (a
        # Max-effort candidate thinking, a compaction summary). After this
        # many seconds an outstanding canary stops blocking everyone else
        # and the next caller probes too; the late outcome is then an
        # ordinary one (review finding 2026-09-12, breaker.py:183). Sized
        # like the controller's CANARY_TIMEOUT_S.
        self.llm_breaker_canary_hold_s: float = _float("LLM_BREAKER_CANARY_HOLD_S", 60.0)
        # --- Continuity in one-model mode (app/continuity.py, CONTRACT §8.3)
        # Only the main model may answer a person. While its breaker is OPEN
        # or the controller reports it STARTING / WEDGED / RECOVERING / DOWN,
        # a chat turn is not failed and not answered by anything else: its
        # row goes `queued`, the person reads one truthful line, and the
        # SAME generation resumes when the controller reports READY. This
        # is how long a turn waits before the row is parked for the resume
        # sweep: a cold start measured 5 m 20 s, plus margin.
        self.llm_queue_max_wait_s: float = _float("LLM_QUEUE_MAX_WAIT_S", 900.0)
        # The resume sweep (CONTRACT §8.4) runs rows a process parked or a
        # restart interrupted when the controller reports READY; a row older
        # than this is left alone — nobody is waiting for it, and a thread
        # would only be surprised by an answer to a day-old question.
        self.llm_resume_max_age_s: float = _float("LLM_RESUME_MAX_AGE_S", 3600.0)
        # --- Long-context admission (app/admission.py, CONTRACT §6.7) ------
        # Two lanes in front of the engine. A prompt at or below the
        # threshold takes the NORMAL lane (ADMISSION_NORMAL_MAX concurrent
        # generations); above it, the LONG lane runs ONE at a time, first
        # draining the normal lane and waiting for the engine to report
        # `requests_running` at or below ADMISSION_LONG_IDLE_MAX (from the
        # controller's engine sample) for at most ADMISSION_LONG_WAIT_S, so a
        # 131k-token prefill never mixes with nine others — the exact load
        # shape of the 2026-09-11 GDN fault (docs/availability/INCIDENT-…).
        self.admission_long_threshold_tokens: int = _int("ADMISSION_LONG_THRESHOLD_TOKENS", 131072)
        self.admission_normal_max: int = _int("ADMISSION_NORMAL_MAX", 10)
        self.admission_long_max: int = _int("ADMISSION_LONG_MAX", 1)
        self.admission_long_idle_max: int = _int("ADMISSION_LONG_IDLE_MAX", 0)
        self.admission_long_wait_s: float = _float("ADMISSION_LONG_WAIT_S", 600.0)
        # How long a NORMAL-lane caller may wait for a slot before it is
        # refused (`llm_admission_rejections_total{reason="timeout"}`), and
        # how deep either lane's waiting line may grow before a newcomer is
        # refused at once (`reason="capacity"`) instead of joining it.
        self.admission_normal_wait_s: float = _float("ADMISSION_NORMAL_WAIT_S", 600.0)
        self.admission_max_waiting: int = _int("ADMISSION_MAX_WAITING", 100)
        self.schema_cache_ttl: float = _float("SCHEMA_CACHE_TTL", 300.0)
        # §8 /health: per-dependency probe timeout — short so /health answers
        # quickly even when every vLLM service is down.
        self.health_probe_timeout: float = _float("HEALTH_PROBE_TIMEOUT", 2.0)

        # --- Typed model/runtime capabilities ------------------------------
        # These defaults reproduce the current DGX/vLLM deployment. Platform
        # profiles may override every capability with <ROLE>_* environment
        # variables without teaching the request layer about runtime names.
        _qwen_chat = dict(
            provider="local",
            backend="vllm",
            supports_chat=True,
            supports_streaming=True,
            supports_reasoning=True,
            reasoning_field=ReasoningField.AUTO,
            supports_tool_calling=True,
            supports_vision=True,
            supports_tokenization=True,
            output_limit=self.model_max_output,
            concurrency=1,
            extra_body_arguments=("chat_template_kwargs",),
        )
        main_capabilities = capabilities_from_env(
            ModelRole.MAIN,
            self.llm_model,
            CapabilityDefaults(context_length=self.model_max_context, **_qwen_chat),
        )
        router_capabilities = capabilities_from_env(
            ModelRole.ROUTER,
            self.router_model,
            CapabilityDefaults(context_length=49_152, **_qwen_chat),
        )
        agent_capabilities = capabilities_from_env(
            ModelRole.AGENT,
            self.agent_model,
            CapabilityDefaults(context_length=49_152, **_qwen_chat),
        )
        vision_capabilities = capabilities_from_env(
            ModelRole.VISION,
            self.vision_model,
            CapabilityDefaults(context_length=self.model_max_context, **_qwen_chat),
        )
        embed_capabilities = capabilities_from_env(
            ModelRole.EMBED,
            self.embed_model,
            CapabilityDefaults(
                provider="local",
                backend="vllm",
                supports_embeddings=True,
                supports_tokenization=True,
                context_length=4096,
                concurrency=1,
            ),
        )
        ocr_capabilities = capabilities_from_env(
            ModelRole.OCR,
            self.ocr_model,
            CapabilityDefaults(
                provider="local",
                backend="vllm",
                enabled=self.ocr_enabled,
                supports_chat=True,
                supports_vision=True,
                supports_ocr=True,
                supports_tokenization=True,
                context_length=8192,
                output_limit=6000,
                concurrency=3,
            ),
            enabled_override=self.ocr_enabled,
        )
        reranker_capabilities = capabilities_from_env(
            ModelRole.RERANKER,
            self.rerank_model,
            CapabilityDefaults(
                provider=(
                    "transformers"
                    if self.rerank_backend is RerankerBackend.INPROCESS
                    else "local"
                ),
                backend=self.rerank_backend.value,
                enabled=self.rerank_enabled,
                supports_reranking=self.rerank_enabled,
                supports_tokenization=self.rerank_enabled,
                context_length=8192,
                concurrency=1,
            ),
            enabled_override=self.rerank_enabled,
            backend_override=self.rerank_backend.value,
        )
        self.model_capabilities = ModelCapabilityRegistry(
            main=main_capabilities,
            router=router_capabilities,
            agent=agent_capabilities,
            vision=vision_capabilities,
            embed=embed_capabilities,
            ocr=ocr_capabilities,
            reranker=reranker_capabilities,
        )
        self.capabilities = self.model_capabilities
        # Named aliases keep call sites readable and are convenient to
        # monkeypatch in focused tests.
        self.main_capabilities = main_capabilities
        self.router_capabilities = router_capabilities
        self.agent_capabilities = agent_capabilities
        self.vision_capabilities = vision_capabilities
        self.embed_capabilities = embed_capabilities
        self.ocr_capabilities = ocr_capabilities
        self.reranker_capabilities = reranker_capabilities


settings = Settings()
