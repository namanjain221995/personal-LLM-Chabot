import { describe, expect, it } from 'vitest';
import {
  activateComposerMenuItem,
  composerMenuItems,
  nextEnabledIndex,
  trustLine,
} from '../lib/composerMenu';
import { DEFAULT_PREFS, type ChatPrefs } from '../lib/prefs';

function prefs(over: Partial<ChatPrefs> = {}): ChatPrefs {
  return { ...DEFAULT_PREFS, ...over };
}

describe('composer "+" menu items', () => {
  it('always offers all four rows: files, web, salesforce, live (owner 2026-08-06)', () => {
    for (const salesforce of [false, true]) {
      const ids = composerMenuItems({
        salesforce,
        sfLive: false,
        webSearchOn: false,
        streaming: false,
      }).map((i) => i.id);
      expect(ids).toEqual(['files', 'web-search', 'salesforce', 'sf-live']);
    }
  });

  it('warns on the web-search row that activating it turns Salesforce off', () => {
    const items = composerMenuItems({
      salesforce: true,
      sfLive: false,
      webSearchOn: false,
      streaming: false,
    });
    expect(items.find((i) => i.id === 'web-search')?.hint).toMatch(
      /turns Salesforce off/,
    );
  });

  it('promises on the Live row that it turns Salesforce on when it is off', () => {
    const items = composerMenuItems({
      salesforce: false,
      sfLive: false,
      webSearchOn: false,
      streaming: false,
    });
    expect(items.find((i) => i.id === 'sf-live')?.hint).toMatch(
      /turns Salesforce on/,
    );
  });

  it('checks the toggle rows from the prefs, never the files row', () => {
    const items = composerMenuItems({
      salesforce: false,
      sfLive: false,
      webSearchOn: true,
      streaming: false,
    });
    expect(items.find((i) => i.id === 'files')?.checked).toBeUndefined();
    expect(items.find((i) => i.id === 'web-search')?.checked).toBe(true);
    expect(items.find((i) => i.id === 'salesforce')?.checked).toBe(false);
  });

  it('disables only the file picker while streaming', () => {
    const items = composerMenuItems({
      salesforce: false,
      sfLive: false,
      webSearchOn: false,
      streaming: true,
    });
    expect(items.find((i) => i.id === 'files')?.disabled).toBe(true);
    expect(items.find((i) => i.id === 'web-search')?.disabled).toBeFalsy();
    expect(items.find((i) => i.id === 'salesforce')?.disabled).toBeFalsy();
  });
});

describe('activating an item', () => {
  it('"files" opens the picker and never touches prefs', () => {
    expect(activateComposerMenuItem('files', prefs())).toEqual({
      kind: 'pick-files',
    });
  });

  it('web search toggles auto → on → auto, never "off"', () => {
    const on = activateComposerMenuItem(
      'web-search',
      prefs({ salesforce: false }),
    );
    expect(on).toEqual({
      kind: 'prefs',
      prefs: prefs({ salesforce: false, webSearch: 'on' }),
    });
    const back = activateComposerMenuItem(
      'web-search',
      prefs({ salesforce: false, webSearch: 'on' }),
    );
    expect(back).toEqual({
      kind: 'prefs',
      prefs: prefs({ salesforce: false, webSearch: 'auto' }),
    });
  });

  it('turning salesforce ON drops a forced web search back to auto', () => {
    const out = activateComposerMenuItem(
      'salesforce',
      prefs({ salesforce: false, webSearch: 'on' }),
    );
    expect(out).toEqual({
      kind: 'prefs',
      prefs: prefs({ salesforce: true, webSearch: 'auto' }),
    });
  });

  it('turning salesforce OFF leaves web search on auto', () => {
    const out = activateComposerMenuItem(
      'salesforce',
      prefs({ salesforce: true, webSearch: 'auto' }),
    );
    expect(out).toEqual({
      kind: 'prefs',
      prefs: prefs({ salesforce: false, webSearch: 'auto' }),
    });
  });
});

describe('roving focus skips disabled rows', () => {
  // Streaming menu: [files: DISABLED, web-search, salesforce, sf-live]
  const streaming = composerMenuItems({
    salesforce: false,
    sfLive: false,
    webSearchOn: false,
    streaming: true,
  });

  it('walks past the disabled files row in the direction of travel', () => {
    // ArrowUp from web-search (1) targets files (0) — dead — wraps to 3.
    expect(nextEnabledIndex(streaming, 0, false)).toBe(3);
    // ArrowDown wrap from sf-live (3) targets 0 — lands on 1.
    expect(nextEnabledIndex(streaming, 0, true)).toBe(1);
  });

  it('returns the target untouched when it is usable', () => {
    expect(nextEnabledIndex(streaming, 1, true)).toBe(1);
  });

  it('stays put when every row is disabled', () => {
    const all = [{ disabled: true }, { disabled: true }];
    expect(nextEnabledIndex(all, 0, true)).toBe(0);
  });
});

describe('trust footer line', () => {
  it('salesforce ON promises local-only unconditionally — the server refuses web search in that mode', () => {
    expect(trustLine(prefs({ salesforce: true, webSearch: 'auto' }))).toMatch(
      /nothing leaves this machine/,
    );
    // Even an (unreachable, sanitize-normalized) stored 'on' must not weaken
    // the line: the SERVER gate makes the promise true regardless.
    expect(trustLine(prefs({ salesforce: true, webSearch: 'on' }))).toMatch(
      /nothing leaves this machine/,
    );
  });

  it('always warns about the internet when salesforce is off', () => {
    expect(trustLine(prefs({ salesforce: false, webSearch: 'auto' }))).toMatch(
      /sent to the internet/,
    );
    expect(trustLine(prefs({ salesforce: false, webSearch: 'on' }))).toMatch(
      /web search is on/,
    );
  });
});

describe('Live Salesforce (2026-08-06)', () => {
  it('shows the Live row unchecked while Salesforce is off', () => {
    const off = composerMenuItems({
      salesforce: false,
      sfLive: true, // stale stored value: must not read as active
      webSearchOn: false,
      streaming: false,
    });
    const live = off.find((i) => i.id === 'sf-live');
    expect(live).toBeDefined();
    expect(live?.checked).toBe(false);
  });

  it('activating sf-live toggles the pref', () => {
    const out = activateComposerMenuItem('sf-live', prefs({ salesforce: true }));
    expect(out.kind).toBe('prefs');
    if (out.kind === 'prefs') expect(out.prefs.sfLive).toBe(true);
  });

  it('one click on Live from web mode enters Salesforce mode AND goes live', () => {
    const out = activateComposerMenuItem(
      'sf-live',
      prefs({ salesforce: false, sfLive: false, webSearch: 'on' }),
    );
    expect(out).toEqual({
      kind: 'prefs',
      prefs: prefs({ salesforce: true, sfLive: true, webSearch: 'auto' }),
    });
  });

  it('one click on Web search from Salesforce mode switches modes', () => {
    const out = activateComposerMenuItem(
      'web-search',
      prefs({ salesforce: true, sfLive: true }),
    );
    expect(out).toEqual({
      kind: 'prefs',
      prefs: prefs({ salesforce: false, sfLive: false, webSearch: 'on' }),
    });
  });

  it('turning Salesforce off also turns Live off — no orphaned sub-toggle', () => {
    const out = activateComposerMenuItem(
      'salesforce',
      prefs({ salesforce: true, sfLive: true }),
    );
    if (out.kind === 'prefs') {
      expect(out.prefs.salesforce).toBe(false);
      expect(out.prefs.sfLive).toBe(false);
    }
  });

  it('the trust line is honest about live queries leaving the machine', () => {
    const live = trustLine(prefs({ salesforce: true, sfLive: true }));
    expect(live).toContain('live Salesforce org');
    expect(live).not.toContain('nothing leaves this machine');
    const synced = trustLine(prefs({ salesforce: true, sfLive: false }));
    expect(synced).toContain('nothing leaves this machine');
  });
});
