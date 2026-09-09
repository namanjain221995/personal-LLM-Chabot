// @vitest-environment jsdom
/**
 * UNSENT TURN (2026-09-09) — a message saved while its files were uploading
 * must never pose as sent after a reload.
 *
 * What happened: a turn with two videos was saved (so a reload keeps the
 * chips), the 20 MB one uploaded, the 400 MB one was still going when the
 * page was reloaded, and the chat request that waits for both never went
 * out. The person saw two "sent" chips, no answer and no error, while the
 * server had already analysed the video that did land. The turn now carries
 * `meta.send_state` until the request starts, and the row reads it.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageRow } from '@/components/MessageRow';
import { unsentTurnState } from '@/components/ChatApp';
import type { ChatMessage } from '@/lib/types';

afterEach(cleanup);

function turn(attachments: { name: string; id?: string }[]): ChatMessage {
  return {
    id: 'u1',
    role: 'user',
    content: '',
    meta: {
      send_state: 'uploading',
      attachments: attachments.map((a) => ({
        kind: 'video',
        name: a.name,
        ...(a.id ? { id: a.id } : {}),
      })),
    },
  } as ChatMessage;
}

describe('a user turn whose uploads never finished', () => {
  it('names the file that did not land, and the one that did', () => {
    const m = turn([{ name: 'big.mp4' }, { name: 'small.mp4', id: 'a'.repeat(32) }]);
    const state = unsentTurnState(m);
    expect(state.missing).toEqual(['big.mp4']);
    expect(state.kept).toEqual(['small.mp4']);
    expect(state.canResend).toBe(false);
    render(
      <MessageRow
        message={m}
        isLast={false}
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
        unsent={state}
      />,
    );
    const notice = screen.getByTestId('unsent-turn');
    expect(notice.textContent).toContain('never sent');
    expect(notice.textContent).toContain('big.mp4 was still uploading');
    expect(notice.textContent).toContain('small.mp4 stayed attached to this chat');
    expect(screen.queryByRole('button', { name: 'Send now' })).toBeNull();
  });

  it('offers Send now when every file it names is on the server', () => {
    const m = turn([
      { name: 'a.mp4', id: 'a'.repeat(32) },
      { name: 'b.mp4', id: 'b'.repeat(32) },
    ]);
    const state = unsentTurnState(m);
    expect(state.canResend).toBe(true);
    const onRegenerate = vi.fn();
    render(
      <MessageRow
        message={m}
        isLast={false}
        onRegenerate={onRegenerate}
        onRetry={vi.fn()}
        unsent={state}
      />,
    );
    expect(screen.getByTestId('unsent-turn').textContent).toContain('never sent');
    fireEvent.click(screen.getByRole('button', { name: 'Send now' }));
    expect(onRegenerate).toHaveBeenCalledTimes(1);
  });

  it('says nothing when the row is not told the turn is unsent', () => {
    const m = turn([{ name: 'a.mp4', id: 'a'.repeat(32) }]);
    render(<MessageRow message={m} isLast={false} onRegenerate={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.queryByTestId('unsent-turn')).toBeNull();
  });
});
