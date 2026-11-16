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
  // Four rows since Live Salesforce was removed (2026-11-04); the
  // owner-2026-08-06 rule is unchanged — the menu still reads as the COMPLETE
  // capability list, with every row always visible whatever the mode.
  it('always offers all four rows: files, web, research, salesforce', () => {
    for (const salesforce of [false, true]) {
      const ids = composerMenuItems({
        salesforce,
        sfLive: false,
        webSearchOn: false, deepResearchOn: false,
        streaming: false,
      }).map((i) => i.id);
      expect(ids).toEqual([
        'files',
        'web-search',
        'deep-research',
        'salesforce',
      ]);
    }
  });

  it('warns on the web-search row that activating it turns Salesforce off', () => {
    const items = composerMenuItems({
      salesforce: true,
      sfLive: false,
      webSearchOn: false, deepResearchOn: false,
      streaming: false,
    });
    expect(items.find((i) => i.id === 'web-search')?.hint).toMatch(
      /turns Salesforce off/,
    );
  });

  it('checks the toggle rows from the prefs, never the files row', () => {
    const items = composerMenuItems({
      salesforce: false,
      sfLive: false,
      webSearchOn: true, deepResearchOn: false,
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
      webSearchOn: false, deepResearchOn: false,
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
  // Streaming menu: [files: DISABLED, web-search, deep-research, salesforce].
  const streaming = composerMenuItems({
    salesforce: false,
    sfLive: false,
    webSearchOn: false, deepResearchOn: false,
    streaming: true,
  });

  it('walks past the disabled files row in the direction of travel', () => {
    // ArrowUp from web-search (1) targets files (0) — dead — wraps to the
    // last row, salesforce at 3.
    expect(nextEnabledIndex(streaming, 0, false)).toBe(3);
    // ArrowDown wrap from salesforce (3) targets 0 — dead — lands on 1.
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

describe('Live Salesforce was removed (2026-11-04)', () => {
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

  it('the trust line says synced whatever a stale pref holds', () => {
    for (const sfLive of [false, true]) {
      const line = trustLine(prefs({ salesforce: true, sfLive }));
      expect(line).toContain('synced Salesforce data');
      expect(line).toContain('nothing leaves this machine');
      expect(line).not.toContain('live');
    }
  });
});

describe('Deep Research interacts correctly with Salesforce (review 2026-08-30)', () => {
  it('turning it on leaves Salesforce mode, because research is web work', () => {
    const out = activateComposerMenuItem(
      'deep-research',
      prefs({ salesforce: true, sfLive: true, deepResearch: false }),
    );
    expect(out.kind).toBe('prefs');
    if (out.kind !== 'prefs') return;
    expect(out.prefs.deepResearch).toBe(true);
    expect(out.prefs.salesforce).toBe(false);
    expect(out.prefs.sfLive).toBe(false);
  });

  it('turning Salesforce on disarms it', () => {
    const out = activateComposerMenuItem(
      'salesforce',
      prefs({ salesforce: false, deepResearch: true }),
    );
    if (out.kind !== 'prefs') return;
    expect(out.prefs.salesforce).toBe(true);
    expect(out.prefs.deepResearch).toBe(false);
  });
});
