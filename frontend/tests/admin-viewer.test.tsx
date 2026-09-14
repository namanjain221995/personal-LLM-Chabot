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

const MEMBER_ROW = {
  id: 7,
  name: 'Fixture Member',
  email: 'member@example.test',
  role: 'member',
  status: 'active',
  joined_at: null,
  last_active_at: null,
};

type Reply = { status: number; body: unknown };

/** A fetch stub answering by URL; records every call in order. */
function routeFetch(routes: Record<string, Reply>) {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      const reply = routes[url] ?? { status: 404, body: { detail: 'Not found.' } };
      return {
        ok: reply.status >= 200 && reply.status < 300,
        status: reply.status,
        json: async () => reply.body,
      };
    }),
  );
  return calls;
}

const INSPECTABLE: Reply = {
  status: 200,
  body: { member: MEMBER_ROW, stats: { conversations: 1 }, may_inspect: true },
};

describe('the read-only transcript viewer', () => {
  it('renders the transcript with chips, timestamps and the audit notice', async () => {
    const calls = routeFetch({
      '/api/admin/members/7': INSPECTABLE,
      '/api/admin/members/7/conversations/c-1': { status: 200, body: PAYLOAD },
    });
    render(<AdminConversationViewerPage />);

    await waitFor(() =>
      expect(screen.getByText('Quarterly numbers')).toBeTruthy(),
    );
    // The rank rule is asked first, then the transcript.
    expect(calls).toEqual([
      '/api/admin/members/7',
      '/api/admin/members/7/conversations/c-1',
    ]);

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
    routeFetch({
      '/api/admin/members/7': INSPECTABLE,
      '/api/admin/members/7/conversations/c-1': {
        status: 404,
        body: { detail: 'No such conversation.' },
      },
    });
    render(<AdminConversationViewerPage />);
    await waitFor(() =>
      expect(screen.getByText('No such conversation.')).toBeTruthy(),
    );
    expect(screen.getByRole('alert')).toBeTruthy();
  });

  it('still shows an error when the member itself is gone', async () => {
    const calls = routeFetch({
      '/api/admin/members/7': { status: 404, body: { detail: 'No such member.' } },
    });
    render(<AdminConversationViewerPage />);
    await waitFor(() => expect(screen.getByText('No such member.')).toBeTruthy());
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(calls).toEqual(['/api/admin/members/7']);
  });

  it('shows the calm privacy notice for a member this role may not inspect, without asking for the transcript', async () => {
    const calls = routeFetch({
      '/api/admin/members/7': {
        status: 200,
        body: {
          member: { ...MEMBER_ROW, role: 'super_admin' },
          stats: null,
          may_inspect: false,
        },
      },
    });
    render(<AdminConversationViewerPage />);
    await waitFor(() =>
      expect(
        screen.getByText(
          "This member's conversations, uploads, reports and sessions are private to higher roles.",
        ),
      ).toBeTruthy(),
    );
    expect(calls).toEqual(['/api/admin/members/7']);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    // The way back stays.
    expect(screen.getByText('Back to member')).toBeTruthy();
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
