'use client';

/**
 * The /accept-invite form.
 *
 * The token comes from ?token=... and is probed with
 * GET /api/auth/invitations/{token}. The orchestrator deliberately answers
 * 404 for expired, used, revoked and unknown alike, so the UI has exactly one
 * "no longer valid" state — nothing to enumerate, nothing to leak. A thrown
 * fetch is different: the server never answered, so the user gets a retry,
 * not a verdict about their invitation.
 *
 * Accepting (POST /api/auth/invitations/accept) auto-logs-in upstream —
 * success is a full navigation to "/" so the fresh session cookie applies
 * everywhere (same reasoning as LoginForm).
 */

import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { IconAlert } from '../icons';
import { Loader } from '../Loader';
import { TechSaraMark } from '../TechSaraMark';
import { AuthField } from './AuthField';
import { FormError } from './FormError';
import { OFFLINE_MESSAGE, readDetail } from './http';

const MIN_PASSWORD_CHARS = 10;

interface Invite {
  email: string;
  workspace_name: string;
  name: string;
}

type Phase =
  | { kind: 'loading' }
  | { kind: 'invalid' }
  | { kind: 'offline' }
  | { kind: 'ready'; token: string; invite: Invite };

export interface AcceptInviteFormProps {
  /** Injected for tests; production defaults to a full navigation. */
  navigate?: (url: string) => void;
}

export function AcceptInviteForm({
  navigate = (url) => window.location.assign(url),
}: AcceptInviteFormProps) {
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' });
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const token = new URLSearchParams(window.location.search).get('token');
    if (!token) {
      setPhase({ kind: 'invalid' });
      return;
    }
    setPhase({ kind: 'loading' });

    let res: Response;
    try {
      res = await fetch(`/api/auth/invitations/${encodeURIComponent(token)}`, {
        cache: 'no-store',
      });
    } catch {
      setPhase({ kind: 'offline' });
      return;
    }
    if (!res.ok) {
      setPhase({ kind: 'invalid' });
      return;
    }

    try {
      const body = (await res.json()) as {
        email?: unknown;
        workspace_name?: unknown;
        name?: unknown;
      };
      if (typeof body.email !== 'string' || typeof body.workspace_name !== 'string') {
        setPhase({ kind: 'invalid' });
        return;
      }
      const invite: Invite = {
        email: body.email,
        workspace_name: body.workspace_name,
        name: typeof body.name === 'string' ? body.name : '',
      };
      setName(invite.name);
      setPhase({ kind: 'ready', token, invite });
    } catch {
      setPhase({ kind: 'invalid' });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy || phase.kind !== 'ready') return;

    if (!name.trim()) {
      setError('Please enter your name.');
      return;
    }
    if (password.length < MIN_PASSWORD_CHARS) {
      setError(`Password must be at least ${MIN_PASSWORD_CHARS} characters.`);
      return;
    }
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }

    setError(null);
    setBusy(true);

    let res: Response;
    try {
      res = await fetch('/api/auth/invitations/accept', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ token: phase.token, name: name.trim(), password }),
      });
    } catch {
      setBusy(false);
      setError(OFFLINE_MESSAGE);
      return;
    }

    if (res.ok) {
      // Auto-login happened upstream; stay busy while the browser navigates.
      navigate('/');
      return;
    }

    const detail = await readDetail(res);
    setBusy(false);
    if (res.status === 404) {
      // The invitation died between page load and submit.
      setPhase({ kind: 'invalid' });
    } else {
      setError(detail ?? `Could not create the account (status ${res.status}).`);
    }
  }

  if (phase.kind === 'loading') {
    return (
      <div className="flex flex-col items-center py-24">
        <Loader size={40} label="Checking invitation" />
        <p className="mt-4 text-sm text-muted">Checking your invitation…</p>
      </div>
    );
  }

  if (phase.kind === 'offline') {
    return (
      <div>
        <TechSaraMark size={44} />
        <h1 className="mt-6 text-2xl font-semibold tracking-tight">
          Can&apos;t check your invitation
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-muted">{OFFLINE_MESSAGE}</p>
        <button
          type="button"
          onClick={() => void load()}
          className="mt-6 inline-flex items-center rounded-md border border-border bg-surface px-4 py-2 text-sm font-medium text-ink transition-colors duration-ts hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Try again
        </button>
      </div>
    );
  }

  if (phase.kind === 'invalid') {
    return (
      <div role="alert">
        <div
          className="flex h-12 w-12 items-center justify-center rounded-full"
          style={{
            background: 'color-mix(in srgb, var(--ts-danger) 12%, transparent)',
          }}
        >
          <IconAlert size={22} className="text-danger" />
        </div>
        <h1 className="mt-6 text-2xl font-semibold tracking-tight">
          This invitation is no longer valid
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-muted">
          The link may have expired, already been used, or been revoked. Please
          contact your workspace administrator to request a new invitation.
        </p>
      </div>
    );
  }

  const { invite } = phase;

  return (
    <div>
      <TechSaraMark size={44} />
      <h1 className="mt-6 text-2xl font-semibold tracking-tight">
        Join {invite.workspace_name}
      </h1>
      <p className="mt-1.5 text-sm text-muted">
        You&apos;ve been invited to the {invite.workspace_name} workspace.
        Finish setting up your account below.
      </p>

      <form onSubmit={onSubmit} className="mt-8 space-y-4">
        <FormError message={error} />

        <AuthField
          id="invite-email"
          label="Email"
          type="email"
          value={invite.email}
          readOnly
        />

        <AuthField
          id="invite-name"
          label="Name"
          value={name}
          onChange={setName}
          autoComplete="name"
          autoFocus
          required
        />

        <AuthField
          id="invite-password"
          label="Password"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
          hint={`At least ${MIN_PASSWORD_CHARS} characters.`}
          required
        />

        <AuthField
          id="invite-confirm"
          label="Confirm password"
          type="password"
          value={confirm}
          onChange={setConfirm}
          autoComplete="new-password"
          required
        />

        <button
          type="submit"
          disabled={busy}
          className="flex w-full items-center justify-center gap-2 rounded-xl bg-accent-strong px-4 py-3 text-sm font-semibold text-white shadow-sm transition-all duration-ts hover:brightness-125 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-35"
        >
          {busy && <Loader size={16} />}
          {busy ? 'Creating account…' : 'Create account'}
        </button>
      </form>

      <p className="mt-6 text-xs leading-relaxed text-faint">
        Workspace content may be accessible to authorized administrators in
        accordance with company policy.
      </p>
    </div>
  );
}
