import { describe, expect, it } from 'vitest';
import {
  IMAGE_ONLY_PROMPT,
  lastUserContent,
  toOrchestratorChatRequest,
} from '../lib/orchestrator';

describe('lastUserContent', () => {
  it('returns the most recent user turn', () => {
    expect(
      lastUserContent({
        messages: [
          { role: 'user', content: 'first question' },
          { role: 'assistant', content: 'an answer' },
          { role: 'user', content: 'follow-up' },
        ],
      }),
    ).toBe('follow-up');
  });

  it('returns empty string when there are no user turns', () => {
    expect(lastUserContent({ messages: [] })).toBe('');
    expect(lastUserContent({})).toBe('');
    expect(
      lastUserContent({ messages: [{ role: 'assistant', content: 'hi' }] }),
    ).toBe('');
  });
});

describe('toOrchestratorChatRequest (§10 ChatRequest mapping)', () => {
  it('maps messages/session_id/image to message/session_id/image_base64', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [
          { role: 'user', content: 'show revenue by month' },
          { role: 'assistant', content: 'here it is' },
          { role: 'user', content: 'now cases by status' },
        ],
        session_id: 'conv-42',
        image: 'aGVsbG8=',
      }),
    ).toEqual({
      message: 'now cases by status',
      messages: [
        { role: 'user', content: 'show revenue by month' },
        { role: 'assistant', content: 'here it is' },
        { role: 'user', content: 'now cases by status' },
      ],
      session_id: 'conv-42',
      image_base64: 'aGVsbG8=',
    });
  });

  it('forwards up to 5 images: image_base64 stays the first, `images` carries all', () => {
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'compare these screenshots' }],
      session_id: 's-img',
      images: ['AAA', 'BBB', 'CCC'],
    });
    expect(out?.image_base64).toBe('AAA');
    expect(out?.images).toEqual(['AAA', 'BBB', 'CCC']);
  });

  it('keeps the exact v1 key set for single-image sends (no `images` key)', () => {
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'what is this?' }],
      session_id: 's-one',
      images: ['AAA'],
    });
    expect(out?.image_base64).toBe('AAA');
    expect(out && 'images' in out).toBe(false);
  });

  it('forwards the conversation as `messages` for within-chat memory', () => {
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'hello' }],
      session_id: 's1',
    });
    expect(out).not.toBeNull();
    expect(Object.keys(out!).sort()).toEqual([
      'image_base64',
      'message',
      'messages',
      'session_id',
    ]);
    expect(out!.messages).toEqual([{ role: 'user', content: 'hello' }]);
  });

  it('sends null image_base64 when no image is attached', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [{ role: 'user', content: 'hello' }],
        session_id: 's1',
      }),
    ).toEqual({
      message: 'hello',
      messages: [{ role: 'user', content: 'hello' }],
      session_id: 's1',
      image_base64: null,
    });
  });

  it('defaults session_id when missing', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [{ role: 'user', content: 'hello' }],
      })?.session_id,
    ).toBe('default');
  });

  it('substitutes a non-empty prompt for image-only sends (min_length=1)', () => {
    const out = toOrchestratorChatRequest({
      messages: [],
      session_id: 's1',
      image: 'aW1n',
    });
    expect(out?.message).toBe(IMAGE_ONLY_PROMPT);
    expect(out?.message.length).toBeGreaterThan(0);
    expect(out?.image_base64).toBe('aW1n');
  });

  it('treats whitespace-only text as empty', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [{ role: 'user', content: '   ' }],
        session_id: 's1',
      }),
    ).toBeNull();
    expect(
      toOrchestratorChatRequest({
        messages: [{ role: 'user', content: '  \n ' }],
        session_id: 's1',
        image: 'aW1n',
      })?.message,
    ).toBe(IMAGE_ONLY_PROMPT);
  });

  it('returns null when there is neither text nor image', () => {
    expect(toOrchestratorChatRequest({})).toBeNull();
    expect(
      toOrchestratorChatRequest({ messages: [], session_id: 's1' }),
    ).toBeNull();
  });

  it('forwards the V2 §1 fields (mode/model/effort/agent/conversation_id)', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [{ role: 'user', content: 'hello' }],
        session_id: 's1',
        conversation_id: 'conv-9',
        mode: 'assistant',
        model: 'fast',
        effort: 'high',
        agent: true,
      }),
    ).toEqual({
      message: 'hello',
      messages: [{ role: 'user', content: 'hello' }],
      session_id: 's1',
      image_base64: null,
      conversation_id: 'conv-9',
      mode: 'assistant',
      model: 'fast',
      effort: 'high',
      agent: true,
    });
  });

  it('keeps agent:false explicit (an intentional off is still sent)', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [{ role: 'user', content: 'hi' }],
        session_id: 's1',
        agent: false,
      }),
    ).toMatchObject({ agent: false });
  });

  it('omits V2 keys entirely for v1-shaped bodies (only message/messages/session/image)', () => {
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'hello' }],
      session_id: 's1',
    });
    expect(Object.keys(out!).sort()).toEqual([
      'image_base64',
      'message',
      'messages',
      'session_id',
    ]);
  });
});
