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
  it('offers files, web search and salesforce while salesforce is off', () => {
    const ids = composerMenuItems({
      salesforce: false,
      webSearchOn: false,
      streaming: false,
    }).map((i) => i.id);
    expect(ids).toEqual(['files', 'web-search', 'salesforce']);
  });

  it('HIDES web search while salesforce is on — that mode never searches', () => {
    const ids = composerMenuItems({
      salesforce: true,
      webSearchOn: false,
      streaming: false,
    }).map((i) => i.id);
    expect(ids).toEqual(['files', 'salesforce']);
  });

  it('checks the toggle rows from the prefs, never the files row', () => {
    const items = composerMenuItems({
      salesforce: false,
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
  // Streaming menu (salesforce off): [files: DISABLED, web-search, salesforce]
  const streaming = composerMenuItems({
    salesforce: false,
    webSearchOn: false,
    streaming: true,
  });

  it('walks past the disabled files row in the direction of travel', () => {
    // ArrowUp from web-search (1) targets files (0) — dead — lands on 2.
    expect(nextEnabledIndex(streaming, 0, false)).toBe(2);
    // ArrowDown wrap from salesforce (2) targets 0 — lands on 1.
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
