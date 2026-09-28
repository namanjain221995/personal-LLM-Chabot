// @vitest-environment jsdom
/**
 * The recorder's edges at the level the composer uses them (the hook, the
 * bar, the logout), fixed on fix/voice-recorder-edges (2026-09-29). Each test
 * holds a promise the person was given; the defects are the verifiers'
 * (agents a7720217aef71e0d9, aa5cae578cec429ac), the security review's items
 * 1 and 10, and the coordinator's capacity decisions.
 *
 * The server is tests/voice-edge-server.ts: several sessions and accounts,
 * one live recording per person, 404 for someone else's session, the idle
 * close, continuation sessions, and GET /api/auth/me. The browser is faked by
 * hand (jsdom has no MediaRecorder or IndexedDB): a slice arrives only when a
 * test emits one, and IndexedDB is fake-indexeddb, fresh for every test.
 */
import 'fake-indexeddb/auto';
import { IDBFactory } from 'fake-indexeddb';
import { act, cleanup, render, renderHook, screen } from '@testing-library/react';
import { Blob as NodeBlob } from 'node:buffer';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { performLogout } from '@/components/AccountMenu';
import { VoiceBar } from '@/components/VoiceBar';
import { useVoiceRecorder } from '@/components/useVoiceRecorder';
import { VOICE_MESSAGES, type SessionProgress } from '@/lib/voice';
import { EdgeServer } from './voice-edge-server';
import { chromeSlices } from './webm-walk';

/** Which bytes the fake microphone produces: the edge server's indexed slices, or real Chrome WebM. */
let bytesFor: (idx: number) => Uint8Array;
const indexed = (idx: number) => {
  const b = new Uint8Array(4000);
  b[0] = idx & 0xff;
  b[1] = (idx >> 8) & 0xff;
  b[2] = (idx >> 16) & 0xff;
  return b;
};
/** Real Chrome slices: the first, then 1-3 over and over (each ends where the next begins). */
const realWebm = (idx: number) => chromeSlices[idx === 0 ? 0 : 1 + ((idx - 1) % 3)]!;

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
    this.ondataavailable?.({ data: new NodeBlob([bytesFor(idx)]) as unknown as Blob });
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

let server: EdgeServer;
let pendingPrompt: Array<(s: MediaStream) => void> = [];
let promptMode: 'grant' | 'hold' = 'grant';
const turn = () => new Promise<void>((r) => setImmediate(r));

/** Real async work (WebCrypto, IndexedDB) lands on real turns; bounded by the wall clock. */
async function until(cond: () => boolean, what: string, budgetMs = 20_000, stepMs = 0): Promise<void> {
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
async function settle(ms = 0, turns = 60) {
  await act(async () => {
    if (ms) await vi.advanceTimersByTimeAsync(ms);
    for (let t = 0; t < turns; t += 1) await turn();
  });
}
function stream(): MediaStream {
  const track = { stop: vi.fn(), addEventListener: vi.fn() };
  return { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
  server = new EdgeServer();
  bytesFor = indexed;
  pendingPrompt = [];
  promptMode = 'grant';
  window.localStorage.clear();
  vi.stubGlobal('indexedDB', new IDBFactory());
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    writable: true,
    value: {
      getUserMedia: vi.fn(() =>
        promptMode === 'grant'
          ? Promise.resolve(stream())
          : new Promise<MediaStream>((resolve) => pendingPrompt.push(resolve)),
      ),
    },
  });
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  vi.stubGlobal('fetch', server.fetch);
  vi.stubGlobal('confirm', vi.fn(() => true));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, 'mediaDevices');
});

function mount() {
  const onTranscript = vi.fn();
  const view = renderHook(() => useVoiceRecorder({ onTranscript }));
  return { view, onTranscript };
}

async function startRecording(ctx = mount()) {
  await act(async () => ctx.view.result.current.start());
  await until(() => ctx.view.result.current.state === 'recording', 'recording started');
  return ctx;
}

/** `count` slices of 5 s; waits for each to reach the server when the network is up. */
async function talk(ctx: ReturnType<typeof mount>, count: number) {
  for (let i = 0; i < count; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      FakeRecorder.last!.emit();
    });
    await settle(0, 30);
  }
}

const liveSession = () => [...server.sessions.values()].find((s) => s.status === 'recording');
/** Slices the server holds for a session. Not parts: an uploader that falls behind sends several slices as one part. */
const partsOn = (id: string) => server.sessions.get(id)!.slices.length;

// ---------------------------------------------------------------------------
// defect 1 + security item 1: X while the server cannot be reached
// ---------------------------------------------------------------------------

describe('defect 1: X while the connection is down', () => {
  it('says the recording is not deleted yet, deletes it when the connection is back, and says so', async () => {
    const ctx = await startRecording();
    await talk(ctx, 24);
    const id = liveSession()!.id;
    await until(() => partsOn(id) === 24, 'two minutes on the server');
    server.online = false;
    await act(async () => ctx.view.result.current.cancel());
    expect(ctx.view.result.current.state).toBe('idle');
    // Before 2026-09-29: no word at all, the outbox dropped, the server still recording.
    await until(
      () => ctx.view.result.current.followUp?.message === VOICE_MESSAGES.discardPending,
      'told it could not be deleted yet',
      20_000,
      500,
    );
    expect(server.sessions.get(id)!.status).toBe('recording');

    server.online = true;
    await act(async () => void window.dispatchEvent(new Event('online')));
    await until(() => server.sessions.get(id)!.status === 'cancelled', 'the recording deleted');
    await until(() => ctx.view.result.current.followUp?.message === VOICE_MESSAGES.discardDone, 'told it is deleted');
    // Never "finished" (and so kept) instead.
    expect(server.requestsFor(id).some((r) => r.path.endsWith('/finish'))).toBe(false);
  });

  it('still deletes it after the tab is closed and opened again', async () => {
    const ctx = await startRecording();
    await talk(ctx, 6);
    const id = liveSession()!.id;
    await until(() => partsOn(id) === 6, 'thirty seconds on the server');
    server.online = false;
    await act(async () => ctx.view.result.current.cancel());
    // Longer than any in-tab retry: the builder's DELETE gave up after four
    // tries over about 15 s and then forgot the recording.
    await settle(30_000);
    ctx.view.unmount(); // the tab closes before the connection comes back
    server.online = true;
    mount(); // and is opened again
    await until(() => server.sessions.get(id)!.status === 'cancelled', 'the discarded recording deleted after reload', 20_000, 500);
  });

  it('pressing the microphone again deletes the discarded recording first, instead of blaming another tab', async () => {
    const ctx = await startRecording();
    await talk(ctx, 3);
    const id = liveSession()!.id;
    await until(() => partsOn(id) === 3, 'on the server');
    server.online = false;
    await act(async () => ctx.view.result.current.cancel());
    await settle(3000);
    server.online = true; // back, but no `online` event yet and the retry timer has not fired
    await act(async () => ctx.view.result.current.start());
    await until(() => ctx.view.result.current.state === 'recording', 'the new recording started');
    expect(server.sessions.get(id)!.status).toBe('cancelled');
    expect(ctx.view.result.current.followUp?.message ?? null).not.toBe(VOICE_MESSAGES.sessionActive);
  });

  it('when the delete still cannot get through, the "already recording" line offers Discard, not End', async () => {
    const ctx = await startRecording();
    await talk(ctx, 3);
    const id = liveSession()!.id;
    await until(() => partsOn(id) === 3, 'on the server');
    server.online = false;
    await act(async () => ctx.view.result.current.cancel());
    await settle(3000);
    server.online = true;
    const real = server.fetch.getMockImplementation()!;
    server.fetch.mockImplementation(async (input, init = {}) =>
      (init.method ?? 'GET') === 'DELETE'
        ? ({ ok: false, status: 503, headers: { get: () => null }, text: async () => '{"detail":"x","reason":"storage_unavailable"}' } as unknown as Response)
        : real(input, init),
    );
    await act(async () => ctx.view.result.current.start());
    await until(() => ctx.view.result.current.followUp?.actionLabel === 'Discard', 'the Discard button');
    expect(ctx.view.result.current.followUp?.message).toBe(VOICE_MESSAGES.sessionActiveDiscarded);
    server.fetch.mockImplementation(real);
    await act(async () => ctx.view.result.current.followUp!.run());
    await until(() => ctx.view.result.current.state === 'recording', 'the new recording started after Discard');
    expect(server.sessions.get(id)!.status).toBe('cancelled');
  });
});

// ---------------------------------------------------------------------------
// defect 6: X while the permission prompt is open
// ---------------------------------------------------------------------------

describe('defect 6: X while the permission prompt is open, then the microphone again', () => {
  it('starts the new recording instead of blaming another tab', async () => {
    promptMode = 'hold';
    const ctx = mount();
    await act(async () => ctx.view.result.current.start());
    await settle(0, 100);
    expect(ctx.view.result.current.state).toBe('requesting');
    await act(async () => ctx.view.result.current.cancel());
    await settle(0, 50);
    await act(async () => ctx.view.result.current.start());
    await settle(0, 100);
    for (const resolve of pendingPrompt.splice(0)) resolve(stream());
    await until(() => ctx.view.result.current.state === 'recording', 'the second press records');
    expect(ctx.view.result.current.followUp?.message ?? null).not.toBe(VOICE_MESSAGES.sessionActive);
    expect([...server.sessions.values()].map((s) => s.status).sort()).toEqual(['cancelled', 'recording']);
  });
});

// ---------------------------------------------------------------------------
// defect 2: a reload while the transcript is being finished
// ---------------------------------------------------------------------------

describe('defect 2: the tab reloads during a long finish', () => {
  it('the reopened tab finds the recording and offers its words back', async () => {
    server.pollsBeforeDone = 1_000_000; // a long backlog
    const ctx = await startRecording();
    await talk(ctx, 4);
    const id = liveSession()!.id;
    await act(async () => ctx.view.result.current.stop());
    await until(() => server.sessions.get(id)!.status === 'finishing', 'finish accepted');
    await settle(2000);
    ctx.view.unmount(); // reload
    server.pollsBeforeDone = 0;
    await settle(40_000); // the old tab's record goes stale
    const again = mount();
    await until(() => again.view.result.current.followUp?.actionLabel === 'Insert it', 'the words offered back', 20_000, 1000);
    await act(async () => again.view.result.current.followUp!.run());
    expect(again.onTranscript).toHaveBeenCalledWith('w0 w1 w2 w3 w4', null, null);
  });

  it('a Retry for gaps not pressed before the reload is offered again after it', async () => {
    server.finalState = (s) =>
      s.polls < 1000
        ? {
            outcome: 'transcribed_with_gaps',
            gaps: [{ start_ms: 5000, end_ms: 10_000, reason: 'engine_unavailable' }],
          }
        : {};
    const ctx = await startRecording();
    await talk(ctx, 3);
    await act(async () => ctx.view.result.current.stop());
    await until(() => ctx.view.result.current.followUp?.actionLabel === 'Retry', 'Retry offered', 20_000, 500);
    ctx.view.unmount();
    await settle(40_000);
    const again = mount();
    await until(() => again.view.result.current.followUp?.actionLabel === 'Retry', 'Retry offered again', 20_000, 1000);
  });
});

// ---------------------------------------------------------------------------
// defect 5 + security item 10: two accounts, one browser
// ---------------------------------------------------------------------------

describe('defect 5: the next person signs in on the same browser', () => {
  it("never touches the previous person's recording, and does not leave their audio readable here", async () => {
    const ctx = await startRecording();
    await talk(ctx, 4);
    const id = liveSession()!.id;
    await until(() => partsOn(id) === 4, 'on the server');
    server.signedIn = null; // A's session expires
    await talk(ctx, 2);
    await until(() => ctx.view.result.current.state === 'error', 'signed out');
    const dbsOfA = (await indexedDB.databases()).map((d) => d.name);
    expect(dbsOfA).toContain('techsara-voice-outbox:u1');
    ctx.view.unmount();
    await settle(40_000);

    server.signedIn = 'u2'; // B signs in
    const seen = server.log.length;
    mount();
    await settle(40_000, 100);
    const byB = server.log.slice(seen).filter((r) => r.path.includes(id));
    // Before 2026-09-29: B's composer adopted A's record, sent it with B's
    // cookie, got the 404 and deleted A's kept seconds.
    expect(byB).toEqual([]);
    const names = (await indexedDB.databases()).map((d) => d.name);
    expect(names).not.toContain('techsara-voice-outbox:u1');
    expect(server.sessions.get(id)!.status).toBe('recording');
  });

  it('asks before a logout deletes audio this browser never uploaded, and keeps the person signed in if they say no', async () => {
    const ctx = await startRecording();
    await talk(ctx, 2);
    const id = liveSession()!.id;
    await until(() => partsOn(id) === 2, 'on the server');
    server.online = false;
    await talk(ctx, 3); // fifteen seconds that never reach the server
    const navigate = vi.fn();
    const ask = vi.fn(() => false);
    vi.stubGlobal('confirm', ask);
    await act(async () => performLogout(server.fetch as unknown as typeof fetch, navigate));
    expect(ask).toHaveBeenCalledWith(expect.stringContaining("hasn't reached the server yet. Signing out deletes it from this browser."));
    expect(navigate).not.toHaveBeenCalled();
    expect(server.log.some((r) => r.path === '/api/auth/logout')).toBe(false);

    ask.mockReturnValue(true);
    server.online = true;
    await act(async () => performLogout(server.fetch as unknown as typeof fetch, navigate));
    expect(navigate).toHaveBeenCalledWith('/login');
    const names = (await indexedDB.databases()).map((d) => d.name);
    expect(names).not.toContain('techsara-voice-outbox:u1');
  });
});

// ---------------------------------------------------------------------------
// defect 7: a tab closed mid-recording
// ---------------------------------------------------------------------------

describe('defect 7: the tab is closed while recording', () => {
  it('tells the server at once (keepalive finish) instead of holding a slot for 600 s, and keeps the outbox', async () => {
    const ctx = await startRecording();
    await talk(ctx, 3);
    const id = liveSession()!.id;
    await until(() => partsOn(id) === 3, 'every part acknowledged');
    await act(async () => void window.dispatchEvent(new Event('pagehide')));
    await settle(0, 30);
    const finish = server.log.find((r) => r.path === `/api/audio/sessions/${id}/finish`);
    expect(finish?.keepalive).toBe(true);
    expect(JSON.parse(finish!.body!)).toMatchObject({ ended_by: 'page_hidden' });
    expect(server.sessions.get(id)!.status).toBe('finishing');
    const records = await new Promise<number>((resolve) => {
      const req = indexedDB.open('techsara-voice-outbox:u1');
      req.onsuccess = () => {
        const tx = req.result.transaction('records', 'readonly');
        const all = tx.objectStore('records').getAll();
        all.onsuccess = () => resolve(all.result.length);
      };
    });
    expect(records).toBe(1);
  });

  it('with a part still on this device it sends no finish, so a reopened tab can still upload into the same recording', async () => {
    const ctx = await startRecording();
    await talk(ctx, 3);
    const id = liveSession()!.id;
    await until(() => partsOn(id) === 3, 'three parts stored');
    server.online = false;
    await talk(ctx, 1);
    server.online = true;
    await act(async () => void window.dispatchEvent(new Event('pagehide')));
    await settle(0, 30);
    // The server continues only a session it closed itself for silence: one
    // closed here would leave that part with nowhere to go.
    expect(server.log.some((r) => r.path.endsWith('/finish'))).toBe(false);
    expect(server.sessions.get(id)!.status).toBe('recording');
  });

  it.each([
    ['within the idle close: into the same session', 60_000, false],
    ['after the idle close: into a session that continues it', 11 * 60_000, true],
  ])('a reopened tab uploads what was left %s, and offers every word back', async (_label, closedFor, continued) => {
    // The first tab talks through a fetch that dies with it: a closed tab
    // sends nothing more, and its timers stop.
    let tabOpen = true;
    vi.stubGlobal('fetch', (input: string, init?: RequestInit) =>
      tabOpen ? server.fetch(input, init) : new Promise<Response>(() => undefined),
    );
    const ctx = await startRecording();
    await talk(ctx, 3);
    const id = liveSession()!.id;
    await until(() => partsOn(id) === 3, 'three parts stored');
    server.online = false;
    await talk(ctx, 2); // ten seconds this device still holds
    await act(async () => void window.dispatchEvent(new Event('pagehide')));
    tabOpen = false;
    vi.clearAllTimers();
    ctx.view.unmount();
    server.online = true;
    await settle(closedFor, 100);
    server.tick();
    vi.stubGlobal('fetch', server.fetch);
    const again = mount();
    await until(() => again.view.result.current.followUp?.actionLabel === 'Insert it', 'offered back', 20_000, 1000);
    const sessions = [...server.sessions.values()];
    expect(sessions).toHaveLength(continued ? 2 : 1);
    if (continued) expect(sessions[1]!.continues).toBe(id);
    await act(async () => again.view.result.current.followUp!.run());
    const words = sessions.flatMap((s) => s.slices.map((i) => `w${i}`)).join(' ');
    // Every slice: the three stored, the two held offline, and the recorder's
    // last one (stopping hands over a final slice, which went to the outbox).
    expect(words).toBe('w0 w1 w2 w3 w4 w5');
    expect(again.onTranscript).toHaveBeenCalledWith(words, null, null);
  });
});

// ---------------------------------------------------------------------------
// defect 8: offline for longer than the server waits, while recording
// ---------------------------------------------------------------------------

describe('defect 8: eleven minutes offline while recording', () => {
  it('keeps recording, continues in a new session when the connection is back, and delivers every word', async () => {
    bytesFor = realWebm;
    const ctx = await startRecording();
    await talk(ctx, 24);
    const id = liveSession()!.id;
    // Real WebM carries no slice index; what the server holds is counted in bytes.
    const twoMinutes = Array.from({ length: 24 }, (_, i) => realWebm(i).byteLength).reduce((a, b) => a + b, 0);
    await until(() => server.sessions.get(id)!.storedBytes === twoMinutes, 'two minutes stored');
    server.online = false;
    await talk(ctx, 132);
    server.online = true;
    await act(async () => void window.dispatchEvent(new Event('online')));
    await until(() => [...server.sessions.values()].some((s) => s.continues === id && s.nextPart > 0), 'continued', 20_000, 1000);
    // Before 2026-09-29 the recorder stopped here and the 132 slices were deleted.
    expect(ctx.view.result.current.state).toBe('recording');
    await talk(ctx, 1);
    await act(async () => ctx.view.result.current.stop());
    await until(() => ctx.onTranscript.mock.calls.length === 1, 'the transcript', 30_000, 1000);
    const [first, continuation] = [...server.sessions.values()];
    const words = [first!, continuation!].flatMap((s) => s.slices.map((i) => `w${i}`)).join(' ');
    expect(ctx.onTranscript.mock.calls[0]![0]).toBe(words);
  }, 60_000);
});

// ---------------------------------------------------------------------------
// the coordinator's capacity decisions
// ---------------------------------------------------------------------------

describe('capacity', () => {
  it('a full server records a short dictation on the ten-minute road, and says it is not stored', async () => {
    server.capacityFull = true;
    const ctx = await startRecording();
    expect(ctx.view.result.current.mode).toBe('legacy');
    expect(ctx.view.result.current.hint).toBe(VOICE_MESSAGES.capacityLegacyHint);
  });

  it('Retry on a long recording the engine never reached asks first, and says how long it is', async () => {
    server.finalState = (s) => ({
      outcome: 'engine_unavailable',
      text: null,
      audio_ms: 23 * 60_000,
      gaps: [{ start_ms: 0, end_ms: s.slices.length * 5000, reason: 'engine_unavailable' }],
    });
    const ctx = await startRecording();
    await talk(ctx, 2);
    await act(async () => ctx.view.result.current.stop());
    await until(() => ctx.view.result.current.followUp?.actionLabel === 'Retry', 'Retry offered', 20_000, 500);
    const ask = vi.fn(() => false);
    vi.stubGlobal('confirm', ask);
    await act(async () => ctx.view.result.current.followUp!.run());
    expect(ask).toHaveBeenCalledWith(
      'Transcribe 23:00 of this recording again? The speech engine works on it for a while, and replies are slower for everyone meanwhile.',
    );
    expect(server.log.some((r) => r.path.endsWith('/retranscribe'))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// the bar
// ---------------------------------------------------------------------------

const progress = (over: Partial<SessionProgress> = {}): SessionProgress => ({
  preview: '',
  tentative: '',
  audioMs: 0,
  backlogMs: 0,
  waitingOn: 'none',
  pendingParts: 0,
  pendingMs: 0,
  offline: false,
  storageTrouble: false,
  lastAckAt: null,
  retentionDays: 0,
  progressive: true,
  ...over,
});

describe('the recording bar', () => {
  const bar = (p: SessionProgress) =>
    render(<VoiceBar state="recording" levels={[]} elapsedMs={1000} maxMs={null} progress={p} onCancel={vi.fn()} onStop={vi.fn()} />);

  it('"Saved to your account" opens the Recordings page, in a new tab so the recording goes on', () => {
    bar(progress());
    const link = screen.getByText('Saved to your account').closest('a');
    expect(link?.getAttribute('href')).toBe('/recordings');
    expect(link?.getAttribute('target')).toBe('_blank');
  });

  it('says plainly when the speech service cannot be reached, even with no backlog yet', () => {
    bar(progress({ waitingOn: 'engine_unavailable' }));
    expect(screen.getByText(VOICE_MESSAGES.engineUnavailableLive)).toBeTruthy();
  });

  it('does not say "Saved to your account" while the server holds nothing, and says how much once it does', () => {
    // Before 2026-09-29 the line was drawn whenever there was progress: after
    // a minute offline with 0 bytes on the server, right under "Connection
    // lost…" (backend verifier afadf78ca3614dad5, item H).
    const view = bar(progress({ offline: true, savedMs: 0, pendingMs: 60_000 }));
    expect(screen.queryByText(/Saved to your account/)).toBeNull();
    view.unmount();
    bar(progress({ savedMs: 65_000, pendingMs: 20_000 }));
    expect(screen.getByText('Saved to your account: 1:05 · 0:20 still on this device')).toBeTruthy();
  });

  it('stops promising an upload "when the connection is back" once the server has stopped waiting', () => {
    bar(progress({ offline: true, offlineLong: true, idleCloseS: 600 }));
    expect(screen.getByText(VOICE_MESSAGES.offlineLong('10 minutes'))).toBeTruthy();
    expect(screen.queryByText(VOICE_MESSAGES.offlineRecording)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// the legacy road (item J): used when the session road is off or refused
// ---------------------------------------------------------------------------

describe('the ten-minute road', () => {
  it('stops before the ten minutes the server refuses at, so the recording is not refused for being 2 ms too long', async () => {
    server.sessionsOff = true;
    const ctx = await startRecording();
    expect(ctx.view.result.current.mode).toBe('legacy');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(594_000);
    });
    expect(ctx.view.result.current.state).toBe('recording');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    await until(() => server.transcribePosts.length === 1, 'posted');
    // Before 2026-09-29 it stopped AT 600,000 ms; in a browser that posted
    // duration_ms=600002 and got 413 "longer than 10 minutes".
    expect(server.transcribePosts[0]!.durationMs).toBeLessThanOrEqual(595_000);
  });

  it('keeps a refused recording, and "Try again" sends the same recording again', async () => {
    server.sessionsOff = true;
    server.transcribeReplies = [{ status: 503, body: { detail: 'The speech engine is busy.' } }];
    const ctx = await startRecording();
    for (let i = 0; i < 3; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
        FakeRecorder.last!.emit();
      });
    }
    await act(async () => ctx.view.result.current.stop());
    await until(() => ctx.view.result.current.followUp !== null, 'the recording offered back');
    const line = ctx.view.result.current.followUp!;
    // Before 2026-09-29: the blob was dropped, and "retryable" meant speaking again.
    expect(line.message).toBe(VOICE_MESSAGES.legacyKept);
    expect([line.actionLabel, line.secondaryLabel]).toEqual(['Try again', 'Save it']);
    await act(async () => line.run());
    await until(() => ctx.onTranscript.mock.calls.length === 1, 'transcribed on the second try');
    expect(ctx.onTranscript).toHaveBeenCalledWith('legacy words', null);
    expect(server.transcribePosts).toHaveLength(2);
    expect(server.transcribePosts[1]!.bytes).toBe(server.transcribePosts[0]!.bytes);
  });
});
