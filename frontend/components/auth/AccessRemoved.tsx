/**
 * The page a removed or deactivated member lands on (2026-09-03).
 *
 * Before this, an admin removing someone produced — for that person — a
 * bare 401, a bounce to the sign-in form, and then "Incorrect email or
 * password" on every attempt: the login form is deliberately generic (it
 * must not confirm which accounts exist), so it can never explain. The
 * explanation belongs HERE, and only a browser that held the real session
 * gets sent here (the server matches the dead cookie's secret first).
 *
 * SERVER-RENDERED on purpose. Everything shown arrives in the query string
 * the session-end handler wrote (nothing secret: the code, the workspace
 * name, when, the admins' contact emails), so the page needs no request,
 * no hook and no JavaScript — the explanation is in the first byte of HTML,
 * not painted after a blank Suspense frame. `app/access-removed/page.tsx`
 * reads `searchParams` and passes props.
 */

import { TechSaraMark } from '../TechSaraMark';
import { IconAlert, IconLogout } from '../icons';

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

export interface AccessRemovedCopy {
  title: string;
  lead: string;
  detail: string;
  /** Under the contact list: what asking an admin can achieve. */
  ask: string;
  /** The one button. A deactivated account may be reactivated and sign in
      as itself again; a removed one cannot, so it is offered a different
      account. */
  button: string;
}

/** Pure, so the wording is unit-tested. */
export function accessRemovedCopy(
  code: string | null | undefined,
  workspace: string,
): AccessRemovedCopy {
  const ws = workspace ? `the ${workspace} workspace` : 'this workspace';
  if (code === 'account_disabled') {
    return {
      title: 'Your account has been deactivated',
      lead: `An administrator deactivated your ${APP_NAME} account in ${ws}.`,
      detail:
        'You have been signed out on every device. Your conversations and ' +
        'files are kept exactly as they were, but this account cannot sign ' +
        'in until an administrator reactivates it.',
      ask: 'If you think this was a mistake, an administrator can reactivate your account in one click.',
      button: 'Back to sign-in',
    };
  }
  return {
    title: 'Your access has been removed',
    lead: `An administrator removed your access to ${ws}.`,
    detail:
      'You have been signed out on every device. Your conversations and ' +
      'files are kept, but this account can no longer sign in to ' +
      `${APP_NAME}.`,
    ask: 'Need access again? An administrator can invite you back at any time.',
    button: 'Sign in with a different account',
  };
}

export interface Contact {
  name: string;
  email: string;
}

/** "Priya Sharma <priya@x.com>" → {name, email}; a bare email → {email}. */
export function parseContact(raw: string): Contact | null {
  const m = /^(.*?)\s*<([^<>\s]+@[^<>\s]+)>$/.exec(raw.trim());
  if (m) return { name: m[1].trim(), email: m[2] };
  const email = raw.trim();
  return /^[^\s@]+@[^\s@]+$/.test(email) ? { name: '', email } : null;
}

/**
 * "3 Sep 2026, 02:40 UTC" — rendered on the server, so the zone is stated
 * rather than silently being the server's.
 */
export function formatWhen(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const date = d.toLocaleDateString('en-GB', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
  const time = d.toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'UTC',
  });
  return `${date}, ${time} UTC`;
}

export function AccessRemoved({
  code,
  workspace = '',
  endedAt,
  contacts = [],
}: {
  code?: string | null;
  workspace?: string;
  endedAt?: string | null;
  contacts?: Contact[];
}) {
  const copy = accessRemovedCopy(code, workspace);
  const when = formatWhen(endedAt);

  return (
    <div data-testid="access-removed">
      <TechSaraMark size={40} />

      <div
        className="mt-7 flex h-12 w-12 items-center justify-center rounded-full"
        style={{ background: 'color-mix(in srgb, var(--ts-danger) 12%, transparent)' }}
        aria-hidden
      >
        <IconAlert size={22} className="text-danger" />
      </div>

      <h1 className="mt-5 text-[30px] font-bold leading-[1.15] tracking-tight">
        {copy.title}
      </h1>
      <p className="mt-3 text-sm leading-relaxed text-ink">
        {copy.lead}
        {when ? (
          <>
            {' '}
            <span className="text-muted">({when})</span>
          </>
        ) : null}
      </p>
      <p className="mt-2 text-sm leading-relaxed text-muted">{copy.detail}</p>

      <section
        aria-label="Who to contact"
        className="mt-6 rounded-xl border border-border bg-surface p-4"
      >
        <p className="text-sm font-medium text-ink">{copy.ask}</p>
        {contacts.length > 0 ? (
          <ul className="mt-3 space-y-1.5">
            {contacts.map((c) => (
              <li key={c.email} className="text-sm">
                <a
                  href={`mailto:${c.email}?subject=${encodeURIComponent(
                    `${APP_NAME} access`,
                  )}`}
                  className="font-medium text-accent underline-offset-2 hover:underline"
                >
                  {c.name || c.email}
                </a>
                {c.name ? <span className="text-muted"> · {c.email}</span> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-muted">
            Contact your workspace administrator.
          </p>
        )}
      </section>

      <a
        href="/login"
        className="mt-6 flex w-full items-center justify-center gap-2 rounded-xl border border-border bg-bg px-4 py-3 text-sm font-semibold text-ink transition-colors duration-ts hover:bg-surface focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-accent/30"
      >
        <IconLogout size={16} />
        {copy.button}
      </a>
    </div>
  );
}
