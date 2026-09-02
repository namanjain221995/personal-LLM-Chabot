/**
 * Compaction — the CLIENT half, and the one rule it exists to enforce:
 * never claim a compaction succeeded unless a summary can actually be seen.
 *
 * `POST /chat/compact` answers `{compacted: true, folded_turns: N}` as soon as
 * the server advanced its fold boundary — including when the summarizer
 * returned nothing at all and an EMPTY summary was stored against those turns.
 * The UI then said "Compacted 5 earlier messages into the summary" and offered
 * a link to read a summary that did not exist. Neither statement was true, and
 * the browser has no way to repair the conversation once that has happened.
 *
 * What it CAN do is stop asserting it. So a claimed compaction is confirmed
 * against the read-only summary endpoint the context ring already calls, and
 * anything that cannot be confirmed is reported as unverified — never as
 * success, and never as "your messages are all still there", which is equally
 * unknowable from here.
 *
 * Everything below is deliberately free of React so the wording and the
 * decisions are testable in node, the way the rest of lib/ is.
 */

/** How the browser resolved one press of the context ring. */
export type CompactOutcome =
  | { kind: 'compacted'; foldedTurns: number; message: string; tone: 'info' }
  | { kind: 'nothing'; message: string; tone: 'info' }
  | { kind: 'unverified'; message: string; tone: 'error' }
  | { kind: 'failed'; message: string; tone: 'error' };

export interface CompactRun {
  outcome: CompactOutcome;
  /**
   * The server's post-compaction foldable count, or null when it could not be
   * read. Null means UNKNOWN, which deliberately leaves the button enabled —
   * a server that cannot answer must not disable a control that still works.
   */
  foldableTurns: number | null;
}

/* -------------------------------------------------------------------------
 * Copy. All of it lives here so it is asserted in node rather than scraped
 * out of a rendered DOM, and so no server string is ever echoed at the user.
 * ---------------------------------------------------------------------- */

/** Nothing older existed to fold. Not an error. */
export const NOTHING_FOLDED = 'Nothing to compact yet.';

/**
 * The server said it compacted, and the browser could not see a summary to
 * show for it.
 *
 * Says only what is actually known. "No usable summary was RETURNED" is what
 * the browser observed — it read the summary endpoint and found nothing
 * usable; whether the server produced one and lost it is not visible from
 * here. The transcript claim is safe for the same kind of reason: compaction
 * never edits `messages`, so "your visible conversation has not been changed"
 * is checkable (CV-08) rather than reassuring.
 *
 * It deliberately does NOT say the model can still see those turns, or that
 * nothing was lost. The confirmed H-10 server defect means older turns CAN be
 * dropped with an empty summary stored against them, and no amount of client
 * code can see that, let alone repair it.
 */
export const COMPACT_UNVERIFIED =
  'Compaction could not be verified. No usable summary was returned. Your ' +
  'visible conversation has not been changed.';

/** The request itself did not complete. */
export const COMPACT_FAILED = 'Could not compact this conversation.';

/** The SummaryPanel's empty state (see `usableSummary`). */
export const NO_USABLE_SUMMARY =
  'No compact summary is available for this conversation.';

/** "1 earlier message" / "12 earlier messages". */
function earlier(count: number): string {
  return `${count} earlier message${count === 1 ? '' : 's'}`;
}

/* ---------------------------------------------------------------- reading */

/**
 * The summary text on a server payload, or null when there is nothing usable.
 *
 * Whitespace counts as nothing: an empty summary and a summary of `"\n "` are
 * the same fact, and the second one is what slips past a bare truthiness
 * check. This is the single definition of "there is a summary" — the panel
 * and the success toast both read it, so they cannot disagree.
 */
export function usableSummary(body: unknown): string | null {
  if (!body || typeof body !== 'object') return null;
  const raw = (body as Record<string, unknown>).summary;
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim();
  return trimmed ? trimmed : null;
}

/* --------------------------------------------------------------- deciding */

export interface CompactOutcomeInput {
  /** What `POST /chat/compact` reported. */
  compacted?: boolean;
  foldedTurns?: number | null;
  /**
   * Did a follow-up read of the summary endpoint find a usable summary?
   * `true` confirmed · `false` proven empty · `null` could not be checked.
   *
   * `null` is treated exactly like `false` on purpose. "I could not check"
   * is not evidence of success, and this control has already shipped one
   * false success; the safe direction is to under-claim.
   */
  summaryVerified: boolean | null;
}

/**
 * What the user is told about one press.
 *
 * The `reason` string the server sends is NOT echoed: it is internal wording
 * ("nothing older to summarize") and the set of reasons is fixed, so it is
 * mapped to product copy here instead of surfacing whatever arrives.
 */
export function compactOutcome({
  compacted,
  foldedTurns,
  summaryVerified,
}: CompactOutcomeInput): CompactOutcome {
  if (!compacted) {
    return { kind: 'nothing', message: NOTHING_FOLDED, tone: 'info' };
  }
  if (summaryVerified !== true) {
    return { kind: 'unverified', message: COMPACT_UNVERIFIED, tone: 'error' };
  }
  // A verified summary with an unusable count still succeeded — the summary is
  // the thing that was checked. Fall back to a countless sentence rather than
  // inventing "0 earlier messages".
  const folded =
    typeof foldedTurns === 'number' && Number.isFinite(foldedTurns)
      ? Math.max(0, Math.floor(foldedTurns))
      : 0;
  return {
    kind: 'compacted',
    foldedTurns: folded,
    tone: 'info',
    message: folded
      ? `Compacted ${earlier(folded)} into the summary.`
      : 'Compacted this conversation into the summary.',
  };
}

/* --------------------------------------------------------------- running */

/**
 * Conversations with a compaction in flight.
 *
 * Module state rather than a component ref on purpose: the guard has to
 * survive a re-render, a re-mount and a trip to another chat and back, and
 * "at most one compaction per conversation" is a property of the CONVERSATION,
 * not of whichever control happens to be on screen. `finally` always clears
 * it, so a failed request cannot wedge the control.
 */
const inFlight = new Set<string>();

/** Is a compaction already running for this conversation? */
export function isCompacting(conversationId: string): boolean {
  return inFlight.has(conversationId);
}

interface CompactBody {
  compacted?: boolean;
  folded_turns?: number;
  foldable_turns?: number;
  total_turns?: number;
  reason?: string;
}

function foldableOf(body: unknown): number | null {
  if (!body || typeof body !== 'object') return null;
  const raw = (body as Record<string, unknown>).foldable_turns;
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw < 0) return null;
  return Math.floor(raw);
}

/**
 * Compact one conversation, then confirm the result before believing it.
 *
 * Returns null when a compaction for this conversation is ALREADY running —
 * the caller should do nothing at all, not show an error. One press is one
 * request; a second press while the first is in flight is not a second
 * request, which is what the disabled button already tries to guarantee and
 * what this makes true even if a click slips past it.
 *
 * Uses only endpoints that already exist: the compact POST and the read-only
 * summary GET the meter popover calls when it opens.
 */
export async function requestCompact(
  conversationId: string,
  messages: readonly { role: string; content: string }[],
): Promise<CompactRun | null> {
  if (inFlight.has(conversationId)) return null;
  inFlight.add(conversationId);
  try {
    const res = await fetch('/api/chat/compact', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: conversationId,
        messages: messages.map((m) => ({ role: m.role, content: m.content })),
      }),
    });
    // A non-2xx never becomes a success sentence, and its body is never shown:
    // the proxy's own failure text ("orchestrator unreachable") is an internal
    // detail, and the real payload is already written to the server log.
    if (!res.ok) {
      return {
        outcome: { kind: 'failed', message: COMPACT_FAILED, tone: 'error' },
        foldableTurns: null,
      };
    }
    const body = (await res.json()) as CompactBody;

    // Confirm the claim. Only a claimed compaction is worth a second call —
    // "nothing was folded" has nothing to verify.
    let summaryVerified: boolean | null = null;
    let foldable = foldableOf(body);
    if (body.compacted) {
      try {
        const check = await fetch(
          `/api/history/conversations/${encodeURIComponent(
            conversationId,
          )}/summary`,
          { cache: 'no-store' },
        );
        if (check.ok) {
          const checked = (await check.json()) as unknown;
          summaryVerified = usableSummary(checked) !== null;
          // The same read carries the fresh foldable count, so the popover is
          // reconciled from server truth without a third round trip.
          const fresh = foldableOf(checked);
          if (fresh !== null) foldable = fresh;
        }
      } catch {
        summaryVerified = null; // unknown → reported as unverified
      }
    }

    return {
      outcome: compactOutcome({
        compacted: body.compacted,
        foldedTurns: body.folded_turns,
        summaryVerified,
      }),
      foldableTurns: foldable,
    };
  } catch {
    return {
      outcome: { kind: 'failed', message: COMPACT_FAILED, tone: 'error' },
      foldableTurns: null,
    };
  } finally {
    inFlight.delete(conversationId);
  }
}
