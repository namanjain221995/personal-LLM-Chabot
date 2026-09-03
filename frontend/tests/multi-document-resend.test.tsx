// @vitest-environment jsdom
/**
 * MULTI-FILE — a four-document turn is re-sent with FOUR documents
 * (2026-09-03, runtime-confirmed by the owner).
 *
 * The bug had three parts, all in the frontend and all fixed at one shared
 * layer:
 *
 *   1. `send()` remembered only the FIRST document's bytes;
 *   2. regenerate, edit and retry each did `attachments.find(kind === 'pdf')`
 *      — a `.find` where an array was needed — and posted one inline `pdf`;
 *   3. none of them used `pdf_uploads`, the array the chat body has carried
 *      for several documents since 2026-09-02, even though every document's
 *      durable upload id was sitting in `meta.attachments`.
 *
 * The owner's proof requirement is honoured literally: nothing below is
 * satisfied by four chips being visible or four names surviving history. The
 * assertions are on the REAL /api/chat body, captured off a stubbed fetch,
 * with the real ChatApp driving the real startStream.
 */

import { act, cleanup, screen, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  attach,
  chatBodies,
  editTo,
  lastBody,
  mockHistory,
  pdf,
  png,
  regenerateLast,
  renderApp,
  resetHarnessState,
  send,
  stubEnv,
  uploads,
  userTurns,
  waitForAnswers,
} from './_wireHarness';
import {
  attachmentsForResend,
  clearAttachments,
  rememberAttachments,
  resendOptionsFor,
} from '@/lib/attachments';

mockHistory();
const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');

const FOUR = ['a.pdf', 'b.pdf', 'c.pdf', 'd.pdf'];
const ids = (body: { pdf_uploads?: { upload_id: string; name: string }[] }) =>
  (body.pdf_uploads ?? []).map((u) => u.upload_id);
const names = (body: { pdf_uploads?: { upload_id: string; name: string }[] }) =>
  (body.pdf_uploads ?? []).map((u) => u.name);

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

/* ============================================ PATH A — the initial send */

describe('PATH A · initial send (must keep working)', () => {
  it('MULTI-FILE-01 · four documents upload and the body carries four distinct ids', async () => {
    await attach(FOUR.map((n) => pdf(n)));
    await send('Read all these files.');

    expect(uploads.map((u) => u.name)).toEqual(FOUR);
    const body = lastBody();
    expect(names(body)).toEqual(FOUR);
    expect(new Set(ids(body)).size).toBe(4);
    expect(body.pdf ?? null).toBeNull();
    // …and the message persists every identity, in order.
    expect(userTurns()[0].meta?.attachments?.map((a) => a.id)).toEqual(ids(body));
  });
});

/* ============================================ PATH B — the changed edit */

describe('PATH B · changed edit', () => {
  it('MULTI-FILE-02/03/04/05 · the edit re-sends ALL FOUR, in order, by id', async () => {
    await attach(FOUR.map((n) => pdf(n)));
    await send('Read these files.');
    const original = ids(lastBody());

    await editTo('Summarize every file individually.');
    await waitFor(() => expect(chatBodies.length).toBe(2));

    const body = lastBody();
    expect(body.messages?.filter((m) => m.role === 'user').pop()?.content).toBe(
      'Summarize every file individually.',
    );
    // The mandatory request-body assertion: four distinct identities, the
    // same four, in the same order — not one inline pdf.
    expect(ids(body)).toEqual(original);
    expect(names(body)).toEqual(FOUR);
    expect(body.pdf ?? null).toBeNull();
    // The new version carries the identities forward verbatim.
    expect(userTurns()).toHaveLength(2);
    expect(userTurns()[1].meta?.attachments?.map((a) => a.id)).toEqual(original);
  });
});

/* ================================================ PATH C — regenerate */

describe('PATH C · regenerate', () => {
  it('MULTI-FILE-06 · "Try again" re-sends ALL FOUR by id', async () => {
    await attach(FOUR.map((n) => pdf(n)));
    await send('Read these files.');
    const original = ids(lastBody());

    await regenerateLast();
    await waitFor(() => expect(chatBodies.length).toBe(2));

    expect(ids(lastBody())).toEqual(original);
    expect(lastBody().pdf ?? null).toBeNull();
    // The same user turn — regenerate adds no version.
    expect(userTurns()).toHaveLength(1);
  });

  it('MULTI-FILE-12 · after a reload (no bytes in this tab) the ids still carry it', async () => {
    await attach(FOUR.map((n) => pdf(n)));
    await send('Read these files.');
    const original = ids(lastBody());

    // A reload keeps the message (and its ids) and loses every remembered byte.
    clearAttachments();
    await regenerateLast();
    await waitFor(() => expect(chatBodies.length).toBe(2));

    expect(ids(lastBody())).toEqual(original);
    expect(screen.queryByText(/no longer in memory/i)).toBeNull();
  });
});

/* ============================== the combination the owner called out */

describe('COMBINATION · unchanged edit on a four-document turn', () => {
  it('EDIT-SAME-07/08 · regenerates with all four, adds no user version', async () => {
    await attach(FOUR.map((n) => pdf(n)));
    await send('Summarize every document.');
    const original = ids(lastBody());
    expect(userTurns()).toHaveLength(1);

    await editTo(null); // open, change nothing, Send
    await waitFor(() => expect(chatBodies.length).toBe(2));
    await waitForAnswers(1);

    // No `1 / 2` — the same turn asked again.
    expect(userTurns()).toHaveLength(1);
    expect(screen.queryByText(/2 \/ 2/)).toBeNull();
    // All four, once each, in order.
    expect(ids(lastBody())).toEqual(original);
    expect(lastBody().pdf ?? null).toBeNull();
    // The attachment metadata was not duplicated onto anything.
    expect(userTurns()[0].meta?.attachments).toHaveLength(4);
    // And no toast was raised along the way.
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

/* ================================================ images & datasets */

describe('MULTI-FILE-08/09 · the other attachment kinds are unchanged', () => {
  it('several images still regenerate with every image', async () => {
    await attach([png('one.png'), png('two.png')]);
    await send('Compare these.');
    const sent = lastBody().images ?? [lastBody().image];
    expect(sent.length).toBe(2);

    await regenerateLast();
    await waitFor(() => expect(chatBodies.length).toBe(2));
    const again = lastBody().images ?? [lastBody().image];
    expect(again.length).toBe(2);
    expect(lastBody().pdf_uploads ?? null).toBeNull();
  });

  it('a dataset turn resends no bytes, no references, and says it is a dataset', () => {
    const out = resendOptionsFor({
      id: 'd1',
      pdfName: 'sales.csv',
      meta: { attachments: [{ name: 'sales.csv', kind: 'dataset', id: 'up-9' }] },
    });
    expect(out).toMatchObject({ pdf: null, pdfUploads: null, dataset: true, missing: false });
  });
});

/* ======================================= identity rules, at the resolver */

describe('resendOptionsFor · identity, not filename', () => {
  beforeEach(clearAttachments);

  it('MULTI-FILE-10 · two documents with the SAME name and different ids both travel', () => {
    const out = resendOptionsFor({
      id: 'm1',
      pdfName: 'report.pdf',
      meta: {
        attachments: [
          { name: 'report.pdf', kind: 'pdf', id: 'id-a' },
          { name: 'report.pdf', kind: 'pdf', id: 'id-b' },
        ],
      },
    });
    expect(out.pdfUploads).toEqual([
      { upload_id: 'id-a', name: 'report.pdf' },
      { upload_id: 'id-b', name: 'report.pdf' },
    ]);
  });

  it('MULTI-FILE-11 · a document with an id AND remembered bytes goes once, by id', () => {
    rememberAttachments('m2', [{ kind: 'pdf', name: 'a.pdf', base64: 'QUJD' }]);
    const out = resendOptionsFor({
      id: 'm2',
      pdfName: 'a.pdf',
      meta: { attachments: [{ name: 'a.pdf', kind: 'pdf', id: 'id-a' }] },
    });
    expect(out.pdfUploads).toEqual([{ upload_id: 'id-a', name: 'a.pdf' }]);
    expect(out.pdf).toBeNull();
    expect(out.missing).toBe(false);
  });

  it('an id-less document falls back to its remembered bytes, by POSITION', () => {
    // Second document has no id yet; the second remembered pdf is its bytes.
    rememberAttachments('m3', [
      { kind: 'pdf', name: 'x.pdf', base64: 'AAAA' },
      { kind: 'pdf', name: 'x.pdf', base64: 'BBBB' },
    ]);
    const out = resendOptionsFor({
      id: 'm3',
      pdfName: 'x.pdf',
      meta: {
        attachments: [
          { name: 'x.pdf', kind: 'pdf', id: 'id-1' },
          { name: 'x.pdf', kind: 'pdf' },
        ],
      },
    });
    expect(out.pdfUploads).toEqual([{ upload_id: 'id-1', name: 'x.pdf' }]);
    expect(out.pdf).toBe('BBBB');
    expect(out.missing).toBe(false);
  });

  it('reports missing — never silently drops — when a document has neither', () => {
    const out = resendOptionsFor({
      id: 'm4',
      pdfName: 'gone.pdf',
      meta: { attachments: [{ name: 'gone.pdf', kind: 'pdf' }] },
    });
    expect(out.missing).toBe(true);
  });

  it('attachmentsForResend keeps every image AND every inline document', () => {
    rememberAttachments('m5', [
      { kind: 'image', name: 'i.png', base64: 'IMG' },
      { kind: 'pdf', name: 'p.pdf', base64: 'PDF' },
    ]);
    const out = attachmentsForResend({
      id: 'm5',
      imageDataUrls: ['data:image/png;base64,IMG'],
      pdfName: 'p.pdf',
      meta: { attachments: [{ name: 'p.pdf', kind: 'pdf' }] },
    });
    expect(out.attachments.map((a) => a.kind)).toEqual(['image', 'pdf']);
    expect(out.missing).toBe(false);
  });
});

/* ================================================= the source itself */

describe('MULTI-FILE-07 · no resend path collapses documents with .find', () => {
  it('ChatApp builds every resend through the shared resolver', () => {
    const src = readFileSync(
      join(process.cwd(), 'components/ChatApp.tsx'),
      'utf8',
    );
    // The exact shape of the bug, in any spelling.
    expect(src).not.toMatch(/\.find\(\s*\(?\w+\)?\s*=>\s*\w+\.kind\s*===\s*'pdf'/);
    // Three resend paths, one resolver.
    expect(src.match(/resendOptionsFor\(/g)?.length).toBeGreaterThanOrEqual(3);
  });
});
