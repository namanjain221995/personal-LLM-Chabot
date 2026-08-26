// @vitest-environment jsdom
/**
 * The composer half of "Edit": `ComposerHandle.prefill`.
 *
 * `insert` already existed, but it belongs to the clarification panel — it
 * appends a single keystroke with no separator, which is right there and wrong
 * here: reusing it would weld a whole prompt onto the end of a half-typed word
 * ("how do I" + "write python…" → "how do Iwrite python…").
 *
 * The two properties that matter:
 * - an unsent draft is never destroyed, because losing something the user
 *   typed to recover something they already sent is a bad trade;
 * - loading text is not sending it. Edit puts the prompt in the box and stops.
 */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createRef } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Composer, type ComposerHandle } from '@/components/Composer';
import { DEFAULT_PREFS } from '@/lib/prefs';

afterEach(cleanup);

function mount() {
  const ref = createRef<ComposerHandle>();
  const onSend = vi.fn();
  const onDraftChange = vi.fn();
  render(
    <Composer
      ref={ref}
      streaming={false}
      prefs={DEFAULT_PREFS}
      onPrefsChange={vi.fn()}
      onSend={onSend}
      onStop={vi.fn()}
      onDraftChange={onDraftChange}
    />,
  );
  const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
  return { ref, box, onSend, onDraftChange };
}

describe('prefill loads a prompt for editing', () => {
  it('puts the exact text in an empty box and focuses it', () => {
    const { ref, box } = mount();
    const text = 'write python full code for Atm management System??';

    act(() => ref.current?.prefill(text));

    expect(box.value).toBe(text);
    expect(document.activeElement).toBe(box);
    // Caret at the end, so typing continues the prompt.
    expect(box.selectionStart).toBe(text.length);
  });

  it('preserves line breaks and unicode exactly', () => {
    const { ref, box } = mount();
    const text = 'line one\n\nline three — “quoted” 🙂\n\ttabbed';
    act(() => ref.current?.prefill(text));
    expect(box.value).toBe(text);
  });

  it('sends nothing and starts no generation', () => {
    const { ref, onSend } = mount();
    act(() => ref.current?.prefill('a prompt'));
    expect(onSend).not.toHaveBeenCalled();
  });

  it('tells the meter about the new draft', () => {
    const { ref, onDraftChange } = mount();
    act(() => ref.current?.prefill('a prompt'));
    expect(onDraftChange).toHaveBeenCalledWith('a prompt');
  });

  it('ignores an empty prefill rather than clearing the box', () => {
    const { ref, box } = mount();
    fireEvent.change(box, { target: { value: 'my draft' } });
    act(() => ref.current?.prefill(''));
    expect(box.value).toBe('my draft');
  });
});

describe('prefill never destroys an unsent draft', () => {
  it('keeps what was typed and starts the loaded prompt on its own paragraph', () => {
    const { ref, box } = mount();
    fireEvent.change(box, { target: { value: 'half a thought' } });

    act(() => ref.current?.prefill('the older prompt'));

    expect(box.value).toBe('half a thought\n\nthe older prompt');
    // Both are visible and either can be deleted — nothing was silently lost.
    expect(box.value).toContain('half a thought');
    expect(box.selectionStart).toBe(box.value.length);
  });

  it('does not double the blank line when the draft already ends in one', () => {
    const { ref, box } = mount();
    fireEvent.change(box, { target: { value: 'half a thought\n\n' } });
    act(() => ref.current?.prefill('the older prompt'));
    expect(box.value).toBe('half a thought\n\nthe older prompt');
  });
});

describe('insert is untouched — the clarification panel still depends on it', () => {
  it('still appends a bare keystroke with no separator', () => {
    const { ref, box } = mount();
    fireEvent.change(box, { target: { value: 'Q' } });
    act(() => ref.current?.insert('3'));
    expect(box.value).toBe('Q3');
  });
});
