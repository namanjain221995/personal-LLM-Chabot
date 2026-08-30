'use client';

/**
 * The live progress line while an answer is still being worked on.
 *
 * WHY IT TICKS (owner request 2026-08-29). It used to render one static
 * sentence from `describe(plan)` — "Planning steps and searching the web" —
 * and then never change. In Max that line can sit there for minutes: measured
 * on this deployment, a 23,520-character paste took 213 s before the first
 * step event arrived, because Max runs several full passes of a 35B model
 * before it has anything to show. A frozen sentence for three and a half
 * minutes reads as a hung app.
 *
 * What it does NOT do is invent progress. The backend sends no events during
 * that gap, so pretending to narrate sub-steps would be fiction. What is
 * true and useful is the clock: elapsed time, ticking every second, plus one
 * honest note once the wait gets long enough to look broken.
 */

import { useEffect, useRef, useState } from 'react';
import { Loader } from './Loader';

/** After this many seconds, say why it is taking so long. */
const EXPLAIN_AFTER_S = 25;

function formatWait(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}m ${s}s`;
}

export function LiveStatus({
  text,
  /** Max runs several model passes, so its waits are the long ones. */
  effortNote,
}: {
  text: string;
  effortNote?: string;
}) {
  const startedAt = useRef(Date.now());
  const [seconds, setSeconds] = useState(0);

  // A new phase restarts the clock: the number should describe THIS stage,
  // not the whole turn, or it stops meaning anything.
  useEffect(() => {
    startedAt.current = Date.now();
    setSeconds(0);
  }, [text]);

  useEffect(() => {
    const tick = () =>
      setSeconds(Math.max(0, Math.round((Date.now() - startedAt.current) / 1000)));
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, []);

  const note = seconds >= EXPLAIN_AFTER_S ? effortNote : undefined;

  return (
    <div className="mb-2 flex items-start gap-2.5 text-sm text-muted">
      <span className="mt-[1px] shrink-0">
        <Loader size={22} />
      </span>
      <span className="min-w-0">
        <span>{text}</span>
        {seconds > 0 && (
          <span className="ml-1.5 tabular-nums text-faint">
            {formatWait(seconds)}
          </span>
        )}
        {note && (
          <span className="mt-0.5 block text-[12.5px] leading-snug text-faint">
            {note}
          </span>
        )}
      </span>
    </div>
  );
}
