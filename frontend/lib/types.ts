/**
 * Shared types for the TechSara frontend.
 * The SSE `meta` shape mirrors §10 of the master spec EXACTLY; V2-DESIGN §2
 * extends it backward-compatibly (route "chat"/"agent", mode/model/effort
 * keys, reasoning + step events). Unknown future meta keys must be tolerated.
 */

/**
 * `clarify` was always emitted by the orchestrator (main.py) and always
 * rendered; it was simply missing from this union, so the type quietly
 * disagreed with the wire. Listed now that Salesforce Intelligence Mode emits
 * it too.
 */
export type Engine =
  | 'sql'
  | 'rag'
  | 'vision'
  | 'report'
  | 'chat'
  | 'agent'
  | 'search'
  | 'url'
  | 'repo'
  | 'clarify';

/**
 * Historically two models. There is now ONE (Qwen3.6-35B-A3B) and the picker
 * chooses EFFORT instead, so this is always "smart" in new requests — kept so
 * stored prefs and older payloads keep working.
 */
export type ModelChoice = 'smart' | 'fast';

/**
 * Four levels on ONE model. Fast and Low answer without the reasoning pass;
 * the difference is what tools they may use (Low may search, Fast may not).
 * Medium and High think first and may also plan multi-step work.
 */
export type ReasoningEffort = 'fast' | 'think' | 'max';

/** Salesforce toggle (V2 §1): "salesforce" is the v1 behavior. */
export type ChatMode = 'salesforce' | 'assistant';

/** `event: step` payload (V2 §2) — agent-mode plan/progress updates. */
export interface AgentStep {
  id: number;
  title: string;
  status: 'running' | 'done' | 'failed';
  detail?: string;
}

/**
 * Mirrors app/core/chart_spec.py ChartType. The first five are the original
 * set; every persisted conversation uses one of them.
 */
export type ChartType =
  | 'bar'
  | 'line'
  | 'area'
  | 'pie'
  | 'scatter'
  | 'horizontal_bar'
  | 'donut'
  | 'funnel'
  | 'histogram';

/**
 * The `meta.chart` payload. The five required fields are unchanged and
 * always present; the optional ones are emitted by the backend only when
 * they differ from their defaults (see ChartSpec.wire_dump), so an old
 * payload is a valid new payload.
 */
export interface ChartSpec {
  type: ChartType;
  x_key: string;
  y_keys: string[];
  title: string;
  stacked: boolean;
  /** Histogram bin count, chosen by trusted backend code. */
  bins?: number;
  show_legend?: boolean;
  show_values?: boolean;
}

export interface Citation {
  record_id: string;
  object: string;
  url: string;
}

export interface ReportFile {
  filename: string;
  type: string;
  size?: number;
}

export type DataRow = Record<string, unknown>;

/**
 * V5 (2026-07-23): a block of long text/code pasted into the composer, shown
 * as a "PASTED" chip. Stored on a user message's `meta.pasted` so it survives
 * server history, and folded into the model input at request time.
 */
export interface PastedText {
  id: string;
  content: string;
  lines: number;
  chars: number;
}

/**
 * `event: meta` payload — single final JSON before `done` (§10).
 * V2 (§2) adds mode / model / effort / steps; the frontend also persists the
 * client-captured reasoning stream here (meta.reasoning, §4d) so it survives
 * the server-side history round-trip. Unknown future keys pass through JSON
 * untouched — nothing here may throw on extras.
 */
export interface DocumentActivity {
  filename: string;
  total_pages: number;
  ocr_pages?: number;
  pages: { page: number; text: string }[];
}

export interface Meta {
  route: Engine;
  sql?: string;
  data?: DataRow[];
  truncated?: boolean;
  chart?: ChartSpec;
  /**
   * Rows the chart draws, when they are not `data` verbatim — histogram
   * bins, or a funnel in trusted stage order. Absent (and absent from
   * every conversation persisted before this key existed) means "draw
   * `data`", which is what the renderer falls back to.
   */
  chart_data?: DataRow[];
  citations?: Citation[];
  report_files?: ReportFile[];
  /** V2 §2: request mode ("salesforce" | "assistant"). */
  mode?: string;
  /** V2 §2: served model id. */
  model?: string;
  /** V2 §2: reasoning effort used. */
  effort?: string;
  /** V2 §3b: agent plan steps (final statuses). */
  steps?: AgentStep[];
  /** V2 §4d: full reasoning text, stored client-side for history. */
  reasoning?: string;
  /** V2 §4d: client-measured "Thought for N s". */
  reasoning_seconds?: number;
  /** V5: long text/code the user pasted as chips on a user message. */
  pasted?: PastedText[];
  /** 2026-08-07: what the document engine read — shown in the Activity
      panel (filename, page count, OCR'd pages, per-page text excerpts). */
  document?: DocumentActivity;
  /** Phase 1: web-search sources for the answer's [n] citations. */
  sources?: WebSource[];
  /** Phase 1: set when search was requested but unavailable. */
  search_unavailable?: boolean;
  /** Phase 3: cited code excerpts (path:Lstart-Lend + snippet). */
  code_sources?: CodeSource[];
  /**
   * Idempotency key for the generation that produced this answer. Sent back
   * when persisting so a reply watched by two attached clients is stored
   * exactly once (the server dedupes appends carrying a known id).
   */
  generation_id?: string;
  /**
   * Set when the prompt had to be shortened to fit the model's window —
   * old turns dropped and/or an oversized message clipped. Surfaced inline so
   * a user who pasted a large document knows part of it was not sent.
   */
  input_trimmed?: { dropped_turns: number; clipped_messages: number };
  /** Phase A/C: this session's context accounting, for the meter. */
  context?: ContextUsage;
  /** The searches behind this answer, kept so history replays the panel. */
  research?: Research;
  /**
   * The question had more than one honest reading, so the answer is a question
   * back. Answering it RESUMES the original request server-side rather than
   * sending a rewritten question as a new message — see lib/clarification.ts.
   *
   * This is the ONLY clarification payload. A second, untyped `clarify` shape
   * used to ride alongside it, rendered by a different component, with no
   * resume token and no server-side state; a conversation persisted while that
   * was live shows its question as ordinary assistant prose, which is what it
   * always was underneath.
   */
  clarification?: import('./clarification').ClarificationRequest;
  /** Which planner decided: "intelligence" (the model) or "deterministic". */
  salesforce_mode?: string;
  /** Provenance for a Salesforce-derived answer, shown under the message. */
  salesforce_sources?: SalesforceSources;
  /** The resolved scope (period, owner, region…) the answer was computed over. */
  salesforce_scope?: string;
  /** Set when a lookup FAILED — distinct from an empty result. */
  salesforce_error?: string;
  /** Stated assumptions, when a detail was assumed rather than asked about. */
  assumptions?: string[];
  /** Final phase snapshot, so a reopened chat shows how the answer was reached. */
  status?: import('./phases').PhaseStatus;
}

/** Where a Salesforce answer's numbers came from. */
export interface SalesforceSources {
  /** "live" (the org) or "synced" (the local copy). */
  source: string;
  /** Object API names / business domains read. */
  objects: string[];
  /** Records that MATCHED, which is not always how many were returned. */
  record_count?: number;
  query_timestamp?: string;
  freshness?: string;
  pages?: number;
  truncated?: boolean;
}

export interface ContextUsage {
  /** Prompt tokens this request actually used. */
  tokens_used: number;
  /** window − reserved output − safety margin. */
  usable_budget: number;
  window: number;
  reserved_output: number;
  /** tokens_used / usable_budget, 0-1+. */
  fraction: number;
  /** Turns folded into the rolling summary so far. */
  summarized_turns: number;
  /** Present when THIS request triggered a compaction. */
  compacted?: { folded_turns: number };
}

/** Phase 3: a cited code excerpt from an indexed repo. */
export interface CodeSource {
  path: string;
  start_line: number;
  end_line: number;
  snippet: string;
}

/** Phase 1: a cited web source. */
export interface WebSource {
  n: number;
  title: string;
  url: string;
  domain: string;
}

/** One result a search returned, before anything was read. */
export interface ResearchResult {
  title: string;
  url: string;
  domain: string;
}

/** One search the model ran, with what it turned up. */
export interface ResearchQuery {
  query: string;
  results: ResearchResult[];
}

/**
 * Live research progress — the searches behind an answer, shown while they
 * happen and kept on the message afterwards so reopening a chat replays them.
 * `elapsedMs` is measured client-side, like reasoningSeconds.
 */
export interface Research {
  queries: ResearchQuery[];
  /** Sources queued for reading (set when the fetch phase starts). */
  reading?: number;
  /** Sources actually read (set when the fetch phase finishes). */
  read?: number;
  /** Wall-clock from the first search to the last, in ms. */
  elapsedMs?: number;
  /** True while searches are still arriving. */
  active?: boolean;
}

export type MessageStatus = 'streaming' | 'done' | 'stopped' | 'error';

export interface ChatMessage {
  id: string;
  /**
   * The SERVER's row id, once this message has been stored. Distinct from
   * `id`, which is client-side and unstable: a live message gets a random
   * uuid and the same message rehydrated from the server gets a positional
   * `srv-<conversation>-<index>`. Anything that must outlive a reload —
   * thumbs — has to key off this instead.
   */
  serverId?: number;
  /** Thumbs, stored server-side. Undefined means "not loaded from the
   *  server yet"; null means "explicitly no opinion". */
  feedback?: 'up' | 'down' | null;
  role: 'user' | 'assistant';
  content: string;
  meta?: Meta;
  status?: MessageStatus;
  /** Populated when status === 'error' — the exact `error` event message. */
  errorMessage?: string;
  /** data: URL preview for a user-attached image. */
  imageDataUrl?: string;
  /** 2026-08-05: previews when SEVERAL images were attached (max 5);
      `imageDataUrl` stays as the single-image/legacy spelling. */
  imageDataUrls?: string[];
  /** V8: filename of a user-attached PDF (shown as a chip in the bubble). */
  pdfName?: string;
  /** Live reasoning stream (V2 §4d) — folded into meta.reasoning on finish. */
  reasoning?: string;
  /** Client-measured thinking duration in whole seconds (V2 §4d). */
  reasoningSeconds?: number;
  /** Live agent step timeline (V2 §4e) — folded into meta.steps on finish. */
  steps?: AgentStep[];
  /** Phase 1: transient web-search progress line ("Reading N sources…"). */
  searchStatus?: string;
  /**
   * Live Salesforce phase (understanding → querying → verifying → …). Drives
   * the ReasoningStar and is folded onto meta.status when the answer finishes,
   * so reopening the chat shows the phase it ended on rather than a blank row.
   */
  phaseStatus?: import('./phases').PhaseStatus;
  /** Live research progress — folded into meta.research on finish. */
  research?: Research;
  createdAt: number;
}

export interface Conversation {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  /** V3 §2: pinned chats float to the top of the sidebar. */
  pinned?: boolean;
  /** V3 §2: archived chats leave Recents for the Archived disclosure. */
  archived?: boolean;
  messages: ChatMessage[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  /** V3 §2 — always populated by the store; optional for older payloads. */
  pinned?: boolean;
  archived?: boolean;
}
