// @vitest-environment jsdom
/**
 * EDIT-SAME — sending an edit with its text unchanged is a REGENERATE
 * (owner request 2026-09-03).
 *
 * It used to just close the editor: nothing happened, which read as a broken
 * button. Creating a second identical version would have been worse — a
 * `1 / 2` with nothing to navigate. So the unchanged submit now does exactly
 * what "Try again" does, through the same function, confirmation rules and
 * branch rules included.
 *
 * Real ChatApp, real startStream, captured /api/chat bodies — the version
 * counts are read from what was STORED, not from labels.
 */

import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  assistantTurns,
  attach,
  chatBodies,
  editTo,
  lastBody,
  mockHistory,
  pdf,
  regenerateLast,
  renderApp,
  resetHarnessState,
  send,
  setUploadFails,
  stubEnv,
  userTurns,
  waitForAnswers,
} from './_wireHarness';
import { clearAttachments } from '@/lib/attachments';

mockHistory();
const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');

/** Whose row a `n / total` navigator sits in: the nearest message bubble up the tree. */
function navigatorOwner(nav: Element): string | null {
  let el = nav.parentElement;
  while (el && !el.querySelector('[data-chat-message-role]')) el = el.parentElement;
  return el?.querySelector('[data-chat-message-role]')?.getAttribute('data-chat-message-role') ?? null;
}

const lastUserContent = () =>
  lastBody().messages?.filter((m) => m.role === 'user').pop()?.content;

beforeEach(() => {
  resetHarnessState();
  stubEnv();
  clearAttachments();
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
  renderApp(ChatApp, Providers);
});

afterEach(async () => {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('EDIT-SAME · unchanged text regenerates', () => {
  it('01/02/03 · no new user version; the same turn; a new answer is generated', async () => {
    await send('Explain attention.');
    expect(userTurns()).toHaveLength(1);
    const originalId = userTurns()[0].id;

    await editTo(null);
    await waitFor(() => expect(chatBodies.length).toBe(2));
    await waitForAnswers(1);

    expect(userTurns()).toHaveLength(1);
    expect(userTurns()[0].id).toBe(originalId);
    expect(lastUserContent()).toBe('Explain attention.');
    // No `1 / 2` on the QUESTION. Since 2026-09-13 a regenerate keeps the
    // earlier answer as a version, so the `2 / 2` that does appear is the
    // answer's — exactly what "Try again" shows.
    const navigators = await screen.findAllByText(/^\d+ \/ \d+$/);
    expect(navigators.map((n) => [n.textContent, navigatorOwner(n)])).toEqual([
      ['2 / 2', 'assistant'],
    ]);
    expect(assistantTurns()).toHaveLength(2);
    // The editor closed and the page is back to one visible turn.
    expect(screen.queryByRole('textbox', { name: 'Edit your message' })).toBeNull();
  });

  it('04 · behaves exactly like "Try again" — each adds one answer version and no user turn', async () => {
    await send('Explain attention.');
    const before = { users: userTurns().length, assistants: assistantTurns().length };
    await regenerateLast();
    await waitFor(() => expect(chatBodies.length).toBe(2));
    await waitForAnswers(1);
    const afterRegenerate = {
      users: userTurns().length,
      assistants: assistantTurns().length,
    };
    expect(afterRegenerate).toEqual({ users: before.users, assistants: before.assistants + 1 });

    await editTo(null);
    await waitFor(() => expect(chatBodies.length).toBe(3));
    await waitForAnswers(1);
    expect({ users: userTurns().length, assistants: assistantTurns().length }).toEqual({
      users: afterRegenerate.users,
      assistants: afterRegenerate.assistants + 1,
    });
    // Both requests said where the new answer goes, the same way.
    expect(chatBodies[1].answer_branch?.parent).toBe(userTurns()[0].meta?.branch?.self);
    expect(chatBodies[2].answer_branch?.parent).toBe(userTurns()[0].meta?.branch?.self);
  });

  it('04 · in a conversation WITH versions it appends an alternative answer', async () => {
    await send('Explain attention.');
    // A real edit first, so the conversation has branches…
    await editTo('Explain attention with an example.');
    await waitFor(() => expect(chatBodies.length).toBe(2));
    await waitForAnswers(1);
    expect(userTurns()).toHaveLength(2);
    const assistantsBefore = assistantTurns().length;

    // …then an unchanged submit on the version now on screen.
    await editTo(null);
    await waitFor(() => expect(chatBodies.length).toBe(3));
    await waitForAnswers(1);

    expect(userTurns()).toHaveLength(2); // still two, not three
    expect(assistantTurns().length).toBe(assistantsBefore + 1); // one more answer
    expect(lastUserContent()).toBe('Explain attention with an example.');
  });

  it('05 · changed text still creates a normal user version', async () => {
    await send('Read these files.');
    await editTo('Compare these files.');
    await waitFor(() => expect(chatBodies.length).toBe(2));
    expect(userTurns()).toHaveLength(2);
    expect(userTurns()[1].content).toBe('Compare these files.');
    expect(lastUserContent()).toBe('Compare these files.');
    await waitForAnswers(1);
    expect(screen.getByText(/2 \/ 2/)).toBeTruthy();
    // …and that navigator is the QUESTION's, which is what tells a real edit
    // apart from the unchanged one above.
    expect(navigatorOwner(screen.getByText(/2 \/ 2/))).toBe('user');
  });

  it('the comparison is the editor\'s trim, so a moved line break IS an edit', async () => {
    await send('Read these files');
    await editTo('Read\nthese files');
    await waitFor(() => expect(chatBodies.length).toBe(2));
    expect(userTurns()).toHaveLength(2);
  });

  it('06 · an attachment-only turn cannot be re-sent empty', async () => {
    await attach([pdf('report.pdf')]);
    await send('');
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }));
    });
    const sendBtn = screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(true);
    expect(chatBodies.length).toBe(1);
  });

  it('09 · a quoted excerpt is re-sent exactly once, never re-wrapped', async () => {
    await send('Why does this happen?');
    userTurns()[0].meta = {
      ...(userTurns()[0].meta ?? {}),
      selected_context: { text: 'drift', messageId: 'x', sourceRole: 'assistant' },
    };
    await editTo(null);
    await waitFor(() => expect(chatBodies.length).toBe(2));
    const content = lastUserContent() ?? '';
    expect(content.match(/Selected context from/g)).toHaveLength(1);
    expect(content).toContain('> drift');
  });

  it('10 · a resend that cannot be rebuilt errors ONCE, however often Send is hit', async () => {
    // No durable id (the upload failed) and no bytes (a reload) — the honest
    // "re-attach" case. Note the send here is a single small document that
    // rides inline, so the answer itself still happens.
    setUploadFails(true);
    await attach([pdf('report.pdf')]);
    await send('Summarize this.');
    clearAttachments();

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }));
    });
    const editor = screen.getByRole('textbox', { name: 'Edit your message' });
    await act(async () => {
      fireEvent.change(editor, { target: { value: 'Summarize this differently.' } });
    });
    for (let i = 0; i < 5; i += 1) {
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Send' }));
      });
    }
    expect(await screen.findAllByText(/Re-attach the file to edit/i)).toHaveLength(1);
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(chatBodies.length).toBe(1);
  });
});
