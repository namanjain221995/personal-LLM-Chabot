/**
 * Shared types for the TechSara frontend.
 * The SSE `meta` shape mirrors §10 of the master spec EXACTLY; V2-DESIGN §2
 * extends it backward-compatibly (route "chat"/"agent", mode/model/effort
 * keys, reasoning + step events). Unknown future meta keys must be tolerated.
 */
import type { ErrorCategory } from './errorTypes';


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
  | 'crawl'
  | 'report'
  | 'chat'
  | 'agent'
  | 'search'
  | 'deep_research'
  | 'url'
  | 'repo'
  | 'clarify'
  // 2026-09-09: a video attached to the chat — transcribed, read off the
  // screen, summarised, then answered with [m:ss] citations.
  | 'video'
  // 2026-09-11: Artifact Studio — the turn produced a real file and the
  // final meta carries `artifacts[]` (docs/artifact-studio/CONTRACT.md §7).
  // In the union, not beside it: `Record<Engine, …>` tables (EngineBadge,
  // fixtures) exist so the compiler makes every route account for itself.
  | 'artifact';

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

/* ------------------------------------------------ Artifact Studio (2026-09-11)
 *
 * Mirrors orchestrator/app/artifacts/types.py — JOB_STATUSES, FileRef.to_json
 * and ArtifactRef.to_json — and docs/artifact-studio/CONTRACT.md §7. The
 * reference is deliberately SMALL: it rides on every history load (the
 * localStorage quota incident, lib/history.ts), so it carries ids, names,
 * sizes and relative API paths, never bytes or page images. Every URL is a
 * path the orchestrator built from ids; the browser prefixes `/api` and
 * nothing else (lib/artifacts.ts).
 */

/** A job's coarse state. The last four are terminal. */
export type ArtifactStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'completed_with_warnings'
  | 'failed'
  | 'cancelled';

/** What a person asked for, at the level that picks the renderer set. */
export type ArtifactKind = 'document' | 'presentation' | 'workbook';

/**
 * A format the studio can actually write and reopen. Nothing else is
 * promised. `csv` since 2026-09-12 (CONTRACT-2 §1); a ZIP is a ROUTE
 * (`download_all_url`), never a file format.
 */
export type ArtifactFormat = 'pdf' | 'docx' | 'pptx' | 'xlsx' | 'csv';

/**
 * Where one file stands in its version (CONTRACT-2 §2): `primary` is the
 * kind's native file (docx/pptx/xlsx), `companion` the same content in
 * another format (the PDF of a deck, the Word/PDF of a workbook), `data` a
 * CSV of one sheet.
 */
export type ArtifactFileRole = 'primary' | 'companion' | 'data';

/**
 * One rendered file of one version.
 *
 * Two generations of this shape are on the wire at once. Refs persisted
 * before 2026-09-12 carry only the fields below `size`… `inline_url` and
 * their URLs point at `/file/{format}`; refs minted since (CONTRACT-2 §2)
 * also carry `file_id`, `role`, `title`, `rows`, `columns`, `preview_url`
 * and point at `/f/{file_id}`. Everything new is optional so the old rows
 * still type-check, and lib/artifacts.ts derives a key and a URL for both.
 */
export interface ArtifactFile {
  /**
   * 16 hex characters, minted by the pipeline from (artifact, version, role,
   * format, sheet) — the file's identity and the React key. Absent on refs
   * persisted before it existed.
   */
  file_id?: string;
  role?: ArtifactFileRole | string;
  format: ArtifactFormat | string;
  /** `quarterly-review-v2.pptx` — the download name, never an identity. */
  filename: string;
  /** The artifact title; for a per-sheet CSV, "<title> — <sheet name>". */
  title?: string;
  mime_type: string;
  size: number;
  sha256?: string;
  /** Counted by reopening the rendered file; `null` when the format has no such thing. */
  pages?: number | null;
  slides?: number | null;
  sheets?: number | null;
  /** DATA rows, the header excluded. */
  rows?: number | null;
  /** The header's width. */
  columns?: number | null;
  /** `/artifacts/{id}/v/{n}/f/{file_id}?disposition=attachment` (legacy: `/file/{format}`). */
  download_url: string;
  /** Same file, `Content-Disposition: inline`. */
  inline_url: string;
  /**
   * `/artifacts/{id}/v/{n}/preview` (pages) or `…/grid?file={file_id}`
   * (xlsx, csv); '' when this file has no preview. Absent on legacy refs,
   * where the version-level `preview_kind` decides.
   */
  preview_url?: string;
}

/** One version of one artifact, as the chat meta and the API describe it. */
export interface ArtifactRef {
  artifact_id: string;
  version: number;
  job_id: string;
  title: string;
  kind: ArtifactKind | string;
  status: ArtifactStatus | string;
  files: ArtifactFile[];
  /** `pages` (rasterised PDF twin) · `grid` (workbook) · `none`. */
  preview_kind: 'pages' | 'grid' | 'none' | string;
  preview_pages: number;
  /** `/artifacts/{id}/v/{n}/preview` or `.../sheets`, '' when none. */
  preview_url: string;
  /** `/artifacts/{id}/v/{n}/preview/1.png?w=240`, '' when no pages. */
  thumbnail_url: string;
  warnings: string[];
  created_at: string;
  operation: 'create' | 'edit' | 'convert' | string;
  parent_version?: number;
  /** `/artifacts/jobs/{job_id}` — polled while `status` is not terminal. */
  status_url: string;
  /**
   * `/artifacts/{id}/v/{n}/zip` — every file of the version in one archive.
   * Present only when the version has two or more files (CONTRACT-2 §2);
   * absent on refs persisted before the route existed.
   */
  download_all_url?: string;
  /** How many files the ZIP holds, when `download_all_url` is present. */
  package?: { count: number };
}

/** GET /artifacts/jobs/{job_id} (docs/artifact-studio/API.md). */
export interface ArtifactJob {
  job_id: string;
  artifact_id: string;
  version: number;
  status: ArtifactStatus | string;
  /** intent · gather · outline · compose · render · validate · preview */
  stage?: string | null;
  /** The stage's human title, as the server words it. */
  stage_title?: string | null;
  progress?: {
    detail?: string | null;
    elapsed_s?: number | null;
    /**
     * Per-stage outcome so far (`{stage: {status, ms, detail}}`), as the
     * pipeline records it. Not read by the browser today — the card shows the
     * CURRENT stage's title — but it is on the wire (api.py job_status) and
     * a type that omits it would misdescribe the answer.
     */
    stages?: Record<string, { status?: string; ms?: number; detail?: string }>;
  } | null;
  attempt?: number;
  failure_category?: string | null;
  /** The safe user-facing sentence — never a stack trace, DSN, host or path. */
  error?: string | null;
  /** Present when terminal and published. */
  artifact?: ArtifactRef | null;
}

/**
 * V5 (2026-07-23): a block of long text/code pasted into the composer, shown
 * as a "PASTED" chip. Stored on a user message's `meta.pasted` so it survives
 * server history, and folded into the model input at request time.
 *
 * No longer WRITTEN since 2026-09-04 — a paste stays in the textarea — but
 * every turn sent before then still carries these, so they are read, rendered
 * and folded exactly as they always were.
 */
export interface PastedText {
  id: string;
  content: string;
  lines: number;
  chars: number;
}

/**
 * 2026-09-03: an excerpt the user highlighted in an earlier message and asked
 * a follow-up about ("Ask TechSara AI").
 *
 * Rides on the user message's `meta` for exactly the reason `pasted` does: the
 * server stores meta as opaque JSON and hands it back verbatim, so the
 * reference survives a reload and can be rendered by any browser — and, like a
 * pasted block, it is folded into the model-visible text at REQUEST time
 * rather than being written into `content`. Keeping it out of `content` is
 * what makes edit and regenerate safe: they re-send the stored message, so a
 * quote living in the text would be re-wrapped and duplicated every pass.
 */
export interface SelectedContext {
  /** The excerpt, outer whitespace trimmed, internal newlines preserved. */
  text: string;
  /** The message it was taken from, as identified when it was captured. */
  messageId: string;
  sourceRole: 'user' | 'assistant';
  /** Set when the excerpt hit the length cap — the card says so. */
  truncated?: boolean;
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

/** A file attached to a user turn, as persisted in server history. */
/**
 * 2026-09-10 (upload reliability): where one attachment's BYTES are, as the
 * browser last knew it. Only the server declares `uploaded`; `interrupted`
 * is this tab's word for "I lost it before the server said so" and is
 * provisional until the session has been asked. See docs/upload-reliability.
 */
export type AttachmentUploadState =
  | 'selected'
  | 'uploading'
  | 'finalizing'
  | 'uploaded'
  | 'interrupted'
  | 'rejected'
  | 'cancelled'
  | 'expired';

export interface MessageAttachment {
  /** Server-side upload id (uploads.id) — absent on PDFs and on turns
      persisted before the upload response arrived. */
  id?: string;
  name: string;
  kind: 'dataset' | 'pdf' | 'video';
  /**
   * 2026-09-10: a browser-minted identity for THIS attachment, assigned at
   * selection and kept through persistence. Files are matched by it, never
   * by position (a list filtered by `id` moves every index under a sibling
   * still uploading) and never by name (two `invoice.pdf` in one turn is
   * legal). Absent on turns saved before this field existed.
   */
  attachment_id?: string;
  /** Bytes as declared by the picker, so a re-selected file can be checked
      against the session it resumes before a single byte is sent. */
  bytes?: number;
  /** Chunked sessions only: the server's session id (same value as `id`
      once finalised) so a reload can ask what already arrived. */
  session_id?: string;
  /** The browser's last knowledge of the bytes. Absent = uploaded, for
      rows written before this field existed (they carried `id`). */
  upload_state?: AttachmentUploadState;
}

/**
 * 2026-09-10: one logical send. Minted when Send is pressed, saved with the
 * user turn, carried on POST /chat as `intent_id`, and recorded server-side
 * in `chat_requests` at acceptance. A retry of the same turn reuses the id,
 * so it can never become a second generation; a reload reconciles `state`
 * with GET /chat/requests/{id} — the server's answer wins.
 */
export type SendIntentState =
  | 'waiting_for_attachments'
  | 'submitting'
  | 'accepted'
  | 'processing'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
  | 'unsent';

export interface SendIntent {
  id: string;
  state: SendIntentState;
  /** The server's generation for the accepted attempt, once known. */
  generation_id?: string;
  attempt?: number;
  /** Why the intent is failed/unsent, in the SAFE public sentence. */
  reason?: string;
}

/**
 * 2026-09-10: a terminal error, PERSISTED. Until now `status`/`errorMessage`
 * lived on the message object only, so an interrupted answer was saved as a
 * blank assistant turn (content '', no error) and rendered as an empty
 * bubble after a reload. This rides on meta like everything that must
 * survive a save.
 */
export interface PersistedError {
  message: string;
  code?: ErrorCategory | string;
  status?: number | null;
  /** True when the server still holds the request and can resume it. */
  resumable?: boolean;
}

/** 2026-09-09: what the video engine did, small enough to ride on meta. */
export interface VideoChapter {
  start: number;
  title: string;
}
export interface VideoActivityItem {
  analysis_id: number;
  filename: string;
  upload_id?: string | null;
  status: 'queued' | 'running' | 'done' | 'failed' | string;
  error?: string;
  duration_s: number;
  content_type?: string;
  language?: string | null;
  has_audio?: boolean | null;
  counts?: Record<string, number | null | undefined>;
  chapters?: VideoChapter[];
  stages?: Record<string, { status?: string; ms?: number | null }>;
}
export interface VideoEvidence {
  start: number;
  end: number;
  modality: 'speech' | 'screen' | 'visual' | string;
  analysis_id: number;
  text: string;
}
export interface VideoActivity {
  /** Which readers saw the frames: 'main+router' means captions came from
      the router vision model and the main model looked at frames on demand. */
  vision?: string;
  videos: VideoActivityItem[];
  evidence?: VideoEvidence[];
  frames_shown?: number[];
}

/**
 * Where this message sits in the conversation TREE (ChatGPT-style editing).
 *
 * Editing a user turn does NOT replace it. The edit is appended as an
 * alternative version alongside the original, and the two become siblings
 * under the same parent; the thread you see is one path down that tree.
 *
 * Only messages created since this feature carry the field. A message without
 * one is treated as a child of whatever physically precedes it — which is
 * exactly what a linear conversation already is — so existing threads need no
 * migration, no rewrite, and no metadata written back to them.
 *
 * `self` is a DURABLE id, deliberately not `ChatMessage.id`: reloading a
 * conversation renumbers every message to `srv-<conversation>-<index>`, so an
 * id-based pointer would dangle after the first refresh. This one rides in
 * `meta`, which the server stores as opaque JSON and hands back verbatim
 * (proven by the reasoning/attachments round-trips in history-server.test.ts).
 */
export interface BranchMeta {
  /** This message's durable identity. Survives a reload. */
  self: string;
  /** Durable id of the message it follows. Absent = start of the thread. */
  parent?: string;
}

export interface Meta {
  /**
   * Which engine answered. Optional: a message may carry meta purely to
   * record its place in the tree, before (or without) any engine running.
   */
  route?: Engine;
  /** Tree position — see BranchMeta. Absent on every pre-branching message. */
  branch?: BranchMeta;
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
  /**
   * 2026-09-11 (Artifact Studio): the files this turn produced, one ref per
   * artifact VERSION. Emitted INSTEAD of `report_files` for artifacts — their
   * ids resolve through /api/artifacts, never the flat /api/reports route.
   * Arrives on the single final meta; after a reload the cards rebuild from
   * it, and a ref whose `status` is not terminal polls its `status_url`.
   */
  artifacts?: ArtifactRef[];
  /**
   * Rows inside the full-result export in `report_files` — which is the whole
   * result, not the `data` preview. Set by the Salesforce engine whenever the
   * preview is shorter than what was retrieved.
   */
  export_rows?: number;
  /** True when even the export hit its own cap (100k rows). */
  export_truncated?: boolean;
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
  /** V5: long text/code pasted as chips. Historic turns only since 2026-09-04. */
  pasted?: PastedText[];
  /**
   * 2026-09-03: the excerpt this turn is replying to. Rendered as a quote
   * above the bubble and folded into the model input at request time.
   */
  selected_context?: SelectedContext;
  /**
   * 2026-08-21: file attachments on a user message, riding on meta for the
   * same reason `pasted` does — so they round-trip through server history and
   * ANY browser can render the file card. `id` is the server's upload_id
   * (datasets; filled in after the upload succeeds); the file itself is
   * already durable server-side, keyed by conversation.
   */
  attachments?: MessageAttachment[];
  /**
   * 2026-09-09: this turn was saved BEFORE its uploads finished (so a reload
   * keeps the words and the chips), and the chat request only goes out once
   * every upload is done. 'uploading' until that request starts, then
   * removed; 'failed' when an upload was refused. A reload mid-upload leaves
   * 'uploading' behind on a turn that never went out — which is exactly how
   * a two-video turn sat under "sent" chips with no answer and no error.
   * MessageRow reads it and says so.
   */
  send_state?: 'uploading' | 'failed';
  /**
   * 2026-09-10: the send intent this user turn carries (see SendIntent).
   * Supersedes `send_state`, which is kept readable: a row with only
   * `send_state` is treated as an intent with no id in state
   * 'waiting_for_attachments' (nothing to look up server-side).
   */
  intent?: SendIntent;
  /** 2026-09-10: on an assistant turn, the persisted terminal error. */
  error?: PersistedError;
  /** 2026-08-07: what the document engine read — shown in the Activity
      panel (filename, page count, OCR'd pages, per-page text excerpts). */
  document?: DocumentActivity;
  /** 2026-09-09: what the video engine understood and cited. */
  video?: VideoActivity;
  /** Phase 1: web-search sources for the answer's [n] citations. */
  sources?: WebSource[];
  /** Phase 1: set when search was requested but unavailable. */
  search_unavailable?: boolean;
  /** V10: facts the background extractor saved from this turn's user
      message — renders the ChatGPT-style "Memory updated" chip. */
  memory_updated?: string[];
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
  /**
   * 2026-09-08: this answer was written across MORE THAN ONE model call.
   *
   * One completion is capped at a few thousand tokens; a long answer is
   * produced by continuing across calls and stitching the seams. Present only
   * when that happened, or when the answer stopped before the model was
   * finished — an ordinary one-call reply carries nothing.
   *
   * `stop_reason` is "complete" only when the MODEL decided it was done.
   * Anything else means we stopped it, and `truncated` is then true.
   * `output_tokens` is null when no server reported usage — NOT MEASURED,
   * never zero.
   */
  continuation?: {
    segments: number;
    output_tokens: number | null;
    stop_reason: string;
    truncated: boolean;
  };
  /** Phase A/C: this session's context accounting, for the meter. */
  context?: ContextUsage;
  /** The searches behind this answer, kept so history replays the panel. */
  research?: Research;
  /**
   * 2026-09-03: sites queued for a background crawl behind a shared link —
   * the page was answered from now; the rest of the site follows quietly.
   */
  site_crawl?: SiteCrawlNotice[];
  /** Deep Research (2026-09-03): what the run established and why it stopped. */
  research_run?: ResearchRun;
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

/** A site the server queued for a background crawl (meta.site_crawl). */
export interface SiteCrawlNotice {
  host: string;
  root_url: string;
  job_id?: number;
  status: string;
}

/** What Deep Research resolved for one subquestion. */
export interface ResearchResolution {
  subquestion: string;
  /** current | historical | superseded | conflicting | unknown */
  status: string;
  value: string;
  as_of: string;
  support: number[];
  independent: number;
  primary: boolean;
  superseded: { value: string; as_of: string; sources: number[] }[];
  conflicts: { value: string; as_of: string; sources: number[] }[];
  confidence: number;
}

/** One round of a research run, as the server counted it. */
export interface ResearchRound {
  iteration: number;
  label: string;
  queries: string[];
  attempted: number;
  fetched: number;
  new_sources: number;
  duplicates: number;
  links_followed: number;
  new_claims: number;
  gain: number;
  elapsed_s: number;
}

/** meta.research_run — the Deep Research engine's account of itself. */
export interface ResearchRun {
  research_id: string;
  iterations: number;
  queries: string[];
  subquestions: string[];
  sources_found: number;
  sources_cited: number;
  missing: string[];
  contradictions: string[];
  elapsed_s: number;
  invalid_citations_removed: number;
  /** Why the loop ended: sufficient · no_information_gain · … (2026-09-03). */
  stop_reason?: string;
  today?: string;
  temporal?: string;
  rounds?: ResearchRound[];
  links_followed?: number;
  primary_sources?: number[];
  duplicates_dropped?: number;
  stale_downranked?: number;
  claims?: number;
  resolutions?: ResearchResolution[];
  confidence?: number;
  verification_rounds?: number;
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
  /**
   * A fatal request-level failure, in the only two fields the error page may
   * render. `errorStatus` is the REAL upstream status (null when the request
   * never got one); `errorCode` is its category. Absent on non-fatal errors,
   * which keep their inline treatment.
   */
  errorStatus?: number | null;
  errorCode?: ErrorCategory;
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
