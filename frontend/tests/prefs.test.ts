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
  it('defaults to Salesforce ON, Smart, Medium, search Auto', () => {
    expect(DEFAULT_PREFS).toEqual({
      salesforce: true,
      model: 'smart',
      effort: 'medium',
      agent: false,
      // The composer toggle is gone (2026-07-28) — the level decides, so the
      // default must be Auto or Low/Medium/High could never search.
      webSearch: 'auto',
    });
    expect(loadPrefs(makeStorage(), 'unknown-conv')).toEqual(DEFAULT_PREFS);
  });

  it('persists per conversation independently', () => {
    const storage = makeStorage();
    savePrefs(storage, 'c1', {
      salesforce: false,
      model: 'fast',
      effort: 'medium',
      agent: false,
      webSearch: 'auto',
    });
    savePrefs(storage, 'c2', {
      salesforce: true,
      model: 'smart',
      effort: 'high',
      agent: false,
      webSearch: 'on',
    });
    expect(loadPrefs(storage, 'c1').salesforce).toBe(false);
    expect(loadPrefs(storage, 'c1').model).toBe('fast');
    expect(loadPrefs(storage, 'c2').effort).toBe('high');
    expect(loadPrefs(storage, 'c2').webSearch).toBe('on');
  });

  it('adopts draft prefs into the conversation created on first send', () => {
    const storage = makeStorage();
    savePrefs(storage, null, {
      salesforce: false,
      model: 'smart',
      effort: 'low',
      agent: false,
      webSearch: 'auto',
    });
    const adopted = adoptDraftPrefs(storage, 'new-conv');
    expect(adopted.effort).toBe('low');
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

  it('sanitizes corrupt or partial payloads back to defaults per field', () => {
    const storage = makeStorage();
    storage.setItem(
      'techsara.chatprefs.v1',
      JSON.stringify({
        c1: { salesforce: 'yes', model: 'gpt5', effort: 'max', agent: 1 },
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
