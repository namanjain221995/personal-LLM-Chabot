// @vitest-environment jsdom
/**
 * UPLOAD RELIABILITY (2026-09-10) — the composer half of the lifecycle
 * contract, through the REAL Composer.
 *
 * The 2026-09-09 incidents all begin here. A file was attached and uploaded
 * with nothing on screen to say so; a chip removed mid-upload kept sending;
 * an early upload was bound to whichever conversation happened to be open
 * when it started and was then linked into whichever one was open when Send
 * was pressed; and an attachment was matched to its landed upload by
 * position, so a sibling that failed shifted everything under it.
 *
 * What is pinned here: the identity minted at selection, the bytes recorded
 * with it, the conversation an early upload is BOUND to, what a chip says
 * while its bytes travel, that a refusal is shown honestly with a Retry that
 * RESUMES, that removing a chip aborts the upload and hands the session
 * back — and, at the resolver, that a video turn whose files are on the
 * server is resendable (fe-attach F1).
 *
 * Only the network is stubbed: the composer, its state and its chips are the
 * real ones. `stubEnv` comes from the shared wire harness so the environment
 * (matchMedia, media elements) is set up exactly as every other component
 * test sets it up.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { stubEnv } from './_wireHarness';
import { Composer, type Attachment } from '@/components/Composer';
import { Providers } from '@/components/Providers';
import { DEFAULT_PREFS } from '@/lib/prefs';
import { CHUNK_PART_BYTES, CHUNK_THRESHOLD_BYTES } from '@/lib/uploadDocument';
import { clearAttachments, resendOptionsFor } from '@/lib/attachments';

const SESSION = 's'.repeat(32);
const UPLOAD = 'u'.repeat(32);

let sent: Attachment[][] = [];
let calls: Array<{ url: string; method: string; signal?: AbortSignal }> = [];

/** A file that CLAIMS a size without holding one — the composer only reads
    `size`, and the uploader only slices. */
function sized(name: string, type: string, size: number): File {
  const file = new File(['seed'], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

const bigVideo = (name = 'standup.mp4') =>
  sized(name, 'video/mp4', CHUNK_THRESHOLD_BYTES + 1);
/** An archive rides the document rail by handle — no base64, so it uploads
    early like a video does, but in ONE request. */
const smallArchive = (name = 'notes.zip') => sized(name, 'application/zip', 4096);

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const okJson = (body: unknown) => ({
  ok: true,
  status: 200,
  json: async () => body,
  text: async () => JSON.stringify(body),
});

const failJson = (status: number, body: unknown) => ({
  ok: false,
  status,
  json: async () => body,
  text: async () => JSON.stringify(body),
});

/**
 * The upload rail, scripted. `parts` decides what each part PUT answers, so a
 * test can hold one open or refuse it.
 */
function stubUploadRail(script: {
  part?: (index: number) => unknown;
  single?: () => unknown;
} = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      calls.push({ url: u, method: init?.method ?? 'GET', signal: init?.signal ?? undefined });
      if (u === '/api/upload') {
        return script.single ? script.single() : okJson({ upload_id: UPLOAD, filename: 'notes.zip' });
      }
      if (u.endsWith('/chunked/init')) {
        return okJson({ upload_id: SESSION, accepted_parts: [], bytes_received: 0 });
      }
      if (u.includes('/part/')) {
        const index = Number(u.slice(u.lastIndexOf('/') + 1));
        if (script.part) return script.part(index);
        return okJson({ accepted_parts: [index] });
      }
      if (u.endsWith('/complete')) {
        return okJson({ upload_id: UPLOAD, filename: 'standup.mp4', bytes: 10 });
      }
      // GET (resume) and DELETE (cancel) land here.
      return okJson({ upload_id: SESSION, status: 'uploading', accepted_parts: [], result: null });
    }),
  );
}

function mount(uploadConversationId: string | null = 'conv-A') {
  return render(
    <Providers>
      <Composer
        streaming={false}
        prefs={DEFAULT_PREFS}
        uploadConversationId={uploadConversationId}
        onPrefsChange={() => undefined}
        onSend={(_text, attachments) => {
          sent.push(attachments);
        }}
        onStop={() => undefined}
      />
    </Providers>,
  );
}

/** Counts chips rather than naming them: two files may share a filename,
    which is the whole point of the identity these tests are about. */
async function attach(files: File[]) {
  const chips = () => screen.queryAllByRole('button', { name: /^Remove attachment/ });
  const before = chips().length;
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files } });
  });
  await waitFor(() => expect(chips().length).toBe(before + files.length));
}

async function send(text = 'what is in this?') {
  await act(async () => {
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: text },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
}

/** The chip that owns this filename, whatever else is on screen. */
const chipFor = (name: string) =>
  screen.getByLabelText(`Remove attachment ${name}`).parentElement as HTMLElement;

beforeEach(() => {
  stubEnv();
  sent = [];
  calls = [];
  clearAttachments();
  window.localStorage.clear();
});

afterEach(async () => {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  cleanup();
  vi.unstubAllGlobals();
  clearAttachments();
});

describe('an attachment’s identity is minted at selection', () => {
  it('every chip carries its own attachment_id and its bytes, through submit', async () => {
    stubUploadRail();
    mount();
    // Two files with the SAME name: identity cannot be the filename.
    await attach([smallArchive('report.zip'), smallArchive('report.zip')]);
    // An unrelated re-render between selection and send must not re-mint it.
    await act(async () => {
      fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
        target: { value: 'typing…' },
      });
    });
    await send('compare them');

    const attachments = sent[0];
    expect(attachments).toHaveLength(2);
    for (const att of attachments) {
      expect(typeof att.attachment_id).toBe('string');
      expect(att.attachment_id.length).toBeGreaterThan(0);
      // Never the composer-local clientId, which dies with the send.
      expect(att.attachment_id).not.toBe(att.clientId);
      expect(att.attachment_id.startsWith('att-')).toBe(false);
      expect(att.bytes).toBe(4096);
    }
    expect(attachments[0].attachment_id).not.toBe(attachments[1].attachment_id);
  });

  it('binds an early upload to the conversation it STARTED in (F2)', async () => {
    stubUploadRail();
    mount('conv-A');
    await attach([smallArchive()]);
    await waitFor(() => expect(calls.some((c) => c.url === '/api/upload')).toBe(true));
    await send();

    const att = sent[0][0];
    expect(att.uploadConversationId).toBe('conv-A');
    expect(att.uploadPromise).toBeTruthy();
    // The bytes really did go to that conversation.
    const form = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.find(
      (c) => c[0] === '/api/upload',
    )?.[1] as RequestInit;
    expect((form.body as FormData).get('conversation_id')).toBe('conv-A');
  });

  it('does not upload early at all in a chat that has no id yet', async () => {
    stubUploadRail();
    mount(null);
    await attach([smallArchive()]);
    await send();
    expect(calls).toHaveLength(0);
    expect(sent[0][0].uploadPromise).toBeUndefined();
  });
});

describe('the chip says what the bytes are doing', () => {
  it('counts parts and bytes for a chunked upload, and carries the session id', async () => {
    const held = deferred<unknown>();
    stubUploadRail({
      part: (index) => (index === 1 ? held.promise : okJson({ accepted_parts: [0] })),
    });
    mount();
    await attach([bigVideo()]);

    // Part 0 has landed, part 1 is still in flight: measurable, so measured.
    await waitFor(() => expect(chipFor('standup.mp4').textContent).toMatch(/1 of 2 parts/));
    const line = chipFor('standup.mp4').querySelector('[aria-live="polite"]');
    expect(line).toBeTruthy();
    expect(line!.textContent).toContain('of 2 parts');

    await act(async () => {
      held.resolve(okJson({ accepted_parts: [0, 1] }));
    });
    await waitFor(() => expect(chipFor('standup.mp4').textContent).toContain('Uploaded'));

    await send('what was decided?');
    expect(sent[0][0].sessionId).toBe(SESSION);
  });

  it('says only “Uploading…” for a single request — no invented percentage', async () => {
    const held = deferred<unknown>();
    stubUploadRail({ single: () => held.promise });
    mount();
    await attach([smallArchive()]);
    await waitFor(() => expect(chipFor('notes.zip').textContent).toContain('Uploading…'));
    expect(chipFor('notes.zip').textContent).not.toMatch(/%|parts/);
    await act(async () => {
      held.resolve(okJson({ upload_id: UPLOAD, filename: 'notes.zip' }));
    });
    await waitFor(() => expect(chipFor('notes.zip').textContent).toContain('Uploaded'));
  });

  it('shows a refusal in the server’s own words, with Retry — which RESUMES', async () => {
    let partAttempts = 0;
    stubUploadRail({
      part: (index) => {
        partAttempts += 1;
        return index === 0 && partAttempts === 1
          ? failJson(413, { detail: 'standup.mp4 is larger than 4 GB.' })
          : okJson({ accepted_parts: [index] });
      },
    });
    mount();
    await attach([bigVideo()]);

    await waitFor(() =>
      expect(chipFor('standup.mp4').textContent).toContain('larger than 4 GB'),
    );
    const retry = screen.getByRole('button', { name: 'Retry uploading standup.mp4' });

    calls = [];
    await act(async () => {
      fireEvent.click(retry);
    });
    await waitFor(() => expect(chipFor('standup.mp4').textContent).toContain('Uploaded'));
    // A retry ASKS the server what it already has rather than re-initialising
    // a second session for the same file.
    expect(calls[0].url).toBe(`/api/upload/chunked/conv-A/${SESSION}`);
    expect(calls[0].method).toBe('GET');
    expect(calls.some((c) => c.url.endsWith('/chunked/init'))).toBe(false);
  });
});

describe('removing a chip stops the upload', () => {
  it('aborts the request in flight and hands the session back', async () => {
    const held = deferred<unknown>();
    stubUploadRail({ part: (index) => (index === 0 ? held.promise : okJson({})) });
    mount();
    await attach([bigVideo()]);
    await waitFor(() => expect(calls.some((c) => c.url.includes('/part/0'))).toBe(true));
    const partCall = calls.find((c) => c.url.includes('/part/0'))!;
    expect(partCall.signal?.aborted).toBe(false);

    await act(async () => {
      fireEvent.click(screen.getByLabelText('Remove attachment standup.mp4'));
    });

    expect(partCall.signal?.aborted).toBe(true);
    // The parts already on the server are given back rather than left to a
    // 24-hour TTL — for a video that also stops an analysis nobody wants.
    await waitFor(() =>
      expect(
        calls.some(
          (c) => c.method === 'DELETE' && c.url === `/api/upload/chunked/conv-A/${SESSION}`,
        ),
      ).toBe(true),
    );
    expect(screen.queryByLabelText('Remove attachment standup.mp4')).toBeNull();
    // Nothing finalises an upload the person cancelled.
    await act(async () => {
      held.resolve(okJson({ accepted_parts: [0] }));
    });
    expect(calls.some((c) => c.url.endsWith('/complete'))).toBe(false);
  });
});

/* ================================================== the resolver (F1) ===== */

describe('resendOptionsFor · a video turn is not “missing” (fe-attach F1)', () => {
  it('resends every video by reference when the ids are there', () => {
    const out = resendOptionsFor({
      id: 'u1',
      // ChatApp sets pdfName for a video turn, and so does the history
      // loader on reload. That is what used to condemn the turn.
      pdfName: 'standup.mp4',
      meta: {
        attachments: [
          { name: 'standup.mp4', kind: 'video', id: 'a'.repeat(32) },
          { name: 'demo.mp4', kind: 'video', id: 'b'.repeat(32) },
        ],
      },
    });
    expect(out.missing).toBe(false);
    expect(out.missingNames).toEqual([]);
    expect(out.videoUploads).toEqual([
      { upload_id: 'a'.repeat(32), name: 'standup.mp4' },
      { upload_id: 'b'.repeat(32), name: 'demo.mp4' },
    ]);
  });

  it('names the ones that never landed, and keeps the ones that did', () => {
    const out = resendOptionsFor({
      id: 'u2',
      pdfName: 'big.mp4',
      meta: {
        attachments: [
          { name: 'big.mp4', kind: 'video' },
          { name: 'small.mp4', kind: 'video', id: 'c'.repeat(32) },
        ],
      },
    });
    expect(out.missing).toBe(true);
    expect(out.missingNames).toEqual(['big.mp4']);
    expect(out.videoUploads).toEqual([{ upload_id: 'c'.repeat(32), name: 'small.mp4' }]);
  });

  it('a document turn keeps saying what it always said', () => {
    expect(resendOptionsFor({ id: 'u3', pdfName: 'gone.pdf' }).missingNames).toEqual([
      'gone.pdf',
    ]);
    expect(
      resendOptionsFor({
        id: 'u4',
        pdfName: 'spec.pdf',
        meta: { attachments: [{ name: 'spec.pdf', kind: 'pdf', id: 'd'.repeat(32) }] },
      }),
    ).toMatchObject({ missing: false, missingNames: [] });
  });
});

/** The part size is what makes "1 of 2 parts" true for this file. */
it('the chunked threshold and part size still describe the same file', () => {
  expect(Math.ceil((CHUNK_THRESHOLD_BYTES + 1) / CHUNK_PART_BYTES)).toBe(2);
});
