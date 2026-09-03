'use client';

/**
 * The /admin shell: gate, sidebar, content column.
 *
 * On mount it resolves /api/auth/me exactly once. Signed out (401) → hard
 * redirect to /login; signed in without members.read → back to the chat.
 * That gating is a COURTESY — every /admin/api/* endpoint 404s server-side
 * without the capability — the client only avoids rendering dead links
 * (Audit Log appears solely with audit.read). Pages read the resolved
 * ME_PAYLOAD from AdminMeContext instead of re-probing.
 */

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Loader } from '@/components/Loader';
import { TechSaraMark } from '@/components/TechSaraMark';
import { AdminMeProvider } from '@/components/admin/AdminMeContext';
import {
  OFFLINE_MESSAGE,
  ROLE_LABEL,
  can,
  parseMe,
  type Me,
} from '@/components/admin/api';
import {
  IconArrowLeft,
  IconGrid,
  IconMail,
  IconShield,
  IconSliders,
  IconUsers,
} from '@/components/admin/icons';
import { nav } from '@/components/admin/nav';
import { ErrorPanel } from '@/components/admin/ui';

interface NavItem {
  href: string;
  label: string;
  icon: ReactNode;
  /** Overview matches exactly; sections match their whole subtree. */
  exact?: boolean;
}

function navItems(me: Me): NavItem[] {
  const items: NavItem[] = [
    { href: '/admin', label: 'Overview', icon: <IconGrid size={15} />, exact: true },
    { href: '/admin/members', label: 'Members', icon: <IconUsers size={15} /> },
    {
      href: '/admin/invitations',
      label: 'Invitations',
      icon: <IconMail size={15} />,
    },
    // Which TOOLS members may use. Visible to anyone who can read the member
    // list; the page itself is read-only without settings.manage.
    { href: '/admin/access', label: 'Access', icon: <IconSliders size={15} /> },
  ];
  if (can(me, 'audit.read')) {
    items.push({
      href: '/admin/audit',
      label: 'Audit Log',
      icon: <IconShield size={15} />,
    });
  }
  return items;
}

export default function AdminLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  const load = useCallback(async () => {
    setError(null);
    let res: Response;
    try {
      res = await fetch('/api/auth/me', { cache: 'no-store' });
    } catch {
      // Network failure (status 0) never redirects — stay usable offline.
      setError(OFFLINE_MESSAGE);
      return;
    }
    if (res.status === 401) {
      nav.assign('/login');
      return;
    }
    if (!res.ok) {
      setError('The session could not be checked. Try again.');
      return;
    }
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      setError('The session could not be checked. Try again.');
      return;
    }
    const parsed = parseMe(body);
    if (!parsed || !can(parsed, 'members.read')) {
      // Signed in, but not an admin — the admin area does not exist for them.
      nav.assign('/');
      return;
    }
    setMe(parsed);
  }, []);

  useEffect(() => {
    void load();
  }, [load, attempt]);

  if (error) {
    return (
      <div className="flex h-dvh items-center justify-center bg-bg px-4 text-ink">
        <div className="w-full max-w-sm">
          <ErrorPanel message={error} onRetry={() => setAttempt((n) => n + 1)} />
        </div>
      </div>
    );
  }

  if (!me) {
    return (
      <div className="flex h-dvh items-center justify-center bg-bg text-muted">
        <Loader size={40} />
      </div>
    );
  }

  const items = navItems(me);
  const isActive = (item: NavItem) =>
    item.exact ? pathname === item.href : pathname.startsWith(item.href);
  // One height for every nav row (36px), one icon box (18px), so the labels
  // form a single column whatever the glyph inside each icon looks like.
  const rowClass = (active: boolean) =>
    `flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-sm transition-colors duration-ts focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-sidebar ${
      active
        ? 'bg-surface-2 font-medium text-ink'
        : 'text-icon hover:bg-surface-2 hover:text-ink'
    }`;

  return (
    <AdminMeProvider me={me}>
      <div className="flex h-dvh overflow-hidden bg-bg text-ink">
        {/* Desktop: fixed 240px admin rail. */}
        <aside
          aria-label="Admin navigation"
          className="hidden w-60 shrink-0 flex-col border-r border-border bg-sidebar md:flex"
        >
          <div className="flex items-center gap-2 px-3 pb-1 pt-3">
            <TechSaraMark size={28} />
            <span className="truncate text-sm font-semibold">TechSara</span>
            <span className="rounded border border-border px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-faint">
              Admin
            </span>
          </div>
          <p className="truncate px-3 pb-2 text-xs text-faint">
            {me.workspace.name}
          </p>

          <nav className="mt-1 min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2">
            {items.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                aria-current={isActive(item) ? 'page' : undefined}
                className={rowClass(isActive(item))}
              >
                <span aria-hidden className="flex w-[18px] shrink-0 justify-center">
                  {item.icon}
                </span>
                {item.label}
              </Link>
            ))}
          </nav>

          <div className="border-t border-border p-2">
            <Link href="/" className={rowClass(false)}>
              <span aria-hidden className="flex w-[18px] shrink-0 justify-center">
                <IconArrowLeft size={15} />
              </span>
              Back to chat
            </Link>
            <p className="truncate px-2.5 pb-1 pt-1.5 text-xs text-faint">
              {me.user.name} · {ROLE_LABEL[me.workspace.role] ?? me.workspace.role}
            </p>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          {/* Mobile: slim header with the same links. */}
          <header className="flex h-[52px] shrink-0 items-center gap-3 overflow-x-auto border-b border-border px-3 md:hidden">
            <TechSaraMark size={24} />
            {items.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                aria-current={isActive(item) ? 'page' : undefined}
                className={`shrink-0 text-sm transition-colors duration-ts ${
                  isActive(item) ? 'font-medium text-ink' : 'text-muted hover:text-ink'
                }`}
              >
                {item.label}
              </Link>
            ))}
            <Link href="/" className="ml-auto shrink-0 text-sm text-muted hover:text-ink">
              Back to chat
            </Link>
          </header>

          <main className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto w-full max-w-admin px-4 py-6 md:px-8 md:py-10">
              {children}
            </div>
          </main>
        </div>
      </div>
    </AdminMeProvider>
  );
}
