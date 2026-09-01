// @vitest-environment jsdom
/**
 * The /login form (workstream B).
 *
 * What matters here is the wire contract and the failure vocabulary:
 * - POST /api/auth/login carries {email, password, remember} exactly, with
 *   `remember` following the checkbox (default ON);
 * - 200 leaves the page via a full navigation to "/";
 * - 401 and 429 surface the orchestrator's {detail} verbatim in a
 *   role="alert" region — the wording is decided server-side;
 * - a thrown fetch is "Cannot reach the server.", never a login verdict.
 *
 * `navigate` is injected because jsdom's window.location.assign is
 * unforgeable; production keeps the real assign default.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LoginForm } from '@/components/auth/LoginForm';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const jsonResponse = (body: unknown, ok = true, status = 200) => ({
  ok,
  status,
  json: async () => body,
});

function fill(email = 'naman@techsara.com', password = 'correct horse battery') {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: email } });
  fireEvent.change(screen.getByLabelText('Password'), {
    target: { value: password },
  });
}

describe('login submit', () => {
  it('POSTs {email, password, remember:true} and navigates to / on 200', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ username: 'naman', user: { id: 1 } }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const navigate = vi.fn();
    render(<LoginForm navigate={navigate} />);

    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/auth/login');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual({
      email: 'naman@techsara.com',
      password: 'correct horse battery',
      remember: true,
    });
  });

  it('sends remember:false when "Stay signed in" is unchecked', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ username: 'naman' }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const navigate = vi.fn();
    render(<LoginForm navigate={navigate} />);

    const checkbox = screen.getByRole('checkbox', {
      name: 'Stay signed in',
    }) as HTMLInputElement;
    expect(checkbox.checked).toBe(true); // default ON
    fireEvent.click(checkbox);
    expect(checkbox.checked).toBe(false);

    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const init = fetchMock.mock.calls[0][1];
    expect(JSON.parse(init?.body as string).remember).toBe(false);
  });

  it('shows the 401 detail verbatim as a role="alert" error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ detail: 'Incorrect email or password.' }, false, 401),
      ),
    );
    const navigate = vi.fn();
    render(<LoginForm navigate={navigate} />);

    fill('naman@techsara.com', 'wrong-password');
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Incorrect email or password.');
    expect(navigate).not.toHaveBeenCalled();
  });

  it('shows the 429 throttle message from the server', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          { detail: 'Too many attempts. Try again in 60 seconds.' },
          false,
          429,
        ),
      ),
    );
    render(<LoginForm navigate={vi.fn()} />);

    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain(
      'Too many attempts. Try again in 60 seconds.',
    );
  });

  it('says "Cannot reach the server." when fetch itself throws', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    const navigate = vi.fn();
    render(<LoginForm navigate={navigate} />);

    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Cannot reach the server.');
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe('password visibility toggle', () => {
  it('reveals and re-hides the password without losing the value', () => {
    vi.stubGlobal('fetch', vi.fn());
    render(<LoginForm navigate={vi.fn()} />);

    const input = screen.getByLabelText('Password') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 's3cret-enough' } });
    expect(input.type).toBe('password');

    fireEvent.click(screen.getByRole('button', { name: 'Show password' }));
    expect(input.type).toBe('text');
    expect(input.value).toBe('s3cret-enough');

    fireEvent.click(screen.getByRole('button', { name: 'Hide password' }));
    expect(input.type).toBe('password');
  });
});

describe('login page furniture', () => {
  it('has the admin footer and no signup link', () => {
    vi.stubGlobal('fetch', vi.fn());
    render(<LoginForm navigate={vi.fn()} />);

    expect(
      screen.getByText('Need access? Contact your workspace administrator.'),
    ).toBeTruthy();
    expect(screen.queryByText(/sign up/i)).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();

    // Proper autofill hints.
    expect(
      screen.getByLabelText('Email').getAttribute('autocomplete'),
    ).toBe('email');
    expect(
      screen.getByLabelText('Password').getAttribute('autocomplete'),
    ).toBe('current-password');
  });
});
