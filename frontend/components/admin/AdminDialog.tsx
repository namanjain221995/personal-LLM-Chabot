'use client';

/**
 * The admin area's modal shell — ConfirmDialog's portal pattern (fixed
 * z-[70] backdrop over bg-black/60, bordered bg-surface panel, Escape and
 * backdrop-click close) with a title row and free-form body, for the forms
 * ConfirmDialog's two-button shape cannot hold (invite, change role, reset
 * password). Portalled to <body>: a transformed ancestor would become the
 * containing block for position:fixed and misplace the dialog.
 */

import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { IconX } from '@/components/icons';

export function AdminDialog({
  open,
  title,
  onClose,
  size = 'sm',
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  /** 'md' for dialogs holding a settings list rather than a short form. */
  size?: 'sm' | 'md';
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    // Focus the panel so Escape works immediately without stealing the
    // first field's focus styling; fields are one Tab away.
    panelRef.current?.focus({ preventScroll: true });
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open || typeof document === 'undefined') return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className={`menu-pop w-full rounded-ts border border-border bg-surface p-4 shadow-2xl focus:outline-none ${
          size === 'md' ? 'max-w-lg' : 'max-w-sm'
        }`}
      >
        <div className="flex items-start justify-between gap-3">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            className="-m-1 rounded-lg p-1 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconX size={15} />
          </button>
        </div>
        <div className="mt-3">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

/** The label + field pattern the admin forms share. */
export function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-muted">{label}</span>
      {children}
    </label>
  );
}

export const FIELD_INPUT =
  'mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none';

export const PRIMARY_BUTTON =
  'inline-flex items-center gap-2 rounded-md bg-accent-strong px-4 py-2 text-sm font-medium text-white transition-all duration-ts hover:brightness-110 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-35';

export const SECONDARY_BUTTON =
  'rounded-lg border border-border px-3 py-1.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink';
