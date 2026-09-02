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
  /** 2026-09-02: documents that streamed to /api/upload; sent by reference. */
  pdf_uploads?: { upload_id: string; name: string }[];
  /** Phase 1: web search mode. */
  web_search?: string;
  deep_research?: boolean;
  /** 2026-08-06: Live Salesforce toggle — query the org, not the copy. */
  sf_live?: boolean;
  /** 2026-08-05: all attached images (max 5); `image` stays the first one. */
  images?: string[];
  /**
   * NEW-14: the turn being sent has an uploaded dataset (.csv/.xlsx/.zip/…).
   *
   * INTERNAL to the frontend and its proxy — deliberately not forwarded, and
   * deliberately not a new orchestrator ChatRequest field. A dataset does not
   * ride inside the chat body at all: it streams to /api/upload first and the
   * orchestrator finds it again through `conversation_id` → `get_uploads`.
   * The one thing that translation cannot recover on its own is whether a
   * TEXTLESS turn was an empty send (which must stay a 400) or a dataset send
   * (which must get a prompt), so that single bit is stated here.
   */
  dataset?: boolean;
  /**
   * NEW-14: what the turn being sent RIGHT NOW says, folded exactly as it is
   * in `messages` — `''` when it carried only attachments.
   *
   * Stated rather than inferred, because it cannot be recovered from
   * `messages`: that array has empty-content turns filtered out of it, so an
   * attachment-only send leaves no trace there at all. Inferring it from the
   * shape of the tail very nearly works and then does not — an assistant turn
   * can be empty too (a generation stopped before its first token, or the
   * failed turn THIS bug produces), and the transcript then ends on the
   * previous question, which is the one thing that must never be re-sent.
   *
   * Internal to the frontend and its proxy; never forwarded.
   */
  current_text?: string;
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
  pdf_uploads?: { upload_id: string; name: string }[];
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
/**
 * NEW-14: what to ask when a dataset arrives with no question attached to it.
 *
 * A dataset never travels in the chat body — only a reference to it does — so
 * unlike an image or a PDF there was nothing here to substitute, and a
 * spreadsheet dropped in without a prompt produced no message at all. The
 * orchestrator requires a non-empty one, and gates the dataset route itself on
 * `request.text` being non-empty, so an empty send failed twice over.
 *
 * Worded as a real question rather than a marker: it is what the model is
 * asked, it can appear in a transcript, and it has to route to the dataset
 * engine on its own merits.
 */
export const DATASET_ONLY_PROMPT =
  'Analyze the uploaded dataset and summarize the key findings.';

/** Content of the most recent user turn, or '' when there is none. */
export function lastUserContent(body: ChatRequestBody): string {
  return (
    [...(body.messages ?? [])].reverse().find((m) => m.role === 'user')
      ?.content ?? ''
  );
}

/**
 * What the CURRENT turn — the one being sent right now — actually says.
 *
 * NEW-14, second root cause. This is not `lastUserContent`, and the difference
 * is the whole bug. `startStream` posts the visible thread ENDING at the turn
 * being sent, and drops empty-content messages on the way out, so an
 * attachment-only turn leaves the transcript ending on an ASSISTANT message.
 * "The newest user turn with text in it" then walks straight past the send in
 * progress and finds the PREVIOUS question — which is how attaching a
 * spreadsheet to a chat that once asked "What is Python?" re-answered "What is
 * Python?" with the spreadsheet silently in scope. A confidently wrong answer
 * is a worse failure than the 400 it replaced.
 *
 * So the sender STATES it. `current_text` is authoritative whenever it is
 * present — including when it is empty, which is the entire point: `''` means
 * "this turn said nothing", and only the attachment fallbacks below may answer
 * for it.
 *
 * The positional read is the fallback for bodies that predate the field. It is
 * deliberately not the primary rule: it infers "the current turn was wordless"
 * from the transcript ending on an assistant turn, and an assistant turn can
 * be empty too — a generation stopped before its first token, or the failed
 * turn this very bug produces — in which case the tail is the PREVIOUS
 * question and the inference silently inverts. Never widened to a search.
 */
export function currentUserContent(body: ChatRequestBody): string {
  if (body.current_text !== undefined) return body.current_text.trim();
  const messages = body.messages ?? [];
  const last = messages[messages.length - 1];
  return last?.role === 'user' ? (last.content ?? '').trim() : '';
}

/**
 * Map the internal body to the orchestrator's ChatRequest shape.
 * Returns null when the request carries neither text nor an attachment —
 * there is nothing valid to forward (the orchestrator would 422).
 */
export function toOrchestratorChatRequest(
  body: ChatRequestBody,
): OrchestratorChatRequest | null {
  // The turn being SENT, never the newest one that happens to have text —
  // see currentUserContent. `.trim()` lives in there so whitespace cannot
  // masquerade as a question and rob an attachment of its fallback.
  const text = currentUserContent(body);
  // 2026-08-05: `images` (max 5) wins over the single `image` spelling.
  const images = body.images?.length
    ? body.images
    : body.image
      ? [body.image]
      : [];
  const image = images[0] ?? null;
  const pdf = body.pdf ?? null;
  const pdfUploads = body.pdf_uploads?.length ? body.pdf_uploads : null;
  // Ordering is unchanged and deliberate: the kinds are mutually exclusive at
  // the composer, and where they are not, the payload that actually travels
  // inside this request outranks the one that only left a reference behind.
  const message =
    text ||
    (image
      ? IMAGE_ONLY_PROMPT
      : pdf || pdfUploads
        ? PDF_ONLY_PROMPT
        : // NEW-14: the dataset itself is already on the server; all this turn
          // needs is a question to ask about it.
          body.dataset
          ? DATASET_ONLY_PROMPT
          : '');
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
    ...(pdfUploads ? { pdf_uploads: pdfUploads } : {}),
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
