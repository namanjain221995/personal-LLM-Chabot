/**
 * Translation between the frontend's internal /api/chat body and the
 * orchestrator's POST /chat contract (§10).
 *
 * The UI (components/ChatApp.tsx) posts the whole visible transcript:
 *   { messages: [{role, content}...], session_id, image? }        (internal)
 * The orchestrator (orchestrator/app/main.py ChatRequest) expects:
 *   { message: str (min_length=1), session_id, image_base64? }    (§10)
 *
 * app/api/chat/route.ts uses `toOrchestratorChatRequest` to convert the
 * former into the latter before proxying. Kept as pure functions here so the
 * contract mapping is unit-tested (tests/chat-contract.test.ts).
 */

/** Body the frontend posts to its own /api/chat endpoint. */
export interface ChatRequestBody {
  messages?: { role: string; content: string }[];
  session_id?: string;
  image?: string;
  /** V2 §1 optional fields — forwarded verbatim when present. */
  conversation_id?: string;
  mode?: string;
  model?: string;
  effort?: string;
  agent?: boolean;
  /** V8: an uploaded PDF (base64) + its filename. */
  pdf?: string;
  pdf_filename?: string;
  /** Phase 1: web search mode. */
  web_search?: string;
  deep_research?: boolean;
  /** 2026-08-06: Live Salesforce toggle — query the org, not the copy. */
  sf_live?: boolean;
  /** 2026-08-05: all attached images (max 5); `image` stays the first one. */
  images?: string[];
  /**
   * Salesforce Intelligence Mode: the answer to a pending clarifying question.
   * Forwarded VERBATIM — it is a server-issued contract (ids, an opaque resume
   * token, an idempotency key), and a proxy that reshaped any of it would break
   * the resume it exists to enable.
   */
  clarification?: Record<string, unknown>;
}

/** Body the orchestrator's POST /chat endpoint accepts (§10 + V2 §1 + V8). */
export interface OrchestratorChatRequest {
  message: string;
  /** V9: the full conversation so the model has within-chat memory. */
  messages?: { role: string; content: string }[];
  session_id: string;
  image_base64: string | null;
  conversation_id?: string;
  mode?: string;
  model?: string;
  effort?: string;
  agent?: boolean;
  pdf?: string;
  pdf_filename?: string;
  web_search?: string;
  deep_research?: boolean;
  sf_live?: boolean;
  /** 2026-08-05: all attached images; image_base64 remains the first. */
  images?: string[];
  /** Salesforce Intelligence Mode: answer to a pending clarifying question. */
  clarification?: Record<string, unknown>;
}

/**
 * Instructions used when the user sends only an attachment with no text — the
 * orchestrator requires a non-empty `message` (min_length=1).
 */
export const IMAGE_ONLY_PROMPT = 'Analyze the attached image.';
export const PDF_ONLY_PROMPT = 'Read this document and summarize the key points.';

/** Content of the most recent user turn, or '' when there is none. */
export function lastUserContent(body: ChatRequestBody): string {
  return (
    [...(body.messages ?? [])].reverse().find((m) => m.role === 'user')
      ?.content ?? ''
  );
}

/**
 * Map the internal body to the orchestrator's ChatRequest shape.
 * Returns null when the request carries neither text nor an image —
 * there is nothing valid to forward (the orchestrator would 422).
 */
export function toOrchestratorChatRequest(
  body: ChatRequestBody,
): OrchestratorChatRequest | null {
  const text = lastUserContent(body).trim();
  // 2026-08-05: `images` (max 5) wins over the single `image` spelling.
  const images = body.images?.length
    ? body.images
    : body.image
      ? [body.image]
      : [];
  const image = images[0] ?? null;
  const pdf = body.pdf ?? null;
  const message =
    text || (image ? IMAGE_ONLY_PROMPT : pdf ? PDF_ONLY_PROMPT : '');
  // A clarification answer is itself valid input even with no text of its own
  // (a "Skip" carries none), because the request it resumes supplies the
  // question. Everything else with no text and no attachment would 422.
  if (!message && !body.clarification) return null;
  return {
    message,
    // V9: forward the whole conversation so the model remembers this chat.
    ...(body.messages && body.messages.length
      ? { messages: body.messages }
      : {}),
    session_id: body.session_id ?? 'default',
    image_base64: image,
    // Only sent when there genuinely are several — single-image requests
    // keep producing the exact v1 key set.
    ...(images.length > 1 ? { images } : {}),
    // V2 §1 fields: include only when the client sent them so v1-shaped
    // requests keep producing the exact v1 key set.
    ...(body.conversation_id !== undefined
      ? { conversation_id: body.conversation_id }
      : {}),
    ...(body.mode !== undefined ? { mode: body.mode } : {}),
    ...(body.sf_live !== undefined ? { sf_live: body.sf_live } : {}),
    ...(body.model !== undefined ? { model: body.model } : {}),
    ...(body.effort !== undefined ? { effort: body.effort } : {}),
    ...(body.agent !== undefined ? { agent: body.agent } : {}),
    // V8: forward the PDF + filename when present.
    ...(pdf ? { pdf, pdf_filename: body.pdf_filename } : {}),
    // Phase 1: forward web-search mode when set.
    ...(body.web_search !== undefined ? { web_search: body.web_search } : {}),
    ...(body.deep_research !== undefined
      ? { deep_research: body.deep_research }
      : {}),
    // Salesforce Intelligence Mode: forwarded untouched when present.
    ...(body.clarification ? { clarification: body.clarification } : {}),
  };
}
