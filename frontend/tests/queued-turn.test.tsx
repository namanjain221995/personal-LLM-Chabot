// @vitest-environment jsdom
/**
 * QUEUED TURN (2026-09-12) — what the person SEES while the main model
 * recovers (docs/availability/CONTRACT.md §8.3, strict one-model mode).
 *
 * The server holds the request and says so in one sentence; nothing here may
 * turn that into a failure. Two rows can carry the state — the answer
 * placeholder while the stream layer still holds it (live, or the
 * `meta.error` a reload reads back), and the question itself once the
 * placeholder is gone (ChatApp.userTurnView) — and both must show the
 * server's sentence, no red, no Retry, and one small honest indicator of
 * what is being waited for. The copy behind the code is checked too: it has
 * to say the request is kept and will resume, and it must not be retryable.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageRow } from '@/components/MessageRow';
import { intentForRetry, userTurnView } from '@/components/ChatApp';
import { copyForCategory, toClientError } from '@/lib/errorTypes';
import type { ChatMessage } from '@/lib/types';

afterEach(cleanup);

//: The exact sentence of orchestrator/app/continuity.py EXPIRED_LINE.
const EXPIRED_LINE =
  'The main model is still recovering. Your request is kept and will resume automatically.';

/** Everything settled: nothing streaming here, the server answered. */
const settled = {
  isLast: true,
  streamingHere: false,
  serverBusy: false,
  uploadingHere: false,
  reconciling: false,
  statusUnknown: false,
  reconnect: null,
};

function row(message: ChatMessage, turn = userTurnView(message, settled)) {
  return render(
    <MessageRow
      message={message}
      isLast={message.role === 'assistant'}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      turn={turn}
      onStopTurn={vi.fn()}
    />,
  );
}

const question = (reason?: string): ChatMessage =>
  ({
    id: 'u1',
    role: 'user',
    content: 'What was decided?',
    createdAt: 1,
    meta: { intent: { id: 'i1', state: 'queued', generation_id: 'g1', ...(reason ? { reason } : {}) } },
  }) as ChatMessage;

const parkedAnswer = (over: Partial<ChatMessage> = {}): ChatMessage =>
  ({
    id: 'a1',
    role: 'assistant',
    content: '',
    status: 'queued',
    createdAt: 2,
    meta: {
      generation_id: 'g1',
      error: { message: EXPIRED_LINE, code: 'MODEL_RECOVERING', status: null, resumable: true },
    },
    ...over,
  }) as ChatMessage;

/** No failure styling and no way to "try again" anywhere in the row. */
function expectNoFailure() {
  expect(screen.queryByRole('alert')).toBeNull();
  expect(screen.queryByTestId('unsent-turn')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Send now' })).toBeNull();
  expect(document.querySelector('.text-danger')).toBeNull();
}

/* ------------------------------------------------------- the category */

describe('the MODEL_RECOVERING category', () => {
  it('says the request is kept and will resume, and is not retryable', () => {
    const copy = copyForCategory('MODEL_RECOVERING');
    expect(copy.message).toMatch(/kept/i);
    expect(copy.message).toMatch(/resume/i);
    expect(copy.retryable).toBe(false);
    const err = toClientError(null, 'MODEL_RECOVERING');
    expect(err.code).toBe('MODEL_RECOVERING');
    expect(err.retryable).toBe(false);
    // No status was ever received for a parked request: no number is shown.
    expect(err.display).toBe('Error');
  });
});

/* ---------------------------------------------- the answer placeholder */

describe('the parked answer row', () => {
  it('shows the server sentence as its status, with the indicator and nothing red', () => {
    row(parkedAnswer());
    const status = screen.getByTestId('queued-turn');
    expect(status.textContent).toContain(EXPIRED_LINE);
    expect(screen.getByTestId('queued-indicator').textContent).toBe(
      'Waiting for the main model to recover…',
    );
    expectNoFailure();
    // Not the ordinary first-token wait either: the sentence IS the state.
    expect(screen.queryByLabelText('Waiting for the first token')).toBeNull();
  });

  it('is not overwritten by a thinking label while parked', () => {
    // A reasoning delta from before the engine went away is still on the
    // message; a parked turn is not thinking, and must not say so.
    row(parkedAnswer({ reasoning: 'Let me consider the options' }));
    expect(screen.queryByText('Thinking…')).toBeNull();
    expect(screen.getByTestId('queued-turn').textContent).toContain(EXPIRED_LINE);
  });

  it('reads the same state back after a reload, from meta.error alone', () => {
    // The history loader rehydrates every assistant row as `done`; the
    // persisted MODEL_RECOVERING record is what survives.
    row(parkedAnswer({ status: 'done' }));
    expect(screen.getByTestId('queued-turn').textContent).toContain(EXPIRED_LINE);
    expectNoFailure();
  });

  it('falls back to the category copy when the record carries no sentence', () => {
    row(parkedAnswer({ meta: { error: { message: '', code: 'MODEL_RECOVERING' } } }));
    expect(screen.getByTestId('queued-turn').textContent).toContain(
      copyForCategory('MODEL_RECOVERING').message,
    );
  });

  it('a genuine failure still renders as one', () => {
    row({
      ...parkedAnswer(),
      status: 'error',
      errorMessage: 'Engine core died',
      meta: { error: { message: 'Engine core died', resumable: false } },
    } as ChatMessage);
    expect(screen.queryByTestId('queued-turn')).toBeNull();
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });
});

/* -------------------------------------------------------- the question */

describe('the question once the placeholder is gone (a reload)', () => {
  it('is a queued turn — the server sentence, the indicator, no action', () => {
    const m = question(EXPIRED_LINE);
    const view = userTurnView(m, settled)!;
    expect(view.kind).toBe('queued');
    row(m, view);
    const turn = screen.getByTestId('turn-queued');
    expect(turn.textContent).toContain(EXPIRED_LINE);
    expect(turn.textContent).toContain('Waiting for the main model to recover…');
    expect(turn.querySelector('button')).toBeNull();
    expectNoFailure();
    // Not "working": Stop would cancel nothing, and "the server has it" is
    // less than the truth.
    expect(screen.queryByTestId('turn-working')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('uses the category copy when the intent has no sentence yet', () => {
    const m = question();
    row(m);
    expect(screen.getByTestId('turn-queued').textContent).toContain(
      copyForCategory('MODEL_RECOVERING').message,
    );
  });

  it('is "checking" only when the reconnect itself could not ask', () => {
    const m = question(EXPIRED_LINE);
    const view = userTurnView(m, {
      ...settled,
      reconnect: { attempt: 2, statusUnknown: true, exhausted: false },
    })!;
    expect(view.kind).toBe('status_unknown');
    // A failed /chat/active ask alone does not demote it: the server HAD it.
    expect(userTurnView(m, { ...settled, statusUnknown: true })!.kind).toBe('queued');
  });

  it('keeps its intent for a retry, so a re-send can never be a second generation', () => {
    expect(intentForRetry(question(EXPIRED_LINE))).toBe('i1');
  });
});
