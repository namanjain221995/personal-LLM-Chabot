/**
 * Client for the Salesforce Intelligence endpoints.
 *
 * Everything here is best-effort and non-throwing. A starter card that fails to
 * load is a missing nicety; a chat that will not open because of one is a
 * broken product. The one thing that MUST be right is the pending
 * clarification, and that is validated rather than trusted — see
 * `parseClarification`.
 */

import { parseClarification, type ClarificationRequest } from './clarification';

/** One suggestion on the starter card. */
export interface StarterOption {
  id: string;
  label: string;
  description: string;
  /** The message sent when it is picked. */
  prompt: string;
}

export interface SalesforceContext {
  enabled: boolean;
  options: StarterOption[];
  pendingClarification: ClarificationRequest | null;
}

export const EMPTY_CONTEXT: SalesforceContext = {
  enabled: false,
  options: [],
  pendingClarification: null,
};

function asOption(raw: unknown): StarterOption | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const id = typeof o.id === 'string' ? o.id : '';
  const label = typeof o.label === 'string' ? o.label : '';
  const prompt = typeof o.prompt === 'string' ? o.prompt : '';
  if (!id || !label || !prompt) return null;
  return {
    id,
    label,
    description: typeof o.description === 'string' ? o.description : '',
    prompt,
  };
}

/** Parse the endpoint's payload. Exported so the shape is unit-tested. */
export function parseSalesforceContext(raw: unknown): SalesforceContext {
  if (!raw || typeof raw !== 'object') return EMPTY_CONTEXT;
  const body = raw as Record<string, unknown>;
  const options = Array.isArray(body.options)
    ? body.options.map(asOption).filter((o): o is StarterOption => o !== null)
    : [];
  return {
    enabled: body.enabled === true,
    options,
    pendingClarification: parseClarification(body.pending_clarification),
  };
}

/**
 * Should the starter card be on screen?
 *
 * It is a STARTER: a one-time suggestion strip for an empty thread. It was
 * rendering under every answer and beneath the streaming indicator, which made
 * it read as permanent furniture bolted to the composer rather than a prompt to
 * get going (owner report, 2026-08-11).
 *
 * Pure, so the rule is unit-tested rather than eyeballed in a screenshot.
 */
export function shouldShowStarter(context: {
  salesforceEnabled: boolean;
  messageCount: number;
  streaming: boolean;
  hasPendingClarification: boolean;
  optionCount: number;
}): boolean {
  return (
    context.salesforceEnabled &&
    context.messageCount === 0 &&
    !context.streaming &&
    // A waiting question is the thing to answer; suggestions beside it compete
    // with it for the same click.
    !context.hasPendingClarification &&
    context.optionCount > 0
  );
}

/** Fetch the starter options and any pending question for a conversation. */
export async function fetchSalesforceContext(
  conversationId: string,
): Promise<SalesforceContext> {
  try {
    const res = await fetch(
      `/api/chat/salesforce/${encodeURIComponent(conversationId)}`,
      { cache: 'no-store' },
    );
    if (!res.ok) return EMPTY_CONTEXT;
    return parseSalesforceContext(await res.json());
  } catch {
    return EMPTY_CONTEXT;
  }
}

/**
 * Cancel the pending question server-side.
 *
 * Fire-and-forget, but NOT optional: the card vanishing from the screen is not
 * what makes the question go away — this is. Without it the server would still
 * be waiting, and the next Salesforce message in the chat would be read as an
 * answer to a question the user had dismissed.
 */
export async function cancelClarification(
  conversationId: string,
): Promise<void> {
  try {
    await fetch('/api/chat/salesforce/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: conversationId }),
    });
  } catch {
    // Offline. The server-side question stays pending, and the next send is
    // classified against it — which is the same code path a typed answer takes,
    // so a genuinely new topic still cancels it correctly.
  }
}
