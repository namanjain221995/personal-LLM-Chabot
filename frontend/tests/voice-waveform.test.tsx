// @vitest-environment jsdom
/**
 * The live waveform, driven end to end.
 *
 * tests/voice-input.test.tsx already pins the hardware promise (every exit
 * path closes the microphone) and `levelFrom` as arithmetic. What was NOT
 * pinned is the wire between them: that real analyser bytes become real bar
 * heights, that louder samples draw taller bars than quiet ones, and that the
 * meter loop and its AudioContext die with the recording rather than outliving
 * it. Those are the failures a visualiser actually ships with — a trace that
 * animates identically whatever the microphone hears, and a rAF that keeps
 * running after Stop.
 *
 * So the fakes here are DELIBERATELY different from the ones next door: the
 * analyser writes controllable samples into the array it is handed, and
 * requestAnimationFrame is a queue this file steps by hand with a timestamp it
 * chooses. Nothing is random — the same amplitude always draws the same bar.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { memo, useEffect, useRef } from 'react';
import { Composer } from '../components/Composer';
import { DEFAULT_PREFS } from '@/lib/prefs';
import { LEVEL_BARS } from '@/lib/voice';

// ---------------------------------------------------------------------------
// A microphone whose loudness this file decides
// ---------------------------------------------------------------------------

/** Peak deviation from the 128 midpoint the fake analyser reports. */
let amplitude = 0;
let contexts: FakeAudioContext[];
let analysers: FakeAnalyser[];
let sources: FakeSource[];
let issuedTracks: Array<{ stop: () => void; stopped: boolean }>;
let recorders: FakeMediaRecorder[];
let getUserMedia: ReturnType<typeof vi.fn>;
/** The clock the recorder measures against, advanced by hand. */
let clock = 0;

/** Pending rAF callbacks, keyed by the handle handed out. */
let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;
let cancelled: number[];

class FakeAnalyser {
  fftSize = 2048;
  smoothingTimeConstant = 0;
  reads = 0;
  /** Exactly what a real AnalyserNode does: fill the caller's array. */
  getByteTimeDomainData(out: Uint8Array) {
    this.reads += 1;
    for (let i = 0; i < out.length; i += 1) {
      out[i] = i % 2 === 0 ? 128 + amplitude : 128 - amplitude;
    }
  }
}

class FakeSource {
  connected: FakeAnalyser | null = null;
  connect(node: FakeAnalyser) {
    this.connected = node;
  }
}

class FakeAudioContext {
  state = 'running';
  /** The stream this context was built on — proof it is the recorder's. */
  sourceStream: MediaStream | null = null;
  constructor() {
    contexts.push(this);
  }
  createAnalyser() {
    const analyser = new FakeAnalyser();
    analysers.push(analyser);
    return analyser as unknown as AnalyserNode;
  }
  createMediaStreamSource(stream: MediaStream) {
    this.sourceStream = stream;
    const source = new FakeSource();
    sources.push(source);
    return source as unknown as MediaStreamAudioSourceNode;
  }
  close() {
    this.state = 'closed';
    return Promise.resolve();
  }
}

class FakeMediaRecorder {
  static isTypeSupported = (type: string) => type === 'audio/webm;codecs=opus';
  state: 'inactive' | 'recording' = 'inactive';
  mimeType = 'audio/webm;codecs=opus';
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readonly stream: MediaStream;
  constructor(stream: MediaStream) {
    this.stream = stream;
    recorders.push(this);
  }
  start() {
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({
      data: new Blob(['fake opus payload'], { type: 'audio/webm' }),
    });
    this.onstop?.();
  }
}

function newFakeStream(): MediaStream {
  const track = {
    kind: 'audio',
    stopped: false,
    stop() {
      this.stopped = true;
    },
  };
  issuedTracks.push(track);
  return { getTracks: () => [track] } as unknown as MediaStream;
}

/** Run every frame currently queued, at `ts`. Re-registrations wait their turn. */
function stepFrame(ts: number): void {
  const due = [...frames.entries()];
  frames.clear();
  for (const [, cb] of due) cb(ts);
}

/** Drive `count` frames far enough apart that the meter pushes a bar on each. */
async function drive(count: number, from = 1_000): Promise<void> {
  for (let i = 0; i < count; i += 1) {
    clock += 100;
    await act(async () => {
      stepFrame(from + i * 100);
    });
  }
}

const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  clock = 1_757_000_000_000;
  vi.spyOn(Date, 'now').mockImplementation(() => clock);
  amplitude = 0;
  contexts = [];
  analysers = [];
  sources = [];
  issuedTracks = [];
  recorders = [];
  frames = new Map();
  nextFrame = 1;
  cancelled = [];

  getUserMedia = vi.fn(async () => newFakeStream());
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    writable: true,
    value: { getUserMedia },
  });
  vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
  vi.stubGlobal('AudioContext', FakeAudioContext);
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    const handle = nextFrame++;
    frames.set(handle, cb);
    return handle;
  });
  vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
    cancelled.push(handle);
    frames.delete(handle);
  });
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
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        text: 'the status',
        language: 'en',
        duration_ms: 1200,
        processing_ms: 310,
      }),
    })),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, 'mediaDevices');
});

function mountComposer(overrides: Record<string, unknown> = {}) {
  const onSend = vi.fn();
  const view = render(
    <Composer
      streaming={false}
      prefs={DEFAULT_PREFS}
      onPrefsChange={vi.fn()}
      onSend={onSend}
      onStop={vi.fn()}
      {...overrides}
    />,
  );
  return {
    ...view,
    onSend,
    box: screen.getByLabelText('Message') as HTMLTextAreaElement,
  };
}

async function startDictating(): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByLabelText('Start voice input'));
    await settle();
  });
}

/** The waveform's bars, or null when no waveform is on screen. */
function bars(): HTMLSpanElement[] | null {
  const status = screen.queryByRole('status');
  const trace = status?.querySelector('div[aria-hidden="true"]');
  if (!trace) return null;
  return [...trace.querySelectorAll('span')] as HTMLSpanElement[];
}

/** The height, in px, of the newest bar — the one the last reading wrote. */
function newestBar(): number {
  const drawn = bars();
  if (!drawn) throw new Error('no waveform on screen');
  return parseFloat(drawn[drawn.length - 1]!.style.height);
}

// ---------------------------------------------------------------------------
// TEST 1 · idle
// ---------------------------------------------------------------------------

describe('the composer when nothing is being recorded', () => {
  it('draws no waveform at all', () => {
    mountComposer();
    expect(bars()).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    // ...and the ordinary composer is exactly the ordinary composer.
    expect(screen.getByLabelText('Message')).toBeTruthy();
    expect(screen.getByLabelText('Send message')).toBeTruthy();
    expect(screen.getByLabelText(/^Effort:/)).toBeTruthy();
    expect(contexts).toHaveLength(0);
  });

  it('opens no AudioContext and no microphone until asked', () => {
    mountComposer();
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(frames.size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// TEST 2 · start — one microphone, two consumers
// ---------------------------------------------------------------------------

describe('starting to record', () => {
  it('shows the waveform and meters the RECORDER’s own stream', async () => {
    mountComposer();
    await startDictating();

    expect(bars()).toHaveLength(LEVEL_BARS);
    // The whole architectural point: ONE permission prompt, one stream, fed
    // to both the recorder and the analyser. A second getUserMedia here would
    // prompt the user twice and open a second capture session.
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(contexts).toHaveLength(1);
    expect(contexts[0]!.sourceStream).toBe(recorders[0]!.stream);
    expect(sources[0]!.connected).toBe(analysers[0]);
    // A frame is queued, and nothing has been read yet.
    expect(frames.size).toBe(1);
    expect(analysers[0]!.reads).toBe(0);
  });

  it('reads the analyser once the loop runs', async () => {
    mountComposer();
    await startDictating();
    await drive(3);
    expect(analysers[0]!.reads).toBeGreaterThanOrEqual(3);
  });
});

// ---------------------------------------------------------------------------
// TEST 3 · the bars follow the microphone
// ---------------------------------------------------------------------------

describe('what the bars are actually drawn from', () => {
  it('draws silence, a soft voice and a loud one at rising heights', async () => {
    mountComposer();
    await startDictating();

    amplitude = 0;
    await drive(2);
    const silence = newestBar();

    amplitude = 8;
    await drive(2, 2_000);
    const soft = newestBar();

    amplitude = 30;
    await drive(2, 3_000);
    const loud = newestBar();

    // The assertion the whole feature exists to earn.
    expect(silence).toBeLessThan(soft);
    expect(soft).toBeLessThan(loud);
    // Silence sits on the 3px floor: "listening, hearing nothing", not broken.
    expect(silence).toBe(3);
    // ...and the loudest bar stays inside the 34px peak, so a shout cannot
    // resize the composer (the trace box is 40px, the row 52px).
    expect(loud).toBeLessThanOrEqual(34);
  });

  it('is a moving history, not one level copied across every bar', async () => {
    mountComposer();
    await startDictating();
    amplitude = 0;
    await drive(4);
    amplitude = 30;
    await drive(2, 2_000);

    const drawn = bars()!.map((b) => parseFloat(b.style.height));
    // The two loud readings entered at the newest end; the quiet ones are
    // still there, older, at the other. A trace that showed one number would
    // have every bar equal.
    expect(new Set(drawn).size).toBeGreaterThan(1);
    expect(drawn[drawn.length - 1]!).toBeGreaterThan(drawn[0]!);
    expect(drawn[0]!).toBe(3);
  });

  it('holds still when the microphone does — no idle animation', async () => {
    // The lie this prevents: a trace that keeps dancing while a muted
    // microphone sends nothing, reassuring the one person who needs telling.
    mountComposer();
    await startDictating();
    amplitude = 0;
    await drive(6);
    const first = bars()!.map((b) => b.style.height);
    await drive(6, 5_000);
    expect(bars()!.map((b) => b.style.height)).toEqual(first);
    expect(new Set(first)).toEqual(new Set(['3px']));
  });
});

// ---------------------------------------------------------------------------
// TEST 4 · stop
// ---------------------------------------------------------------------------

describe('stopping the recording', () => {
  it('takes the waveform down and shuts the meter off with it', async () => {
    mountComposer();
    await startDictating();
    amplitude = 30;
    await drive(3);
    const readsWhileRecording = analysers[0]!.reads;

    clock += 2_000;
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
      await settle();
    });

    // The trace is gone, the loop is cancelled, the context is closed and the
    // microphone is off. All four, because leaving any one of them is a leak.
    expect(bars()).toBeNull();
    expect(cancelled.length).toBeGreaterThan(0);
    expect(contexts[0]!.state).toBe('closed');
    expect(issuedTracks.every((t) => t.stopped)).toBe(true);

    // Nothing is left queued, and a stray frame that fired anyway would read
    // nothing and update nothing.
    const stray = [...frames.values()];
    await act(async () => {
      for (const cb of stray) cb(9_000);
    });
    expect(analysers[0]!.reads).toBe(readsWhileRecording);
    expect(frames.size).toBe(0);
  });

  it('leaves nothing running after a cancel either', async () => {
    mountComposer();
    await startDictating();
    await drive(2);
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Cancel recording'));
      await settle();
    });
    expect(bars()).toBeNull();
    expect(contexts[0]!.state).toBe('closed');
    expect(issuedTracks.every((t) => t.stopped)).toBe(true);
    expect(frames.size).toBe(0);
  });

  it('starts a SECOND recording clean — one context and one loop, not two', async () => {
    // The bug this prevents: two meter loops racing on the same bar buffer
    // after a start/stop/start, which halves the frame budget and draws a
    // trace that stutters.
    mountComposer();
    await startDictating();
    await drive(2);
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Cancel recording'));
      await settle();
    });
    await startDictating();
    await drive(2, 4_000);

    expect(contexts).toHaveLength(2);
    expect(contexts[0]!.state).toBe('closed');
    expect(contexts[1]!.state).toBe('running');
    // Exactly one frame in flight: the live loop's own re-registration.
    expect(frames.size).toBe(1);
    expect(getUserMedia).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// TEST 5 · unmount
// ---------------------------------------------------------------------------

describe('unmounting mid-sentence', () => {
  it('cancels the frame, closes the context and releases the microphone', async () => {
    const view = mountComposer();
    await startDictating();
    amplitude = 20;
    await drive(2);

    await act(async () => {
      view.unmount();
      await settle();
    });

    expect(cancelled.length).toBeGreaterThan(0);
    expect(contexts[0]!.state).toBe('closed');
    expect(issuedTracks.every((t) => t.stopped)).toBe(true);

    // React logs an error if a dead component is updated; firing whatever was
    // left queued must therefore produce nothing at all.
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const stray = [...frames.values()];
    await act(async () => {
      for (const cb of stray) cb(9_000);
    });
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// TEST 6 · a microphone that never opens
// ---------------------------------------------------------------------------

describe('when the microphone is refused', () => {
  it('draws no waveform and builds no audio graph', async () => {
    getUserMedia.mockRejectedValueOnce(
      new DOMException('nope', 'NotAllowedError'),
    );
    mountComposer();
    await startDictating();

    expect(bars()).toBeNull();
    expect(contexts).toHaveLength(0);
    expect(frames.size).toBe(0);
    // The ordinary composer is back, ready to be typed into.
    expect(screen.getByLabelText('Send message')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// TEST 7 & 8 · the draft, and the transcript that joins it
// ---------------------------------------------------------------------------

describe('the draft underneath the waveform', () => {
  it('keeps what was typed while the trace is on screen, and appends the transcript', async () => {
    const { box, onSend } = mountComposer();
    act(() => {
      fireEvent.change(box, { target: { value: 'Please write about' } });
    });
    await startDictating();
    amplitude = 25;
    await drive(3);

    // Showing the waveform must not cost the draft: the textarea stays
    // mounted behind it with every character intact.
    expect(bars()).toHaveLength(LEVEL_BARS);
    expect((screen.getByLabelText('Message') as HTMLTextAreaElement).value).toBe(
      'Please write about',
    );

    clock += 2_000; // past MIN_RECORDING_MS — a sentence, not a mis-click
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
      await settle();
    });

    expect(box.value).toBe('Please write about the status');
    expect(onSend).not.toHaveBeenCalled();
    expect(bars()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// TEST 9 · the controls the waveform must not disturb
// ---------------------------------------------------------------------------

describe('the controls row', () => {
  it('still reads Fast → Mic → Send at the sizes it was set to', () => {
    mountComposer();
    const fast = screen.getByLabelText(/^Effort:/);
    const mic = screen.getByLabelText('Start voice input');
    const send = screen.getByLabelText('Send message');
    const following = (a: Element, b: Element) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(following(fast, mic)).toBe(true);
    expect(following(mic, send)).toBe(true);
    // The ~10% enlargement (2026-09-07) must not come back off. The exact
    // value is the owner's to tune, so this pins the floor it was raised to,
    // not the pixel it currently sits on.
    expect(Number(mic.querySelector('svg')!.getAttribute('width'))).toBeGreaterThanOrEqual(18);
    expect(Number(fast.querySelector('svg')!.getAttribute('width'))).toBeGreaterThanOrEqual(13);
  });

  it('is replaced by the recording bar, and comes back unchanged', async () => {
    mountComposer();
    await startDictating();
    expect(screen.queryByLabelText('Send message')).toBeNull();
    expect(screen.queryByLabelText(/^Effort:/)).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Cancel recording'));
      await settle();
    });
    const fast = screen.getByLabelText(/^Effort:/);
    const mic = screen.getByLabelText('Start voice input');
    expect(
      Boolean(fast.compareDocumentPosition(mic) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);
    expect(Number(mic.querySelector('svg')!.getAttribute('width'))).toBeGreaterThanOrEqual(18);
  });
});

// ---------------------------------------------------------------------------
// TEST 10 · who re-renders sixteen times a second
// ---------------------------------------------------------------------------

describe('the cost of the meter', () => {
  it('re-renders nothing above the composer while the trace moves', async () => {
    // M-08/M-09 bought a conversation that does NOT re-render while an answer
    // streams. A meter that pushed its level into a parent would spend that
    // budget on a decoration: every bar would re-render the whole thread.
    const parentRenders = { count: 0 };
    const siblingRenders = { count: 0 };

    const Sibling = memo(function Sibling() {
      const seen = useRef(0);
      seen.current += 1;
      siblingRenders.count = seen.current;
      return <div data-testid="sibling" />;
    });

    function Host() {
      parentRenders.count += 1;
      useEffect(() => undefined, []);
      return (
        <>
          <Sibling />
          <Composer
            streaming={false}
            prefs={DEFAULT_PREFS}
            onPrefsChange={vi.fn()}
            onSend={vi.fn()}
            onStop={vi.fn()}
          />
        </>
      );
    }

    render(<Host />);
    await startDictating();
    const parentBefore = parentRenders.count;
    const siblingBefore = siblingRenders.count;

    amplitude = 22;
    await drive(20);

    // Twenty frames of live audio moved the trace...
    expect(analysers[0]!.reads).toBeGreaterThanOrEqual(20);
    expect(newestBar()).toBeGreaterThan(2);
    // ...and cost the tree above it exactly nothing.
    expect(parentRenders.count).toBe(parentBefore);
    expect(siblingRenders.count).toBe(siblingBefore);
  });
});

// ---------------------------------------------------------------------------
// The colour the bars are actually PAINTED in
// ---------------------------------------------------------------------------

/**
 * THE BUG THIS FILE EXISTS FOR, SECOND EDITION (2026-09-07).
 *
 * Every test above passed while the waveform was invisible in the browser.
 * They asserted the DOM, the heights and the audio — all of which were
 * correct — and never asked whether the bar's colour class produces a colour.
 * It did not: `bg-ink/45` puts an opacity modifier on `ink`, which
 * tailwind.config.ts defines as the BARE var() `var(--ts-text)`. Tailwind 3
 * cannot parse a var() as a colour, so it drops the whole utility rather than
 * dimming it, and forty-eight bars rendered with no background-color at all.
 * Measured in Chrome against the served stylesheet: rgba(0, 0, 0, 0).
 *
 * jsdom applies no CSS, so no amount of rendering here would have caught it.
 * What CAN be checked without a browser is the rule the bug broke: never put
 * an opacity modifier on a colour token that is a bare var(). That is what
 * these two tests do, by reading the real config and the real component.
 */

/** `name: 'value'` pairs from the `colors` map in tailwind.config.ts. */
async function tailwindColours(): Promise<Map<string, string>> {
  const { readFileSync } = await import('node:fs');
  const config = readFileSync(`${process.cwd()}/tailwind.config.ts`, 'utf8');
  const body = config.slice(config.indexOf('colors: {'));
  const out = new Map<string, string>();
  for (const line of body.slice(0, body.indexOf('\n      },')).split('\n')) {
    const match = line.match(/^\s*'?([\w-]+)'?:\s*'([^']+)'/);
    if (match) out.set(match[1]!, match[2]!);
  }
  return out;
}

/** The Waveform bar's className — the one carrying the 2px bar width. */
async function barClassName(): Promise<string> {
  const { readFileSync } = await import('node:fs');
  const source = readFileSync(`${process.cwd()}/components/VoiceBar.tsx`, 'utf8');
  const found = [...source.matchAll(/className="([^"]*w-\[2px\][^"]*)"/g)];
  expect(found).toHaveLength(1);
  return found[0]![1]!;
}

describe('the waveform bar colour', () => {
  it('never puts an opacity modifier on a bare var() colour', async () => {
    const colours = await tailwindColours();
    const bare = [...colours.entries()]
      .filter(([, value]) => value.startsWith('var('))
      .map(([name]) => name);
    // Sanity: `ink` IS one of them, so this test is looking at the real trap.
    expect(bare).toContain('ink');
    // ...and `accent` is not, because it was given rgb(... / <alpha-value>).
    expect(bare).not.toContain('accent');

    const className = await barClassName();
    const dropped = bare.filter((name) =>
      new RegExp(`(?:^|\\s)(?:bg|text|border|ring)-${name}\\/\\d+`).test(className),
    );
    expect(dropped).toEqual([]);
    // The bars must still be painted SOMETHING — a class list with no
    // background at all is the same invisible trace by another route.
    expect(className).toMatch(/(?:^|\s)bg-[\w-]+/);
  });

  it('paints them with a token that both themes define', async () => {
    const { readFileSync } = await import('node:fs');
    const className = await barClassName();
    const token = className.match(/(?:^|\s)bg-([\w-]+)(?:\/\d+)?/)![1]!;
    const colours = await tailwindColours();
    const value = colours.get(token);
    expect(value).toBeTruthy();

    const variable = value!.match(/var\((--[\w-]+)\)/)?.[1];
    expect(variable).toBeTruthy();
    const css = readFileSync(`${process.cwd()}/app/globals.css`, 'utf8');
    const declarations = [
      ...css.matchAll(new RegExp(`${variable}:\\s*([^;]+);`, 'g')),
    ].map((m) => m[1]!.trim());
    // Once for the dark palette, once for the light one: a trace painted in a
    // colour only one theme defines is invisible in the other.
    expect(declarations.length).toBeGreaterThanOrEqual(2);
    for (const declaration of declarations) {
      // Either a colour, or the bare channel triple that feeds Tailwind's
      // <alpha-value> slot (`--ts-accent-rgb: 96 165 250`).
      expect(declaration).toMatch(/^(#[0-9a-fA-F]{3,8}|rgb|hsl|\d{1,3} \d{1,3} \d{1,3}$)/);
    }
  });

  it('keeps the trace able to take the width it is given', async () => {
    // Measured in Chrome against the stylesheet the dev server serves:
    // a 620.8px trace box at a 768px composer and 211.8px at a narrow one,
    // holding 285px and 190px of bars respectively — 48 of 48 inside, none
    // clipped, in both. What is assertable without a browser is the class
    // that produces that: the trace grows, the bars do not shrink.
    const { readFileSync } = await import('node:fs');
    const source = readFileSync(`${process.cwd()}/components/VoiceBar.tsx`, 'utf8');
    const trace = source.match(/className="([^"]*justify-center[^"]*)"/)![1]!;
    expect(trace).toContain('flex-1');
    expect(trace).not.toMatch(/(?:^|\s)(hidden|w-0)(?:\s|$)/);
    expect(await barClassName()).toContain('shrink-0');
  });
});

// ---------------------------------------------------------------------------
// How the trace PRESENTS itself (2026-09-08 — centred, larger, TechSara blue)
// ---------------------------------------------------------------------------

describe('the trace presentation', () => {
  it('is centred in its box, not anchored to the Stop button', async () => {
    const { readFileSync } = await import('node:fs');
    const source = readFileSync(`${process.cwd()}/components/VoiceBar.tsx`, 'utf8');
    const trace = source.match(/className="([^"]*overflow-hidden[^"]*)"/)![1]!;
    expect(trace).toContain('justify-center');
    // The old alignment grew the trace leftward out of the timer, which read
    // as hugging Stop. It must not come back.
    expect(trace).not.toContain('justify-end');
    // Bars still grow from their own middle: the row is vertically centred.
    expect(trace).toContain('items-center');
  });

  it('draws a taller peak than the old 26px ceiling, on the same 52px row', async () => {
    const { readFileSync } = await import('node:fs');
    mountComposer();
    await startDictating();
    // Full-scale audio: 127 either side of the 128 midpoint is a level of 1.
    amplitude = 127;
    await drive(2);
    expect(newestBar()).toBe(34);

    amplitude = 0;
    await drive(2, 4_000);
    expect(newestBar()).toBe(3); // the silence floor, still nearly flat

    // The trace box grew (h-8 -> h-10) but the ROW did not: the composer's
    // recording height is fixed at 52px, so nothing above it moves.
    const source = readFileSync(`${process.cwd()}/components/VoiceBar.tsx`, 'utf8');
    expect(source).toMatch(/className="[^"]*\bh-10\b[^"]*overflow-hidden/);
    expect(source).toContain('h-[52px]');
  });

  it('is painted in the TechSara accent blue, by token', async () => {
    const colours = await tailwindColours();
    const className = await barClassName();
    const token = className.match(/(?:^|\s)bg-([\w-]+)(?:\/\d+)?/)![1]!;
    expect(token).toBe('accent');
    // The token that is BLUE and theme-aware, and the one form that survives
    // an opacity modifier if anyone ever adds one.
    expect(colours.get('accent')).toBe('rgb(var(--ts-accent-rgb) / <alpha-value>)');
    // Not the grey it replaced, and not the Send button's fill — which is a
    // dark smudge at 3px on the dark composer.
    expect(className).not.toContain('bg-icon');
    expect(className).not.toContain('bg-accent-strong');
  });

  it('keeps rounded ends and thickens the bars only above md', async () => {
    const { readFileSync } = await import('node:fs');
    const className = await barClassName();
    expect(className).toContain('rounded-full');
    // 3px bars with 3px gaps are 285px — fine in a 620px trace box, too wide
    // for the 211px one a phone gives it, so the phone keeps 2px/2px. Both
    // fit, which is what lets `justify-center` centre without clipping the
    // NEWEST bars.
    expect(className).toContain('w-[2px]');
    expect(className).toContain('md:w-[3px]');
    const source = readFileSync(`${process.cwd()}/components/VoiceBar.tsx`, 'utf8');
    const trace = source.match(/className="([^"]*overflow-hidden[^"]*)"/)![1]!;
    expect(trace).toContain('gap-[2px]');
    expect(trace).toContain('md:gap-[3px]');
  });

  it('still keeps the trace decorative and the status spoken', async () => {
    mountComposer();
    await startDictating();
    const status = screen.getByRole('status');
    expect(status.getAttribute('aria-live')).toBe('polite');
    expect(
      status.querySelector('div[aria-hidden="true"]')!.getAttribute('aria-hidden'),
    ).toBe('true');
    expect(screen.getByLabelText('Cancel recording')).toBeTruthy();
    expect(screen.getByLabelText('Stop recording and transcribe')).toBeTruthy();
    expect(screen.getByText(/Recording, \d+:\d\d elapsed/)).toBeTruthy();
  });
});
