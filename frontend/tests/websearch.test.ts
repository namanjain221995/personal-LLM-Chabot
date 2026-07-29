import { describe, expect, it } from 'vitest';
import { toChatStreamEvent } from '../lib/sse';
import { toOrchestratorChatRequest } from '../lib/orchestrator';

describe('web search (Phase 1) frontend wiring', () => {
  it('parses the status SSE event', () => {
    const ev = toChatStreamEvent({ event: 'status', data: '{"text":"Reading 5 sources…"}' });
    expect(ev).toEqual({ kind: 'status', text: 'Reading 5 sources…' });
  });

  it('ignores a malformed status event without crashing', () => {
    expect(toChatStreamEvent({ event: 'status', data: '{"nope":1}' })).toBeNull();
  });

  it('forwards web_search mode to the orchestrator', () => {
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'latest news' }],
      session_id: 's1',
      web_search: 'on',
    });
    expect(out?.web_search).toBe('on');
  });

  it('omits web_search when not set', () => {
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'hi' }],
      session_id: 's1',
    });
    expect(out && 'web_search' in out).toBe(false);
  });
});
