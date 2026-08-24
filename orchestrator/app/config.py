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


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


#: Valid CHART_TRIGGER_MODE values. `automatic` is intentionally not one.
CHART_TRIGGER_MODES = ("explicit", "hybrid")


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
        # Unlimited-OCR (2026-08-06): baidu's 3.3B document-OCR VLM on its own
        # vLLM service. Uploaded images and rendered PDF pages are transcribed
        # here FIRST; the transcript rides with the pixels to the main model.
        # Disabled or unreachable → the pipeline runs pixels-only as before.
        self.ocr_enabled: bool = _bool("OCR_ENABLED", True)
        self.ocr_base_url: str = os.environ.get(
            "OCR_BASE_URL", "http://vllm-ocr:30004/v1"
        ).rstrip("/")
        self.ocr_model: str = os.environ.get("OCR_MODEL", "baidu/Unlimited-OCR")

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

        # --- Data stores (defaults match §6/§7 and what the sync-worker writes) ---
        self.duckdb_path: str = os.environ.get("DUCKDB_PATH", "/data/warehouse.duckdb")
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

        # --- Misc ---
        self.session_max_turns: int = _int("SESSION_MAX_TURNS", 20)
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
