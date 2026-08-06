import { describe, expect, it } from 'vitest';
import type { StorageLike } from '../lib/history';
import { loadFeedback, saveFeedback, toggleFeedback } from '../lib/feedback';

function makeStorage(): StorageLike {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

describe('thumbs feedback (ChatGPT-style action row, 2026-08-05)', () => {
  it('clicking a thumb sets it, again clears it, the other switches', () => {
    expect(toggleFeedback(null, 'up')).toBe('up');
    expect(toggleFeedback('up', 'up')).toBe(null);
    expect(toggleFeedback('up', 'down')).toBe('down');
    expect(toggleFeedback('down', 'down')).toBe(null);
  });

  it('persists per message and clears with null', () => {
    const storage = makeStorage();
    expect(loadFeedback(storage, 'm1')).toBe(null);
    saveFeedback(storage, 'm1', 'up');
    saveFeedback(storage, 'm2', 'down');
    expect(loadFeedback(storage, 'm1')).toBe('up');
    expect(loadFeedback(storage, 'm2')).toBe('down');
    saveFeedback(storage, 'm1', null);
    expect(loadFeedback(storage, 'm1')).toBe(null);
    expect(loadFeedback(storage, 'm2')).toBe('down');
  });

  it('survives corrupt storage without throwing', () => {
    const storage = makeStorage();
    storage.setItem('techsara.feedback.v1', 'not json');
    expect(loadFeedback(storage, 'm1')).toBe(null);
    saveFeedback(storage, 'm1', 'up');
    expect(loadFeedback(storage, 'm1')).toBe('up');
  });
});
