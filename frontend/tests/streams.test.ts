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
