// @vitest-environment jsdom
/**
 * The admin conversation viewer is READ-ONLY oversight: plain
 * whitespace-preserved bubbles (no Markdown pipeline), timestamps, the
 * model/mode chips carried in message meta — and the notice that this very
 * view is being audited. Nothing on the page can write.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/navigation', () => ({
  useParams: () => ({ id: '7', cid: 'c-1' }),
}));

vi.mock('next/link', () => ({
  __esModule: true,
  default: (props: ComponentProps<'a'> & { children?: ReactNode }) => {
    const { children, ...rest } = props;
    return <a {...rest}>{children}</a>;
  },
}));

import AdminConversationViewerPage, {
  metaChips,
} from '@/app/admin/members/[id]/conversations/[cid]/page';

const PAYLOAD = {
  conversation: {
    id: 'c-1',
    title: 'Quarterly numbers',
    created_at: '2026-08-30T09:00:00Z',
    updated_at: '2026-08-31T10:30:00Z',
  },
  messages: [
    {
      id: 1,
      role: 'user',
      content: 'Show me Q3\nby region',
      created_at: '2026-08-31T10:29:00Z',
      meta: null,
    },
    {
      id: 2,
      role: 'assistant',
      content: 'Here are the Q3 numbers by region.',
      created_at: '2026-08-31T10:30:00Z',
      meta: { model: 'qwen3-35b', mode: 'sql' },
    },
  ],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('the read-only transcript viewer', () => {
  it('renders the transcript with chips, timestamps and the audit notice', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return { ok: true, status: 200, json: async () => PAYLOAD };
      }),
    );
    render(<AdminConversationViewerPage />);

    await waitFor(() =>
      expect(screen.getByText('Quarterly numbers')).toBeTruthy(),
    );
    expect(calls).toEqual(['/api/admin/members/7/conversations/c-1']);

    // Both messages, with the user's line breaks preserved, not markdownified.
    const userBubble = screen.getByText(/Show me Q3/);
    expect(userBubble.textContent).toBe('Show me Q3\nby region');
    expect(userBubble.className).toContain('whitespace-pre-wrap');
    expect(
      screen.getByText('Here are the Q3 numbers by region.'),
    ).toBeTruthy();

    // Model/mode chips from meta.
    expect(screen.getByText('qwen3-35b')).toBeTruthy();
    expect(screen.getByText('sql')).toBeTruthy();

    // The quiet oversight notice.
    expect(
      screen.getByText('Administrative access is recorded in the audit log.'),
    ).toBeTruthy();

    // READ-ONLY: no way to type or send anything here.
    expect(document.querySelector('textarea')).toBeNull();
    expect(document.querySelector('input')).toBeNull();
    expect(document.querySelector('button[type="submit"]')).toBeNull();
  });

  it('shows the failure, not a blank page, when the load is refused', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 404,
        json: async () => ({ detail: 'No such conversation.' }),
      })),
    );
    render(<AdminConversationViewerPage />);
    await waitFor(() =>
      expect(screen.getByText('No such conversation.')).toBeTruthy(),
    );
  });
});

describe('metaChips', () => {
  it('picks model and mode when present', () => {
    expect(metaChips({ model: 'qwen3-35b', mode: 'sql' })).toEqual([
      'qwen3-35b',
      'sql',
    ]);
  });

  it('falls back through engine and route for the second chip', () => {
    expect(metaChips({ engine: 'rag' })).toEqual(['rag']);
    expect(metaChips({ route: 'chat' })).toEqual(['chat']);
  });

  it('renders nothing for absent or malformed meta', () => {
    expect(metaChips(null)).toEqual([]);
    expect(metaChips({})).toEqual([]);
    expect(metaChips({ model: 42, mode: { nested: true } } as never)).toEqual(
      [],
    );
  });
});
