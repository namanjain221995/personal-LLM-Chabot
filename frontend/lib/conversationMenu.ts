/**
 * Headless model for the sidebar conversation "⋯" menu (V3 §2).
 *
 * Every decision the menu makes — which items exist, what activating one
 * does, where the keyboard moves focus, and where the popover is placed so
 * it never clips out of the sidebar — lives here as a pure function, so the
 * behavior is unit-tested in the node environment vitest runs in.
 * `components/ConversationMenu.tsx` is then a thin rendering shell over it.
 */

export type ConversationMenuItemId =
  | 'rename'
  | 'pin'
  | 'archive'
  | 'export'
  | 'delete'
  | 'delete-confirm'
  | 'delete-cancel';

export interface ConversationMenuItem {
  id: ConversationMenuItemId;
  label: string;
  /** Danger items render in --ts-danger. */
  danger?: boolean;
}

export interface ConversationMenuFlags {
  pinned: boolean;
  archived: boolean;
}

/**
 * Menu items in display order (V3 §2): Rename · Pin/Unpin · Archive/
 * Unarchive · Export chat · Delete. While `confirmingDelete` is set the
 * Delete row is replaced by an inline "Delete? / Cancel" pair so a misclick
 * can never destroy a conversation. ChatGPT's "Move to project" is
 * deliberately absent — V3 has no projects concept and a dead item is worse
 * than no item.
 */
export function conversationMenuItems(
  flags: ConversationMenuFlags,
  confirmingDelete = false,
): ConversationMenuItem[] {
  if (confirmingDelete) {
    return [
      { id: 'delete-confirm', label: 'Delete', danger: true },
      { id: 'delete-cancel', label: 'Cancel' },
    ];
  }
  return [
    { id: 'rename', label: 'Rename' },
    { id: 'pin', label: flags.pinned ? 'Unpin chat' : 'Pin chat' },
    { id: 'archive', label: flags.archived ? 'Unarchive' : 'Archive' },
    { id: 'export', label: 'Export chat' },
    { id: 'delete', label: 'Delete', danger: true },
  ];
}

export interface ConversationMenuHandlers {
  onRename(): void;
  onTogglePin(): void;
  onToggleArchive(): void;
  onExport(): void;
  onDelete(): void;
}

/** The store-facing side of the menu, as the sidebar wires it up. */
export interface ConversationMenuActions {
  /** Switch the row into the existing inline-rename editor. */
  rename(id: string): void;
  setPinned(id: string, pinned: boolean): void;
  setArchived(id: string, archived: boolean): void;
  exportChat(id: string): void;
  remove(id: string): void;
}

/**
 * Binds one conversation row to those actions. Keeping the toggle polarity
 * here (rather than inline in the sidebar) is what makes "Pin chat pins and
 * Unpin chat unpins" a unit-testable claim.
 */
export function conversationMenuHandlers(
  conversation: { id: string; pinned?: boolean; archived?: boolean },
  actions: ConversationMenuActions,
): ConversationMenuHandlers {
  const { id } = conversation;
  return {
    onRename: () => actions.rename(id),
    onTogglePin: () => actions.setPinned(id, conversation.pinned !== true),
    onToggleArchive: () =>
      actions.setArchived(id, conversation.archived !== true),
    onExport: () => actions.exportChat(id),
    onDelete: () => actions.remove(id),
  };
}

/** What the menu should do after an item was activated. */
export type ConversationMenuOutcome =
  /** Run the action and dismiss the popover. */
  | { kind: 'close' }
  /** Stay open and swap in the inline confirm row. */
  | { kind: 'confirm-delete' }
  /** Stay open and go back to the normal item list. */
  | { kind: 'cancel-delete' };

/**
 * Runs the handler behind `id` and reports what the popover should do next.
 * "delete" NEVER deletes — it only arms the confirm step.
 */
export function activateMenuItem(
  id: ConversationMenuItemId,
  handlers: ConversationMenuHandlers,
): ConversationMenuOutcome {
  switch (id) {
    case 'rename':
      handlers.onRename();
      return { kind: 'close' };
    case 'pin':
      handlers.onTogglePin();
      return { kind: 'close' };
    case 'archive':
      handlers.onToggleArchive();
      return { kind: 'close' };
    case 'export':
      handlers.onExport();
      return { kind: 'close' };
    case 'delete':
      return { kind: 'confirm-delete' };
    case 'delete-confirm':
      handlers.onDelete();
      return { kind: 'close' };
    case 'delete-cancel':
      return { kind: 'cancel-delete' };
  }
}

export type MenuKeyAction =
  /** Move roving focus to `index`. */
  | { kind: 'move'; index: number }
  /** Dismiss and return focus to the trigger. */
  | { kind: 'close' }
  /** Activate the focused item. */
  | { kind: 'activate' };

/**
 * WAI-ARIA menu keyboard map: Arrow up/down wrap, Home/End jump to the
 * ends, Escape and Tab close, Enter/Space activate. Returns null for keys
 * the menu does not own (so they keep their default behavior).
 */
export function menuKeyAction(
  key: string,
  current: number,
  count: number,
): MenuKeyAction | null {
  if (key === 'Escape' || key === 'Tab') return { kind: 'close' };
  if (count === 0) return null;
  switch (key) {
    case 'ArrowDown':
      return { kind: 'move', index: (current + 1 + count) % count };
    case 'ArrowUp':
      return { kind: 'move', index: (current - 1 + count) % count };
    case 'Home':
      return { kind: 'move', index: 0 };
    case 'End':
      return { kind: 'move', index: count - 1 };
    case 'Enter':
    case ' ':
      return { kind: 'activate' };
    default:
      return null;
  }
}

/* ------------------------------------------------------------ placement */

export interface MenuRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

export interface MenuSize {
  width: number;
  height: number;
}

export interface MenuViewport {
  width: number;
  height: number;
}

export interface MenuPosition {
  top: number;
  left: number;
  placement: 'below' | 'above';
}

/**
 * Places the popover in viewport (fixed) coordinates.
 *
 * The menu is right-aligned with the trigger so a 260px sidebar can never
 * push it off-screen horizontally, and it FLIPS ABOVE the trigger when the
 * row sits near the bottom of the window — the sidebar list scrolls, so the
 * last conversation is exactly where a naive "always below" menu would be
 * clipped. Everything is finally clamped into the viewport with `margin`.
 */
export function placeMenu(
  trigger: MenuRect,
  menu: MenuSize,
  viewport: MenuViewport,
  gap = 6,
  margin = 8,
): MenuPosition {
  const below = trigger.top + trigger.height + gap;
  const above = trigger.top - menu.height - gap;
  const fitsBelow = below + menu.height <= viewport.height - margin;
  const fitsAbove = above >= margin;
  const placement: MenuPosition['placement'] =
    fitsBelow || !fitsAbove ? 'below' : 'above';

  const clamp = (value: number, max: number) =>
    Math.max(margin, Math.min(value, Math.max(margin, max)));

  return {
    placement,
    top: clamp(
      placement === 'below' ? below : above,
      viewport.height - menu.height - margin,
    ),
    left: clamp(
      trigger.left + trigger.width - menu.width,
      viewport.width - menu.width - margin,
    ),
  };
}
