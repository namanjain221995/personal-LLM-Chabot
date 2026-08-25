// @vitest-environment jsdom
/**
 * A failed send must carry the REAL status onto the message.
 *
 * The old client read `body.message` and matched `/orchestrator is
 * unreachable/i` against it — so classification depended on prose, and any
 * non-JSON body (an intermediary's own HTML error page) fell into the
 * unreachable bucket for want of a string to match. These assert the status
 * is what decides, and that the safe copy is what gets persisted.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getLiveStream, startStream } from '../lib/streams';
import type { ChatMessage } from '../lib/types';

const USER: ChatMessage = {
  id: 'u1',
  role: 'user',
  content: 'hello',
  createdAt: 0,
};

let conversation = 0;

function nextId(): string {
  conversation += 1;
  return `conv-${conversation}`;
}

async function failWith(res: Response | (() => never)): Promise<ChatMessage> {
  const id = nextId();
  vi.stubGlobal(
    'fetch',
    typeof res === 'function' ? async () => res() : async () => res,
  );
  await startStream({
    conversationId: id,
    turns: [USER],
    prefs: { model: 'smart', effort: 'medium', mode: 'assistant' } as never,
  });
  const view = getLiveStream(id);
  const last = view?.messages[view.messages.length - 1];
  if (!last) throw new Error('no assistant message was recorded');
  return last;
}

beforeEach(() => {
  vi.stubGlobal('localStorage', {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('a failed send records the real status', () => {
  it.each([
    [404, 'NOT_FOUND'],
    [500, 'APPLICATION_ERROR'],
    [502, 'MODEL_UNAVAILABLE'],
    [503, 'ORCHESTRATOR_UNAVAILABLE'],
    [504, 'TIMEOUT'],
  ])('keeps %i as %s', async (status, code) => {
    const msg = await failWith(
      new Response(JSON.stringify({ code }), {
        status,
        headers: { 'content-type': 'application/json' },
      }),
    );
    expect(msg.status).toBe('error');
    expect(msg.errorStatus).toBe(status);
    expect(msg.errorCode).toBe(code);
  });

  it('falls back to the status when the body is not JSON', async () => {
    // An intermediary's own HTML error page. This is the case the old prose
    // matching mis-reported as "orchestrator unreachable".
    const msg = await failWith(
      new Response('<html><body>502 Bad Gateway</body></html>', {
        status: 502,
        headers: { 'content-type': 'text/html' },
      }),
    );
    expect(msg.errorStatus).toBe(502);
    expect(msg.errorCode).toBe('MODEL_UNAVAILABLE');
  });

  it('records no status at all for a transport failure', async () => {
    const msg = await failWith(() => {
      throw new TypeError('Failed to fetch');
    });
    expect(msg.errorStatus).toBeNull();
    expect(msg.errorCode).toBe('NETWORK_ERROR');
  });

  it('persists only the SAFE sentence, never upstream text', async () => {
    const msg = await failWith(
      new Response(
        JSON.stringify({
          code: 'APPLICATION_ERROR',
          // A body that should never be read for display.
          detail: 'Traceback: connect ECONNREFUSED 10.0.0.4:8080 token=sk-abc',
        }),
        { status: 500, headers: { 'content-type': 'application/json' } },
      ),
    );
    expect(msg.errorMessage).toBeTruthy();
    expect(msg.errorMessage).not.toMatch(
      /Traceback|ECONNREFUSED|10\.0\.0\.4|sk-abc|8080/,
    );
  });

  it('distinguishes a 404 from a timeout — they used to be identical', async () => {
    const notFound = await failWith(
      new Response(JSON.stringify({ code: 'NOT_FOUND' }), { status: 404 }),
    );
    const timeout = await failWith(
      new Response(JSON.stringify({ code: 'TIMEOUT' }), { status: 504 }),
    );
    expect(notFound.errorCode).not.toBe(timeout.errorCode);
    expect(notFound.errorMessage).not.toBe(timeout.errorMessage);
  });
});
