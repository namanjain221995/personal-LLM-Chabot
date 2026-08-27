/**
 * What a refused truncate is allowed to claim.
 *
 * Truncate is the one sanctioned shrink: editing a turn, or regenerating an
 * older answer, both go through it. Its failures used to be collapsed into a
 * single sentence — "This conversation changed elsewhere" — which is true for
 * exactly one of them.
 *
 * The case that exposed it: the orchestrator was simply not running, the
 * frontend's own proxy answered its own request with 502 "The orchestrator is
 * unreachable", and the user was told their conversation had been changed by
 * somebody else. Nothing had changed and nobody else was involved. Worse, the
 * "recovery" then force-read the thread from the same dead server, so the one
 * branch that runs on failure could only fail again.
 *
 * The distinction these tests hold: a rejection the SERVER SENT (409/404) is a
 * conflict worth re-reading; anything else means the request never landed, so
 * nothing was destroyed and there is nothing to re-read.
 */

import { describe, expect, it } from 'vitest';
import { HistoryApiError, truncateFailure } from '../lib/historyApi';

describe('truncateFailure — a rejection the server actually sent', () => {
  it('treats 409 as a real conflict and re-reads the thread', () => {
    const out = truncateFailure(
      new HistoryApiError(409, 'History request failed with status 409.'),
    );
    expect(out.reload).toBe(true);
    expect(out.message).toContain('changed elsewhere');
  });

  it('treats 404 the same way — the row is gone, re-read to find out', () => {
    const out = truncateFailure(new HistoryApiError(404, 'gone'));
    expect(out.reload).toBe(true);
    expect(out.message).toContain('changed elsewhere');
  });
});

describe('truncateFailure — the request never landed', () => {
  it('does NOT claim a conflict when the orchestrator is down (502)', () => {
    // The exact shape of the reported bug: the frontend proxy could not reach
    // the orchestrator and answered 502 itself.
    const out = truncateFailure(
      new HistoryApiError(502, 'History request failed with status 502.'),
    );
    expect(out.message).not.toContain('changed elsewhere');
    expect(out.message).toContain('Could not reach the server');
    // Nothing was destroyed, so there is nothing to re-read — and the server
    // that just failed cannot answer a re-read either.
    expect(out.reload).toBe(false);
  });

  it('says nothing was changed, so a retry is obviously safe', () => {
    const out = truncateFailure(new HistoryApiError(502, 'x'));
    expect(out.message).toContain('nothing was changed');
  });

  it('handles a dead network (status 0) the same way', () => {
    const out = truncateFailure(
      new HistoryApiError(0, 'History server unreachable.'),
    );
    expect(out.reload).toBe(false);
    expect(out.message).toContain('Could not reach the server');
  });

  it('handles a server error (500) the same way', () => {
    const out = truncateFailure(new HistoryApiError(500, 'boom'));
    expect(out.reload).toBe(false);
    expect(out.message).not.toContain('changed elsewhere');
  });

  it('handles a thrown non-HistoryApiError without inventing a conflict', () => {
    for (const err of [new Error('boom'), 'a string', null, undefined]) {
      const out = truncateFailure(err);
      expect(out.reload).toBe(false);
      expect(out.message).toContain('Could not reach the server');
    }
  });
});

describe('truncateFailure — the copy itself', () => {
  it('never returns an upstream sentence for display', () => {
    // Upstream bodies can carry a DSN, an echoed header or a traceback; the
    // rule in this codebase is that displayed copy is copy WE wrote.
    const leak = 'postgres://user:hunter2@10.0.0.4:5432/techsara exploded';
    for (const status of [0, 404, 409, 500, 502]) {
      const out = truncateFailure(new HistoryApiError(status, leak));
      expect(out.message).not.toContain(leak);
      expect(out.message).not.toContain('hunter2');
    }
  });

  it('always says something, whatever went wrong', () => {
    for (const status of [0, 400, 404, 409, 418, 500, 502, 503]) {
      const out = truncateFailure(new HistoryApiError(status, 'x'));
      expect(out.message.trim().length).toBeGreaterThan(0);
    }
  });
});
