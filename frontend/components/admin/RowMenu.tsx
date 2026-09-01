'use client';

/**
 * The admin table's per-row "⋯" menu. Same idiom as ConversationMenu — the
 * trigger stays in the row, the panel is PORTALLED to <body> and
 * fixed-positioned (a transformed/scrolling ancestor would clip or misplace
 * an absolute menu), right-aligned with the trigger and flipped above it
 * near the bottom of the window. Escape and outside-pointerdown close it;
 * focus returns to the trigger.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { IconDots } from '@/components/icons';

const MENU_WIDTH = 208;
/** Row height estimate for the flip-above check only. */
const ITEM_HEIGHT = 36;

export interface RowMenuItem {
  id: string;
  label: string;
  icon?: ReactNode;
  danger?: boolean;
}

export function RowMenu({
  label,
  items,
  onSelect,
}: {
  /** Accessible name for the trigger, e.g. `Actions for Ada Lovelace`. */
  label: string;
  items: RowMenuItem[];
  onSelect: (id: string) => void;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(
    null,
  );

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    setPosition(null);
    if (refocus) triggerRef.current?.focus({ preventScroll: true });
  }, []);

  function openMenu() {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const estimated = items.length * ITEM_HEIGHT + 8;
    const below = rect.bottom + 4;
    const top =
      below + estimated > window.innerHeight && rect.top - estimated - 4 > 0
        ? rect.top - estimated - 4
        : below;
    setPosition({
      top,
      left: Math.max(8, rect.right - MENU_WIDTH),
    });
    setOpen(true);
  }

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close(true);
      }
    }
    function onPointerDown(e: PointerEvent) {
      const target = e.target as Node;
      if (menuRef.current?.contains(target)) return;
      if (triggerRef.current?.contains(target)) return;
      close(false);
    }
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onPointerDown);
    };
  }, [open, close]);

  if (items.length === 0) return null;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          if (open) close(true);
          else openMenu();
        }}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        title="Actions"
        className={`rounded-md p-1 text-faint transition-colors duration-ts hover:bg-border hover:text-ink ${
          open ? 'bg-border text-ink' : ''
        }`}
      >
        <IconDots size={15} />
      </button>

      {open &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label={label}
            style={{
              position: 'fixed',
              width: MENU_WIDTH,
              top: position?.top ?? 0,
              left: position?.left ?? 0,
            }}
            className="menu-pop z-50 rounded-ts border border-border bg-surface p-1 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            {items.map((item) => (
              <button
                key={item.id}
                type="button"
                role="menuitem"
                onClick={() => {
                  close(false);
                  onSelect(item.id);
                }}
                className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm transition-colors duration-ts focus:outline-none ${
                  item.danger
                    ? 'text-danger hover:bg-danger/10 focus:bg-danger/10'
                    : 'text-ink hover:bg-surface-2 focus:bg-surface-2'
                }`}
              >
                {item.icon && (
                  <span
                    className={item.danger ? 'text-danger' : 'text-muted'}
                    aria-hidden
                  >
                    {item.icon}
                  </span>
                )}
                {item.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
