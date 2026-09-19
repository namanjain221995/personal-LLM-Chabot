// @vitest-environment jsdom
/**
 * QA round 1 (B12, 2026-09-18) — the seams the builder's audio tests leave open.
 *
 * 1. What must NOT change: for every file that is not audio, the new
 *    `mediaKindFor(...) === 'video'` predicate must say exactly what the
 *    4810da0 predicate said (`type.startsWith('video/') || VIDEO_EXT_RE`).
 * 2. Unicode / right-to-left names, upper case, a Windows octet-stream type.
 * 3. A mixed batch (document + audio), duplicates, the five-document cap,
 *    exactly 4 GB, and the account gate for extension-only and type-only audio.
 * 4. The picker's accept list keeps every 4810da0 token and has no duplicates.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  attach,
  box,
  chatBodies,
  mockHistory,
  pdf,
  renderApp,
  resetHarnessState,
  stubEnv,
  waitForAnswers,
} from './_wireHarness';

mockHistory();

const { ChatApp } = await import('@/components/ChatApp');
const { Composer } = await import('@/components/Composer');
const { Providers } = await import('@/components/Providers');
const { clearAttachments, mediaKindFor } = await import('@/lib/attachments');

const media = (name: string, type: string) =>
  new File(['\x00\x00\x00\x20ftypM4A '], name, { type });

function sized(file: File, bytes: number): File {
  Object.defineProperty(file, 'size', { value: bytes });
  return file;
}

const GB = 1024 * 1024 * 1024;

function recordUploads(): FormData[] {
  const forms: FormData[] = [];
  const harness = globalThis.fetch;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url) === '/api/upload') forms.push(init?.body as FormData);
      return harness(url, init);
    }),
  );
  return forms;
}

const chipOf = (name: string) =>
  screen.getByLabelText(`Remove attachment ${name}`).parentElement!;

async function sendText(text: string) {
  await act(async () => {
    fireEvent.change(box(), { target: { value: text } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
  await waitFor(() => expect(chatBodies.length).toBe(1), { timeout: 4000 });
  await waitForAnswers(1);
}

const PREFS = {
  salesforce: false,
  sfLive: false,
  model: 'smart',
  effort: 'think',
  agent: false,
  webSearch: 'off',
  deepResearch: false,
} as const;

function renderComposer(features: Record<string, boolean> = {}) {
  render(
    <Providers>
      <Composer
        streaming={false}
        prefs={PREFS}
        features={features}
        onPrefsChange={() => undefined}
        onSend={() => undefined}
        onStop={() => undefined}
      />
    </Providers>,
  );
  return document.querySelector('input[type="file"]') as HTMLInputElement;
}

beforeEach(() => {
  stubEnv();
  resetHarnessState();
  clearAttachments();
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(async () => {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  cleanup();
  vi.unstubAllGlobals();
  clearAttachments();
});

// ---------------------------------------------------------------------------
// 1. What must not change.
// ---------------------------------------------------------------------------

/** The composer's video predicate exactly as it stood at 4810da0. */
const OLD_VIDEO_EXT_RE = /\.(mp4|m4v|mov|webm|mkv|avi|mpg|mpeg|3gp|ogv)$/;
const oldIsVideo = (name: string, type: string) =>
  type.startsWith('video/') || OLD_VIDEO_EXT_RE.test(name.toLowerCase());

const NOT_AUDIO: [string, string][] = [
  ['report.pdf', 'application/pdf'],
  ['report.pdf', ''],
  ['brief.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
  ['notes.txt', 'text/plain'],
  ['README.md', ''],
  ['sales.csv', 'text/csv'],
  ['sales.csv', 'application/vnd.ms-excel'],
  ['sales.csv', ''],
  ['orders.tsv', ''],
  ['events.parquet', ''],
  ['book.xlsx', ''],
  ['rows.json', 'application/json'],
  ['rows.jsonl', ''],
  ['rows.ndjson', ''],
  ['bundle.zip', 'application/zip'],
  ['bundle.tar', ''],
  ['bundle.tar.gz', 'application/gzip'],
  ['bundle.tgz', ''],
  ['main.py', 'text/x-python'],
  ['index.ts', ''],
  // Chrome and Windows report a TypeScript file as an MPEG transport stream;
  // 4810da0 already put it on the video rail, and this change must not move it.
  ['index.ts', 'video/mp2t'],
  ['server.log', ''],
  ['shot.png', 'image/png'],
  ['photo.heic', ''],
  ['standup.mp4', 'video/mp4'],
  ['standup.mp4', ''],
  ['MOVIE.MP4', ''],
  ['clip.m4v', ''],
  ['clip.mov', 'video/quicktime'],
  ['screen.webm', ''],
  ['screen.webm', 'video/webm'],
  ['film.mkv', ''],
  ['film.mkv', 'video/x-matroska'],
  ['old.avi', ''],
  ['tape.mpg', ''],
  ['tape.mpeg', ''],
  ['phone.3gp', ''],
  ['theora.ogv', ''],
  ['theora.ogv', 'application/ogg'],
  ['nameless', ''],
  ['', ''],
  ['video.mp4.pdf', ''],
  ['song.mp3.zip', 'application/zip'],
  ['m4a.txt', 'text/plain'],
];

describe('what is NOT audio: the video predicate is unchanged from 4810da0', () => {
  it.each(NOT_AUDIO)('%s (%s)', (name, type) => {
    expect(mediaKindFor(name, type) === 'video').toBe(oldIsVideo(name, type));
    // …and nothing that is not audio is ever called audio.
    expect(mediaKindFor(name, type)).not.toBe('audio');
  });

  it.each([
    ['film.mkv', ''],
    ['old.avi', ''],
    ['tape.mpg', ''],
    ['phone.3gp', ''],
  ])('%s with no type still chips VIDEO end to end', async (name, type) => {
    const forms = recordUploads();
    renderApp(ChatApp, Providers);
    await attach([media(name, type)]);
    expect(chipOf(name).textContent).toContain('VIDEO');
    await sendText('what happens?');
    expect(forms.map((f) => f.get('purpose'))).toEqual(['video']);
  });

  it('a TypeScript file with no type is still a document, never media', async () => {
    const forms = recordUploads();
    renderApp(ChatApp, Providers);
    await attach([new File(['export const a = 1;\n'], 'index.ts', { type: '' })]);
    const chip = chipOf('index.ts');
    expect(chip.textContent).not.toContain('AUDIO');
    expect(chip.textContent).not.toContain('VIDEO');
    await sendText('explain');
    const body = chatBodies[0] as Record<string, unknown>;
    expect(body.video_uploads).toBeUndefined();
    expect(forms.map((f) => f.get('purpose'))).not.toContain('video');
  });

  it('a JSON dataset is still a dataset', async () => {
    renderApp(ChatApp, Providers);
    await attach([new File(['[{"a":1}]'], 'rows.json', { type: 'application/json' })]);
    expect(chipOf('rows.json').textContent).toContain('DATASET');
  });
});

// ---------------------------------------------------------------------------
// 2. Names and types real people and browsers produce.
// ---------------------------------------------------------------------------

describe('audio names and types from the wild', () => {
  it.each([
    ['MEETING.MP3', ''],
    ['اجتماع الفريق.m4a', 'audio/mp4'],
    ['会议记录.opus', ''],
    ['réunion équipe.flac', ''],
    // Windows hands an unmapped extension over as octet-stream, not ''.
    ['call.m4a', 'application/octet-stream'],
    // Some platforms type Ogg as application/ogg.
    ['voice.ogg', 'application/ogg'],
    ['VOICE.WAV', 'AUDIO/WAV'],
  ])('%s (%s) chips AUDIO and uploads with purpose=video, name intact', async (name, type) => {
    const forms = recordUploads();
    renderApp(ChatApp, Providers);
    await attach([media(name, type)]);
    expect(chipOf(name).textContent).toContain('AUDIO');
    await sendText('what was said?');
    expect(forms.map((f) => f.get('purpose'))).toEqual(['video']);
    const body = chatBodies[0] as Record<string, unknown>;
    const refs = body.video_uploads as { upload_id: string; name: string }[];
    expect(refs.map((r) => r.name)).toEqual([name]);
    expect(body.pdf_uploads).toBeUndefined();
  });

  it('mediaKindFor: the extension decides only when the type says nothing', () => {
    expect(mediaKindFor(' memo.m4a ', '')).toBe('audio');
    expect(mediaKindFor('report.pdf.m4a', '')).toBe('audio');
    expect(mediaKindFor('memo.m4a.pdf', '')).toBeNull();
    expect(mediaKindFor('recording.webm', 'audio/webm')).toBe('audio');
    expect(mediaKindFor('recording.webm', 'video/webm')).toBe('video');
    expect(mediaKindFor('recording.webm', '')).toBe('video');
    expect(mediaKindFor('clip.mp4', 'audio/mp4')).toBe('audio');
    expect(mediaKindFor('x', undefined)).toBeNull();
    expect(mediaKindFor('x', null)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 3. Batches, caps and the gate.
// ---------------------------------------------------------------------------

describe('audio in batches, at the caps and behind the gate', () => {
  it('a PDF and an audio file in one batch each take their own rail', async () => {
    const forms = recordUploads();
    renderApp(ChatApp, Providers);
    await attach([pdf('report.pdf'), media('memo.m4a', 'audio/mp4')]);
    expect(chipOf('report.pdf').textContent).toContain('PDF');
    expect(chipOf('memo.m4a').textContent).toContain('AUDIO');
    await sendText('compare them');
    const body = chatBodies[0] as Record<string, unknown>;
    const videos = (body.video_uploads as { name: string }[]).map((r) => r.name);
    expect(videos).toEqual(['memo.m4a']);
    const docs = (body.pdf_uploads as { name: string }[] | undefined)?.map((u) => u.name) ?? [];
    expect(docs.includes('report.pdf') || typeof body.pdf === 'string').toBe(true);
    expect(forms.filter((f) => f.get('purpose') === 'video')).toHaveLength(1);
  });

  it('the same audio file twice behaves exactly as the same video file twice', async () => {
    const input = renderComposer();
    const a = media('memo.m4a', 'audio/mp4');
    const v = media('standup.mp4', 'video/mp4');
    await act(async () => {
      fireEvent.change(input, { target: { files: [a, a, v, v] } });
    });
    const audioChips = screen.getAllByLabelText('Remove attachment memo.m4a');
    const videoChips = screen.getAllByLabelText('Remove attachment standup.mp4');
    expect(audioChips.length).toBe(videoChips.length);
    for (const c of audioChips) expect(c.parentElement!.textContent).toContain('AUDIO');
  });

  it('six audio files stop at the five-document cap, exactly like six videos', async () => {
    // Compared with video rather than with the toast: at 4810da0
    // appendDocument decided `refused` inside a setState updater that React
    // ran after the check, so the toast never showed for ANY streamed kind.
    // The repair round fixed that; audio-attachment.test.tsx ("the
    // five-document cap says so") pins the toast. What audio must do here is
    // what video does.
    const input = renderComposer();
    const audio = Array.from({ length: 6 }, (_, i) => media(`part-${i}.mp3`, 'audio/mpeg'));
    await act(async () => {
      fireEvent.change(input, { target: { files: audio } });
    });
    const audioCount = screen.getAllByLabelText(/Remove attachment part-/).length;
    cleanup();
    const input2 = renderComposer();
    const video = Array.from({ length: 6 }, (_, i) => media(`clip-${i}.mp4`, 'video/mp4'));
    await act(async () => {
      fireEvent.change(input2, { target: { files: video } });
    });
    const videoCount = screen.getAllByLabelText(/Remove attachment clip-/).length;
    expect(audioCount).toBe(5);
    expect(audioCount).toBe(videoCount);
  });

  it('exactly 4 GB of audio is accepted', async () => {
    const input = renderComposer();
    await act(async () => {
      fireEvent.change(input, { target: { files: [sized(media('long.wav', 'audio/wav'), 4 * GB)] } });
    });
    expect(chipOf('long.wav').textContent).toContain('AUDIO');
  });

  it.each([
    ['take.flac', ''],
    ['voice-memo', 'audio/ogg'],
  ])('%s (%s) is refused when the account may not use video understanding', async (name, type) => {
    const input = renderComposer({ video_analysis: false });
    await act(async () => {
      fireEvent.change(input, { target: { files: [media(name, type)] } });
    });
    await waitFor(() => expect(screen.getByText(/Video understanding is turned off/i)).toBeTruthy());
    expect(screen.queryByLabelText(`Remove attachment ${name}`)).toBeNull();
  });

  it('the gate does not spread to documents: a PDF still attaches with video off', async () => {
    const input = renderComposer({ video_analysis: false });
    await act(async () => {
      fireEvent.change(input, { target: { files: [pdf('report.pdf')] } });
    });
    await waitFor(() => expect(chipOf('report.pdf').textContent).toContain('PDF'));
    expect(screen.queryByText(/Video understanding is turned off/i)).toBeNull();
  });

  it('an audio chip removed before Send sends nothing on the video rail', async () => {
    const forms = recordUploads();
    renderApp(ChatApp, Providers);
    await attach([media('memo.m4a', 'audio/mp4')]);
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Remove attachment memo.m4a'));
    });
    await sendText('hello');
    const body = chatBodies[0] as Record<string, unknown>;
    expect(body.video_uploads).toBeUndefined();
    expect(forms).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 4. The picker.
// ---------------------------------------------------------------------------

const ACCEPT_4810DA0 =
  'image/*,application/pdf,.pdf,.docx,.txt,.md,.zip,.tar,.tar.gz,.tgz,.csv,.tsv,.parquet,.xlsx,.json,.jsonl,.ndjson,video/*,.mp4,.m4v,.mov,.webm,.mkv';

describe('the picker accept list', () => {
  it('keeps every 4810da0 token, adds audio, and repeats nothing', () => {
    const input = renderComposer();
    const accept = (input.getAttribute('accept') ?? '').split(',');
    for (const token of ACCEPT_4810DA0.split(',')) expect(accept).toContain(token);
    expect(new Set(accept).size).toBe(accept.length);
    for (const token of accept) expect(token).toMatch(/^(\.[a-z0-9.]+|[a-z]+\/[a-z0-9.+*-]+)$/);
    const added = accept.filter((t) => !ACCEPT_4810DA0.split(',').includes(t));
    expect(added.sort()).toEqual(['.aac', '.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav', 'audio/*'].sort());
  });
});
