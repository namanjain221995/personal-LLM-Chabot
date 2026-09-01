/**
 * The app's global keyboard map.
 *
 * Two defects lived here, and both are the same class of mistake: a shortcut
 * that fires when it should not.
 *
 * NEW-08 — new chat was Ctrl/Cmd+Shift+O, which is the bookmark manager in
 * Chrome, Edge and Firefox on Windows. The sidebar printed it on a <kbd> chip,
 * so the app was advertising a chord the browser can answer first.
 *
 * L-17 — Escape stopped a running generation no matter where focus was,
 * including inside the composer, where Escape means "leave this field" and
 * absolutely does not mean "throw away the answer being written".
 *
 * The map is pure, so all of it is testable here rather than through a
 * component. The one thing tests cannot check is the browser's own behaviour,
 * which is precisely why the chord choice is asserted against the label the
 * user is shown instead of being hard-coded twice.
 */

import { describe, expect, it } from 'vitest';
import {
  NEW_CHAT_SHORTCUT_LABEL,
  shortcutAction,
  type ShortcutContext,
} from '@/lib/searchPalette';

/** Nothing open, nothing generating, focus somewhere inert. */
function ctx(over: Partial<ShortcutContext> = {}): ShortcutContext {
  return { paletteOpen: false, streaming: false, typing: false, ...over };
}

describe('NEW-08 — the new-chat chord', () => {
  it('fires on Ctrl+Shift+Enter', () => {
    expect(
      shortcutAction({ key: 'Enter', ctrlKey: true, shiftKey: true }, ctx()),
    ).toBe('new-chat');
  });

  it('fires on Cmd+Shift+Enter, so macOS is not left out', () => {
    expect(
      shortcutAction({ key: 'Enter', metaKey: true, shiftKey: true }, ctx()),
    ).toBe('new-chat');
  });

  it('no longer answers to the retired Ctrl/Cmd+Shift+O', () => {
    // The chord the browser takes for its bookmark manager. If this ever
    // returns 'new-chat' again the app is back to advertising a shortcut it
    // cannot be sure of receiving.
    expect(
      shortcutAction({ key: 'o', ctrlKey: true, shiftKey: true }, ctx()),
    ).toBeNull();
    expect(
      shortcutAction({ key: 'O', metaKey: true, shiftKey: true }, ctx()),
    ).toBeNull();
  });

  it('requires the exact modifiers — every near miss is inert', () => {
    // Enter alone and Shift+Enter belong to the composer: send, and newline.
    expect(shortcutAction({ key: 'Enter' }, ctx())).toBeNull();
    expect(shortcutAction({ key: 'Enter', shiftKey: true }, ctx())).toBeNull();
    // Ctrl+Enter without Shift is a common "send" chord elsewhere; it must not
    // silently become "discard this draft and open a new chat".
    expect(shortcutAction({ key: 'Enter', ctrlKey: true }, ctx())).toBeNull();
    expect(shortcutAction({ key: 'Enter', metaKey: true }, ctx())).toBeNull();
  });

  it('advertises exactly the chord it handles', () => {
    // The <kbd> chip is a promise to the user. Derive the chord FROM the label
    // so the two cannot drift apart the way they did before: changing one
    // without the other fails here.
    const needsShift = NEW_CHAT_SHORTCUT_LABEL.includes('⇧');
    const key = NEW_CHAT_SHORTCUT_LABEL.includes('⏎')
      ? 'Enter'
      : (NEW_CHAT_SHORTCUT_LABEL.trim().split(/\s+/).at(-1) as string);

    expect(shortcutAction({ key, ctrlKey: true, shiftKey: needsShift }, ctx())).toBe(
      'new-chat',
    );
    expect(shortcutAction({ key, metaKey: true, shiftKey: needsShift }, ctx())).toBe(
      'new-chat',
    );
    // …and the chip names the modifiers exactly: flipping Shift breaks it.
    expect(
      shortcutAction({ key, ctrlKey: true, shiftKey: !needsShift }, ctx()),
    ).not.toBe('new-chat');
  });

  it('still opens a new chat mid-sentence and while the palette is up', () => {
    // A three-key chord cannot be struck by accident, so unlike "/" it is not
    // suppressed while typing — that is what makes it usable from the composer.
    expect(
      shortcutAction(
        { key: 'Enter', ctrlKey: true, shiftKey: true },
        ctx({ typing: true }),
      ),
    ).toBe('new-chat');
    expect(
      shortcutAction(
        { key: 'Enter', ctrlKey: true, shiftKey: true },
        ctx({ paletteOpen: true }),
      ),
    ).toBe('new-chat');
  });
});

describe('L-17 — Escape while typing', () => {
  it('does not stop a generation when focus is in an editable field', () => {
    expect(
      shortcutAction({ key: 'Escape' }, ctx({ streaming: true, typing: true })),
    ).toBeNull();
  });

  it('still stops the generation when focus is not in a field', () => {
    expect(
      shortcutAction({ key: 'Escape' }, ctx({ streaming: true })),
    ).toBe('stop-streaming');
  });

  it('closes the palette first, typing or not', () => {
    // The palette's own input reports typing:true — Escape has to keep closing
    // it, or the modal becomes impossible to dismiss from the keyboard.
    expect(
      shortcutAction(
        { key: 'Escape' },
        ctx({ paletteOpen: true, typing: true, streaming: true }),
      ),
    ).toBe('close-palette');
    expect(
      shortcutAction({ key: 'Escape' }, ctx({ paletteOpen: true })),
    ).toBe('close-palette');
  });

  it('does nothing when there is nothing to stop', () => {
    expect(shortcutAction({ key: 'Escape' }, ctx())).toBeNull();
    expect(shortcutAction({ key: 'Escape' }, ctx({ typing: true }))).toBeNull();
  });
});

describe('the rest of the map is unchanged', () => {
  it('Ctrl/Cmd+K opens search, and only the bare chord', () => {
    expect(shortcutAction({ key: 'k', ctrlKey: true }, ctx())).toBe('open-search');
    expect(shortcutAction({ key: 'k', metaKey: true }, ctx())).toBe('open-search');
    expect(
      shortcutAction({ key: 'k', ctrlKey: true }, ctx({ paletteOpen: true })),
    ).toBeNull();
    expect(
      shortcutAction({ key: 'k', ctrlKey: true, shiftKey: true }, ctx()),
    ).toBeNull();
  });

  it('"/" focuses the composer unless something else owns the keyboard', () => {
    expect(shortcutAction({ key: '/' }, ctx())).toBe('focus-composer');
    expect(shortcutAction({ key: '/' }, ctx({ typing: true }))).toBeNull();
    expect(shortcutAction({ key: '/' }, ctx({ paletteOpen: true }))).toBeNull();
  });

  it('leaves every other modifier chord to the browser', () => {
    for (const key of ['t', 'n', 'w', 'p', 's', 'f', 'l']) {
      expect(shortcutAction({ key, ctrlKey: true }, ctx())).toBeNull();
      expect(shortcutAction({ key, ctrlKey: true, shiftKey: true }, ctx())).toBeNull();
    }
  });
});
