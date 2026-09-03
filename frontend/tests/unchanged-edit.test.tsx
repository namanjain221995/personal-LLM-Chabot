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
    expect(screen.queryByText(/2 \/ 2/)).toBeNull();
    // The editor closed and the page is back to one visible turn.
    expect(screen.queryByRole('textbox', { name: 'Edit your message' })).toBeNull();
  });

  it('04 · behaves exactly like "Try again" — same stored shape after each', async () => {
    await send('Explain attention.');
    await regenerateLast();
    await waitFor(() => expect(chatBodies.length).toBe(2));
    await waitForAnswers(1);
    const afterRegenerate = {
      users: userTurns().length,
      assistants: assistantTurns().length,
    };

    await editTo(null);
    await waitFor(() => expect(chatBodies.length).toBe(3));
    await waitForAnswers(1);
    expect({ users: userTurns().length, assistants: assistantTurns().length }).toEqual(
      afterRegenerate,
    );
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
