import { describe, expect, it } from 'vitest';
import { attachBaseTurns } from '../lib/streams';
import type { ChatMessage } from '../lib/types';

function msg(role: 'user' | 'assistant', content: string): ChatMessage {
  return { id: content, role, content, createdAt: 1 };
}

describe('attachBaseTurns (re-join after reload)', () => {
  it('keeps everything up to and including the last user message', () => {
    const messages = [
      msg('user', 'q1'),
      msg('assistant', 'a1'),
      msg('user', 'q2'),
    ];
    expect(attachBaseTurns(messages)).toEqual(messages);
  });

  it('drops a trailing assistant answer — the server replay rebuilds it', () => {
    const messages = [
      msg('user', 'q1'),
      msg('assistant', 'partial answer from before the reload'),
    ];
    expect(attachBaseTurns(messages)).toEqual([messages[0]]);
  });

  it('handles empty threads', () => {
    expect(attachBaseTurns([])).toEqual([]);
  });
});

import { withLiveProgressRetired } from '../lib/streams';

describe('withLiveProgressRetired', () => {
  // Review round 2026-08-30: every terminal patch (done, error, unreachable,
  // interrupted) must drop the live progress line and phase marker. They are
  // persisted with the message, so a surviving "Searching the web…" made a
  // reloaded or errored answer tick a fake clock forever.
  it('clears searchStatus and phaseStatus on any terminal patch', () => {
    const patch = withLiveProgressRetired({ status: 'error' });
    expect(patch.searchStatus).toBeUndefined();
    expect('searchStatus' in patch).toBe(true); // explicit undefined overwrites
    expect(patch.phaseStatus).toBeUndefined();
    expect(patch.status).toBe('error');
  });

  it('never overrides what the terminal patch itself sets', () => {
    expect(withLiveProgressRetired({ status: 'done', content: 'x' })).toEqual({
      searchStatus: undefined,
      phaseStatus: undefined,
      status: 'done',
      content: 'x',
    });
  });
});
