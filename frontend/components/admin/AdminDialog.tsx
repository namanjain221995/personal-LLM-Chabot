'use client';

/**
 * The admin area's modal shell — ConfirmDialog's portal pattern (fixed
 * z-[70] backdrop over bg-black/60, bordered bg-surface panel, Escape and
 * backdrop-click close) with a title row and free-form body, for the forms
 * ConfirmDialog's two-button shape cannot hold (invite, change role, reset
 * password). Portalled to <body>: a transformed ancestor would become the
 * containing block for position:fixed and misplace the dialog.
 *
 * TALLER THAN THE WINDOW (responsive audit, 2026-09-13). The Manage access
 * dialog is ~1225px of switches; centred in a 900px window with no height
 * cap, its title row (and the close button) sat above the top of the screen
 * and Cancel / Save access below the bottom, out of reach of mouse, touch
 * and wheel alike. The panel is now capped to the overlay's height and only
 * the BODY scrolls, so the title and close button never leave the screen;
 * a body that ends in a button row can pin it with `DIALOG_FOOTER`.
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
        data-testid="admin-dialog-panel"
        className={`menu-pop flex max-h-full w-full flex-col rounded-ts border border-border bg-surface p-4 shadow-2xl focus:outline-none ${
          size === 'md' ? 'max-w-lg' : 'max-w-sm'
        }`}
      >
        <div className="flex shrink-0 items-start justify-between gap-3">
          <h2 className="min-w-0 text-sm font-semibold text-ink [overflow-wrap:anywhere]">
            {title}
          </h2>
          {/* 32px hit area; the negative margin keeps the glyph where the
              23px button used to put it. */}
          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            className="-m-2 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconX size={15} />
          </button>
        </div>
        {/* -mx-1 px-1: room for the focus rings of full-width controls,
            which `overflow-y-auto` would otherwise clip at the sides. */}
        <div
          data-testid="admin-dialog-body"
          className="-mx-1 mt-3 min-h-0 overflow-y-auto px-1"
        >
          {children}
        </div>
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

/**
 * A dialog's button row that stays in view while a long body scrolls under
 * it — the Manage access dialog's Cancel / Save access. `bg-surface` so the
 * rows scrolling beneath do not show through.
 */
export const DIALOG_FOOTER =
  'sticky bottom-0 -mx-1 mt-4 flex justify-end gap-2 bg-surface px-1 pb-0.5 pt-3';

export const SECONDARY_BUTTON =
  'rounded-lg border border-border px-3 py-1.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink';
