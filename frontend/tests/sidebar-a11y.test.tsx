// @vitest-environment jsdom
/**
 * The sidebar's keyboard and screen-reader behaviour.
 *
 * The panel is rendered TWICE — the desktop column and the mobile drawer — and
 * CSS alone decides which one a given viewport shows. jsdom loads no CSS, so
 * every test here sees both copies at once. That is not a limitation to work
 * around: it is exactly the condition that produced all three bugs below, so
 * the tests meet them head on.
 *
 * M-12 — both copies used the same literal ids, so `sidebar-pinned` and
 *        friends appeared twice and every aria-labelledby pointed at whichever
 *        the document happened to hold first.
 * L-02 — the collapsed desktop column is `w-0`, not unmounted, so its controls
 *        stayed in the tab order behind a zero-width edge.
 * L-03 — the drawer looked modal and behaved like a plain div: no dialog role,
 *        no focus trap, no Escape, no way back to the control that opened it.
 */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useRef, useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Sidebar } from '@/components/Sidebar';
import { focusableWithin } from '@/lib/focusTrap';
import { NEW_CHAT_SHORTCUT_LABEL } from '@/lib/searchPalette';
import type { ConversationSummary } from '@/lib/types';

afterEach(cleanup);

function conv(
  id: string,
  title: string,
  over: Partial<ConversationSummary> = {},
): ConversationSummary {
  return { id, title, createdAt: 1, updatedAt: 1, ...over };
}

/** Pinned, recent AND archived, so every id-bearing section is rendered. */
const CONVERSATIONS = [
  conv('a', 'Pinned chat', { pinned: true }),
  conv('b', 'Recent chat'),
];
const ARCHIVED = [conv('c', 'Archived chat', { archived: true })];

const noop = () => undefined;

function renderSidebar(props: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  return render(
    <Sidebar
      open
      onClose={noop}
      conversations={CONVERSATIONS}
      archived={ARCHIVED}
      activeId="b"
      onNewChat={noop}
      onOpenSearch={noop}
      onSelect={noop}
      onRename={noop}
      onDelete={noop}
      onSetPinned={noop}
      onSetArchived={noop}
      onExport={noop}
      onLoadArchived={noop}
      {...props}
    />,
  );
}

/* --------------------------------------------------------------- M-12 */

describe('M-12 — the two copies never share an id', () => {
  it('mounts both panels and still emits no duplicate id', () => {
    const { container } = renderSidebar();

    // Guard the premise: if only one copy were mounted this test would pass
    // for the wrong reason and stop protecting anything.
    expect(container.querySelectorAll('[aria-label="Sidebar"]')).toHaveLength(2);

    const ids = Array.from(container.querySelectorAll('[id]')).map((el) => el.id);
    expect(ids.length).toBeGreaterThan(0);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('never emits the old hard-coded ids again', () => {
    const { container } = renderSidebar();
    for (const legacy of [
      'sidebar-pinned',
      'sidebar-recents',
      'sidebar-archived-list',
    ]) {
      expect(container.querySelector(`[id="${legacy}"]`)).toBeNull();
    }
  });

  it('resolves every aria reference to exactly one element in its OWN copy', () => {
    const { container } = renderSidebar();
    const panels = Array.from(
      container.querySelectorAll<HTMLElement>('[aria-label="Sidebar"]'),
    );
    expect(panels).toHaveLength(2);

    let checked = 0;
    for (const panel of panels) {
      const refs = panel.querySelectorAll('[aria-labelledby],[aria-controls]');
      for (const el of Array.from(refs)) {
        for (const attr of ['aria-labelledby', 'aria-controls'] as const) {
          const target = el.getAttribute(attr);
          if (!target) continue;
          checked += 1;
          // Unique across the whole document…
          expect(
            container.querySelectorAll(`[id="${target}"]`),
          ).toHaveLength(1);
          // …and the element it names is the one in the same panel, not the
          // twin that happens to sit earlier in the document.
          expect(panel.querySelector(`[id="${target}"]`)).not.toBeNull();
        }
      }
    }
    expect(checked).toBeGreaterThan(0);
  });

  it('does not label the Recents section with a heading it did not render', () => {
    // The Recents heading only exists when there are pinned chats above it.
    // Without any, the section used to point aria-labelledby at nothing.
    const { container } = renderSidebar({
      conversations: [conv('b', 'Recent chat')],
    });
    for (const el of Array.from(container.querySelectorAll('[aria-labelledby]'))) {
      const target = el.getAttribute('aria-labelledby') as string;
      expect(container.querySelector(`[id="${target}"]`)).not.toBeNull();
    }
  });
});

/* --------------------------------------------------------------- L-02 */

describe('L-02 — the collapsed desktop column', () => {
  /** The desktop column is the copy that is not the drawer. */
  function desktopColumn(container: HTMLElement): HTMLElement {
    const el = container.querySelector<HTMLElement>(
      'aside[aria-label="Sidebar"]:not([role="dialog"])',
    );
    if (!el) throw new Error('desktop column not found');
    return el;
  }

  it('is inert and hidden from assistive tech while collapsed', () => {
    const { container } = renderSidebar({ open: false });
    const column = desktopColumn(container);

    // jsdom has no layout and does not enforce inert, so this asserts the
    // contract with the browser: the attribute is what removes the whole
    // subtree from the tab order and the accessibility tree at once.
    expect(column.hasAttribute('inert')).toBe(true);
    expect(column.getAttribute('aria-hidden')).toBe('true');

    // The controls are still THERE — that is the point, the column animates
    // its width rather than unmounting — which is why inert has to do the work.
    expect(focusableWithin(column).length).toBeGreaterThan(0);
  });

  it('is neither inert nor aria-hidden once expanded', () => {
    const { container } = renderSidebar({ open: true });
    const column = desktopColumn(container);
    expect(column.hasAttribute('inert')).toBe(false);
    expect(column.getAttribute('aria-hidden')).not.toBe('true');
  });

  it('never leaves focusable content inside an aria-hidden subtree', () => {
    // The invalid pattern in its own right: aria-hidden over content a
    // keyboard can still reach. Asserted for both states.
    for (const open of [true, false]) {
      const { container, unmount } = renderSidebar({ open });
      for (const hidden of Array.from(
        container.querySelectorAll<HTMLElement>('[aria-hidden="true"]'),
      )) {
        if (focusableWithin(hidden).length > 0) {
          expect(hidden.hasAttribute('inert')).toBe(true);
        }
      }
      unmount();
    }
  });
});

/* --------------------------------------------------------------- L-03 */

describe('L-03 — the mobile drawer behaves like a dialog', () => {
  /**
   * The drawer as ChatApp mounts it: a header toggle that exists only while
   * the sidebar is closed, plus a control outside the drawer that the trap
   * must never hand focus to.
   */
  function Harness({ onClosed }: { onClosed?: () => void } = {}) {
    const [open, setOpen] = useState(true);
    const toggleRef = useRef<HTMLButtonElement>(null);
    return (
      <>
        <button type="button">Outside control</button>
        {!open && (
          <button
            ref={toggleRef}
            type="button"
            onClick={() => setOpen(true)}
            aria-label="Show sidebar"
          />
        )}
        <Sidebar
          open={open}
          onClose={() => {
            setOpen(false);
            onClosed?.();
          }}
          conversations={CONVERSATIONS}
          archived={ARCHIVED}
          activeId="b"
          onNewChat={noop}
          onOpenSearch={noop}
          onSelect={noop}
          onRename={noop}
          onDelete={noop}
          onSetPinned={noop}
          onSetArchived={noop}
          onExport={noop}
          onLoadArchived={noop}
          restoreFocusRef={toggleRef}
        />
      </>
    );
  }

  it('carries dialog semantics and an accessible name', () => {
    renderSidebar();
    const dialog = screen.getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.getAttribute('aria-label')).toBe('Sidebar');
  });

  it('moves focus into the drawer when it opens', () => {
    renderSidebar();
    const dialog = screen.getByRole('dialog');
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it('advertises the shortcut the keyboard map actually implements', () => {
    renderSidebar();
    const dialog = screen.getByRole('dialog');
    expect(
      within(dialog).getByText(NEW_CHAT_SHORTCUT_LABEL),
    ).toBeTruthy();
  });

  it('still opens a new chat when the button is clicked', () => {
    // The chord changed; the pointer path must not have.
    const onNewChat = vi.fn();
    renderSidebar({ onNewChat });
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /New chat/ }));
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });

  it('has no file input to trigger by accident', () => {
    // NEW-08 was reported as the shortcut opening a file picker. Nothing in
    // the sidebar can pick a file at all — attachments live in the composer —
    // so a regression here would mean the shortcut is reaching the browser.
    const { container } = renderSidebar();
    expect(container.querySelector('input[type="file"]')).toBeNull();
  });

  it('keeps Tab inside the drawer, wrapping at the end', () => {
    renderSidebar();
    const dialog = screen.getByRole('dialog');
    const stops = focusableWithin(dialog);
    expect(stops.length).toBeGreaterThan(1);

    // Tab from the container enters at the first stop…
    fireEvent.keyDown(dialog, { key: 'Tab' });
    expect(document.activeElement).toBe(stops[0]);

    // …and from the last one it wraps back to the first rather than escaping
    // to the page behind.
    stops[stops.length - 1].focus();
    fireEvent.keyDown(stops[stops.length - 1], { key: 'Tab' });
    expect(document.activeElement).toBe(stops[0]);
  });

  it('keeps Shift+Tab inside the drawer, wrapping at the start', () => {
    renderSidebar();
    const dialog = screen.getByRole('dialog');
    const stops = focusableWithin(dialog);

    stops[0].focus();
    fireEvent.keyDown(stops[0], { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(stops[stops.length - 1]);

    // From the container, Shift+Tab enters at the END — otherwise the first
    // keystroke after opening walks the wrong way.
    dialog.focus();
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(stops[stops.length - 1]);
  });

  it('never hands focus to a control behind the drawer', () => {
    render(<Harness />);
    const dialog = screen.getByRole('dialog');
    const outside = screen.getByRole('button', { name: 'Outside control' });
    const stops = focusableWithin(dialog);

    // Walk a full cycle and then some; the trap must never land outside.
    let cursor: HTMLElement = dialog;
    for (let i = 0; i < stops.length + 3; i += 1) {
      fireEvent.keyDown(cursor, { key: 'Tab' });
      cursor = document.activeElement as HTMLElement;
      expect(cursor).not.toBe(outside);
      expect(dialog.contains(cursor)).toBe(true);
    }
  });

  it('is not reachable through the backdrop, which is not a tab stop', () => {
    const { container } = renderSidebar();
    const backdrop = container.querySelector<HTMLElement>(
      '.fixed > button[aria-hidden="true"]',
    );
    expect(backdrop).not.toBeNull();
    expect(backdrop?.getAttribute('tabindex')).toBe('-1');
  });

  it('closes on Escape', () => {
    const onClose = vi.fn();
    renderSidebar({ onClose });
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not let that Escape reach the window-level shortcut map', () => {
    // Behind the drawer, an Escape that keeps travelling reads as "stop the
    // generation" — a destructive action nobody asked for.
    const onWindowKey = vi.fn();
    window.addEventListener('keydown', onWindowKey);
    try {
      renderSidebar({ onClose: noop });
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
      expect(onWindowKey).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('keydown', onWindowKey);
    }
  });

  it('leaves Escape to an inner layer that owns it', () => {
    // A row's ⋯ menu portals its popup out of the drawer but still bubbles
    // through the React tree to the drawer's handler. Escape belongs to the
    // innermost layer, so the drawer must ignore it.
    const onClose = vi.fn();
    renderSidebar({ onClose });
    const outside = document.createElement('button');
    document.body.appendChild(outside);
    try {
      fireEvent.keyDown(outside, { key: 'Escape' });
      expect(onClose).not.toHaveBeenCalled();
    } finally {
      outside.remove();
    }
  });

  it('returns focus to the control that opened it', () => {
    render(<Harness />);
    const dialog = screen.getByRole('dialog');
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(dialog, { key: 'Escape' });

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByRole('button', { name: 'Show sidebar' }),
    );
  });

  it('does not steal focus when the drawer never had it', () => {
    // The desktop path: the drawer subtree is display:none there, so focus
    // never enters it and collapsing the column must leave the caret alone.
    const outside = document.createElement('input');
    document.body.appendChild(outside);
    try {
      const { rerender } = renderSidebar({ open: true });
      outside.focus();
      expect(document.activeElement).toBe(outside);

      rerender(
        <Sidebar
          open={false}
          onClose={noop}
          conversations={CONVERSATIONS}
          archived={ARCHIVED}
          activeId="b"
          onNewChat={noop}
          onOpenSearch={noop}
          onSelect={noop}
          onRename={noop}
          onDelete={noop}
          onSetPinned={noop}
          onSetArchived={noop}
          onExport={noop}
          onLoadArchived={noop}
        />,
      );

      expect(document.activeElement).toBe(outside);
    } finally {
      outside.remove();
    }
  });
});
