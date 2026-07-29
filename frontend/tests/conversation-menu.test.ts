/**
 * V3 §2 — conversation "⋯" menu behavior.
 *
 * The menu's decisions live in lib/conversationMenu.ts as pure functions
 * (components/ConversationMenu.tsx only renders them), so the item list,
 * the store calls behind each item, the delete confirm step, the keyboard
 * map and the flip-up placement are all covered here without a DOM.
 */

import { describe, expect, it, vi } from 'vitest';
import {
  activateMenuItem,
  conversationMenuHandlers,
  conversationMenuItems,
  menuKeyAction,
  placeMenu,
  type ConversationMenuActions,
} from '../lib/conversationMenu';

/** A stand-in for the history store, wired exactly as the sidebar wires it. */
function makeActions() {
  return {
    rename: vi.fn(),
    setPinned: vi.fn(),
    setArchived: vi.fn(),
    exportChat: vi.fn(),
    remove: vi.fn(),
  } satisfies ConversationMenuActions;
}

describe('menu items (V3 §2)', () => {
  it('lists Rename · Pin · Archive · Export · Delete in that order', () => {
    const items = conversationMenuItems({ pinned: false, archived: false });
    expect(items.map((i) => i.id)).toEqual([
      'rename',
      'pin',
      'archive',
      'export',
      'delete',
    ]);
    expect(items.map((i) => i.label)).toEqual([
      'Rename',
      'Pin chat',
      'Archive',
      'Export chat',
      'Delete',
    ]);
    expect(items.at(-1)?.danger).toBe(true);
  });

  it('reflects pinned / archived state in the labels', () => {
    const items = conversationMenuItems({ pinned: true, archived: true });
    expect(items.map((i) => i.label)).toContain('Unpin chat');
    expect(items.map((i) => i.label)).toContain('Unarchive');
  });

  it('has no dead "Move to project" item (out of scope for V3)', () => {
    const labels = conversationMenuItems({
      pinned: false,
      archived: false,
    }).map((i) => i.label.toLowerCase());
    expect(labels.some((l) => l.includes('project'))).toBe(false);
  });
});

describe('menu actions reach the store (V3 §2)', () => {
  it('rename opens the inline editor for this conversation', () => {
    const actions = makeActions();
    const handlers = conversationMenuHandlers({ id: 'c1' }, actions);
    expect(activateMenuItem('rename', handlers)).toEqual({ kind: 'close' });
    expect(actions.rename).toHaveBeenCalledWith('c1');
  });

  it('pin/unpin toggles setPinned with the opposite of the current state', () => {
    const actions = makeActions();
    activateMenuItem(
      'pin',
      conversationMenuHandlers({ id: 'c1', pinned: false }, actions),
    );
    expect(actions.setPinned).toHaveBeenCalledWith('c1', true);

    activateMenuItem(
      'pin',
      conversationMenuHandlers({ id: 'c2', pinned: true }, actions),
    );
    expect(actions.setPinned).toHaveBeenLastCalledWith('c2', false);
  });

  it('archive/unarchive toggles setArchived the same way', () => {
    const actions = makeActions();
    activateMenuItem(
      'archive',
      conversationMenuHandlers({ id: 'c1', archived: false }, actions),
    );
    expect(actions.setArchived).toHaveBeenCalledWith('c1', true);

    activateMenuItem(
      'archive',
      conversationMenuHandlers({ id: 'c1', archived: true }, actions),
    );
    expect(actions.setArchived).toHaveBeenLastCalledWith('c1', false);
  });

  it('export asks for this conversation only', () => {
    const actions = makeActions();
    activateMenuItem(
      'export',
      conversationMenuHandlers({ id: 'c9' }, actions),
    );
    expect(actions.exportChat).toHaveBeenCalledWith('c9');
    expect(actions.remove).not.toHaveBeenCalled();
  });
});

describe('delete needs the confirm step (V3 §2)', () => {
  it('does not delete on the first activation — it arms the confirm row', () => {
    const actions = makeActions();
    const handlers = conversationMenuHandlers({ id: 'c1' }, actions);
    expect(activateMenuItem('delete', handlers)).toEqual({
      kind: 'confirm-delete',
    });
    expect(actions.remove).not.toHaveBeenCalled();

    const confirming = conversationMenuItems(
      { pinned: false, archived: false },
      true,
    );
    expect(confirming.map((i) => i.id)).toEqual([
      'delete-confirm',
      'delete-cancel',
    ]);
  });

  it('deletes only after the confirm item', () => {
    const actions = makeActions();
    const handlers = conversationMenuHandlers({ id: 'c1' }, actions);
    activateMenuItem('delete', handlers);
    expect(activateMenuItem('delete-confirm', handlers)).toEqual({
      kind: 'close',
    });
    expect(actions.remove).toHaveBeenCalledWith('c1');
  });

  it('cancel keeps the menu open and destroys nothing', () => {
    const actions = makeActions();
    const handlers = conversationMenuHandlers({ id: 'c1' }, actions);
    activateMenuItem('delete', handlers);
    expect(activateMenuItem('delete-cancel', handlers)).toEqual({
      kind: 'cancel-delete',
    });
    expect(actions.remove).not.toHaveBeenCalled();
  });
});

describe('keyboard navigation (V3 §2)', () => {
  it('Escape closes (the component then returns focus to the trigger)', () => {
    expect(menuKeyAction('Escape', 0, 5)).toEqual({ kind: 'close' });
    expect(menuKeyAction('Tab', 2, 5)).toEqual({ kind: 'close' });
    // …even with nothing focusable left.
    expect(menuKeyAction('Escape', 0, 0)).toEqual({ kind: 'close' });
  });

  it('arrows move focus and wrap at both ends', () => {
    expect(menuKeyAction('ArrowDown', 0, 5)).toEqual({ kind: 'move', index: 1 });
    expect(menuKeyAction('ArrowDown', 4, 5)).toEqual({ kind: 'move', index: 0 });
    expect(menuKeyAction('ArrowUp', 0, 5)).toEqual({ kind: 'move', index: 4 });
    expect(menuKeyAction('ArrowUp', 3, 5)).toEqual({ kind: 'move', index: 2 });
  });

  it('Home / End jump to the ends and Enter activates', () => {
    expect(menuKeyAction('Home', 3, 5)).toEqual({ kind: 'move', index: 0 });
    expect(menuKeyAction('End', 1, 5)).toEqual({ kind: 'move', index: 4 });
    expect(menuKeyAction('Enter', 1, 5)).toEqual({ kind: 'activate' });
    expect(menuKeyAction(' ', 1, 5)).toEqual({ kind: 'activate' });
  });

  it('leaves unrelated keys alone', () => {
    expect(menuKeyAction('a', 0, 5)).toBeNull();
    expect(menuKeyAction('ArrowLeft', 0, 5)).toBeNull();
  });
});

describe('placement never clips (V3 §2)', () => {
  const menu = { width: 208, height: 200 };
  const viewport = { width: 1280, height: 800 };

  it('drops below the trigger when there is room', () => {
    const pos = placeMenu(
      { top: 120, left: 220, width: 24, height: 24 },
      menu,
      viewport,
    );
    expect(pos.placement).toBe('below');
    expect(pos.top).toBe(150);
  });

  it('flips above for a row near the bottom of the sidebar', () => {
    const pos = placeMenu(
      { top: 740, left: 220, width: 24, height: 24 },
      menu,
      viewport,
    );
    expect(pos.placement).toBe('above');
    expect(pos.top).toBe(534);
    expect(pos.top).toBeGreaterThanOrEqual(8);
  });

  it('right-aligns with the trigger so a 260px sidebar cannot push it out', () => {
    const pos = placeMenu(
      { top: 120, left: 228, width: 24, height: 24 },
      menu,
      viewport,
    );
    expect(pos.left).toBe(44); // 228 + 24 - 208
    expect(pos.left).toBeGreaterThanOrEqual(8);
  });

  it('stays on screen when neither side fits', () => {
    const tall = { width: 208, height: 780 };
    const pos = placeMenu(
      { top: 400, left: 40, width: 24, height: 24 },
      tall,
      viewport,
    );
    expect(pos.top).toBeGreaterThanOrEqual(8);
    expect(pos.top + tall.height).toBeLessThanOrEqual(viewport.height);
    expect(pos.left).toBeGreaterThanOrEqual(8);
  });
});
