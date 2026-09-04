'use client';

/**
 * The composer while it is listening.
 *
 * Replaces the controls row rather than sitting beside it, because recording
 * is a MODE: while it is on there is nothing useful to do with the model
 * picker or the attachment button, and leaving them there invites a click
 * that cannot work. The row keeps the same height and the same rounded
 * surface, so entering and leaving the mode moves nothing on the page.
 *
 * THE WAVEFORM IS REAL. Every bar is one RMS reading of the microphone taken
 * ~16 times a second (components/useVoiceRecorder.ts). Silence is flat. A
 * looping animation would be easier and would also be a lie — someone whose
 * microphone is muted deserves to see that nothing is arriving, and that is
 * exactly the moment a fake visualiser would reassure them.
 *
 * Bars are drawn as elements, not canvas: forty-eight 2px divs cost nothing,
 * they inherit the theme's colours without a resolve step, and they stay
 * crisp on a HiDPI screen without a devicePixelRatio dance.
 */

import { IconStop, IconX } from './icons';
import { Loader } from './Loader';
import { LEVEL_BARS } from '@/lib/voice';
import type { VoiceState } from '@/lib/voice';

/** mm:ss — a dictation is never long enough to need hours. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function Waveform({ levels }: { levels: number[] }) {
  return (
    <div
      aria-hidden
      className="flex h-8 flex-1 items-center justify-end gap-[2px] overflow-hidden"
    >
      {levels.map((level, index) => (
        <span
          key={index}
          className="w-[2px] shrink-0 rounded-full bg-ink/45 transition-[height] duration-100 ease-out"
          style={{
            // A floor of 2px keeps the trace visible through silence, so the
            // bar reads as "listening, nothing heard" rather than "broken".
            height: `${Math.max(2, Math.round(level * 26))}px`,
            // The oldest bars fade out, which gives the trace direction
            // without moving anything.
            opacity: 0.35 + 0.65 * (index / Math.max(1, levels.length - 1)),
          }}
        />
      ))}
    </div>
  );
}

export function VoiceBar({
  state,
  levels,
  elapsedMs,
  maxMs,
  onCancel,
  onStop,
}: {
  state: Extract<VoiceState, 'requesting' | 'recording' | 'transcribing'>;
  levels: number[];
  elapsedMs: number;
  maxMs: number;
  onCancel: () => void;
  onStop: () => void;
}) {
  const recording = state === 'recording';
  const transcribing = state === 'transcribing';
  const remaining = Math.max(0, maxMs - elapsedMs);
  // Only in the last thirty seconds. A countdown that is always on turns a
  // two-sentence dictation into a timed exam.
  const closing = recording && remaining <= 30_000;

  return (
    <div
      className="flex h-[52px] items-center gap-3 px-2"
      // One live region for the whole bar: a screen reader is told the state
      // changed, not read a timer forty-eight times a second.
      role="status"
      aria-live="polite"
    >
      <button
        type="button"
        onClick={onCancel}
        aria-label={transcribing ? 'Cancel transcription' : 'Cancel recording'}
        title={transcribing ? 'Cancel' : 'Cancel recording (Esc)'}
        className="shrink-0 rounded-lg p-2 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <IconX size={17} />
      </button>

      {transcribing ? (
        <span className="flex flex-1 items-center justify-center gap-2.5 text-sm text-muted">
          <Loader size={16} />
          Transcribing…
        </span>
      ) : state === 'requesting' ? (
        <span className="flex flex-1 items-center justify-center gap-2.5 text-sm text-muted">
          <Loader size={16} />
          Waiting for the microphone…
        </span>
      ) : (
        <>
          <Waveform levels={levels} />
          <span
            className={`shrink-0 text-xs tabular-nums ${
              closing ? 'text-warn' : 'text-muted'
            }`}
            title={closing ? 'Recording will stop at the limit' : undefined}
          >
            {closing
              ? `−${formatElapsed(remaining)}`
              : formatElapsed(elapsedMs)}
          </span>
        </>
      )}

      {/* The primary action stays in the primary position — the same corner
          the send button occupies, so the thumb does not have to move. */}
      <button
        type="button"
        onClick={onStop}
        disabled={!recording}
        aria-label="Stop recording and transcribe"
        title="Stop and transcribe (Enter)"
        className="shrink-0 rounded-lg bg-accent-strong p-2 text-white transition-all duration-ts hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-35"
      >
        <IconStop size={17} />
      </button>

      {/* The state in words, for a screen reader. The waveform above is
          aria-hidden and the timer is decoration; this sentence is what is
          actually announced. */}
      <span className="sr-only">
        {transcribing
          ? 'Transcribing your recording'
          : recording
            ? `Recording, ${formatElapsed(elapsedMs)} elapsed`
            : 'Waiting for microphone permission'}
      </span>
    </div>
  );
}

/** The bar's shape when there is nothing to draw yet — keeps tests honest. */
export const VOICE_BAR_LEVELS = LEVEL_BARS;
