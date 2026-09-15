// @vitest-environment jsdom
/**
 * AS3 fix B4 — the chat host for "Edit with a prompt" and "Restore vN".
 *
 * Before the fix ChatApp neither passed `onEditPrompt` down to the cards nor
 * registered as the ARTIFACT_EDIT_EVENT host, so both controls stayed hidden
 * (a control nobody handles is not shown), and nothing put `artifact_id` on
 * the /chat body. These tests drive the REAL ChatApp through the REAL
 * startStream with only the network stubbed: an answer carrying an artifact
 * card, the card's edit box, and the JSON actually posted to /api/chat, then
 * the proxy translation that forwards it to the orchestrator.
 */

import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ANSWER,
  box,
  chatBodies,
  lastBody,
  mockHistory,
  renderApp,
  resetHarnessState,
  stubEnv,
  waitForAnswers,
} from './_wireHarness';
import { artifactEditHostReady, requestArtifactEdit } from '@/lib/artifacts';
import { toOrchestratorChatRequest, type ChatRequestBody } from '@/lib/orchestrator';
import type { ArtifactRef } from '@/lib/types';

mockHistory();
const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');

const ID = 'b4c0ffee00000000000000000000beef';
const JOB = 'ffffffffffffffffffffffffffffffff';

const card = (version: number): ArtifactRef => ({
  artifact_id: ID,
  version,
  job_id: JOB,
  title: 'Risk Register',
  kind: 'document',
  status: 'completed',
  files: [
    {
      file_id: `${version}`.padStart(16, 'a'),
      role: 'primary',
      format: 'docx',
      filename: `risk-register-v${version}.docx`,
      title: 'Risk Register',
      mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      size: 2048,
      download_url: `/artifacts/${ID}/v/${version}/f/${`${version}`.padStart(16, 'a')}?disposition=attachment`,
      inline_url: `/artifacts/${ID}/v/${version}/f/${`${version}`.padStart(16, 'a')}?disposition=inline`,
      preview_url: '',
    },
  ],
  preview_kind: 'none',
  preview_pages: 0,
  preview_url: '',
  thumbnail_url: '',
  warnings: [],
  created_at: '2026-09-15T10:00:00Z',
  operation: version === 1 ? 'create' : 'edit',
  status_url: `/artifacts/jobs/${JOB}`,
});

/** token, a meta carrying one artifact card, done. */
function sseWithCard(version: number): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(c) {
      const enc = new TextEncoder();
      c.enqueue(enc.encode(`event: token\ndata: ${JSON.stringify({ text: ANSWER })}\n\n`));
      c.enqueue(enc.encode(`event: meta\ndata: ${JSON.stringify({ route: 'artifact', artifacts: [card(version)] })}\n\n`));
      c.enqueue(enc.encode('event: done\ndata: {}\n\n'));
      c.close();
    },
  });
}

beforeEach(() => {
  resetHarnessState();
  stubEnv();
  // Every answer carries the artifact card; everything else keeps the harness.
  const base = globalThis.fetch;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/chat')) {
        chatBodies.push(JSON.parse(String(init?.body)) as ChatRequestBody);
        return { ok: true, status: 200, body: sseWithCard(chatBodies.length) };
      }
      return base(url, init);
    }),
  );
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

async function firstTurn() {
  await act(async () => {
    fireEvent.change(box(), { target: { value: 'make a risk register document' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
  await waitFor(() => expect(chatBodies.length).toBe(1), { timeout: 4000 });
  await waitForAnswers(1);
}

describe('ChatApp is the artifact edit host', () => {
  it('registers as the host while mounted', () => {
    expect(artifactEditHostReady()).toBe(true);
  });

  it('shows "Edit with a prompt" on a card and sends the edit as a chat turn with artifact_id', async () => {
    await firstTurn();
    expect(lastBody().artifact_id).toBeUndefined();

    const open = await screen.findByTestId('artifact-edit-open', {}, { timeout: 4000 });
    await act(async () => {
      fireEvent.click(open);
    });
    const input = screen.getByTestId('artifact-edit-input');
    await act(async () => {
      fireEvent.change(input, { target: { value: 'make the Risks section paragraphs italic' } });
      fireEvent.click(screen.getByTestId('artifact-edit-send'));
    });
    await waitFor(() => expect(chatBodies.length).toBe(2), { timeout: 4000 });
    const body = lastBody();
    expect(body.artifact_id).toBe(ID);
    expect(body.current_text).toBe('make the Risks section paragraphs italic');
    // The edit is a normal turn: the words are the last user message.
    expect(body.messages?.[body.messages.length - 1]).toEqual({
      role: 'user',
      content: 'make the Risks section paragraphs italic',
    });
    // …and the proxy forwards the id to the orchestrator.
    expect(toOrchestratorChatRequest(body)?.artifact_id).toBe(ID);
    await waitForAnswers(2);
  });

  it('answers the ARTIFACT_EDIT_EVENT the panel restore dispatches', async () => {
    await firstTurn();
    await act(async () => {
      expect(requestArtifactEdit(ID, 'Restore version 1')).toBe(true);
    });
    await waitFor(() => expect(chatBodies.length).toBe(2), { timeout: 4000 });
    expect(lastBody().artifact_id).toBe(ID);
    expect(lastBody().current_text).toBe('Restore version 1');
    await waitForAnswers(2);
  });

  it('an ordinary composer send carries no artifact_id', async () => {
    await firstTurn();
    await act(async () => {
      fireEvent.change(box(), { target: { value: 'thanks' } });
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });
    await waitFor(() => expect(chatBodies.length).toBe(2), { timeout: 4000 });
    expect('artifact_id' in lastBody()).toBe(false);
    await waitForAnswers(2);
  });
});

describe('toOrchestratorChatRequest · artifact_id', () => {
  const base: ChatRequestBody = { messages: [{ role: 'user', content: 'make it landscape' }], session_id: 's' };
  it('forwards a well-formed id and drops anything else', () => {
    expect(toOrchestratorChatRequest({ ...base, artifact_id: ID })?.artifact_id).toBe(ID);
    for (const bad of ['', 'x', ID.toUpperCase(), `${ID}0`, '../etc', 42 as never]) {
      expect('artifact_id' in (toOrchestratorChatRequest({ ...base, artifact_id: bad }) ?? {})).toBe(false);
    }
    expect('artifact_id' in (toOrchestratorChatRequest(base) ?? {})).toBe(false);
  });
});
