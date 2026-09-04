// @vitest-environment jsdom
/**
 * The share dialog.
 *
 * Every test here asserts the same underlying rule from a different angle:
 * THE DIALOG DECIDES NOTHING. It renders what the server said. So the tests
 * feed it server answers and check what a person sees — never that the
 * component worked out for itself whether something was safe to publish.
 *
 * The disabled Create button is a courtesy, not a control. That is why one
 * test drives the create request anyway and confirms the server's refusal
 * still reaches the person: if the button were the only thing stopping a
 * publish, this test is what would fail.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ShareDialog } from '@/components/ShareDialog';

const toast = vi.fn();
vi.mock('@/components/Providers', () => ({ useToast: () => ({ toast }) }));

const OPEN_POLICY = {
  public_allowed: true,
  workspace_allowed: true,
  blocking_reasons: [],
  warnings: [],
  shareable_messages: 4,
};

function statusBody(over: Record<string, unknown> = {}) {
  return {
    enabled: true,
    share: null,
    policy: OPEN_POLICY,
    unshared_messages: 0,
    expiry_choices: ['24h', '7d', '30d', '90d'],
    default_expiry: '30d',
    ...over,
  };
}

const ACTIVE_SHARE = {
  id: 1,
  visibility: 'public',
  status: 'active',
  url: 'https://ai.example.com/share/abc123',
  created_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + 7 * 86_400_000).toISOString(),
  show_owner_name: false,
  version: 1,
  message_count: 4,
  last_message_id: 9,
  view_count: 3,
  last_viewed_at: new Date().toISOString(),
};

/** Route each call by method+path, so a test states only what it cares about. */
function serve(routes: Record<string, () => { status?: number; body: unknown }>) {
  const calls: { method: string; url: string; body?: string }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      calls.push({ method, url, body: init?.body as string | undefined });
      const key = `${method} ${url.split('?')[0]}`;
      const handler = routes[key] ?? routes[method] ?? null;
      if (!handler) throw new Error(`unmocked request: ${key}`);
      const { status = 200, body } = handler();
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
      };
    }),
  );
  return calls;
}

const PATH = '/api/conversations/c-1/share';

function mount() {
  render(
    <ShareDialog
      conversationId="c-1"
      title="A conversation about retrieval"
      onClose={() => undefined}
    />,
  );
}

beforeEach(() => {
  toast.mockClear();
  Object.assign(navigator, { clipboard: { writeText: vi.fn(async () => undefined) } });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('before anything is shared', () => {
  it('says plainly that it publishes a copy, not the conversation', async () => {
    serve({ [`GET ${PATH}`]: () => ({ body: statusBody() }) });
    mount();
    await screen.findByText(/read-only copy of this conversation as it is right now/i);
    expect(screen.getByText(/messages you send later are not added automatically/i)).toBeTruthy();
    expect(screen.getByText(/your email address is never shown/i)).toBeTruthy();
  });

  it('shows the server’s refusal in the server’s words', async () => {
    serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({
          policy: {
            ...OPEN_POLICY,
            public_allowed: false,
            blocking_reasons: [
              'This conversation draws on Salesforce records, which cannot be shared outside the workspace.',
            ],
          },
        }),
      }),
    });
    mount();
    await screen.findByText(/draws on Salesforce records/i);
    // The narrower option is still open, and the dialog says so.
    expect(screen.getByText(/still share it inside this workspace/i)).toBeTruthy();
  });

  it('offers only the expiry options the workspace allows', async () => {
    serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({ expiry_choices: ['24h', '7d'], default_expiry: '7d' }),
      }),
    });
    mount();
    await screen.findByText(/read-only copy/i);
    expect(screen.getByRole('option', { name: '24 hours' })).toBeTruthy();
    // "never" is a policy decision; if the server did not offer it, it is not
    // an option a person can pick and have refused later.
    expect(screen.queryByRole('option', { name: 'No expiry' })).toBeNull();
  });

  it('lets the server refuse even when the button was reachable', async () => {
    // The whole point: the disabled button is a courtesy. Drive the request.
    serve({
      [`GET ${PATH}`]: () => ({ body: statusBody() }),
      [`POST ${PATH}`]: () => ({
        status: 422,
        body: { detail: 'A message appears to contain an AWS access key.' },
      }),
    });
    mount();
    const create = await screen.findByRole('button', { name: 'Create link' });
    fireEvent.click(create);
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        'A message appears to contain an AWS access key.',
        'error',
      ),
    );
    expect(screen.queryByLabelText('Share link')).toBeNull();
  });

  it('sends exactly what the person chose', async () => {
    const calls = serve({
      [`GET ${PATH}`]: () => ({ body: statusBody() }),
      [`POST ${PATH}`]: () => ({
        body: { share: ACTIVE_SHARE, url: 'https://ai.example.com/share/abc.secret', token: 'abc.secret', truncated: false },
      }),
    });
    mount();
    await screen.findByRole('button', { name: 'Create link' });
    fireEvent.change(screen.getByDisplayValue('30 days'), { target: { value: '7d' } });
    fireEvent.click(screen.getByLabelText(/show my display name/i));
    fireEvent.click(screen.getByRole('button', { name: 'Create link' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    const sent = JSON.parse(calls.find((c) => c.method === 'POST')!.body!);
    expect(sent).toEqual({ visibility: 'public', expiry: '7d', show_owner_name: true });
  });
});

describe('once a link exists', () => {
  it('shows the full link once, and afterwards only the half that is stored', async () => {
    let created = false;
    serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({ share: created ? ACTIVE_SHARE : null }),
      }),
      [`POST ${PATH}`]: () => {
        created = true;
        return {
          body: {
            share: ACTIVE_SHARE,
            url: 'https://ai.example.com/share/abc123.the-secret-half',
            token: 'abc123.the-secret-half',
            truncated: false,
          },
        };
      },
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Create link' }));

    const field = (await screen.findByLabelText('Share link')) as HTMLInputElement;
    await waitFor(() => expect(field.value).toContain('the-secret-half'));
    // The server never returns it again — the state after a reload is the
    // stored half plus an explanation, not a broken link presented as whole.
    expect(screen.queryByText(/shown once, when it was created/i)).toBeNull();
  });

  it('explains itself when the secret is gone rather than offering half a link', async () => {
    serve({ [`GET ${PATH}`]: () => ({ body: statusBody({ share: ACTIVE_SHARE }) }) });
    mount();
    await screen.findByLabelText('Share link');
    expect(screen.getByText(/full link was shown once/i)).toBeTruthy();
    expect(screen.getByText(/stop sharing and create a new one/i)).toBeTruthy();
  });

  it('does not stop sharing on one click', async () => {
    const calls = serve({
      [`GET ${PATH}`]: () => ({ body: statusBody({ share: ACTIVE_SHARE }) }),
      [`DELETE ${PATH}`]: () => ({ body: { revoked: true } }),
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Stop sharing' }));
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
    expect(screen.getByText('Stop sharing?')).toBeTruthy();

    // Backing out leaves the link alone.
    fireEvent.click(screen.getByRole('button', { name: 'Keep it' }));
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
  });

  it('stops sharing when the confirmation is taken', async () => {
    const calls = serve({
      [`GET ${PATH}`]: () => ({ body: statusBody({ share: ACTIVE_SHARE }) }),
      [`DELETE ${PATH}`]: () => ({ body: { revoked: true } }),
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Stop sharing' }));
    fireEvent.click(screen.getAllByRole('button', { name: 'Stop sharing' })[0]);
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true));
  });

  it('offers to republish only when there is something unpublished', async () => {
    serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({ share: ACTIVE_SHARE, unshared_messages: 2 }),
      }),
      [`POST ${PATH}`]: () => ({ body: { share: { ...ACTIVE_SHARE, version: 2 } } }),
    });
    mount();
    await screen.findByText(/2 newer messages have not been added/i);
    fireEvent.click(screen.getByRole('button', { name: 'Update the link' }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Shared link updated', 'info'));
  });

  it('does not offer to republish an up-to-date link', async () => {
    serve({ [`GET ${PATH}`]: () => ({ body: statusBody({ share: ACTIVE_SHARE }) }) });
    mount();
    await screen.findByLabelText('Share link');
    expect(screen.queryByRole('button', { name: 'Update the link' })).toBeNull();
  });

  it('tells the truth when the browser refuses the clipboard', async () => {
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn(async () => {
          throw new Error('denied');
        }),
      },
    });
    serve({ [`GET ${PATH}`]: () => ({ body: statusBody({ share: ACTIVE_SHARE }) }) });
    mount();
    await screen.findByLabelText('Share link');
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    // Not a fake success toast: the field stays selectable and says so —
    // once visibly, once into the aria-live region, which is why this is
    // findAllBy and both are asserted rather than one being an accident.
    const said = await screen.findAllByText(/refused clipboard access/i);
    expect(said).toHaveLength(2);
    expect(said.some((n) => n.className.includes('sr-only'))).toBe(true);
    expect(toast).not.toHaveBeenCalledWith('Link copied', 'info');
    // And the link is still there to copy by hand.
    expect(screen.getByLabelText('Share link')).toBeTruthy();
  });
});

describe('the dialog itself', () => {
  it('is a modal dialog with an accessible name', async () => {
    serve({ [`GET ${PATH}`]: () => ({ body: statusBody() }) });
    mount();
    const dialog = await screen.findByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(screen.getByText('Share this conversation')).toBeTruthy();
  });

  it('survives a server that is simply not there', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('fetch failed');
      }),
    );
    mount();
    await screen.findByText(/could not be reached/i);
    // The footer stays (so Cancel still works) but the action is inert —
    // a Create button that posts into a void is worse than a dead one.
    expect(
      screen.getByRole('button', { name: 'Create link' }).hasAttribute('disabled'),
    ).toBe(true);
  });
});

describe('a link that never expires', () => {
  const NEVER_SHARE = { ...ACTIVE_SHARE, expires_at: null };

  it('says so, and offers "No expiry" when the workspace allows it', async () => {
    serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({
          share: NEVER_SHARE,
          expiry_choices: ['24h', '7d', '30d', '90d', 'never'],
        }),
      }),
    });
    mount();
    await screen.findByLabelText('Share link');
    const select = screen.getByLabelText('Expires') as HTMLSelectElement;
    expect(select.value).toBe('never');
    expect(screen.getByText('Never expires')).toBeTruthy();
  });

  it('still shows the truth after the workspace withdraws the option', async () => {
    // The contradiction this prevents: a React select given a value that is
    // not among its options renders the FIRST one, so the control would have
    // said "24 hours" directly above a caption reading "Never expires".
    serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({
          share: NEVER_SHARE,
          expiry_choices: ['24h', '7d', '30d', '90d'],
        }),
      }),
    });
    mount();
    await screen.findByLabelText('Share link');
    const select = screen.getByLabelText('Expires') as HTMLSelectElement;
    expect(select.value).toBe('never');
    expect(screen.getByText('Never expires')).toBeTruthy();
    // Readable, but not re-selectable — the server would refuse it anyway.
    const option = screen.getByRole('option', { name: 'No expiry' }) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
    // The options policy DOES allow stay selectable.
    expect((screen.getByRole('option', { name: '7 days' }) as HTMLOptionElement).disabled).toBe(false);
  });

  it('turns a dated link into one that never expires', async () => {
    const calls = serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({
          share: ACTIVE_SHARE,
          expiry_choices: ['24h', '7d', '30d', '90d', 'never'],
        }),
      }),
      [`PATCH ${PATH}`]: () => ({ body: { share: NEVER_SHARE } }),
    });
    mount();
    await screen.findByLabelText('Share link');
    fireEvent.change(screen.getByLabelText('Expires'), { target: { value: 'never' } });

    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    expect(JSON.parse(calls.find((c) => c.method === 'PATCH')!.body!)).toEqual({
      expiry: 'never',
    });
    // A sentence, not "Link now no expiry".
    expect(toast).toHaveBeenCalledWith('This link no longer expires', 'info');
  });

  it('reports a shortened expiry as a sentence too', async () => {
    serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({ share: NEVER_SHARE, expiry_choices: ['24h', '7d', 'never'] }),
      }),
      [`PATCH ${PATH}`]: () => ({ body: { share: ACTIVE_SHARE } }),
    });
    mount();
    await screen.findByLabelText('Share link');
    fireEvent.change(screen.getByLabelText('Expires'), { target: { value: '7d' } });
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith('Link now expires in 7 days', 'info'),
    );
  });

  it('puts the control back when the server refuses the change', async () => {
    // Without the revert the select kept showing the value the server had
    // just rejected, directly above a caption reporting the real one.
    serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({
          share: NEVER_SHARE,
          expiry_choices: ['24h', '7d', '30d', '90d', 'never'],
        }),
      }),
      [`PATCH ${PATH}`]: () => ({
        status: 422,
        body: { detail: 'This workspace caps shared links at 7 days.' },
      }),
    });
    mount();
    await screen.findByLabelText('Share link');
    const select = screen.getByLabelText('Expires') as HTMLSelectElement;
    expect(select.value).toBe('never');

    fireEvent.change(select, { target: { value: '90d' } });
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        'This workspace caps shared links at 7 days.',
        'error',
      ),
    );
    // Back to what the link actually is, and still agreeing with the caption.
    expect(select.value).toBe('never');
    expect(screen.getByText('Never expires')).toBeTruthy();
  });

  it('surfaces the server’s refusal if policy changed under the person', async () => {
    serve({
      [`GET ${PATH}`]: () => ({
        body: statusBody({
          share: ACTIVE_SHARE,
          expiry_choices: ['24h', '7d', '30d', '90d', 'never'],
        }),
      }),
      [`PATCH ${PATH}`]: () => ({
        status: 422,
        body: { detail: 'This workspace requires shared links to expire.' },
      }),
    });
    mount();
    await screen.findByLabelText('Share link');
    fireEvent.change(screen.getByLabelText('Expires'), { target: { value: 'never' } });
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        'This workspace requires shared links to expire.',
        'error',
      ),
    );
  });
});
