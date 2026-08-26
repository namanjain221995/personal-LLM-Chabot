// @vitest-environment jsdom
/**
 * Edit · Copy on the user's own messages.
 *
 * The transcript gave the assistant a full action row and the user nothing at
 * all — the user branch of `MessageRow` returned before any of it. So the one
 * text in the thread the user actually wrote was the one text they could not
 * copy, and re-asking a long prompt with one word changed meant retyping it.
 *
 * The load-bearing constraint is what these two must NOT do. This task is
 * frontend-only: Edit hands the prompt back to the composer and stops. It does
 * not rewrite the stored message, does not discard the turns after it, does not
 * call the truncate endpoint, and does not send anything. A test that only
 * checked "the text arrives in the box" would pass just as happily for a
 * version that quietly deleted half the conversation on the way.
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
  const onEdit = vi.fn();
  render(
    <MessageRow
      message={message}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      onEdit={onEdit}
      {...props}
    />,
  );
  return { onEdit };
}

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
    const { onEdit } = renderRow(user());
    fireEvent.click(screen.getByRole('button', { name: 'Copy message' }));

    expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);
    expect(fetchSpy).not.toHaveBeenCalled(); // no backend, no generation
    expect(onEdit).not.toHaveBeenCalled();
  });

  it('renders the bubble text unchanged next to it', () => {
    renderRow(user({ content: 'exact prompt' }));
    expect(screen.getByText('exact prompt')).toBeTruthy();
  });
});

/* ------------------------------------------------------------ G. Edit */

describe('G — Edit on a user message', () => {
  it('hands the exact text to the host and does nothing else', () => {
    const text = 'first line\nsecond line';
    const { onEdit } = renderRow(user({ content: text }));

    fireEvent.click(screen.getByRole('button', { name: 'Edit message' }));

    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(onEdit).toHaveBeenCalledWith(text); // line breaks preserved
    // No request of any kind — in particular no /truncate, and no send.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('leaves the historical message object untouched', () => {
    const message = user({ content: 'original' });
    const snapshot = JSON.stringify(message);
    renderRow(message);

    fireEvent.click(screen.getByRole('button', { name: 'Edit message' }));

    expect(JSON.stringify(message)).toBe(snapshot);
    // …and the bubble still shows the original, not an edit box.
    expect(screen.getByText('original')).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('is absent when the host offers no edit handler', () => {
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

/* ------------------------------------------- H. nothing worth acting on */

describe('H — a turn with no text gets no action row', () => {
  it('shows neither action for an attachment-only message', () => {
    renderRow(user({ content: '', pdfName: 'report.pdf' }));
    expect(screen.queryByRole('button', { name: 'Copy message' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Edit message' })).toBeNull();
    // The attachment chip itself is untouched.
    expect(screen.getByText('report.pdf')).toBeTruthy();
  });

  it('shows neither action for whitespace-only content', () => {
    renderRow(user({ content: '   \n ' }));
    expect(screen.queryByRole('button', { name: 'Copy message' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Edit message' })).toBeNull();
  });
});

/* --------------------------------------------------- I. no regression */

describe('I — the existing UI is unchanged', () => {
  it('assistant messages keep their own action row exactly as it was', () => {
    render(
      <MessageRow
        message={assistant()}
        isLast
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
        onEdit={vi.fn()}
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
});
