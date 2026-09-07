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
 *
 * THE BAR COLOUR IS A COMPILING UTILITY, NOT `bg-ink/45`, AND THAT MATTERS.
 * `ink` is `var(--ts-text)` in tailwind.config.ts — a BARE var(). Tailwind 3
 * cannot parse a var() as a colour, so an opacity modifier on one does not
 * dim it: the whole utility is dropped and NO background-color is emitted.
 * `bg-ink/45` therefore shipped forty-eight fully transparent bars — the DOM,
 * the heights and the audio were all correct and the trace was invisible.
 * (The same trap is recorded three times in tailwind.config.ts, for `accent`,
 * `danger` and `ok`, which were given `rgb(... / <alpha-value>)` to escape
 * it.) The trace is now `bg-accent` — that `<alpha-value>` form, used with no
 * modifier at all, so it cannot fall into the hole twice. The fade across the
 * trace is the inline `opacity` below, which is a style, not a utility, and
 * was never affected.
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

/** Tallest a bar is drawn, at level 1.0. The container must be taller. */
const PEAK_PX = 34;
/** Silence. Not zero: a flat row of dots reads as "listening", not "broken". */
const FLOOR_PX = 3;

function Waveform({ levels }: { levels: number[] }) {
  return (
    <div
      aria-hidden
      // CENTRED, not end-aligned (owner request 2026-09-07). The trace used to
      // grow leftward from the timer, which read as hugging the Stop button.
      //
      // Centring is safe ONLY because the whole trace fits. `justify-center`
      // plus `overflow-hidden` clips at BOTH ends, so an overflowing trace
      // loses its newest bars — the one thing a live meter must not do.
      //
      // The arithmetic, with LEVEL_BARS = 48 (bars x width + gaps x gap):
      //   md+     48x6 + 47x3 = 429px  in a  620px box on a 768px composer
      //   mobile  48x3 + 47x1 = 191px  in a ~228px box on a 375px one
      //
      // The mobile GAP is 1px, not 2px: when the bars were thickened from
      // 2px to 3px (2026-09-07) the gap was left alone, and 48x3 + 47x2 is
      // 238px — ten pixels wider than the box, clipping the newest bars on
      // every phone. Thicker bars are the point; the gap is what gives way.
      className="flex h-10 flex-1 items-center justify-center gap-[1px] overflow-hidden md:gap-[3px]"
    >
      {levels.map((level, index) => (
        <span
          key={index}
          // `bg-accent` is the theme-aware TechSara blue (#60a5fa on dark,
          // #1d4ed8 on light) — the token tuned for contrast AGAINST a
          // surface, which is what a 3px mark needs. `accent-strong` is the
          // Send button's FILL colour and reads as a dark smudge at this
          // width on the dark composer. No opacity modifier is used here, and
          // `accent` is the rgb(... / <alpha-value>) form in any case, so
          // this compiles — unlike the `bg-ink/45` it replaced.
          className="w-[3px] shrink-0 rounded-full bg-accent transition-[height] duration-100 ease-out md:w-[6px]"
          style={{
            height: `${Math.max(FLOOR_PX, Math.round(level * PEAK_PX))}px`,
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
