/**
 * The /api/history/* allowlist.
 *
 * These exist because a real bug shipped here. Per-message feedback lives at
 * `conversations/<id>/messages/<mid>/feedback` — five segments — and the proxy
 * capped the path at three. The browser therefore got a 404 from its OWN
 * frontend and the request never reached the orchestrator, so the thumb
 * silently did nothing. The store had tests and the endpoint had tests; the
 * layer between them had none.
 */
import { describe, expect, it } from 'vitest';

import { classifyHistoryPath } from '../lib/historyRoutes';

const kind = (parts: string[], method = 'GET') =>
  classifyHistoryPath(parts, method).kind;

describe('history proxy allowlist', () => {
  it('forwards the conversations tree', () => {
    expect(kind(['conversations'])).toBe('conversations');
    expect(kind(['conversations', 'abc'])).toBe('conversations');
    expect(kind(['conversations', 'abc', 'messages'], 'POST')).toBe(
      'conversations',
    );
    expect(kind(['conversations', 'abc', 'truncate'], 'POST')).toBe(
      'conversations',
    );
  });

  it('forwards per-message feedback — the five-segment path that was rejected', () => {
    expect(kind(['conversations', 'abc', 'messages', '42', 'feedback'], 'PUT')).toBe(
      'message-feedback',
    );
  });

  it('allows feedback only via PUT', () => {
    for (const method of ['GET', 'POST', 'DELETE', 'PATCH']) {
      expect(
        kind(['conversations', 'abc', 'messages', '42', 'feedback'], method),
      ).toBe('reject');
    }
  });

  it('does not become a passthrough for any five-segment path', () => {
    // The reason this is an allowlist and not a raised depth cap.
    expect(kind(['conversations', 'a', 'messages', '1', 'bogus'], 'PUT')).toBe(
      'reject',
    );
    expect(kind(['conversations', 'a', 'anything', '1', 'feedback'], 'PUT')).toBe(
      'reject',
    );
    expect(kind(['secrets', 'a', 'b', 'c', 'd'], 'PUT')).toBe('reject');
    expect(
      kind(['conversations', 'a', 'messages', '1', 'feedback', 'x'], 'PUT'),
    ).toBe('reject');
  });

  it('keeps search read-only', () => {
    expect(kind(['search'], 'GET')).toBe('search');
    for (const method of ['POST', 'PUT', 'DELETE']) {
      expect(kind(['search'], method)).toBe('reject');
    }
  });

  it('rejects anything outside the documented tree', () => {
    expect(kind([])).toBe('reject');
    expect(kind(['users'])).toBe('reject');
    expect(kind(['conversations', 'a', 'b', 'c'], 'PUT')).toBe('reject');
  });
});
