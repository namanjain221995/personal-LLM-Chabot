// @vitest-environment jsdom
/**
 * AUDIO (B12, 2026-09-18) — an audio file rides the video rail.
 *
 * Before this, the composer knew ten video extensions and `video/*` and no
 * audio at all, so a voice memo (.m4a) fell into the "upload anything"
 * fallback: chipped as PDF, streamed as purpose=document, and answered as
 * "[Binary file … not readable as text]". The server has always been able to
 * analyse a file with no video stream (the frames stage skips, the transcript
 * and the summary run), so the fix is routing: an audio file is sent with
 * purpose=video exactly like a video, under the video's 4 GB cap and its
 * feature gate, and its chip says AUDIO.
 *
 * The same real-wire harness as tests/video-attachment.test.tsx.
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
  userTurns,
  waitForAnswers,
} from './_wireHarness';

mockHistory();

const { ChatApp } = await import('@/components/ChatApp');
const { Composer } = await import('@/components/Composer');
const { Providers } = await import('@/components/Providers');
const { clearAttachments, mediaKindFor } = await import('@/lib/attachments');

const media = (name: string, type: string) =>
  new File(['\x00\x00\x00\x20ftypM4A '], name, { type });

/** A File whose reported size is `bytes` without allocating it. */
function sized(file: File, bytes: number): File {
  Object.defineProperty(file, 'size', { value: bytes });
  return file;
}

const GB = 1024 * 1024 * 1024;
const MB = 1024 * 1024;

/**
 * Records every /api/upload form, then lets the harness answer it (so a PDF
 * still gets a plain document reply and a dataset a dataset one).
 */
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

function chipOf(name: string): HTMLElement {
  return screen.getByLabelText(`Remove attachment ${name}`).parentElement!;
}

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

/** The Composer alone: no conversation yet, so nothing starts uploading. */
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

// What real browsers report: Chrome/Firefox on Linux and macOS give these
// types; Windows without a registry mapping gives '' and only the name is
// left to go on, so each extension is also tried with no type at all.
const AUDIO_FILES: [string, string][] = [
  ['memo.m4a', 'audio/mp4'],
  ['memo-x.m4a', 'audio/x-m4a'],
  ['call.mp3', 'audio/mpeg'],
  ['clip.wav', 'audio/wav'],
  ['bare.m4a', ''],
  ['bare.mp3', ''],
  ['bare.wav', ''],
  ['note.ogg', ''],
  ['talk.opus', ''],
  ['take.flac', ''],
  ['voice.aac', ''],
  // An audio/* file with no extension at all: the type is all there is.
  ['voice-memo', 'audio/ogg'],
];

describe('an audio attachment', () => {
  it.each(AUDIO_FILES)('%s (%s) is chipped as AUDIO and uploads with purpose=video', async (name, type) => {
    const forms = recordUploads();
    renderApp(ChatApp, Providers);
    await attach([media(name, type)]);
    const chip = chipOf(name);
    expect(chip.textContent).toContain('AUDIO');
    expect(chip.textContent).not.toContain('PDF');
    expect(chip.textContent).not.toContain('VIDEO');

    await sendText('what was said?');
    expect(forms).toHaveLength(1);
    expect(forms[0].get('purpose')).toBe('video');

    const body = chatBodies[0] as Record<string, unknown>;
    const refs = body.video_uploads as { upload_id: string; name: string }[];
    expect(refs).toHaveLength(1);
    expect(refs[0].name).toBe(name);
    expect(refs[0].upload_id).toMatch(/^[0-9a-f]{32}$/);
    expect(body.pdf_uploads).toBeUndefined();
    expect(body.pdf).toBeUndefined();
    // The sent turn remembers it on the video rail, by its durable id.
    const meta = userTurns()[0]?.meta;
    expect(meta?.attachments?.[0]).toMatchObject({ name, kind: 'video' });
    expect(meta?.attachments?.[0]?.id).toBe(refs[0].upload_id);
  });

  it('is offered by the file picker', () => {
    const input = renderComposer();
    const accept = (input.getAttribute('accept') ?? '').split(',');
    for (const token of ['audio/*', '.mp3', '.m4a', '.wav', '.ogg', '.opus', '.flac', '.aac']) {
      expect(accept).toContain(token);
    }
    // …and everything that was offered before still is.
    for (const token of ['image/*', 'application/pdf', '.pdf', '.docx', '.csv', '.xlsx', 'video/*', '.mp4', '.mov', '.webm', '.mkv']) {
      expect(accept).toContain(token);
    }
  });

  it('takes the video cap, not the 512 MB document cap', async () => {
    const input = renderComposer();
    await act(async () => {
      fireEvent.change(input, { target: { files: [sized(media('long-call.m4a', 'audio/mp4'), 600 * MB)] } });
    });
    expect(chipOf('long-call.m4a').textContent).toContain('AUDIO');

    await act(async () => {
      fireEvent.change(input, { target: { files: [sized(media('huge.wav', 'audio/wav'), 4 * GB + 1)] } });
    });
    await waitFor(() => expect(screen.getByText(/huge\.wav is .* the limit is 4 GB/)).toBeTruthy());
    expect(screen.queryByLabelText('Remove attachment huge.wav')).toBeNull();
  });

  it('is refused with the video toast when the account may not use video understanding', async () => {
    const input = renderComposer({ video_analysis: false });
    await act(async () => {
      fireEvent.change(input, { target: { files: [media('memo.m4a', 'audio/mp4')] } });
    });
    await waitFor(() =>
      expect(screen.getByText(/Video understanding is turned off/i)).toBeTruthy(),
    );
    expect(screen.queryByLabelText('Remove attachment memo.m4a')).toBeNull();
  });
});

describe('what is NOT audio routes exactly as before', () => {
  it('a video is still chipped VIDEO', async () => {
    renderApp(ChatApp, Providers);
    await attach([media('standup.mp4', 'video/mp4')]);
    expect(chipOf('standup.mp4').textContent).toContain('VIDEO');
  });

  it('a PDF is a document: chipped PDF, uploaded with purpose=document, never video_uploads', async () => {
    const forms = recordUploads();
    renderApp(ChatApp, Providers);
    await attach([pdf('report.pdf')]);
    expect(chipOf('report.pdf').textContent).toContain('PDF');
    await sendText('summarise it');
    const body = chatBodies[0] as Record<string, unknown>;
    expect(body.video_uploads).toBeUndefined();
    expect(forms.map((f) => f.get('purpose'))).not.toContain('video');
    const onRecord = (body.pdf_uploads as { name: string }[] | undefined)?.map((u) => u.name) ?? [];
    expect(onRecord.includes('report.pdf') || typeof body.pdf === 'string').toBe(true);
  });

  it('a CSV is a dataset: chipped DATASET and sent as one, never video_uploads', async () => {
    const forms = recordUploads();
    renderApp(ChatApp, Providers);
    await attach([new File(['a,b\n1,2\n'], 'sales.csv', { type: 'text/csv' })]);
    expect(chipOf('sales.csv').textContent).toContain('DATASET');
    await sendText('total of b?');
    const body = chatBodies[0] as Record<string, unknown>;
    expect(body.dataset).toBe(true);
    expect(body.video_uploads).toBeUndefined();
    expect(forms.map((f) => f.get('purpose'))).not.toContain('video');
  });
});

describe('mediaKindFor', () => {
  it('names audio by its type first, then its extension', () => {
    expect(mediaKindFor('memo.m4a', 'audio/mp4')).toBe('audio');
    expect(mediaKindFor('recording', 'audio/webm')).toBe('audio');
    expect(mediaKindFor('recording.webm', 'audio/webm;codecs=opus')).toBe('audio');
    expect(mediaKindFor('MEETING.M4A', '')).toBe('audio');
    expect(mediaKindFor('standup.mp4', 'video/mp4')).toBe('video');
    expect(mediaKindFor('screen.webm', '')).toBe('video');
    expect(mediaKindFor('report.pdf', 'application/pdf')).toBeNull();
    expect(mediaKindFor('sales.csv', 'text/csv')).toBeNull();
    expect(mediaKindFor('notes', '')).toBeNull();
  });
});

/* ---- QA round 1 repairs (2026-09-18) ---------------------------------- */

describe('a file typed audio/* that is not a recording stays a document', () => {
  // A playlist is a text list of paths; ffmpeg demuxes a file NAMED .m3u or
  // .m3u8 as HLS and opens what it lists. MIDI is a score with nothing to
  // decode. Before B12 these were documents; the audio/* rule must not
  // claim them.
  it.each([
    ['radio.m3u', 'audio/mpegurl'],
    ['radio.m3u', 'audio/x-mpegurl'],
    ['radio.m3u', ''],
    ['stations.pls', 'audio/x-scpls'],
    ['live.m3u8', 'application/vnd.apple.mpegurl'],
    ['live.m3u8', 'audio/mpegurl'],
    ['tune.mid', 'audio/midi'],
    ['tune.midi', 'audio/x-midi'],
    ['ringtone', 'audio/sp-midi'],
  ])('%s (%s) is not media', (name, type) => {
    expect(mediaKindFor(name, type)).toBeNull();
  });

  it('a playlist picked in the composer is chipped as a document, not AUDIO', async () => {
    const forms = recordUploads();
    renderApp(ChatApp, Providers);
    await attach([new File(['#EXTM3U\n/home/me/a.mp3\n'], 'radio.m3u', { type: 'audio/x-mpegurl' })]);
    const chip = chipOf('radio.m3u');
    expect(chip.textContent).not.toContain('AUDIO');
    expect(chip.textContent).not.toContain('VIDEO');
    await sendText('what is in this list?');
    const body = chatBodies[0] as Record<string, unknown>;
    expect(body.video_uploads).toBeUndefined();
    expect(forms.map((f) => f.get('purpose'))).not.toContain('video');
  });
});

describe('the five-document cap says so', () => {
  // appendDocument used to set `refused` inside the setAttachments updater,
  // which React runs AFTER the `if (refused) toast(...)` check for every
  // file but the first of a batch: the sixth streamed file (video, audio,
  // big PDF, archive) vanished with no message (QA, 2026-09-18).
  it.each([
    ['audio', (i: number) => media(`part-${i}.mp3`, 'audio/mpeg')],
    ['video', (i: number) => media(`clip-${i}.mp4`, 'video/mp4')],
  ])('six %s files in one pick: five chips and the toast', async (_kind, make) => {
    const input = renderComposer();
    const files = Array.from({ length: 6 }, (_, i) => make(i));
    await act(async () => {
      fireEvent.change(input, { target: { files } });
    });
    expect(screen.getAllByLabelText(/Remove attachment (part|clip)-/)).toHaveLength(5);
    expect(screen.queryByLabelText(/Remove attachment (part|clip)-5\./)).toBeNull();
    await waitFor(() => expect(screen.getByText('You can attach up to 5 documents.')).toBeTruthy());
  });

  it('the cap counts across picks, and a removed chip frees its place', async () => {
    const input = renderComposer();
    await act(async () => {
      fireEvent.change(input, {
        target: { files: Array.from({ length: 5 }, (_, i) => media(`a-${i}.m4a`, 'audio/mp4')) },
      });
    });
    expect(screen.queryByText('You can attach up to 5 documents.')).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Remove attachment a-0.m4a'));
    });
    await act(async () => {
      fireEvent.change(input, {
        target: { files: [media('b-0.m4a', 'audio/mp4'), media('b-1.m4a', 'audio/mp4')] },
      });
    });
    expect(chipOf('b-0.m4a').textContent).toContain('AUDIO');
    expect(screen.queryByLabelText('Remove attachment b-1.m4a')).toBeNull();
    await waitFor(() => expect(screen.getByText('You can attach up to 5 documents.')).toBeTruthy());
    expect(screen.getAllByLabelText(/Remove attachment [ab]-/)).toHaveLength(5);
  });
});
