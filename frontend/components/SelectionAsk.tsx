'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  askPlacement,
  candidateFromSelection,
  type SelectionCandidate,
} from '@/lib/selectedContext';

/** Roughly what the button measures; refined from the real node once mounted. */
const ASSUMED = { width: 148, height: 32 };

/**
 * The floating "Ask TechSara AI" action that appears over a text selection
 * inside a chat message.
 *
 * SUPPLEMENTAL, never in the way. It adds a button next to a selection and
 * changes nothing else: no `preventDefault` on selection, no `user-select`
 * override, no clipboard handling. Ctrl/Cmd+C, drag handles, the context menu
 * and every other native gesture behave exactly as they did before this
 * existed — which is the whole reason detection listens rather than intercepts.
 *
 * WHEN IT RUNS is the performance story. The thread re-renders on every
 * streamed token, so geometry must never be tied to rendering. Two listeners
 * do the work instead:
 *
 *   - `selectionchange` — high frequency (it fires throughout a drag), so it
 *     does the CHEAPEST possible thing: if the selection is gone or collapsed,
 *     drop the candidate. It never measures anything.
 *   - `pointerup` / `keyup` — low frequency and end-of-gesture, so this is
 *     where the range is validated and measured, once.
 *
 * A candidate therefore costs one `getBoundingClientRect` per completed
 * selection gesture, and exactly zero work per token.
 */
export function SelectionAsk({
  candidate,
  onCandidateChange,
  onAsk,
}: {
  candidate: SelectionCandidate | null;
  onCandidateChange: (next: SelectionCandidate | null) => void;
  onAsk: (candidate: SelectionCandidate) => void;
}) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [place, setPlace] = useState<{ top: number; left: number } | null>(null);
  // Read in listeners without making them depend on the current value — a
  // changing dep would tear the listeners down and rebuild them mid-gesture.
  const hasCandidate = useRef(false);
  hasCandidate.current = candidate !== null;

  const measure = useCallback((rect: SelectionCandidate['rect']) => {
    const node = buttonRef.current;
    const size = node
      ? { width: node.offsetWidth || ASSUMED.width, height: node.offsetHeight || ASSUMED.height }
      : ASSUMED;
    const { top, left } = askPlacement(rect, size, {
      width: window.innerWidth,
      height: window.innerHeight,
    });
    setPlace({ top, left });
  }, []);

  useEffect(() => {
    // Settle after the browser has finished updating the selection: reading it
    // synchronously inside pointerup gives the range as it was BEFORE the
    // gesture ended, which on a click-to-clear is the previous selection.
    let raf = 0;
    const evaluate = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        onCandidateChange(
          candidateFromSelection(
            typeof window === 'undefined' ? null : window.getSelection(),
          ),
        );
      });
    };

    const onSelectionChange = () => {
      if (!hasCandidate.current) return;
      const sel = window.getSelection();
      // Cheap guard only. Anything more here would run dozens of times during
      // a single drag-select.
      if (!sel || sel.rangeCount === 0 || sel.isCollapsed) {
        onCandidateChange(null);
      }
    };

    document.addEventListener('selectionchange', onSelectionChange);
    document.addEventListener('pointerup', evaluate);
    document.addEventListener('keyup', evaluate);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener('selectionchange', onSelectionChange);
      document.removeEventListener('pointerup', evaluate);
      document.removeEventListener('keyup', evaluate);
    };
  }, [onCandidateChange]);

  // Reposition while the action is open. Bound ONLY when there is something to
  // position, so scrolling a conversation with no selection costs nothing.
  useEffect(() => {
    if (!candidate) {
      setPlace(null);
      return;
    }
    measure(candidate.rect);
    let raf = 0;
    const follow = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const sel = typeof window === 'undefined' ? null : window.getSelection();
        const live = candidateFromSelection(sel);
        // The selection scrolled out from under itself (its message unmounted,
        // or it was cleared while off screen): drop the action rather than
        // leave it hovering over unrelated text.
        if (!live) {
          onCandidateChange(null);
          return;
        }
        measure(live.rect);
      });
    };
    // Capture: the thread scrolls in its own container, and a scroll event on
    // an inner element does not bubble to window.
    window.addEventListener('scroll', follow, true);
    window.addEventListener('resize', follow);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('scroll', follow, true);
      window.removeEventListener('resize', follow);
    };
  }, [candidate, measure, onCandidateChange]);

  if (!candidate) return null;

  return (
    <div
      className="fixed z-50"
      style={{ top: place?.top ?? candidate.rect.top, left: place?.left ?? candidate.rect.left }}
    >
      <button
        ref={buttonRef}
        type="button"
        // Without this the button's own mousedown collapses the selection
        // before the click lands, and the excerpt is gone by the time it is
        // read. Scoped to this button — nothing global is prevented.
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => onAsk(candidate)}
        aria-label="Ask TechSara AI about the selected text"
        className="inline-flex select-none items-center gap-1.5 rounded-full border border-border bg-surface px-3 py-1.5 text-xs font-medium text-ink shadow-lg transition-colors duration-ts hover:bg-surface-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      >
        <IconQuote />
        Ask TechSara AI
      </button>
    </div>
  );
}

/** A quote mark, sized to sit beside 12px text. Local — nothing else wants it. */
function IconQuote() {
  return (
    <svg
      width={12}
      height={12}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      className="shrink-0 text-accent"
    >
      <path d="M7 7h4v4c0 2.5-1.5 4.5-4 5" />
      <path d="M15 7h4v4c0 2.5-1.5 4.5-4 5" />
    </svg>
  );
}
