/**
 * Salesforce mode's client-side contracts: the starter-card payload, the chat
 * proxy mapping for a clarification answer, and the stream manager's dedupe.
 *
 * These are the seams where a resume actually breaks — a proxy that reshapes
 * the response, a starter card that offers an object nobody can query, or a
 * retried fetch that generates a second answer.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import {
  EMPTY_CONTEXT,
  parseSalesforceContext,
  shouldShowStarter,
} from '@/lib/salesforceApi';
import { toOrchestratorChatRequest } from '@/lib/orchestrator';
import {
  clarificationAlreadySubmitted,
  markClarificationSubmitted,
} from '@/lib/streams';
import { foldStreamState } from '@/lib/sse';

describe('the starter-card payload', () => {
  it('parses options the server supplied', () => {
    const context = parseSalesforceContext({
      enabled: true,
      options: [
        {
          id: 'pipeline',
          label: 'Analyze opportunities or pipeline',
          description: 'Open pipeline, stages and amounts.',
          prompt: 'Show my open pipeline',
        },
      ],
      pending_clarification: null,
    });
    expect(context.enabled).toBe(true);
    expect(context.options[0].prompt).toBe('Show my open pipeline');
    expect(context.pendingClarification).toBeNull();
  });

  it('drops an option that would send nothing', () => {
    // A chip with no prompt is a button that does nothing when clicked.
    const context = parseSalesforceContext({
      enabled: true,
      options: [{ id: 'x', label: 'Find a record' }],
    });
    expect(context.options).toEqual([]);
  });

  it('degrades to nothing rather than throwing on a bad payload', () => {
    expect(parseSalesforceContext(null)).toEqual(EMPTY_CONTEXT);
    expect(parseSalesforceContext('nope')).toEqual(EMPTY_CONTEXT);
    expect(parseSalesforceContext({ options: 'not-a-list' }).options).toEqual([]);
  });

  it('validates a restored pending question the same way a live one is', () => {
    const context = parseSalesforceContext({
      enabled: true,
      options: [],
      pending_clarification: {
        clarification_id: 'clr_1',
        conversation_id: 'conv',
        run_id: 'r',
        root_user_message_id: 'm',
        intent_id: 'i',
        source: 'salesforce',
        header: 'Salesforce',
        question: 'Which period?',
        slot: 'date_range',
        options: [
          { id: 'a', label: 'This month' },
          { id: 'b', label: 'This quarter' },
        ],
        allow_custom: true,
        custom_placeholder: '',
        multi_select: false,
        round_number: 1,
        created_at: '',
        state: 'pending',
        resume_token: 'tok',
        question_fingerprint: 'fp',
      },
    });
    expect(context.pendingClarification?.question).toBe('Which period?');
  });

  it('refuses a restored question that could not be answered', () => {
    const context = parseSalesforceContext({
      enabled: true,
      options: [],
      pending_clarification: { clarification_id: 'clr_1', question: 'Which?' },
    });
    expect(context.pendingClarification).toBeNull();
  });
});

describe('the chat proxy', () => {
  it('forwards a clarification answer verbatim', () => {
    // It is a server-issued contract: ids, an opaque resume token and an
    // idempotency key. A proxy that reshaped any of it would break the resume.
    const clarification = {
      clarification_id: 'clr_1',
      conversation_id: 'conv',
      client_message_id: 'clr-clr_1|q||',
      selected_option_ids: ['q'],
      custom_text: '',
      skipped: false,
      resume_token: 'tok',
    };
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'This quarter' }],
      conversation_id: 'conv',
      mode: 'salesforce',
      clarification,
    });
    expect(out?.clarification).toEqual(clarification);
  });

  it('accepts a skip that carries no text of its own', () => {
    const out = toOrchestratorChatRequest({
      conversation_id: 'conv',
      clarification: { clarification_id: 'clr_1', skipped: true },
    });
    expect(out).not.toBeNull();
    expect(out?.message).toBe('');
  });

  it('still refuses a body with nothing in it at all', () => {
    expect(toOrchestratorChatRequest({ messages: [] })).toBeNull();
  });

  it('leaves a v1-shaped request byte-identical', () => {
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'hello' }],
      session_id: 's',
    });
    expect(out).toEqual({
      message: 'hello',
      messages: [{ role: 'user', content: 'hello' }],
      session_id: 's',
      image_base64: null,
    });
  });
});

describe('submission dedupe', () => {
  beforeEach(() => {
    markClarificationSubmitted('warm-up');
  });

  it('recognises a repeat of the same answer', () => {
    const key = `clr-${Math.random()}`;
    expect(clarificationAlreadySubmitted(key)).toBe(false);
    markClarificationSubmitted(key);
    expect(clarificationAlreadySubmitted(key)).toBe(true);
  });

  it('does not confuse two different answers', () => {
    markClarificationSubmitted('clr-a|q||');
    expect(clarificationAlreadySubmitted('clr-a|y||')).toBe(false);
  });
});

describe('folding the live phase onto the persisted meta', () => {
  it('keeps the last phase so a reopened chat shows how the answer was reached', () => {
    const folded = foldStreamState(
      { route: 'sql' },
      { phaseStatus: { phase: 'drafting_answer', label: 'Preparing the answer' } },
    );
    expect(folded.status?.phase).toBe('drafting_answer');
  });

  it("lets the server's own final status win", () => {
    // The server knows whether the run completed or failed; the client only
    // ever saw the labels go past.
    const folded = foldStreamState(
      { route: 'sql', status: { phase: 'completed', label: 'Done' } },
      { phaseStatus: { phase: 'drafting_answer', label: 'Preparing the answer' } },
    );
    expect(folded.status?.phase).toBe('completed');
  });

  it('leaves a meta with no phase alone', () => {
    expect(foldStreamState({ route: 'chat' }, {}).status).toBeUndefined();
  });
});

describe('when the starter card is on screen', () => {
  // Owner report 2026-08-11: it sat under every answer and beneath the
  // streaming indicator, so a one-time suggestion strip read as permanent
  // furniture bolted to the composer.
  const base = {
    salesforceEnabled: true,
    messageCount: 0,
    streaming: false,
    hasPendingClarification: false,
    optionCount: 4,
  };

  it('shows on an empty Salesforce thread', () => {
    expect(shouldShowStarter(base)).toBe(true);
  });

  it('disappears the moment the thread has any message', () => {
    expect(shouldShowStarter({ ...base, messageCount: 1 })).toBe(false);
    expect(shouldShowStarter({ ...base, messageCount: 12 })).toBe(false);
  });

  it('stays away while a reply is streaming', () => {
    expect(shouldShowStarter({ ...base, streaming: true })).toBe(false);
  });

  it('yields to a question that is waiting to be answered', () => {
    expect(
      shouldShowStarter({ ...base, hasPendingClarification: true }),
    ).toBe(false);
  });

  it('is absent with Salesforce off, or with nothing to suggest', () => {
    expect(shouldShowStarter({ ...base, salesforceEnabled: false })).toBe(false);
    expect(shouldShowStarter({ ...base, optionCount: 0 })).toBe(false);
  });
});
