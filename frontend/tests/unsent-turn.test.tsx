// @vitest-environment jsdom
/**
 * UNSENT TURN (2026-09-09, rewritten 2026-09-10) — a message saved while its
 * files were uploading must never pose as sent, and must never pose as LOST
 * either.
 *
 * What happened: a turn with two videos was saved (so a reload keeps the
 * chips), the 20 MB one uploaded, the 400 MB one was still going when the
 * page was reloaded, and the chat request that waits for both never went
 * out. The person saw two "sent" chips, no answer and no error, while the
 * server had already analysed the video that did land.
 *
 * The 2026-09-09 fix said so with one sentence — "the page closed while its
 * files were still uploading" — for every way a send can fail (T-02): a
 * refused upload, a 413 at the edge, a dead network and a page that really
 * did close all read identically, and the notice could appear over a
 * generation that was running perfectly well. What the row shows is now a
 * decision made by the host from the intent, the server's answer and this
 * tab's own work (ChatApp.userTurnView), and every state has its own
 * sentence and its own action.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageRow, type UserTurnView } from '@/components/MessageRow';
import { unsentTurnState, userTurnView } from '@/components/ChatApp';
import type { ChatMessage, SendIntent } from '@/lib/types';

afterEach(cleanup);

function turn(
  attachments: { name: string; id?: string }[],
  intent?: SendIntent,
): ChatMessage {
  return {
    id: 'u1',
    role: 'user',
    content: '',
    meta: {
      ...(intent ? { intent } : { send_state: 'uploading' }),
      attachments: attachments.map((a) => ({
        kind: 'video',
        name: a.name,
        ...(a.id ? { id: a.id } : {}),
      })),
    },
  } as ChatMessage;
}

/** Everything settled: the server answered and nothing is running. */
const settled = {
  isLast: true,
  streamingHere: false,
  serverBusy: false,
  uploadingHere: false,
  reconciling: false,
  statusUnknown: false,
  reconnect: null,
};

function row(message: ChatMessage, view: UserTurnView | null) {
  return render(
    <MessageRow
      message={message}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      turn={view}
    />,
  );
}

describe('a user turn whose uploads never finished', () => {
  it('names the file that did not land, and the one that did', () => {
    const m = turn([{ name: 'big.mp4' }, { name: 'small.mp4', id: 'a'.repeat(32) }], {
      id: 'i1',
      state: 'unsent',
      reason: 'big.mp4 did not finish uploading, so nothing was sent.',
    });
    const state = unsentTurnState(m);
    expect(state.missing).toEqual(['big.mp4']);
    expect(state.kept).toEqual(['small.mp4']);
    expect(state.canResend).toBe(false);

    const view = userTurnView(m, settled)!;
    expect(view.kind).toBe('unsent');
    row(m, view);
    const notice = screen.getByTestId('unsent-turn');
    expect(notice.textContent).toContain('big.mp4 did not finish uploading');
    expect(notice.textContent).toContain('small.mp4 is on the server');
    // Never a silent subset: the only send offered is the explicit one.
    expect(screen.queryByRole('button', { name: 'Send now' })).toBeNull();
    expect(
      screen.getByRole('button', { name: 'Send with small.mp4' }),
    ).toBeTruthy();
  });

  it('offers Send now when every file it names is on the server', () => {
    const m = turn(
      [
        { name: 'a.mp4', id: 'a'.repeat(32) },
        { name: 'b.mp4', id: 'b'.repeat(32) },
      ],
      { id: 'i2', state: 'unsent' },
    );
    const view = userTurnView(m, settled)!;
    expect(view.canResend).toBe(true);
    const onRegenerate = vi.fn();
    render(
      <MessageRow
        message={m}
        isLast={false}
        onRegenerate={onRegenerate}
        onRetry={vi.fn()}
        turn={view}
      />,
    );
    expect(screen.getByTestId('unsent-turn').textContent).toContain(
      'never sent',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Send now' }));
    expect(onRegenerate).toHaveBeenCalledTimes(1);
  });

  it('says nothing when the row is not told anything about the send', () => {
    const m = turn([{ name: 'a.mp4', id: 'a'.repeat(32) }]);
    render(
      <MessageRow message={m} isLast={false} onRegenerate={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(screen.queryByTestId('unsent-turn')).toBeNull();
  });
});

/* ------------------------------------------------------------------ T-02 */

describe('the notice tells the truth about WHY (T-02)', () => {
  it('a refused upload is reported as the refusal, not as "the page closed"', () => {
    const m = turn([{ name: 'huge.mp4' }], {
      id: 'i3',
      state: 'unsent',
      reason: 'huge.mp4 is larger than the 4 GB limit, so nothing was sent.',
    });
    row(m, userTurnView(m, settled));
    const notice = screen.getByTestId('unsent-turn');
    expect(notice.textContent).toContain('larger than the 4 GB limit');
    expect(notice.textContent).not.toContain('page closed');
  });

  it('a failed generation says so and offers Retry', () => {
    const m = turn([{ name: 'a.mp4', id: 'a'.repeat(32) }], {
      id: 'i4',
      state: 'failed',
      reason: 'The server stopped working on this answer.',
    });
    row(m, userTurnView(m, settled));
    expect(screen.getByTestId('unsent-turn').textContent).toContain(
      'The server stopped working on this answer.',
    );
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });
});

/* ---------------------------------------------- the states that are NOT lost */

describe('what a turn says when it is not lost', () => {
  it('an accepted send says the server has it, and offers Stop', () => {
    const m = turn([{ name: 'a.mp4', id: 'a'.repeat(32) }], {
      id: 'i5',
      state: 'accepted',
      generation_id: 'g1',
    });
    const view = userTurnView(m, settled)!;
    expect(view.kind).toBe('working');
    const onStopTurn = vi.fn();
    render(
      <MessageRow
        message={m}
        isLast={false}
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
        turn={view}
        onStopTurn={onStopTurn}
      />,
    );
    expect(screen.queryByTestId('unsent-turn')).toBeNull();
    expect(screen.getByTestId('turn-working').textContent).toContain(
      'the server has it',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(onStopTurn).toHaveBeenCalledTimes(1);
  });

  it('an interrupted send says it is resuming — never "never sent"', () => {
    const m = turn([{ name: 'a.mp4', id: 'a'.repeat(32) }], {
      id: 'i6',
      state: 'interrupted',
    });
    const view = userTurnView(m, {
      ...settled,
      reconnect: { attempt: 1, statusUnknown: false, exhausted: false },
    })!;
    expect(view.kind).toBe('interrupted');
    row(m, view);
    expect(screen.queryByTestId('unsent-turn')).toBeNull();
    expect(screen.getByTestId('turn-interrupted').textContent).toContain(
      'Resuming',
    );
  });

  it('a stalled reconnect offers Resume rather than pretending to work', () => {
    const m = turn([{ name: 'a.mp4', id: 'a'.repeat(32) }], {
      id: 'i7',
      state: 'interrupted',
    });
    const view = userTurnView(m, {
      ...settled,
      reconnect: { attempt: 20, statusUnknown: false, exhausted: true },
    })!;
    row(m, view);
    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
  });

  it('an unreachable server is "checking", with a Retry — not a red notice', () => {
    const m = turn([{ name: 'a.mp4', id: 'a'.repeat(32) }], {
      id: 'i8',
      state: 'accepted',
    });
    const view = userTurnView(m, { ...settled, serverBusy: false, statusUnknown: true })!;
    expect(view.kind).toBe('working'); // accepted outranks: the server had it
    const unknown = userTurnView(
      { ...m, meta: { ...m.meta, intent: { id: 'i8', state: 'unsent' } } } as ChatMessage,
      { ...settled, statusUnknown: true },
    )!;
    expect(unknown.kind).toBe('unsent'); // the server DID say so, earlier
    row(m, userTurnView({ ...m, meta: { ...m.meta, intent: undefined, send_state: 'uploading' } } as ChatMessage, {
      ...settled,
      statusUnknown: true,
    }));
    expect(screen.queryByTestId('unsent-turn')).toBeNull();
    expect(screen.getByTestId('turn-status_unknown').textContent).toContain(
      'Checking with the server',
    );
  });
});

/* ------------------------------------------------------- legacy rows (14a) */

describe('rows written before intents existed', () => {
  const legacy = () => turn([{ name: 'a.mp4', id: 'a'.repeat(32) }]);

  it('warn ONLY after the server has answered', () => {
    // Still asking: nothing may claim the turn was lost (T-01).
    expect(userTurnView(legacy(), { ...settled, reconciling: true })!.kind).toBe(
      'status_unknown',
    );
    // The ask failed: still not an answer about the send.
    expect(
      userTurnView(legacy(), { ...settled, statusUnknown: true })!.kind,
    ).toBe('status_unknown');
    // The server is generating for this conversation: it went out after all.
    expect(userTurnView(legacy(), { ...settled, serverBusy: true })!.kind).toBe(
      'working',
    );
    // Asked, answered, nothing running: now it may say so.
    expect(userTurnView(legacy(), settled)!.kind).toBe('unsent');
  });

  it('say nothing at all while this tab is still uploading for the turn', () => {
    expect(userTurnView(legacy(), { ...settled, uploadingHere: true })).toBeNull();
  });

  it('say nothing on a turn that is not the last one', () => {
    expect(userTurnView(legacy(), { ...settled, isLast: false })).toBeNull();
  });
});
