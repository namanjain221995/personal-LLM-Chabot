import { describe, expect, it } from 'vitest';
import {
  extractUpstreamMessage,
  friendlyError,
  trimNotice,
} from '../lib/errors';

// The exact payload the user was shown in the chat thread.
const CONTEXT_400 =
  `Error code: 400 - {'error': {'message': "This model's maximum context ` +
  `length is 8192 tokens. However, you requested 8000 output tokens and your ` +
  `prompt contains at least 193 input tokens, for a total of at least 8193 ` +
  `tokens. Please reduce the length of the input prompt or the number of ` +
  `requested output tokens. (parameter=input_tokens, value=193)", 'type': ` +
  `'BadRequestError', 'param': 'input_tokens', 'code': 400}}`;

describe('extractUpstreamMessage', () => {
  it('pulls the sentence out of a python-repr payload', () => {
    expect(extractUpstreamMessage(CONTEXT_400)).toMatch(
      /^This model's maximum context length is 8192 tokens\./,
    );
  });

  it('pulls it out of real JSON too', () => {
    expect(
      extractUpstreamMessage('{"error": {"message": "boom happened"}}'),
    ).toBe('boom happened');
  });

  it('is null when there is no message field', () => {
    expect(extractUpstreamMessage('plain failure text')).toBeNull();
  });
});

describe('friendlyError', () => {
  it('explains a context overflow in plain language and hides the payload', () => {
    const out = friendlyError(CONTEXT_400);
    expect(out.message).toContain('too long for the selected model');
    expect(out.message).toContain('Smart');
    // No JSON, no token arithmetic, no error codes in the visible sentence.
    expect(out.message).not.toMatch(/\{|\}|BadRequestError|8192|400/);
  });

  it('explains a connection failure', () => {
    const out = friendlyError('Connection refused to http://vllm:30000');
    expect(out.message).toContain('temporarily unavailable');
    // The upstream host must not survive into the visible sentence.
    expect(out.message).not.toMatch(/vllm|30000|http/i);
  });

  it('explains an out-of-memory failure', () => {
    expect(friendlyError('CUDA out of memory').message).toContain(
      'ran out of memory',
    );
  });

  /**
   * This assertion is INVERTED from what it was. It used to require that an
   * unclassified failure be shown to the user verbatim ("something odd"),
   * which is precisely how raw upstream payloads reached the thread. There is
   * no way to know what such a string contains — a DSN, a header, a path — so
   * an unrecognized error now gets our own sentence and the original goes to
   * the server log.
   */
  it('never renders an unrecognized upstream sentence', () => {
    const raw = `Error code: 500 - {'error': {'message': "connect ECONNREFUSED 10.0.0.4:8080, token=sk-abcd1234efgh"}}`;
    const out = friendlyError(raw);
    expect(out.message).not.toMatch(/ECONNREFUSED|10\.0\.0\.4|8080|sk-/);
    expect(out.message).toMatch(/couldn't complete|try again/i);
  });

  it('does not echo a bare upstream sentence either', () => {
    const out = friendlyError('Traceback (most recent call last): boom');
    expect(out.message).not.toMatch(/Traceback|boom/i);
  });

  it('handles missing/empty input', () => {
    expect(friendlyError(undefined).message).toBe(
      'The engine reported an error.',
    );
    expect(friendlyError('   ').message).toBe('The engine reported an error.');
  });
});

describe('trimNotice', () => {
  it('says part of a long message was left out when content was clipped', () => {
    const out = trimNotice({ dropped_turns: 0, clipped_messages: 1 });
    expect(out).toContain('shortened');
    expect(out).toContain('left out');
  });

  it('reports dropped turns with correct singular/plural', () => {
    expect(trimNotice({ dropped_turns: 1, clipped_messages: 0 })).toContain(
      '1 earlier turn was',
    );
    expect(trimNotice({ dropped_turns: 3, clipped_messages: 0 })).toContain(
      '3 earlier turns were',
    );
  });

  it('mentions both when both happened', () => {
    const out = trimNotice({ dropped_turns: 2, clipped_messages: 1 });
    expect(out).toContain('left out');
    expect(out).toContain('2 earlier turns');
  });
});
