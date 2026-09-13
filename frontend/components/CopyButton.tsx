'use client';

import { useEffect, useRef, useState } from 'react';
import { IconCheck, IconCopy } from './icons';

/** How long the "Copied" confirmation stays, per press. */
const CONFIRM_MS = 1600;

export function CopyButton({
  text,
  label,
  className = '',
  variant = 'chip',
}: {
  text: string;
  label: string;
  className?: string;
  /** "icon": ghost icon-only button for the message action row. */
  variant?: 'chip' | 'icon';
}) {
  const [copied, setCopied] = useState(false);
  /** The live reset timer, so a second press can own it instead of racing it. */
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Nothing may fire into an unmounted button: the confirmation outlives a
  // message that is regenerated, edited, or scrolled out of a switched chat.
  useEffect(() => {
    return () => {
      if (resetTimer.current !== null) clearTimeout(resetTimer.current);
    };
  }, []);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard API unavailable (non-secure context) — fallback.
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    setCopied(true);
    // Each press OWNS the confirmation window. The previous timer used to be
    // left running, so copying twice inside CONFIRM_MS let the FIRST press's
    // timer clear the SECOND press's tick — the confirmation vanished after a
    // few hundred ms instead of its full duration.
    if (resetTimer.current !== null) clearTimeout(resetTimer.current);
    resetTimer.current = setTimeout(() => {
      resetTimer.current = null;
      setCopied(false);
    }, CONFIRM_MS);
  }

  if (variant === 'icon') {
    return (
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? 'Copied' : label}
        title={copied ? 'Copied' : label}
        className={`rounded-lg p-2 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink ${className}`}
      >
        {copied ? (
          <IconCheck size={18} className="text-accent" />
        ) : (
          <IconCopy size={18} />
        )}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={copied ? 'Copied' : label}
      title={copied ? 'Copied' : label}
      // A 30 px chip is under the 32 px tap floor: on a phone (max-sm) and on
      // any touch screen wider than one (pointer:coarse — a tablet reading
      // the docs), it is 32 px tall. A mouse keeps the compact chip.
      className={`inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs max-sm:min-h-8 [@media(pointer:coarse)]:min-h-8 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink ${className}`}
    >
      {copied ? (
        <IconCheck size={13} className="text-accent" />
      ) : (
        <IconCopy size={13} />
      )}
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}
