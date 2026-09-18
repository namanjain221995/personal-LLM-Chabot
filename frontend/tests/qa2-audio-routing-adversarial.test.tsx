// @vitest-environment jsdom
/**
 * QA2 round 1 (security lens), B12: what the audio routing must NOT claim.
 *
 * `mediaKindFor` trusts a declared `audio/*` type before the name. Real
 * browsers declare `audio/*` for files that are not recordings: a playlist
 * (`.m3u` is `audio/mpegurl` in /etc/mime.types, `audio/x-mpegurl` on
 * Firefox/Windows; `.pls` is `audio/x-scpls`) and a MIDI score
 * (`audio/midi`, `audio/sp-midi`). A playlist is a TEXT list of paths or
 * URLs. Sent with purpose=video it is stored as `source.m3u` and handed to
 * ffprobe/ffmpeg, whose HLS demuxer opens the entries it lists. At 4810da0
 * these went to the document rail and were read as text.
 */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { mockHistory, resetHarnessState, stubEnv } from './_wireHarness';

mockHistory();

const { Composer } = await import('@/components/Composer');
const { Providers } = await import('@/components/Providers');
const { clearAttachments } = await import('@/lib/attachments');

const PREFS = {
  salesforce: false,
  sfLive: false,
  model: 'smart',
  effort: 'think',
  agent: false,
  webSearch: 'off',
  deepResearch: false,
} as const;

function renderComposer() {
  render(
    <Providers>
      <Composer
        streaming={false}
        prefs={PREFS}
        features={{}}
        onPrefsChange={() => undefined}
        onSend={() => undefined}
        onStop={() => undefined}
      />
    </Providers>,
  );
  return document.querySelector('input[type="file"]') as HTMLInputElement;
}

async function pick(input: HTMLInputElement, file: File) {
  await act(async () => {
    fireEvent.change(input, { target: { files: [file] } });
  });
}

async function chipText(name: string): Promise<string> {
  // A small document is read (FileReader) before its chip appears.
  const remove = await screen.findByLabelText(`Remove attachment ${name}`, undefined, { timeout: 3000 });
  return remove.parentElement!.textContent ?? '';
}

beforeEach(() => {
  stubEnv();
  resetHarnessState();
  clearAttachments();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  clearAttachments();
});

describe('a file the browser types audio/* that is not a recording stays off the video rail', () => {
  it.each([
    ['radio.m3u', 'audio/mpegurl'],
    ['radio2.m3u', 'audio/x-mpegurl'],
    ['stations.pls', 'audio/x-scpls'],
  ])('%s (%s) is not sent for video analysis', async (name, type) => {
    const input = renderComposer();
    await pick(input, new File(['#EXTM3U\n#EXTINF:-1,x\n/home/me/a.mp3\n'], name, { type }));
    const text = await chipText(name);
    expect(text).not.toContain('AUDIO');
    expect(text).not.toContain('VIDEO');
  });
});

describe('what must not change (passes at 4810da0 and at the head)', () => {
  it.each([
    // A TypeScript source file: Windows/Chrome report '' or video/mp2t; with
    // '' it must stay a document (the server list has .ts, the client's must not).
    ['index.ts', '', 'PDF'],
    // An HLS playlist typed by its registered Apple type: text, a document.
    ['stream.m3u8', 'application/vnd.apple.mpegurl', 'PDF'],
    ['notes.md', 'text/markdown', 'PDF'],
    ['rows.json', 'application/json', 'DATASET'],
    ['bundle.zip', 'application/zip', 'PDF'],
  ])('%s (%s) is chipped %s', async (name, type, badge) => {
    const input = renderComposer();
    await pick(input, new File(['x'], name, { type }));
    const text = await chipText(name);
    expect(text).toContain(badge);
    expect(text).not.toContain('AUDIO');
    expect(text).not.toContain('VIDEO');
  });

  it('an image whose name ends in an audio word stays an image', async () => {
    const input = renderComposer();
    await pick(input, new File(['x'], 'cover.mp3.png', { type: 'image/png' }));
    expect(screen.queryByText('AUDIO')).toBeNull();
    expect(screen.queryByText('VIDEO')).toBeNull();
  });

  it('a PDF the OS mislabels audio/* is still a PDF (isPdf runs first)', async () => {
    const input = renderComposer();
    await pick(input, new File(['%PDF-1.4'], 'minutes.pdf', { type: 'audio/mpeg' }));
    expect(await chipText('minutes.pdf')).toContain('PDF');
  });
});

describe('audio labels (head only)', () => {
  it.each([
    ['REC_0001.WAV', ''],
    ['clip.webm', 'audio/webm'],
    ['voice.oga', 'audio/ogg'],
  ])('%s (%s) is chipped AUDIO', async (name, type) => {
    const input = renderComposer();
    await pick(input, new File(['x'], name, { type }));
    expect(await chipText(name)).toContain('AUDIO');
  });
});
