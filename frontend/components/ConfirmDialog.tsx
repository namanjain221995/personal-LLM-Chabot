'use client';

/**
 * Small confirmation modal for destructive actions.
 *
 * Portalled to <body> — a transformed ancestor would otherwise become the
 * containing block for position:fixed and both mis-place the dialog and paint
 * it behind the thread (the bug that hit the ⋯ menu and the diagram viewer).
 */

import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { IconAlert } from './icons';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = 'Delete',
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Escape cancels; focus lands on Cancel so a stray Enter is harmless.
  useEffect(() => {
    if (!open) return;
    cancelRef.current?.focus({ preventScroll: true });
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCancel();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onCancel]);

  if (!open || typeof document === 'undefined') return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4"
      onClick={onCancel}
    >
      <div
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-sm rounded-ts border border-border bg-surface p-4 shadow-2xl"
      >
        <div className="flex items-start gap-3">
          <IconAlert size={18} className="mt-0.5 shrink-0 text-danger" />
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-ink">{title}</h2>
            <p className="mt-1 text-sm text-muted">{body}</p>
          </div>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-white transition-opacity duration-ts hover:opacity-90"
            style={{ background: 'var(--ts-danger)' }}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
