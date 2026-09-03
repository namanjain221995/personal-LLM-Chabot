// @vitest-environment jsdom
/**
 * Pasting into the composer (owner decision, 2026-09-04).
 *
 * Text pastes into the textarea. All of it, at any length: the composer used
 * to swallow anything over 1,200 characters or 12 lines into a "PASTED"
 * attachment chip, which meant a user who pasted a log could no longer see or
 * edit what they had just pasted. There is no threshold and no cap now.
 *
 * An IMAGE on the clipboard is still an attachment — that path is untouched,
 * and these tests guard the line between the two.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Composer } from '@/components/Composer';
import { DEFAULT_PREFS } from '@/lib/prefs';

afterEach(cleanup);

function mount() {
  const onSend = vi.fn();
  render(
    <Composer
      streaming={false}
      prefs={DEFAULT_PREFS}
      onPrefsChange={vi.fn()}
      onSend={onSend}
      onStop={vi.fn()}
    />,
  );
  return {
    box: screen.getByLabelText('Message') as HTMLTextAreaElement,
    onSend,
  };
}

/** A paste event carrying only text — the shape jsdom gives a real paste. */
function pasteText(box: HTMLTextAreaElement, text: string) {
  const event = new Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', {
    value: { items: [], getData: () => text },
  });
  act(() => {
    box.dispatchEvent(event);
  });
  return event;
}

const HUGE = Array.from({ length: 400 }, (_, i) => `line ${i} of a stack trace`).join(
  '\n',
);

describe('pasting text', () => {
  it('does not intercept the paste — the browser writes it into the box', () => {
    const { box } = mount();
    const event = pasteText(box, HUGE);
    // Not prevented: jsdom does not implement the insertion itself, so what
    // this asserts is precisely the thing that matters — the composer stands
    // aside and lets the default happen.
    expect(event.defaultPrevented).toBe(false);
  });

  it('makes no chip, however long the paste is', () => {
    const { box } = mount();
    pasteText(box, HUGE);
    expect(screen.queryByTitle('Show pasted text')).toBeNull();
    expect(screen.queryByText(/^PASTED$/)).toBeNull();
  });

  it('sends the pasted text as the message, at any size', () => {
    const { box, onSend } = mount();
    pasteText(box, HUGE);
    // The browser's insertion, which jsdom leaves to us.
    act(() => {
      fireEvent.change(box, { target: { value: HUGE } });
    });
    act(() => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    expect(onSend).toHaveBeenCalledTimes(1);
    const [text, attachments] = onSend.mock.calls[0];
    expect(text).toBe(HUGE);
    expect(text.length).toBeGreaterThan(1200);
    expect(attachments).toEqual([]);
  });
});

describe('pasting an image', () => {
  it('is still taken as an attachment', () => {
    const { box } = mount();
    const file = new File(['x'], 'shot.png', { type: 'image/png' });
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', {
      value: {
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }],
        getData: () => '',
      },
    });
    act(() => {
      box.dispatchEvent(event);
    });
    // The image branch claims the event; the text branch never would.
    expect(event.defaultPrevented).toBe(true);
  });
});
