// @vitest-environment jsdom
/**
 * Voice dictation (2026-09-04), defended at the three levels it can break.
 *
 * THE PROMISE ABOUT HARDWARE. A page that holds the microphone open after the
 * person pressed Stop leaves the browser's capture indicator lit, and there is
 * no transcript worth that. Every path out of recording — stop, cancel, an
 * unmount mid-sentence — is asserted here to have called stop() on every track
 * the browser ever handed us. Those are the tests to fix first if they go red.
 *
 * THE PROMISE ABOUT THE DRAFT. Dictation produces a DRAFT, never a send. The
 * transcript joins whatever is already in the box and stays there for the
 * person to edit; a composer that posted a half-heard sentence to the model
 * would be worse than one with no microphone at all. That is the last test in
 * this file and the most important one in it.
 *
 * THE PROMISE ABOUT STATE. Recording is full of races — a double-clicked Stop,
 * Enter pressed while the meter is running, a cancel during the permission
 * prompt. The transitions in lib/voice.ts are a table precisely so those races
 * are decidable, so the table is asserted whole rather than sampled.
 *
 * jsdom has no MediaRecorder, no getUserMedia and no AudioContext. The fakes
 * below are hand-driven on purpose: nothing fires until a test fires it, so a
 * test that expects a transcript has to have produced the audio for it.
 */
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  LEVEL_BARS,
  canTransition,
  describeCaptureError,
  levelFrom,
  mergeTranscript,
  pickMimeType,
  type VoiceState,
} from '@/lib/voice';
import { VOICE_BAR_LEVELS, VoiceBar, formatElapsed } from '@/components/VoiceBar';
import { useVoiceRecorder } from '@/components/useVoiceRecorder';
import { Composer } from '@/components/Composer';
import { DEFAULT_PREFS } from '@/lib/prefs';

// ---------------------------------------------------------------------------
// The state machine — pure, and tested without a DOM in sight
// ---------------------------------------------------------------------------

const ALL_STATES: VoiceState[] = [
  'idle',
  'requesting',
  'recording',
  'transcribing',
  'error',
];

/**
 * The table, written out again by hand. Copying it from the module would only
 * assert that the module equals itself; written here it is a second opinion,
 * and a change to the real one has to be argued for in a diff to this file.
 */
const LEGAL: Record<VoiceState, VoiceState[]> = {
  idle: ['requesting'],
  requesting: ['recording', 'idle', 'error'],
  recording: ['transcribing', 'idle', 'error'],
  transcribing: ['idle', 'error'],
  error: ['idle', 'requesting'],
};

describe('the voice state machine', () => {
  it('permits exactly the twelve transitions the design names and no others', () => {
    for (const from of ALL_STATES) {
      for (const to of ALL_STATES) {
        expect({ from, to, allowed: canTransition(from, to) }).toEqual({
          from,
          to,
          allowed: LEGAL[from].includes(to),
        });
      }
    }
  });

  it('refuses to record without having asked for the microphone first', () => {
    // The failure this prevents: a button handler that sets "recording"
    // directly, so the bar appears, the timer runs and no audio exists.
    expect(canTransition('idle', 'recording')).toBe(false);
    expect(canTransition('idle', 'requesting')).toBe(true);
  });

  it('refuses to reopen a recording that is already being transcribed', () => {
    // The failure this prevents: a second click on Stop, or a stale closure
    // from an older render, restarting a capture whose upload is in flight.
    expect(canTransition('transcribing', 'recording')).toBe(false);
    expect(canTransition('transcribing', 'idle')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Merging — somebody's half-written sentence is on the line
// ---------------------------------------------------------------------------

describe('merging a transcript into a draft', () => {
  it('keeps what was already typed and joins it with exactly one space', () => {
    expect(mergeTranscript('Please write about', 'the status')).toBe(
      'Please write about the status',
    );
    // The draft already ends in a space: a second one would show up as a gap
    // in the middle of the person's own sentence.
    expect(mergeTranscript('Please write about ', 'the status')).toBe(
      'Please write about the status',
    );
    expect(mergeTranscript('', 'the status')).toBe('the status');
  });

  it('adds no space after an opening bracket or an opening quote', () => {
    expect(mergeTranscript('He said "', 'hello there')).toBe('He said "hello there');
    expect(mergeTranscript('(', 'aside')).toBe('(aside');
    expect(mergeTranscript('a list of [', 'three things')).toBe(
      'a list of [three things',
    );
  });

  it('returns the draft untouched when nothing was heard', () => {
    // The failure this prevents: an empty transcript trimming, blanking or
    // appending whitespace to text the person is still writing.
    expect(mergeTranscript('half a sentence', '')).toBe('half a sentence');
    expect(mergeTranscript('half a sentence', '   \n ')).toBe('half a sentence');
  });
});

// ---------------------------------------------------------------------------
// The meter — a visualiser that lies is worse than no visualiser
// ---------------------------------------------------------------------------

/** Time-domain samples at a given amplitude around the 128 midpoint. */
function samplesAt(amplitude: number): Uint8Array {
  const out = new Uint8Array(256);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = i % 2 === 0 ? 128 + amplitude : 128 - amplitude;
  }
  return out;
}

describe('the level meter', () => {
  it('reads digital silence as nothing at all', () => {
    // The failure this prevents: a muted microphone drawing a lively trace,
    // which reassures the one person who most needs to be told to check.
    expect(levelFrom(new Uint8Array(512).fill(128))).toBe(0);
    expect(levelFrom(new Uint8Array(0))).toBe(0);
  });

  it('reads a full-scale signal as a full bar', () => {
    expect(levelFrom(samplesAt(127))).toBeCloseTo(1, 5);
  });

  it('rises with the signal at every step in between', () => {
    const readings = [2, 4, 8, 16, 32].map((a) => levelFrom(samplesAt(a)));
    for (let i = 1; i < readings.length; i += 1) {
      expect(readings[i]!).toBeGreaterThan(readings[i - 1]!);
    }
    expect(readings[0]!).toBeGreaterThan(0);
    expect(readings[readings.length - 1]!).toBeLessThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// Containers and refusals
// ---------------------------------------------------------------------------

describe('choosing a recording container', () => {
  it('takes Opus when it can have it', () => {
    expect(pickMimeType(() => true)).toBe('audio/webm;codecs=opus');
  });

  it('falls back to the browser default rather than naming a format it cannot record', () => {
    // The failure this prevents: handing MediaRecorder a mimeType it rejects,
    // which throws in the constructor and loses the recording before it starts.
    expect(pickMimeType(() => false)).toBe('');
    expect(pickMimeType((type) => type === 'audio/mp4')).toBe('audio/mp4');
  });
});

describe('explaining a microphone that would not open', () => {
  it('tells a blocked microphone to change a browser setting, and does not offer a retry', () => {
    // Re-prompting after NotAllowedError cannot work — the browser will not
    // ask again — so a "Try again" button here would be a button that lies.
    const err = describeCaptureError(new DOMException('denied', 'NotAllowedError'));
    expect(err.permission).toBe(true);
    expect(err.retryable).toBe(false);
    expect(err.message).toMatch(/browser settings/i);
  });

  it('offers a retry when another application is holding the device', () => {
    const err = describeCaptureError(new DOMException('busy', 'NotReadableError'));
    expect(err.retryable).toBe(true);
    expect(err.permission).toBeUndefined();
    expect(err.message).toMatch(/another application/i);
  });
});

// ---------------------------------------------------------------------------
// The browser, faked by hand
// ---------------------------------------------------------------------------

interface FakeTrack {
  kind: string;
  stop: ReturnType<typeof vi.fn>;
}

/** Every track handed out by getUserMedia, across every stream, this test. */
let issuedTracks: FakeTrack[][] = [];

function newFakeStream(): MediaStream {
  const tracks: FakeTrack[] = [{ kind: 'audio', stop: vi.fn() }];
  issuedTracks.push(tracks);
  return { getTracks: () => tracks } as unknown as MediaStream;
}

function microphoneTracks(): FakeTrack[] {
  return issuedTracks.flat();
}

/**
 * A MediaRecorder that does nothing until a test tells it to. stop() emits one
 * chunk and then fires onstop, in that order and synchronously, which is what
 * the spec promises and what the hook's release path relies on.
 */
class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  static supported: (type: string) => boolean = (type) =>
    type === 'audio/webm;codecs=opus';

  static isTypeSupported(type: string): boolean {
    return FakeMediaRecorder.supported(type);
  }

  state: 'inactive' | 'recording' | 'paused' = 'inactive';
  mimeType: string;
  timeslice: number | undefined;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(_stream: MediaStream, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? '';
    FakeMediaRecorder.instances.push(this);
  }

  start(timeslice?: number): void {
    this.timeslice = timeslice;
    this.state = 'recording';
  }

  stop(): void {
    if (this.state === 'inactive') return;
    // Inactive BEFORE the handlers run: the hook's release() re-enters here
    // from inside onstop, and a fake that stayed "recording" would recurse.
    this.state = 'inactive';
    this.ondataavailable?.({
      data: new Blob(['fake opus payload'], { type: 'audio/webm' }),
    });
    this.onstop?.();
  }
}

class FakeAudioContext {
  state = 'running';
  createAnalyser() {
    return {
      fftSize: 2048,
      smoothingTimeConstant: 0,
      getByteTimeDomainData: () => undefined,
    };
  }
  createMediaStreamSource() {
    return { connect: () => undefined };
  }
  close() {
    this.state = 'closed';
    return Promise.resolve();
  }
}

function jsonResponse(status: number, payload: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as unknown as Response;
}

let getUserMedia: ReturnType<typeof vi.fn>;
let fetchMock: ReturnType<typeof vi.fn>;
/** The clock the hook measures recordings against, advanced by hand. */
let clock = 0;

/** Let every pending promise settle without pretending timers are fake. */
const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  clock = 1_757_000_000_000;
  vi.spyOn(Date, 'now').mockImplementation(() => clock);

  issuedTracks = [];
  FakeMediaRecorder.instances = [];
  FakeMediaRecorder.supported = (type) => type === 'audio/webm;codecs=opus';

  getUserMedia = vi.fn(async () => newFakeStream());
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    writable: true,
    value: { getUserMedia },
  });

  vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
  vi.stubGlobal('AudioContext', FakeAudioContext);
  // The meter loop is left un-driven: a rAF that actually ran would spin for
  // the whole test and none of these assertions are about the waveform.
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());

  fetchMock = vi.fn(async () =>
    jsonResponse(200, {
      text: 'the status',
      language: 'en',
      duration_ms: 1200,
      processing_ms: 310,
    }),
  );
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, 'mediaDevices');
});

// ---------------------------------------------------------------------------
// The hook
// ---------------------------------------------------------------------------

function mountRecorder() {
  const onTranscript = vi.fn();
  const view = renderHook(() => useVoiceRecorder({ onTranscript }));
  return { ...view, onTranscript };
}

async function record(view: ReturnType<typeof mountRecorder>, forMs = 1200) {
  await act(async () => {
    view.result.current.start();
    await settle();
  });
  clock += forMs;
}

describe('the recorder hook asking for a microphone', () => {
  it('asks for speech-tuned mono audio, not whatever the device defaults to', () => {
    // Echo cancellation, noise suppression and gain control measurably improve
    // an office recording; stereo only doubles the upload for a mono model.
    const view = mountRecorder();
    act(() => {
      view.result.current.start();
    });
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
  });

  it('claims no support until it has mounted, so the server and the client agree', () => {
    // Next renders the composer on the SERVER, where MediaRecorder does not
    // exist. Reading `voiceSupported()` in the render body therefore made the
    // server emit a composer with no microphone and the browser hydrate one
    // with it — a mismatch React repairs by throwing the subtree away. The
    // first render has to say what the server said; the effect then corrects
    // it.
    const seen: boolean[] = [];
    function Probe() {
      seen.push(useVoiceRecorder({ onTranscript: vi.fn() }).supported);
      return null;
    }
    render(<Probe />);
    expect(seen[0]).toBe(false);
    expect(seen[seen.length - 1]).toBe(true);
  });

  it('lands a denied microphone in the error state and does not ask a second time', async () => {
    // The failure this prevents: a retry loop against a permission the browser
    // has already refused, which re-prompts nobody and just spins.
    getUserMedia.mockRejectedValueOnce(new DOMException('nope', 'NotAllowedError'));
    const view = mountRecorder();
    await act(async () => {
      view.result.current.start();
      await settle();
    });
    expect(view.result.current.state).toBe('error');
    expect(view.result.current.error?.permission).toBe(true);
    expect(view.result.current.error?.retryable).toBe(false);
    expect(view.result.current.error?.message).toMatch(/browser settings/i);
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('the recorder hook finishing a recording', () => {
  it('uploads the audio and hands the text back to whoever asked for it', async () => {
    const view = mountRecorder();
    await record(view, 1200);
    expect(view.result.current.state).toBe('recording');

    await act(async () => {
      view.result.current.stop();
      await settle();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe('POST');

    // THE RECORDING IS THE BODY, not a multipart field. A FormData upload is
    // parsed server-side through a spooled temporary file that rolls over onto
    // disk past 1 MB — about ninety seconds of speech — which would put the
    // audio on a disk in a feature sold on it never being written anywhere.
    // Everything beside the bytes rides in the query string.
    const blob = init.body as Blob;
    // A blob of zero bytes would upload, cost a GPU slot and come back empty:
    // the size assertion is what proves the chunks actually reached the body.
    expect(blob.size).toBeGreaterThan(0);
    expect(new URL(url, 'http://x').searchParams.get('duration_ms')).toBe('1200');
    expect(
      (init.headers as Record<string, string>)['content-type'],
    ).toContain('audio/webm');

    expect(view.onTranscript).toHaveBeenCalledWith('the status');
    expect(view.result.current.state).toBe('idle');
  });

  it('does not transcribe twice when Stop is double-clicked', async () => {
    // The failure this prevents: two uploads of one recording, two charges of
    // GPU time, and a transcript merged into the draft twice.
    const view = mountRecorder();
    await record(view, 1200);
    await act(async () => {
      view.result.current.stop();
      view.result.current.stop();
      await settle();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(view.onTranscript).toHaveBeenCalledTimes(1);
  });

  it('throws a cancelled recording away without uploading anything', async () => {
    const view = mountRecorder();
    await record(view, 4000);
    await act(async () => {
      view.result.current.cancel();
      await settle();
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(view.onTranscript).not.toHaveBeenCalled();
    expect(view.result.current.state).toBe('idle');
  });

  it('refuses a mis-click locally instead of sending silence to the engine', async () => {
    // Under MIN_RECORDING_MS there is nothing to transcribe. Sending it anyway
    // reports the engine's empty answer as a failure, which reads as a bug.
    const view = mountRecorder();
    await record(view, 40);
    await act(async () => {
      view.result.current.stop();
      await settle();
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(view.result.current.state).toBe('error');
    expect(view.result.current.error?.message).toMatch(/too short/i);
  });

  it('repeats the server’s own sentence when the account may not dictate', async () => {
    // A 403 is the orchestrator saying the feature is off for this person.
    // Replacing that with a generic "try again" would send them round a loop
    // that cannot end, so the sentence is quoted and the retry withheld.
    fetchMock.mockResolvedValueOnce(
      jsonResponse(403, { detail: 'Voice input is not enabled for your account.' }),
    );
    const view = mountRecorder();
    await record(view, 1500);
    await act(async () => {
      view.result.current.stop();
      await settle();
    });
    expect(view.result.current.state).toBe('error');
    expect(view.result.current.error?.message).toBe(
      'Voice input is not enabled for your account.',
    );
    expect(view.result.current.error?.retryable).toBe(false);
    expect(view.onTranscript).not.toHaveBeenCalled();
  });
});

describe('the microphone is released on every path out of recording', () => {
  it('releases it when the recording is stopped and transcribed', async () => {
    const view = mountRecorder();
    await record(view, 1200);
    expect(microphoneTracks()).toHaveLength(1);
    await act(async () => {
      view.result.current.stop();
      await settle();
    });
    for (const track of microphoneTracks()) {
      expect(track.stop).toHaveBeenCalled();
    }
  });

  it('releases it when the recording is thrown away', async () => {
    const view = mountRecorder();
    await record(view, 1200);
    await act(async () => {
      view.result.current.cancel();
      await settle();
    });
    for (const track of microphoneTracks()) {
      expect(track.stop).toHaveBeenCalled();
    }
  });

  it('releases it when the recorder itself refuses to start', async () => {
    // getUserMedia succeeded, so the capture indicator is already lit, and
    // THEN start() throws — a device that ended between the grant and here, or
    // a container the constructor accepted and the encoder did not. Unguarded
    // this escapes as an unhandled rejection with the microphone still open,
    // on a page frozen at "Waiting for the microphone…" recording nothing.
    const view = mountRecorder();
    const refusing = vi
      .spyOn(FakeMediaRecorder.prototype, 'start')
      .mockImplementation(() => {
        throw new Error('NotSupportedError');
      });
    await act(async () => {
      view.result.current.start();
      await settle();
    });
    refusing.mockRestore();

    expect(microphoneTracks()).toHaveLength(1);
    for (const track of microphoneTracks()) {
      expect(track.stop).toHaveBeenCalled();
    }
    expect(view.result.current.state).toBe('error');
  });

  it('releases it when the page navigates away mid-sentence', async () => {
    // Unmount is the path React gives no second chance at: a track left live
    // here keeps the browser's capture indicator lit on a page that is gone.
    const view = mountRecorder();
    await record(view, 1200);
    await act(async () => {
      view.unmount();
      await settle();
    });
    for (const track of microphoneTracks()) {
      expect(track.stop).toHaveBeenCalled();
    }
    // The upload that was already in flight is allowed to finish, but its text
    // is dropped: calling back into a tree React has torn down would set state
    // on a component that no longer exists.
    expect(view.onTranscript).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// The bar
// ---------------------------------------------------------------------------

function renderBar(
  overrides: Partial<ComponentProps<typeof VoiceBar>> = {},
): ReturnType<typeof render> {
  return render(
    <VoiceBar
      state="recording"
      levels={Array.from({ length: LEVEL_BARS }, (_, i) => i / LEVEL_BARS)}
      elapsedMs={3_000}
      maxMs={10 * 60 * 1000}
      onCancel={vi.fn()}
      onStop={vi.fn()}
      {...overrides}
    />,
  );
}

describe('the recording bar', () => {
  it('counts in minutes and seconds, and never below zero', () => {
    expect(formatElapsed(0)).toBe('0:00');
    expect(formatElapsed(9_400)).toBe('0:09');
    expect(formatElapsed(65_000)).toBe('1:05');
    expect(formatElapsed(750_000)).toBe('12:30');
    expect(formatElapsed(-2_000)).toBe('0:00');
  });

  it('offers a way out and a way to finish', () => {
    const onCancel = vi.fn();
    const onStop = vi.fn();
    renderBar({ onCancel, onStop });
    fireEvent.click(screen.getByLabelText('Cancel recording'));
    fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('says in words what the waveform says in pictures', () => {
    // The trace is aria-hidden and the timer is decoration, so this sentence
    // is the ONLY thing a screen reader has to know recording is happening.
    renderBar();
    const live = screen.getByRole('status');
    expect(live.getAttribute('aria-live')).toBe('polite');
    expect(screen.getByText('Recording, 0:03 elapsed')).toBeTruthy();
  });

  it('renames its escape hatch once the recording is uploading', () => {
    renderBar({ state: 'transcribing' });
    expect(screen.getByLabelText('Cancel transcription')).toBeTruthy();
    expect(screen.getByText('Transcribing your recording')).toBeTruthy();
    // Nothing left to stop — the recorder is already closed.
    expect(
      (screen.getByLabelText('Stop recording and transcribe') as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it('draws one bar per level, so a flat trace means a silent microphone', () => {
    const { container } = renderBar();
    expect(container.querySelectorAll('div[aria-hidden="true"] span')).toHaveLength(
      LEVEL_BARS,
    );
    expect(VOICE_BAR_LEVELS).toBe(LEVEL_BARS);
  });
});

// ---------------------------------------------------------------------------
// The composer
// ---------------------------------------------------------------------------

function mountComposer(
  overrides: Partial<ComponentProps<typeof Composer>> = {},
): { box: HTMLTextAreaElement; onSend: ReturnType<typeof vi.fn> } {
  const onSend = vi.fn();
  render(
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
    box: screen.getByLabelText('Message') as HTMLTextAreaElement,
    onSend,
  };
}

async function startDictating(): Promise<void> {
  await act(async () => {
    fireEvent.click(screen.getByLabelText('Start voice input'));
    await settle();
  });
}

describe('the composer offering dictation', () => {
  it('puts a microphone in the controls row', () => {
    mountComposer();
    expect(screen.getByLabelText('Start voice input')).toBeTruthy();
  });

  it('offers nothing at all when the account may not use voice input', () => {
    // An offered control that always fails is worse than no control: the
    // orchestrator refuses regardless, so the button would only waste a click.
    mountComposer({ features: { voice_input: false } });
    expect(screen.queryByLabelText('Start voice input')).toBeNull();
  });

  it('replaces the controls row while recording rather than sitting beside it', async () => {
    // Recording is a MODE. Leaving the model picker and Send behind invites a
    // click that silently does nothing, or worse, sends the half-spoken turn.
    mountComposer();
    expect(screen.getByLabelText('Send message')).toBeTruthy();
    await startDictating();
    expect(screen.queryByLabelText('Send message')).toBeNull();
    expect(screen.queryByLabelText(/^Effort:/)).toBeNull();
    expect(screen.getByLabelText('Stop recording and transcribe')).toBeTruthy();
  });

  it('merges the transcript into the draft and sends nothing', async () => {
    // THE test of this feature. Dictation produces a draft the person edits,
    // exactly as if they had typed it. A composer that posted what it heard
    // would put a mis-heard sentence in front of the model and in the history.
    const { box, onSend } = mountComposer();
    act(() => {
      fireEvent.change(box, { target: { value: 'Please write about' } });
    });
    await startDictating();
    clock += 2_000;
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
      await settle();
    });
    expect(box.value).toBe('Please write about the status');
    expect(onSend).not.toHaveBeenCalled();
    // Back to the ordinary composer, with the draft waiting to be edited.
    expect(screen.getByLabelText('Send message')).toBeTruthy();
  });

  it('does not send on Enter while the microphone is still open', async () => {
    // The one outcome dictation must never produce: the message posted
    // WITHOUT the words currently being spoken into it.
    const { box, onSend } = mountComposer();
    act(() => {
      fireEvent.change(box, { target: { value: 'half a thought' } });
    });
    await startDictating();
    act(() => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    expect(onSend).not.toHaveBeenCalled();
    expect(box.value).toBe('half a thought');
  });
});

// ---------------------------------------------------------------------------
// The header that made the whole feature impossible
// ---------------------------------------------------------------------------

describe('the browser is actually allowed to open the microphone', () => {
  /**
   * `Permissions-Policy: microphone=()` is an empty ALLOWLIST, not a default.
   * It tells the browser that NO origin may use the microphone, so
   * getUserMedia is refused before the person is ever prompted — and the
   * refusal arrives as NotAllowedError, which is indistinguishable from
   * someone clicking Block. Shipped that way, the composer told every user
   * "allow it in your browser settings" and no setting they could reach would
   * have helped.
   *
   * This reads the real config rather than mocking it, because the failure it
   * guards is precisely a config file drifting away from the feature.
   */
  // `import.meta.url` is an http URL under jsdom, so the config is read from
  // the working directory instead — vitest runs with the frontend root as cwd.
  async function permissionsPolicy(): Promise<string> {
    const { readFileSync } = await import('node:fs');
    const config = readFileSync(`${process.cwd()}/next.config.mjs`, 'utf8');
    return config.match(/value: '([^']*microphone[^']*)'/)?.[1] ?? '';
  }

  it('does not send an empty microphone allowlist', async () => {
    const header = await permissionsPolicy();
    expect(header).toBeTruthy();
    expect(header).not.toContain('microphone=()');
    expect(header).toContain('microphone=(self)');
  });

  it('still denies the camera and geolocation outright', async () => {
    // Dictation needed one permission. It must not have quietly bought three.
    const header = await permissionsPolicy();
    expect(header).toContain('camera=()');
    expect(header).toContain('geolocation=()');
  });
});
