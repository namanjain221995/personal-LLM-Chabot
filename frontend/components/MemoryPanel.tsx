'use client';

/**
 * The memory panel (B11): everything the assistant saved about the signed-in
 * person, where each fact came from and when, and a way to delete one fact or
 * all of them.
 *
 * Why it exists: the orchestrator has saved facts since V10 and injects them
 * into later turns as true, but nothing in the app could show them. 132
 * production rows written under the old extraction rules — task requests, one
 * jailbreak attempt — sat where their owners could neither see nor remove
 * them, and the "Memory updated" chip under an answer opened nothing.
 *
 * Two hosts: SettingsDialog's Memory section renders <MemoryPanel/> inline,
 * and the chip opens <MemoryDialog/>, a modal on SettingsDialog's portal
 * recipe with the same panel inside.
 *
 * Nested dialogs and Escape: the per-row ConfirmDialog and the typed
 * clear-all dialog are portalled, but React still bubbles their key events
 * through THIS tree to whichever dialog hosts the panel, and that host closes
 * on Escape. So the panel claims an Escape that belongs to a nested dialog,
 * closes the nested one itself and stops it there — the host stays open.
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { createPortal } from 'react-dom';
import type { FetchLike } from '@/lib/auth';
import { formatDay, formatWhen } from '@/lib/format';
import {
  CLEAR_ALL_PHRASE,
  clearFacts,
  deleteFact,
  excerptQuote,
  factKey,
  isUnknownSource,
  listFacts,
  sourceLabel,
  type MemoryFact,
} from '@/lib/memory';
import { ConfirmDialog } from './ConfirmDialog';
import { Loader } from './Loader';
import { useToast } from './Providers';
import { IconAlert, IconTrash, IconX } from './icons';

/** A fact quoted inside a sentence (confirm body, button label), kept short. */
function shortFact(fact: string, max = 80): string {
  const flat = fact.replace(/\s+/g, ' ').trim();
  return flat.length <= max ? flat : `${flat.slice(0, max).trimEnd()}…`;
}

/** Keep Tab inside a modal: from the last control back to the first, and back. */
function trapTab(e: ReactKeyboardEvent<HTMLElement>, root: HTMLElement | null) {
  if (e.key !== 'Tab' || !root) return;
  const focusable = Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
    ),
  );
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

/* ------------------------------------------------------------ the panel */

interface MemoryPanelProps {
  /** Injectable for tests — same idiom as SecuritySettings. */
  fetchFn?: FetchLike;
  /**
   * Facts the reply that opened the panel just saved (`meta.memory_updated`),
   * marked "Just saved" so the person finds them without reading the list.
   */
  highlight?: readonly string[];
  /** The modal host has its own "Memory" title; the settings section does not. */
  showHeading?: boolean;
}

export function MemoryPanel({
  fetchFn = fetch,
  highlight,
  showHeading = true,
}: MemoryPanelProps) {
  const { toast } = useToast();
  const [facts, setFacts] = useState<MemoryFact[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState<MemoryFact | null>(null);
  const [clearOpen, setClearOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  /** Where focus goes once a row is gone: a fact id, or 'top'. */
  const [focusAfter, setFocusAfter] = useState<number | 'top' | null>(null);
  const deleteButtons = useRef(new Map<number, HTMLButtonElement>());
  const topRef = useRef<HTMLDivElement>(null);
  const clearButtonRef = useRef<HTMLButtonElement>(null);
  const headingId = useId();

  const justSaved = useMemo(
    () => new Set((highlight ?? []).map(factKey)),
    [highlight],
  );

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoadError(null);
      setFacts(null);
      try {
        const rows = await listFacts(fetchFn, signal);
        if (!signal?.aborted) setFacts(rows);
      } catch {
        if (!signal?.aborted) {
          setLoadError('Could not load your memory. Check your connection and try again.');
        }
      }
    },
    [fetchFn],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (focusAfter === null) return;
    const target =
      focusAfter === 'top'
        ? topRef.current
        : (deleteButtons.current.get(focusAfter) ?? topRef.current);
    target?.focus({ preventScroll: false });
    setFocusAfter(null);
  }, [focusAfter, facts]);

  async function removeOne(fact: MemoryFact) {
    setPending(null);
    setActionError(null);
    setBusy(true);
    try {
      await deleteFact(fact.id, fetchFn);
      const list = facts ?? [];
      const at = list.findIndex((f) => f.id === fact.id);
      const next = list[at + 1] ?? list[at - 1] ?? null;
      setFacts(list.filter((f) => f.id !== fact.id));
      setFocusAfter(next ? next.id : 'top');
      toast('Deleted from memory');
    } catch {
      setActionError(`“${shortFact(fact.fact, 60)}” was not deleted. Try again.`);
      setFocusAfter(fact.id);
    } finally {
      setBusy(false);
    }
  }

  async function removeAll() {
    setClearOpen(false);
    setActionError(null);
    setBusy(true);
    try {
      const n = await clearFacts(fetchFn);
      setFacts([]);
      setFocusAfter('top');
      toast(n === 1 ? 'Deleted 1 saved fact' : `Deleted ${n} saved facts`);
    } catch {
      setActionError('Your memory was not deleted. Try again.');
      clearButtonRef.current?.focus();
    } finally {
      setBusy(false);
    }
  }

  function cancelPending() {
    const id = pending?.id;
    setPending(null);
    if (id !== undefined) deleteButtons.current.get(id)?.focus();
  }

  function cancelClear() {
    setClearOpen(false);
    clearButtonRef.current?.focus();
  }

  // See the header: an Escape inside a nested dialog is that dialog's, not
  // the host's.
  function onKeyDown(e: ReactKeyboardEvent<HTMLElement>) {
    if (e.key !== 'Escape') return;
    if (pending) {
      e.stopPropagation();
      cancelPending();
    } else if (clearOpen) {
      e.stopPropagation();
      cancelClear();
    }
  }

  const count = facts?.length ?? 0;

  return (
    <section aria-labelledby={headingId} onKeyDown={onKeyDown}>
      <div ref={topRef} tabIndex={-1} className="rounded-md focus:outline-none">
        <h3
          id={headingId}
          className={showHeading ? 'text-sm font-semibold text-ink' : 'sr-only'}
        >
          Memory
        </h3>
        <p className={`${showHeading ? 'mt-1 ' : ''}max-w-prose text-xs text-muted`}>
          What the assistant has saved about you and brings into later chats.
          Delete anything that is wrong, out of date or not about you.
        </p>
      </div>

      {facts === null && !loadError && (
        <div className="flex justify-center py-8">
          <Loader size={22} label="Loading memory" />
        </div>
      )}

      {loadError && (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <p role="alert" className="flex items-center gap-1.5 text-sm text-danger">
            <IconAlert size={14} className="shrink-0" />
            {loadError}
          </p>
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Retry
          </button>
        </div>
      )}

      {facts !== null && count === 0 && (
        <div className="mt-4 rounded-ts border border-dashed border-border px-4 py-6">
          <p className="text-sm text-ink">Nothing saved yet.</p>
          <p className="mt-1 max-w-prose text-xs text-muted">
            When you tell the assistant something about yourself, such as your
            role or how you like answers written, it can save it here so later
            chats start from it.
          </p>
        </div>
      )}

      {actionError && (
        <p role="alert" className="mt-3 flex items-start gap-1.5 text-sm text-danger">
          <IconAlert size={14} className="mt-0.5 shrink-0" />
          <span className="[overflow-wrap:anywhere]">{actionError}</span>
        </p>
      )}

      {facts !== null && count > 0 && (
        <>
          <ul
            aria-label="Saved facts"
            className="mt-4 divide-y divide-border rounded-ts border border-border"
          >
            {facts.map((f) => (
              <FactRow
                key={f.id}
                fact={f}
                justSaved={justSaved.has(factKey(f.fact))}
                busy={busy}
                onDelete={() => setPending(f)}
                buttonRef={(el) => {
                  if (el) deleteButtons.current.set(f.id, el);
                  else deleteButtons.current.delete(f.id);
                }}
              />
            ))}
          </ul>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-faint">
              {count === 1 ? '1 saved fact' : `${count} saved facts`}
            </p>
            <button
              ref={clearButtonRef}
              type="button"
              disabled={busy}
              onClick={() => setClearOpen(true)}
              className="rounded-lg border border-border px-3 py-1.5 text-sm text-danger transition-colors duration-ts hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:cursor-not-allowed disabled:opacity-35 max-sm:min-h-8 [@media(pointer:coarse)]:min-h-8"
            >
              Delete all
            </button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={pending !== null}
        title="Delete this from memory?"
        body={
          pending
            ? `The assistant will stop using “${shortFact(pending.fact)}” in later chats. This cannot be undone.`
            : ''
        }
        confirmLabel="Delete"
        onConfirm={() => {
          if (pending) void removeOne(pending);
        }}
        onCancel={cancelPending}
      />

      <ClearAllDialog
        open={clearOpen}
        count={count}
        onConfirm={() => void removeAll()}
        onCancel={cancelClear}
      />
    </section>
  );
}

/* ------------------------------------------------------------- one row */

function FactRow({
  fact,
  justSaved,
  busy,
  onDelete,
  buttonRef,
}: {
  fact: MemoryFact;
  justSaved: boolean;
  busy: boolean;
  onDelete: () => void;
  buttonRef: (el: HTMLButtonElement | null) => void;
}) {
  const quote = excerptQuote(fact.source_excerpt);
  const unknown = isUnknownSource(fact.source);
  const when = fact.created_at ?? fact.updated_at;
  return (
    <li data-testid="memory-fact" className="flex items-start gap-3 px-3 py-2.5">
      <div className="min-w-0 flex-1">
        <p className="text-sm text-ink [overflow-wrap:anywhere]">{fact.fact}</p>
        {quote && (
          // The person's own words behind the fact, so they can judge the
          // extractor's paraphrase rather than trust it.
          <p className="mt-1 border-l-2 border-accent/50 pl-2 text-xs text-muted [overflow-wrap:anywhere]">
            “{quote}”
          </p>
        )}
        <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-faint">
          <span
            className={unknown ? 'text-warn' : undefined}
            title={
              unknown
                ? 'Saved before the assistant recorded where facts come from.'
                : undefined
            }
          >
            {sourceLabel(fact.source)}
          </span>
          {when && (
            <time
              dateTime={when}
              title={`Saved ${formatWhen(when)}${
                fact.updated_at && fact.updated_at !== fact.created_at
                  ? ` · updated ${formatWhen(fact.updated_at)}`
                  : ''
              }`}
            >
              {formatDay(when)}
            </time>
          )}
          {justSaved && (
            <span className="rounded-full border border-accent/50 bg-accent/10 px-2 py-px text-[10px] font-medium text-accent">
              Just saved
            </span>
          )}
        </p>
      </div>
      <button
        ref={buttonRef}
        type="button"
        disabled={busy}
        onClick={onDelete}
        aria-label={`Delete “${shortFact(fact.fact)}”`}
        title="Delete"
        className="inline-flex shrink-0 items-center justify-center rounded-md p-1.5 text-faint transition-colors duration-ts hover:bg-danger/10 hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:cursor-not-allowed disabled:opacity-35 max-sm:h-8 max-sm:w-8 [@media(pointer:coarse)]:h-8 [@media(pointer:coarse)]:w-8"
      >
        <IconTrash size={15} />
      </button>
    </li>
  );
}

/* --------------------------------------------- the typed clear-all confirm */

/**
 * ConfirmDialog with a typed phrase in front of the button. Clearing memory is
 * irreversible and sits a few pixels from the per-row delete, so one click
 * (or one Enter) must not be enough. The comparison ignores case and outer
 * spaces because phone keyboards capitalise the first letter.
 */
function ClearAllDialog({
  open,
  count,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  count: number;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [typed, setTyped] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const inputId = useId();
  const matches = typed.trim().toLowerCase() === CLEAR_ALL_PHRASE;

  useEffect(() => {
    if (!open) return;
    setTyped('');
    inputRef.current?.focus({ preventScroll: true });
  }, [open]);

  if (!open || typeof document === 'undefined') return null;

  function onKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onCancel();
      return;
    }
    trapTab(e, panelRef.current);
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[75] flex items-center justify-center bg-black/60 p-4"
      onClick={onCancel}
    >
      <div
        ref={panelRef}
        role="alertdialog"
        aria-modal="true"
        aria-label="Delete everything the assistant remembers?"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
        className="w-full max-w-sm rounded-ts border border-border bg-surface p-4 shadow-2xl"
      >
        <div className="flex items-start gap-3">
          <IconAlert size={18} className="mt-0.5 shrink-0 text-danger" />
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-ink">
              Delete everything the assistant remembers?
            </h2>
            <p className="mt-1 text-sm text-muted">
              {count === 1 ? 'The 1 saved fact' : `All ${count} saved facts`} will
              be deleted and later chats will start without them. This cannot be
              undone.
            </p>
          </div>
        </div>
        <form
          className="mt-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (matches) onConfirm();
          }}
        >
          <label htmlFor={inputId} className="block text-xs font-medium text-muted">
            Type <strong className="font-semibold text-ink">{CLEAR_ALL_PHRASE}</strong> to
            confirm
          </label>
          <input
            ref={inputRef}
            id={inputId}
            type="text"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => {
              // jsdom does not submit a form on Enter; browsers do. Handle it
              // here so both behave the same, and a near miss does nothing.
              if (e.key === 'Enter') {
                e.preventDefault();
                if (matches) onConfirm();
              }
            }}
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            className="mt-1.5 w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-ink placeholder:text-faint focus:border-danger/60 focus:outline-none"
          />
          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!matches}
              className="rounded-lg px-3 py-1.5 text-sm font-medium text-white transition-opacity duration-ts hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:cursor-not-allowed disabled:opacity-35"
              style={{ background: 'var(--ts-danger)' }}
            >
              Delete all memory
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}

/* ------------------------------------------------- the chip's modal host */

interface MemoryDialogProps {
  open: boolean;
  onClose: () => void;
  fetchFn?: FetchLike;
  highlight?: readonly string[];
}

/**
 * The panel as a modal, for the "Memory updated" chip. SettingsDialog's
 * recipe: portalled to <body> (a transformed ancestor would otherwise capture
 * position:fixed), z-[70], Escape handled on the panel so a nested dialog's
 * Escape closes only that dialog.
 */
export function MemoryDialog({ open, onClose, fetchFn, highlight }: MemoryDialogProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) closeRef.current?.focus({ preventScroll: true });
  }, [open]);

  if (!open || typeof document === 'undefined') return null;

  function onPanelKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose();
      return;
    }
    // Only while focus is in THIS panel: a nested confirm portals outside it
    // and keeps its own Tab order.
    if (panelRef.current?.contains(document.activeElement)) {
      trapTab(e, panelRef.current);
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Memory"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onPanelKeyDown}
        className="palette-panel flex max-h-[85dvh] w-full max-w-lg flex-col overflow-hidden rounded-ts border border-border bg-surface shadow-2xl"
      >
        <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">Memory</h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close memory"
            className="inline-flex items-center justify-center rounded-md p-1 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent max-sm:h-8 max-sm:w-8 max-sm:p-0 [@media(pointer:coarse)]:h-8 [@media(pointer:coarse)]:w-8 [@media(pointer:coarse)]:p-0"
          >
            <IconX size={15} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <MemoryPanel fetchFn={fetchFn} highlight={highlight} showHeading={false} />
        </div>
      </div>
    </div>,
    document.body,
  );
}
