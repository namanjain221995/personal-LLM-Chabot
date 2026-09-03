// @vitest-environment jsdom
/**
 * Edit · Copy on the user's own messages.
 *
 * The transcript gave the assistant a full action row and the user nothing at
 * all — the user branch of `MessageRow` returned before any of it. So the one
 * text in the thread the user actually wrote was the one text they could not
 * copy, and re-asking a long prompt with one word changed meant retyping it.
 *
 * Edit then landed the prompt in the COMPOSER, which was the wrong place: the
 * rewrite appeared at the bottom of the screen instead of on the message it
 * belonged to, on top of whatever was already typed there, and re-opening it
 * stacked copy after copy of the same sentence in the box. It now rewrites the
 * message where the message is — a textarea over Cancel · Send, ChatGPT-style.
 *
 * The load-bearing constraint has moved with it. The row may not decide
 * anything: sending an edit really does replace the turn and discard every
 * turn after it, so the row only ever REPORTS (`onEditStart`/`onEditSubmit`)
 * and the host owns the truncate, the confirmation and the regeneration. A
 * test that only checked "the box appears with the right text in it" would
 * pass just as happily for a version that deleted half the conversation on its
 * own initiative.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageRow } from '@/components/MessageRow';
import type { ChatMessage } from '@/lib/types';

afterEach(cleanup);

const user = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'u1',
  role: 'user',
  content: 'write python full code for Atm management System??',
  createdAt: 0,
  ...over,
});

const assistant = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'a1',
  role: 'assistant',
  content: 'Here is the answer.',
  status: 'done',
  createdAt: 0,
  ...over,
});

function renderRow(message: ChatMessage, props: Record<string, unknown> = {}) {
  const onEditStart = vi.fn();
  const onEditCancel = vi.fn();
  const onEditSubmit = vi.fn();
  const view = render(
    <MessageRow
      message={message}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      onEditStart={onEditStart}
      onEditCancel={onEditCancel}
      onEditSubmit={onEditSubmit}
      {...props}
    />,
  );
  return { onEditStart, onEditCancel, onEditSubmit, ...view };
}

/** The row rendered with its editor already open — the host's `editing`. */
function renderEditing(message: ChatMessage) {
  return renderRow(message, { editing: true });
}

const editor = () => screen.getByRole('textbox') as HTMLTextAreaElement;
const sendBtn = () =>
  screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement;

/** Every network path the row could possibly reach, watched at once. */
let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchSpy = vi.fn(async () => {
    throw new Error('no request may be made from a message action');
  });
  vi.stubGlobal('fetch', fetchSpy);
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn(async () => undefined) },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/* ------------------------------------------------------------ F. Copy */

describe('F — Copy on a user message', () => {
  it('is present and copies the message EXACTLY', async () => {
    const text =
      'line one\n\n  line three with  spacing  \nand émoji 🙂 — plus “smart quotes”';
    renderRow(user({ content: text }));

    const copy = screen.getByRole('button', { name: 'Copy message' });
    fireEvent.click(copy);

    expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(text);
  });

  it('copies once per click, and touches nothing else', () => {
    const { onEditStart } = renderRow(user());
    fireEvent.click(screen.getByRole('button', { name: 'Copy message' }));

    expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);
    expect(fetchSpy).not.toHaveBeenCalled(); // no backend, no generation
    expect(onEditStart).not.toHaveBeenCalled();
  });

  it('renders the bubble text unchanged next to it', () => {
    renderRow(user({ content: 'exact prompt' }));
    expect(screen.getByText('exact prompt')).toBeTruthy();
  });
});

/* ------------------------------------------------- G. Edit — opening it */

describe('G — the pencil asks the host to open the editor', () => {
  it('reports the request and does nothing else', () => {
    const { onEditStart, onEditSubmit } = renderRow(user());

    fireEvent.click(screen.getByRole('button', { name: 'Edit message' }));

    expect(onEditStart).toHaveBeenCalledTimes(1);
    // Opening is not sending, and no request of any kind is made — in
    // particular no /truncate.
    expect(onEditSubmit).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('does NOT open the box by itself — `editing` is the host s to give', () => {
    const message = user({ content: 'original' });
    const snapshot = JSON.stringify(message);
    renderRow(message);

    fireEvent.click(screen.getByRole('button', { name: 'Edit message' }));

    // A row that opened its own editor would be a row that could rewrite a
    // turn the host never agreed to touch.
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('original')).toBeTruthy();
    expect(JSON.stringify(message)).toBe(snapshot);
  });

  it('is absent when the host offers no way to start an edit', () => {
    render(
      <MessageRow
        message={user()}
        isLast={false}
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: 'Edit message' })).toBeNull();
    // Copy does not depend on the host and stays.
    expect(screen.getByRole('button', { name: 'Copy message' })).toBeTruthy();
  });
});

/* ------------------------------------------------- H. Edit — the editor */

describe('H — the open editor', () => {
  it('replaces the bubble with a textarea holding the exact text', () => {
    const text = 'first line\nsecond line';
    renderEditing(user({ content: text }));

    expect(editor().value).toBe(text); // line breaks preserved
    // The read-only bubble and its actions are gone while it is open.
    expect(screen.queryByRole('button', { name: 'Edit message' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Copy message' })).toBeNull();
  });

  it('seeds itself from the message however it was opened', () => {
    // Rendered straight into `editing` with no click anywhere: the box must
    // still be correct, or the row is only accidentally controlled.
    renderEditing(user({ content: 'seeded from the message' }));
    expect(editor().value).toBe('seeded from the message');
  });

  it('focuses the box and parks the caret after the last character', () => {
    const text = 'edit me';
    renderEditing(user({ content: text }));

    const ta = editor();
    expect(document.activeElement).toBe(ta);
    // Typing continues the prompt instead of inserting at position 0.
    expect(ta.selectionStart).toBe(text.length);
    expect(ta.selectionEnd).toBe(text.length);
  });

  it('offers Cancel and Send, and reaches no network to render', () => {
    renderEditing(user());
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
    expect(sendBtn()).toBeTruthy();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('re-opening starts from the message, never from an abandoned edit', () => {
    const message = user({ content: 'the original' });
    const { rerender } = renderEditing(message);

    fireEvent.change(editor(), { target: { value: 'half-typed rubbish' } });
    // Close…
    rerender(
      <MessageRow
        message={message}
        isLast={false}
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
        onEditStart={vi.fn()}
        editing={false}
      />,
    );
    // …and open again.
    rerender(
      <MessageRow
        message={message}
        isLast={false}
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
        onEditStart={vi.fn()}
        editing
      />,
    );

    expect(editor().value).toBe('the original');
  });

  it('keeps the turn s attachments visible while it is being rewritten', () => {
    renderEditing(user({ content: 'about this file', pdfName: 'report.pdf' }));
    expect(screen.getByText('report.pdf')).toBeTruthy();
    expect(editor().value).toBe('about this file');
  });
});

/* -------------------------------------------------- I. Edit — cancelling */

describe('I — Cancel', () => {
  it('reports the cancel and submits nothing', () => {
    const { onEditCancel, onEditSubmit } = renderEditing(user());

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onEditCancel).toHaveBeenCalledTimes(1);
    expect(onEditSubmit).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('discards a rewrite without ever reporting it', () => {
    const { onEditCancel, onEditSubmit } = renderEditing(user());

    fireEvent.change(editor(), { target: { value: 'a change I regret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onEditCancel).toHaveBeenCalledTimes(1);
    expect(onEditSubmit).not.toHaveBeenCalled();
  });

  it('Escape cancels too, and is swallowed before the window shortcuts', () => {
    const { onEditCancel, onEditSubmit } = renderEditing(user());
    const onWindowKey = vi.fn();
    window.addEventListener('keydown', onWindowKey);

    fireEvent.keyDown(editor(), { key: 'Escape' });

    expect(onEditCancel).toHaveBeenCalledTimes(1);
    expect(onEditSubmit).not.toHaveBeenCalled();
    // Esc must not also reach the app-level handler behind the thread.
    expect(onWindowKey).not.toHaveBeenCalled();
    window.removeEventListener('keydown', onWindowKey);
  });

  it('leaves the stored message object untouched', () => {
    const message = user({ content: 'original' });
    const snapshot = JSON.stringify(message);
    renderEditing(message);

    fireEvent.change(editor(), { target: { value: 'rewritten' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    // The row never writes to history — only the host does, and only on send.
    expect(JSON.stringify(message)).toBe(snapshot);
  });
});

/* ---------------------------------------------------- J. Edit — sending */

describe('J — Send', () => {
  it('reports the rewritten text, trimmed', () => {
    const { onEditSubmit } = renderEditing(user());

    fireEvent.change(editor(), { target: { value: '  the new question  ' } });
    fireEvent.click(sendBtn());

    expect(onEditSubmit).toHaveBeenCalledTimes(1);
    expect(onEditSubmit).toHaveBeenCalledWith('the new question');
  });

  it('keeps the line breaks inside the rewrite', () => {
    const { onEditSubmit } = renderEditing(user());

    fireEvent.change(editor(), { target: { value: 'line one\nline two' } });
    fireEvent.click(sendBtn());

    expect(onEditSubmit).toHaveBeenCalledWith('line one\nline two');
  });

  it('sends on Enter and breaks the line on Shift+Enter', () => {
    const { onEditSubmit } = renderEditing(user());
    fireEvent.change(editor(), { target: { value: 'ask this instead' } });

    fireEvent.keyDown(editor(), { key: 'Enter', shiftKey: true });
    expect(onEditSubmit).not.toHaveBeenCalled();

    fireEvent.keyDown(editor(), { key: 'Enter' });
    expect(onEditSubmit).toHaveBeenCalledTimes(1);
    expect(onEditSubmit).toHaveBeenCalledWith('ask this instead');
  });

  it('refuses to send an emptied box, by button or by Enter', () => {
    const { onEditSubmit } = renderEditing(user());

    fireEvent.change(editor(), { target: { value: '   ' } });

    expect(sendBtn().disabled).toBe(true);
    fireEvent.click(sendBtn());
    fireEvent.keyDown(editor(), { key: 'Enter' });
    // Cancel is how you back out of an edit — never an empty send.
    expect(onEditSubmit).not.toHaveBeenCalled();
  });

  it('re-enables Send as soon as there is something to send again', () => {
    renderEditing(user());
    fireEvent.change(editor(), { target: { value: '' } });
    expect(sendBtn().disabled).toBe(true);
    fireEvent.change(editor(), { target: { value: 'back' } });
    expect(sendBtn().disabled).toBe(false);
  });

  it('reports and stops — it does not truncate, store or generate', () => {
    const message = user({ content: 'original' });
    const snapshot = JSON.stringify(message);
    const { onEditSubmit } = renderEditing(message);

    fireEvent.change(editor(), { target: { value: 'rewritten' } });
    fireEvent.click(sendBtn());

    expect(onEditSubmit).toHaveBeenCalledWith('rewritten');
    // THE constraint: the row reports, the host acts. No /truncate, no
    // /api/chat, and the historical message object is not rewritten here.
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(JSON.stringify(message)).toBe(snapshot);
  });

  it('an unchanged text is still reported — the host decides it is a no-op', () => {
    const { onEditSubmit } = renderEditing(user({ content: 'same' }));

    fireEvent.click(sendBtn());

    // The row cannot know whether re-asking is meaningful; only the host can
    // weigh it against the turns it would discard.
    expect(onEditSubmit).toHaveBeenCalledWith('same');
  });
});

/* ------------------------------------------- K. what is worth acting on */

/**
 * REPLACES "a turn with no text gets no action row", which pinned the bug
 * rather than the requirement.
 *
 * That test asserted that an attachment-only turn showed neither Edit nor
 * Copy, and it passed for two years while the actual user problem was exactly
 * that: attach two documents, forget to type the question, and there was no
 * way back to the turn. The row is about the TURN, not about its prose.
 *
 * Copy's half of the old assertion survives unchanged, because it was never
 * the bug: Copy still means "copy what I wrote".
 */
describe('K — the action row belongs to the turn, not to its text', () => {
  it('EDIT-FILE-02 · an attachment-only message IS editable', () => {
    renderRow(user({ content: '', pdfName: 'report.pdf' }));
    expect(screen.getByRole('button', { name: 'Edit message' })).toBeTruthy();
    // The attachment chip itself is untouched.
    expect(screen.getByText('report.pdf')).toBeTruthy();
  });

  it('EDIT-FILE-14 · …but offers no Copy, because there is nothing to copy', () => {
    renderRow(user({ content: '', pdfName: 'report.pdf' }));
    expect(screen.queryByRole('button', { name: 'Copy message' })).toBeNull();
  });

  it('an image-only turn is editable too', () => {
    renderRow(user({ content: '', imageDataUrl: 'data:image/png;base64,AAA' }));
    expect(screen.getByRole('button', { name: 'Edit message' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Copy message' })).toBeNull();
  });

  it('a paste-only turn is editable — the same missing-prompt problem', () => {
    renderRow(
      user({
        content: '',
        meta: { pasted: [{ id: 'p1', content: 'LOG', lines: 1, chars: 3 }] },
      }),
    );
    expect(screen.getByRole('button', { name: 'Edit message' })).toBeTruthy();
  });

  it('a turn with NOTHING in it still gets no row at all', () => {
    // Whitespace and no attachment: there is genuinely nothing to act on, and
    // this is the half of the old test that was always right.
    renderRow(user({ content: '   \n ' }));
    expect(screen.queryByRole('button', { name: 'Copy message' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Edit message' })).toBeNull();
  });

  it('renders no empty row where the host offers no editor', () => {
    // Previews and tests pass no `onEditStart`. An attachment-only turn there
    // has no Edit and no Copy, so the row must not render as an empty flex box
    // with margin — invisible, but it would still move the layout.
    const { container } = render(
      <MessageRow
        message={user({ content: '', pdfName: 'report.pdf' })}
        isLast={false}
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(container.querySelectorAll('.mt-1\\.5.flex.items-center').length).toBe(0);
  });
});

/* --------------------------------------------------- L. no regression */

describe('L — the existing UI is unchanged', () => {
  it('assistant messages keep their own action row exactly as it was', () => {
    render(
      <MessageRow
        message={assistant()}
        isLast
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
        onEditStart={vi.fn()}
      />,
    );
    for (const name of [
      'Copy message',
      'Good response',
      'Bad response',
      'Try again',
    ]) {
      expect(screen.getByRole('button', { name })).toBeTruthy();
    }
    // Edit belongs to the user's own words only.
    expect(screen.queryByRole('button', { name: 'Edit message' })).toBeNull();
  });

  it('the user bubble keeps its exact classes', () => {
    renderRow(user({ content: 'hello' }));
    const bubble = screen.getByText('hello');
    expect(bubble.className).toBe(
      'whitespace-pre-wrap break-words rounded-[20px] bg-bubble px-4 py-2.5 text-[15px] leading-relaxed',
    );
  });

  it('user actions are hover-revealed, like every non-last assistant row', () => {
    renderRow(user());
    const row = screen
      .getByRole('button', { name: 'Copy message' })
      .parentElement as HTMLElement;
    expect(row.className).toContain('opacity-0');
    expect(row.className).toContain('group-hover/msg:opacity-100');
    expect(row.className).toContain('focus-within:opacity-100');
    // Same geometry tokens as the assistant row — no new spacing scale.
    expect(row.className).toContain('mt-1.5');
    expect(row.className).toContain('gap-0.5');
  });

  it('reuses the assistant row s ghost-icon button styling verbatim', () => {
    renderRow(user());
    const edit = screen.getByRole('button', { name: 'Edit message' });
    expect(edit.className).toBe(
      'rounded-lg p-2 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink',
    );
  });

  it('the editor wears the bubble s own shape and colour', () => {
    renderEditing(user());
    // It reads as the same turn being rewritten, not as a foreign panel.
    const box = editor().parentElement as HTMLElement;
    expect(box.className).toContain('rounded-[20px]');
    expect(box.className).toContain('bg-bubble');
  });
});
