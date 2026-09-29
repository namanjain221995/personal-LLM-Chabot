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

import { useEffect, useId, useState } from 'react';
import { IconStop, IconX } from './icons';
import { Loader } from './Loader';
import {
  BACKLOG_NOTICE_MS,
  LEVEL_BARS,
  VOICE_MESSAGES,
  formatElapsed,
  idleWords,
} from '@/lib/voice';
import type { SessionProgress, VoiceState } from '@/lib/voice';
import { spaceBetween, type VoiceLanguage } from '@/lib/voiceLive';
import type { VoiceFollowUp } from './useVoiceRecorder';

/**
 * m:ss, and h:mm:ss from an hour. It lives in lib/voice.ts now, because the
 * failure sentences quote durations too; re-exported here for the callers
 * and tests that always imported it from the bar.
 */
export { formatElapsed };

/**
 * Milliseconds since `active` last became true; 0 while it is false.
 *
 * The transcription wait gets its OWN clock. `elapsedMs` stops at the
 * recording's length when the recorder closes, and a ten-minute recording
 * decodes for four to seven minutes more (595 s of audio took 268.3 s on a
 * quiet replica; 300 s took 219.7 s on a busy one, measured 2026-09-18).
 * Minutes of a bare "Transcribing…" read as a hang, and a clock frozen at
 * the recording's length reads as one too.
 */
function useWaitClock(active: boolean): number {
  const [waitedMs, setWaitedMs] = useState(0);
  useEffect(() => {
    setWaitedMs(0);
    if (!active) return;
    const started = Date.now();
    const timer = setInterval(() => setWaitedMs(Date.now() - started), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return waitedMs;
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


/** The live transcript's languages, each named in itself. */
const LANGUAGE_CHOICES: ReadonlyArray<{ value: VoiceLanguage; label: string; lang?: string }> = [
  { value: 'auto', label: 'Auto' },
  { value: 'en', label: 'English', lang: 'en' },
  { value: 'hi', label: 'हिन्दी', lang: 'hi' },
];

/**
 * Which language the live transcript listens for (2026-09-30). The English
 * model makes a third fewer errors on English than the multilingual one
 * (LibriSpeech 4.13% against 6.23% WER), and pinning Hindi helps the
 * multilingual one on Hindi (FLEURS-hi 7.48% against 8.58% on auto), so a
 * person who knows gets to say. Changing it mid-recording starts the live
 * stream again in the new language from the last committed word; the stored
 * recording is not touched.
 *
 * Native radio buttons, so the arrow keys, the one Tab stop and what a screen
 * reader says are the browser's own. Outside the status row, like the
 * transcript: the row is the one live region, and a control inside it would
 * be announced with every state change. Tokens only: `bg-surface-2` and
 * `text-ink` for the choice, with no opacity modifier on a var() token (it
 * compiles to nothing, the trap described at the top of this file).
 */
function LanguageChoice({
  value,
  onChange,
}: {
  value: VoiceLanguage;
  onChange: (language: VoiceLanguage) => void;
}) {
  const name = useId();
  return (
    <div
      role="radiogroup"
      aria-label="Language of the live transcript"
      data-testid="voice-language"
      className="ml-auto flex shrink-0 items-center rounded-lg border border-border p-0.5"
    >
      {LANGUAGE_CHOICES.map((choice) => (
        <label key={choice.value} className="cursor-pointer">
          <input
            type="radio"
            name={name}
            value={choice.value}
            checked={value === choice.value}
            onChange={() => onChange(choice.value)}
            className="peer sr-only"
          />
          <span
            lang={choice.lang}
            className="block rounded-md px-2 py-0.5 text-muted transition-colors duration-ts hover:text-ink peer-checked:bg-surface-2 peer-checked:text-ink peer-focus-visible:ring-2 peer-focus-visible:ring-accent"
          >
            {choice.label}
          </span>
        </label>
      ))}
    </div>
  );
}

/**
 * The lines above the controls row while a SESSION records or finishes: what
 * has been transcribed so far, where the recording is kept, and anything the
 * person should know about the upload.
 *
 * Not a live region. The transcript grows every few seconds for as long as
 * someone talks, and announcing each piece would drown the one sentence the
 * status row announces. A screen reader can still read it on demand.
 */
function SessionPanel({
  state,
  progress,
  hint,
  warning,
  language,
  onLanguage,
}: {
  state: 'requesting' | 'recording' | 'finishing';
  progress: SessionProgress | null;
  hint: string | null;
  warning: string | null;
  language: VoiceLanguage | null;
  onLanguage: ((language: VoiceLanguage) => void) | null;
}) {
  const lines: Array<{ key: string; text: string; tone: 'muted' | 'warn' }> = [];
  if (hint) lines.push({ key: 'hint', text: hint, tone: 'muted' });
  if (warning) lines.push({ key: 'warning', text: warning, tone: 'warn' });
  if (progress) {
    if (progress.offline) {
      lines.push({
        key: 'offline',
        // Past the server's idle close the audio no longer simply "uploads
        // when the connection is back": the server has closed the recording,
        // and what it missed waits on this device (2026-09-29).
        text: progress.offlineLong
          ? VOICE_MESSAGES.offlineLong(idleWords(progress.idleCloseS ?? 600))
          : state === 'finishing'
            ? VOICE_MESSAGES.offlineFinishing(formatElapsed(progress.pendingMs))
            : VOICE_MESSAGES.offlineRecording,
        tone: 'warn',
      });
    } else if (progress.storageTrouble) {
      lines.push({ key: 'storage', text: VOICE_MESSAGES.storageTrouble, tone: 'warn' });
    }
    if (!progress.progressive && state === 'recording') {
      lines.push({ key: 'progressive', text: VOICE_MESSAGES.notProgressive, tone: 'muted' });
    } else if (state === 'recording' && progress.backlogMs > BACKLOG_NOTICE_MS) {
      lines.push({
        key: 'behind',
        text: VOICE_MESSAGES.behind(formatElapsed(progress.backlogMs), progress.waitingOn),
        tone: 'muted',
      });
    }
    // An engine that cannot be reached is said at once, whatever the backlog:
    // it is not "behind", and the audio being saved is what matters now.
    if (progress.waitingOn === 'engine_unavailable' && !lines.some((l) => l.key === 'behind')) {
      lines.push({ key: 'engine', text: VOICE_MESSAGES.engineUnavailableLive, tone: 'warn' });
    }
  }
  // In the order they were spoken: the server's final segments, the text it
  // still holds back, then the live transcript's committed utterances and
  // the one being heard now (lib/voiceLive.ts drops what the first two
  // already cover). Committed words in ink; words that may still change in
  // muted.
  const pieces: Array<{ key: string; text: string; settled: boolean }> = [
    { key: 'preview', text: progress?.preview ?? '', settled: true },
    { key: 'tentative', text: progress?.tentative ?? '', settled: false },
    { key: 'live', text: progress?.live?.committed ?? '', settled: true },
    { key: 'partial', text: progress?.live?.partial ?? '', settled: false },
  ].filter((piece) => piece.text);
  const saved = progress !== null && progress.savedMs !== 0;
  const choosing = state === 'recording' && language !== null && onLanguage !== null;
  if (!lines.length && !pieces.length && !progress && !choosing) return null;
  return (
    <div className="flex flex-col gap-1 px-3 pt-1 text-xs" data-testid="voice-session-panel">
      {pieces.length > 0 && (
        // THE TAIL, NOT THE HEAD (2026-09-29). This was `line-clamp-3`, which
        // keeps the FIRST three lines: past three lines (the preview holds up
        // to 600 characters) the words being spoken now were the ones cut,
        // while this comment said the opposite. Measured in Chromium: the
        // newest words sat at y=356 in a box that ended at y=74. The box is
        // now anchored to its bottom edge (a column justified to its end, four
        // lines at most, the overflow hidden), so older lines leave by the top
        // and the newest words are always the ones in view: seven lines of
        // text in the 80 px box, measured the same way, put the last line at
        // 614-632 inside a box spanning 553-633 and the first three above it.
        <div className="flex max-h-20 flex-col justify-end overflow-hidden" data-testid="voice-transcript">
          <p className="break-words text-sm leading-5 text-ink" dir="auto">
            {pieces.map((piece, index) => {
              const before = index > 0 ? spaceBetween(pieces[index - 1]!.text, piece.text) : '';
              return piece.settled ? (
                <span key={piece.key}>
                  {before}
                  {piece.text}
                </span>
              ) : (
                <span key={piece.key} className="text-muted">
                  {before}
                  {piece.text}
                </span>
              );
            })}
          </p>
        </div>
      )}
      {lines.map((line) => (
        <p key={line.key} className={line.tone === 'warn' ? 'text-warn' : 'text-muted'}>
          {line.text}
        </p>
      ))}
      {(saved || choosing) && (
        // One row: where the recording is kept on the left, the live
        // transcript's language at the right, above the Stop button. On a
        // narrow phone the language wraps under the saved line.
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {saved && progress && (
            <p className="min-w-0 text-faint">
              {/* Where the stored recordings are (feat/voice-recordings-page). A
                  new tab: leaving this page while recording would end the
                  recording. Drawn only once the server has acknowledged audio,
                  and it says how much, and how much is still only on this device. */}
              <a
                href="/recordings"
                target="_blank"
                rel="noopener"
                className="underline-offset-2 hover:text-muted hover:underline"
              >
                {progress.savedMs === undefined
                  ? VOICE_MESSAGES.saved(progress.retentionDays)
                  : VOICE_MESSAGES.savedSoFar(
                      formatElapsed(progress.savedMs),
                      progress.pendingMs > 0 ? formatElapsed(progress.pendingMs) : null,
                      progress.retentionDays,
                    )}
              </a>
            </p>
          )}
          {choosing && <LanguageChoice value={language} onChange={onLanguage} />}
        </div>
      )}
    </div>
  );
}

export function VoiceBar({
  state,
  levels,
  elapsedMs,
  maxMs,
  progress = null,
  hint = null,
  warning = null,
  language = null,
  onLanguage = null,
  onCancel,
  onStop,
}: {
  state: Extract<VoiceState, 'requesting' | 'recording' | 'finishing'>;
  levels: number[];
  elapsedMs: number;
  /**
   * The ceiling in force, or null for none. Only the legacy road has one
   * (ten minutes); a session records for as long as the person talks, so it
   * shows no countdown and nothing warns that it will stop.
   */
  maxMs: number | null;
  /** Upload and transcript progress on the session road. */
  progress?: SessionProgress | null;
  /** One sentence about the road this recording took. */
  hint?: string | null;
  /** Something that happened while recording, e.g. the screen went off. */
  warning?: string | null;
  /**
   * The live transcript's language, while the recording has one; the
   * control is drawn only then, and only while recording.
   */
  language?: VoiceLanguage | null;
  onLanguage?: ((language: VoiceLanguage) => void) | null;
  onCancel: () => void;
  onStop: () => void;
}) {
  const recording = state === 'recording';
  const finishing = state === 'finishing';
  const waitedMs = useWaitClock(finishing);
  const remaining = maxMs === null ? Infinity : Math.max(0, maxMs - elapsedMs);
  // Only in the last thirty seconds, and only where there IS a limit. A
  // countdown that is always on turns a two-sentence dictation into a timed
  // exam.
  const closing = recording && remaining <= 30_000;
  const finishingText =
    progress && progress.backlogMs > 0
      ? VOICE_MESSAGES.finishingTail(formatElapsed(progress.backlogMs))
      : progress
        ? VOICE_MESSAGES.finishing
        : 'Transcribing…';

  return (
    <div className="flex flex-col">
      <SessionPanel
        state={state}
        progress={progress}
        hint={hint}
        warning={warning}
        language={language}
        onLanguage={onLanguage}
      />
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
          aria-label={finishing ? 'Cancel transcription' : 'Cancel recording'}
          title={finishing ? 'Cancel' : 'Cancel recording (Esc)'}
          className="shrink-0 rounded-lg p-2 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <IconX size={17} />
        </button>

        {finishing ? (
          <span className="flex min-w-0 flex-1 items-center justify-center gap-2.5 text-sm text-muted">
            <Loader size={16} />
            <span className="truncate">{finishingText}</span>
            {/* aria-hidden: the live region announces the state once (the
                sr-only sentence below), not a number every second. */}
            <span aria-hidden="true" className="text-xs tabular-nums">
              {formatElapsed(waitedMs)}
            </span>
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
          {finishing
            ? 'Transcribing your recording'
            : recording
              ? `Recording, ${formatElapsed(elapsedMs)} elapsed`
              : 'Waiting for microphone permission'}
        </span>
      </div>
    </div>
  );
}

/**
 * One line beside the composer after a recording, with the thing the person
 * can do about it: Retry a saved recording's missing parts, end a recording
 * left running in another tab, or insert a recording a closed tab finished.
 * A toast would be gone before they could press the button.
 *
 * THE ACTIONS WRAP UNDER THE MESSAGE WHEN THEY DO NOT FIT (2026-09-30). They
 * used to sit beside it at their full width whatever the width of the line,
 * and the message took what was left. On a 390 px phone (311 px of composer)
 * "Retry" beside "Insert what was heard live (part of the recording)" left
 * the message 0 px wide: one letter per line, a line 2,300 px tall, and the
 * second button running off the edge (measured in Chromium). The message now
 * keeps at least 12rem, the actions go to the next row together when they
 * cannot stand beside it, and a label longer than the row wraps inside its
 * button; the row's -ml-2 lines the buttons' words up with the message's
 * (their hover fill reaches into the line's padding). Wide enough, nothing
 * moves: message, actions, dismiss, in one row.
 */
export function VoiceFollowUpLine({ followUp }: { followUp: VoiceFollowUp }) {
  const primary = followUp.actionLabel ? (
    <button
      type="button"
      onClick={followUp.run}
      disabled={followUp.busy}
      className="max-w-full rounded-lg px-2 py-0.5 text-left font-medium text-accent transition-colors duration-ts hover:bg-surface-2 disabled:cursor-wait disabled:opacity-60"
    >
      {followUp.busy ? <Loader size={12} /> : followUp.actionLabel}
    </button>
  ) : null;
  const secondary =
    followUp.secondaryLabel && followUp.runSecondary ? (
      <button
        type="button"
        onClick={followUp.runSecondary}
        disabled={followUp.busy}
        className="max-w-full rounded-lg px-2 py-0.5 text-left text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:opacity-60"
      >
        {followUp.secondaryLabel}
      </button>
    ) : null;
  return (
    <div
      role={followUp.tone === 'error' ? 'alert' : undefined}
      className={`mb-2 flex items-start gap-2 rounded-ts border border-border px-3 py-2 text-xs ${
        followUp.tone === 'error' ? 'text-ink' : 'text-muted'
      }`}
    >
      <div className="flex min-w-0 flex-1 flex-wrap items-start gap-x-2 gap-y-1">
        <p className="min-w-[min(12rem,100%)] flex-1 break-words">{followUp.message}</p>
        {(primary || secondary) && (
          <div className="-ml-2 flex max-w-full flex-wrap items-start gap-1" data-testid="voice-follow-up-actions">
            {primary}
            {secondary}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={followUp.dismiss}
        aria-label="Dismiss"
        className="shrink-0 rounded-lg p-0.5 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
      >
        <IconX size={13} />
      </button>
    </div>
  );
}

/** The bar's shape when there is nothing to draw yet — keeps tests honest. */
export const VOICE_BAR_LEVELS = LEVEL_BARS;
