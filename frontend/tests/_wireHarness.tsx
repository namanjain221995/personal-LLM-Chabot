// @vitest-environment jsdom
/**
 * Shared real-wire harness (2026-09-03): the REAL ChatApp through the REAL
 * startStream, with only the network stubbed, so the JSON actually posted to
 * /api/chat can be asserted on. Cloned from PART 2 of
 * tests/dataset-chat-request.test.tsx, plus an /api/upload stub that mints a
 * DISTINCT 32-hex upload id per call — the identity the multi-document tests
 * are about.
 *
 * Not a test file itself (no `.test.` in the name); vitest only collects
 * `tests/**\/*.test.ts(x)`.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, vi } from 'vitest';
import type { ChatRequestBody } from '@/lib/orchestrator';
import type { ChatMessage } from '@/lib/types';

export const ANSWER = 'Here is the answer.';

export let stored: ChatMessage[] = [];
export let chatBodies: ChatRequestBody[] = [];
export let uploads: { name: string; upload_id: string }[] = [];
/** Flip to make every /api/upload fail — the "no durable id" case. */
let uploadFails = false;
export function setUploadFails(v: boolean) {
  uploadFails = v;
}

export function resetHarnessState() {
  stored = [];
  chatBodies = [];
  uploads = [];
  uploadFails = false;
}

export function mockHistory() {
  vi.mock('@/lib/history', () => ({
    newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
    setEvictListener: () => undefined,
    rebuildHistoryStore: async () => {
      throw new Error('unexpected account switch in test');
    },
    getHistoryStore: () => ({
      ready: async () => undefined,
      list: () => [],
      listArchived: () => [],
      get: () => null,
      create: (title: string) => ({
        id: 'conv-1',
        title,
        messages: [],
        createdAt: 0,
        updatedAt: 0,
      }),
      saveMessages: (_id: string, msgs: ChatMessage[]) => {
        stored = msgs;
      },
      load: async () => null,
      setActiveUser: () => false,
      wipeLocal: async () => undefined,
      migrateLocalConversations: async () => 0,
      refresh: async () => true,
      refreshArchived: async () => true,
      generateTitle: async () => undefined,
      truncateMessages: async () => undefined,
      setMessageFeedback: async () => undefined,
      exportMarkdown: async () => null,
      remove: () => undefined,
      rename: () => undefined,
      setPinned: () => undefined,
      setArchived: () => undefined,
    }),
  }));
  vi.mock('@/lib/auth', () => ({
    fetchMe: async () => ({ ok: true, username: 'tester', user: null }),
    userScopeKey: () => 'tester',
    redirectToLogin: () => undefined,
  }));
  vi.mock('@/lib/salesforceApi', () => ({
    fetchSalesforceContext: async () => ({ options: [], pending: null }),
    cancelClarification: async () => undefined,
    shouldShowStarter: () => false,
  }));
  vi.mock('@/lib/compact', () => ({
    isCompacting: () => false,
    requestCompact: async () => null,
  }));
}

/** A one-token answer then `done`, so the real consume() finishes cleanly. */
function sseBody(text: string): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(c) {
      const enc = new TextEncoder();
      c.enqueue(enc.encode(`event: token\ndata: ${JSON.stringify({ text })}\n\n`));
      c.enqueue(enc.encode('event: done\ndata: {}\n\n'));
      c.close();
    },
  });
}

export function stubEnv() {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => undefined);
  HTMLMediaElement.prototype.play = async () => undefined;
  HTMLMediaElement.prototype.pause = () => undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.startsWith('/api/chat')) {
        chatBodies.push(JSON.parse(String(init?.body)) as ChatRequestBody);
        return { ok: true, status: 200, body: sseBody(ANSWER) };
      }
      if (u === '/api/upload') {
        if (uploadFails) {
          return { ok: false, status: 500, json: async () => ({ detail: 'upload failed' }) };
        }
        const form = init?.body as FormData;
        const file = form.get('file');
        const name = file instanceof File ? file.name : 'doc.pdf';
        // Distinct, valid-looking ids: 31 zeros + a hex digit per upload.
        const upload_id = `${'0'.repeat(31)}${(uploads.length + 1).toString(16)}`;
        uploads.push({ name, upload_id });
        return {
          ok: true,
          status: 200,
          json: async () => ({ upload_id, filename: name, files: 1 }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    }),
  );
}

export const pdf = (name: string, bytes = `%PDF-1.4 ${name}`) =>
  new File([bytes], name, { type: 'application/pdf' });
export const png = (name: string) => new File(['x'], name, { type: 'image/png' });

export const box = () => screen.getByRole('textbox', { name: 'Message' });
export const userTurns = () => stored.filter((m) => m.role === 'user');
export const assistantTurns = () => stored.filter((m) => m.role === 'assistant');
export const lastBody = () => chatBodies[chatBodies.length - 1];

/** Attach files through the picker and wait for every chip. */
export async function attach(files: File[]) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files } });
  });
  for (const f of files) await screen.findByLabelText(`Remove attachment ${f.name}`);
}

/** Type and press Send, then wait for the answer to land on screen. */
export async function send(text: string) {
  const before = chatBodies.length;
  await act(async () => {
    if (text) fireEvent.change(box(), { target: { value: text } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
  await waitFor(() => expect(chatBodies.length).toBe(before + 1), { timeout: 4000 });
  await waitForAnswers(before + 1);
}

/** The Nth answer is on screen (the stream closed, so regenerate is allowed). */
export async function waitForAnswers(n: number) {
  await waitFor(
    () =>
      expect(
        document.querySelectorAll('[data-chat-message-role="assistant"]').length,
      ).toBe(n),
    { timeout: 4000 },
  );
}

/** Open the inline editor on the (only visible) user turn and submit `text`. */
export async function editTo(text: string | null) {
  await act(async () => {
    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }));
  });
  const editor = screen.getByRole('textbox', { name: 'Edit your message' }) as HTMLTextAreaElement;
  await act(async () => {
    if (text !== null) fireEvent.change(editor, { target: { value: text } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  });
  return editor;
}

export async function regenerateLast() {
  const buttons = screen.getAllByRole('button', { name: /Try again/i });
  await act(async () => {
    fireEvent.click(buttons[buttons.length - 1]);
  });
}

export function renderApp(ChatApp: React.ComponentType, Providers: React.ComponentType<{ children: React.ReactNode }>) {
  return render(
    <Providers>
      <ChatApp />
    </Providers>,
  );
}
