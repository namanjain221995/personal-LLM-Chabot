// @vitest-environment jsdom
/**
 * The active-mode pills, and what they look like on a phone.
 *
 * At 375px "[☁ Live Salesforce ×]" ate the composer's control row: the row is
 * `flex-wrap`, so it never scrolled the page sideways — it WRAPPED, pushing
 * the effort picker and Send onto a second line and breaking the alignment in
 * the owner's screenshot. Below `md` the pills now show icon + × only.
 *
 * The label is hidden with `sr-only`, NOT `hidden`. Every icon in this app is
 * `aria-hidden` (components/icons.tsx), so the label IS each button's
 * accessible name — `display:none` would leave four nameless buttons on every
 * phone. `sr-only` is absolutely positioned, so it is not a flex item and
 * contributes neither width nor gap: real layout removal, with the accessible
 * name unchanged at every width. That is what these tests pin.
 *
 * jsdom applies no CSS, so a media query cannot be *rendered* here. What is
 * assertable — and what actually matters — is that the label is present in the
 * DOM, carries the responsive contract, and still names its button.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Composer } from '../components/Composer';
import { DEFAULT_PREFS, type ChatPrefs } from '@/lib/prefs';

/** Every mode that renders a pill, and the prefs that switch it on. */
const MODES = [
  {
    name: 'Salesforce',
    label: 'Salesforce',
    prefs: { salesforce: true, sfLive: false },
  },
  {
    name: 'Live Salesforce',
    label: 'Live Salesforce',
    prefs: { salesforce: true, sfLive: true },
  },
  {
    name: 'Web search',
    label: 'Web search',
    prefs: { salesforce: false, webSearch: 'on' as const },
  },
  {
    name: 'Deep research',
    label: 'Deep research',
    prefs: { salesforce: false, deepResearch: true },
  },
];

let changed: ChatPrefs[];

beforeEach(() => {
  changed = [];
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount(overrides: Partial<ChatPrefs>) {
  const prefs: ChatPrefs = { ...DEFAULT_PREFS, ...overrides };
  render(
    <Composer
      streaming={false}
      prefs={prefs}
      onPrefsChange={(p) => changed.push(p)}
      onSend={() => undefined}
      onStop={() => undefined}
    />,
  );
  return prefs;
}

/** The pill for `label` — the toggle button whose accessible name it is. */
const chip = (label: string) => screen.getByRole('button', { name: label });

describe('mode pills · every mode renders one, and it is the same pill', () => {
  for (const mode of MODES) {
    describe(mode.name, () => {
      it('renders a pressed toggle whose accessible name is the mode', () => {
        mount(mode.prefs);
        const button = chip(mode.label);
        expect(button).toBeTruthy();
        // The label survives label-hiding: this is the whole point of using
        // sr-only rather than display:none.
        expect(button.getAttribute('aria-pressed')).toBe('true');
        expect(button.textContent).toContain(mode.label);
      });

      it('hides the label below md and restores it at md — via true layout removal', () => {
        mount(mode.prefs);
        const span = chip(mode.label).querySelector('span');
        expect(span).not.toBeNull();
        expect(span!.textContent).toBe(mode.label);
        // sr-only is position:absolute — not a flex item, so no width, no gap.
        expect(span!.className).toContain('sr-only');
        // ...and md:not-sr-only puts it back in the flow on desktop.
        expect(span!.className).toContain('md:not-sr-only');
        // NOT display:none — that would strip the accessible name.
        expect(span!.className).not.toContain('hidden');
      });

      it('keeps its OWN icon and the × visible at every width', () => {
        mount(mode.prefs);
        const svgs = chip(mode.label).querySelectorAll('svg');
        // The mode icon and the ×, neither of them responsive.
        expect(svgs).toHaveLength(2);
        for (const svg of svgs) {
          expect(svg.getAttribute('aria-hidden')).toBe('true');
          expect(svg.className.baseVal ?? '').not.toContain('sr-only');
          expect(svg.className.baseVal ?? '').not.toContain('hidden');
        }
      });

      it('is keyboard reachable and focusable', () => {
        mount(mode.prefs);
        const button = chip(mode.label);
        expect(button.tagName).toBe('BUTTON');
        expect(button.getAttribute('type')).toBe('button');
        expect(button.hasAttribute('disabled')).toBe(false);
        act(() => button.focus());
        expect(document.activeElement).toBe(button);
      });

      it('explains itself on hover, as before', () => {
        mount(mode.prefs);
        expect(chip(mode.label).getAttribute('title')).toBeTruthy();
      });
    });
  }
});

describe('mode pills · the icons are distinct, not one generic glyph', () => {
  it('each mode draws different artwork', () => {
    const paths = new Set<string>();
    for (const mode of MODES) {
      cleanup();
      mount(mode.prefs);
      const icon = chip(mode.label).querySelectorAll('svg')[0];
      paths.add(icon.innerHTML);
    }
    expect(paths.size).toBe(MODES.length);
  });
});

describe('mode pills · clicking × changes only that mode', () => {
  it('Salesforce turns Salesforce off and leaves the rest alone', () => {
    const before = mount({ salesforce: true, sfLive: false });
    fireEvent.click(chip('Salesforce'));

    expect(changed).toHaveLength(1);
    const after = changed[0];
    expect(after.salesforce).toBe(false);
    // Everything unrelated is byte-identical.
    expect(after.model).toBe(before.model);
    expect(after.effort).toBe(before.effort);
    expect(after.webSearch).toBe(before.webSearch);
    expect(after.deepResearch).toBe(before.deepResearch);
  });

  it('Live Salesforce steps DOWN to synced rather than all the way off', () => {
    mount({ salesforce: true, sfLive: true });
    fireEvent.click(chip('Live Salesforce'));

    expect(changed).toHaveLength(1);
    // The documented one-×-per-level rule, unchanged by this work.
    expect(changed[0].sfLive).toBe(false);
    expect(changed[0].salesforce).toBe(true);
  });

  it('Web search returns to auto and touches nothing else', () => {
    const before = mount({ salesforce: false, webSearch: 'on' });
    fireEvent.click(chip('Web search'));

    expect(changed).toHaveLength(1);
    const after = changed[0];
    expect(after.webSearch).toBe('auto');
    expect(after.salesforce).toBe(before.salesforce);
    expect(after.deepResearch).toBe(before.deepResearch);
    expect(after.model).toBe(before.model);
  });

  it('Deep research cancels itself and touches nothing else', () => {
    const before = mount({ salesforce: false, deepResearch: true });
    fireEvent.click(chip('Deep research'));

    expect(changed).toHaveLength(1);
    const after = changed[0];
    expect(after.deepResearch).toBe(false);
    expect(after.salesforce).toBe(before.salesforce);
    expect(after.webSearch).toBe(before.webSearch);
    expect(after.model).toBe(before.model);
  });

  it('a pill is rendered ONLY while its mode is on', () => {
    mount(DEFAULT_PREFS);
    for (const mode of MODES) {
      expect(screen.queryByRole('button', { name: mode.label })).toBeNull();
    }
  });
});

describe('mode pills · several at once', () => {
  it('Web search and Deep research coexist, each compact and independent', () => {
    mount({ salesforce: false, webSearch: 'on', deepResearch: true });

    const web = chip('Web search');
    const deep = chip('Deep research');
    expect(web).not.toBe(deep);

    // Both labels are removed from layout on a phone, so two active modes
    // cost two icons and two ×s — not two full sentences.
    for (const button of [web, deep]) {
      expect(button.querySelector('span')!.className).toContain('sr-only');
      expect(button.querySelectorAll('svg')).toHaveLength(2);
    }

    // Closing one leaves the other's mode untouched.
    fireEvent.click(deep);
    expect(changed).toHaveLength(1);
    expect(changed[0].deepResearch).toBe(false);
    expect(changed[0].webSearch).toBe('on');
  });

  it('Salesforce and Live Salesforce are never both shown — one pill per mode', () => {
    mount({ salesforce: true, sfLive: true });
    expect(screen.queryByRole('button', { name: 'Salesforce' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Live Salesforce' })).toBeTruthy();
  });
});

describe('mode pills · the control row still holds its other controls', () => {
  it('Send and the effort picker are present alongside two active pills', () => {
    mount({ salesforce: false, webSearch: 'on', deepResearch: true });
    expect(screen.getByRole('button', { name: 'Send message' })).toBeTruthy();
    // The effort picker (Fast/Think) keeps its own trigger button.
    expect(screen.getByRole('button', { name: /Fast|Think|Max/i })).toBeTruthy();
  });
});
