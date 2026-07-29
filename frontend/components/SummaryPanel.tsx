'use client';

/**
 * Read-only view of what the assistant still remembers about the compacted
 * part of a conversation.
 *
 * Compaction is otherwise invisible — the user sees a notice saying older
 * messages were summarized, with no way to check what survived. This makes
 * that inspectable, which matters because the summary is what the model will
 * answer from once the original turns are out of the window.
 */

import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { IconFileText, IconX } from './icons';

interface SummaryPanelProps {
  conversationId: string | null;
  open: boolean;
  onClose: () => void;
}

export function SummaryPanel({
  conversationId,
  open,
  onClose,
}: SummaryPanelProps) {
  const [state, setState] = useState<
    { kind: 'loading' } | { kind: 'ready'; summary: string | null } | { kind: 'error' }
  >({ kind: 'loading' });

  useEffect(() => {
    if (!open || !conversationId) return;
    let cancelled = false;
    setState({ kind: 'loading' });
    void (async () => {
      try {
        const res = await fetch(
          `/api/history/conversations/${encodeURIComponent(conversationId)}/summary`,
          { cache: 'no-store' },
        );
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as { summary?: string | null };
        if (!cancelled) {
          setState({ kind: 'ready', summary: body.summary ?? null });
        }
      } catch {
        if (!cancelled) setState({ kind: 'error' });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, conversationId]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open || typeof document === 'undefined') return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[65] flex items-start justify-center bg-black/70 p-4 pt-[8vh]"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Conversation summary"
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[70vh] w-full max-w-2xl flex-col rounded-ts border border-border bg-surface shadow-2xl"
      >
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <IconFileText size={15} className="shrink-0 text-muted" />
          <h2 className="flex-1 text-sm font-semibold">
            What the assistant remembers
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close summary"
            className="rounded-md p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconX size={15} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {state.kind === 'loading' && (
            <p className="text-sm text-faint">Loading…</p>
          )}
          {state.kind === 'error' && (
            <p className="text-sm text-danger">
              Couldn&apos;t load the summary for this conversation.
            </p>
          )}
          {state.kind === 'ready' && !state.summary && (
            <p className="text-sm text-muted">
              Nothing has been compacted yet — the assistant still sees every
              message in this chat exactly as written.
            </p>
          )}
          {state.kind === 'ready' && state.summary && (
            <pre className="whitespace-pre-wrap break-words font-sans text-[13px] leading-relaxed text-ink">
              {state.summary}
            </pre>
          )}
        </div>
        <p className="border-t border-border px-4 py-2 text-[11px] text-faint">
          Older messages were summarized to free up space. This summary is what
          the model sees in place of them — it is read-only.
        </p>
      </div>
    </div>,
    document.body,
  );
}
