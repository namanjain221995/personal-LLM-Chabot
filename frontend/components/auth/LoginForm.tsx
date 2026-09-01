'use client';

/**
 * The /login form. Enterprise sign-in only — there is no self-serve signup;
 * accounts exist by invitation, so the footer points at the workspace
 * administrator instead of a register link.
 *
 * POST /api/auth/login {email, password, remember}. 200 sets the HttpOnly
 * session cookie upstream, so success is a FULL navigation (assign('/')) —
 * client routing would keep app state from before the session existed.
 * Errors surface the orchestrator's {detail} verbatim (401 wording and 429
 * throttle text are decided server-side); a thrown fetch means the server
 * never answered, which is its own message, not a login failure.
 */

import { useState, type FormEvent } from 'react';
import { Loader } from '../Loader';
import { TechSaraMark } from '../TechSaraMark';
import { AuthField } from './AuthField';
import { FormError } from './FormError';
import { OFFLINE_MESSAGE, readDetail } from './http';

export interface LoginFormProps {
  /**
   * Injected for tests. Production keeps the default: a full navigation so
   * every fetch after login carries the new session cookie.
   */
  navigate?: (url: string) => void;
}

export function LoginForm({
  navigate = (url) => window.location.assign(url),
}: LoginFormProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);

    let res: Response;
    try {
      res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email: email.trim(), password, remember }),
      });
    } catch {
      setBusy(false);
      setError(OFFLINE_MESSAGE);
      return;
    }

    if (res.ok) {
      // Stay in the busy state while the browser navigates.
      navigate('/');
      return;
    }

    const detail = await readDetail(res);
    setBusy(false);
    if (res.status === 401) {
      setError(detail ?? 'Incorrect email or password.');
    } else if (res.status === 429) {
      setError(detail ?? 'Too many attempts. Please wait a moment and try again.');
    } else {
      setError(detail ?? `Sign-in failed (status ${res.status}). Please try again.`);
    }
  }

  return (
    <div>
      <TechSaraMark size={40} />
      {/* Larger than the app's page headings: on a page with one job, the
          welcome IS the layout, as in the reference design. */}
      <h1 className="mt-7 text-[34px] font-bold leading-[1.15] tracking-tight">
        Welcome back
      </h1>
      <p className="mt-2.5 text-sm leading-relaxed text-muted">
        Sign in to continue to your{' '}
        <span className="font-medium text-ink">TechSara</span> workspace.
      </p>

      <form onSubmit={onSubmit} className="mt-8 space-y-4">
        <FormError message={error} />

        <AuthField
          id="login-email"
          label="Email"
          type="email"
          value={email}
          onChange={setEmail}
          autoComplete="email"
          autoFocus
          placeholder="you@company.com"
          required
        />

        <AuthField
          id="login-password"
          label="Password"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
          required
        />

        <label className="flex w-fit cursor-pointer items-center gap-2.5 pt-0.5 text-sm text-muted transition-colors duration-ts hover:text-ink">
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            className="h-4 w-4 rounded accent-accent"
          />
          Stay signed in
        </label>

        <button
          type="submit"
          disabled={busy}
          className="flex w-full items-center justify-center gap-2 rounded-xl bg-accent-strong px-4 py-3 text-sm font-semibold text-white shadow-sm transition-all duration-ts hover:brightness-125 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-35"
        >
          {busy && <Loader size={16} />}
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>

      <p className="mt-10 border-t border-border pt-6 text-xs text-faint">
        Need access? Contact your workspace administrator.
      </p>
    </div>
  );
}
