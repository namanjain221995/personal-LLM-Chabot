/**
 * The client half of the clarification contract.
 *
 * These are the rules that decide whether a click resumes the user's request or
 * throws it away, so they are tested as pure functions rather than through the
 * card: a keyboard map that is only exercised by simulating keystrokes is a
 * keyboard map with untested branches.
 */

import { describe, expect, it } from 'vitest';
import {
  answerSummary,
  buildResponse,
  cardKeyAction,
  cardState,
  clientMessageId,
  composerPlaceholder,
  optionShortcut,
  parseClarification,
  pendingClarification,
  rowCount,
  wrapIndex,
  type ClarificationRequest,
} from '@/lib/clarification';
import type { ChatMessage } from '@/lib/types';

const WIRE = {
  clarification_id: 'clr_1',
  conversation_id: 'conv',
  run_id: 'run',
  root_user_message_id: 'msg',
  intent_id: 'int',
  source: 'salesforce',
  header: 'Salesforce',
  question: 'Which period should I use?',
  slot: 'date_range',
  options: [
    { id: 'm', label: 'This month', value: 'THIS_MONTH' },
    { id: 'q', label: 'This quarter', value: 'THIS_QUARTER' },
    { id: 'y', label: 'This year', value: 'THIS_YEAR' },
  ],
  allow_custom: true,
  custom_placeholder: 'Enter another date range…',
  multi_select: false,
  round_number: 1,
  created_at: '2026-08-11T09:00:00+00:00',
  state: 'pending',
  resume_token: 'tok',
  question_fingerprint: 'fp',
};

function request(): ClarificationRequest {
  return parseClarification(WIRE)!;
}

describe('parsing', () => {
  it('accepts a well-formed payload', () => {
    const parsed = request();
    expect(parsed.question).toBe('Which period should I use?');
    expect(parsed.options).toHaveLength(3);
    expect(parsed.allow_custom).toBe(true);
  });

  it('renders nothing rather than a card whose options cannot be submitted', () => {
    // Every one of these would produce buttons that send ids the server never
    // offered — the user clicks, waits, and is told their answer was invalid.
    expect(parseClarification(null)).toBeNull();
    expect(parseClarification({ ...WIRE, clarification_id: '' })).toBeNull();
    expect(parseClarification({ ...WIRE, resume_token: '' })).toBeNull();
    expect(parseClarification({ ...WIRE, question: '   ' })).toBeNull();
    expect(parseClarification({ ...WIRE, options: [WIRE.options[0]] })).toBeNull();
  });

  it('drops malformed options and refuses what is left if it is not a choice', () => {
    expect(
      parseClarification({ ...WIRE, options: [WIRE.options[0], { label: 'no id' }] }),
    ).toBeNull();
  });

  it('deduplicates option ids rather than rendering two buttons that submit the same thing', () => {
    const parsed = parseClarification({
      ...WIRE,
      options: [WIRE.options[0], WIRE.options[0], WIRE.options[1]],
    });
    expect(parsed!.options.map((o) => o.id)).toEqual(['m', 'q']);
  });

  it('caps the card at four options', () => {
    const parsed = parseClarification({
      ...WIRE,
      options: [
        ...WIRE.options,
        { id: 'a', label: 'A' },
        { id: 'b', label: 'B' },
      ],
    });
    expect(parsed!.options).toHaveLength(4);
  });

  it('falls back a missing value to the label so an option always means something', () => {
    const parsed = parseClarification({
      ...WIRE,
      options: [
        { id: 'a', label: 'This month' },
        { id: 'b', label: 'This year' },
      ],
    });
    expect(parsed!.options[0].value).toBe('This month');
  });

  it('keeps only string metadata, so a nested object cannot reach the DOM', () => {
    const parsed = parseClarification({
      ...WIRE,
      options: [
        { id: 'a', label: 'Acme', metadata: { City: 'Leeds', nested: { x: 1 } } },
        { id: 'b', label: 'Acme Corp' },
      ],
    });
    expect(parsed!.options[0].metadata).toEqual({ City: 'Leeds' });
  });
});

describe('finding the pending question in a thread', () => {
  function message(partial: Partial<ChatMessage>): ChatMessage {
    return {
      id: 'x',
      role: 'assistant',
      content: '',
      createdAt: 0,
      ...partial,
    } as ChatMessage;
  }

  it('reads it off the last assistant message', () => {
    const found = pendingClarification([
      message({ role: 'user', content: 'show my pipeline' }),
      message({ meta: { route: 'clarify', clarification: WIRE } as never }),
    ]);
    expect(found?.clarification_id).toBe('clr_1');
  });

  it('ignores an older card that has already been answered', () => {
    const found = pendingClarification([
      message({ meta: { route: 'clarify', clarification: WIRE } as never }),
      message({ role: 'user', content: 'This quarter' }),
      message({ content: 'Here is your pipeline.', meta: { route: 'sql' } }),
    ]);
    expect(found).toBeNull();
  });

  it('ignores a card the server has already marked answered', () => {
    const found = pendingClarification([
      message({
        meta: { route: 'clarify', clarification: { ...WIRE, state: 'answered' } } as never,
      }),
    ]);
    expect(found).toBeNull();
  });

  it('finds nothing in a thread that never asked', () => {
    expect(pendingClarification([])).toBeNull();
    expect(
      pendingClarification([message({ content: 'Here you go.', meta: { route: 'sql' } })]),
    ).toBeNull();
  });

  // Which card is LIVE. The transcript used to be full of working controls:
  // every message carrying a clarification rendered an interactive card, and
  // the parent's in-flight lock is keyed by id and clears when the run ends —
  // so answering one question re-armed every older one, and clicking a
  // different option on any of them started a fresh run against an intent the
  // conversation had long moved past.
  describe('which card is still live', () => {
    const asked = message({
      id: 'a1',
      meta: { route: 'clarify', clarification: WIRE } as never,
    });

    it('marks the question the thread is waiting on as pending', () => {
      const thread = [message({ role: 'user', content: 'show my pipeline' }), asked];
      expect(cardState(thread, 1)).toEqual({ pending: true, answeredWith: '' });
    });

    it('marks an answered question as history, quoting the answer', () => {
      const thread = [
        asked,
        message({ role: 'user', content: 'This quarter' }),
        message({ content: 'Here is your pipeline.', meta: { route: 'sql' } }),
      ];
      expect(cardState(thread, 0)).toEqual({
        pending: false,
        answeredWith: 'This quarter',
      });
    });

    it('marks EVERY older card as history when a newer one is live', () => {
      const older = message({
        id: 'a0',
        meta: {
          route: 'clarify',
          clarification: { ...WIRE, clarification_id: 'clr_0' },
        } as never,
      });
      const thread = [
        older,
        message({ role: 'user', content: 'This quarter' }),
        asked,
      ];
      expect(cardState(thread, 0).pending).toBe(false);
      expect(cardState(thread, 2).pending).toBe(true);
    });

    it('has nothing to say about a message with no card', () => {
      expect(cardState([message({ content: 'hi' })], 0)).toEqual({
        pending: false,
        answeredWith: '',
      });
      expect(cardState([], 5)).toEqual({ pending: false, answeredWith: '' });
    });

    it('reports a skipped question as answered with nothing quoted', () => {
      // A skip carries no text of its own; the parent supplies the wording.
      const thread = [asked, message({ role: 'user', content: '' })];
      expect(cardState(thread, 0)).toEqual({ pending: false, answeredWith: '' });
    });
  });
});

describe('keyboard', () => {
  const context = {
    optionCount: 3,
    allowCustom: true,
    typingCustom: false,
  };

  it('maps number keys to the options in order', () => {
    expect(cardKeyAction({ key: '1' }, context)).toEqual({
      kind: 'select',
      optionId: '0',
    });
    expect(cardKeyAction({ key: '3' }, context)).toEqual({
      kind: 'select',
      optionId: '2',
    });
  });

  it('maps the next number to "Something else"', () => {
    expect(cardKeyAction({ key: '4' }, context)).toEqual({ kind: 'custom' });
  });

  it('does not steal digits while the custom box has focus', () => {
    // Typing "2026" into "which year?" must not select option 2 and submit.
    expect(
      cardKeyAction({ key: '2' }, { ...context, typingCustom: true }),
    ).toBeNull();
  });

  it('moves with the arrow keys in both axes', () => {
    expect(cardKeyAction({ key: 'ArrowDown' }, context)).toEqual({
      kind: 'move',
      delta: 1,
    });
    expect(cardKeyAction({ key: 'ArrowUp' }, context)).toEqual({
      kind: 'move',
      delta: -1,
    });
    expect(cardKeyAction({ key: 'ArrowRight' }, context)).toEqual({
      kind: 'move',
      delta: 1,
    });
  });

  it('confirms on Enter', () => {
    expect(cardKeyAction({ key: 'Enter' }, context)).toEqual({ kind: 'confirm' });
  });

  it('LEAVES for the composer on Escape rather than answering', () => {
    // Wanting to type your own answer is the commonest reason to press it.
    // Submitting "no preference" there would answer on the user's behalf with
    // something they never chose.
    expect(cardKeyAction({ key: 'Escape' }, context)).toEqual({ kind: 'leave' });
  });

  it('hands over to the composer when Enter lands on the "Something else" row', () => {
    // Otherwise Enter there would send an empty answer.
    expect(
      cardKeyAction({ key: 'Enter' }, { ...context, activeIndex: 3 }),
    ).toEqual({ kind: 'custom' });
  });

  // The panel takes focus when a question appears so the number keys work
  // immediately. That is only safe because TYPING still goes where typing
  // goes: without this, answering "the scheduled interview for Dileep" in your
  // own words would lose its first letter, and any digit in what you typed
  // would have picked an option and sent it.
  it('forwards an ordinary keystroke to the composer, carrying the character', () => {
    expect(cardKeyAction({ key: 'a' }, context)).toEqual({
      kind: 'leave',
      text: 'a',
    });
    expect(cardKeyAction({ key: 'D' }, context)).toEqual({
      kind: 'leave',
      text: 'D',
    });
  });

  it('forwards a digit PAST the end of the card instead of eating it', () => {
    // "90 days" starts with a 9. On a three-option card that is text, not a
    // shortcut.
    expect(cardKeyAction({ key: '9' }, context)).toEqual({
      kind: 'leave',
      text: '9',
    });
  });

  it('leaves Space alone, so it still activates the focused option', () => {
    expect(cardKeyAction({ key: ' ' }, context)).toBeNull();
  });

  it('never forwards a navigation or modifier key as text', () => {
    for (const key of ['Tab', 'Shift', 'ArrowUp', 'Backspace', 'F5']) {
      const action = cardKeyAction({ key }, context);
      expect(action?.kind).not.toBe('leave');
    }
  });

  it('TOGGLES rather than submits when several answers are allowed', () => {
    const multi = { ...context, multiSelect: true };
    expect(cardKeyAction({ key: '2' }, multi)).toEqual({
      kind: 'toggle',
      optionId: '1',
    });
    // …and a single-answer card still submits straight away.
    expect(cardKeyAction({ key: '2' }, context)).toEqual({
      kind: 'select',
      optionId: '1',
    });
  });

  it('sends on Cmd/Ctrl+Enter from anywhere, including the text field', () => {
    expect(cardKeyAction({ key: 'Enter', metaKey: true }, context)).toEqual({
      kind: 'confirm',
    });
    expect(
      cardKeyAction({ key: 'Enter', ctrlKey: true }, { ...context, typingCustom: true }),
    ).toEqual({ kind: 'confirm' });
  });

  it('counts the "Something else" row as part of the arrow loop', () => {
    expect(rowCount(3, true)).toBe(4);
    expect(rowCount(3, false)).toBe(3);
  });

  it('never fires on a browser or OS shortcut', () => {
    expect(cardKeyAction({ key: '1', metaKey: true }, context)).toBeNull();
    expect(cardKeyAction({ key: 'ArrowDown', ctrlKey: true }, context)).toBeNull();
  });

  it('cycles rather than dead-ending at either edge', () => {
    expect(wrapIndex(0, -1, 3)).toBe(2);
    expect(wrapIndex(2, 1, 3)).toBe(0);
    expect(wrapIndex(0, 1, 0)).toBe(0);
  });

  it('offers a shortcut hint only where a single key is unambiguous', () => {
    expect(optionShortcut(0)).toBe('1');
    expect(optionShortcut(8)).toBe('9');
    expect(optionShortcut(9)).toBeNull();
  });
});

describe('submission', () => {
  it('builds a response from a selected option', () => {
    const response = buildResponse(request(), { optionIds: ['q'] })!;
    expect(response.selected_option_ids).toEqual(['q']);
    expect(response.resume_token).toBe('tok');
    expect(response.clarification_id).toBe('clr_1');
  });

  it('builds a response from custom text', () => {
    const response = buildResponse(request(), { customText: ' last 90 days ' })!;
    expect(response.custom_text).toBe('last 90 days');
  });

  it('refuses a selection that says nothing', () => {
    expect(buildResponse(request(), {})).toBeNull();
    expect(buildResponse(request(), { customText: '   ' })).toBeNull();
  });

  it('drops an option id the card never offered', () => {
    expect(buildResponse(request(), { optionIds: ['nope'] })).toBeNull();
  });

  it('refuses several answers to a single-select question', () => {
    expect(buildResponse(request(), { optionIds: ['m', 'q'] })).toBeNull();
  });

  it('accepts a skip with nothing else', () => {
    const response = buildResponse(request(), { skipped: true })!;
    expect(response.skipped).toBe(true);
  });

  it('derives the SAME idempotency key for the same answer', () => {
    // A double-click, a retried fetch and a reconnect must all be recognised
    // as one submission — so the key comes from WHAT was answered, never from
    // a clock or a random value.
    const first = buildResponse(request(), { optionIds: ['q'] })!;
    const second = buildResponse(request(), { optionIds: ['q'] })!;
    expect(first.client_message_id).toBe(second.client_message_id);
  });

  it('derives a different key for a different answer', () => {
    expect(clientMessageId('clr_1', { optionIds: ['q'] })).not.toBe(
      clientMessageId('clr_1', { optionIds: ['y'] }),
    );
    expect(clientMessageId('clr_1', { customText: 'a' })).not.toBe(
      clientMessageId('clr_1', { customText: 'b' }),
    );
  });
});

describe('presentation', () => {
  it('summarises the answer for the transcript', () => {
    const req = request();
    expect(
      answerSummary(req, buildResponse(req, { optionIds: ['q'] })!),
    ).toBe('This quarter');
    expect(
      answerSummary(req, buildResponse(req, { customText: 'last 90 days' })!),
    ).toBe('last 90 days');
    expect(answerSummary(req, buildResponse(req, { skipped: true })!)).toContain(
      'No preference',
    );
  });

  it('adopts the question-specific composer placeholder', () => {
    expect(composerPlaceholder(request(), 'Ask anything…')).toBe(
      'Enter another date range…',
    );
    expect(composerPlaceholder(null, 'Ask anything…')).toBe('Ask anything…');
  });
});

describe('the idempotency key survives a long typed answer', () => {
  // The raw text used to be embedded in the id, and the server caps
  // client_message_id at 80 characters — so any typed answer longer than
  // ~37 chars was REJECTED as malformed and the user was told their answer
  // could not be read.
  it('stays under the server cap however much the user types', () => {
    const id = clientMessageId('clr_' + 'a'.repeat(32), {
      customText:
        'as i have given the name Please check that and use exactly those five people',
    });
    expect(id.length).toBeLessThanOrEqual(80);
  });

  it('is still deterministic and still distinguishes answers', () => {
    const cid = 'clr_' + 'b'.repeat(32);
    const a1 = clientMessageId(cid, { customText: 'use the names I gave' });
    const a2 = clientMessageId(cid, { customText: 'use the names I gave' });
    const b = clientMessageId(cid, { customText: 'actually just Jayesh' });
    expect(a1).toBe(a2);
    expect(a1).not.toBe(b);
    expect(clientMessageId(cid, { skipped: true })).not.toBe(a1);
  });
});
