"""Central configuration for the orchestrator service (spec §6, orchestrator env vars).

Every value comes from an environment variable with a sensible default for the
local docker-compose network. Reading configuration never touches the network,
the GPU, or any model runtime.
"""
from __future__ import annotations

import os

from .core.exports import EXPORT_ROW_CAP, PREVIEW_ROW_CAP

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


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


class Settings:
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

        # --- Reranker (lazy transformers import; needs torch → base image only) ---
        self.rerank_enabled: bool = _bool("RERANK_ENABLED", True)
        # §6: docker-compose sets RERANKER_MODEL=Qwen/Qwen3-Reranker-0.6B.
        # RERANK_MODEL is kept as a secondary fallback for local overrides.
        self.rerank_model: str = (
            os.environ.get("RERANKER_MODEL")
            or os.environ.get("RERANK_MODEL")
            or "Qwen/Qwen3-Reranker-0.6B"
        )

        # --- Data stores (defaults match §6/§7 and what the sync-worker writes) ---
        self.duckdb_path: str = os.environ.get("DUCKDB_PATH", "/data/warehouse.duckdb")
        self.lancedb_dir: str = os.environ.get("LANCEDB_DIR", "/data/lancedb")
        self.lancedb_table: str = os.environ.get("LANCEDB_TABLE", "chunks")
        self.parquet_dir: str = os.environ.get("PARQUET_DIR", "/data/parquet")
        self.reports_dir: str = os.environ.get("REPORTS_DIR", "/reports")

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
        self.sf_live_timeout: float = float(os.environ.get("SF_LIVE_TIMEOUT", "45"))
        self.sf_live_enabled: bool = os.environ.get(
            "SF_LIVE_ENABLED", "true"
        ).lower() not in ("0", "false", "no")
        self.model_max_context: int = _int("MODEL_MAX_CONTEXT", 262144)
        self.model_max_output: int = _int("MODEL_MAX_OUTPUT", 8192)
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
        self.keep_recent_turns: int = _int("KEEP_RECENT_TURNS", 8)
        self.summary_max_tokens: int = _int("SUMMARY_MAX_TOKENS", 2000)
        # An answer this short is a failure for a thinking model (it burns the
        # budget reasoning and emits nothing), so input is trimmed further
        # rather than letting the output floor drop below this.
        self.min_output_floor: int = _int("MIN_OUTPUT_FLOOR", 1024)

        # --- Phase B: semantic recall over compacted turns.
        self.semantic_recall_enabled: bool = _bool("SEMANTIC_RECALL_ENABLED", True)
        self.retrieve_top_k: int = _int("RETRIEVE_TOP_K", 6)

        # --- Phase C: context meter.
        self.context_meter_enabled: bool = _bool("CONTEXT_METER_ENABLED", True)

        # --- Phase 4: ZIP & dataset uploads. Files are PROFILED, never
        # executed and never sent to the model raw.
        self.dataset_uploads_enabled: bool = _bool("DATASET_UPLOADS_ENABLED", True)
        self.upload_max_mb: int = _int("UPLOAD_MAX_MB", 200)
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

        # --- Row caps (§8) ---
        self.sql_preview_row_cap: int = _int("SQL_PREVIEW_ROW_CAP", PREVIEW_ROW_CAP)  # 500
        self.export_row_cap: int = _int("EXPORT_ROW_CAP", EXPORT_ROW_CAP)  # 100_000

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

        # --- V2 app state (V2-DESIGN §3c): auth + server-side history live in
        # SQLite (stdlib sqlite3, WAL). This is APP state only — the analytics
        # data plane stays DuckDB/LanceDB/Parquet per SPEC §4.
        self.app_db_path: str = os.environ.get("APP_DB_PATH", "/data/app.sqlite3")
        # Session-cookie signing secret: SESSION_SECRET env wins; otherwise a
        # secret is generated once and persisted here (chmod 600).
        self.session_secret_file: str = os.environ.get(
            "SESSION_SECRET_FILE", "/data/.session_secret"
        )

        # --- Misc ---
        self.session_max_turns: int = _int("SESSION_MAX_TURNS", 20)
        self.llm_request_timeout: float = _float("LLM_REQUEST_TIMEOUT", 300.0)
        self.schema_cache_ttl: float = _float("SCHEMA_CACHE_TTL", 300.0)
        # §8 /health: per-dependency probe timeout — short so /health answers
        # quickly even when every vLLM service is down.
        self.health_probe_timeout: float = _float("HEALTH_PROBE_TIMEOUT", 2.0)


settings = Settings()
