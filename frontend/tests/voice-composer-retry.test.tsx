// @vitest-environment jsdom
/**
 * Defect 4 at the composer (fix/voice-recorder-edges, 2026-09-29): Retry on a
 * transcript with gaps re-reads the stored audio and returns the WHOLE text
 * again. It must land where the first transcript went.
 *
 * Before: `replaceTranscript` looked for the first text verbatim and, when
 * the person had changed even one character of it, appended the whole new
 * transcript after it (the verifier measured 79,889 -> 159,786 characters on
 * a 9,000-word draft). Now the composer tracks where the transcript went and
 * merges the new words into the person's edits, or asks.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Blob as NodeBlob } from 'node:buffer';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Composer } from '@/components/Composer';
import { DEFAULT_PREFS } from '@/lib/prefs';
import { VOICE_MESSAGES } from '@/lib/voice';
import { FakeSessionServer, sliceBytes } from './voice-session-fake';

class FakeRecorder {
  static last: FakeRecorder | null = null;
  static isTypeSupported = (type: string) => type === 'audio/webm;codecs=opus';
  state: 'inactive' | 'recording' = 'inactive';
  mimeType: string;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  next = 0;
  constructor(_stream: MediaStream, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? '';
    FakeRecorder.last = this;
  }
  start() {
    this.state = 'recording';
  }
  emit() {
    const idx = this.next;
    this.next += 1;
    this.ondataavailable?.({ data: new NodeBlob([sliceBytes(idx)]) as unknown as Blob });
  }
  stop() {
    if (this.state === 'inactive') return;
    this.state = 'inactive';
    this.emit();
    this.onstop?.();
  }
}

// Each test drives minutes of fake time and waits, bounded by the wall
// clock, for real async work (WebCrypto, IndexedDB). Under the full suite's
// parallel load that takes far longer than vitest's 5 s default, and a test
// cut off there keeps running into the next one.
vi.setConfig({ testTimeout: 60_000 });

let server: FakeSessionServer;
const turn = () => new Promise<void>((resolve) => setImmediate(resolve));
async function until(cond: () => boolean, what: string, stepMs = 0, budgetMs = 20_000): Promise<void> {
  const deadline = performance.now() + budgetMs;
  while (performance.now() < deadline) {
    if (cond()) return;
    await act(async () => {
      if (stepMs) await vi.advanceTimersByTimeAsync(stepMs);
      for (let t = 0; t < 10; t += 1) await turn();
    });
  }
  if (!cond()) throw new Error(`never happened: ${what}`);
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    writable: true,
    value: {
      getUserMedia: vi.fn(async () => {
        const track = { stop: vi.fn(), addEventListener: vi.fn() };
        return { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
      }),
    },
  });
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
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
  // The engine missed 0:10-0:15 the first time; Retry re-reads the saved audio.
  server = new FakeSessionServer({
    finalState: (words, s) =>
      s.retranscribes === 0
        ? {
            outcome: 'transcribed_with_gaps',
            gaps: [{ start_ms: 10_000, end_ms: 15_000, reason: 'engine_unavailable' }],
            text: words.filter((w) => w !== 'w2').join(' '),
          }
        : {},
  });
  vi.stubGlobal('fetch', server.fetch);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, 'mediaDevices');
});

/** Dictate four slices after "Draft:", and come back with the gap offered for Retry. */
async function dictateWithAGap(): Promise<HTMLTextAreaElement> {
  render(<Composer streaming={false} prefs={DEFAULT_PREFS} onPrefsChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />);
  const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: 'Draft:' } });
  await act(async () => {
    fireEvent.click(screen.getByLabelText('Start voice input'));
  });
  await until(() => FakeRecorder.last?.state === 'recording', 'recording started');
  const rec = FakeRecorder.last!;
  for (let i = 0; i < 4; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      rec.emit();
    });
    await until(() => server.appendedSlices.length >= i + 1, `slice ${i} on the server`);
  }
  await act(async () => {
    fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
  });
  await until(() => box.value !== 'Draft:', 'the transcript in the draft', 500);
  expect(box.value).toBe('Draft: w0 w1 w3 w4');
  return box;
}

describe('defect 4: Retry after the person edited the transcript', () => {
  it('fills the gap where it was and keeps the edit, instead of appending the whole transcript again', async () => {
    const box = await dictateWithAGap();
    // The person fixes one word in the dictated text.
    fireEvent.change(box, { target: { value: 'Draft: w0 W1 w3 w4' } });
    await act(async () => {
      fireEvent.click(screen.getByText('Retry'));
    });
    await until(() => server.retranscribes === 1 && box.value !== 'Draft: w0 W1 w3 w4', 'the Retry landed', 500);
    // Before 2026-09-29: 'Draft: w0 W1 w3 w4 w0 w1 w2 w3 w4'.
    expect(box.value).toBe('Draft: w0 W1 w2 w3 w4');
  });

  it('follows the transcript when the person types before and after it', async () => {
    const box = await dictateWithAGap();
    fireEvent.change(box, { target: { value: 'My notes. Draft: w0 w1 w3 w4 — thanks' } });
    await act(async () => {
      fireEvent.click(screen.getByText('Retry'));
    });
    await until(() => box.value.includes('w2'), 'the Retry landed', 500);
    expect(box.value).toBe('My notes. Draft: w0 w1 w2 w3 w4 — thanks');
  });

  it('asks, and changes nothing, when the edit collides with what the Retry changes', async () => {
    const box = await dictateWithAGap();
    // The person rewrote the very stretch the missing words belong in.
    fireEvent.change(box, { target: { value: 'Draft: w0 xx w4' } });
    await act(async () => {
      fireEvent.click(screen.getByText('Retry'));
    });
    await until(() => screen.queryByText(VOICE_MESSAGES.retryUnplaced) !== null, 'asked', 500);
    expect(box.value).toBe('Draft: w0 xx w4');
    // Only if the person says so does the new transcript go in, once.
    await act(async () => {
      fireEvent.click(screen.getByText('Insert it'));
    });
    expect(box.value).toBe('Draft: w0 xx w4 w0 w1 w2 w3 w4');
  });
});
