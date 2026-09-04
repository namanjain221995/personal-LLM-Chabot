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
 * The transitions live in lib/voice.ts and are unit-tested without a DOM.
 * What is here is the part that genuinely needs the browser.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AUDIO_CONSTRAINTS,
  LEVEL_BARS,
  MIN_RECORDING_MS,
  canTransition,
  describeCaptureError,
  levelFrom,
  pickMimeType,
  transcribe,
  voiceSupported,
  type VoiceError,
  type VoiceState,
} from '@/lib/voice';

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
  /** Throw the recording away. Never transcribes. */
  cancel: () => void;
  dismissError: () => void;
}

export function useVoiceRecorder({
  onTranscript,
  maxMs = 10 * 60 * 1000,
}: {
  /** Called once, with the text, when a recording transcribes successfully. */
  onTranscript: (text: string) => void;
  /** Hard ceiling; the recorder stops itself rather than being refused later. */
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

  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const audioContext = useRef<AudioContext | null>(null);
  const frame = useRef<number | null>(null);
  const chunks = useRef<Blob[]>([]);
  const startedAt = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const outcome = useRef<'stop' | 'cancel'>('stop');
  const levelBuffer = useRef<number[]>(new Array(LEVEL_BARS).fill(0));
  // The state machine's own copy, read inside callbacks that were created in
  // an older render. React state alone would let a stale closure re-enter a
  // transition that has already happened.
  const current = useRef<VoiceState>('idle');
  const alive = useRef(true);

  const move = useCallback((next: VoiceState): boolean => {
    if (!canTransition(current.current, next)) return false;
    current.current = next;
    if (alive.current) setState(next);
    return true;
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
  }, []);

  useEffect(() => {
    setSupported(voiceSupported());
  }, []);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      abort.current?.abort();
      release();
    };
  }, [release]);

  /** The meter loop: one rAF per frame while recording, and not one after. */
  const runMeter = useCallback((analyser: AnalyserNode) => {
    const samples = new Uint8Array(analyser.fftSize);
    let lastPush = 0;
    const tick = (now: number) => {
      if (current.current !== 'recording') return;
      analyser.getByteTimeDomainData(samples);
      // ~16 bars a second. Pushing at the display's rate would scroll three
      // seconds of history past in under one, and cost battery for a trace
      // nobody can read at that speed.
      if (now - lastPush >= 60) {
        lastPush = now;
        const next = levelBuffer.current.slice(1);
        next.push(levelFrom(samples));
        levelBuffer.current = next;
        if (alive.current) setLevels(next);
      }
      if (alive.current) setElapsedMs(Date.now() - startedAt.current);
      frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
  }, []);

  const finish = useCallback(
    async (blob: Blob, durationMs: number, mimeType: string) => {
      if (!move('transcribing')) return;
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
      onTranscript(result.text);
    },
    [move, onTranscript],
  );

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

    void (async () => {
      let media: MediaStream;
      try {
        media = await navigator.mediaDevices.getUserMedia({
          audio: AUDIO_CONSTRAINTS,
        });
      } catch (err) {
        if (!alive.current) return;
        setError(describeCaptureError(err));
        move('error');
        return;
      }
      // Cancelled while the permission prompt was open: the stream arrived
      // for a recording nobody wants any more.
      if (!alive.current || current.current !== 'requesting') {
        media.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = media;

      const mimeType = pickMimeType();
      let rec: MediaRecorder;
      try {
        rec = new MediaRecorder(media, mimeType ? { mimeType } : undefined);
      } catch {
        release();
        setError({
          message: 'This browser could not start a recording.',
          retryable: false,
        });
        move('error');
        return;
      }

      chunks.current = [];
      outcome.current = 'stop';
      startedAt.current = Date.now();
      levelBuffer.current = new Array(LEVEL_BARS).fill(0);
      setLevels(levelBuffer.current);
      setElapsedMs(0);

      rec.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) chunks.current.push(event.data);
      };
      rec.onstop = () => {
        const durationMs = Date.now() - startedAt.current;
        const parts = chunks.current;
        chunks.current = [];
        const type = rec.mimeType || mimeType || 'audio/webm';
        release();
        if (outcome.current === 'cancel') {
          move('idle');
          return;
        }
        const blob = new Blob(parts, { type });
        if (durationMs < MIN_RECORDING_MS || blob.size === 0) {
          // A tap, not speech. Say so rather than sending silence to a GPU
          // and reporting its empty answer as a failure.
          setError({
            message: 'That was too short to transcribe. Hold the button and speak.',
            retryable: true,
          });
          move('error');
          return;
        }
        void finish(blob, durationMs, type);
      };
      rec.onerror = () => {
        release();
        setError({ message: 'Recording stopped unexpectedly.', retryable: true });
        move('error');
      };

      recorder.current = rec;
      try {
        // A timeslice makes the recorder emit as it goes, so a tab that is
        // closed mid-recording has still produced something, and a long
        // dictation does not sit in one growing buffer.
        rec.start(1000);
      } catch {
        // start() throws on its own (a device that ended between the grant
        // and here, a container the constructor accepted and the encoder did
        // not). Unguarded it escapes this async function as an unhandled
        // rejection, and the microphone it already opened stays open: the
        // capture indicator burns on a page stuck saying "Waiting for the
        // microphone…" with nothing recording behind it.
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
        }
      } catch {
        // No meter. The recording itself is unaffected, and the bar falls
        // back to a flat trace rather than failing the dictation.
      }
    })();
  }, [finish, move, release, runMeter]);

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
    abort.current?.abort();
    abort.current = null;
    if (current.current === 'recording') {
      outcome.current = 'cancel';
      const rec = recorder.current;
      if (rec && rec.state !== 'inactive') {
        try {
          rec.stop();
          return; // onstop releases and returns to idle
        } catch {
          /* fall through to the hard reset */
        }
      }
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

  // The ceiling. Enforced here rather than only on the server so the person
  // sees a finished recording instead of a rejected upload.
  useEffect(() => {
    if (state !== 'recording') return;
    const remaining = Math.max(0, maxMs - elapsedMs);
    const timer = setTimeout(() => stop(), remaining);
    return () => clearTimeout(timer);
    // Only re-armed when recording starts: `elapsedMs` changes every frame
    // and is read once, at arm time, on purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, maxMs, stop]);

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
  };
}
