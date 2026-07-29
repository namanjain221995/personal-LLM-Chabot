'use client';

/**
 * Conversation "⋯" menu (V3 §2) — the ChatGPT row menu: Rename · Pin/Unpin ·
 * Archive/Unarchive · Export chat · Delete (with an inline confirm step so a
 * misclick cannot destroy a conversation).
 *
 * Every decision — item list, what an item does, keyboard map, popover
 * placement — comes from the pure helpers in lib/conversationMenu.ts; this
 * file is the rendering shell. Styling mirrors components/ModelPicker.tsx
 * (the app's other popover): surface panel, 1px border, 10px radius, xl
 * shadow, surface-2 hover.
 *
 * The popover is FIXED-positioned: the sidebar list is a scroll container,
 * so an absolutely-positioned menu would be clipped at the list edges. It
 * stays inside the same React tree (no portal), right-aligned with the
 * trigger and flipped above it near the bottom of the window.
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { createPortal } from 'react-dom';
import {
  activateMenuItem,
  conversationMenuItems,
  menuKeyAction,
  placeMenu,
  type ConversationMenuItemId,
  type MenuPosition,
} from '@/lib/conversationMenu';
import {
  IconArchive,
  IconCheck,
  IconDots,
  IconDownload,
  IconPencil,
  IconPin,
  IconPinOff,
  IconTrash,
  IconUnarchive,
  IconX,
} from './icons';

const MENU_WIDTH = 208;

/** useLayoutEffect warns during SSR; the measurement is client-only anyway. */
const useMeasureEffect =
  typeof window === 'undefined' ? useEffect : useLayoutEffect;

/** Focus lands on Cancel, never on the destructive button. */
const CONFIRM_FOCUS_INDEX = 1;

export interface ConversationMenuProps {
  /** Conversation title — used for the trigger's accessible name. */
  title: string;
  pinned: boolean;
  archived: boolean;
  /** The row is the open conversation: keep the trigger permanently visible. */
  active?: boolean;
  onRename: () => void;
  onTogglePin: () => void;
  onToggleArchive: () => void;
  onExport: () => void;
  onDelete: () => void;
  /** Lets the row keep its hover affordances while the menu is open. */
  onOpenChange?: (open: boolean) => void;
}

function itemIcon(
  id: ConversationMenuItemId,
  pinned: boolean,
  archived: boolean,
) {
  switch (id) {
    case 'rename':
      return <IconPencil size={14} />;
    case 'pin':
      return pinned ? <IconPinOff size={14} /> : <IconPin size={14} />;
    case 'archive':
      return archived ? <IconUnarchive size={14} /> : <IconArchive size={14} />;
    case 'export':
      return <IconDownload size={14} />;
    case 'delete':
      return <IconTrash size={14} />;
    case 'delete-confirm':
      return <IconCheck size={14} />;
    case 'delete-cancel':
      return <IconX size={14} />;
  }
}

export function ConversationMenu({
  title,
  pinned,
  archived,
  active = false,
  onRename,
  onTogglePin,
  onToggleArchive,
  onExport,
  onDelete,
  onOpenChange,
}: ConversationMenuProps) {
  const [open, setOpen] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const [position, setPosition] = useState<MenuPosition | null>(null);

  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const items = conversationMenuItems({ pinned, archived }, confirmingDelete);

  const close = useCallback(
    (restoreFocus: boolean) => {
      setOpen(false);
      setConfirmingDelete(false);
      setPosition(null);
      onOpenChange?.(false);
      if (restoreFocus) triggerRef.current?.focus();
    },
    [onOpenChange],
  );

  function openMenu() {
    setConfirmingDelete(false);
    setFocusIndex(0);
    setPosition(null);
    setOpen(true);
    onOpenChange?.(true);
  }

  // Place the popover from real measurements, before paint so it never
  // flashes at the wrong spot. Re-runs when the item list changes height
  // (the confirm step is shorter than the full menu).
  useMeasureEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger || !menu) return;
    const rect = trigger.getBoundingClientRect();
    setPosition(
      placeMenu(
        {
          top: rect.top,
          left: rect.left,
          width: rect.width,
          height: rect.height,
        },
        { width: MENU_WIDTH, height: menu.offsetHeight },
        { width: window.innerWidth, height: window.innerHeight },
      ),
    );
  }, [open, items.length]);

  // Outside click / Escape / anything that moves the trigger closes the menu
  // — same contract as the ModelPicker popover.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      const target = e.target as Node;
      if (
        menuRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      ) {
        return;
      }
      close(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') close(true);
    }
    function onViewportChange(e?: Event) {
      // Ignore scrolls that come from inside the menu itself — focusing an
      // item can make the browser scroll an ancestor, and closing on that
      // would slam the menu shut the instant it opens.
      const target = e?.target as Node | undefined;
      if (target && menuRef.current?.contains(target)) return;
      close(false);
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('resize', onViewportChange);
    // Capture phase: the sidebar list scrolls, and it is not the window.
    window.addEventListener('scroll', onViewportChange, true);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('resize', onViewportChange);
      window.removeEventListener('scroll', onViewportChange, true);
    };
  }, [open, close]);

  // Roving focus: the focused item is the only tab stop.
  useEffect(() => {
    if (!open || !position) return;
    // preventScroll: the menu is position:fixed, so scrolling an ancestor to
    // "reveal" it is both pointless and would trip the close-on-scroll guard.
    itemRefs.current[focusIndex]?.focus({ preventScroll: true });
  }, [open, position, focusIndex, confirmingDelete]);

  function activate(id: ConversationMenuItemId) {
    const outcome = activateMenuItem(id, {
      onRename,
      onTogglePin,
      onToggleArchive,
      onExport,
      onDelete,
    });
    if (outcome.kind === 'confirm-delete') {
      setConfirmingDelete(true);
      setFocusIndex(CONFIRM_FOCUS_INDEX);
      return;
    }
    if (outcome.kind === 'cancel-delete') {
      setConfirmingDelete(false);
      setFocusIndex(0);
      return;
    }
    // Rename hands focus to the row's inline editor and Delete removes the
    // row entirely — pulling focus back to a gone/stale trigger would fight
    // both, so only the harmless items restore it.
    close(id !== 'rename' && id !== 'delete-confirm');
  }

  function onMenuKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    const action = menuKeyAction(e.key, focusIndex, items.length);
    if (!action) return;
    if (action.kind === 'close') {
      if (e.key !== 'Tab') e.preventDefault();
      close(true);
      return;
    }
    if (action.kind === 'move') {
      e.preventDefault();
      setFocusIndex(action.index);
      return;
    }
    // Enter/Space activate the focused item; native buttons already do that,
    // so nothing is needed here beyond keeping the page from scrolling.
    if (e.key === ' ') e.preventDefault();
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => (open ? close(true) : openMenu())}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Options for conversation: ${title}`}
        title="Options"
        className={`rounded-md p-1 text-faint transition-colors duration-ts hover:bg-border hover:text-ink focus-visible:opacity-100 ${
          open || active
            ? 'opacity-100'
            : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'
        } ${open ? 'bg-border text-ink' : ''}`}
      >
        <IconDots size={15} />
      </button>

      {/* Portalled to <body>: the sidebar row wraps this in a transformed
          element, and a transformed ancestor becomes the containing block for
          position:fixed — which both offset the menu and painted it behind
          the thread column. The portal escapes that containing block and the
          sidebar's stacking context. */}
      {open &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label={`Conversation options: ${title}`}
            onKeyDown={onMenuKeyDown}
            style={{
              position: 'fixed',
              width: MENU_WIDTH,
              top: position?.top ?? 0,
              left: position?.left ?? 0,
              visibility: position ? 'visible' : 'hidden',
            }}
            className="menu-pop z-50 rounded-ts border border-border bg-surface p-1 shadow-xl"
          >
            {confirmingDelete && (
              <p className="px-2.5 pb-1 pt-1.5 text-xs text-muted">
                Delete this chat?
              </p>
            )}
            {items.map((item, index) => (
              <button
                key={item.id}
                ref={(el) => {
                  itemRefs.current[index] = el;
                }}
                type="button"
                role="menuitem"
                tabIndex={index === focusIndex ? 0 : -1}
                onClick={() => activate(item.id)}
                onMouseEnter={() => setFocusIndex(index)}
                className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm transition-colors duration-ts focus:outline-none ${
                  item.danger
                    ? 'text-danger hover:bg-danger/10 focus:bg-danger/10'
                    : 'text-ink hover:bg-surface-2 focus:bg-surface-2'
                }`}
              >
                <span
                  className={item.danger ? 'text-danger' : 'text-muted'}
                  aria-hidden
                >
                  {itemIcon(item.id, pinned, archived)}
                </span>
                {item.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
