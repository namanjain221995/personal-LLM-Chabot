/**
 * "Ask TechSara AI" — the pure half (2026-09-03).
 *
 * Normalisation, the length cap, the model-visible wrapper and the floating
 * action's clamping are all decided without a DOM, so they are tested without
 * one. The DOM half (what counts as a valid selection, and the flow through
 * the app) lives in tests/selection-ask.test.tsx and
 * tests/selected-context-reply.test.tsx.
 */

import { describe, expect, it } from 'vitest';
import {
  ASK_GAP,
  ASK_MARGIN,
  SELECTED_CONTEXT_MAX_CHARS,
  askPlacement,
  foldSelectedContext,
  foldTurnForModel,
  makeSelectedContext,
  normalizeSelectedText,
  previewSelectedText,
} from '@/lib/selectedContext';
import type { SelectedContext } from '@/lib/types';

const ctx = (over: Partial<SelectedContext> = {}): SelectedContext => ({
  text: 'model drift happens when production data changes',
  messageId: 'm1',
  sourceRole: 'assistant',
  ...over,
});

describe('normalizeSelectedText', () => {
  it('trims the whitespace a drag picks up at either end', () => {
    expect(normalizeSelectedText('  \n hello world \n\n ')).toBe('hello world');
  });

  it('KEEPS internal newlines — a selected list is not one line', () => {
    expect(normalizeSelectedText('- one\n- two\n- three')).toBe(
      '- one\n- two\n- three',
    );
  });

  it('drops trailing spaces per line and collapses blank-line runs', () => {
    expect(normalizeSelectedText('a   \n\n\n\nb  ')).toBe('a\n\nb');
  });

  it('normalises CRLF so a Windows selection is not double-spaced', () => {
    expect(normalizeSelectedText('a\r\nb')).toBe('a\nb');
  });

  it('is empty for whitespace-only input', () => {
    for (const raw of ['', '   ', '\n\n', '\t \n ']) {
      expect(normalizeSelectedText(raw)).toBe('');
    }
  });
});

describe('makeSelectedContext', () => {
  it('REPLY-03 · whitespace-only selection produces nothing at all', () => {
    expect(makeSelectedContext('   \n ', 'm1', 'assistant')).toBeNull();
  });

  it('refuses a selection with no message behind it', () => {
    expect(makeSelectedContext('real text', '', 'assistant')).toBeNull();
  });

  it('keeps the role it was captured from', () => {
    expect(makeSelectedContext('hi', 'm2', 'user')).toEqual({
      text: 'hi',
      messageId: 'm2',
      sourceRole: 'user',
    });
  });

  it('caps a huge selection and SAYS it capped it', () => {
    const long = 'x'.repeat(SELECTED_CONTEXT_MAX_CHARS + 500);
    const made = makeSelectedContext(long, 'm1', 'assistant')!;
    expect(made.text.length).toBe(SELECTED_CONTEXT_MAX_CHARS);
    expect(made.truncated).toBe(true);
  });

  it('does not mark an exactly-at-the-cap selection as truncated', () => {
    const exact = 'y'.repeat(SELECTED_CONTEXT_MAX_CHARS);
    expect(makeSelectedContext(exact, 'm1', 'assistant')!.truncated).toBeUndefined();
  });
});

describe('previewSelectedText', () => {
  it('flattens to one line for the card', () => {
    expect(previewSelectedText('a\nb\n\nc')).toBe('a b c');
  });

  it('elides past the limit', () => {
    expect(previewSelectedText('abcdefghij', 4)).toBe('abcd…');
  });

  it('leaves a short excerpt exactly as it is', () => {
    expect(previewSelectedText('short', 40)).toBe('short');
  });
});

describe('foldSelectedContext · what the model actually receives', () => {
  it('REPLY-11 · no quote means byte-identical content', () => {
    expect(foldSelectedContext('Why does this happen?', null)).toBe(
      'Why does this happen?',
    );
    expect(foldSelectedContext('unchanged', undefined)).toBe('unchanged');
  });

  it('REPLY-12 · the quote and the follow-up both travel, and are separable', () => {
    const out = foldSelectedContext('Why does this happen?', ctx());
    expect(out).toBe(
      'Selected context from a previous assistant message:\n' +
        '\n' +
        '> model drift happens when production data changes\n' +
        '\n' +
        'User follow-up:\n' +
        '\n' +
        'Why does this happen?',
    );
  });

  it('names the right speaker for a quoted user message', () => {
    expect(foldSelectedContext('q', ctx({ sourceRole: 'user' }))).toContain(
      'from a previous user message',
    );
  });

  it('quotes every line, blank ones included, so the block has one boundary', () => {
    const out = foldSelectedContext('q', ctx({ text: 'one\n\ntwo' }));
    expect(out).toContain('> one\n>\n> two');
  });

  it('a capped excerpt says so INSIDE the prompt, not only in the UI', () => {
    expect(foldSelectedContext('q', ctx({ truncated: true }))).toContain(
      '[…excerpt truncated]',
    );
  });

  it('omits the follow-up heading when the turn carried no text', () => {
    const out = foldSelectedContext('', ctx());
    expect(out).toContain('> model drift');
    expect(out).not.toContain('User follow-up');
  });

  it('a quote of pure whitespace is ignored rather than emitted empty', () => {
    expect(foldSelectedContext('q', ctx({ text: '   ' }))).toBe('q');
  });
});

describe('foldTurnForModel · the one place a stored turn becomes model text', () => {
  it('leaves an ordinary turn exactly as it was', () => {
    expect(foldTurnForModel({ content: 'hello' })).toBe('hello');
  });

  it('still folds pasted blocks when there is no quote', () => {
    const out = foldTurnForModel({
      content: 'summarize',
      meta: { pasted: [{ id: 'p1', content: 'BLOCK', lines: 1, chars: 5 }] },
    });
    expect(out).toBe('BLOCK\n\nsummarize');
  });

  it('orders a turn that has BOTH: paste, then question, wrapped by the quote', () => {
    const out = foldTurnForModel({
      content: 'why?',
      meta: {
        pasted: [{ id: 'p1', content: 'LOG', lines: 1, chars: 3 }],
        selected_context: ctx(),
      },
    });
    expect(out).toBe(
      'Selected context from a previous assistant message:\n' +
        '\n' +
        '> model drift happens when production data changes\n' +
        '\n' +
        'User follow-up:\n' +
        '\n' +
        'LOG\n' +
        '\n' +
        'why?',
    );
  });

  it('REPLY-24 · folding twice cannot duplicate the quote', () => {
    // This is the property that makes edit and regenerate safe: they re-send
    // the STORED message, and the stored message's `content` never contained
    // the wrapper, so replaying it produces the same string every time.
    const turn = { content: 'why?', meta: { selected_context: ctx() } };
    const once = foldTurnForModel(turn);
    expect(foldTurnForModel(turn)).toBe(once);
    expect(once.match(/Selected context from/g)).toHaveLength(1);
    expect(turn.content).toBe('why?');
  });
});

describe('askPlacement · the floating action stays on screen', () => {
  const size = { width: 150, height: 30 };
  const viewport = { width: 1000, height: 800 };

  it('sits above the selection and centred on it', () => {
    const p = askPlacement(
      { top: 400, bottom: 420, left: 300, right: 500 },
      size,
      viewport,
    );
    expect(p.side).toBe('above');
    expect(p.top).toBe(400 - size.height - ASK_GAP);
    expect(p.left).toBe(400 - size.width / 2);
  });

  it('flips below when the selection is against the top edge', () => {
    const p = askPlacement({ top: 2, bottom: 24, left: 300, right: 500 }, size, viewport);
    expect(p.side).toBe('below');
    expect(p.top).toBe(24 + ASK_GAP);
  });

  it('stays above when BOTH edges are tight rather than falling off the bottom', () => {
    const p = askPlacement(
      { top: 10, bottom: 780, left: 300, right: 500 },
      size,
      { width: 1000, height: 800 },
    );
    expect(p.side).toBe('above');
    expect(p.top).toBeGreaterThanOrEqual(ASK_MARGIN);
    expect(p.top + size.height).toBeLessThanOrEqual(800 - ASK_MARGIN);
  });

  it('clamps to the left edge for a selection at the far left', () => {
    const p = askPlacement({ top: 300, bottom: 320, left: 0, right: 20 }, size, viewport);
    expect(p.left).toBe(ASK_MARGIN);
  });

  it('clamps to the right edge for a selection at the far right', () => {
    const p = askPlacement(
      { top: 300, bottom: 320, left: 980, right: 1000 },
      size,
      viewport,
    );
    expect(p.left).toBe(viewport.width - size.width - ASK_MARGIN);
  });

  it('pins the LEFT edge on a viewport narrower than the action itself', () => {
    // A right-clamp here would push the label off the left of a phone and
    // leave nothing readable to tap.
    const p = askPlacement(
      { top: 300, bottom: 320, left: 10, right: 100 },
      size,
      { width: 120, height: 600 },
    );
    expect(p.left).toBe(ASK_MARGIN);
  });
});
