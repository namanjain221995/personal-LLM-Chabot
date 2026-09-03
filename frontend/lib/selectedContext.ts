/**
 * "Ask TechSara AI" — replying to a specific excerpt of an earlier message
 * (2026-09-03).
 *
 * The excerpt never enters the composer textarea. It is captured as structured
 * state, carried on the sent user message's `meta.selected_context`, and folded
 * into the text the model receives at REQUEST time — exactly the arrangement
 * `meta.pasted` has used since V5, and for the same three reasons: the server
 * round-trips meta untouched so the reference survives a reload; any browser
 * can render the quote from history; and because `content` is never rewritten,
 * edit and regenerate re-send the same turn without re-wrapping the quote a
 * second time.
 *
 * Pure module — no React, no DOM. The geometry helper takes plain rectangles so
 * the clamping it does is testable without a layout engine.
 */

import { foldModelContent } from './pasted';
import type { Meta, SelectedContext } from './types';

/**
 * Hard cap on what travels to the model, in characters.
 *
 * A selection is a gesture, not a file: the point is "this bit here", and a
 * user who wants a whole document already has the paste chip and the upload
 * rail for it. 4000 characters is roughly 1000 tokens — comfortably more than
 * any paragraph anyone points at, and small enough that quoting it cannot be
 * what pushes a conversation into a compaction. Past it the excerpt is cut and
 * `truncated` is set, which the reference card SAYS rather than hiding.
 */
export const SELECTED_CONTEXT_MAX_CHARS = 4000;

/** How much of the excerpt the reference card shows before eliding. */
export const SELECTED_CONTEXT_PREVIEW_CHARS = 180;

/**
 * Tidy a raw selection without damaging it.
 *
 * Outer whitespace goes (a drag almost always picks up a leading space or a
 * trailing newline) and so do trailing spaces on each line and runs of more
 * than one blank line. Internal single newlines STAY: a selected list or a
 * selected code block is meaningless once its lines are joined, and the whole
 * feature is about pointing at text precisely.
 */
export function normalizeSelectedText(raw: string): string {
  return (raw ?? '')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((line) => line.replace(/[ \t]+$/, ''))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/**
 * Build the reference, or null when there is nothing to reference.
 *
 * Whitespace-only selections are the common accident — a double-click that
 * lands between words, a triple-click on an empty line — and they must produce
 * no action at all rather than an empty quote card.
 */
export function makeSelectedContext(
  raw: string,
  messageId: string,
  sourceRole: 'user' | 'assistant',
): SelectedContext | null {
  const text = normalizeSelectedText(raw);
  if (!text || !messageId) return null;
  if (text.length <= SELECTED_CONTEXT_MAX_CHARS) {
    return { text, messageId, sourceRole };
  }
  return {
    text: text.slice(0, SELECTED_CONTEXT_MAX_CHARS).trimEnd(),
    messageId,
    sourceRole,
    truncated: true,
  };
}

/** One-line preview for the reference card. Display only — never sent. */
export function previewSelectedText(
  text: string,
  limit = SELECTED_CONTEXT_PREVIEW_CHARS,
): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit).trimEnd()}…`;
}

/**
 * The model-visible wrapper.
 *
 * Assembled at send time and shown to nobody: the UI renders the quote from
 * `meta.selected_context` as its own block, so the user never sees this
 * scaffolding and never has to edit around it. Blockquote markers make the
 * boundary between "the text I pointed at" and "what I am asking" unambiguous
 * to the model without inventing a private syntax.
 *
 * A turn with a quote and no typed text (a quote sent alongside an attachment)
 * omits the follow-up section rather than emitting an empty heading.
 */
export function foldSelectedContext(
  content: string,
  context?: SelectedContext | null,
): string {
  if (!context || !context.text.trim()) return content;
  const origin =
    context.sourceRole === 'user'
      ? 'a previous user message'
      : 'a previous assistant message';
  const quoted = context.text
    .split('\n')
    .map((line) => (line ? `> ${line}` : '>'))
    .join('\n');
  const head = `Selected context from ${origin}:\n\n${quoted}${
    context.truncated ? '\n>\n> […excerpt truncated]' : ''
  }`;
  const body = (content ?? '').trim();
  return body ? `${head}\n\nUser follow-up:\n\n${body}` : head;
}

/**
 * The single place a stored turn becomes the string the model reads.
 *
 * Pasted blocks fold first (they are the turn's own content), then the quote
 * wraps the result — so a turn that both pastes a log and points at a sentence
 * arrives as context, context, question, in that order.
 */
export function foldTurnForModel(message: {
  content: string;
  meta?: Meta;
}): string {
  return foldSelectedContext(
    foldModelContent(message.content, message.meta?.pasted),
    message.meta?.selected_context,
  );
}

/* ----------------------------------------------------------- positioning */

export interface Rect {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export interface AskPlacement {
  top: number;
  left: number;
  /** Which side of the selection the action ended up on. */
  side: 'above' | 'below';
}

/** Breathing room between the action and both the selection and the edges. */
export const ASK_GAP = 8;
export const ASK_MARGIN = 8;

/**
 * Where to put the floating action, in viewport coordinates.
 *
 * Above the selection by preference — that is the edge a mouse has just left,
 * so the button is not under the cursor and not covering the words the user is
 * still reading. It flips below only when there is genuinely no room, and is
 * clamped on all four edges afterwards, because a narrow phone in landscape
 * will otherwise push it off-screen where nothing can reach it.
 */
export function askPlacement(
  selection: Rect,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
): AskPlacement {
  const needed = size.height + ASK_GAP;
  const side: 'above' | 'below' =
    selection.top >= needed + ASK_MARGIN ||
    selection.bottom + needed + ASK_MARGIN > viewport.height
      ? 'above'
      : 'below';
  const rawTop =
    side === 'above'
      ? selection.top - size.height - ASK_GAP
      : selection.bottom + ASK_GAP;
  const rawLeft = (selection.left + selection.right) / 2 - size.width / 2;
  // Math.max last: on a viewport narrower than the action itself, pinning the
  // LEFT edge keeps the label readable, where clamping right would hide it.
  return {
    side,
    top: Math.max(
      ASK_MARGIN,
      Math.min(rawTop, viewport.height - size.height - ASK_MARGIN),
    ),
    left: Math.max(
      ASK_MARGIN,
      Math.min(rawLeft, viewport.width - size.width - ASK_MARGIN),
    ),
  };
}

/* ------------------------------------------------------ selection reading */

/** Marks a region whose text may be quoted. Set by MessageRow. */
export const MESSAGE_ID_ATTR = 'data-chat-message-id';
export const MESSAGE_ROLE_ATTR = 'data-chat-message-role';

export interface SelectionCandidate {
  context: SelectedContext;
  /** Viewport rect of the selection, for positioning the action. */
  rect: Rect;
}

function messageHost(node: Node | null): HTMLElement | null {
  const el =
    node == null
      ? null
      : node.nodeType === 1
        ? (node as HTMLElement)
        : node.parentElement;
  return el?.closest?.(`[${MESSAGE_ID_ATTR}]`) ?? null;
}

/**
 * Turn the live selection into a candidate, or null.
 *
 * Everything this refuses, it refuses on purpose:
 *
 *  - collapsed or whitespace-only — the accidental click and the stray drag;
 *  - either end outside a marked message region — sidebar titles, buttons,
 *    account text, toolbar labels, every piece of chrome that is not content;
 *  - the two ends in DIFFERENT messages — a reference that spans two turns has
 *    no single origin, and guessing one would attribute words to the wrong
 *    speaker, so it produces no action at all;
 *  - anything inside an input, textarea or contenteditable — the composer's
 *    own text is not a message and must keep its native behaviour;
 *  - a degenerate rectangle — a range that measures nothing cannot be pointed
 *    at, and positioning against it would put the action at the origin.
 */
export function candidateFromSelection(
  selection: Selection | null,
): SelectionCandidate | null {
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
    return null;
  }
  const text = normalizeSelectedText(selection.toString());
  if (!text) return null;

  const range = selection.getRangeAt(0);
  const start = messageHost(range.startContainer);
  const end = messageHost(range.endContainer);
  if (!start || !end || start !== end) return null;

  const common =
    range.commonAncestorContainer.nodeType === 1
      ? (range.commonAncestorContainer as HTMLElement)
      : range.commonAncestorContainer.parentElement;
  if (common?.closest('input, textarea, [contenteditable=""], [contenteditable="true"]')) {
    return null;
  }

  const messageId = start.getAttribute(MESSAGE_ID_ATTR) ?? '';
  const role = start.getAttribute(MESSAGE_ROLE_ATTR) === 'user' ? 'user' : 'assistant';
  const context = makeSelectedContext(text, messageId, role);
  if (!context) return null;

  const r = range.getBoundingClientRect();
  if (!r || (r.width === 0 && r.height === 0)) return null;

  return {
    context,
    rect: { top: r.top, bottom: r.bottom, left: r.left, right: r.right },
  };
}
