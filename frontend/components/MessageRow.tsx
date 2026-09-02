'use client';

/**
 * Message rendering (§9): quiet right-aligned user bubbles; full-width
 * assistant rows (no bubble) with markdown, streaming caret, pre-first-token
 * shimmer, "Stopped" marker, inline error rows with Retry, and a
 * ChatGPT-style icon action row (2026-08-05): copy · like · dislike · try
 * again, plus a "Sources" book button that opens the right-side
 * ActivityPanel (thinking + web research + time taken). Thinking and
 * research render inline only WHILE streaming; finished answers keep them
 * behind the Sources button instead.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChatMessage } from '@/lib/types';
import type { VersionInfo } from '@/lib/branching';
import { parseClarification } from '@/lib/clarification';
import { ClarificationRecord } from './ClarificationCard';
import { Loader } from './Loader';
import { LiveStatus } from './LiveStatus';
import { ReasoningStar } from './ReasoningStar';
import { SalesforceSourceLine } from './SalesforceSourceLine';
import {
  loadFeedback,
  saveFeedback,
  toggleFeedback,
  type MessageFeedback,
} from '@/lib/feedback';
import { stripCitations } from '@/lib/citations';
import {
  attachmentFile,
  resolveAttachmentAsync,
  uploadRefFor,
  writeInternalAttachment,
  fileBadgeFor,
  resolveAttachment,
  type ResolvedAttachment,
} from '@/lib/attachments';
import { AttachmentPreview, type ServerPreviewLoaders } from './AttachmentPreview';
import {
  fetchDocumentText,
  fetchUploadProfile,
  workbookFromProfile,
} from '@/lib/previewData';
import { AgentTimeline } from './AgentTimeline';
import { ActivityPanel } from './ActivityPanel';
import { countSources, ResearchPanel } from './ResearchPanel';
import { Markdown } from './Markdown';
import { PastedChip } from './PastedChip';
import { ProofDrawer } from './ProofDrawer';
import { CopyButton } from './CopyButton';
import { ReasoningAccordion } from './ReasoningAccordion';
import { friendlyError, trimNotice } from '@/lib/errors';
import {
  IconAlert,
  IconBook,
  IconChevronRight,
  IconFileText,
  IconPencil,
  IconRefresh,
  IconThumbDown,
  IconThumbUp,
} from './icons';

/**
 * The message action row, shared by both roles so they cannot drift apart.
 *
 * Assistant rows pin the LAST answer's actions open; every other row — and
 * every user row — reveals them on hover or keyboard focus. Same classes,
 * same spacing, same reveal, whoever sent the message.
 */
const ACTION_ROW =
  'mt-1.5 flex items-center gap-0.5 transition-opacity duration-ts';
const ACTION_ROW_HIDDEN =
  'opacity-0 focus-within:opacity-100 group-hover/msg:opacity-100';
/**
 * `‹ 2 / 2 ›` — which version of an edited turn is on screen.
 *
 * Deliberately tiny and quiet: it sits in the message's existing action row,
 * borrows its icon button styling, and adds no new spacing or colour of its
 * own. An end of the range simply disables its arrow rather than hiding it,
 * so the control does not change width as you move through versions.
 */
function VersionNav({
  versions,
  onSelect,
}: {
  versions: VersionInfo;
  onSelect: (parent: string, id: string) => void;
}) {
  const step = (target: string | undefined) => () => {
    if (target) onSelect(versions.parent, target);
  };
  return (
    <span className="mr-0.5 inline-flex items-center gap-0.5 text-xs text-muted">
      <button
        type="button"
        onClick={step(versions.previous)}
        disabled={!versions.previous}
        aria-label="Previous version"
        title="Previous version"
        className={VERSION_ARROW}
      >
        <IconChevronRight size={14} className="rotate-180" />
      </button>
      <span aria-live="polite" className="tabular-nums">
        {versions.number} / {versions.total}
      </span>
      <button
        type="button"
        onClick={step(versions.next)}
        disabled={!versions.next}
        aria-label="Next version"
        title="Next version"
        className={VERSION_ARROW}
      >
        <IconChevronRight size={14} />
      </button>
    </span>
  );
}

const VERSION_ARROW =
  'rounded p-1 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent';

/** The ghost icon button used by every action in that row. */
const ACTION_BUTTON =
  'rounded-lg p-2 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink';

/** The inline editor grows with its text, then scrolls (~12 rows). */
const EDIT_MAX_HEIGHT = 288;
/** Cancel · Send under the inline editor — ChatGPT's pill pair. */
const EDIT_BUTTON =
  'rounded-full px-4 py-1.5 text-sm font-medium transition-all duration-ts';

/**
 * NEW-09 / NEW-09A: an attachment on a sent message, as a control that
 * PREVIEWS it.
 *
 * Everything here used to be a `<span>` and an `<img>` — no role, no focus, no
 * handler — so the most obvious gesture in the thread, clicking the file you
 * just sent, did nothing at all. A native `<button>` is the fix and not a
 * detail of it: it brings the mouse, Enter, Space, the focus ring and the
 * screen-reader announcement along in one step.
 *
 * NEW-09A corrected what the button DOES. It used to open a tab or, for
 * anything a browser cannot render, save the file — and manual testing found
 * both spellings putting attachments in the Downloads tray. It now opens an
 * in-app dialog and nothing else. There is no path from this card to a
 * download, which is why the click handler resolves bytes and sets state
 * rather than touching `window` at all.
 *
 * 2026-09-02: the visible "Attach again" button that sat under the card was
 * removed on owner request. Only the button went — the card is still the drag
 * SOURCE for re-attaching (`draggable` + `onDragStart` below), and ChatApp's
 * `reuseAttachment` is still the single handler behind every internal drop.
 */
function OpenableAttachment({
  messageId,
  index,
  name,
  dataUrl,
  className,
  onReuse,
  upload,
  loaders,
  children,
}: {
  messageId: string;
  index: number;
  /** What to call the file in the accessible name. */
  name: string;
  /** The message's own persisted preview, when it has one (images do). */
  dataUrl?: string;
  className: string;
  /**
   * PHASE 4A/4B — can this file be put back in the composer?
   *
   * Read as a CAPABILITY FLAG, not as a click handler: the visible "Attach
   * again" button this used to render was removed on owner request
   * (2026-09-02), leaving the internal drag as the gesture. Its presence is
   * still what makes the card `draggable` and what lets `onDragStart` write
   * the reference, so omitting it (previews, tests) correctly leaves a row
   * that cannot offer to fill a composer that is not there. Keep it wired:
   * deleting it would silently make every historical card undraggable.
   */
  onReuse?: () => void;
  /** PHASE 3: where the bytes can be fetched from, when this is an upload. */
  upload?: ReturnType<typeof uploadRefFor>;
  /**
   * PHASE 4C — where to ask for a preview a browser cannot render itself.
   * Absent (no conversation behind this row) the dialog keeps its honest
   * "no preview" card, exactly as before.
   */
  loaders?: ServerPreviewLoaders;
  children: React.ReactNode;
}) {
  // Resolved on click, not on render: reading the store during render would
  // mint nothing but would tie the row to a mutable module-level map.
  const [source, setSource] = useState<ResolvedAttachment | null>(null);
  const label = `${name} — preview`;

  /**
   * PHASE 3/4C. Opening a card is now allowed to go to the network, because
   * after a reload that is the only place the bytes are — this used to report
   * a file the server was still holding as "no longer available".
   *
   * A format with its OWN server-backed preview (a workbook's profile, a
   * document's extracted text) skips the byte fetch entirely: the dialog is
   * about to ask for something smaller and more useful, and downloading 50 MB
   * of .xlsx to then not read it would be pure waste.
   */
  function open() {
    const local = resolveAttachment(messageId, index, { name, dataUrl });
    if (loaders || local.kind !== 'unavailable' || !upload) {
      setSource(local);
      return;
    }
    setSource({ ...local, kind: 'loading' });
    void resolveAttachmentAsync(messageId, index, {
      name,
      dataUrl,
      upload,
    }).then(setSource);
  }
  return (
    <>
      <button
        type="button"
        aria-label={label}
        title={label}
        // 4B: the card is the drag SOURCE. A <button> is not draggable by
        // default, which is why dragging one previously did nothing at all.
        draggable={Boolean(onReuse)}
        onDragStart={(e) => {
          if (!onReuse) return;
          // Identity only — never a path, a blob: URL, or anything in
          // text/plain (NEW-10A: a drag whose readable part was text got
          // typed into the prompt).
          if (!writeInternalAttachment(e.dataTransfer, { messageId, index })) {
            e.preventDefault();
          }
        }}
        onClick={open}
        className={className}
      >
        {children}
      </button>
      {source && (
        <AttachmentPreview
          source={source}
          onClose={() => setSource(null)}
          {...(loaders ?? {})}
        />
      )}
    </>
  );
}

/**
 * PHASE 4C — where a preview should ask for what the browser cannot read.
 *
 * Chosen by EXTENSION, like every other classification in this feature: a
 * workbook comes from the profile the orchestrator stored at upload time, a
 * .docx from the text it extracted with the standard library. Anything else
 * gets no loader at all, which is what keeps the honest "no preview" card for
 * .zip, .parquet and the executable formats — they are not merely unhandled
 * here, they are deliberately unreachable.
 *
 * Returns undefined without a conversation to ask about, so a row rendered
 * outside a chat behaves exactly as it did before this existed.
 */
function serverPreviewLoaders(
  conversationId: string | null,
  message: ChatMessage,
  index: number,
  name: string,
): ServerPreviewLoaders | undefined {
  if (!conversationId) return undefined;
  if (/\.xlsx$/i.test(name)) {
    const upload = uploadRefFor(conversationId, message, index);
    if (!upload) return undefined;
    return {
      loadWorkbook: async (signal) => {
        const found = await fetchUploadProfile(
          upload.conversationId,
          upload.uploadId,
          signal,
        );
        if (!found || found.expired) return null;
        return workbookFromProfile(found.profile, found.filename || name);
      },
    };
  }
  if (/\.docx$/i.test(name)) {
    return {
      loadDocumentText: (signal) =>
        fetchDocumentText(conversationId, name, signal),
    };
  }
  return undefined;
}

/**
 * H-01: what a dataset turn is doing right now.
 *
 * Lives on the ROW rather than on the message because it describes an
 * in-flight request, not the message itself — it must never be persisted, and
 * it must survive the stream manager replacing every message object.
 */
export type UploadStatus = 'uploading' | 'failed';

export function MessageRow({
  message,
  isLast,
  onRegenerate,
  onShowSummary,
  onRetry,
  uploadStatus = null,
  versions = null,
  onSelectVersion,
  onEditStart,
  editing = false,
  onEditCancel,
  onEditSubmit,
  onFeedback,
  clarificationPending = false,
  clarificationAnswer = '',
  onReuseAttachment,
  conversationId = null,
}: {
  message: ChatMessage;
  isLast: boolean;
  onRegenerate: () => void;
  /** Opens the read-only rolling-summary panel (compaction notice). */
  onShowSummary?: () => void;
  onRetry: () => void;
  /**
   * H-01: the dataset attached to this turn is still uploading, or failed.
   *
   * A dataset does not travel in the chat request — it streams to /api/upload
   * first — so the turn can sit on screen for a long time with nothing
   * happening yet. Without this the thread showed a question and no sign that
   * anything was in progress, which read as a frozen app.
   */
  uploadStatus?: UploadStatus | null;
  /**
   * "Edit" on a USER message: rewrite it IN PLACE, ChatGPT-style.
   *
   * The pencil swaps the bubble for a textarea seeded with the exact text,
   * over a Cancel · Send pair. This IS the destructive path the old
   * composer-prefill version deliberately avoided — sending really does
   * replace this turn and discard every turn after it — so the host owns the
   * state: it confirms when turns would be lost, drives the server's truncate
   * endpoint, and keeps at most one editor open. Omitting `onEditStart`
   * (previews, tests) hides the control and leaves the row read-only.
   */
  onEditStart?: () => void;
  /**
   * The alternatives of this turn, when it has more than one.
   *
   * Editing a message adds a version beside it instead of replacing it, so a
   * turn can have several. null — the usual case — renders no control at all:
   * `1 / 1` is not a navigator.
   */
  versions?: VersionInfo | null;
  /** Show a different version. Pure view selection — see ChatApp. */
  onSelectVersion?: (parent: string, id: string) => void;
  /** Is THIS row the one open for editing? Owned by the host. */
  editing?: boolean;
  /** Close the editor and change nothing at all. */
  onEditCancel?: () => void;
  /** Commit the rewrite. Always trimmed and non-empty. */
  onEditSubmit?: (text: string) => void;
  /**
   * Is THIS message's question the one the thread is waiting on?
   *
   * Computed by the parent from the whole thread (`cardState`), because a
   * message cannot know what came after it. A live question renders NOTHING
   * here: it is a temporary control and it belongs to the composer. Only an
   * answered one leaves a record behind.
   */
  clarificationPending?: boolean;
  /** What was chosen, for the record of an answered question. */
  clarificationAnswer?: string;
  /** Persist a thumb server-side. Omitted in contexts with no store
   *  (previews, tests), where the localStorage fallback still applies. */
  onFeedback?: (feedback: MessageFeedback | null) => void;
  /**
   * PHASE 4A/4B: put the Nth attachment of this turn back in the composer.
   *
   * Omitted in contexts with no composer (previews, tests), which is what
   * makes those rows' cards undraggable — a row cannot offer to fill an input
   * that is not there. The host's `reuseAttachment` is still reached on every
   * internal drop; this prop is what tells the card it may start such a drag.
   */
  onReuseAttachment?: (index: number) => void;
  /**
   * PHASE 4C: which conversation this row belongs to, so a preview can ask the
   * server for a workbook profile or a document's extracted text. null (a row
   * rendered outside a chat) simply means no server-backed preview.
   */
  conversationId?: string | null;
}) {
  // Hooks live above the user-bubble early return (rules of hooks).
  const [activityOpen, setActivityOpen] = useState(false);
  // The SERVER's value wins when the message has one: it is the copy that
  // survives a reload. localStorage is the fallback for messages that have
  // not been stored yet (and for anything rendering without a store), which
  // is also the whole reason thumbs used to disappear on refresh — a
  // rehydrated message is keyed differently from the live one it replaced.
  const [feedback, setFeedback] = useState<MessageFeedback | null>(() => {
    if (message.feedback === 'up' || message.feedback === 'down') {
      return message.feedback;
    }
    if (message.feedback === null) return null;
    return typeof window === 'undefined'
      ? null
      : loadFeedback(window.localStorage, message.id);
  });

  // A conversation loaded after this row first rendered (or refreshed by a
  // detached generation) brings the stored thumb with it.
  useEffect(() => {
    if (message.feedback === 'up' || message.feedback === 'down') {
      setFeedback(message.feedback);
    } else if (message.feedback === null) {
      setFeedback(null);
    }
  }, [message.feedback]);

  function onThumb(kind: MessageFeedback) {
    const next = toggleFeedback(feedback, kind);
    setFeedback(next);
    // Keep writing localStorage: it is what covers a message with no server
    // row yet, and it costs nothing for one that has one.
    saveFeedback(window.localStorage, message.id, next);
    onFeedback?.(next);
  }

  /**
   * The inline editor's own text: seeded from the message when the pencil
   * opens it, dropped when it closes. Re-opening therefore always starts from
   * what was actually sent, never from an abandoned half-edit.
   */
  const [editDraft, setEditDraft] = useState('');
  const editRef = useRef<HTMLTextAreaElement>(null);
  /** Armed when the editor opens; consumed once the seeded text has landed. */
  const focusOnOpen = useRef(false);

  // Opening seeds the box from the message; closing empties it. Keyed on
  // `editing` ALONE — `message.content` must not be a dependency or a
  // re-render mid-edit (a sibling stream tick, a history refresh) would throw
  // away what is being typed. Seeding here rather than in the click handler
  // is what makes the row a real controlled component: whoever opens it, the
  // box is correct.
  useEffect(() => {
    setEditDraft(editing ? (message.content ?? '') : '');
    if (editing) focusOnOpen.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);

  const autogrowEdit = useCallback((ta: HTMLTextAreaElement | null) => {
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(ta.scrollHeight, EDIT_MAX_HEIGHT)}px`;
    ta.style.overflowY = ta.scrollHeight > EDIT_MAX_HEIGHT ? 'auto' : 'hidden';
  }, []);

  // Size, focus and drop the caret after the last character — deferred to the
  // render AFTER the seed lands. `value` is React state, so doing this when
  // the node mounts would measure and select against a textarea React has not
  // written the text into yet, parking the caret at position 0.
  useEffect(() => {
    if (!focusOnOpen.current) return;
    const ta = editRef.current;
    if (!ta) return;
    focusOnOpen.current = false;
    autogrowEdit(ta);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }, [editDraft, autogrowEdit]);

  function beginEdit() {
    onEditStart?.();
  }

  function commitEdit() {
    const next = editDraft.trim();
    // An empty box is not an edit — Cancel is how you back out of one. What
    // an UNCHANGED text means is the host's call; it gets the trimmed string.
    if (!next) return;
    onEditSubmit?.(next);
  }

  if (message.role === 'user') {
    // Actions need something to act ON. An attachment-only turn (a dropped
    // PDF, images with no caption) has no text to copy or reuse, so it gets
    // no row rather than two buttons that would silently do nothing.
    const userText = message.content ?? '';
    const showUserActions = Boolean(userText.trim());
    return (
      // `group/msg` only marks the hover scope — it paints nothing. The
      // bubble below is untouched.
      <div className="group/msg flex justify-end">
        {/* The editor needs room to be typed in, so it takes the full thread
            width; the sent bubble keeps hugging its own text. */}
        <div className={editing ? 'w-full' : 'max-w-[85%] sm:max-w-[70%]'}>
          {(message.imageDataUrls?.length || message.imageDataUrl) && (
            <div className="mb-1.5 flex flex-wrap justify-end gap-1.5">
              {/* 2026-08-05: up to 5 images per turn — `imageDataUrls` when
                  several, the legacy single `imageDataUrl` otherwise.
                  data: URL previews — next/image can't optimize these. */}
              {(message.imageDataUrls?.length
                ? message.imageDataUrls
                : [message.imageDataUrl as string]
              ).map((url, i) => {
                // The real filename when this tab still holds the file;
                // otherwise the position, which is all the message persists.
                const held = attachmentFile(message.id, i);
                const name = held?.name ?? `Attached image ${i + 1}`;
                return (
                  <OpenableAttachment
                    key={i}
                    messageId={message.id}
                    index={i}
                    name={name}
                    dataUrl={url}
                    onReuse={
                      onReuseAttachment ? () => onReuseAttachment(i) : undefined
                    }
                    upload={uploadRefFor(conversationId, message, i)}
                    className="block rounded-ts focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={url}
                      alt={`Attached image ${i + 1}`}
                      className="max-h-40 rounded-ts border border-border object-cover"
                    />
                  </OpenableAttachment>
                );
              })}
            </div>
          )}
          {message.pdfName && (
            <div className="mb-1.5 flex flex-col items-end">
              {/* Same chip, now a real control (NEW-09). The classes below are
                  the ones it already had; only the hover tint and the focus
                  ring are new, because a thing that can be clicked has to look
                  and behave like it. */}
              <OpenableAttachment
                messageId={message.id}
                index={0}
                name={message.pdfName}
                onReuse={
                  onReuseAttachment ? () => onReuseAttachment(0) : undefined
                }
                upload={uploadRefFor(conversationId, message, 0)}
                loaders={serverPreviewLoaders(
                  conversationId,
                  message,
                  0,
                  message.pdfName,
                )}
                className="inline-flex items-center gap-2 rounded-2xl border border-border bg-surface-2 py-1.5 pl-1.5 pr-3 text-left transition-colors duration-ts hover:border-accent/50 hover:bg-surface focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
              >
                <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-danger/15 text-danger">
                  <IconFileText size={16} />
                </span>
                <span className="flex flex-col">
                  <span className="max-w-[220px] truncate text-xs text-ink">
                    {message.pdfName}
                  </span>
                  {/* pdfName carries EVERY non-image attachment (datasets
                      included) — a .csv labelled "PDF" was just wrong. */}
                  <span className="text-[10px] uppercase tracking-wide text-faint">
                    {fileBadgeFor(message.pdfName)}
                  </span>
                </span>
              </OpenableAttachment>
            </div>
          )}
          {message.meta?.pasted?.map((p) => (
            <div key={p.id} className="mb-1.5 flex justify-end">
              <PastedChip pasted={p} />
            </div>
          ))}
          {editing ? (
            /* The in-place editor: the bubble becomes a textarea over
               Cancel · Send, keeping the bubble's own shape and colour so the
               turn still reads as the user's while it is being rewritten. */
            <div className="rounded-[20px] bg-bubble px-4 py-3">
              <textarea
                ref={editRef}
                value={editDraft}
                onChange={(e) => {
                  setEditDraft(e.target.value);
                  autogrowEdit(e.target);
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    // Swallowed here so it cannot ALSO reach the window-level
                    // shortcut handler sitting behind the thread.
                    e.preventDefault();
                    e.stopPropagation();
                    onEditCancel?.();
                    return;
                  }
                  // Enter sends, Shift+Enter breaks the line — the composer's
                  // own contract, so the two boxes never disagree.
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    commitEdit();
                  }
                }}
                rows={1}
                aria-label="Edit your message"
                className="block w-full resize-none bg-transparent text-[15px] leading-relaxed text-ink focus:outline-none"
              />
              <div className="mt-2 flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => onEditCancel?.()}
                  className={`${EDIT_BUTTON} border border-border text-ink hover:bg-surface-2`}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={commitEdit}
                  disabled={!editDraft.trim()}
                  className={`${EDIT_BUTTON} bg-accent-strong text-white hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40`}
                >
                  Send
                </button>
              </div>
            </div>
          ) : (
            <>
              {message.content && (
                <div className="whitespace-pre-wrap break-words rounded-[20px] bg-bubble px-4 py-2.5 text-[15px] leading-relaxed">
                  {message.content}
                </div>
              )}
              {/* Edit · Copy, in the assistant row's own classes and its
                  hover-to-reveal behaviour, right-aligned under the bubble to
                  follow it. Copy reaches nothing; Edit only opens the box
                  above — neither sends or stores anything by itself. */}
              {showUserActions && (
                <div
                  className={`${ACTION_ROW} ${
                    // A turn WITH versions keeps its navigator visible: it is
                    // the only sign that another answer exists, and hiding it
                    // until hover would hide the fact itself.
                    versions ? '' : ACTION_ROW_HIDDEN
                  } justify-end`}
                >
                  {versions && onSelectVersion && (
                    <VersionNav versions={versions} onSelect={onSelectVersion} />
                  )}
                  {onEditStart && (
                    <button
                      type="button"
                      onClick={beginEdit}
                      aria-label="Edit message"
                      title="Edit"
                      className={ACTION_BUTTON}
                    >
                      <IconPencil size={18} />
                    </button>
                  )}
                  {/* The user's own words, verbatim: no citation stripping
                      (there are none to strip) and no trimming. */}
                  <CopyButton
                    text={userText}
                    label="Copy message"
                    variant="icon"
                  />
                </div>
              )}
            </>
          )}
          {/* H-01: a dataset turn is on screen long before its file has even
              finished uploading, so the turn says so itself. Indeterminate on
              purpose — /api/upload reports no byte progress, and a percentage
              we cannot measure would be a fiction. */}
          {uploadStatus && (
            <div
              className="mt-1.5 flex items-center justify-end gap-2 text-xs"
              aria-live="polite"
            >
              {uploadStatus === 'uploading' ? (
                <>
                  <Loader size={16} />
                  <span className="text-muted">Uploading dataset…</span>
                </>
              ) : (
                <>
                  <IconAlert size={13} className="shrink-0 text-danger" />
                  <span className="text-danger">
                    Dataset upload failed — nothing was sent to the model.
                  </span>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  const streaming = message.status === 'streaming';
  // V2: reasoning + steps live on the message while streaming and inside
  // meta once persisted (§4d/§4e) — read whichever is present.
  const reasoningText = message.meta?.reasoning ?? message.reasoning ?? '';
  const reasoningSeconds =
    message.meta?.reasoning_seconds ?? message.reasoningSeconds;
  const steps = message.steps ?? message.meta?.steps ?? [];
  // Live while streaming, from the stored meta once persisted.
  const research = message.research ?? message.meta?.research;
  // The Sources book button opens the ActivityPanel; it appears only when
  // the finished answer actually has thinking, an agent plan, web research,
  // or numbered [n] citations (meta.sources) behind it — none of which show
  // inline once the answer is done.
  const webSources = message.meta?.sources;
  const hasActivity =
    Boolean(reasoningText) ||
    steps.length > 0 ||
    Boolean(research && countSources(research) > 0) ||
    Boolean(webSources?.length) ||
    Boolean(message.meta?.document);
  // Salesforce Intelligence Mode: the live phase, or the one the finished
  // answer ended on. Only the LIVE one animates — a reopened chat shows the
  // final phase as history, not as work still in progress.
  const phaseStatus = message.phaseStatus ?? null;
  const clarification = parseClarification(message.meta?.clarification);
  // A turn whose ONLY content is a live question renders nothing at all: the
  // question itself is a temporary control and the composer is showing it.
  // Without this the transcript kept an empty assistant row under the request,
  // complete with copy / thumbs / retry buttons for a message with no text —
  // which reads as an answer that failed rather than a question being asked.
  if (clarification && clarificationPending && !hasActivity && !message.meta?.data) {
    return null;
  }
  const showShimmer =
    streaming &&
    message.content.length === 0 &&
    reasoningText.length === 0 &&
    steps.length === 0 &&
    // The research panel is itself a progress indicator — showing the
    // shimmer underneath it would read as two things loading.
    !research &&
    !message.searchStatus &&
    // …and so is the Reasoning Star, which carries a real phase label.
    !phaseStatus;

  return (
    <div className="group/msg w-full">
      {phaseStatus && streaming && (
        <ReasoningStar
          status={phaseStatus}
          size={message.content.length === 0 ? 'lg' : 'sm'}
          className="mb-2"
        />
      )}
      {message.searchStatus && streaming && message.content.length === 0 && (
        <LiveStatus
          text={message.searchStatus}
          effortNote={
            // `meta.effort` does not exist yet while streaming, so the phase
            // text itself is the signal: only the planning path runs several
            // full model passes before it can show a step. Measured: 213 s to
            // the first step on a 23,520-character paste (2026-08-29).
            /^planning/i.test(message.searchStatus)
              ? 'It plans first, then runs each step — a long input can take a few minutes before the first step appears.'
              : undefined
          }
        />
      )}
      {showShimmer ? (
        /* The ordinary "waiting for the first token" state. It used to be three
           shimmering bars pretending to be text; it is the same event as every
           other kind of work, so it now says so with the same artwork. */
        <div className="py-1">
          <Loader size={28} label="Waiting for the first token" />
        </div>
      ) : (
        <>
          {/* Inline thinking/research are LIVE progress only (2026-08-05):
              once the answer is done they move behind the Sources button so
              finished messages stay clean, ChatGPT-style. */}
          {reasoningText && streaming && (
            <ReasoningAccordion
              text={reasoningText}
              seconds={reasoningSeconds}
              thinking={streaming && message.content.length === 0}
            />
          )}

          {research && (streaming || research.active) && (
            <ResearchPanel research={research} />
          )}

          {/* The agent plan card is live progress too (owner request
              2026-08-05): once the answer is done it moves into the
              ActivityPanel behind the Sources button. */}
          {steps.length > 0 && streaming && <AgentTimeline steps={steps} />}

          {/* The question is streamed as TEXT as well as carried on the card,
              so that a client with no card renderer — and the stored history a
              future one reads back — still shows a usable question. When the
              card IS rendering, showing both means the user reads the same
              question and the same four options twice, one of them inert. The
              card wins; nothing is lost, because the text is still in
              `message.content` and still goes to the model on the next turn. */}
          {message.content && !clarification && (
            /* No font-size override: the body inherits --ts-fs-base (16px).
               It used to be hardcoded to 15px, which is why assistant prose
               read dimmer than it should — thin stems at 15px on pure black
               lose weight to greyscale antialiasing, and #ffffff stops
               looking white. It also left `.md h3`/`h4` (16px) rendering
               LARGER than the body they head. */
            <div>
              {/* [n] citation markers are stripped for display (2026-08-05)
                  — the numbered sources live in the ActivityPanel instead.
                  The stored content keeps them, so nothing is lost. */}
              <Markdown text={stripCitations(message.content)} />
              {streaming && <span aria-hidden className="stream-caret" />}
            </div>
          )}

          {message.meta?.context?.compacted && (
            <button
              type="button"
              onClick={onShowSummary}
              className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
            >
              <IconFileText size={12} className="shrink-0" />
              Conversation compacted — older messages were summarized to free up
              space.
              <span className="text-faint underline">See summary</span>
            </button>
          )}

          {/* A LIVE question is not rendered here at all — the composer owns
              it (see ClarificationCard). What the transcript keeps is the
              record of one already answered, so the user turn after it
              ("Interview") reads as an answer rather than a non sequitur. */}
          {clarification && !clarificationPending && (
            <ClarificationRecord
              question={clarification.question}
              answer={clarificationAnswer || 'Answered'}
            />
          )}

          {message.meta?.salesforce_sources && (
            <SalesforceSourceLine
              sources={message.meta.salesforce_sources}
              scope={message.meta.salesforce_scope}
              assumptions={message.meta.assumptions}
            />
          )}


          {message.meta?.input_trimmed && (
            <p className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted">
              <IconAlert size={12} className="shrink-0 text-warn" />
              {trimNotice(message.meta.input_trimmed)}
            </p>
          )}

          {(message.meta?.memory_updated?.length ?? 0) > 0 && (
            <p
              className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted"
              title={message.meta!.memory_updated!.join('\n')}
            >
              Memory updated
            </p>
          )}

          {message.status === 'stopped' && (
            <p className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-0.5 text-xs text-muted">
              Stopped
            </p>
          )}

          {message.status === 'error' &&
            (() => {
              // Raw upstream payloads ("Error code: 400 - {'error': …}") are
              // unreadable in a chat thread AND are not the user's business:
              // they can quote a DSN, a header or a traceback. Only the plain
              // sentence is rendered. The original is written to the server
              // log instead (lib/serverLog.ts), which is where an engineer
              // can actually use it.
              const friendly = friendlyError(message.errorMessage);
              return (
                <div
                  role="alert"
                  className="mt-2 rounded-ts border bg-surface px-3.5 py-2.5"
                  style={{ borderColor: 'color-mix(in srgb, var(--ts-danger) 40%, transparent)' }}
                >
                  <div className="flex flex-wrap items-center gap-3">
                    <IconAlert size={16} className="shrink-0 text-danger" />
                    <span className="min-w-0 flex-1 text-sm">
                      {friendly.message}
                    </span>
                    <button
                      type="button"
                      onClick={onRetry}
                      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs font-medium transition-colors duration-ts hover:bg-surface-2"
                    >
                      <IconRefresh size={13} />
                      Retry
                    </button>
                  </div>
                </div>
              );
            })()}

          {message.meta && <ProofDrawer meta={message.meta} />}

          {!streaming && message.content && message.status !== 'error' && (
            <div
              className={`${ACTION_ROW} ${
                isLast || versions ? 'opacity-100' : ACTION_ROW_HIDDEN
              }`}
            >
              {/* An answer gets alternatives too: retrying inside a branched
                  conversation adds one beside the old answer rather than
                  destroying it, so it needs the same way back. */}
              {versions && onSelectVersion && (
                <VersionNav versions={versions} onSelect={onSelectVersion} />
              )}
              {/* ChatGPT-style icon row (2026-08-05): quiet ghost icons
                  instead of labelled chip buttons. */}
              <CopyButton
                text={stripCitations(message.content)}
                label="Copy message"
                variant="icon"
              />
              <button
                type="button"
                onClick={() => onThumb('up')}
                aria-label="Good response"
                aria-pressed={feedback === 'up'}
                title="Good response"
                className={`rounded-lg p-2 transition-colors duration-ts hover:bg-surface-2 ${
                  feedback === 'up'
                    ? 'text-accent'
                    : 'text-icon hover:text-ink'
                }`}
              >
                <IconThumbUp size={18} />
              </button>
              <button
                type="button"
                onClick={() => onThumb('down')}
                aria-label="Bad response"
                aria-pressed={feedback === 'down'}
                title="Bad response"
                className={`rounded-lg p-2 transition-colors duration-ts hover:bg-surface-2 ${
                  feedback === 'down'
                    ? 'text-danger'
                    : 'text-icon hover:text-ink'
                }`}
              >
                <IconThumbDown size={18} />
              </button>
              <button
                type="button"
                onClick={onRegenerate}
                aria-label="Try again"
                title="Try again"
                className={ACTION_BUTTON}
              >
                <IconRefresh size={18} />
              </button>
              {hasActivity && (
                <button
                  type="button"
                  onClick={() => setActivityOpen(true)}
                  aria-label="Show sources and thinking"
                  aria-expanded={activityOpen}
                  title="Sources — searches and thinking behind this answer"
                  className="ml-0.5 inline-flex items-center gap-1.5 rounded-lg px-2.5 py-2 text-xs font-medium text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                >
                  <IconBook size={16} />
                  Sources
                </button>
              )}
            </div>
          )}

          <ActivityPanel
            documentRead={message.meta?.document}
            open={activityOpen}
            onClose={() => setActivityOpen(false)}
            reasoning={reasoningText || undefined}
            reasoningSeconds={reasoningSeconds}
            steps={steps}
            research={research}
            sources={webSources}
          />
        </>
      )}
    </div>
  );
}
