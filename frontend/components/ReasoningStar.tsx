'use client';

/**
 * The Salesforce phase indicator: the shared `Loader` artwork plus the label
 * the backend actually reported.
 *
 * The contract is the important part and is unchanged: it is driven ENTIRELY by
 * `meta.status` / the `status` SSE event. There is no timer stepping through
 * plausible-looking phases; if the server has not said it is querying
 * Salesforce, this does not say so either. When the backend stops, so does the
 * indicator.
 */

import { memo } from 'react';
import {
  accessibleStatus,
  isActivePhase,
  starState,
  type PhaseStatus,
  type StarState,
} from '@/lib/phases';
import { Loader } from './Loader';

export { LOADER_POSTER, LOADER_SRC } from './Loader';

/**
 * Playback rate per state. The artwork is one clip, so tempo is the only honest
 * way to distinguish "searching" from "drafting" — and changing rate does not
 * restart the loop, so moving between phases stays continuous.
 */
const RATE: Record<StarState, number> = {
  understanding: 0.8,
  searching: 1.25,
  calculating: 1.15,
  verifying: 0.95,
  drafting: 0.8,
  reconnecting: 0.6,
};

export interface ReasoningStarProps {
  status: PhaseStatus | null | undefined;
  /** `sm` sits inline in an assistant row; `lg` centres an empty response. */
  size?: 'sm' | 'lg';
  /** Hide the text label (the artwork alone, e.g. beside an existing line). */
  hideLabel?: boolean;
  className?: string;
}

export const ReasoningStar = memo(function ReasoningStar({
  status,
  size = 'sm',
  hideLabel = false,
  className = '',
}: ReasoningStarProps) {
  const state = starState(status?.phase);

  // Nothing to show: no status, a terminal phase, or a phase that hands over to
  // the clarification card. Rendering an idle indicator would be a fabricated
  // claim that work is still happening.
  if (!status || !state || !isActivePhase(status.phase)) return null;

  return (
    <div
      className={`flex items-center gap-2.5 ${className}`}
      data-phase={status.phase}
    >
      <Loader size={size === 'lg' ? 40 : 22} rate={RATE[state]} />
      {!hideLabel && <span className="text-sm text-muted">{status.label}</span>}
      {/* One polite live region carries the phase to a screen reader. The
          visible label is decorative for that audience — announcing both would
          read every phase twice. */}
      <span aria-live="polite" aria-atomic="true" className="sr-only">
        {accessibleStatus(status)}
      </span>
    </div>
  );
});
