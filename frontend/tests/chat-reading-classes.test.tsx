// @vitest-environment jsdom
/**
 * The component half of the 2026-09-13 reading-size change (owner, next to
 * ChatGPT: "a bigger sidebar, brighter text, bigger text in the chat and the
 * sidebar"). reading-size.test.ts pins the CSS; this pins that the components
 * actually wear it — a `.chat-answer` rule no element carries changes nothing.
 *
 * jsdom loads no stylesheet, so these read class names, not pixels. The
 * pixels were measured in Chrome at 390, 768, 1280, 1440 and 1920 px.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Composer } from '@/components/Composer';
import { MessageRow } from '@/components/MessageRow';
import { SharedConversation } from '@/components/SharedConversation';
import { Sidebar } from '@/components/Sidebar';
import { DEFAULT_PREFS } from '@/lib/prefs';
import type { ChatPrefs } from '@/lib/prefs';
import type { ChatMessage } from '@/lib/types';

vi.mock('@/lib/share', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/share')>()),
  getPublicShare: vi.fn(async () => ({
    snapshot: {
      schema: 1,
      title: 'Shared',
      shared_at: '2026-09-13T00:00:00Z',
      truncated: false,
      messages: [
        { role: 'user', content: 'The shared question' },
        { role: 'assistant', content: 'The shared answer' },
      ],
    },
    visibility: 'public',
    shared_at: '2026-09-13T00:00:00Z',
    expires_at: null,
  })),
}));

afterEach(cleanup);

const classes = (el: Element | null) => (el?.getAttribute('class') ?? '').split(/\s+/);

function renderMessage(message: ChatMessage, props: Record<string, unknown> = {}) {
  return render(
    <MessageRow
      message={message}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      onEditStart={vi.fn()}
      onEditCancel={vi.fn()}
      onEditSubmit={vi.fn()}
      {...props}
    />,
  );
}

function renderComposer(prefs: ChatPrefs) {
  render(
    <Composer
      streaming={false}
      prefs={prefs}
      onPrefsChange={vi.fn()}
      onSend={vi.fn()}
      onStop={vi.fn()}
    />,
  );
}

describe('the chat thread', () => {
  it('wraps the assistant answer in chat-answer, which sets the 16/17px reading size', () => {
    renderMessage({ id: 'a1', role: 'assistant', content: 'An answer.', status: 'done', createdAt: 0 });
    expect(classes(document.querySelector('[data-chat-message-role="assistant"]'))).toContain('chat-answer');
  });

  it('writes the edit box at the same 16px as the bubble it replaces', () => {
    renderMessage({ id: 'u1', role: 'user', content: 'A question', createdAt: 0 }, { editing: true });
    const box = screen.getByLabelText('Edit your message');
    expect(classes(box)).toContain('text-base');
    expect(classes(box)).not.toContain('text-[15px]');
  });

  it('wraps the answer on a public share page in chat-answer too, and sets its question at 16px', async () => {
    render(<SharedConversation token="t" />);
    const answer = await screen.findByText('The shared answer');
    expect(answer.closest('.chat-answer')).not.toBeNull();
    expect(classes(screen.getByText('The shared question'))).toContain('text-base');
  });
});

describe('the composer', () => {
  it('types at 16px, so a phone browser never zooms the page when the box is focused', () => {
    renderComposer(DEFAULT_PREFS);
    const box = screen.getByLabelText('Message');
    expect(classes(box)).toContain('text-base');
    expect(classes(box)).not.toContain('text-[15px]');
  });

  it('never sets the relaxed privacy line at half opacity, which put it below AA in both themes', () => {
    renderComposer({ ...DEFAULT_PREFS, salesforce: false, webSearch: 'auto' });
    const line = screen.getByText(/Salesforce is off — answers may use the web/);
    expect(classes(line)).toContain('text-faint');
    expect(classes(line)).not.toContain('opacity-50');
  });

  it('sets the forced web-search warning in the brighter muted ink, so it stays louder than the relaxed line', () => {
    renderComposer({ ...DEFAULT_PREFS, salesforce: false, webSearch: 'on' });
    const line = screen.getByText(/web search is on/);
    expect(classes(line)).toContain('text-muted');
    expect(classes(line)).not.toContain('text-faint');
  });
});

describe('the sidebar', () => {
  const noop = () => undefined;
  function renderSidebar() {
    return render(
      <Sidebar
        open
        onClose={noop}
        conversations={[
          { id: 'a', title: 'Pinned chat', createdAt: 1, updatedAt: 1, pinned: true },
          { id: 'b', title: 'Recent chat', createdAt: 1, updatedAt: 1 },
        ]}
        archived={[]}
        activeId="b"
        onNewChat={noop}
        onOpenSearch={noop}
        onSelect={noop}
        onRename={noop}
        onDelete={noop}
        onSetPinned={noop}
        onSetArchived={noop}
        onExport={noop}
        onLoadArchived={noop}
      />,
    );
  }

  it('takes its width from the w-sidebar token in both the desktop column and the phone drawer', () => {
    const { container } = renderSidebar();
    const panels = container.querySelectorAll('[aria-label="Sidebar"] .w-sidebar');
    expect(panels.length).toBe(2);
  });

  it('sets each conversation row at 15px on a 22px line with 7px padding, a 36px row', () => {
    const { container } = renderSidebar();
    // The row itself is the li's direct button; its "⋯" menu is a sibling.
    const rows = Array.from(container.querySelectorAll('li > button')).filter(
      (b) => b.textContent?.trim() === 'Recent chat',
    );
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(classes(row)).toEqual(expect.arrayContaining(['text-[15px]', 'leading-[22px]', 'py-[7px]']));
      expect(classes(row)).not.toContain('text-sm');
    }
  });

  it('sets New chat at 15px and the section labels at 12px, so the chrome keeps pace with the rows', async () => {
    renderSidebar();
    for (const button of screen.getAllByRole('button', { name: /New chat/ })) {
      expect(classes(button)).toContain('text-[15px]');
    }
    await waitFor(() => expect(screen.getAllByText('Pinned').length).toBeGreaterThan(0));
    for (const label of screen.getAllByText('Pinned')) {
      expect(classes(label)).toContain('text-[12px]');
    }
  });
});
