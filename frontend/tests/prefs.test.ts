import { describe, expect, it } from 'vitest';
import type { StorageLike } from '../lib/history';
import {
  adoptDraftPrefs,
  DEFAULT_PREFS,
  loadPrefs,
  removePrefs,
  savePrefs,
} from '../lib/prefs';

function makeStorage(): StorageLike {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

describe('composer prefs (V2 §4c per-conversation persistence)', () => {
  it('defaults to Salesforce ON, Smart, Think, search Auto', () => {
    expect(DEFAULT_PREFS).toEqual({
      salesforce: true,
      sfLive: false,
      model: 'smart',
      effort: 'think',
      agent: false,
      // The composer toggle is gone (2026-07-28) — the level decides, so the
      // default must be Auto or Fast could never search.
      webSearch: 'auto', deepResearch: false,
    });
    expect(loadPrefs(makeStorage(), 'unknown-conv')).toEqual(DEFAULT_PREFS);
  });

  it('persists per conversation independently', () => {
    const storage = makeStorage();
    savePrefs(storage, 'c1', {
      salesforce: false,
      sfLive: false,
      model: 'fast',
      effort: 'think',
      agent: false,
      webSearch: 'auto', deepResearch: false,
    });
    savePrefs(storage, 'c2', {
      // Forced search is only coherent with Salesforce OFF (2026-08-05).
      salesforce: false,
      sfLive: false,
      model: 'smart',
      effort: 'max',
      agent: false,
      webSearch: 'on', deepResearch: false,
    });
    expect(loadPrefs(storage, 'c1').salesforce).toBe(false);
    expect(loadPrefs(storage, 'c1').model).toBe('fast');
    expect(loadPrefs(storage, 'c2').effort).toBe('max');
    expect(loadPrefs(storage, 'c2').webSearch).toBe('on');
  });

  it('adopts draft prefs into the conversation created on first send', () => {
    const storage = makeStorage();
    savePrefs(storage, null, {
      salesforce: false,
      sfLive: false,
      model: 'smart',
      effort: 'fast',
      agent: false,
      webSearch: 'auto', deepResearch: false,
    });
    const adopted = adoptDraftPrefs(storage, 'new-conv');
    expect(adopted.effort).toBe('fast');
    expect(loadPrefs(storage, 'new-conv').salesforce).toBe(false);
    // Draft slot resets afterwards.
    expect(loadPrefs(storage, null)).toEqual(DEFAULT_PREFS);
  });

  it('migrates prefs saved by the removed toggles', () => {
    // Both controls are gone from the composer, so a value saved by the old UI
    // can no longer be undone by the user: a stored "off" would disable search
    // forever, and a stored agent:true would force the slow path at Fast.
    const storage = makeStorage();
    storage.setItem(
      'techsara.chatprefs.v1',
      JSON.stringify({ c1: { ...DEFAULT_PREFS, agent: true, webSearch: 'off' } }),
    );
    expect(loadPrefs(storage, 'c1').agent).toBe(false);
    expect(loadPrefs(storage, 'c1').webSearch).toBe('auto');
  });

  it('normalizes a stored forced search away when salesforce is on', () => {
    // Salesforce mode hides the web-search control (2026-08-05), so a stored
    // "on" there would be invisible and un-undoable — and the server refuses
    // to honour it anyway. It must come back as "auto".
    const storage = makeStorage();
    storage.setItem(
      'techsara.chatprefs.v1',
      JSON.stringify({
        c1: { ...DEFAULT_PREFS, salesforce: true, webSearch: 'on' },
        c2: { ...DEFAULT_PREFS, salesforce: false, webSearch: 'on' },
      }),
    );
    expect(loadPrefs(storage, 'c1').webSearch).toBe('auto');
    expect(loadPrefs(storage, 'c2').webSearch).toBe('on');
  });

  it('sanitizes corrupt or partial payloads back to defaults per field', () => {
    const storage = makeStorage();
    storage.setItem(
      'techsara.chatprefs.v1',
      JSON.stringify({
        c1: { salesforce: 'yes', model: 'gpt5', effort: 'ultra', agent: 1 },
      }),
    );
    expect(loadPrefs(storage, 'c1')).toEqual(DEFAULT_PREFS);
    storage.setItem('techsara.chatprefs.v1', 'not json');
    expect(loadPrefs(storage, 'c1')).toEqual(DEFAULT_PREFS);
  });

  it('removes prefs with the conversation', () => {
    const storage = makeStorage();
    savePrefs(storage, 'c1', { ...DEFAULT_PREFS, agent: true });
    removePrefs(storage, 'c1');
    expect(loadPrefs(storage, 'c1')).toEqual(DEFAULT_PREFS);
  });
});

describe('Live Salesforce pref (2026-08-06)', () => {
  it('sfLive never survives salesforce being off — its menu row would be gone', () => {
    const storage = makeStorage();
    savePrefs(storage, 'c1', {
      salesforce: false,
      sfLive: true,
      model: 'smart',
      effort: 'think',
      agent: false,
      webSearch: 'auto', deepResearch: false,
    });
    expect(loadPrefs(storage, 'c1').sfLive).toBe(false);
  });

  it('sfLive persists while salesforce stays on', () => {
    const storage = makeStorage();
    savePrefs(storage, 'c1', {
      salesforce: true,
      sfLive: true,
      model: 'smart',
      effort: 'think',
      agent: false,
      webSearch: 'auto', deepResearch: false,
    });
    expect(loadPrefs(storage, 'c1').sfLive).toBe(true);
  });
});

describe('legacy effort values (pre-collapse builds)', () => {
  it('normalizes stored low/medium/high/extra_high to the 3-level ladder', () => {
    const storage = makeStorage();
    storage.setItem(
      'techsara.chatprefs.v1',
      JSON.stringify({
        // Raw JSON as an old build wrote it — deliberately not typed.
        a: { ...structuredClone(DEFAULT_PREFS), effort: 'low' as string },
        b: { ...structuredClone(DEFAULT_PREFS), effort: 'medium' as string },
        c: { ...structuredClone(DEFAULT_PREFS), effort: 'high' as string },
        d: { ...structuredClone(DEFAULT_PREFS), effort: 'extra_high' as string },
      }),
    );
    expect(loadPrefs(storage, 'a').effort).toBe('fast');
    expect(loadPrefs(storage, 'b').effort).toBe('think');
    expect(loadPrefs(storage, 'c').effort).toBe('think');
    expect(loadPrefs(storage, 'd').effort).toBe('max');
  });
});
