// @vitest-environment jsdom
/**
 * "Ask TechSara AI" — what counts as a selection worth offering it for
 * (2026-09-03).
 *
 * The rules this pins are all REFUSALS, and each one is a way the feature
 * would otherwise misbehave in front of a user: an action floating over the
 * sidebar, over a button label, over the composer's own draft, or — worst —
 * over a selection spanning two turns, where the quote would end up attributed
 * to whichever speaker the code happened to pick first.
 *
 * jsdom has no layout engine, so `Range.getBoundingClientRect` returns zeros
 * and would make every candidate degenerate. It is stubbed with a plausible
 * rectangle; nothing else about the selection is faked, and the real
 * Selection/Range objects do the work.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { candidateFromSelection } from '@/lib/selectedContext';
import { SelectionAsk } from '@/components/SelectionAsk';
import type { SelectionCandidate } from '@/lib/selectedContext';

/**
 * A rect the placement maths can work with.
 *
 * jsdom does not implement `Range.getBoundingClientRect` AT ALL — it is not a
 * zero-valued stub that could be spied on, the method is simply absent — so it
 * is installed on the prototype rather than mocked, and removed again after
 * each test.
 */
let stubbedRect = { top: 300, bottom: 320, left: 200, right: 400, width: 200, height: 20 };

function stubRects() {
  stubbedRect = { top: 300, bottom: 320, left: 200, right: 400, width: 200, height: 20 };
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true,
    writable: true,
    value: () => ({ ...stubbedRect, x: stubbedRect.left, y: stubbedRect.top, toJSON: () => ({}) }),
  });
}

/** Select from the first text node of `a` to the last of `b`. */
function selectAcross(a: Element, b: Element = a) {
  const range = document.createRange();
  range.setStart(a.firstChild ?? a, 0);
  const endNode = b.lastChild ?? b;
  range.setEnd(endNode, endNode.nodeType === 3 ? (endNode.textContent ?? '').length : 0);
  const sel = window.getSelection()!;
  sel.removeAllRanges();
  sel.addRange(range);
  return sel;
}

beforeEach(() => {
  stubRects();
  document.body.innerHTML = '';
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  delete (Range.prototype as Partial<Range>).getBoundingClientRect;
  window.getSelection()?.removeAllRanges();
});

/* ================================================== what may be quoted */

describe('candidateFromSelection', () => {
  function mount(html: string) {
    const host = document.createElement('div');
    host.innerHTML = html;
    document.body.appendChild(host);
    return host;
  }

  it('REPLY-01 · a selection inside an assistant message is a candidate', () => {
    const host = mount(
      '<div data-chat-message-id="a1" data-chat-message-role="assistant"><p>model drift happens</p></div>',
    );
    const sel = selectAcross(host.querySelector('p')!);
    const found = candidateFromSelection(sel)!;
    expect(found).not.toBeNull();
    expect(found.context).toEqual({
      text: 'model drift happens',
      messageId: 'a1',
      sourceRole: 'assistant',
    });
    expect(found.rect.top).toBe(300);
  });

  it('REPLY-02 · a selection inside a user message works too, with its role', () => {
    const host = mount(
      '<div data-chat-message-id="u1" data-chat-message-role="user"><span>what I asked</span></div>',
    );
    const found = candidateFromSelection(selectAcross(host.querySelector('span')!))!;
    expect(found.context.sourceRole).toBe('user');
    expect(found.context.messageId).toBe('u1');
  });

  it('spans several block elements INSIDE one message, keeping the newlines', () => {
    const host = mount(
      '<div data-chat-message-id="a1" data-chat-message-role="assistant">' +
        '<p>first para</p><p>second para</p></div>',
    );
    const paras = host.querySelectorAll('p');
    const found = candidateFromSelection(selectAcross(paras[0], paras[1]))!;
    expect(found.context.messageId).toBe('a1');
    expect(found.context.text).toContain('first para');
    expect(found.context.text).toContain('second para');
  });

  it('REPLY-03 · a whitespace-only selection is not a candidate', () => {
    const host = mount(
      '<div data-chat-message-id="a1" data-chat-message-role="assistant"><p>   </p></div>',
    );
    expect(candidateFromSelection(selectAcross(host.querySelector('p')!))).toBeNull();
  });

  it('REPLY-04 · text outside any message region is not a candidate', () => {
    // Sidebar titles, account names, toolbar labels, button text — none of
    // them carry the marker, and none of them may offer to be quoted.
    const host = mount('<nav><button>New chat</button><span>naman@example.com</span></nav>');
    expect(candidateFromSelection(selectAcross(host.querySelector('button')!))).toBeNull();
    expect(candidateFromSelection(selectAcross(host.querySelector('span')!))).toBeNull();
  });

  it('REPLY-05 · a selection inside a textarea or contenteditable is refused', () => {
    const host = mount(
      '<div data-chat-message-id="a1" data-chat-message-role="assistant">' +
        '<div contenteditable="true"><span>draft text</span></div></div>',
    );
    expect(candidateFromSelection(selectAcross(host.querySelector('span')!))).toBeNull();

    // A real <textarea>'s own selection never produces a document Range over
    // a marked region at all — there is nothing to find.
    const ta = document.createElement('textarea');
    ta.value = 'typed into the composer';
    document.body.appendChild(ta);
    ta.focus();
    ta.setSelectionRange(0, 5);
    expect(candidateFromSelection(window.getSelection())).toBeNull();
  });

  it('REPLY-06 · a selection spanning TWO messages is refused, not guessed', () => {
    const host = mount(
      '<div data-chat-message-id="a1" data-chat-message-role="assistant"><p>first message</p></div>' +
        '<div data-chat-message-id="u2" data-chat-message-role="user"><p>second message</p></div>',
    );
    const paras = host.querySelectorAll('p');
    expect(candidateFromSelection(selectAcross(paras[0], paras[1]))).toBeNull();
  });

  it('a collapsed caret is not a selection', () => {
    const host = mount(
      '<div data-chat-message-id="a1" data-chat-message-role="assistant"><p>text</p></div>',
    );
    const range = document.createRange();
    range.setStart(host.querySelector('p')!.firstChild!, 2);
    range.collapse(true);
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    expect(candidateFromSelection(sel)).toBeNull();
  });

  it('a range that measures nothing is refused rather than positioned at 0,0', () => {
    stubbedRect = { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 };
    const host = mount(
      '<div data-chat-message-id="a1" data-chat-message-role="assistant"><p>text</p></div>',
    );
    expect(candidateFromSelection(selectAcross(host.querySelector('p')!))).toBeNull();
  });

  it('tolerates no selection at all', () => {
    expect(candidateFromSelection(null)).toBeNull();
  });
});

/* ============================================== the floating action itself */

describe('SelectionAsk', () => {
  const candidate: SelectionCandidate = {
    context: { text: 'model drift', messageId: 'a1', sourceRole: 'assistant' },
    rect: { top: 300, bottom: 320, left: 200, right: 400 },
  };

  it('renders nothing without a candidate', () => {
    render(
      <SelectionAsk candidate={null} onCandidateChange={() => {}} onAsk={() => {}} />,
    );
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('is a real, labelled, focusable button — not a clickable div', () => {
    render(
      <SelectionAsk candidate={candidate} onCandidateChange={() => {}} onAsk={() => {}} />,
    );
    const btn = screen.getByRole('button', {
      name: /Ask TechSara AI about the selected text/i,
    });
    expect(btn.tagName).toBe('BUTTON');
    btn.focus();
    expect(document.activeElement).toBe(btn);
  });

  it('hands the whole candidate back when clicked', () => {
    const onAsk = vi.fn();
    render(
      <SelectionAsk candidate={candidate} onCandidateChange={() => {}} onAsk={onAsk} />,
    );
    fireEvent.click(screen.getByRole('button'));
    expect(onAsk).toHaveBeenCalledWith(candidate);
  });

  it('REPLY-19 · its mousedown is defaulted away so the selection survives the click', () => {
    // Without this the button's own mousedown collapses the selection before
    // the click handler ever reads it — the excerpt would be gone. Nothing
    // GLOBAL is prevented: copy, drag handles and the context menu are
    // untouched, which is why the assertion is scoped to this one node.
    render(
      <SelectionAsk candidate={candidate} onCandidateChange={() => {}} onAsk={() => {}} />,
    );
    const down = fireEvent.mouseDown(screen.getByRole('button'));
    // fireEvent returns false when a handler called preventDefault().
    expect(down).toBe(false);
  });

  it('positions itself against the viewport, above the selection', () => {
    render(
      <SelectionAsk candidate={candidate} onCandidateChange={() => {}} onAsk={() => {}} />,
    );
    const box = screen.getByRole('button').parentElement as HTMLElement;
    expect(box.style.position === '' ? getComputedStyle(box).position : box.style.position)
      .toBeDefined();
    expect(parseFloat(box.style.top)).toBeLessThan(300);
  });

  it('drops the candidate as soon as the browser selection collapses', () => {
    const onCandidateChange = vi.fn();
    render(
      <SelectionAsk
        candidate={candidate}
        onCandidateChange={onCandidateChange}
        onAsk={() => {}}
      />,
    );
    // No range at all is the collapsed case jsdom can express.
    window.getSelection()?.removeAllRanges();
    act(() => {
      document.dispatchEvent(new Event('selectionchange'));
    });
    expect(onCandidateChange).toHaveBeenCalledWith(null);
  });

  it('removes every listener it added on unmount', () => {
    const remove = vi.spyOn(document, 'removeEventListener');
    const { unmount } = render(
      <SelectionAsk candidate={null} onCandidateChange={() => {}} onAsk={() => {}} />,
    );
    unmount();
    const removed = remove.mock.calls.map((c) => c[0]);
    expect(removed).toContain('selectionchange');
    expect(removed).toContain('pointerup');
    expect(removed).toContain('keyup');
  });
});
