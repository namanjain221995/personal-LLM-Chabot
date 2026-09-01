'use client';

/**
 * Security + Sessions sections of the settings dialog (enterprise auth
 * retrofit).
 *
 * Security — change password against POST /api/auth/password. Success is a
 * toast; a wrong current password (403) and a weak new one (422 {detail})
 * are inline errors next to the form. The min-length check mirrors the
 * server rule (10 chars) to save a round-trip; the server stays the
 * authority.
 *
 * Sessions — GET /api/auth/sessions, rendered with a COARSE browser · OS
 * label parsed from the user agent (never the raw UA string: it is long,
 * leaky, and meaningless to most people). Revoking one session or all
 * others goes through ConfirmDialog first — both are destructive from the
 * other device's point of view.
 */

import { useCallback, useEffect, useState, type FormEvent } from 'react';
import type { FetchLike } from '@/lib/auth';
import { formatWhen } from '@/lib/format';
import { ConfirmDialog } from './ConfirmDialog';
import { Loader } from './Loader';
import { useToast } from './Providers';
import { IconAlert } from './icons';

/* ------------------------------------------------------------- user agent */

/**
 * Coarse "Chrome · Linux" from a raw user agent. Order matters: Chrome UAs
 * contain "Safari", Edge contains "Chrome", Android contains "Linux", and
 * iPads mention "Mac OS" — the more specific token wins.
 */
export function describeUserAgent(ua: string | null | undefined): string {
  if (!ua) return 'Unknown device';
  const browser = /edg(?:e|a|ios)?\//i.test(ua)
    ? 'Edge'
    : /opr\/|opera/i.test(ua)
      ? 'Opera'
      : /firefox|fxios/i.test(ua)
        ? 'Firefox'
        : /chrome|crios/i.test(ua)
          ? 'Chrome'
          : /safari/i.test(ua)
            ? 'Safari'
            : /curl|wget|python-requests|httpx/i.test(ua)
              ? 'API client'
              : null;
  const os = /windows/i.test(ua)
    ? 'Windows'
    : /iphone|ipad|ipod/i.test(ua)
      ? 'iOS'
      : /android/i.test(ua)
        ? 'Android'
        : /mac os|macintosh/i.test(ua)
          ? 'macOS'
          : /linux|x11/i.test(ua)
            ? 'Linux'
            : null;
  if (browser && os) return `${browser} · ${os}`;
  return browser ?? os ?? 'Unknown device';
}

async function readDetail(res: Response): Promise<string | null> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    return typeof body.detail === 'string' ? body.detail : null;
  } catch {
    return null;
  }
}

/* --------------------------------------------------------------- password */

/** Mirrors the orchestrator's minimum (contract: 422 under 10 chars). */
const MIN_PASSWORD_LENGTH = 10;

const FIELD_CLASS =
  'w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none';

export function PasswordSection({ fetchFn = fetch }: { fetchFn?: FetchLike }) {
  const { toast } = useToast();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy) return;
    if (next !== confirm) {
      setError('New passwords do not match.');
      return;
    }
    if (next.length < MIN_PASSWORD_LENGTH) {
      setError(
        `New password must be at least ${MIN_PASSWORD_LENGTH} characters.`,
      );
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const res = await fetchFn('/api/auth/password', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          current_password: current,
          new_password: next,
        }),
      });
      if (res.ok) {
        toast('Password updated');
        setCurrent('');
        setNext('');
        setConfirm('');
        return;
      }
      if (res.status === 403) {
        setError('Current password is incorrect.');
        return;
      }
      setError((await readDetail(res)) ?? 'Could not change the password.');
    } catch {
      setError('Network error — the password was not changed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Security">
      <h3 className="text-sm font-semibold text-ink">Change password</h3>
      <p className="mt-1 text-xs text-muted">
        Other sessions stay signed in — revoke them under Sessions.
      </p>
      <form onSubmit={submit} className="mt-4 max-w-xs space-y-3">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">
            Current password
          </span>
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            className={FIELD_CLASS}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">
            New password
          </span>
          <input
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            className={FIELD_CLASS}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-muted">
            Confirm new password
          </span>
          <input
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            className={FIELD_CLASS}
          />
        </label>

        {error && (
          <p role="alert" className="flex items-start gap-1.5 text-sm text-danger">
            <IconAlert size={14} className="mt-0.5 shrink-0" />
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy || !current || !next || !confirm}
          className="inline-flex items-center gap-2 rounded-md bg-accent-strong px-4 py-2 text-sm font-medium text-white transition-all duration-ts hover:brightness-110 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-35"
        >
          {busy && <Loader size={16} />}
          Change password
        </button>
      </form>
    </section>
  );
}

/* --------------------------------------------------------------- sessions */

export interface SessionInfo {
  id: string;
  current: boolean;
  created_at: string;
  last_seen_at: string;
  user_agent: string | null;
}

type PendingRevoke = { kind: 'one'; id: string } | { kind: 'others' } | null;

export function SessionsSection({ fetchFn = fetch }: { fetchFn?: FetchLike }) {
  const { toast } = useToast();
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingRevoke>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await fetchFn('/api/auth/sessions', { cache: 'no-store' });
      if (!res.ok) {
        setError('Could not load sessions.');
        return;
      }
      const body = (await res.json()) as { sessions?: SessionInfo[] };
      const rows = Array.isArray(body.sessions) ? body.sessions : [];
      // Current device first, regardless of server order.
      setSessions(
        [...rows].sort((a, b) => Number(b.current) - Number(a.current)),
      );
    } catch {
      setError('Could not load sessions.');
    }
  }, [fetchFn]);

  useEffect(() => {
    void load();
  }, [load]);

  async function revoke(target: Exclude<PendingRevoke, null>) {
    setPending(null);
    setBusy(true);
    try {
      const res = await fetchFn('/api/auth/sessions/revoke', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(
          target.kind === 'one' ? { session_id: target.id } : { others: true },
        ),
      });
      if (!res.ok) {
        toast('Could not sign out the session', 'error');
        return;
      }
      const body = (await res.json().catch(() => ({}))) as {
        revoked?: number;
      };
      const n = typeof body.revoked === 'number' ? body.revoked : 1;
      toast(n === 1 ? 'Signed out 1 session' : `Signed out ${n} sessions`);
      await load();
    } catch {
      toast('Could not sign out the session', 'error');
    } finally {
      setBusy(false);
    }
  }

  const others = sessions?.some((s) => !s.current) ?? false;

  return (
    <section aria-label="Sessions">
      <h3 className="text-sm font-semibold text-ink">Sessions</h3>
      <p className="mt-1 text-xs text-muted">
        Everywhere this account is signed in.
      </p>

      {sessions === null && !error && (
        <div className="flex justify-center py-8">
          <Loader size={22} label="Loading sessions" />
        </div>
      )}

      {error && (
        <div className="mt-4 flex items-center gap-3">
          <p className="flex items-center gap-1.5 text-sm text-danger">
            <IconAlert size={14} className="shrink-0" />
            {error}
          </p>
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            Retry
          </button>
        </div>
      )}

      {sessions !== null && sessions.length > 0 && (
        <ul className="mt-4 divide-y divide-border rounded-ts border border-border">
          {sessions.map((s) => (
            <li key={s.id} className="flex items-center gap-3 px-3 py-2.5">
              <div className="min-w-0 flex-1">
                <p className="flex items-center gap-2 text-sm text-ink">
                  <span className="truncate">
                    {describeUserAgent(s.user_agent)}
                  </span>
                  {s.current && (
                    <span className="shrink-0 rounded-full border border-accent/50 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">
                      This device
                    </span>
                  )}
                </p>
                <p className="mt-0.5 truncate text-xs text-muted">
                  Created {formatWhen(s.created_at)} · Last seen{' '}
                  {formatWhen(s.last_seen_at)}
                </p>
              </div>
              {!s.current && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setPending({ kind: 'one', id: s.id })}
                  className="shrink-0 rounded-md border border-border px-2.5 py-1 text-xs text-danger transition-colors duration-ts hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-35"
                >
                  Sign out
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {others && (
        <button
          type="button"
          disabled={busy}
          onClick={() => setPending({ kind: 'others' })}
          className="mt-3 rounded-lg border border-border px-3 py-1.5 text-sm text-danger transition-colors duration-ts hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-35"
        >
          Log out other sessions
        </button>
      )}

      <ConfirmDialog
        open={pending !== null}
        title={
          pending?.kind === 'others'
            ? 'Log out other sessions?'
            : 'Sign out this session?'
        }
        body={
          pending?.kind === 'others'
            ? 'Every session except this one will be signed out immediately.'
            : 'That device will be signed out immediately and will have to log in again.'
        }
        confirmLabel={pending?.kind === 'others' ? 'Log out others' : 'Sign out'}
        onConfirm={() => {
          const target = pending;
          if (target) void revoke(target);
        }}
        onCancel={() => setPending(null)}
      />
    </section>
  );
}
