// @vitest-environment jsdom
/**
 * The developer console at /api.
 *
 * The first block is the one that matters: THE GATE IS ON THE SERVER. A member
 * who types /api gets the same answer the admin surface gives — the page is
 * not there — and that answer is produced before any console markup exists,
 * not by leaving a link out of a menu. The distinction between "refused" and
 * "could not be checked" is tested too, because answering 404 while the
 * orchestrator restarts would tell an admin their console had been removed.
 *
 * After that: the CONTRACT §17 navigation and the capabilities each entry
 * hangs on, an empty state for every section (a console that invents a number
 * to avoid an empty state is worse than a blank one), a table that renders its
 * rows, and the show-once key flow — which must show the secret exactly once
 * and must offer no way back to it afterwards.
 */
import { act, cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import {
  ProjectSelect,
  limitsEnforcement,
  usageLimitText,
} from '@/components/devplatform/shared';
import type { Project } from '@/components/devplatform/types';
import userEvent from '@testing-library/user-event';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  search: new URLSearchParams(),
  session: { state: 'allowed' } as Record<string, unknown>,
  /**
   * When true, a closed AdminDialog still renders its children (marked
   * `data-closed`). The real dialog returns null when closed, which hides
   * whether a component kept a secret in STATE after closing — the exact gap
   * the wave-2 review found. Off by default so every other test sees the real
   * dialog.
   */
  dialogsAlwaysMounted: false,
  /** One router for every render, as Next's is, so `replace` can be asserted. */
  router: { replace: vi.fn(), push: vi.fn() },
}));

vi.mock('next/navigation', () => ({
  usePathname: () => '/api',
  useRouter: () => state.router,
  useSearchParams: () => state.search,
  notFound: () => {
    throw new Error('NEXT_NOT_FOUND');
  },
  redirect: (to: string) => {
    throw new Error(`NEXT_REDIRECT:${to}`);
  },
}));

vi.mock('next/link', () => ({
  __esModule: true,
  default: (props: ComponentProps<'a'> & { children?: ReactNode }) => {
    const { children, ...rest } = props;
    return <a {...rest}>{children}</a>;
  },
}));

// The async queries in this file wait on a whole console render (fetch mocks,
// effects, a portal). The default 1 s budget failed on a loaded CI runner —
// Pipeline run 34754339201, "Unable to find role=dialog" after 1148 ms — while
// the same test passes in about 300 ms on an idle one. Nothing asserted here
// is about speed, so the budget reflects shared CI hardware, not a looser test.
configure({ asyncUtilTimeout: 5000 });

vi.mock('@/components/admin/AdminDialog', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/components/admin/AdminDialog')>();
  return {
    ...real,
    AdminDialog: (props: Parameters<typeof real.AdminDialog>[0]) => {
      if (state.dialogsAlwaysMounted && !props.open) {
        return <div data-closed="true">{props.children}</div>;
      }
      // Rendered as an ELEMENT, not called as a function (2026-09-13). Calling
      // it inlined AdminDialog's hooks into this mock's fiber on open renders
      // only, so hooks ran conditionally and React logged "Expected static
      // flag was missing" on every run of the show-once tests. The secret
      // lives in the parent dialog's state (CreateKeyDialog, WebhookDialog),
      // so the closed-but-mounted assertions still test what they claim.
      return <real.AdminDialog {...props} />;
    },
  };
});

vi.mock('@/components/devplatform/server', () => ({
  consoleSession: async () => state.session,
}));

import DeveloperConsoleLayout from '@/app/api/layout';
import { ConsoleShell } from '@/components/devplatform/ConsoleShell';
import {
  API_CONSOLE_ACCESS,
  resolveConsoleSession,
} from '@/components/devplatform/session';
import { consoleNav, tabAllowed, tabFromQuery } from '@/components/devplatform/nav';
import {
  KEY_ENV_VAR,
  curlSnippet,
  javascriptSnippet,
  pythonSnippet,
  shellQuote,
  snippetBody,
} from '@/components/devplatform/snippets';
import { SCOPES } from '@/components/devplatform/types';
import { USAGE_LIMIT_KEYS, limitsChanges } from '@/components/devplatform/Limits';
import { consolePaths } from '@/components/devplatform/paths';
import type { Me } from '@/components/admin/api';

const ADMIN: Me = {
  user: { id: 1, name: 'Grace Hopper', email: 'grace@corp.com' },
  workspace: { id: 'w1', name: 'Corp Workspace', role: 'admin' },
  capabilities: [
    'members.read',
    API_CONSOLE_ACCESS,
    'api.projects.read',
    'api.projects.manage',
    'api.keys.create',
    'api.keys.revoke',
    'api.usage.read',
    'api.logs.read',
    'api.webhooks.manage',
  ],
  features: {},
};

const SUPER_ADMIN: Me = {
  ...ADMIN,
  workspace: { ...ADMIN.workspace, role: 'super_admin' },
  capabilities: [...ADMIN.capabilities, 'api.models.manage', 'api.limits.manage'],
};

const MEMBER: Me = {
  ...ADMIN,
  workspace: { ...ADMIN.workspace, role: 'member' },
  capabilities: [],
};

/** A `GET /overview` answer in console_api's real shape. */
function overviewOf(stats: { projects?: number; active_keys?: number; requests?: number }) {
  return {
    workspace: { id: 'w1', name: 'Corp Workspace' },
    stats: {
      projects: stats.projects ?? 0,
      active_projects: stats.projects ?? 0,
      keys: stats.active_keys ?? 0,
      active_keys: stats.active_keys ?? 0,
      models: 1,
      today: {
        day: '2026-09-13',
        requests: stats.requests ?? 0,
        input_tokens: 0,
        output_tokens: 0,
        errors: 0,
      },
    },
    capabilities: {},
  };
}

/** Answer the console's own BFF from a path → body table. */
function serve(routes: Record<string, unknown>) {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL, init?: RequestInit) => {
      const full = String(url);
      calls.push({ url: full, init: init ?? {} });
      const path = full.replace('/api/devplatform/', '').split('?')[0] as string;
      const body = routes[path];
      if (body === undefined) {
        return new Response(JSON.stringify({ message: 'Unknown console endpoint.' }), {
          status: 404,
          headers: { 'content-type': 'application/json' },
        });
      }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );
  return calls;
}

beforeEach(() => {
  state.search = new URLSearchParams();
  state.session = { state: 'allowed', me: ADMIN };
  state.dialogsAlwaysMounted = false;
  state.router.replace.mockClear();
  state.router.push.mockClear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// The server-side gate
// ---------------------------------------------------------------------------

describe('resolving the console session on the server', () => {
  it('refuses a signed-in member who does not hold api.console.access', async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            user: { id: 2, name: 'Bob', email: 'bob@corp.com' },
            workspace: { id: 'w1', name: 'Corp', role: 'member' },
            capabilities: ['workspace.read'],
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
    );
    const session = await resolveConsoleSession('ts_session=abc', fetchImpl);
    expect(session.state).toBe('refused');
  });

  it('admits an account that holds api.console.access', async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            user: { id: 1, name: 'Grace', email: 'grace@corp.com' },
            workspace: { id: 'w1', name: 'Corp', role: 'admin' },
            capabilities: [API_CONSOLE_ACCESS],
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
    );
    const session = await resolveConsoleSession('ts_session=abc', fetchImpl);
    expect(session.state).toBe('allowed');
  });

  it('does not ask the orchestrator at all when there is no session cookie', async () => {
    const fetchImpl = vi.fn();
    const session = await resolveConsoleSession(null, fetchImpl as unknown as typeof fetch);
    expect(session.state).toBe('signed-out');
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('treats a 401 as signed out rather than as a refusal', async () => {
    const fetchImpl = vi.fn(
      async () => new Response('{}', { status: 401, headers: { 'content-type': 'application/json' } }),
    );
    const session = await resolveConsoleSession('ts_session=stale', fetchImpl);
    expect(session.state).toBe('signed-out');
  });

  it('says the server could not be reached instead of pretending the console is gone', async () => {
    const thrown = vi.fn(async () => {
      throw new TypeError('fetch failed');
    });
    await expect(
      resolveConsoleSession('ts_session=abc', thrown as unknown as typeof fetch),
    ).resolves.toMatchObject({ state: 'unavailable' });

    const garbage = vi.fn(
      async () => new Response('not json', { status: 200 }),
    );
    await expect(
      resolveConsoleSession('ts_session=abc', garbage),
    ).resolves.toMatchObject({ state: 'unavailable' });

    const wrongShape = vi.fn(
      async () =>
        new Response(JSON.stringify({ hello: 'world' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    );
    await expect(
      resolveConsoleSession('ts_session=abc', wrongShape),
    ).resolves.toMatchObject({ state: 'unavailable' });
  });

  it('sends the session cookie and nothing else it was not given', async () => {
    const seen: { url: string; init: RequestInit }[] = [];
    const fetchImpl = (async (url: string | URL, init?: RequestInit) => {
      seen.push({ url: String(url), init: init ?? {} });
      return new Response(
        JSON.stringify({
          user: { id: 1, name: 'G', email: 'g@c.com' },
          workspace: { id: 'w1', name: 'Corp', role: 'admin' },
          capabilities: [API_CONSOLE_ACCESS],
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
    }) as unknown as typeof fetch;

    await resolveConsoleSession('ts_session=abc', fetchImpl);
    expect(seen).toHaveLength(1);
    expect(seen[0]!.url).toContain('/auth/me');
    const headers = seen[0]!.init.headers as Record<string, string>;
    expect(headers.cookie).toBe('ts_session=abc');
    // Only the one cookie crosses the hop, never the browser's whole jar.
    expect(Object.keys(headers)).toEqual(['cookie']);
  });
});

describe('the /api page gate', () => {
  async function mountLayout() {
    const element = await DeveloperConsoleLayout({
      children: <p>console content</p>,
    });
    render(element);
  }

  it('answers a member with a 404, not with a hidden menu item', async () => {
    state.session = { state: 'refused' };
    await expect(mountLayout()).rejects.toThrow('NEXT_NOT_FOUND');
  });

  it('sends a signed-out visitor to sign in', async () => {
    state.session = { state: 'signed-out' };
    await expect(mountLayout()).rejects.toThrow('NEXT_REDIRECT:/login');
  });

  it('renders the console for an account that holds the capability', async () => {
    state.session = { state: 'allowed', me: ADMIN };
    await mountLayout();
    expect(screen.getByText('console content')).toBeTruthy();
  });

  it('says the server could not be reached rather than 404 when it cannot ask', async () => {
    state.session = { state: 'unavailable', reason: 'The server could not be reached.' };
    await mountLayout();
    expect(screen.getByText(/could not be reached/i)).toBeTruthy();
    expect(screen.queryByText('console content')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Navigation (CONTRACT §17)
// ---------------------------------------------------------------------------

describe('the console navigation', () => {
  it('carries every section the contract names', () => {
    const labels = consoleNav(SUPER_ADMIN).flatMap((g) => g.items.map((i) => i.label));
    for (const expected of [
      'Overview',
      'Projects',
      'API keys',
      'Models',
      'Playground',
      'Usage',
      'Request logs',
      'Webhooks',
      'Limits',
      'Documentation',
      'Settings',
    ]) {
      expect(labels).toContain(expected);
    }
  });

  it('hides Limits from an admin, because that capability is super-admin only', () => {
    const labels = consoleNav(ADMIN).flatMap((g) => g.items.map((i) => i.label));
    expect(labels).not.toContain('Limits');
    expect(labels).toContain('Projects');
  });

  it('falls back to Overview for a tab that does not exist or is not allowed', () => {
    expect(tabFromQuery('projects')).toBe('projects');
    expect(tabFromQuery('nonsense')).toBe('overview');
    expect(tabFromQuery(null)).toBe('overview');
    expect(tabAllowed(ADMIN, 'limits')).toBe(false);
    expect(tabAllowed(SUPER_ADMIN, 'limits')).toBe(true);
  });

  it('has nothing at all for a member', () => {
    // Belt and braces behind the server gate: even handed a member, the nav
    // draws no console section they could click.
    const labels = consoleNav(MEMBER).flatMap((g) => g.items.map((i) => i.label));
    expect(labels).not.toContain('Projects');
    expect(labels).not.toContain('API keys');
  });

  it('links out to the documentation, the admin area and the chat', async () => {
    serve({ overview: overviewOf({}) });
    render(<ConsoleShell me={SUPER_ADMIN} />);
    await waitFor(() => expect(screen.getAllByText('Overview').length).toBeGreaterThan(0));
    expect(document.querySelector('a[href="/docs"]')).not.toBeNull();
    expect(document.querySelector('a[href="/admin"]')).not.toBeNull();
    expect(document.querySelector('a[href="/"]')).not.toBeNull();
    expect(document.querySelector('a[href="/api?tab=keys"]')).not.toBeNull();
  });

  it('renders a demoted admin Overview when their bookmarked tab is gone', async () => {
    state.search = new URLSearchParams('tab=limits');
    serve({ overview: overviewOf({}) });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByText(/No API key yet/i)).toBeTruthy());
  });
});

// ---------------------------------------------------------------------------
// Empty states
// ---------------------------------------------------------------------------

describe('every section has an empty state and invents no numbers', () => {
  const cases: { tab: string; routes: Record<string, unknown>; expect: RegExp }[] = [
    {
      tab: '',
      routes: { overview: overviewOf({}) },
      expect: /No API key yet/i,
    },
    { tab: 'projects', routes: { projects: { projects: [] } }, expect: /No projects yet/i },
    { tab: 'keys', routes: { projects: { projects: [] } }, expect: /No projects, so no keys/i },
    { tab: 'models', routes: { models: { models: [] } }, expect: /No models are published/i },
    { tab: 'playground', routes: { models: { models: [] } }, expect: /No chat model is published/i },
    { tab: 'usage', routes: { projects: { projects: [] } }, expect: /No projects to measure/i },
    { tab: 'logs', routes: { projects: { projects: [] } }, expect: /No projects to log/i },
    { tab: 'webhooks', routes: { projects: { projects: [] } }, expect: /No projects to notify/i },
    { tab: 'limits', routes: { projects: { projects: [] } }, expect: /No projects to limit/i },
  ];

  for (const testCase of cases) {
    it(`shows one for ${testCase.tab || 'overview'}`, async () => {
      state.search = new URLSearchParams(testCase.tab ? `tab=${testCase.tab}` : '');
      serve(testCase.routes);
      render(<ConsoleShell me={SUPER_ADMIN} />);
      await waitFor(() => expect(screen.getByText(testCase.expect)).toBeTruthy());
      // Nothing anywhere claims a figure it was not given.
      expect(screen.queryByText('0 requests')).toBeNull();
    });
  }

  it('shows the endpoint on Settings without asking the server for it', async () => {
    state.search = new URLSearchParams('tab=settings');
    serve({});
    render(<ConsoleShell me={SUPER_ADMIN} />);
    await waitFor(() => expect(screen.getByText('Endpoint')).toBeTruthy());
    expect(screen.getByText(/\/v1$/)).toBeTruthy();
  });

  it("renders the overview figures from console_api's own stats", async () => {
    serve({ overview: overviewOf({ projects: 2, active_keys: 3, requests: 41 }) });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByText('Requests today')).toBeTruthy());
    const row = screen.getByText('Requests today').parentElement as HTMLElement;
    await waitFor(() => expect(within(row).getByText('41')).toBeTruthy());
  });

  it('renders an overview that failed to load as an em dash, never as zero', async () => {
    serve({});
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByText('Unknown console endpoint.')).toBeTruthy());
    const row = screen.getByText('Requests today').parentElement as HTMLElement;
    expect(within(row).getByText('—')).toBeTruthy();
    expect(within(row).queryByText('0')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Tables
// ---------------------------------------------------------------------------

const PROJECT = {
  id: 'proj_0123456789abcdef01234567',
  name: 'Billing assistant',
  environment: 'live',
  status: 'active',
  allowed_models: [],
  allowed_origins: [],
  ip_allowlist: [],
  retention_days: null,
  metadata: {},
  limits: {
    rpm: null,
    input_tpm: null,
    output_tpm: null,
    max_concurrency: null,
    daily_token_quota: null,
    max_input_tokens: null,
    max_output_tokens: null,
  },
  created_by: 1,
  created_at: '2026-09-01T10:00:00Z',
  disabled_at: null,
};

describe('the projects table', () => {
  it('renders a row per project with its id and environment', async () => {
    state.search = new URLSearchParams('tab=projects');
    serve({
      projects: {
        projects: [
          PROJECT,
          { ...PROJECT, id: 'proj_second', name: 'Scratch', environment: 'test' },
        ],
      },
    });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByText('Billing assistant')).toBeTruthy());
    expect(screen.getByText('Scratch')).toBeTruthy();
    expect(screen.getByText(PROJECT.id)).toBeTruthy();
    expect(screen.getByText('Live')).toBeTruthy();
    expect(screen.getByText('Test')).toBeTruthy();
  });

  it('announces the result in the live region', async () => {
    state.search = new URLSearchParams('tab=projects');
    serve({ projects: { projects: [PROJECT] } });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() =>
      expect(screen.getByTestId('console-status').textContent).toBe('1 project.'),
    );
    const region = screen.getByTestId('console-status');
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(region.getAttribute('role')).toBe('status');
  });
});

// ---------------------------------------------------------------------------
// The scope vocabulary
// ---------------------------------------------------------------------------

describe('the scopes the console offers', () => {
  it('is exactly the closed vocabulary the server declares, and nothing more', () => {
    // orchestrator/app/apiplatform/scopes.py: Scope has these seven members
    // (the last three since 2026-09-13, with the embeddings, rerank and speech
    // endpoints) and `validate` raises UnknownScopeError for anything else, so
    // an eighth box on this form would be a key the platform refuses to mint.
    expect(SCOPES.map((s) => s.id)).toEqual([
      'models.read',
      'responses.read',
      'responses.write',
      'usage.read',
      'embeddings.write',
      'rerank.write',
      'audio.write',
    ]);
  });

  it("repeats the server's own description of each one", () => {
    const hints = Object.fromEntries(SCOPES.map((s) => [s.id, s.hint]));
    expect(hints['models.read']).toBe('List the models this key may use.');
    expect(hints['responses.read']).toBe('Read responses created by this project.');
    expect(hints['responses.write']).toBe('Create and cancel responses.');
    expect(hints['usage.read']).toBe('Read this project’s usage counters.');
    expect(hints['embeddings.write']).toBe('Create embeddings.');
    expect(hints['rerank.write']).toBe('Rerank documents against a query.');
    expect(hints['audio.write']).toBe('Transcribe audio.');
  });

  it('names no webhook scope, because the platform defines none', () => {
    expect(SCOPES.map((s) => s.id).join(' ')).not.toContain('webhook');
  });
});

// ---------------------------------------------------------------------------
// The show-once key flow
// ---------------------------------------------------------------------------

describe('creating an API key', () => {
  const posted: unknown[] = [];

  async function openDialog() {
    posted.length = 0;
    state.search = new URLSearchParams('tab=keys');
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string | URL, init?: RequestInit) => {
        const path = String(url).replace('/api/devplatform/', '').split('?')[0];
        if (path === 'projects') {
          return new Response(JSON.stringify({ projects: [PROJECT] }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        if (path === consolePaths.keys(PROJECT.id) && (init?.method ?? 'GET') === 'GET') {
          return new Response(JSON.stringify({ keys: [] }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        if (path === consolePaths.keys(PROJECT.id)) {
          posted.push(JSON.parse(String(init?.body)));
          return new Response(
            JSON.stringify({
              secret: 'tsk_live_0123456789abcdef_SuperSecretValue123456',
              key: {
                id: 'key_1',
                name: 'Production server',
                public_id: '0123456789abcdef',
                last_four: '3456',
                project_id: PROJECT.id,
                environment: 'live',
                scopes: ['models.read', 'responses.write'],
                status: 'active',
                created_at: '2026-09-13T09:00:00Z',
                last_used_at: null,
                expires_at: null,
                revoked_at: null,
              },
            }),
            { status: 200, headers: { 'content-type': 'application/json' } },
          );
        }
        return new Response('{}', {
          status: 404,
          headers: { 'content-type': 'application/json' },
        });
      }),
    );
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() =>
      expect(screen.getByText(/No keys in this project/i)).toBeTruthy(),
    );
    const user = userEvent.setup();
    await user.click(screen.getAllByRole('button', { name: /create key/i })[0]!);
    const dialog = await screen.findByRole('dialog');
    return { user, dialog };
  }

  it('shows the plaintext key exactly once, and says it cannot be shown again', async () => {
    const { user, dialog } = await openDialog();
    await user.type(within(dialog).getByLabelText('Name'), 'Production server');
    await user.click(within(dialog).getByRole('button', { name: 'Create key' }));

    const secret = await screen.findByTestId('created-key-secret');
    expect(secret.textContent).toBe(
      'tsk_live_0123456789abcdef_SuperSecretValue123456',
    );
    expect(screen.getByText(/shown once and cannot be retrieved later/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /copy key/i })).toBeTruthy();
  });

  it('drops the secret when the dialog is dismissed, with no way back to it', async () => {
    const { user, dialog } = await openDialog();
    await user.type(within(dialog).getByLabelText('Name'), 'Production server');
    await user.click(within(dialog).getByRole('button', { name: 'Create key' }));
    await screen.findByTestId('created-key-secret');

    await user.click(screen.getByRole('button', { name: 'Done' }));

    await waitFor(() => expect(screen.queryByTestId('created-key-secret')).toBeNull());
    expect(document.body.textContent).not.toContain('SuperSecretValue123456');
    // There is no reveal-later control anywhere in the console.
    expect(screen.queryByRole('button', { name: /reveal|show key|show secret/i })).toBeNull();
  });

  it('reopens clean rather than bringing the last key back', async () => {
    const { user, dialog } = await openDialog();
    await user.type(within(dialog).getByLabelText('Name'), 'Production server');
    await user.click(within(dialog).getByRole('button', { name: 'Create key' }));
    await screen.findByTestId('created-key-secret');
    await user.click(screen.getByRole('button', { name: 'Done' }));

    await user.click(screen.getAllByRole('button', { name: /create key/i })[0]!);
    const again = await screen.findByRole('dialog');
    expect(within(again).queryByTestId('created-key-secret')).toBeNull();
    expect((within(again).getByLabelText('Name') as HTMLInputElement).value).toBe('');
  });
});

// ---------------------------------------------------------------------------
// The playground never touches a key
// ---------------------------------------------------------------------------

const MODEL = {
  id: 'techsara-35b',
  object: 'model',
  owned_by: 'techsara',
  status: 'available',
  capabilities: { chat: true, streaming: true, vision: true, tools: false, embeddings: false },
  max_input_tokens: 1000000,
  max_output_tokens: 8192,
  enabled: true,
};

describe('the playground', () => {
  it('offers no field for an API key and stores none', async () => {
    state.search = new URLSearchParams('tab=playground');
    serve({
      models: {
        models: [
          MODEL,
        ],
      },
    });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByLabelText('Input')).toBeTruthy());
    expect(screen.queryByLabelText(/api key/i)).toBeNull();
    expect(screen.queryByPlaceholderText(/tsk_live/i)).toBeNull();
    expect(Object.keys(window.localStorage)).toHaveLength(0);
    expect(screen.getByText(/never asks for, stores or sends an API key/i)).toBeTruthy();
  });

  it('offers cURL, Python and JavaScript, each reading the key from the environment', async () => {
    state.search = new URLSearchParams('tab=playground');
    serve({
      models: {
        models: [
          MODEL,
        ],
      },
    });
    render(<ConsoleShell me={ADMIN} />);
    const snippet = await screen.findByTestId('playground-snippet');
    expect(snippet.textContent).toContain(`$${KEY_ENV_VAR}`);
    expect(snippet.textContent).not.toContain('tsk_live_');

    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: 'Python' }));
    await waitFor(() =>
      expect(screen.getByTestId('playground-snippet').textContent).toContain('httpx'),
    );
    expect(screen.getByTestId('playground-snippet').textContent).toContain(KEY_ENV_VAR);
  });
});

describe('the copyable snippets', () => {
  const request = {
    baseUrl: 'https://ai.techsarasolutions.com',
    model: 'techsara-35b',
    input: "What's new?",
    instructions: 'Answer in British English.',
    stream: true,
    temperature: 0.2,
    maxOutputTokens: 512,
  };

  it('never contains a key, in any language', () => {
    for (const snippet of [
      curlSnippet(request),
      pythonSnippet(request),
      javascriptSnippet(request),
    ]) {
      expect(snippet).toContain(KEY_ENV_VAR);
      expect(snippet).not.toMatch(/tsk_(live|test)_[A-Za-z0-9]/);
    }
  });

  it('points at /v1/responses on this deployment', () => {
    expect(curlSnippet(request)).toContain(
      'https://ai.techsarasolutions.com/v1/responses',
    );
    expect(pythonSnippet(request)).toContain(
      '"https://ai.techsarasolutions.com/v1/responses"',
    );
    expect(javascriptSnippet(request)).toContain(
      '"https://ai.techsarasolutions.com/v1/responses"',
    );
  });

  it('survives an apostrophe in the prompt, which a shell otherwise breaks on', () => {
    expect(shellQuote("What's new?")).toBe(`'What'\\''s new?'`);
    // The real assertion: take the -d argument back apart the way a POSIX
    // shell would, and the JSON must come out whole. Without the escape the
    // quoting closes early and the snippet a person pastes does not run.
    const curl = curlSnippet(request);
    const quoted = curl.slice(curl.indexOf(" -d '") + 4);
    const unquoted = quoted.slice(1, -1).split(`'\\''`).join("'");
    expect(JSON.parse(unquoted)).toEqual(snippetBody(request));
    expect(unquoted).toContain("What's new?");
  });

  it('omits a parameter the caller did not set rather than sending null', () => {
    const body = snippetBody({
      baseUrl: 'https://x.test',
      model: 'm',
      input: 'hi',
      temperature: null,
      maxOutputTokens: null,
    });
    expect(body).toEqual({ model: 'm', input: 'hi' });
    expect('temperature' in body).toBe(false);
    expect('stream' in body).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Wave-2 review fixes (2026-09-13)
// ---------------------------------------------------------------------------

const jsonResponse = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

/** Route a stubbed fetch by `METHOD path` (query stripped) to a handler. */
function route(
  handlers: Record<string, (url: string, init: RequestInit) => Response | Promise<Response>>,
) {
  const calls: { method: string; url: string; init: RequestInit }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL, init?: RequestInit) => {
      const full = String(url);
      const method = (init?.method ?? 'GET').toUpperCase();
      calls.push({ method, url: full, init: init ?? {} });
      const path = full.replace('/api/devplatform/', '').split('?')[0] as string;
      const handler = handlers[`${method} ${path}`];
      if (!handler) return jsonResponse({ message: 'Unknown console endpoint.' }, 404);
      return handler(full, init ?? {});
    }),
  );
  return calls;
}

describe('the server-side gate under a stalled orchestrator', () => {
  it('gives up and says the server could not be reached when /auth/me never answers', async () => {
    let seen: RequestInit | undefined;
    const never = ((_url: string, init?: RequestInit) => {
      seen = init;
      return new Promise<Response>(() => undefined);
    }) as unknown as typeof fetch;
    const started = Date.now();
    const session = await resolveConsoleSession('ts_session=abc', never, 30);
    expect(session.state).toBe('unavailable');
    expect(Date.now() - started).toBeLessThan(2000);
    expect(seen?.redirect).toBe('manual');
    expect(seen?.signal?.aborted).toBe(true);
  });

  it('gives up when the headers arrive and the body never finishes', async () => {
    const stalledBody = (async () =>
      ({
        status: 200,
        ok: true,
        json: () => new Promise(() => undefined),
      }) as unknown as Response) as unknown as typeof fetch;
    const session = await resolveConsoleSession('ts_session=abc', stalledBody, 30);
    expect(session.state).toBe('unavailable');
  });

  it('does not follow a redirect with the session cookie attached', async () => {
    const redirected = (async () =>
      new Response(null, { status: 302, headers: { location: 'http://elsewhere/' } })) as unknown as typeof fetch;
    const session = await resolveConsoleSession('ts_session=abc', redirected, 1000);
    expect(session.state).toBe('unavailable');
  });
});

describe('the show-once key after the dialog closes', () => {
  const SECRET = 'tsk_live_0123456789abcdef_SuperSecretValue123456';
  const KEY = {
    id: 'key_1',
    project_id: PROJECT.id,
    name: 'Production server',
    public_id: '0123456789abcdef',
    last_four: '3456',
    environment: 'live',
    scopes: ['models.read'],
    status: 'active',
    created_at: '2026-09-13T09:00:00Z',
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
  };

  function mountKeys(mint: () => Response | Promise<Response>) {
    state.search = new URLSearchParams('tab=keys');
    const calls = route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.keys(PROJECT.id)}`]: () => jsonResponse({ keys: [] }),
      [`POST ${consolePaths.keys(PROJECT.id)}`]: mint,
    });
    render(<ConsoleShell me={ADMIN} />);
    return calls;
  }

  it('drops the plaintext from component state on close, not on the next open', async () => {
    // With the dialog's children kept mounted while closed, only a cleared
    // STATE removes the secret from the document. The old effect returned
    // early on close and this assertion failed.
    state.dialogsAlwaysMounted = true;
    const calls = mountKeys(() => jsonResponse({ key: KEY, secret: SECRET }));
    await waitFor(() => expect(screen.getByText(/No keys in this project/i)).toBeTruthy());
    const user = userEvent.setup();
    await user.click(screen.getAllByRole('button', { name: /create key/i })[0]!);
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Name'), 'Production server');
    await user.click(within(dialog).getByRole('button', { name: 'Create key' }));
    await screen.findByTestId('created-key-secret');

    await user.click(screen.getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(document.body.textContent).not.toContain('SuperSecretValue'));
    expect(screen.queryByTestId('created-key-secret')).toBeNull();

    // And it went to the right place, with no tenant in the body.
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.url).toBe(`/api/devplatform/${consolePaths.keys(PROJECT.id)}`);
    expect(JSON.parse(String(post.init.body))).toEqual({
      name: 'Production server',
      scopes: [
        'models.read',
        'responses.read',
        'responses.write',
        'embeddings.write',
        'rerank.write',
        'audio.write',
      ],
    });
    expect(Object.keys(window.sessionStorage)).toHaveLength(0);
    expect(Object.keys(window.localStorage)).toHaveLength(0);
  });

  it('does not put back a key that was minted after the dialog was dismissed', async () => {
    state.dialogsAlwaysMounted = true;
    const pending = deferred<Response>();
    mountKeys(() => pending.promise);
    await waitFor(() => expect(screen.getByText(/No keys in this project/i)).toBeTruthy());
    const user = userEvent.setup();
    await user.click(screen.getAllByRole('button', { name: /create key/i })[0]!);
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('Name'), 'Production server');
    await user.click(within(dialog).getByRole('button', { name: 'Create key' }));
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    await act(async () => {
      pending.resolve(jsonResponse({ key: KEY, secret: SECRET }));
      await pending.promise;
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(document.body.textContent).not.toContain('SuperSecretValue');
  });
});

describe('a project-scoped panel after the project changes', () => {
  it("never shows the previous project's keys under the new project's name", async () => {
    state.search = new URLSearchParams('tab=keys');
    const ALPHA = { ...PROJECT, id: 'proj_a', name: 'Alpha' };
    const BRAVO = { ...PROJECT, id: 'proj_b', name: 'Bravo' };
    const bravoKeys = deferred<Response>();
    const keyRow = (id: string, name: string, project: string) => ({
      id,
      project_id: project,
      name,
      public_id: '0123456789abcdef',
      last_four: '0000',
      environment: 'live',
      scopes: [],
      status: 'active',
      created_at: null,
      last_used_at: null,
      expires_at: null,
      revoked_at: null,
    });
    route({
      'GET projects': () => jsonResponse({ projects: [ALPHA, BRAVO] }),
      'GET projects/proj_a/keys': () =>
        jsonResponse({ keys: [keyRow('key_a', 'ALPHA-KEY', 'proj_a')] }),
      'GET projects/proj_b/keys': () => bravoKeys.promise,
    });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByText('ALPHA-KEY')).toBeTruthy());

    fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'proj_b' } });
    // Bravo's answer has not arrived. Alpha's row must already be gone.
    expect((screen.getByLabelText('Project') as HTMLSelectElement).value).toBe('proj_b');
    expect(screen.queryByText('ALPHA-KEY')).toBeNull();

    await act(async () => {
      bravoKeys.resolve(jsonResponse({ keys: [keyRow('key_b', 'BRAVO-KEY', 'proj_b')] }));
      await bravoKeys.promise;
    });
    await waitFor(() => expect(screen.getByText('BRAVO-KEY')).toBeTruthy());
    expect(screen.queryByText('ALPHA-KEY')).toBeNull();
  });

  it('revokes through the project the key belongs to', async () => {
    state.search = new URLSearchParams('tab=keys');
    const calls = route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.keys(PROJECT.id)}`]: () =>
        jsonResponse({
          keys: [
            {
              id: 'key_9',
              project_id: PROJECT.id,
              name: 'Old server',
              public_id: '0123456789abcdef',
              last_four: '9999',
              environment: 'live',
              scopes: [],
              status: 'active',
              created_at: null,
              last_used_at: null,
              expires_at: null,
              revoked_at: null,
            },
          ],
        }),
      [`POST ${consolePaths.revokeKey(PROJECT.id, 'key_9')}`]: () => jsonResponse({ key: {} }),
    });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByText('Old server')).toBeTruthy());
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Actions for Old server' }));
    await user.click(await screen.findByRole('menuitem', { name: /revoke/i }));
    await user.click(await screen.findByRole('button', { name: 'Revoke' }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/key_9/revoke'))).toBe(true),
    );
    expect(calls.find((c) => c.method === 'POST')!.url).toBe(
      `/api/devplatform/projects/${PROJECT.id}/keys/key_9/revoke`,
    );
  });
});

describe('the limits form', () => {
  const NONE = {
    rpm: null,
    input_tpm: null,
    output_tpm: null,
    max_concurrency: null,
    daily_token_quota: null,
    max_input_tokens: null,
    max_output_tokens: null,
  };
  const blank = {
    rpm: '',
    input_tpm: '',
    output_tpm: '',
    max_concurrency: '',
    daily_token_quota: '',
    max_input_tokens: '',
    max_output_tokens: '',
  };

  it('sends a 0 as a 0, because zero means zero allowed', () => {
    expect(limitsChanges(NONE, { ...blank, rpm: '0' })).toEqual({ rpm: 0 });
  });

  it('accepts a 1,000,000-token output ceiling and refuses one above it, as the server does', () => {
    expect(limitsChanges(NONE, { ...blank, max_output_tokens: '1000000' })).toEqual({
      max_output_tokens: 1_000_000,
    });
    expect(() => limitsChanges(NONE, { ...blank, max_output_tokens: '1000001' })).toThrow(
      'Max output tokens per request must be at most 1,000,000.',
    );
  });

  it('sends nothing for a field left empty, so it keeps inheriting', () => {
    expect(limitsChanges(NONE, blank)).toEqual({});
  });

  it('sends only what changed', () => {
    expect(
      limitsChanges({ ...NONE, rpm: 60 }, { ...blank, rpm: '60', max_concurrency: '2' }),
    ).toEqual({ max_concurrency: 2 });
  });

  it('refuses to turn an emptied field into a zero', () => {
    expect(() => limitsChanges({ ...NONE, rpm: 60 }, blank)).toThrow(/cannot be emptied/);
  });

  it('refuses a value that is not a whole number', () => {
    expect(() => limitsChanges(NONE, { ...blank, rpm: '1.5' })).toThrow(/whole number/);
    expect(() => limitsChanges(NONE, { ...blank, max_output_tokens: '0' })).toThrow(/at least 1/);
  });

  it('shows a stored 0 as 0 and an inherited limit as empty, and PUTs under the project', async () => {
    state.search = new URLSearchParams('tab=limits');
    const calls = route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.limits(PROJECT.id)}`]: () =>
        jsonResponse({ limits: { ...NONE, rpm: 0 }, can_manage: true }),
      [`PUT ${consolePaths.limits(PROJECT.id)}`]: () =>
        jsonResponse({ limits: { ...NONE, rpm: 0, input_tpm: 5 } }),
    });
    render(<ConsoleShell me={SUPER_ADMIN} />);
    const rpm = (await screen.findByLabelText('Requests per minute')) as HTMLInputElement;
    await waitFor(() => expect(rpm.value).toBe('0'));
    const tpm = screen.getByLabelText('Input tokens per minute') as HTMLInputElement;
    expect(tpm.value).toBe('');
    expect(screen.getAllByText(/Saved: Platform default\./).length).toBeGreaterThan(0);

    fireEvent.change(tpm, { target: { value: '5' } });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Save limits' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    const put = calls.find((c) => c.method === 'PUT')!;
    expect(put.url).toBe(`/api/devplatform/projects/${PROJECT.id}/limits`);
    expect(JSON.parse(String(put.init.body))).toEqual({ input_tpm: 5 });
  });
});

// ---------------------------------------------------------------------------
// PUBLIC_API_ENFORCE_LIMITS (owner decision 2026-09-13: unlimited by default)
// ---------------------------------------------------------------------------

describe('usage limits when the server does not enforce them', () => {
  const NONE = {
    rpm: null,
    input_tpm: null,
    output_tpm: null,
    max_concurrency: null,
    daily_token_quota: null,
    max_input_tokens: null,
    max_output_tokens: null,
  };
  const blank = {
    rpm: '',
    input_tpm: '',
    output_tpm: '',
    max_concurrency: '',
    daily_token_quota: '',
    max_input_tokens: '',
    max_output_tokens: '',
  };
  const USAGE_LABELS = [
    'Requests per minute',
    'Input tokens per minute',
    'Output tokens per minute',
    'Concurrent requests',
    'Daily token quota',
  ];

  it('reads the flag wherever the orchestrator puts it, and reports a pre-switch answer as unknown', () => {
    expect(limitsEnforcement({ limits: NONE, enforced: false })).toBe(false);
    expect(limitsEnforcement({ limits: { ...NONE, enforced: false } })).toBe(false);
    expect(limitsEnforcement({ stats: { limits_enforced: false } })).toBe(false);
    expect(limitsEnforcement({ limits_enforced: true })).toBe(true);
    expect(limitsEnforcement(undefined, { enforce_limits: false })).toBe(false);
    expect(limitsEnforcement({ limits: NONE, can_manage: true })).toBeNull();
    expect(limitsEnforcement(null, undefined)).toBeNull();
  });

  it('says Unlimited for a usage limit only when the server said so, whatever is stored', () => {
    expect(usageLimitText(60, false)).toBe('Unlimited');
    expect(usageLimitText(null, false)).toBe('Unlimited');
    expect(usageLimitText(60, true)).toBe('60');
    expect(usageLimitText(0, true)).toBe('0 — nothing allowed');
    // A pre-switch orchestrator enforced, so unknown keeps the enforced words.
    expect(usageLimitText(null, null)).toBe('Platform default');
  });

  it('never sends a usage limit while they are off, but still sends a per-request ceiling', () => {
    expect(USAGE_LIMIT_KEYS).toEqual([
      'rpm',
      'input_tpm',
      'output_tpm',
      'max_concurrency',
      'daily_token_quota',
    ]);
    expect(
      limitsChanges(NONE, { ...blank, rpm: '5', max_output_tokens: '2048' }, true),
    ).toEqual({ max_output_tokens: 2048 });
    // And with the switch on, the same draft sends both — unchanged behaviour.
    expect(
      limitsChanges(NONE, { ...blank, rpm: '5', max_output_tokens: '2048' }, false),
    ).toEqual({ rpm: 5, max_output_tokens: 2048 });
  });

  it('renders the Limits tab as Unlimited with no usage-limit inputs, no stored numbers, and saves only the per-request ceiling', async () => {
    state.search = new URLSearchParams('tab=limits');
    const calls = route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      // A stored 60 from before the switch: it must not be drawn as a ceiling.
      [`GET ${consolePaths.limits(PROJECT.id)}`]: () =>
        jsonResponse({ limits: { ...NONE, rpm: 60 }, enforced: false, can_manage: true }),
      [`PUT ${consolePaths.limits(PROJECT.id)}`]: () =>
        jsonResponse({ limits: { ...NONE, max_output_tokens: 2048 }, enforced: false }),
    });
    render(<ConsoleShell me={SUPER_ADMIN} />);
    const maxOut = (await screen.findByLabelText(
      'Max output tokens per request',
    )) as HTMLInputElement;
    await waitFor(() => expect(screen.getAllByText('Unlimited')).toHaveLength(5));
    for (const label of USAGE_LABELS) {
      expect(screen.queryByLabelText(label)).toBeNull();
      const row = screen.getByText(label).parentElement as HTMLElement;
      expect(within(row).getByText('Unlimited')).toBeTruthy();
    }
    expect(screen.queryByText(/Platform default 60/)).toBeNull();
    expect(screen.queryByText(/Saved: 60/)).toBeNull();
    expect(screen.queryByText(/They are enforced in the orchestrator/)).toBeNull();
    expect(screen.getByText(/The public API is unlimited/)).toBeTruthy();
    expect(screen.getByLabelText('Max input tokens per request')).toBeTruthy();

    fireEvent.change(maxOut, { target: { value: '2048' } });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Save limits' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    const put = calls.find((c) => c.method === 'PUT')!;
    expect(JSON.parse(String(put.init.body))).toEqual({ max_output_tokens: 2048 });
  });

  it('keeps every enforced field and its stored number when the server says the limits are on', async () => {
    state.search = new URLSearchParams('tab=limits');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.limits(PROJECT.id)}`]: () =>
        jsonResponse({ limits: { ...NONE, rpm: 60 }, enforced: true, can_manage: true }),
    });
    render(<ConsoleShell me={SUPER_ADMIN} />);
    const rpm = (await screen.findByLabelText('Requests per minute')) as HTMLInputElement;
    await waitFor(() => expect(rpm.value).toBe('60'));
    for (const label of USAGE_LABELS) expect(screen.getByLabelText(label)).toBeTruthy();
    expect(screen.getByText(/Saved: 60\./)).toBeTruthy();
    expect(screen.getByText(/They are enforced in the orchestrator/)).toBeTruthy();
    expect(screen.queryByText('Unlimited')).toBeNull();
  });

  it('shows Unlimited in the project settings dialog instead of a stored rate', async () => {
    state.search = new URLSearchParams('tab=projects');
    serve({
      projects: {
        projects: [
          { ...PROJECT, limits: { ...PROJECT.limits, rpm: 60, enforced: false } },
        ],
      },
    });
    render(<ConsoleShell me={ADMIN} />);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole('button', { name: `Actions for ${PROJECT.name}` }),
    );
    await user.click(await screen.findByRole('menuitem', { name: /view settings/i }));
    const row = (await screen.findByText('Requests / minute')).parentElement as HTMLElement;
    expect(within(row).getByText('Unlimited')).toBeTruthy();
    expect(screen.getAllByText('Unlimited')).toHaveLength(5);
    expect(screen.queryByText('60')).toBeNull();
  });

  it('shows the stored rate in the project settings dialog when limits are enforced', async () => {
    state.search = new URLSearchParams('tab=projects');
    serve({
      projects: {
        projects: [{ ...PROJECT, limits: { ...PROJECT.limits, rpm: 60, enforced: true } }],
      },
    });
    render(<ConsoleShell me={ADMIN} />);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole('button', { name: `Actions for ${PROJECT.name}` }),
    );
    await user.click(await screen.findByRole('menuitem', { name: /view settings/i }));
    const row = (await screen.findByText('Requests / minute')).parentElement as HTMLElement;
    expect(within(row).getByText('60')).toBeTruthy();
    expect(screen.queryByText('Unlimited')).toBeNull();
  });

  it('says Unlimited on the overview when the limits are off', async () => {
    serve({ overview: { ...overviewOf({ active_keys: 1 }), limits_enforced: false } });
    render(<ConsoleShell me={ADMIN} />);
    const label = await screen.findByText('Usage limits');
    expect(within(label.parentElement as HTMLElement).getByText('Unlimited')).toBeTruthy();
    expect(screen.queryByText('Enforced per project')).toBeNull();
  });

  it('says the limits are enforced on the overview when they are on', async () => {
    const body = overviewOf({ active_keys: 1 });
    serve({ overview: { ...body, stats: { ...body.stats, limits_enforced: true } } });
    render(<ConsoleShell me={ADMIN} />);
    const label = await screen.findByText('Usage limits');
    expect(
      within(label.parentElement as HTMLElement).getByText('Enforced per project'),
    ).toBeTruthy();
    expect(screen.queryByText('Unlimited')).toBeNull();
  });

  it('draws no limits line on the overview when the server does not say, rather than guessing', async () => {
    serve({ overview: overviewOf({ active_keys: 1, requests: 3 }) });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByText('3')).toBeTruthy());
    expect(screen.queryByText('Usage limits')).toBeNull();
  });
});

describe('the usage and request-log queries', () => {
  it('asks for usage with the parameters the router reads, and no others', async () => {
    state.search = new URLSearchParams('tab=usage');
    const calls = route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      'GET usage': () =>
        jsonResponse({
          range: { days: 30, start: '2026-08-15', end: '2026-09-13' },
          series: [],
          totals: { requests: 0, input_tokens: 0, output_tokens: 0, errors: 0, rate_limited: 0, total_tokens: 0 },
          projects: [],
        }),
    });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(calls.some((c) => c.url.includes('usage'))).toBe(true));
    expect(calls.find((c) => c.url.includes('usage'))!.url).toBe('/api/devplatform/usage?days=30');

    // Wait for the project list to populate the select BEFORE choosing from
    // it (2026-09-13): setting a <select> to a value it has no option for is a
    // silent no-op, so under CPU load — the projects response landing after
    // the first usage call — the change did nothing, no project_id was ever
    // sent, and this test failed about one run in four.
    await waitFor(() =>
      expect(
        (screen.getByLabelText('Project') as HTMLSelectElement).querySelector(
          `option[value="${PROJECT.id}"]`,
        ),
      ).not.toBeNull(),
    );
    fireEvent.change(screen.getByLabelText('Project'), { target: { value: PROJECT.id } });
    fireEvent.change(screen.getByLabelText('Time range'), { target: { value: '7' } });
    await waitFor(() =>
      expect(calls.map((c) => c.url)).toContain(
        `/api/devplatform/usage?project_id=${PROJECT.id}&days=7`,
      ),
    );
    for (const call of calls) {
      expect(call.url).not.toMatch(/[?&](range|project)=/);
    }
  });

  it('asks for request logs under the project, bounded, with no offset', async () => {
    state.search = new URLSearchParams('tab=logs');
    const calls = route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.logs(PROJECT.id)}`]: () =>
        jsonResponse({
          project: { id: PROJECT.id, name: PROJECT.name },
          requests: [
            {
              id: 'resp_1',
              request_id: 'req_abc',
              model: 'techsara-35b',
              status: 'completed',
              background: false,
              streamed: true,
              input_tokens: 5,
              output_tokens: null,
              ttft_ms: 10,
              duration_ms: 100,
              error_code: '',
              metadata: {},
              key: { id: 'key_1', name: 'Server', last_four: '1234' },
              created_at: '2026-09-13T09:00:00Z',
              started_at: null,
              completed_at: null,
            },
          ],
        }),
    });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByText('req_abc')).toBeTruthy());
    expect(calls.find((c) => c.url.includes('/logs'))!.url).toBe(
      `/api/devplatform/projects/${PROJECT.id}/logs?limit=100`,
    );
    // An empty error_code is a success, shown by its status, not in red.
    expect(screen.getByText('completed')).toBeTruthy();
    // A null token count is not measured, never a zero.
    expect(screen.getByText('5 → —')).toBeTruthy();
  });
});

describe('webhook endpoints', () => {
  // 2026-09-13, wave-3 re-verify: a rewrite dropped the `secret` that
  // console_api.create_webhook returns exactly once, and this test asserted
  // the drop. Every console-created endpoint was then unverifiable. These pin
  // the show-once flow instead, the same one the key dialog uses.
  const WEBHOOK_SECRET = 'whsec_OnlyEverShownOnce_7f3a9c';
  const ENDPOINT = {
    id: 'whe_1',
    project_id: PROJECT.id,
    url: 'https://example.com/hook',
    events: ['response.completed', 'response.failed', 'response.cancelled'],
    status: 'active',
    include_output: false,
    has_secret: true,
    rotation_in_progress: false,
    created_at: '2026-09-13T09:00:00Z',
    last_delivery_at: null,
    last_delivery_status: '',
    consecutive_failures: 0,
  };

  function mountWebhooks(answer: () => Response | Promise<Response>) {
    state.search = new URLSearchParams('tab=webhooks');
    return route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.webhooks(PROJECT.id)}`]: () => jsonResponse({ webhooks: [] }),
      [`POST ${consolePaths.webhooks(PROJECT.id)}`]: answer,
    });
  }

  async function submitEndpoint() {
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(screen.getByText(/No endpoints in this project/i)).toBeTruthy());
    const user = userEvent.setup();
    // The toolbar's button comes first; a kept-mounted closed dialog adds a second.
    await user.click(screen.getAllByRole('button', { name: /add endpoint/i })[0]!);
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('HTTPS endpoint'), 'https://example.com/hook');
    await user.click(within(dialog).getByRole('button', { name: 'Add endpoint' }));
    return { user, dialog };
  }

  it('creates under the project and shows the signing secret once, in a copy box that says it cannot be shown again', async () => {
    const calls = mountWebhooks(() => jsonResponse({ webhook: ENDPOINT, secret: WEBHOOK_SECRET }));
    await submitEndpoint();

    const secret = await screen.findByTestId('created-webhook-secret');
    expect(secret.textContent).toBe(WEBHOOK_SECRET);
    expect(screen.getByRole('button', { name: /copy secret/i })).toBeTruthy();
    expect(screen.getByText(/shown once and cannot be retrieved later/i)).toBeTruthy();
    // Once: one element holds it, and nothing else on the page repeats it.
    expect(document.body.textContent!.split(WEBHOOK_SECRET)).toHaveLength(2);
    expect(screen.queryByText(/never displays it/i)).toBeNull();

    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.url).toBe(`/api/devplatform/projects/${PROJECT.id}/webhooks`);
    expect(JSON.parse(String(post.init.body))).toEqual({
      url: 'https://example.com/hook',
      events: ['response.completed', 'response.failed', 'response.cancelled'],
    });
  });

  it('drops the signing secret on dismissal and does not bring it back when the dialog reopens', async () => {
    state.dialogsAlwaysMounted = true;
    mountWebhooks(() => jsonResponse({ webhook: ENDPOINT, secret: WEBHOOK_SECRET }));
    const { user } = await submitEndpoint();
    await screen.findByTestId('created-webhook-secret');

    await user.click(screen.getByRole('button', { name: 'Done' }));
    // With the dialog kept mounted while closed, only clearing state on the
    // CLOSING edge removes the plaintext from the page.
    await waitFor(() => expect(document.body.textContent).not.toContain(WEBHOOK_SECRET));
    expect(screen.queryByTestId('created-webhook-secret')).toBeNull();
    expect(screen.queryByRole('button', { name: /reveal|show secret/i })).toBeNull();

    await user.click(screen.getAllByRole('button', { name: /add endpoint/i })[0]!);
    await waitFor(() =>
      expect((screen.getByLabelText('HTTPS endpoint') as HTMLInputElement).value).toBe(''),
    );
    expect(document.body.textContent).not.toContain(WEBHOOK_SECRET);
    expect(Object.keys(window.localStorage)).toHaveLength(0);
    expect(Object.keys(window.sessionStorage)).toHaveLength(0);
  });

  it('does not put back a signing secret that arrives after the dialog was dismissed', async () => {
    state.dialogsAlwaysMounted = true;
    const pending = deferred<Response>();
    mountWebhooks(() => pending.promise);
    const { user, dialog } = await submitEndpoint();
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    await act(async () => {
      pending.resolve(jsonResponse({ webhook: ENDPOINT, secret: WEBHOOK_SECRET }));
      await pending.promise;
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(document.body.textContent).not.toContain(WEBHOOK_SECRET);
  });
});

describe('the models table', () => {
  it("toggles a model with PUT, which is what console_api declares", async () => {
    state.search = new URLSearchParams('tab=models');
    const calls = route({
      'GET models': () => jsonResponse({ models: [MODEL], can_manage: true }),
      'PUT models/techsara-35b': () => jsonResponse({ model: { id: 'techsara-35b', enabled: false } }),
    });
    render(<ConsoleShell me={SUPER_ADMIN} />);
    const badges = await screen.findAllByRole('list', { name: 'techsara-35b capabilities' });
    expect(within(badges[0]!).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Chat',
      'Streaming',
      'Vision',
    ]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('switch', { name: /publish techsara-35b/i }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    expect(calls.find((c) => c.method === 'PUT')!.url).toBe('/api/devplatform/models/techsara-35b');
    expect(calls.some((c) => c.method === 'PATCH')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// The playground stream, driven end to end
// ---------------------------------------------------------------------------

/** One §10 frame, as SequencedEvents writes it. */
const frame = (name: string, sequence: number, payload: Record<string, unknown> = {}) =>
  `event: ${name}\ndata: ${JSON.stringify({ type: name, sequence_number: sequence, ...payload })}\n\n`;

function controlledStream() {
  const enc = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    stream,
    push: (text: string) => controller.enqueue(enc.encode(text)),
    close: () => controller.close(),
  };
}

describe('the playground stream', () => {
  function mountPlayground(answer: (init: RequestInit) => Response) {
    state.search = new URLSearchParams('tab=playground');
    const calls = route({
      'GET models': () => jsonResponse({ models: [MODEL], can_manage: false }),
      'POST playground/execute': (_url, init) => answer(init),
    });
    render(<ConsoleShell me={ADMIN} />);
    return calls;
  }

  async function runPrompt() {
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Input'), 'Say hello');
    await user.click(screen.getByRole('button', { name: 'Run' }));
    return user;
  }

  const inspectorRows = () =>
    Array.from(document.querySelectorAll('section[aria-label="Response"] ol li')).map((li) =>
      (li.textContent ?? '').replace(/\s+/g, ' ').trim(),
    );

  it('streams deltas into the output, lets the done text win, and numbers every frame', async () => {
    const s = controlledStream();
    const calls = mountPlayground(
      () =>
        new Response(s.stream, {
          status: 200,
          headers: { 'content-type': 'text/event-stream', 'x-request-id': 'req_play' },
        }),
    );
    await runPrompt();

    s.push(frame('response.created', 1, { response: { status: 'queued', usage: null } }));
    s.push(frame('response.in_progress', 2, { response: { status: 'in_progress', usage: null } }));
    // A frame split across two chunks, the way a proxy delivers it.
    const third = frame('response.output_text.delta', 3, { delta: 'Hel' });
    s.push(third.slice(0, 20));
    s.push(third.slice(20));
    s.push(frame('response.output_text.delta', 4, { delta: 'lo' }));
    s.push(frame('response.output_text.delta', 5, { delta: ' world' }));

    const output = await screen.findByTestId('playground-output');
    await waitFor(() => expect(output.textContent).toBe('Hello world'));
    expect(screen.getByText('req_play')).toBeTruthy();

    s.push(frame('response.output_text.done', 6, { text: 'Hello world!' }));
    s.push(
      frame('response.completed', 7, {
        response: {
          status: 'completed',
          usage: { input_tokens: 5, output_tokens: 3, total_tokens: 8 },
        },
      }),
    );
    s.close();

    await waitFor(() => expect(output.textContent).toBe('Hello world!'));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run' })).toBeTruthy());

    const rows = inspectorRows();
    expect(rows).toEqual([
      '1response.created',
      '2response.in_progress',
      '3response.output_text.delta×3',
      '6response.output_text.done',
      '7response.completed',
    ]);
    expect(rows.filter((r) => /response\.(completed|failed)|^\d+error$/.test(r))).toHaveLength(1);
    const total = screen.getByText('Total tokens').parentElement as HTMLElement;
    expect(within(total).getByText('8')).toBeTruthy();

    // The request went to the real router path, on the session, with no key.
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.url).toBe('/api/devplatform/playground/execute');
    const headers = post.init.headers as Record<string, string>;
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain('authorization');
    expect(JSON.parse(String(post.init.body))).toMatchObject({
      model: 'techsara-35b',
      input: 'Say hello',
      stream: true,
    });
  });

  it('shows usage as not measured when the terminal carries usage: null', async () => {
    const s = controlledStream();
    mountPlayground(
      () => new Response(s.stream, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    await runPrompt();
    s.push(frame('response.created', 1, { response: { usage: null } }));
    s.push(frame('response.output_text.done', 2, { text: 'x' }));
    s.push(frame('response.completed', 3, { response: { status: 'completed', usage: null } }));
    s.close();
    await waitFor(() => expect(inspectorRows()).toHaveLength(3));
    const total = screen.getByText('Total tokens').parentElement as HTMLElement;
    expect(within(total).getByText('—')).toBeTruthy();
  });

  it("shows the error terminal's own sentence", async () => {
    const s = controlledStream();
    mountPlayground(
      () => new Response(s.stream, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    await runPrompt();
    s.push(frame('response.created', 1, { response: { usage: null } }));
    s.push(
      frame('error', 2, { code: 'model_unavailable', message: 'The model is unavailable.', param: null }),
    );
    s.close();
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('The model is unavailable.'));
  });

  it('shows the CONTRACT §9 envelope sentence on a refusal, not a bare status', async () => {
    mountPlayground(() =>
      jsonResponse(
        { error: { message: 'Rate limit reached.', type: 'rate_limit_error', code: 'rate_limit_error' } },
        429,
        { 'x-request-id': 'req_429' },
      ),
    );
    await runPrompt();
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Rate limit reached.'));
    expect(screen.getByText('req_429')).toBeTruthy();
  });

  it('aborts the upstream request when Stop is pressed', async () => {
    const s = controlledStream();
    const calls = mountPlayground(
      () => new Response(s.stream, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    const user = await runPrompt();
    s.push(frame('response.created', 1, { response: { usage: null } }));
    await waitFor(() => expect(inspectorRows()).toHaveLength(1));
    const signal = calls.find((c) => c.method === 'POST')!.init.signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    await user.click(screen.getByRole('button', { name: 'Stop' }));
    expect(signal.aborted).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Visual QA, 2026-09-13: what the 1440px and 400px screenshots found
// ---------------------------------------------------------------------------
//
// jsdom applies no stylesheet, so these read the geometry the markup DECLARES:
// the fixed `<col>` widths that stay visible below `lg` (a column hidden below
// lg carries `hidden lg:table-cell`), and the wrapper class that lifts the
// table's desktop min-width on a phone. Every console table must leave its
// identity column readable on a 400px screen AND keep its actions on screen —
// the screenshot of the keys table at 400px showed the Key column alone, with
// the menu that revokes a leaked key scrolled out of reach.

/** A 400px phone less the 16px gutter on each side. */
const PHONE_TABLE_WIDTH = 400 - 32;
/** The narrowest identity column that still reads (a name plus a wrapped id). */
const READABLE_IDENTITY = 160;

function phoneGeometry(table: HTMLTableElement) {
  const hiddenOnPhone = (el: Element) => el.className.split(/\s+/).includes('hidden');
  const visible = Array.from(table.querySelectorAll('col')).filter((c) => !hiddenOnPhone(c));
  const fixed = visible.reduce((sum, col) => sum + (parseFloat(col.style.width) || 0), 0);
  // The inline min-width is the DESKTOP floor; below xl (so below lg too) a
  // wrapper must lift it, or the table keeps scrolling sideways past a 400px
  // screen.
  const lifted = table.closest('[class~="max-xl:[&_table]:!min-w-0"]') !== null;
  return { identity: PHONE_TABLE_WIDTH - fixed, lifted, hiddenOnPhone };
}

function expectReachableOnPhone(control: HTMLElement) {
  const table = control.closest('table') as HTMLTableElement;
  const { identity, lifted, hiddenOnPhone } = phoneGeometry(table);
  expect(lifted).toBe(true);
  expect(identity).toBeGreaterThanOrEqual(READABLE_IDENTITY);
  expect(hiddenOnPhone(control.closest('td') as HTMLElement)).toBe(false);
}

const KEY_ROW = {
  id: 'key_1',
  project_id: PROJECT.id,
  name: 'Edge server',
  public_id: '0e4005cb9284109d',
  last_four: 'LtSL',
  environment: 'test',
  scopes: ['models.read', 'responses.read', 'responses.write'],
  status: 'active',
  created_at: '2026-09-13T09:00:00Z',
  last_used_at: '2026-09-13T09:00:00Z',
  expires_at: null,
  revoked_at: null,
};

describe('the console tables on a 400px phone', () => {
  it('keeps the menu that revokes a key on screen, beside a readable key column', async () => {
    state.search = new URLSearchParams('tab=keys');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.keys(PROJECT.id)}`]: () => jsonResponse({ keys: [KEY_ROW] }),
    });
    render(<ConsoleShell me={ADMIN} />);
    const menu = await screen.findByRole('button', { name: 'Actions for Edge server' });
    expectReachableOnPhone(menu);
    // The key's status is what a person decides to revoke on: it stays too.
    expectReachableOnPhone(screen.getByText('Active'));
  });

  it('keeps the project row menu on screen', async () => {
    state.search = new URLSearchParams('tab=projects');
    serve({ projects: { projects: [PROJECT] } });
    render(<ConsoleShell me={ADMIN} />);
    expectReachableOnPhone(
      await screen.findByRole('button', { name: `Actions for ${PROJECT.name}` }),
    );
  });

  it('keeps the webhook row menu on screen', async () => {
    state.search = new URLSearchParams('tab=webhooks');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.webhooks(PROJECT.id)}`]: () =>
        jsonResponse({
          webhooks: [
            {
              id: 'whe_1',
              project_id: PROJECT.id,
              url: 'https://example.com/hook',
              events: ['response.completed'],
              status: 'active',
              include_output: false,
              has_secret: true,
              rotation_in_progress: false,
              created_at: null,
              last_delivery_at: null,
              last_delivery_status: '',
              consecutive_failures: 0,
              disabled_at: null,
            },
          ],
        }),
    });
    render(<ConsoleShell me={ADMIN} />);
    expectReachableOnPhone(
      await screen.findByRole('button', { name: 'Actions for https://example.com/hook' }),
    );
  });

  it("keeps a request log line's status and its copy button on screen", async () => {
    state.search = new URLSearchParams('tab=logs');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.logs(PROJECT.id)}`]: () =>
        jsonResponse({
          project: { id: PROJECT.id, name: PROJECT.name },
          requests: [
            {
              id: 'resp_1',
              request_id: 'req_0a6e65d0818a4abb942cdc3e5ae5939d',
              model: 'techsara-35b',
              status: 'completed',
              background: false,
              streamed: true,
              input_tokens: 23,
              output_tokens: 16,
              ttft_ms: 10,
              duration_ms: 211,
              error_code: '',
              metadata: {},
              key: null,
              created_at: '2026-09-13T09:00:00Z',
              started_at: null,
              completed_at: null,
            },
          ],
        }),
    });
    render(<ConsoleShell me={ADMIN} />);
    expectReachableOnPhone(await screen.findByRole('button', { name: 'Copy request id' }));
    expectReachableOnPhone(screen.getByText('completed'));
  });

  it('keeps the publish switch on screen for a super admin', async () => {
    state.search = new URLSearchParams('tab=models');
    route({ 'GET models': () => jsonResponse({ models: [MODEL], can_manage: true }) });
    render(<ConsoleShell me={SUPER_ADMIN} />);
    expectReachableOnPhone(await screen.findByRole('switch', { name: /publish techsara-35b/i }));
  });

  it('lets a long identifier wrap inside its own cell instead of running under the next column', async () => {
    // The table cell is `whitespace-nowrap`; a break-all id inside it cannot
    // break at all, which is how the request id ran under "When" at 1440px.
    state.search = new URLSearchParams('tab=keys');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.keys(PROJECT.id)}`]: () => jsonResponse({ keys: [KEY_ROW] }),
    });
    render(<ConsoleShell me={ADMIN} />);
    const id = await screen.findByText('tsk_test_0e4005cb9284109d…LtSL');
    expect(id.closest('.whitespace-normal')).not.toBeNull();
  });
});

describe('the console tables at 1440px', () => {
  it('keeps every <col> a column at lg, so a hidden-below-lg column does not shift the widths after it', async () => {
    // The real cause of the Scopes overlap: AdminTable's `lg:table-cell` on a
    // <col> makes Chrome drop it from the colgroup, and each later width
    // slides one track left — Scopes drew at Status's 110px. jsdom lays out
    // nothing, so this pins the rule that puts the column back: every table
    // with such a <col> sits inside a wrapper restoring `display: table-column`.
    state.search = new URLSearchParams('tab=keys');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.keys(PROJECT.id)}`]: () => jsonResponse({ keys: [KEY_ROW] }),
    });
    render(<ConsoleShell me={ADMIN} />);
    const table = (await screen.findByTestId('key-scopes')).closest('table') as HTMLTableElement;
    const shifting = Array.from(table.querySelectorAll('col')).filter((c) =>
      c.className.includes('lg:table-cell'),
    );
    expect(shifting.length).toBeGreaterThan(0);
    expect(table.closest('[class~="lg:[&_col]:!table-column"]')).not.toBeNull();
  });
});

describe('the scopes cell at 1440px', () => {
  it('stays inside its column and still carries every scope', async () => {
    // The screenshot: "models.read, responses.read, responses.write" ran over
    // Status and Last used, because `truncate` on an inline span clips nothing.
    state.search = new URLSearchParams('tab=keys');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.keys(PROJECT.id)}`]: () => jsonResponse({ keys: [KEY_ROW] }),
    });
    render(<ConsoleShell me={ADMIN} />);
    const cell = (await screen.findByTestId('key-scopes')) as HTMLElement;
    const full = 'models.read, responses.read, responses.write';
    // Every scope is available in full: on hover, and to a screen reader.
    expect(cell.getAttribute('title')).toBe(full);
    expect(within(cell).getByText(full).className).toContain('sr-only');
    // What is drawn is one scope and a count, which fits a 200px column.
    const drawn = Array.from(cell.querySelectorAll('[aria-hidden="true"]')).map((el) => el.textContent);
    expect(drawn).toEqual(['models.read', '+2']);
    // A flex container clips its truncating child; an inline span does not.
    expect(cell.className).toContain('flex');
    expect(cell.className).toContain('min-w-0');
  });

  it('says None for a key with no scopes', async () => {
    state.search = new URLSearchParams('tab=keys');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.keys(PROJECT.id)}`]: () =>
        jsonResponse({ keys: [{ ...KEY_ROW, scopes: [] }] }),
    });
    render(<ConsoleShell me={ADMIN} />);
    expect((await screen.findByTestId('key-scopes')).textContent).toBe('None');
  });
});

describe('the playground hint under Run', () => {
  it('reads as one sentence with the key variable in its place, not floated to the end of a line', async () => {
    state.search = new URLSearchParams('tab=playground');
    route({ 'GET models': () => jsonResponse({ models: [MODEL], can_manage: false }) });
    render(<ConsoleShell me={ADMIN} />);
    const chip = await screen.findByText(`$${KEY_ENV_VAR}`, { selector: 'code' });
    const sentence = chip.parentElement as HTMLElement;
    // The chip shares an inline run with its words. As a direct child of the
    // flex paragraph it became its own flex item and jumped the queue.
    expect(sentence.className).not.toMatch(/\bflex\b/);
    expect((sentence.textContent ?? '').replace(/\s+/g, ' ').trim()).toBe(
      `This runs on your signed-in session, not on an API key. Copy the snippet to run the same request from your own code with $${KEY_ENV_VAR}.`,
    );
  });
});

describe('hydrating the playground', () => {
  it('renders the same snippet on the server and on the first client pass, so React reports no mismatch (#418)', async () => {
    const { renderToString } = await import('react-dom/server');
    const { hydrateRoot } = await import('react-dom/client');
    const { PlaygroundPanel } = await import('@/components/devplatform/Playground');
    route({ 'GET models': () => jsonResponse({ models: [MODEL], can_manage: false }) });

    // The server has no window. Render there first, exactly as Next does.
    const realWindow = globalThis.window;
    vi.stubGlobal('window', undefined);
    let html: string;
    try {
      html = renderToString(<PlaygroundPanel />);
    } finally {
      vi.stubGlobal('window', realWindow);
    }

    const container = document.createElement('div');
    container.innerHTML = html;
    document.body.appendChild(container);
    const recoverable = vi.fn();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    let root: ReturnType<typeof hydrateRoot> | undefined;
    await act(async () => {
      root = hydrateRoot(container, <PlaygroundPanel />, { onRecoverableError: recoverable });
    });
    try {
      expect(recoverable).not.toHaveBeenCalled();
      // And after mount the snippet does point at this deployment.
      await waitFor(() =>
        expect(container.querySelector('[data-testid="playground-snippet"]')!.textContent).toContain(
          `${window.location.origin}/v1/responses`,
        ),
      );
    } finally {
      act(() => root?.unmount());
      container.remove();
    }
  });
});

describe('the project select on a narrow screen', () => {
  it('is bounded by the toolbar so it cannot cover the primary action', () => {
    // Visual QA at 400 px, 2026-09-13: the shared select sized itself to its
    // longest option and would not shrink, so a long project name sat on top
    // of "Create key". jsdom does no layout; this pins the clamp that the
    // browser screenshot proved fixes it.
    render(
      <ProjectSelect
        projects={[{ ...PROJECT, name: 'a-very-long-project-name-that-would-not-fit-on-a-phone-screen' } as unknown as Project]}
        value={PROJECT.id}
        onChange={() => undefined}
      />,
    );
    const wrapper = screen.getByTestId('project-select');
    expect(wrapper.className).toContain('min-w-0');
    expect(wrapper.className).toContain('max-w-full');
    expect(wrapper.className).toContain('[&_select]:max-w-full');
    expect(within(wrapper).getByLabelText('Project')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Every model TechSara runs, and 1,000,000 output tokens (owner request,
// 2026-09-13)
// ---------------------------------------------------------------------------

const CHAT_ENDPOINTS = ['/v1/responses', '/v1/chat/completions'];

function capabilities(...on: string[]) {
  const flags = ['chat', 'streaming', 'vision', 'ocr', 'embeddings', 'rerank', 'audio_transcription', 'tools', 'background'];
  return Object.fromEntries(flags.map((flag) => [flag, on.includes(flag)])) as Record<string, boolean>;
}

/** The six catalogue entries as `console_api._console_model` sends them. */
const CATALOGUE = [
  {
    id: 'techsara-35b', object: 'model', owned_by: 'techsara', status: 'available', kind: 'chat',
    capabilities: capabilities('chat', 'streaming', 'vision', 'background'), endpoints: CHAT_ENDPOINTS,
    context_window: 1_000_000, max_input_tokens: 999_232, max_output_tokens: 1_000_000,
    default_max_output_tokens: 8192, limits: { max_images_per_request: 16 }, enabled: true,
  },
  {
    id: 'techsara-8b-vision', object: 'model', owned_by: 'techsara', status: 'available', kind: 'chat',
    capabilities: capabilities('chat', 'streaming', 'vision', 'background'), endpoints: CHAT_ENDPOINTS,
    context_window: 24_576, max_input_tokens: 24_320, max_output_tokens: 24_576,
    default_max_output_tokens: 8192, limits: { max_images_per_request: 8 }, enabled: true,
  },
  {
    id: 'techsara-ocr', object: 'model', owned_by: 'techsara', status: 'available', kind: 'chat',
    capabilities: capabilities('chat', 'streaming', 'vision', 'ocr', 'background'), endpoints: CHAT_ENDPOINTS,
    context_window: 8192, max_input_tokens: 7936, max_output_tokens: 8192,
    default_max_output_tokens: 8192, limits: { max_images_per_request: 1 }, enabled: true,
  },
  {
    id: 'techsara-embed', object: 'model', owned_by: 'techsara', status: 'available', kind: 'embedding',
    capabilities: capabilities('embeddings'), endpoints: ['/v1/embeddings'],
    context_window: 4096, max_input_tokens: 4096, max_output_tokens: null, default_max_output_tokens: null,
    limits: { max_inputs_per_request: 256, embedding_dimensions: 1024 }, enabled: true,
  },
  {
    id: 'techsara-rerank', object: 'model', owned_by: 'techsara', status: 'available', kind: 'rerank',
    capabilities: capabilities('rerank'), endpoints: ['/v1/rerank'],
    context_window: 4096, max_input_tokens: 4096, max_output_tokens: null, default_max_output_tokens: null,
    limits: { max_documents_per_request: 100 }, enabled: false,
  },
  {
    id: 'techsara-whisper', object: 'model', owned_by: 'techsara', status: 'not_configured', kind: 'transcription',
    capabilities: capabilities('audio_transcription'), endpoints: ['/v1/audio/transcriptions'],
    context_window: null, max_input_tokens: null, max_output_tokens: null, default_max_output_tokens: null,
    limits: { max_audio_seconds: 300, max_audio_bytes: 26_214_400, response_formats: ['json', 'text', 'verbose_json'] },
    enabled: true,
  },
];

const cardFacts = (id: string) =>
  Object.fromEntries(
    Array.from(screen.getByTestId(`model-card-${id}`).querySelectorAll('dt')).map((dt) => [
      dt.textContent,
      dt.nextElementSibling?.textContent,
    ]),
  );

describe('the models page lists every model the platform runs', () => {
  function mountModels(me: Me = SUPER_ADMIN) {
    state.search = new URLSearchParams('tab=models');
    const calls = route({
      'GET models': () => jsonResponse({ models: CATALOGUE, can_manage: true }),
      'PUT models/techsara-whisper': () => jsonResponse({ model: { id: 'techsara-whisper', enabled: false } }),
    });
    render(<ConsoleShell me={me} />);
    return calls;
  }

  it('shows all six with their kind and capability badges, and a publish switch on each', async () => {
    mountModels();
    const table = (await screen.findByRole('switch', { name: /publish techsara-35b/i })).closest('table')!;
    for (const model of CATALOGUE) {
      expect(within(table).getByRole('switch', { name: `Publish ${model.id} to API keys` })).toBeTruthy();
    }
    const badges = (id: string) =>
      within(within(table).getByRole('list', { name: `${id} capabilities` }))
        .getAllByRole('listitem')
        .map((li) => li.textContent);
    expect(badges('techsara-35b')).toEqual(['Chat', 'Streaming', 'Vision', 'Background']);
    expect(badges('techsara-ocr')).toEqual(['Chat', 'Streaming', 'Vision', 'OCR', 'Background']);
    expect(badges('techsara-embed')).toEqual(['Embeddings']);
    expect(badges('techsara-rerank')).toEqual(['Rerank']);
    expect(badges('techsara-whisper')).toEqual(['Speech-to-text']);
    expect(within(table).getByText('Speech-to-text model')).toBeTruthy();
    expect(within(table).getByText('Reranker')).toBeTruthy();
  });

  it('draws techsara-35b’s output ceiling as 1.0M in the table and 1,000,000 on its card', async () => {
    mountModels();
    const row = (await screen.findByRole('switch', { name: /publish techsara-35b/i })).closest('tr')!;
    const cells = Array.from(row.querySelectorAll('td')).map((td) => td.textContent);
    // Context window, max input (999,232 = the window less the safety margin
    // and the minimum output), max output.
    expect(cells.slice(1, 4)).toEqual(['1.0M', '999K', '1.0M']);
    expect(cardFacts('techsara-35b')).toMatchObject({
      'Context window': '1,000,000',
      'Max input tokens': '999,232',
      'Max output tokens': '1,000,000',
      'Default output tokens': '8,192',
      'Images per request': '16',
    });
    expect(screen.getByTestId('model-card-techsara-35b').textContent).toContain(
      'must stream or run in the background',
    );
    expect(screen.getByTestId('model-card-techsara-35b').textContent).toContain('about 2.8–4.0 hours');
    // A 24,576-token ceiling finishes in minutes and gets no such warning.
    expect(screen.getByTestId('model-card-techsara-8b-vision').textContent).not.toContain('background');
  });

  it('gives each kind the ceilings that apply to it and no dash for one that does not', async () => {
    mountModels();
    await screen.findByTestId('model-card-techsara-embed');
    expect(cardFacts('techsara-embed')).toEqual({
      'Max tokens per input': '4,096',
      'Inputs per request': '256',
      'Embedding dimensions': '1,024',
    });
    expect(cardFacts('techsara-rerank')).toEqual({
      'Max tokens per query and document': '4,096',
      'Documents per request': '100',
    });
    expect(cardFacts('techsara-whisper')).toEqual({
      'Max audio length': '300 s (5 min)',
      'Max audio file': '25 MiB',
      'Response formats': 'json, text, verbose_json',
    });
    const embedCard = screen.getByTestId('model-card-techsara-embed');
    expect(within(embedCard).getByText('POST /v1/embeddings')).toBeTruthy();
    expect(within(screen.getByTestId('model-card-techsara-35b')).getByText('POST /v1/chat/completions')).toBeTruthy();
  });

  it('says a model is not configured on this deployment rather than hiding it, and it can still be withdrawn', async () => {
    const calls = mountModels();
    const row = (await screen.findByRole('switch', { name: /publish techsara-whisper/i })).closest('tr')!;
    expect(within(row).getByText('Not configured on this deployment')).toBeTruthy();
    expect(
      within(screen.getByTestId('model-card-techsara-whisper')).getByText('Not configured on this deployment'),
    ).toBeTruthy();
    // A configured model says nothing of the kind.
    const main = screen.getByRole('switch', { name: /publish techsara-35b/i }).closest('tr')!;
    expect(within(main).queryByText('Not configured on this deployment')).toBeNull();

    const user = userEvent.setup();
    await user.click(screen.getByRole('switch', { name: /publish techsara-whisper/i }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    expect(calls.find((c) => c.method === 'PUT')!.url).toBe('/api/devplatform/models/techsara-whisper');
  });

  it('keeps every publish switch on a 400px phone, with the ceilings on the cards instead of the table', async () => {
    mountModels();
    for (const model of CATALOGUE) {
      expectReachableOnPhone(await screen.findByRole('switch', { name: `Publish ${model.id} to API keys` }));
    }
    const row = screen.getByRole('switch', { name: /publish techsara-35b/i }).closest('tr')!;
    const ceilings = Array.from(row.querySelectorAll('td')).slice(1, 4);
    for (const cell of ceilings) expect(cell.className.split(/\s+/)).toContain('hidden');
    // The badges wrap inside their own cell rather than running under the switch.
    const identity = row.querySelector('td')!.firstElementChild as HTMLElement;
    expect(identity.className).toContain('whitespace-normal');
  });

  it('still renders an orchestrator that predates the catalogue, as the chat model it is', async () => {
    state.search = new URLSearchParams('tab=models');
    route({ 'GET models': () => jsonResponse({ models: [MODEL], can_manage: false }) });
    render(<ConsoleShell me={ADMIN} />);
    const card = await screen.findByTestId('model-card-techsara-35b');
    expect(within(card).getByText('Chat model')).toBeTruthy();
    expect(within(card).getByText('POST /v1/responses')).toBeTruthy();
    expect(cardFacts('techsara-35b')['Context window']).toBe('—');
    expect(cardFacts('techsara-35b')['Max output tokens']).toBe('8,192');
  });
});

describe('the playground targets every chat model', () => {
  function mountPlayground(answer?: (init: RequestInit) => Response) {
    state.search = new URLSearchParams('tab=playground');
    const calls = route({
      'GET models': () => jsonResponse({ models: CATALOGUE, can_manage: false }),
      'POST playground/execute': (_url, init) =>
        answer ? answer(init) : jsonResponse({ error: { message: 'unused' } }, 500),
    });
    render(<ConsoleShell me={ADMIN} />);
    return calls;
  }

  const modelSelect = () => screen.getByLabelText('Model') as HTMLSelectElement;
  const maxOutput = () => screen.getByLabelText('Max output tokens') as HTMLInputElement;

  it('offers the published chat models, lists OCR as needing an image, and leaves out the other kinds', async () => {
    mountPlayground();
    await waitFor(() => expect(modelSelect().options.length).toBeGreaterThan(0));
    const options = Array.from(modelSelect().options).map((o) => ({ id: o.value, disabled: o.disabled, text: o.textContent }));
    expect(options).toEqual([
      { id: 'techsara-35b', disabled: false, text: 'techsara-35b' },
      { id: 'techsara-8b-vision', disabled: false, text: 'techsara-8b-vision' },
      { id: 'techsara-ocr', disabled: true, text: 'techsara-ocr (needs an image — not in the playground yet)' },
    ]);
  });

  it('bounds max output tokens by the chosen model: 1,000,000 on techsara-35b, 24,576 on techsara-8b-vision', async () => {
    mountPlayground();
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Input'), 'Write a very long answer.');
    expect(maxOutput().max).toBe('1000000');
    expect(screen.getByTestId('playground-max-output-hint').textContent).toContain('Up to 1,000,000 for techsara-35b.');
    expect(screen.getByTestId('playground-max-output-hint').textContent).toContain('about 2.8–4.0 hours');

    await user.clear(maxOutput());
    await user.type(maxOutput(), '1000000');
    expect((screen.getByRole('button', { name: 'Run' }) as HTMLButtonElement).disabled).toBe(false);

    await user.clear(maxOutput());
    await user.type(maxOutput(), '1000001');
    expect(screen.getByTestId('playground-max-output-hint').textContent).toBe(
      'Enter a whole number from 1 to 1,000,000.',
    );
    expect(maxOutput().getAttribute('aria-invalid')).toBe('true');
    expect((screen.getByRole('button', { name: 'Run' }) as HTMLButtonElement).disabled).toBe(true);

    await user.clear(maxOutput());
    await user.type(maxOutput(), '30000');
    await user.selectOptions(modelSelect(), 'techsara-8b-vision');
    expect(maxOutput().max).toBe('24576');
    expect(screen.getByTestId('playground-max-output-hint').textContent).toBe(
      'Enter a whole number from 1 to 24,576.',
    );
    expect((screen.getByRole('button', { name: 'Run' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('sends a 1,000,000-token request for the chosen model and puts the same model in the snippet', async () => {
    const s = controlledStream();
    const calls = mountPlayground(
      () => new Response(s.stream, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Input'), 'Everything, please.');
    await user.clear(maxOutput());
    await user.type(maxOutput(), '1000000');
    expect(screen.getByTestId('playground-snippet').textContent).toContain('"max_output_tokens": 1000000');
    await user.click(screen.getByRole('button', { name: 'Run' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(JSON.parse(String(calls.find((c) => c.method === 'POST')!.init.body))).toMatchObject({
      model: 'techsara-35b',
      max_output_tokens: 1_000_000,
      stream: true,
    });
    s.close();

    await user.selectOptions(modelSelect(), 'techsara-8b-vision');
    await waitFor(() =>
      expect(screen.getByTestId('playground-snippet').textContent).toContain('"model": "techsara-8b-vision"'),
    );
  });

  it('shows the output ceiling the terminal event says was applied, and when the answer stopped there', async () => {
    const s = controlledStream();
    mountPlayground(
      () => new Response(s.stream, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Input'), 'Say hello');
    await user.click(screen.getByRole('button', { name: 'Run' }));
    s.push(frame('response.created', 1, { response: { status: 'queued', usage: null, max_output_tokens: 512 } }));
    s.push(frame('response.output_text.done', 2, { text: 'Hello' }));
    s.push(
      frame('response.completed', 3, {
        response: {
          status: 'completed',
          usage: { input_tokens: 5, output_tokens: 498, total_tokens: 503 },
          max_output_tokens: 498,
          incomplete_details: { reason: 'max_output_tokens' },
        },
      }),
    );
    s.close();
    const applied = await screen.findByTestId('playground-applied-ceiling');
    expect(applied.textContent).toBe(
      'Output ceiling applied: 498 tokens — the answer stopped because it reached this ceiling.',
    );
  });

  it('says nothing about an applied ceiling the server did not report', async () => {
    const s = controlledStream();
    mountPlayground(
      () => new Response(s.stream, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Input'), 'Say hello');
    await user.click(screen.getByRole('button', { name: 'Run' }));
    s.push(frame('response.completed', 1, { response: { status: 'completed', usage: null } }));
    s.close();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run' })).toBeTruthy());
    await waitFor(() => expect(document.querySelectorAll('section[aria-label="Response"] ol li')).toHaveLength(1));
    expect(screen.queryByTestId('playground-applied-ceiling')).toBeNull();
  });
});

describe('the helpers behind the playground bounds', () => {
  it('reads an applied ceiling from a terminal response and treats a missing one as unknown', async () => {
    const { readAppliedCeiling } = await import('@/components/devplatform/Playground');
    expect(readAppliedCeiling({ response: { max_output_tokens: 999_000, incomplete_details: null } })).toEqual({
      maxOutputTokens: 999_000,
      stoppedAtCeiling: false,
    });
    expect(readAppliedCeiling({ response: { usage: null } })).toEqual({ maxOutputTokens: null, stoppedAtCeiling: false });
    expect(readAppliedCeiling({ max_output_tokens: 7, incomplete_details: { reason: 'max_output_tokens' } })).toEqual({
      maxOutputTokens: 7,
      stoppedAtCeiling: true,
    });
  });

  it('refuses a max output that is empty, fractional, zero or over the ceiling', async () => {
    const { maxOutputProblem } = await import('@/components/devplatform/Playground');
    expect(maxOutputProblem('1000000', 1_000_000)).toBeNull();
    expect(maxOutputProblem('1', 1_000_000)).toBeNull();
    for (const bad of ['', '0', '-3', '2.5', 'abc', '1000001']) {
      expect(maxOutputProblem(bad, 1_000_000)).toBe('Enter a whole number from 1 to 1,000,000.');
    }
    // An unreported ceiling still refuses what can never be valid, and leaves the rest to the server.
    expect(maxOutputProblem('5000000', null)).toBeNull();
    expect(maxOutputProblem('0', null)).toBe('Enter a whole number of at least 1.');
  });
});

describe('the streaming snippets', () => {
  const streamed = {
    baseUrl: 'https://ai.techsarasolutions.com',
    model: 'techsara-8b-vision',
    input: 'Describe "this".',
    stream: true,
    temperature: 0.2,
    maxOutputTokens: 1_000_000,
  };

  it('reads the Python event stream line by line with a per-chunk timeout, in Python literals', () => {
    const snippet = pythonSnippet(streamed);
    expect(snippet.startsWith('import json\nimport os\nimport httpx\n')).toBe(true);
    expect(snippet).toContain('with httpx.stream(');
    expect(snippet).toContain('"stream": True');
    expect(snippet).not.toMatch(/:\s(true|false|null)\b/);
    expect(snippet).toContain('timeout=httpx.Timeout(30.0, read=60.0)');
    expect(snippet).toContain('for line in response.iter_lines():');
    expect(snippet).not.toContain('response.json()');
    expect(snippet).toContain('"model": "techsara-8b-vision"');
    expect(snippet).toContain('"max_output_tokens": 1000000');
    expect(snippet).toContain('"input": "Describe \\"this\\"."');
  });

  it('keeps the synchronous Python snippet on response.json()', () => {
    const snippet = pythonSnippet({ ...streamed, stream: false });
    expect(snippet).toContain('httpx.post(');
    expect(snippet).toContain('print(response.json())');
    expect(snippet).not.toContain('"stream"');
  });

  it('prints the answer from its delta events and raises on either failure terminal, in both languages', () => {
    // Printing raw `data:` lines put JSON on the terminal and exited 0 after a
    // generation that died. tests/devplatform-snippets-run.test.ts runs both
    // against a stub CONTRACT §10 stream; this pins the branches.
    const python = pythonSnippet(streamed);
    expect(python).toContain('if event == "response.output_text.delta":');
    expect(python).toContain('print(data["delta"], end="", flush=True)');
    expect(python).toContain('elif event == "response.failed":');
    expect(python).toContain('elif event == "error":');
    const js = javascriptSnippet(streamed);
    expect(js).toContain("if (event === 'response.output_text.delta') process.stdout.write(data.delta);");
    expect(js).toContain("else if (event === 'response.failed') throw new Error(");
    expect(js).toContain("else if (event === 'error') throw new Error(");
    // A chunk may end mid-line: the unfinished tail is kept for the next one.
    expect(js).toContain('buffer = lines.pop();');
  });

  it('says how to run the JavaScript, because top-level await needs an ES module', () => {
    for (const stream of [true, false]) {
      expect(javascriptSnippet({ ...streamed, stream }).split('\n')[0]).toBe(
        '// Node 18 or later. Save as request.mjs and run: node request.mjs',
      );
    }
  });

  it('reads the JavaScript body as a stream and asks curl not to buffer', () => {
    const js = javascriptSnippet(streamed);
    expect(js).toContain('for await (const chunk of response.body)');
    expect(js).not.toContain('response.json()');
    expect(javascriptSnippet({ ...streamed, stream: false })).toContain('console.log(await response.json());');
    expect(curlSnippet(streamed).startsWith('curl -N https://ai.techsarasolutions.com/v1/responses')).toBe(true);
    expect(curlSnippet({ ...streamed, stream: false }).startsWith('curl https://')).toBe(true);
  });

  it('writes nested values and empty containers as Python', async () => {
    const { pythonLiteral } = await import('@/components/devplatform/snippets');
    expect(pythonLiteral({ a: [true, null, 1.5], b: {}, c: [] })).toBe(
      '{\n    "a": [\n        True,\n        None,\n        1.5\n    ],\n    "b": {},\n    "c": []\n}',
    );
  });
});

// ---------------------------------------------------------------------------
// Responsive audit, 2026-09-13: what the console still did wrong at every width
// ---------------------------------------------------------------------------

/**
 * The px the console gives a table at a window width, from lg up: the 240px
 * rail, a 16px allowance for the page's scrollbar, then the content column's
 * 1180px cap less its 32px of padding a side.
 */
const consoleColumnAt = (viewport: number) => Math.min(viewport - 240 - 16, 1180) - 64;

/**
 * What a table draws at a laptop or desktop width, read from the markup — jsdom
 * applies no stylesheet, so the tiers are read off the wrapper's class names,
 * which are the exact rules the stylesheet is generated from.
 */
function desktopGeometry(table: HTMLTableElement, viewport: number) {
  const wrapper = table.closest('[data-testid="console-table"]') as HTMLElement;
  expect(wrapper).not.toBeNull();
  const classes = wrapper.className.split(/\s+/);
  const has = (tier: 'xl' | '2xl', n: number) => {
    const rules = ['col', 'th', 'td'].map((el) => `max-${tier}:[&_${el}:nth-child(${n})]:!hidden`);
    const present = rules.filter((rule) => classes.includes(rule));
    // A tier that hid the <col> but not its cells (or the reverse) would
    // shift every later column, so it is all three or none.
    expect(present.length === 0 || present.length === 3).toBe(true);
    return present.length === 3;
  };
  const labels = Array.from(table.querySelectorAll('thead th')).map((th) => th.textContent ?? '');
  const cols = Array.from(table.querySelectorAll('col'));
  const hidden = (index: number) =>
    (viewport < 1280 && has('xl', index + 1)) || (viewport < 1536 && has('2xl', index + 1));
  const visible = cols.filter((_, i) => !hidden(i));
  const fixed = visible.reduce((sum, col) => sum + (parseFloat(col.style.width) || 0), 0);
  const lifted = classes.includes('max-xl:[&_table]:!min-w-0');
  const floor = parseFloat(table.style.getPropertyValue('--admin-table-min'));
  const column = consoleColumnAt(viewport);
  const width = lifted && viewport < 1280 ? column : Math.max(column, floor);
  return {
    clipped: width > column,
    identity: width - fixed,
    hiddenLabels: labels.filter((_, i) => hidden(i)),
  };
}

const LAPTOP_AND_DESKTOP = [1024, 1100, 1279, 1280, 1366, 1440, 1535, 1536, 1920];

function expectFitsEveryDesktop(table: HTMLTableElement) {
  for (const viewport of LAPTOP_AND_DESKTOP) {
    const { clipped, identity } = desktopGeometry(table, viewport);
    expect({ viewport, clipped }).toEqual({ viewport, clipped: false });
    expect({ viewport, readable: identity >= READABLE_IDENTITY }).toEqual({
      viewport,
      readable: true,
    });
  }
}

const LOG_ROW = {
  id: 'resp_1',
  request_id: 'req_0a6e65d0818a4abb942cdc3e5ae5939d',
  model: 'techsara-35b',
  status: 'completed',
  background: false,
  streamed: true,
  input_tokens: 23,
  output_tokens: 16,
  ttft_ms: 10,
  duration_ms: 211,
  error_code: '',
  metadata: {},
  key: { id: 'key_1', name: 'a key with a long descriptive name', last_four: '7ON6' },
  created_at: '2026-09-13T09:00:00Z',
  started_at: null,
  completed_at: null,
};

const WEBHOOK_ROW = {
  id: 'whe_1',
  project_id: PROJECT.id,
  url: 'https://hooks.example.com/services/techsara/billing-assistant/production/receiver',
  events: ['response.completed'],
  status: 'active',
  include_output: false,
  has_secret: true,
  rotation_in_progress: false,
  created_at: null,
  last_delivery_at: null,
  last_delivery_status: '',
  consecutive_failures: 0,
  disabled_at: null,
};

describe('the console tables from 1024px to 1920px', () => {
  it('keeps the keys table and its revoke menu inside the column, folding Environment until xl', async () => {
    state.search = new URLSearchParams('tab=keys');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.keys(PROJECT.id)}`]: () => jsonResponse({ keys: [KEY_ROW] }),
    });
    render(<ConsoleShell me={ADMIN} />);
    const menu = await screen.findByRole('button', { name: 'Actions for Edge server' });
    const table = menu.closest('table') as HTMLTableElement;
    expectFitsEveryDesktop(table);
    expect(desktopGeometry(table, 1024).hiddenLabels).toEqual(['Environment']);
    expect(desktopGeometry(table, 1280).hiddenLabels).toEqual([]);
  });

  it('keeps the request log readable at 1024 by folding Key and Duration until xl and Shape until 2xl', async () => {
    state.search = new URLSearchParams('tab=logs');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.logs(PROJECT.id)}`]: () =>
        jsonResponse({ project: { id: PROJECT.id, name: PROJECT.name }, requests: [LOG_ROW] }),
    });
    render(<ConsoleShell me={ADMIN} />);
    const copy = await screen.findByRole('button', { name: 'Copy request id' });
    const table = copy.closest('table') as HTMLTableElement;
    expectFitsEveryDesktop(table);
    expect(desktopGeometry(table, 1024).hiddenLabels).toEqual(['Key', 'Duration', 'Shape']);
    expect(desktopGeometry(table, 1280).hiddenLabels).toEqual(['Shape']);
    expect(desktopGeometry(table, 1536).hiddenLabels).toEqual([]);
  });

  it('keeps the webhooks, projects and models tables inside the column at every desktop width', async () => {
    state.search = new URLSearchParams('tab=webhooks');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.webhooks(PROJECT.id)}`]: () => jsonResponse({ webhooks: [WEBHOOK_ROW] }),
      'GET models': () => jsonResponse({ models: [MODEL], can_manage: true }),
    });
    const { unmount } = render(<ConsoleShell me={SUPER_ADMIN} />);
    const hook = await screen.findByRole('button', { name: `Actions for ${WEBHOOK_ROW.url}` });
    expectFitsEveryDesktop(hook.closest('table') as HTMLTableElement);
    unmount();

    state.search = new URLSearchParams('tab=projects');
    const projects = render(<ConsoleShell me={SUPER_ADMIN} />);
    const row = await screen.findByRole('button', { name: `Actions for ${PROJECT.name}` });
    expectFitsEveryDesktop(row.closest('table') as HTMLTableElement);
    projects.unmount();

    state.search = new URLSearchParams('tab=models');
    render(<ConsoleShell me={SUPER_ADMIN} />);
    const toggle = await screen.findByRole('switch', { name: /publish techsara-35b/i });
    const table = toggle.closest('table') as HTMLTableElement;
    expectFitsEveryDesktop(table);
    expect(desktopGeometry(table, 1024).hiddenLabels).toEqual(['Max input']);
  });

  it('keeps the last four of a key visible when its name is too long for the log column', async () => {
    state.search = new URLSearchParams('tab=logs');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.logs(PROJECT.id)}`]: () =>
        jsonResponse({ project: { id: PROJECT.id, name: PROJECT.name }, requests: [LOG_ROW] }),
    });
    render(<ConsoleShell me={ADMIN} />);
    const name = await screen.findByText(LOG_ROW.key.name);
    // The name truncates; the last four sit in their own unshrinkable span.
    expect(name.className).toContain('truncate');
    const lastFour = name.nextElementSibling as HTMLElement;
    expect(lastFour.textContent).toContain('…7ON6');
    expect(lastFour.className).toContain('shrink-0');
  });
});

describe('the consoleTableClass tiers', () => {
  it('emits one complete col, th and td rule per folded column and nothing for the rest', async () => {
    const { consoleTableClass } = await import('@/components/devplatform/shared');
    const classes = consoleTableClass([{}, { hideBelow: 'xl' }, {}, { hideBelow: '2xl' }]).split(' ');
    expect(classes).toContain('max-xl:[&_table]:!min-w-0');
    for (const el of ['col', 'th', 'td']) {
      expect(classes).toContain(`max-xl:[&_${el}:nth-child(2)]:!hidden`);
      expect(classes).toContain(`max-2xl:[&_${el}:nth-child(4)]:!hidden`);
    }
    expect(classes.some((c) => c.includes('nth-child(1)') || c.includes('nth-child(3)'))).toBe(false);
  });
});

describe('a console request that fails', () => {
  const refuse = () =>
    jsonResponse({ error: { message: 'The console service is restarting.' } }, 503);

  const falseEmpty: [string, RegExp][] = [
    ['keys', /No projects, so no keys|No keys in this project/],
    ['logs', /No projects to log|No requests yet/],
    ['webhooks', /No projects to notify|No endpoints in this project/],
    ['limits', /No projects to limit/],
  ];

  for (const [tab, empty] of falseEmpty) {
    it(`shows the error and a Retry on ${tab} when the project list fails, never an empty state`, async () => {
      state.search = new URLSearchParams(`tab=${tab}`);
      const calls = route({ 'GET projects': refuse });
      render(<ConsoleShell me={SUPER_ADMIN} />);
      const alert = await screen.findByRole('alert');
      expect(alert.textContent).toContain('The console service is restarting.');
      expect(screen.queryByText(empty)).toBeNull();

      const projectCalls = () => calls.filter((c) => c.url.includes('/api/devplatform/projects')).length;
      const before = projectCalls();
      await userEvent.setup().click(within(alert).getByRole('button', { name: 'Retry' }));
      await waitFor(() => expect(projectCalls()).toBe(before + 1));
    });
  }

  it('says nothing about keys while the project list is still on its way', async () => {
    state.search = new URLSearchParams('tab=keys');
    const pending = deferred<Response>();
    route({ 'GET projects': () => pending.promise });
    render(<ConsoleShell me={ADMIN} />);
    await screen.findByRole('heading', { name: 'API keys' });
    expect(screen.queryByText(/No keys in this project|No projects, so no keys/)).toBeNull();
    await act(async () => {
      pending.resolve(jsonResponse({ projects: [] }));
    });
    await screen.findByText(/No projects, so no keys/);
  });

  it('shows the model list failure with a Retry on the playground instead of "no chat model"', async () => {
    state.search = new URLSearchParams('tab=playground');
    const calls = route({ 'GET models': refuse });
    render(<ConsoleShell me={ADMIN} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('The console service is restarting.');
    expect(screen.queryByText(/No chat model is published/)).toBeNull();
    await userEvent.setup().click(within(alert).getByRole('button', { name: 'Retry' }));
    await waitFor(() =>
      expect(calls.filter((c) => c.url.includes('/api/devplatform/models')).length).toBe(2),
    );
  });

  it('shows no project count beside the projects error, and the count once the list arrives', async () => {
    state.search = new URLSearchParams('tab=projects');
    let fail = true;
    route({ 'GET projects': () => (fail ? refuse() : jsonResponse({ projects: [PROJECT] })) });
    render(<ConsoleShell me={ADMIN} />);
    const alert = await screen.findByRole('alert');
    expect(screen.queryByText(/\d+ projects?$/)).toBeNull();
    fail = false;
    await userEvent.setup().click(within(alert).getByRole('button', { name: 'Retry' }));
    await screen.findByText('1 project');
  });

  it('offers a Retry when the limits of a project fail to load', async () => {
    state.search = new URLSearchParams('tab=limits');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      [`GET ${consolePaths.limits(PROJECT.id)}`]: refuse,
    });
    render(<ConsoleShell me={SUPER_ADMIN} />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: 'Retry' })).toBeTruthy();
  });
});

describe('the console navigation below lg', () => {
  function mountAt(tab: string) {
    state.search = new URLSearchParams(tab ? `tab=${tab}` : '');
    route({
      'GET projects': () => jsonResponse({ projects: [] }),
      'GET overview': () => jsonResponse(overviewOf({})),
    });
    render(<ConsoleShell me={SUPER_ADMIN} />);
  }

  it('is a menu button with a 40px target that names the section on show, not a sideways strip', async () => {
    mountAt('webhooks');
    const toggle = screen.getByRole('button', { name: 'Open console menu' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-controls')).toBe('console-rail');
    expect(toggle.className).toContain('h-10');
    expect(toggle.className).toContain('w-10');
    expect(screen.getByTestId('console-current-section').textContent).toBe('Webhooks');
    const header = toggle.closest('header') as HTMLElement;
    expect(header.className).not.toContain('overflow-x-auto');
    expect(header.querySelectorAll('a')).toHaveLength(0);
    // Closed, the rail is not drawn below lg; from lg it is the column.
    const rail = document.getElementById('console-rail') as HTMLElement;
    expect(rail.className.split(/\s+/)).toContain('hidden');
    expect(rail.className).toContain('lg:flex');
  });

  it('opens a drawer holding every section and both ways out, focused on the section on show', async () => {
    mountAt('webhooks');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Open console menu' }));
    const toggle = screen.getByRole('button', { name: 'Close console menu' });
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const rail = document.getElementById('console-rail') as HTMLElement;
    expect(rail.className.split(/\s+/)).not.toContain('hidden');
    expect(rail.className).toContain('fixed');
    const active = within(rail).getByRole('link', { name: 'Webhooks' });
    expect(active.getAttribute('aria-current')).toBe('page');
    expect(document.activeElement).toBe(active);
    const links = within(rail).getAllByRole('link');
    for (const name of ['Overview', 'API keys', 'Limits', 'Documentation', 'Settings', 'Admin', 'Back to chat']) {
      expect(links.some((link) => link.textContent === name)).toBe(true);
    }
    // Every row is 36px tall: a thumb's target, where the strip's were 22px.
    for (const link of links) expect(link.className).toContain('h-9');
  });

  it('closes on Escape and hands focus back to the menu button', async () => {
    mountAt('keys');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Open console menu' }));
    await user.keyboard('{Escape}');
    const toggle = screen.getByRole('button', { name: 'Open console menu' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(toggle);
  });

  it('closes when the scrim is tapped or the section on show is chosen again', async () => {
    mountAt('keys');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Open console menu' }));
    await user.click(screen.getByTestId('console-drawer-scrim'));
    expect(screen.getByRole('button', { name: 'Open console menu' }).getAttribute('aria-expanded')).toBe('false');

    await user.click(screen.getByRole('button', { name: 'Open console menu' }));
    const rail = document.getElementById('console-rail') as HTMLElement;
    const current = within(rail).getByRole('link', { name: 'API keys' });
    // jsdom cannot navigate; the click still reaches React's handler.
    current.addEventListener('click', (event) => event.preventDefault());
    fireEvent.click(current);
    expect(screen.getByRole('button', { name: 'Open console menu' }).getAttribute('aria-expanded')).toBe('false');
  });
});

describe('the address bar and the document title', () => {
  it('names the section on show in the document title', async () => {
    const { consoleTitle } = await import('@/components/devplatform/ConsoleShell');
    state.search = new URLSearchParams('tab=keys');
    route({ 'GET projects': () => jsonResponse({ projects: [] }) });
    const { unmount } = render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(document.title).toBe('API keys · Developer platform · TechSara'));
    unmount();

    state.search = new URLSearchParams('tab=logs');
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(document.title).toBe('Request logs · Developer platform · TechSara'));
    expect(consoleTitle(undefined)).toBe('Developer platform · TechSara');
  });

  it('keeps the section in the title when Next swaps in a fresh metadata <title> on a soft navigation', async () => {
    // Measured in Chrome: on every tab change Next removes the layout's
    // <title> and inserts a new "Developer platform · TechSara" after the
    // shell's effect ran, so a plain document.title assignment lost.
    for (const el of Array.from(document.querySelectorAll('title'))) el.remove();
    const theirs = document.createElement('title');
    theirs.textContent = 'Developer platform · TechSara';
    document.head.appendChild(theirs);
    try {
      state.search = new URLSearchParams('tab=keys');
      route({ 'GET projects': () => jsonResponse({ projects: [] }) });
      render(<ConsoleShell me={ADMIN} />);
      await waitFor(() => expect(document.title).toBe('API keys · Developer platform · TechSara'));
      // The text node React holds is rewritten in place, not replaced.
      expect(theirs.firstChild?.nodeValue).toBe('API keys · Developer platform · TechSara');

      theirs.remove();
      const fresh = document.createElement('title');
      fresh.textContent = 'Developer platform · TechSara';
      document.head.appendChild(fresh);
      await waitFor(() => expect(document.title).toBe('API keys · Developer platform · TechSara'));
      expect(document.querySelectorAll('title')).toHaveLength(1);
    } finally {
      cleanup();
      for (const el of Array.from(document.querySelectorAll('title'))) el.remove();
    }
  });

  it('replaces ?tab=limits with the Overview an admin is actually shown', async () => {
    state.search = new URLSearchParams('tab=limits');
    route({ 'GET overview': () => jsonResponse(overviewOf({})) });
    render(<ConsoleShell me={ADMIN} />);
    await waitFor(() => expect(state.router.replace).toHaveBeenCalledWith('/api', { scroll: false }));
    expect(state.router.push).not.toHaveBeenCalled();
    await waitFor(() => expect(document.title).toBe('Overview · Developer platform · TechSara'));
  });

  it('drops a tab that names nothing and keeps the rest of the query', async () => {
    state.search = new URLSearchParams('tab=nonsense&utm=mail');
    route({ 'GET overview': () => jsonResponse(overviewOf({})) });
    render(<ConsoleShell me={SUPER_ADMIN} />);
    await waitFor(() =>
      expect(state.router.replace).toHaveBeenCalledWith('/api?utm=mail', { scroll: false }),
    );
  });

  it('leaves a tab this account may open exactly as it is', async () => {
    state.search = new URLSearchParams('tab=limits');
    route({ 'GET projects': () => jsonResponse({ projects: [] }) });
    render(<ConsoleShell me={SUPER_ADMIN} />);
    await screen.findByText(/No projects to limit/);
    expect(state.router.replace).not.toHaveBeenCalled();
  });
});

describe('the usage chart axis', () => {
  it('draws every day of the window and leaves a gap, not a zero, for a day with no row', async () => {
    const { usageDays } = await import('@/components/devplatform/Usage');
    const day = (d: string, requests: number, errors = 0) => ({
      day: d,
      requests,
      errors,
      input_tokens: 0,
      output_tokens: 0,
      rate_limited: 0,
    });
    const axis = usageDays({
      range: { days: 5, start: '2026-08-30', end: '2026-09-03' },
      series: [day('2026-08-30', 4, 1), day('2026-09-03', 9)],
    });
    // Across a month boundary, local-midnight labels so no zone moves a day.
    expect(axis.labels).toEqual([
      '2026-08-30T00:00:00',
      '2026-08-31T00:00:00',
      '2026-09-01T00:00:00',
      '2026-09-02T00:00:00',
      '2026-09-03T00:00:00',
    ]);
    expect(axis.requests).toEqual([4, null, null, null, 9]);
    expect(axis.errors).toEqual([1, null, null, null, 0]);
  });

  it('falls back to the listed days when the server sends no usable range', async () => {
    const { usageDays } = await import('@/components/devplatform/Usage');
    const row = { day: '2026-09-13', requests: 600, errors: 0, input_tokens: 0, output_tokens: 0, rate_limited: 0 };
    const axis = usageDays({
      range: { days: 30, start: 'garbage', end: '2026-09-13' },
      series: [row],
    });
    expect(axis.labels).toEqual(['2026-09-13T00:00:00']);
    expect(axis.requests).toEqual([600]);
    expect(usageDays(null)).toEqual({ labels: [], requests: [], errors: [] });
  });

  it('bounds both toolbar selects to the toolbar so a long project name cannot widen a phone', async () => {
    state.search = new URLSearchParams('tab=usage');
    const long = { ...PROJECT, name: 'conformance-node-2026-09-13T07-52-43-441Z' };
    route({
      'GET projects': () => jsonResponse({ projects: [long] }),
      'GET usage': () =>
        jsonResponse({
          range: { days: 30, start: '2026-08-15', end: '2026-09-13' },
          series: [],
          totals: { requests: 0, input_tokens: 0, output_tokens: 0, errors: 0, rate_limited: 0, total_tokens: 0 },
          projects: [],
        }),
    });
    render(<ConsoleShell me={ADMIN} />);
    await screen.findByRole('option', { name: /conformance-node/ });
    for (const label of ['Project', 'Time range']) {
      const wrapper = screen.getByLabelText(label).parentElement!.parentElement as HTMLElement;
      expect(wrapper.className).toContain('min-w-0');
      expect(wrapper.className).toContain('max-w-full');
      expect(wrapper.className).toContain('[&_select]:max-w-full');
    }
  });
});

describe('the usage page when its report fails', () => {
  it('shows the error with a Retry and claims no project served nothing', async () => {
    state.search = new URLSearchParams('tab=usage');
    route({
      'GET projects': () => jsonResponse({ projects: [PROJECT] }),
      'GET usage': () => jsonResponse({ error: { message: 'The console service is restarting.' } }, 503),
    });
    render(<ConsoleShell me={ADMIN} />);
    await screen.findByText('The console service is restarting.');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(screen.queryByText(/No project has served a request/)).toBeNull();
    expect(screen.queryByText(/No API requests in this window/)).toBeNull();
  });
});

describe('a request log status that is wider than its column', () => {
  it('wraps an error code after its underscores instead of running into the next column', async () => {
    const { StatusCell } = await import('@/components/devplatform/RequestLogs');
    render(
      <StatusCell
        row={{ ...(LOG_ROW as unknown as import('@/components/devplatform/types').RequestLogRow), status: 'failed', error_code: 'model_unavailable' }}
      />,
    );
    const cell = screen.getByTestId('log-status');
    expect(cell.textContent).toBe('model_unavailable');
    expect(cell.className).toContain('whitespace-normal');
    expect(cell.className).toContain('[overflow-wrap:anywhere]');
    expect(cell.className).toContain('text-danger');
    // One break opportunity after the underscore, none inside the words.
    expect(cell.querySelectorAll('wbr')).toHaveLength(1);
  });
});

describe('the playground with a long unbroken token', () => {
  it('lets both columns shrink below lg and breaks the answer, the error and the request id anywhere', async () => {
    state.search = new URLSearchParams('tab=playground');
    const token = 'A'.repeat(1200);
    route({
      'GET models': () => jsonResponse({ models: [MODEL], can_manage: false }),
      'POST playground/execute': () =>
        jsonResponse(
          { error: { message: `Refused: see https://docs.example.com/${token}` } },
          429,
          { 'x-request-id': `req_${token}` },
        ),
    });
    render(<ConsoleShell me={ADMIN} />);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText('Input'), 'Say hello');
    await user.click(screen.getByRole('button', { name: 'Run' }));

    const error = await screen.findByTestId('playground-error');
    expect(error.textContent).toContain(token);
    expect(error.className).toContain('min-w-0');
    expect(error.className).toContain('[overflow-wrap:anywhere]');

    const request = screen.getByRole('region', { name: 'Request' });
    const response = screen.getByRole('region', { name: 'Response' });
    const grid = request.parentElement as HTMLElement;
    // An implicit track grows to min-content; an explicit minmax(0,1fr) cannot.
    expect(grid.className.split(/\s+/)).toContain('grid-cols-[minmax(0,1fr)]');
    expect(request.className).toContain('min-w-0');
    expect(response.className).toContain('min-w-0');

    const output = screen.getByTestId('playground-output');
    expect(output.className).toContain('[overflow-wrap:anywhere]');
    // `break-words` does not lower min-content, which is the whole bug.
    expect(output.className).not.toContain('break-words');
    const id = within(response).getByText(`req_${token}`);
    expect(id.className).toContain('[overflow-wrap:anywhere]');
  });
});
