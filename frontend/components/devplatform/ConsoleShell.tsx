'use client';

/**
 * The developer console's shell: rail, header, and the section on show.
 *
 * It is the admin shell's twin on purpose — same 240px rail, same 36px nav
 * rows, same drawer below lg, same content column — so that moving between
 * /admin and /api feels like moving between two rooms of one building rather
 * than two products. CONTRACT §17 asks for exactly that:
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
 * discussed, and Back goes where Back should go. So the URL must also say
 * what is ON SCREEN: a `?tab=` this account cannot open (or that does not
 * exist) is replaced with the Overview it renders, and the document title
 * names the section, so browser history, tabs and a screen reader's page
 * announcement can tell Keys from Logs (audit, 2026-09-13).
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { TechSaraMark } from '@/components/TechSaraMark';
import {
  IconBook,
  IconFileText,
  IconMenu,
  IconPackage,
  IconPlay,
  IconX,
} from '@/components/icons';
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

/** The product name every console title ends with. */
export const CONSOLE_TITLE = 'Developer platform · TechSara';

/** The document title for a section: its label, then the console's name. */
export function consoleTitle(label: string | undefined): string {
  return label ? `${label} · ${CONSOLE_TITLE}` : CONSOLE_TITLE;
}

/**
 * Keep the document title on the section on show.
 *
 * NOT a plain `document.title = …` in an effect (measured in Chrome,
 * 2026-09-13). The layout's metadata owns a `<title>`, and on every soft
 * navigation between tabs Next REMOVES that element and inserts a fresh one
 * saying "Developer platform · TechSara" — after this component's effect has
 * run. The assignment either landed on the element about to be removed, or,
 * in the gap between the two, created a second `<title>` that the new first
 * one then outranked: Keys → Request logs read "Developer platform ·
 * TechSara" in the tab strip.
 *
 * So the title is re-applied whenever the head's titles change, by rewriting
 * the TEXT of the first `<title>` in place (React keeps a reference to that
 * text node; replacing the node would detach it). A `<title>` of this hook's
 * own is created only when the document has none at all — never in a
 * Next-rendered page — and removed as soon as another appears.
 */
function useConsoleTitle(label: string | undefined) {
  useEffect(() => {
    const wanted = consoleTitle(label);
    const OWN = 'data-console-title';
    const apply = () => {
      const titles = Array.from(document.querySelectorAll('title'));
      const theirs = titles.find((el) => !el.hasAttribute(OWN));
      if (theirs) {
        for (const el of titles) if (el.hasAttribute(OWN)) el.remove();
        const text = theirs.firstChild;
        if (text && text.nodeType === Node.TEXT_NODE) {
          if (text.nodeValue !== wanted) text.nodeValue = wanted;
        } else if (theirs.textContent !== wanted) {
          theirs.textContent = wanted;
        }
        return;
      }
      // Only this hook's own title (a document with no other): update it.
      if (titles[0] && titles[0].textContent !== wanted) titles[0].textContent = wanted;
    };
    apply();
    if (document.querySelectorAll('title').length === 0) {
      const own = document.createElement('title');
      own.setAttribute(OWN, '');
      own.textContent = wanted;
      document.head.appendChild(own);
    }
    const observer = new MutationObserver(apply);
    observer.observe(document.head, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  }, [label]);
}

export function ConsoleShell({ me }: { me: Me }) {
  const params = useSearchParams();
  const router = useRouter();
  const raw = params.get('tab');
  const requested = tabFromQuery(raw);
  // A URL kept from before a demotion must not render a section this account
  // may no longer use. Overview is the honest landing place.
  const tab = tabAllowed(me, requested) ? requested : 'overview';
  const groups = consoleNav(me);
  const flat = groups.flatMap((g) => g.items);
  const current = flat.find((item) => !item.external && item.id === tab);

  // Below lg the rail is a drawer, opened from the slim header.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const toggleRef = useRef<HTMLButtonElement | null>(null);
  const railRef = useRef<HTMLElement | null>(null);

  // The URL says what is on screen. `?tab=limits` for an admin, or a
  // `?tab=` that names nothing, used to render Overview under the old query
  // — so a link copied from the address bar still named a section the page
  // was not showing. Replaced, not pushed: Back must not return to it.
  const search = params.toString();
  useEffect(() => {
    if (raw === null || raw === tab) return;
    const next = new URLSearchParams(search);
    next.delete('tab');
    const rest = next.toString();
    router.replace(`/api${rest ? `?${rest}` : ''}`, { scroll: false });
  }, [raw, tab, search, router]);

  useConsoleTitle(current?.label);

  // A section change closes the drawer — whichever link caused it.
  useEffect(() => {
    setDrawerOpen(false);
  }, [tab]);

  // Escape closes the drawer and hands focus back to the toggle; opening it
  // moves focus to the section on show, scrolled into view, so the drawer
  // opens on "where am I" rather than on the top of a list.
  useEffect(() => {
    if (!drawerOpen) return undefined;
    const rail = railRef.current;
    const target =
      rail?.querySelector<HTMLElement>('a[aria-current="page"]') ??
      rail?.querySelector<HTMLElement>('a[href]');
    target?.focus({ preventScroll: true });
    target?.scrollIntoView?.({ block: 'nearest' });
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setDrawerOpen(false);
        toggleRef.current?.focus();
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [drawerOpen]);

  const rowClass = (active: boolean) =>
    `flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-sm transition-colors duration-ts focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-sidebar ${
      active
        ? 'bg-surface-2 font-medium text-ink'
        : 'text-icon hover:bg-surface-2 hover:text-ink'
    }`;

  return (
    <ConsoleStatusProvider>
      <div className="flex h-dvh overflow-hidden bg-bg text-ink">
        {/* The scrim exists only while the drawer is open, and only below lg.
            It starts under the 52px header so the toggle stays tappable. */}
        {drawerOpen && (
          <div
            aria-hidden="true"
            data-testid="console-drawer-scrim"
            onClick={() => setDrawerOpen(false)}
            className="fixed inset-x-0 bottom-0 top-[52px] z-40 bg-black/60 lg:hidden"
          />
        )}
        {/* ONE rail: the fixed 240px column from lg up, a drawer below it —
            the admin area's pattern. Below md it used to be a header strip of
            the same links that scrolled sideways with no hint: at 360px eight
            of twelve links were off-screen, a deep link's section was never
            scrolled into view, and every link was 22px tall (audit,
            2026-09-13). The column started at md, which left a 768px tablet's
            tables 464px. */}
        <aside
          id="console-rail"
          ref={railRef}
          aria-label="Developer console navigation"
          className={`${
            drawerOpen
              ? 'fixed bottom-0 left-0 top-[52px] z-50 flex w-72 max-w-[85vw] shadow-2xl'
              : 'hidden'
          } shrink-0 flex-col border-r border-border bg-sidebar lg:static lg:z-auto lg:flex lg:w-60 lg:max-w-none lg:shadow-none`}
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
                        // The section already on show does not change `tab`,
                        // so its own link closes the drawer by hand.
                        onClick={() => setDrawerOpen(false)}
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
          {/* Below lg: a slim header that names the section on show and opens
              the rail as a drawer. */}
          <header className="flex h-[52px] shrink-0 items-center gap-2 border-b border-border px-2 lg:hidden">
            <button
              ref={toggleRef}
              type="button"
              onClick={() => setDrawerOpen((open) => !open)}
              aria-expanded={drawerOpen}
              aria-controls="console-rail"
              aria-label={drawerOpen ? 'Close console menu' : 'Open console menu'}
              className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {drawerOpen ? <IconX size={18} /> : <IconMenu size={18} />}
            </button>
            <TechSaraMark size={24} />
            <span className="rounded border border-border px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-faint">
              Developer
            </span>
            {current && (
              <span
                data-testid="console-current-section"
                className="min-w-0 truncate text-sm font-medium text-ink"
              >
                {current.label}
              </span>
            )}
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
