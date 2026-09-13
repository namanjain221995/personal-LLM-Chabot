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
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ProjectSelect } from '@/components/devplatform/shared';
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
}));

vi.mock('next/navigation', () => ({
  usePathname: () => '/api',
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
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

vi.mock('@/components/admin/AdminDialog', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/components/admin/AdminDialog')>();
  return {
    ...real,
    AdminDialog: (props: Parameters<typeof real.AdminDialog>[0]) => {
      if (state.dialogsAlwaysMounted && !props.open) {
        return <div data-closed="true">{props.children}</div>;
      }
      return real.AdminDialog(props);
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
import { limitsChanges } from '@/components/devplatform/Limits';
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
    { tab: 'playground', routes: { models: { models: [] } }, expect: /No model is published/i },
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
    // orchestrator/app/apiplatform/scopes.py: Scope has these four members and
    // `validate` raises UnknownScopeError for anything else, so a fifth box on
    // this form would be a key the platform refuses to mint.
    expect(SCOPES.map((s) => s.id)).toEqual([
      'models.read',
      'responses.read',
      'responses.write',
      'usage.read',
    ]);
  });

  it("repeats the server's own description of each one", () => {
    const hints = Object.fromEntries(SCOPES.map((s) => [s.id, s.hint]));
    expect(hints['models.read']).toBe('List the models this key may use.');
    expect(hints['responses.read']).toBe('Read responses created by this project.');
    expect(hints['responses.write']).toBe('Create and cancel responses.');
    expect(hints['usage.read']).toBe('Read this project’s usage counters.');
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
      scopes: ['models.read', 'responses.read', 'responses.write'],
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
    await waitFor(() => expect(screen.getByText('Chat · Streaming · Vision')).toBeTruthy());
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
  // The inline min-width is the DESKTOP floor; below lg a wrapper must lift it,
  // or the table keeps scrolling sideways past a 400px screen.
  const lifted = table.closest('[class*="max-lg:[&_table]:!min-w-0"]') !== null;
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
