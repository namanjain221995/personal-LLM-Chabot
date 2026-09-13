'use client';

/**
 * The developer console's shell: rail, header, and the section on show.
 *
 * It is the admin shell's twin on purpose — same 240px rail, same 36px nav
 * rows, same mobile header that scrolls sideways, same content column — so
 * that moving between /admin and /api feels like moving between two rooms of
 * one building rather than two products. CONTRACT §17 asks for exactly that:
 * the console "reuses the admin design system so it looks like the product
 * rather than a bolted-on page".
 *
 * WHAT IT DOES NOT DO: gate. By the time this renders, the server has already
 * resolved the session and refused anyone without `api.console.access`
 * (app/api/layout.tsx). `me` arrives as a prop from that resolution rather
 * than being fetched again here — a second client-side probe would be a second
 * answer to a question already answered, and the moment two gates disagree the
 * weaker one is the one that ships.
 *
 * The section lives in `?tab=`, not in component state, for the reason the
 * analytics filters do: a colleague can be sent the exact view being
 * discussed, and Back goes where Back should go.
 */

import type { ReactNode } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { TechSaraMark } from '@/components/TechSaraMark';
import { IconBook, IconFileText, IconPackage, IconPlay } from '@/components/icons';
import {
  IconArrowLeft,
  IconChart,
  IconCpu,
  IconGauge,
  IconGrid,
  IconKey,
  IconLink,
  IconShield,
  IconSliders,
} from '@/components/admin/icons';
import { ROLE_LABEL, type Me } from '@/components/admin/api';
import { ConsoleStatusProvider } from './status';
import { consoleNav, tabAllowed, tabFromQuery, type TabId } from './nav';
import { OverviewPanel } from './Overview';
import { ProjectsPanel } from './Projects';
import { KeysPanel } from './Keys';
import { ModelsPanel } from './Models';
import { PlaygroundPanel } from './Playground';
import { UsagePanel } from './Usage';
import { RequestLogsPanel } from './RequestLogs';
import { WebhooksPanel } from './Webhooks';
import { LimitsPanel } from './Limits';
import { SettingsPanel } from './Settings';

/** One glyph per section. Kept beside the shell because nav.ts stays pure. */
const ICONS: Record<string, ReactNode> = {
  overview: <IconGrid size={15} />,
  projects: <IconPackage size={15} />,
  keys: <IconKey size={15} />,
  models: <IconCpu size={15} />,
  playground: <IconPlay size={15} />,
  usage: <IconChart size={15} />,
  logs: <IconFileText size={15} />,
  webhooks: <IconLink size={15} />,
  limits: <IconGauge size={15} />,
  docs: <IconBook size={15} />,
  settings: <IconSliders size={15} />,
};

function Panel({ tab, me }: { tab: TabId; me: Me }) {
  switch (tab) {
    case 'projects':
      return <ProjectsPanel me={me} />;
    case 'keys':
      return <KeysPanel me={me} />;
    case 'models':
      return <ModelsPanel me={me} />;
    case 'playground':
      return <PlaygroundPanel />;
    case 'usage':
      return <UsagePanel />;
    case 'logs':
      return <RequestLogsPanel />;
    case 'webhooks':
      return <WebhooksPanel />;
    case 'limits':
      return <LimitsPanel />;
    case 'settings':
      return <SettingsPanel me={me} />;
    default:
      return <OverviewPanel me={me} />;
  }
}

export function ConsoleShell({ me }: { me: Me }) {
  const params = useSearchParams();
  const requested = tabFromQuery(params.get('tab'));
  // A URL kept from before a demotion must not render a section this account
  // may no longer use. Overview is the honest landing place.
  const tab = tabAllowed(me, requested) ? requested : 'overview';
  const groups = consoleNav(me);
  const flat = groups.flatMap((g) => g.items);

  const rowClass = (active: boolean) =>
    `flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-sm transition-colors duration-ts focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-sidebar ${
      active
        ? 'bg-surface-2 font-medium text-ink'
        : 'text-icon hover:bg-surface-2 hover:text-ink'
    }`;

  return (
    <ConsoleStatusProvider>
      <div className="flex h-dvh overflow-hidden bg-bg text-ink">
        {/* Desktop: the same fixed 240px rail the admin area uses. */}
        <aside
          aria-label="Developer console navigation"
          className="hidden w-60 shrink-0 flex-col border-r border-border bg-sidebar md:flex"
        >
          <div className="flex items-center gap-2 px-3 pb-1 pt-3">
            <TechSaraMark size={28} />
            <span className="truncate text-sm font-semibold">TechSara</span>
            <span className="rounded border border-border px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-faint">
              Developer
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
                  {group.items.map((item) => {
                    const active = !item.external && item.id === tab;
                    return (
                      <Link
                        key={item.id}
                        href={item.href}
                        aria-current={active ? 'page' : undefined}
                        className={rowClass(active)}
                      >
                        <span
                          aria-hidden
                          className="flex w-[18px] shrink-0 justify-center"
                        >
                          {ICONS[item.id]}
                        </span>
                        {item.label}
                      </Link>
                    );
                  })}
                </div>
              </div>
            ))}
          </nav>

          <div className="border-t border-border p-2">
            {/* Both ways back, because the console is reached from both. */}
            <Link href="/admin" className={rowClass(false)}>
              <span aria-hidden className="flex w-[18px] shrink-0 justify-center">
                <IconShield size={15} />
              </span>
              Admin
            </Link>
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
          {/* Phone: the same links in a single scrolling row. */}
          <header className="flex h-[52px] shrink-0 items-center gap-3 overflow-x-auto border-b border-border px-3 md:hidden">
            <TechSaraMark size={24} />
            {flat.map((item) => (
              <Link
                key={item.id}
                href={item.href}
                aria-current={!item.external && item.id === tab ? 'page' : undefined}
                className={`shrink-0 text-sm transition-colors duration-ts ${
                  !item.external && item.id === tab
                    ? 'font-medium text-ink'
                    : 'text-muted hover:text-ink'
                }`}
              >
                {item.label}
              </Link>
            ))}
            <Link
              href="/admin"
              className="ml-auto shrink-0 text-sm text-muted hover:text-ink"
            >
              Admin
            </Link>
            <Link href="/" className="shrink-0 text-sm text-muted hover:text-ink">
              Chat
            </Link>
          </header>

          <main className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto w-full max-w-[1180px] px-4 py-6 md:px-8 md:py-10">
              <Panel tab={tab} me={me} />
            </div>
          </main>
        </div>
      </div>
    </ConsoleStatusProvider>
  );
}
