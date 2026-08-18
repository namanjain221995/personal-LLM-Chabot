/**
 * Salesforce Intelligence Mode — the client half of the clarification contract.
 *
 * Mirrors `orchestrator/app/core/sf_intel/models.py`. The two files are ONE
 * contract and must change together; the runtime validators below are what stop
 * a drift between them from rendering a broken card instead of failing loudly.
 *
 * Everything here is pure. The card component (`ClarificationCard.tsx`) owns
 * pixels and focus; selection, keyboard mapping, submission identity and the
 * "is this pending question still mine?" rules live here so they are unit
 * tested (tests/clarification.test.ts) rather than click-tested.
 */

import type { ChatMessage } from './types';

/** Slots the server may ask about. Kept in sync with SLOTS in models.py. */
export const CLARIFICATION_SLOTS = [
  'object',
  'record_identity',
  'metric',
  'date_range',
  'owner_scope',
  'region',
  'status',
  'comparison_baseline',
  'grouping',
  'result_format',
  'filter',
] as const;

export type ClarificationSlot = (typeof CLARIFICATION_SLOTS)[number];

export type ClarificationState = 'pending' | 'answered' | 'skipped' | 'cancelled';

export interface ClarificationOption {
  id: string;
  label: string;
  description?: string;
  value?: string;
  metadata?: Record<string, string>;
}

export interface ClarificationRequest {
  clarification_id: string;
  conversation_id: string;
  run_id: string;
  root_user_message_id: string;
  intent_id: string;
  source: 'salesforce';
  header: string;
  question: string;
  slot: string;
  options: ClarificationOption[];
  allow_custom: boolean;
  custom_placeholder: string;
  multi_select: boolean;
  round_number: number;
  created_at: string;
  state: ClarificationState;
  resume_token: string;
  question_fingerprint: string;
}

/** What the client posts back, alongside the chat message. */
export interface ClarificationResponse {
  clarification_id: string;
  conversation_id: string;
  client_message_id: string;
  selected_option_ids: string[];
  custom_text: string;
  skipped: boolean;
  resume_token: string;
}

const MAX_OPTIONS = 4;

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function asOption(raw: unknown): ClarificationOption | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const id = asString(o.id).trim();
  const label = asString(o.label).trim();
  if (!id || !label) return null;
  const metadata =
    o.metadata && typeof o.metadata === 'object' && !Array.isArray(o.metadata)
      ? Object.fromEntries(
          Object.entries(o.metadata as Record<string, unknown>)
            .filter(([, v]) => typeof v === 'string')
            .map(([k, v]) => [k, v as string]),
        )
      : undefined;
  return {
    id,
    label,
    description: asString(o.description) || undefined,
    value: asString(o.value) || label,
    ...(metadata && Object.keys(metadata).length ? { metadata } : {}),
  };
}

/**
 * Validate a `meta.clarification` payload from the server.
 *
 * Returns null rather than throwing, and null means "render nothing". A card
 * built from a half-understood payload is worse than no card: its options would
 * submit ids the server never offered, and every one of those is rejected — so
 * the user would click, wait, and be told their answer was invalid.
 */
export function parseClarification(raw: unknown): ClarificationRequest | null {
  if (!raw || typeof raw !== 'object') return null;
  const c = raw as Record<string, unknown>;
  const clarificationId = asString(c.clarification_id).trim();
  const question = asString(c.question).trim();
  const resumeToken = asString(c.resume_token).trim();
  if (!clarificationId || !question || !resumeToken) return null;

  const options = Array.isArray(c.options)
    ? c.options.map(asOption).filter((o): o is ClarificationOption => o !== null)
    : [];
  // Fewer than two options is not a choice; more than four is not a card. Both
  // are server bugs, and neither should reach a user as a broken control.
  if (options.length < 2) return null;

  const seen = new Set<string>();
  const unique = options.filter((o) => {
    if (seen.has(o.id)) return false;
    seen.add(o.id);
    return true;
  });
  if (unique.length < 2) return null;

  const state = asString(c.state, 'pending') as ClarificationState;
  return {
    clarification_id: clarificationId,
    conversation_id: asString(c.conversation_id),
    run_id: asString(c.run_id),
    root_user_message_id: asString(c.root_user_message_id),
    intent_id: asString(c.intent_id),
    source: 'salesforce',
    header: asString(c.header, 'Salesforce') || 'Salesforce',
    question,
    slot: asString(c.slot, 'filter'),
    options: unique.slice(0, MAX_OPTIONS),
    allow_custom: c.allow_custom !== false,
    custom_placeholder:
      asString(c.custom_placeholder) || 'Tell me what you meant…',
    multi_select: c.multi_select === true,
    round_number: typeof c.round_number === 'number' ? c.round_number : 1,
    created_at: asString(c.created_at),
    state:
      state === 'answered' || state === 'skipped' || state === 'cancelled'
        ? state
        : 'pending',
    resume_token: resumeToken,
    question_fingerprint: asString(c.question_fingerprint),
  };
}

/**
 * The pending question for a thread, or null.
 *
 * Read from the LAST assistant message only. An older card in the same thread
 * has already been answered — the answer is the turn after it — and re-offering
 * it would let a user resume an intent the conversation has moved past.
 */
export function pendingClarification(
  messages: readonly ChatMessage[],
): ClarificationRequest | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.role !== 'assistant') {
      // A user turn after the card means it has been responded to.
      return null;
    }
    const parsed = parseClarification(message.meta?.clarification);
    if (parsed) return parsed.state === 'pending' ? parsed : null;
    if (message.content || message.meta) return null;
  }
  return null;
}

/**
 * How a card at `index` in the thread should render.
 *
 * A card is LIVE only while it is the question the thread is waiting on. Every
 * older one is history: it has already been answered, and the answer is the
 * turn after it.
 *
 * Without this the transcript was full of live controls. `MessageRow` rendered
 * a card for any message carrying `meta.clarification`, and the parent's
 * "answer in flight" lock is keyed by id and clears when the run finishes — so
 * once an answer came back, every card in the conversation was clickable
 * again, and clicking a different option on a question from ten turns ago
 * started a whole new run against an intent the chat had long moved past.
 */
export function cardState(
  messages: readonly ChatMessage[],
  index: number,
): { pending: boolean; answeredWith: string } {
  const request = parseClarification(messages[index]?.meta?.clarification);
  if (!request) return { pending: false, answeredWith: '' };
  // Defined in terms of `pendingClarification` rather than re-deriving
  // "is this the last one": two answers to that question that could disagree
  // is how a card ends up locked in one place and live in another.
  const active = pendingClarification(messages);
  if (active?.clarification_id === request.clarification_id) {
    return { pending: true, answeredWith: '' };
  }
  const reply = messages.slice(index + 1).find((m) => m.role === 'user');
  return { pending: false, answeredWith: (reply?.content ?? '').trim() };
}

/** Keyboard hints: 1..N for the options, then N+1 for "Something else". */
export function optionShortcut(index: number): string | null {
  return index < 9 ? String(index + 1) : null;
}

export type CardAction =
  /** Single-select: choose and submit in one go. */
  | { kind: 'select'; optionId: string }
  /** Multi-select: tick or untick, without submitting. */
  | { kind: 'toggle'; optionId: string }
  | { kind: 'move'; delta: number }
  /** Hand free-text answering to the main composer. */
  | { kind: 'custom' }
  /** Send what is currently ticked (the Done button, or Enter/⌘Enter). */
  | { kind: 'confirm' }
  /**
   * Leave the panel for the composer, optionally carrying the character that
   * triggered it.
   *
   * The panel sits directly above the composer and takes focus when a question
   * appears, so the number keys work the moment it does. That is only safe if
   * TYPING still goes where typing goes: without this, someone answering "the
   * scheduled interview for Dileep" in their own words would have their first
   * letter swallowed, and any digit in what they typed would have picked an
   * option and sent it.
   */
  | { kind: 'leave'; text?: string }
  | null;

export interface KeyContext {
  optionCount: number;
  allowCustom: boolean;
  /** True while a text box has focus — digits are then literal text. */
  typingCustom: boolean;
  /** Several answers allowed: keys TOGGLE, and Done/⌘Enter sends. */
  multiSelect?: boolean;
  /** Index of the focused row, counting the "Something else" row last. */
  activeIndex?: number;
}

/**
 * How many focusable rows the card has — the options plus the "Something else"
 * row, which is part of the same arrow-key loop rather than a separate stop.
 */
export function rowCount(optionCount: number, allowCustom: boolean): number {
  return optionCount + (allowCustom ? 1 : 0);
}

/**
 * Map a keydown to a card action. Pure, so every branch is unit-tested rather
 * than click-tested.
 *
 * Number keys are DISABLED while the custom box has focus: typing "2026" into
 * "which year?" must not tick option 2 and send the card.
 */
export function cardKeyAction(
  event: { key: string; metaKey?: boolean; ctrlKey?: boolean; altKey?: boolean },
  context: KeyContext,
): CardAction {
  const { key } = event;
  const accel = Boolean(event.metaKey || event.ctrlKey);

  // ⌘/Ctrl+Enter sends whatever is ticked, from anywhere — including from
  // inside the text field, which is where a keyboard user ends up.
  if (accel && key === 'Enter') return { kind: 'confirm' };
  if (event.metaKey || event.ctrlKey || event.altKey) return null;

  // Escape always returns to the composer. It does NOT answer the question:
  // wanting to type your own answer is the commonest reason to press it, and
  // silently submitting "no preference" for that would answer on the user's
  // behalf with something they never chose.
  if (key === 'Escape') return { kind: 'leave' };
  if (key === 'ArrowDown' || key === 'ArrowRight') return { kind: 'move', delta: 1 };
  if (key === 'ArrowUp' || key === 'ArrowLeft') return { kind: 'move', delta: -1 };

  if (key === 'Enter') {
    // Enter on the "Something else" row hands over to the composer rather than
    // sending an empty answer.
    if (
      context.allowCustom &&
      context.activeIndex === context.optionCount &&
      !context.typingCustom
    ) {
      return { kind: 'custom' };
    }
    return { kind: 'confirm' };
  }

  if (context.typingCustom) return null;

  if (/^[1-9]$/.test(key)) {
    const index = Number(key) - 1;
    if (index < context.optionCount) {
      return context.multiSelect
        ? { kind: 'toggle', optionId: String(index) }
        : { kind: 'select', optionId: String(index) };
    }
    if (context.allowCustom && index === context.optionCount) {
      return { kind: 'custom' };
    }
    // A digit past the end of the card is not a shortcut, it is the start of
    // something being typed ("90 days"). Fall through.
  }

  // Any other single printable character belongs to the composer.
  if (key.length === 1 && key !== ' ') return { kind: 'leave', text: key };
  return null;
}

/** Wrap an index into range — arrow keys cycle rather than dead-ending. */
export function wrapIndex(current: number, delta: number, length: number): number {
  if (length <= 0) return 0;
  return (((current + delta) % length) + length) % length;
}

/**
 * A stable idempotency key for ONE answer to ONE question.
 *
 * Derived from what was answered, not from a clock or a random value: a
 * double-click, a retried fetch after a timeout, and a reconnect all produce
 * the same key, so the server recognises them as the same submission and
 * returns the first result instead of generating a second answer.
 */
export function clientMessageId(
  clarificationId: string,
  selection: { optionIds?: readonly string[]; customText?: string; skipped?: boolean },
): string {
  const parts = [
    clarificationId,
    (selection.optionIds ?? []).join('+'),
    (selection.customText ?? '').trim(),
    selection.skipped ? 'skip' : '',
  ].join('|');
  // HASHED, not embedded. The raw text used to ride inside the id, and the
  // server caps client_message_id at 80 characters — so any typed answer
  // longer than ~37 chars ("as i have given the name Please check that")
  // failed validation and the user was told their answer could not be read.
  // FNV-1a keeps the property the id exists for: a double-click, a retried
  // fetch and a reconnect of the SAME answer all produce the SAME key.
  let h = 0x811c9dc5;
  for (let i = 0; i < parts.length; i += 1) {
    h ^= parts.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `clr-${clarificationId.slice(0, 40)}-${h.toString(16).padStart(8, '0')}`;
}

export interface Selection {
  optionIds?: readonly string[];
  customText?: string;
  skipped?: boolean;
}

/** Build the response body. Returns null when the selection says nothing. */
export function buildResponse(
  request: ClarificationRequest,
  selection: Selection,
): ClarificationResponse | null {
  const optionIds = (selection.optionIds ?? []).filter((id) =>
    request.options.some((o) => o.id === id),
  );
  const customText = (selection.customText ?? '').trim();
  const skipped = selection.skipped === true;
  if (!skipped && optionIds.length === 0 && !customText) return null;
  if (!request.multi_select && optionIds.length > 1) return null;
  return {
    clarification_id: request.clarification_id,
    conversation_id: request.conversation_id,
    client_message_id: clientMessageId(request.clarification_id, {
      optionIds,
      customText,
      skipped,
    }),
    selected_option_ids: [...optionIds],
    custom_text: customText,
    skipped,
    resume_token: request.resume_token,
  };
}

/**
 * The text shown in the thread for an answer.
 *
 * The card collapses once answered, so without this the transcript would jump
 * from a question to an answer with nothing in between explaining which reading
 * was chosen — which is also what the model sees on the next turn.
 */
export function answerSummary(
  request: ClarificationRequest,
  response: ClarificationResponse,
): string {
  if (response.skipped) return 'No preference — use your best judgement.';
  const labels = response.selected_option_ids
    .map((id) => request.options.find((o) => o.id === id)?.label)
    .filter((label): label is string => Boolean(label));
  if (labels.length) return labels.join(', ');
  return response.custom_text;
}

/** Placeholder the composer adopts while a question is waiting. */
export function composerPlaceholder(
  request: ClarificationRequest | null,
  fallback: string,
): string {
  if (!request) return fallback;
  return request.custom_placeholder || 'Tell me what you meant…';
}
