'use client';

/**
 * The microphone, as a hook.
 *
 * Owns four pieces of hardware-adjacent state that all have to be released
 * together — the MediaStream, the MediaRecorder, the AudioContext and the
 * animation frame — and guarantees that every exit path releases all four.
 * That guarantee is the reason this is one hook rather than four effects: a
 * component that forgets one of them leaves the browser's recording indicator
 * lit after the person pressed Stop.
 *
 * TWO ROADS (2026-09-29). Pressing the microphone opens a recording SESSION
 * on the server in parallel with the permission prompt. When the server has
 * sessions, the recorder runs one MediaRecorder for as long as the person
 * talks — an hour, two — and hands every 5 s timeslice to a `VoiceSession`
 * (lib/voice.ts), which uploads it while recording continues, keeps it in an
 * outbox until the server acknowledges it, and brings the transcript back as
 * it forms. There is no ceiling on this road. Only when the server answers
 * `sessions_off` does the recorder fall back to the old road: one blob,
 * posted at Stop, stopped at ten minutes.
 *
 * The old recorder held every chunk in `chunks` until Stop, then posted it
 * once: driving the origin/dev hook in a test harness on 2026-09-29 with 1 s
 * slices of 16,087 bytes (the 128.7 kb/s Chrome 153 was measured recording
 * at), it made no request for ten minutes, stopped itself at 600,000 ms and
 * sent one 9,652,200-byte body; a dropped request was not retried and lost
 * all of it. On the session road the tab holds only what the server has not
 * yet acknowledged.
 *
 * The transitions live in lib/voice.ts and are unit-tested without a DOM.
 * What is here is the part that genuinely needs the browser.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AUDIO_CONSTRAINTS,
  DISCARD_CONFIRM_AFTER_MS,
  LEGACY_MAX_MS,
  LEVEL_BARS,
  MIN_RECORDING_MS,
  OUTBOX_STALE_MS,
  VOICE_MESSAGES,
  VoiceSession,
  canTransition,
  deleteSession,
  describeCaptureError,
  endOtherSession,
  formatElapsed,
  levelFrom,
  newClientKey,
  openOutbox,
  openSession,
  pickMimeType,
  retranscribeSession,
  transcribe,
  voiceSupported,
  type EndedBy,
  type OpenResult,
  type OutboxRecord,
  type OutboxStore,
  type SessionProgress,
  type SessionResult,
  type VoiceError,
  type VoiceOffer,
  type VoiceState,
} from '@/lib/voice';

/** A line beside the composer that the person can act on, or dismiss. */
export interface VoiceFollowUp {
  message: string;
  tone: 'info' | 'error';
  /** The button's label, or null for a line with nothing to press. */
  actionLabel: string | null;
  /** True while the action runs; the button shows a spinner. */
  busy: boolean;
  run: () => void;
  dismiss: () => void;
}

export interface VoiceRecorder {
  state: VoiceState;
  /** 0..1 per bar, oldest first. Always LEVEL_BARS long. */
  levels: number[];
  elapsedMs: number;
  error: VoiceError | null;
  supported: boolean;
  /** Ask for the microphone and begin. Safe to call twice. */
  start: () => void;
  /** Finish and transcribe. */
  stop: () => void;
  /**
   * Throw the recording away. Never transcribes. On the session road this
   * DELETES the stored recording, and asks first when it is over a minute.
   */
  cancel: () => void;
  dismissError: () => void;
  /** Which road this recording took, or null before it has one. */
  mode: 'session' | 'legacy' | null;
  /** The ceiling in force, or null on the session road, which has none. */
  limitMs: number | null;
  /** Upload and transcript progress on the session road; null otherwise. */
  progress: SessionProgress | null;
  /** One sentence about the road itself, e.g. why this one stops at 10:00. */
  hint: string | null;
  /** Something that happened while recording, e.g. the screen went off. */
  warning: string | null;
  followUp: VoiceFollowUp | null;
}

type WakeLockLike = { release: () => Promise<void> };

export function useVoiceRecorder({
  onTranscript,
  maxMs = LEGACY_MAX_MS,
}: {
  /**
   * Called with the text when a recording transcribes successfully.
   *
   * `notice` is one short line to show beside the draft, or null. It never
   * withholds the text: a draft a person can edit beats a warning they cannot
   * act on. `replaces` is set when a saved recording was transcribed again:
   * it is the text the first attempt put into the draft.
   */
  onTranscript: (text: string, notice: string | null, replaces?: string | null) => void;
  /**
   * The LEGACY road's ceiling; the recorder stops itself there rather than
   * being refused later. The session road ignores it: it has no ceiling.
   */
  maxMs?: number;
}): VoiceRecorder {
  const [state, setState] = useState<VoiceState>('idle');
  // Resolved AFTER mount, never during render. `voiceSupported()` asks for
  // MediaRecorder, which does not exist while Next renders this page on the
  // server: reading it in the render body makes the server emit a composer
  // with no microphone and the browser hydrate one with it, which React
  // reports as a mismatch and repairs by throwing the subtree away.
  const [supported, setSupported] = useState(false);
  const [levels, setLevels] = useState<number[]>(() => new Array(LEVEL_BARS).fill(0));
  const [elapsedMs, setElapsedMs] = useState(0);
  const [error, setError] = useState<VoiceError | null>(null);
  const [mode, setMode] = useState<'session' | 'legacy' | null>(null);
  const [progress, setProgress] = useState<SessionProgress | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [followUp, setFollowUp] = useState<VoiceFollowUp | null>(null);

  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const audioContext = useRef<AudioContext | null>(null);
  const frame = useRef<number | null>(null);
  const clockTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  /** LEGACY ROAD ONLY. The session road never collects the recording. */
  const chunks = useRef<Blob[]>([]);
  const session = useRef<VoiceSession | null>(null);
  const modeRef = useRef<'session' | 'legacy' | null>(null);
  const startedAt = useRef(0);
  /** The recording's length once it has stopped; the discard question quotes it. */
  const recordedMs = useRef<number | null>(null);
  const abort = useRef<AbortController | null>(null);
  const outcome = useRef<'stop' | 'cancel'>('stop');
  const endReason = useRef<EndedBy>('person');
  const notices = useRef<string[]>([]);
  const peakLevel = useRef(0);
  const meterRan = useRef(false);
  const lastDataAt = useRef(0);
  const hiddenAt = useRef<number | null>(null);
  const wakeLock = useRef<WakeLockLike | null>(null);
  const levelBuffer = useRef<number[]>(new Array(LEVEL_BARS).fill(0));
  const store = useRef<Promise<OutboxStore> | null>(null);
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;
  const startRef = useRef<() => void>(() => undefined);
  // The state machine's own copy, read inside callbacks that were created in
  // an older render. React state alone would let a stale closure re-enter a
  // transition that has already happened.
  const current = useRef<VoiceState>('idle');
  const alive = useRef(true);
  /** Bumped by every start and cancel, so a stale async step knows it is stale. */
  const generation = useRef(0);

  const getStore = useCallback(() => (store.current ??= openOutbox()), []);

  const move = useCallback((next: VoiceState): boolean => {
    if (!canTransition(current.current, next)) return false;
    current.current = next;
    if (alive.current) setState(next);
    return true;
  }, []);

  const releaseWakeLock = useCallback(() => {
    const lock = wakeLock.current;
    wakeLock.current = null;
    void lock?.release().catch(() => undefined);
  }, []);

  const takeWakeLock = useCallback(async () => {
    // Without it a phone locks its screen mid-dictation and, on iOS, stops
    // capturing audio. Not every browser has it; recording works without.
    const nav = navigator as Navigator & {
      wakeLock?: { request: (type: 'screen') => Promise<WakeLockLike> };
    };
    if (!nav.wakeLock || wakeLock.current) return;
    try {
      wakeLock.current = await nav.wakeLock.request('screen');
    } catch {
      /* denied, or the page is hidden; the visibility handler asks again */
    }
  }, []);

  /**
   * Release EVERYTHING. Idempotent, and called from every path out of
   * recording — including unmount, where React gives no second chance.
   */
  const release = useCallback(() => {
    if (frame.current !== null) {
      cancelAnimationFrame(frame.current);
      frame.current = null;
    }
    if (clockTimer.current !== null) {
      clearInterval(clockTimer.current);
      clockTimer.current = null;
    }
    const rec = recorder.current;
    recorder.current = null;
    if (rec && rec.state !== 'inactive') {
      try {
        rec.stop();
      } catch {
        // Already stopping; the handlers below still run.
      }
    }
    // The tracks are the microphone. Stopping them is what turns the
    // browser's recording indicator off, and it must happen even if the
    // recorder or the context is already broken.
    stream.current?.getTracks().forEach((track) => {
      try {
        track.stop();
      } catch {
        /* a track that is already ended throws on some browsers */
      }
    });
    stream.current = null;
    const context = audioContext.current;
    audioContext.current = null;
    if (context && context.state !== 'closed') {
      void context.close().catch(() => undefined);
    }
    releaseWakeLock();
  }, [releaseWakeLock]);

  useEffect(() => {
    setSupported(voiceSupported());
  }, []);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      abort.current?.abort();
      // A session that is still recording is ended in the background by its
      // onstop (below), after the recorder's last slice: what was recorded
      // is finished and transcribed on the server, and kept there.
      release();
    };
  }, [release]);

  /** The meter loop: one rAF per frame while recording, and not one after. */
  const runMeter = useCallback((analyser: AnalyserNode) => {
    const samples = new Uint8Array(analyser.fftSize);
    let lastPush = 0;
    meterRan.current = true;
    const tick = (now: number) => {
      if (current.current !== 'recording') return;
      analyser.getByteTimeDomainData(samples);
      // ~16 bars a second. Pushing at the display's rate would scroll three
      // seconds of history past in under one, and cost battery for a trace
      // nobody can read at that speed.
      if (now - lastPush >= 60) {
        lastPush = now;
        const level = levelFrom(samples);
        if (level > peakLevel.current) peakLevel.current = level;
        const next = levelBuffer.current.slice(1);
        next.push(level);
        levelBuffer.current = next;
        if (alive.current) setLevels(next);
      }
      if (alive.current) setElapsedMs(Date.now() - startedAt.current);
      frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
  }, []);

  const toFollowUp = useCallback(
    (offer: VoiceOffer, tone: 'info' | 'error'): VoiceFollowUp => {
      const dismiss = () => setFollowUp(null);
      const base = { message: offer.message, tone, actionLabel: offer.label, busy: false, dismiss };
      if (offer.kind === 'insert') {
        return {
          ...base,
          run: () => {
            setFollowUp(null);
            onTranscriptRef.current(offer.text, null);
          },
        };
      }
      if (offer.kind === 'end_other') {
        return {
          ...base,
          run: () => {
            setFollowUp({ ...base, busy: true, run: () => undefined });
            void endOtherSession(offer.sessionId).then((err) => {
              if (!alive.current) return;
              if (err) {
                setFollowUp({ ...base, message: err.message, tone: 'error', actionLabel: null, run: () => undefined });
                return;
              }
              setFollowUp(null);
              startRef.current();
            });
          },
        };
      }
      return {
        ...base,
        run: () => {
          setFollowUp({ ...base, message: VOICE_MESSAGES.retrying, tone: 'info', busy: true, run: () => undefined });
          void retranscribeSession(offer).then((result) => {
            if (!alive.current) return;
            deliverAfterRetry(result, offer.replaces);
          });
        },
      };
    },
    // `deliverAfterRetry` is declared below and only called later.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  function deliverAfterRetry(result: SessionResult, replaces: string | null) {
    if (result.kind === 'withdrawn') {
      setFollowUp(null);
      return;
    }
    if (result.kind === 'text') {
      onTranscriptRef.current(result.text, result.notices.join(' ') || null, replaces);
      setFollowUp(result.offer ? toFollowUp(result.offer, 'info') : null);
      return;
    }
    setFollowUp(
      result.offer
        ? toFollowUp(result.offer, 'error')
        : {
            message: result.error.message,
            tone: 'error',
            actionLabel: null,
            busy: false,
            run: () => undefined,
            dismiss: () => setFollowUp(null),
          },
    );
  }

  /** Show what a finished session came to, from `finishing`. */
  const deliver = useCallback(
    (result: SessionResult) => {
      if (!alive.current) return;
      if (result.kind === 'withdrawn') {
        move('idle');
        return;
      }
      if (result.kind === 'text') {
        move('idle');
        onTranscriptRef.current(result.text, result.notices.join(' ') || null);
        if (result.offer) setFollowUp(toFollowUp(result.offer, 'info'));
        return;
      }
      if (result.offer) {
        // An error the person can act on stays beside the composer with its
        // button, instead of a toast that is gone before they can press it.
        move('idle');
        setFollowUp(toFollowUp(result.offer, 'error'));
        return;
      }
      setError(result.error);
      move('error');
    },
    [move, toFollowUp],
  );

  /** LEGACY ROAD: one blob, one POST. */
  const finishLegacy = useCallback(
    async (blob: Blob, durationMs: number, mimeType: string) => {
      if (!move('finishing')) return;
      const controller = new AbortController();
      abort.current = controller;
      const result = await transcribe(blob, {
        durationMs,
        mimeType,
        signal: controller.signal,
      });
      abort.current = null;
      if (!alive.current) return;
      if ('error' in result) {
        // An empty message is a withdrawal (the person pressed X), not a
        // failure to report.
        if (!result.error.message) {
          move('idle');
          return;
        }
        setError(result.error);
        move('error');
        return;
      }
      move('idle');
      onTranscriptRef.current(result.text, result.notice);
    },
    [move],
  );

  /** SESSION ROAD: upload what is left, finish, wait for the words. */
  const finishSession = useCallback(
    async (s: VoiceSession, durationMs: number) => {
      if (!move('finishing')) return;
      const result = await s.end(endReason.current, durationMs, {
        notices: notices.current,
        peakLevel: meterRan.current ? peakLevel.current : null,
      });
      if (session.current !== s) return; // discarded, or superseded
      session.current = null;
      deliver(result);
    },
    [deliver, move],
  );

  /** The server stopped taking parts while the recorder was still running. */
  const onInterrupt = useCallback(() => {
    const rec = recorder.current;
    if (rec && rec.state !== 'inactive' && current.current === 'recording') {
      outcome.current = 'stop';
      try {
        rec.stop();
      } catch {
        /* onstop still runs and ends the session */
      }
    }
  }, []);

  /**
   * A recording another tab left behind — a reload, a crash, a sign-out — is
   * finished here and offered back, never inserted unasked: the draft on
   * screen may belong to a different conversation.
   */
  const adopt = useCallback(
    async (record: OutboxRecord, outbox: OutboxStore) => {
      const s = await VoiceSession.adopt(record, { store: outbox });
      const result = await s.end(record.endedBy ?? 'page_hidden', record.durationMs);
      if (!alive.current) return;
      if (result.kind === 'text') {
        setFollowUp(
          toFollowUp(
            {
              kind: 'insert',
              text: result.text,
              message: VOICE_MESSAGES.recovered(formatElapsed(record.durationMs)),
              label: VOICE_MESSAGES.recoveredAction,
            },
            'info',
          ),
        );
      } else if (result.kind === 'error' && result.offer) {
        setFollowUp(toFollowUp(result.offer, 'error'));
      }
    },
    [toFollowUp],
  );

  const scanOrphans = useCallback(async () => {
    const outbox = await getStore();
    if (!outbox.persistent) return;
    let records: OutboxRecord[] = [];
    try {
      records = await outbox.listRecords();
    } catch {
      return;
    }
    for (const record of records) {
      if (!alive.current) return;
      if (session.current?.sessionId === record.sessionId) continue;
      const now = Date.now();
      const claimed = await outbox.claim(record.sessionId, now - OUTBOX_STALE_MS, now).catch(() => null);
      if (claimed) await adopt(claimed, outbox).catch(() => undefined);
    }
  }, [adopt, getStore]);

  useEffect(() => {
    void scanOrphans();
    // A tab that was signed out a few seconds ago still looks alive; look
    // again once its outbox has had time to go stale.
    const later = setTimeout(() => void scanOrphans(), OUTBOX_STALE_MS + 1000);
    return () => clearTimeout(later);
  }, [scanOrphans]);

  const start = useCallback(() => {
    if (!voiceSupported()) {
      setError({
        message: 'This browser cannot record audio. Try Chrome, Edge or Safari.',
        retryable: false,
      });
      current.current = 'error';
      setState('error');
      return;
    }
    // `error` can restart directly; `idle` is the ordinary path. Anything
    // else is a double click and is ignored.
    if (current.current === 'error') {
      current.current = 'idle';
      setState('idle');
      setError(null);
    }
    if (!move('requesting')) return;
    const gen = ++generation.current;
    setFollowUp(null);
    setHint(null);
    setWarning(null);
    setProgress(null);
    setMode(null);
    modeRef.current = null;

    // The session is asked for NOW, in parallel with the permission prompt,
    // so no audio ever waits on it.
    const clientKey = newClientKey();
    const picked = pickMimeType();
    const opening: Promise<OpenResult> | null = picked
      ? openSession({ clientKey, mimeType: picked })
      : null;
    // A session opened for a recording that never happened is deleted, or it
    // would hold this person's one live-recording slot until it idles out.
    const abandon = (open: Promise<OpenResult> | null) => {
      void open?.then((r) => {
        if (r.kind === 'session') void deleteSession(r.sessionId);
      });
    };

    void (async () => {
      let media: MediaStream;
      try {
        media = await navigator.mediaDevices.getUserMedia({
          audio: AUDIO_CONSTRAINTS,
        });
      } catch (err) {
        abandon(opening);
        if (!alive.current || gen !== generation.current) return;
        setError(describeCaptureError(err));
        move('error');
        return;
      }
      // Cancelled while the permission prompt was open: the stream arrived
      // for a recording nobody wants any more.
      if (!alive.current || current.current !== 'requesting' || gen !== generation.current) {
        media.getTracks().forEach((track) => track.stop());
        abandon(opening);
        return;
      }
      stream.current = media;

      // A browser with none of our preferred containers: build the recorder
      // first so the session can be told what it actually records.
      let early: MediaRecorder | null = null;
      if (!picked) {
        try {
          early = new MediaRecorder(media);
        } catch {
          early = null;
        }
      }
      const opened: OpenResult = await (opening ??
        openSession({ clientKey, mimeType: early?.mimeType || 'audio/webm' }));
      if (!alive.current || current.current !== 'requesting' || gen !== generation.current) {
        if (opened.kind === 'session') void deleteSession(opened.sessionId);
        return;
      }
      if (opened.kind === 'aborted') {
        release();
        move('idle');
        return;
      }
      if (opened.kind === 'error') {
        release();
        setError(opened.error);
        move('error');
        return;
      }
      if (opened.kind === 'active') {
        release();
        move('idle');
        if (opened.sessionId) {
          const sid = opened.sessionId;
          // Our own recording, left behind by a reload of this device, is
          // finished here rather than blamed on "another tab".
          const outbox = await getStore();
          const now = Date.now();
          const mine = await outbox.claim(sid, now - OUTBOX_STALE_MS, now).catch(() => null);
          if (mine) {
            await adopt(mine, outbox).catch(() => undefined);
            return;
          }
          setFollowUp(
            toFollowUp(
              {
                kind: 'end_other',
                sessionId: sid,
                message: VOICE_MESSAGES.sessionActive,
                label: VOICE_MESSAGES.sessionActiveAction,
              },
              'error',
            ),
          );
        } else {
          setError({ message: VOICE_MESSAGES.sessionActive, retryable: false });
          move('error');
        }
        return;
      }

      const onSessionRoad = opened.kind === 'session';
      const mimeType = picked || early?.mimeType || '';
      let rec: MediaRecorder;
      try {
        if (early) {
          rec = early;
        } else {
          const options: MediaRecorderOptions = mimeType ? { mimeType } : {};
          if (onSessionRoad && opened.config.bitsPerSecond) {
            options.audioBitsPerSecond = opened.config.bitsPerSecond;
          }
          rec = new MediaRecorder(media, mimeType || options.audioBitsPerSecond ? options : undefined);
        }
      } catch {
        if (onSessionRoad) void deleteSession(opened.sessionId);
        release();
        setError({
          message: 'This browser could not start a recording.',
          retryable: false,
        });
        move('error');
        return;
      }

      let s: VoiceSession | null = null;
      if (onSessionRoad) {
        const outbox = await getStore();
        s = new VoiceSession(
          {
            sessionId: opened.sessionId,
            mimeType: rec.mimeType || mimeType || 'audio/webm',
            config: opened.config,
            state: opened.state,
          },
          { store: outbox },
          {
            onProgress: (p) => {
              if (alive.current && session.current === s) setProgress(p);
            },
            onInterrupt,
          },
        );
        await s.open();
        if (!alive.current || current.current !== 'requesting' || gen !== generation.current) {
          void s.discard();
          return;
        }
        session.current = s;
        modeRef.current = 'session';
        setMode('session');
        setProgress(null);
      } else {
        session.current = null;
        modeRef.current = 'legacy';
        setMode('legacy');
        setHint(VOICE_MESSAGES.legacyHint);
      }

      chunks.current = [];
      outcome.current = 'stop';
      endReason.current = 'person';
      notices.current = [];
      peakLevel.current = 0;
      meterRan.current = false;
      hiddenAt.current = null;
      startedAt.current = Date.now();
      recordedMs.current = null;
      lastDataAt.current = startedAt.current;
      levelBuffer.current = new Array(LEVEL_BARS).fill(0);
      setLevels(levelBuffer.current);
      setElapsedMs(0);

      rec.ondataavailable = (event) => {
        if (!event.data || event.data.size === 0) return;
        lastDataAt.current = Date.now();
        if (s) {
          // Straight to the session and out of this closure: nothing here
          // keeps a reference, so the recording never accumulates in the tab.
          s.addSlice(event.data, Date.now() - startedAt.current);
        } else {
          chunks.current.push(event.data);
        }
      };
      let stopped = false;
      const onStopped = () => {
        if (stopped) return;
        stopped = true;
        const durationMs = Date.now() - startedAt.current;
        recordedMs.current = durationMs;
        release();
        if (s) {
          if (outcome.current === 'cancel') {
            if (session.current === s) session.current = null;
            void s.discard();
            move('idle');
            return;
          }
          if (!alive.current) {
            // Unmounted mid-recording: nobody is left to show the words to,
            // but the recording is the person's and is finished anyway.
            if (session.current === s) session.current = null;
            void s.end('page_hidden', durationMs).catch(() => undefined);
            return;
          }
          if (session.current !== s) return; // superseded
          if (durationMs < MIN_RECORDING_MS) {
            session.current = null;
            void s.discard();
            setError({ message: VOICE_MESSAGES.tooShort, retryable: true });
            move('error');
            return;
          }
          void finishSession(s, durationMs);
          return;
        }
        const parts = chunks.current;
        chunks.current = [];
        const type = rec.mimeType || mimeType || 'audio/webm';
        if (outcome.current === 'cancel') {
          move('idle');
          return;
        }
        const blob = new Blob(parts, { type });
        if (durationMs < MIN_RECORDING_MS || blob.size === 0) {
          // A tap, not speech. Say so rather than sending silence to a GPU
          // and reporting its empty answer as a failure.
          setError({ message: VOICE_MESSAGES.tooShort, retryable: true });
          move('error');
          return;
        }
        void finishLegacy(blob, durationMs, type);
      };
      rec.onstop = onStopped;
      rec.onerror = () => {
        if (s) {
          // The audio up to here is already uploaded or in the outbox, so
          // this ends the recording rather than losing it.
          endReason.current = 'recorder_error';
          notices.current.push(
            VOICE_MESSAGES.recorderError(formatElapsed(Date.now() - startedAt.current)),
          );
          if (rec.state !== 'inactive') {
            try {
              rec.stop();
              return;
            } catch {
              /* fall through */
            }
          }
          queueMicrotask(onStopped);
          return;
        }
        release();
        setError({ message: 'Recording stopped unexpectedly.', retryable: true });
        move('error');
      };

      // A capture that ends by itself — iOS suspending the microphone when
      // the screen locks, a headset unplugged — ends the recording, and the
      // person is told why when they come back.
      for (const track of media.getAudioTracks?.() ?? []) {
        track.addEventListener?.('ended', () => {
          if (current.current !== 'recording' || recorder.current !== rec) return;
          const now = Date.now() - startedAt.current;
          if (hiddenAt.current !== null || document.visibilityState === 'hidden') {
            endReason.current = 'page_hidden';
            const from = formatElapsed(lastDataAt.current - startedAt.current);
            notices.current.push(VOICE_MESSAGES.pausedHidden(from, formatElapsed(now)));
          } else {
            endReason.current = 'recorder_error';
            notices.current.push(VOICE_MESSAGES.recorderError(formatElapsed(now)));
          }
          if (rec.state !== 'inactive') {
            try {
              rec.stop();
            } catch {
              queueMicrotask(onStopped);
            }
          }
        });
      }

      recorder.current = rec;
      try {
        // The session road's timeslice is the server's part size (5 s): each
        // slice is uploaded as it arrives. The legacy road keeps 1 s.
        rec.start(onSessionRoad ? opened.config.partMs : 1000);
      } catch {
        // start() throws on its own (a device that ended between the grant
        // and here, a container the constructor accepted and the encoder did
        // not). Unguarded it escapes this async function as an unhandled
        // rejection, and the microphone it already opened stays open: the
        // capture indicator burns on a page stuck saying "Waiting for the
        // microphone…" with nothing recording behind it.
        recorder.current = null;
        if (s) {
          session.current = null;
          void s.discard();
        }
        release();
        setError({
          message: 'Recording could not start. Check your microphone and try again.',
          retryable: true,
        });
        move('error');
        return;
      }
      if (!move('recording')) {
        release();
        return;
      }
      void takeWakeLock();

      let metered = false;
      try {
        const Ctx =
          window.AudioContext ??
          (window as unknown as { webkitAudioContext?: typeof AudioContext })
            .webkitAudioContext;
        if (Ctx) {
          const context = new Ctx();
          audioContext.current = context;
          const analyser = context.createAnalyser();
          // 1024 samples is ~21 ms at 48 kHz: long enough for a stable RMS,
          // short enough that the meter tracks syllables rather than phrases.
          analyser.fftSize = 1024;
          analyser.smoothingTimeConstant = 0.6;
          context.createMediaStreamSource(media).connect(analyser);
          runMeter(analyser);
          metered = true;
        }
      } catch {
        // No meter. The recording itself is unaffected, and the bar falls
        // back to a flat trace rather than failing the dictation.
      }
      if (!metered) {
        // The meter also drives the clock; without it the clock ticks here.
        clockTimer.current = setInterval(() => {
          if (alive.current && current.current === 'recording') {
            setElapsedMs(Date.now() - startedAt.current);
          }
        }, 1000);
      }
    })();
  }, [adopt, finishLegacy, finishSession, getStore, move, onInterrupt, release, runMeter, takeWakeLock, toFollowUp]);
  startRef.current = start;

  const stop = useCallback(() => {
    if (current.current !== 'recording') return;
    outcome.current = 'stop';
    const rec = recorder.current;
    if (!rec || rec.state === 'inactive') {
      release();
      move('idle');
      return;
    }
    // The microphone is closed by `release()` inside onstop, which fires
    // synchronously after this in every browser that implements the spec.
    try {
      rec.stop();
    } catch {
      release();
      move('idle');
    }
  }, [move, release]);

  const cancel = useCallback(() => {
    const s = session.current;
    if (s && (current.current === 'recording' || current.current === 'finishing')) {
      // The recording is saved on the server; X deletes it. Past a minute
      // that is worth a question.
      // While finishing, the recording's own length, not the time since it began.
      const length =
        current.current === 'recording' || recordedMs.current === null
          ? Math.max(Date.now() - startedAt.current, 0)
          : recordedMs.current;
      if (length > DISCARD_CONFIRM_AFTER_MS) {
        const ask =
          typeof window !== 'undefined' && typeof window.confirm === 'function'
            ? window.confirm.bind(window)
            : () => true;
        if (!ask(VOICE_MESSAGES.discardConfirm(formatElapsed(length)))) return;
      }
    }
    generation.current += 1;
    abort.current?.abort();
    abort.current = null;
    if (current.current === 'recording') {
      outcome.current = 'cancel';
      const rec = recorder.current;
      if (rec && rec.state !== 'inactive') {
        try {
          rec.stop();
          return; // onstop releases, discards and returns to idle
        } catch {
          /* fall through to the hard reset */
        }
      }
    }
    if (s) {
      session.current = null;
      void s.discard();
    }
    release();
    chunks.current = [];
    current.current = 'idle';
    setState('idle');
    setError(null);
    setElapsedMs(0);
  }, [release]);

  const dismissError = useCallback(() => {
    setError(null);
    current.current = 'idle';
    setState('idle');
  }, []);

  // The legacy road's ceiling. Enforced here rather than only on the server
  // so the person sees a finished recording instead of a rejected upload.
  // The session road has none: nothing is armed, however long they talk.
  useEffect(() => {
    if (state !== 'recording' || mode !== 'legacy') return;
    const remaining = Math.max(0, maxMs - elapsedMs);
    const timer = setTimeout(() => stop(), remaining);
    return () => clearTimeout(timer);
    // Only re-armed when recording starts: `elapsedMs` changes every frame
    // and is read once, at arm time, on purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, mode, maxMs, stop]);

  // A phone that turns its screen off may stop capturing. Coming back, the
  // wake lock is taken again and the person is told about any stretch that
  // produced no audio.
  useEffect(() => {
    if (state !== 'recording' && state !== 'finishing') return;
    const onVisibility = () => {
      if (current.current !== 'recording') return;
      if (document.visibilityState === 'hidden') {
        hiddenAt.current = Date.now();
        return;
      }
      void takeWakeLock();
      const partMs = session.current?.config.partMs ?? 1000;
      const now = Date.now();
      if (hiddenAt.current !== null && now - lastDataAt.current > 2 * partMs) {
        const line = VOICE_MESSAGES.pausedHidden(
          formatElapsed(lastDataAt.current - startedAt.current),
          formatElapsed(now - startedAt.current),
        );
        notices.current.push(line);
        setWarning(line);
      }
      hiddenAt.current = null;
    };
    const onOnline = () => session.current?.nudge();
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('online', onOnline);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('online', onOnline);
    };
  }, [state, takeWakeLock]);

  return {
    state,
    levels,
    elapsedMs,
    error,
    supported,
    start,
    stop,
    cancel,
    dismissError,
    mode,
    limitMs: mode === 'session' ? null : maxMs,
    progress,
    hint,
    warning,
    followUp,
  };
}
