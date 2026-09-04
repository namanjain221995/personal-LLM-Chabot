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
  IconChart,
  IconCloudCog,
  IconCpu,
  IconFlask,
  IconGauge,
  IconGrid,
  IconLink,
  IconMail,
  IconMessages,
  IconMic,
  IconServer,
  IconShield,
  IconSliders,
  IconTrophy,
  IconUsers,
  IconWorld,
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

interface NavGroup {
  /** Rendered as the small caps rule above the group. Omitted for the first. */
  title?: string;
  items: NavItem[];
}

/**
 * The rail's shape.
 *
 * Grouped since the console arrived (2026-09-04): fourteen flat links is a
 * list to read, where five labelled groups is a structure to scan. Groups
 * appear only when their capability does — the analytics and infrastructure
 * sections are ANALYTICS_READ, which rbac.py gives to super admins alone, and
 * the pages behind them 404 for everyone else regardless of what is drawn
 * here.
 */
function navGroups(me: Me): NavGroup[] {
  const groups: NavGroup[] = [
    {
      items: [
        { href: '/admin', label: 'Overview', icon: <IconGrid size={15} />, exact: true },
      ],
    },
    {
      title: 'Access',
      items: [
        { href: '/admin/members', label: 'Members', icon: <IconUsers size={15} /> },
        {
          href: '/admin/invitations',
          label: 'Invitations',
          icon: <IconMail size={15} />,
        },
        // Which TOOLS members may use. Visible to anyone who can read the
        // member list; the page is read-only without settings.manage.
        { href: '/admin/access', label: 'Tool access', icon: <IconSliders size={15} /> },
      ],
    },
  ];
  if (can(me, 'analytics.read')) {
    groups.push(
      {
        title: 'Analytics',
        items: [
          {
            href: '/admin/analytics',
            label: 'Usage',
            icon: <IconChart size={15} />,
            exact: true,
          },
          {
            href: '/admin/analytics/leaderboards',
            label: 'Leaderboards',
            icon: <IconTrophy size={15} />,
          },
          {
            href: '/admin/analytics/chat',
            label: 'Chat',
            icon: <IconMessages size={15} />,
          },
          {
            href: '/admin/analytics/research',
            label: 'Deep research',
            icon: <IconFlask size={15} />,
          },
          {
            href: '/admin/analytics/search',
            label: 'Web search',
            icon: <IconWorld size={15} />,
          },
          {
            href: '/admin/analytics/salesforce',
            label: 'Salesforce',
            icon: <IconCloudCog size={15} />,
          },
          {
            href: '/admin/analytics/voice',
            label: 'Voice',
            icon: <IconMic size={15} />,
          },
          {
            href: '/admin/analytics/models',
            label: 'Models',
            icon: <IconCpu size={15} />,
          },
          {
            href: '/admin/analytics/performance',
            label: 'Performance',
            icon: <IconGauge size={15} />,
          },
        ],
      },
      {
        title: 'Infrastructure',
        items: [
          {
            href: '/admin/analytics/nodes',
            label: 'Nodes',
            icon: <IconServer size={15} />,
          },
          {
            href: '/admin/analytics/gpu',
            label: 'GPU',
            icon: <IconCpu size={15} />,
          },
        ],
      },
    );
  }
  // Governance: what leaves the workspace, and the trail of who did what.
  // Both are SUPER_ADMIN capabilities and both 404 server-side without them.
  const security: NavItem[] = [];
  if (can(me, 'shares.manage')) {
    security.push({
      href: '/admin/shares',
      label: 'Shared links',
      icon: <IconLink size={15} />,
    });
  }
  if (can(me, 'audit.read')) {
    security.push({
      href: '/admin/audit',
      label: 'Audit Log',
      icon: <IconShield size={15} />,
    });
  }
  if (security.length) groups.push({ title: 'Security', items: security });
  return groups;
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

  const groups = navGroups(me);
  const items = groups.flatMap((g) => g.items);
  const isActive = (item: NavItem) =>
    item.exact ? pathname === item.href : pathname.startsWith(item.href);
  // The console's charts and its leaderboard rail need width the settings
  // pages do not: 1180px is right for a roster, and cramped for a page with a
  // 336px rail beside a time series.
  const wide = pathname.startsWith('/admin/analytics');
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

          <nav className="mt-1 min-h-0 flex-1 overflow-y-auto px-2 pb-2">
            {groups.map((group, i) => (
              <div key={group.title ?? 'general'} className={i === 0 ? '' : 'mt-4'}>
                {group.title && (
                  <p className="px-2.5 pb-1.5 text-[11px] font-medium uppercase tracking-[0.06em] text-faint">
                    {group.title}
                  </p>
                )}
                <div className="space-y-0.5">
                  {group.items.map((item) => (
                    <Link
                      key={item.href}
                      href={item.href}
                      aria-current={isActive(item) ? 'page' : undefined}
                      className={rowClass(isActive(item))}
                    >
                      <span
                        aria-hidden
                        className="flex w-[18px] shrink-0 justify-center"
                      >
                        {item.icon}
                      </span>
                      {item.label}
                    </Link>
                  ))}
                </div>
              </div>
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
            <div
              className={`mx-auto w-full px-4 py-6 md:px-8 md:py-10 ${
                wide ? 'max-w-[1560px]' : 'max-w-admin'
              }`}
            >
              {children}
            </div>
          </main>
        </div>
      </div>
    </AdminMeProvider>
  );
}
