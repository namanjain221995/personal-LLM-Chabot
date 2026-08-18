// @vitest-environment jsdom
/**
 * What the TRANSCRIPT does with a clarification.
 *
 * The answer is: as little as possible. The live question is a temporary
 * control owned by the composer, so a message row renders nothing for it; once
 * answered it leaves one quiet, non-interactive line so the user turn that
 * follows ("Interview") reads as the answer it is.
 *
 * This is the join neither the panel's own tests nor the pure logic can see,
 * and it was the actual production defect: every message carrying
 * `meta.clarification` rendered an interactive card, so once an answer came
 * back and the in-flight lock cleared, clicking a different option on a
 * question from ten turns ago started a fresh run against a dead intent.
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageRow } from '@/components/MessageRow';
import type { ChatMessage } from '@/lib/types';

afterEach(cleanup);

const CARD = {
  clarification_id: 'clr_1',
  conversation_id: 'conv',
  run_id: 'run',
  root_user_message_id: 'msg',
  intent_id: 'int',
  source: 'salesforce',
  header: 'Mock count',
  question: 'Do you want the interviews or the candidates?',
  slot: 'metric',
  options: [
    { id: 'iv', label: 'Count of interviews', value: 'interview_count' },
    { id: 'cd', label: 'Count of candidates', value: 'candidate_count' },
  ],
  allow_custom: true,
  custom_placeholder: 'Tell me what you meant…',
  multi_select: false,
  round_number: 1,
  created_at: '2026-08-17T09:00:00+00:00',
  state: 'pending',
  resume_token: 'tok',
  question_fingerprint: 'fp',
};

function row(overrides: Record<string, unknown> = {}) {
  const message: ChatMessage = {
    id: 'a1',
    role: 'assistant',
    content: 'Do you want the interviews or the candidates?',
    createdAt: 0,
    meta: { route: 'clarify', clarification: CARD },
  } as ChatMessage;
  return render(
    <MessageRow
      message={message}
      isLast
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      {...overrides}
    />,
  );
}

describe('while the question is live', () => {
  it('renders no control at all — the composer owns it', () => {
    row({ clarificationPending: true });
    expect(screen.queryByTestId('clarification-card')).toBeNull();
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.queryByRole('button', { name: /Count of interviews/ })).toBeNull();
  });

  it('does not repeat the question as prose either', () => {
    // It is streamed as text as well, so a client with no panel renderer and
    // the stored history a future one reads back both show something usable.
    // On screen the panel above the composer IS the question; printing it here
    // too would show the same words twice, one copy inert.
    row({ clarificationPending: true });
    expect(
      screen.queryByText(/Do you want the interviews or the candidates/),
    ).toBeNull();
  });
});

describe('once the question has been answered', () => {
  it('leaves one quiet line saying what was asked and what was chosen', () => {
    row({ clarificationPending: false, clarificationAnswer: 'Count of interviews' });
    expect(screen.getByText('Count of interviews')).toBeTruthy();
    expect(
      screen.getByText(/Do you want the interviews or the candidates/),
    ).toBeTruthy();
  });

  it('leaves NOTHING clickable behind', () => {
    row({ clarificationPending: false, clarificationAnswer: 'Count of interviews' });
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.queryByRole('radiogroup')).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByRole('button', { name: /Count of/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Something else/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Skip/ })).toBeNull();
  });

  it('still says it was answered when the answer was a skip', () => {
    // A skip leaves no user turn to quote.
    row({ clarificationPending: false, clarificationAnswer: '' });
    expect(screen.getByText('Answered')).toBeTruthy();
  });
});

describe('a conversation persisted before the typed panel existed', () => {
  it('renders its question as ordinary prose rather than crashing', () => {
    // The legacy `meta.clarify` payload is gone from the contract. A stored
    // message that still carries one has readable content underneath it —
    // which is what that card always was.
    const legacy = {
      id: 'a0',
      role: 'assistant',
      content: 'Which interviews do you mean?\n\n**1.** Client-facing\n**2.** Internal',
      createdAt: 0,
      meta: {
        route: 'clarify',
        clarify: { question: 'Which interviews do you mean?', options: [] },
      },
    } as unknown as ChatMessage;
    render(
      <MessageRow message={legacy} isLast onRegenerate={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(screen.getByText(/Which interviews do you mean/)).toBeTruthy();
    expect(screen.queryByTestId('clarification-card')).toBeNull();
    expect(screen.queryByRole('radio')).toBeNull();
  });
});
