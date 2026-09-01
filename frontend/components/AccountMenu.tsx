'use client';

/**
 * Sidebar footer account row + menu (enterprise auth retrofit).
 *
 * The row lives in the sidebar footer — the design map's "natural slot for
 * the first account affordance" — and shows who is signed in: avatar initial,
 * name, email, chevron. Clicking it opens a portalled popover (fixed-position
 * like ConversationMenu: a transformed ancestor would otherwise become the
 * containing block for position:fixed) with the workspace header, the
 * signed-in identity, and the account actions. "Workspace settings" appears
 * ONLY when the server-granted capabilities include "members.read" —
 * visibility is driven by ME_PAYLOAD.capabilities, never by comparing role
 * strings here (contract §Roles).
 *
 * Identity comes from GET /api/auth/me, fetched HERE and cached module-level:
 * ChatApp is owned by another workstream this round, so no new props are
 * threaded through it. The cache also de-dupes this row's fetch with the
 * settings dialog's profile section. A failed probe is NOT cached (401 and
 * offline alike) — the row degrades to a plain "Account" label and the menu
 * keeps working, because ChatApp owns the redirect-to-login decision.
 */

import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import type { FetchLike } from '@/lib/auth';
import { menuKeyAction } from '@/lib/conversationMenu';
import { SettingsDialog, type SettingsSection } from './SettingsDialog';
import { IconChevronDown, IconLogout } from './icons';

/* ------------------------------------------------------- account identity */

export interface AccountUser {
  id: number;
  name: string;
  email: string;
}

export interface AccountWorkspace {
  id: string;
  name: string;
  role: string;
}

/** Parsed ME_PAYLOAD — `username` is the legacy key, kept as the fallback. */
export interface Account {
  username: string;
  user: AccountUser | null;
  workspace: AccountWorkspace | null;
  capabilities: string[];
}

function parseAccount(body: unknown): Account | null {
  if (typeof body !== 'object' || body === null) return null;
  const raw = body as Record<string, unknown>;
  const user = raw.user as Record<string, unknown> | undefined;
  const workspace = raw.workspace as Record<string, unknown> | undefined;
  const username =
    typeof raw.username === 'string'
      ? raw.username
      : typeof user?.name === 'string'
        ? user.name
        : null;
  if (username === null) return null;
  return {
    username,
    user:
      user &&
      typeof user.id === 'number' &&
      typeof user.name === 'string' &&
      typeof user.email === 'string'
        ? { id: user.id, name: user.name, email: user.email }
        : null,
    workspace:
      workspace &&
      typeof workspace.id === 'string' &&
      typeof workspace.name === 'string' &&
      typeof workspace.role === 'string'
        ? { id: workspace.id, name: workspace.name, role: workspace.role }
        : null,
    capabilities: Array.isArray(raw.capabilities)
      ? raw.capabilities.filter((c): c is string => typeof c === 'string')
      : [],
  };
}

let cachedAccount: Account | null = null;
let inflight: Promise<Account | null> | null = null;

/** Forget the cached identity — on logout, or between tests. */
export function clearAccountCache(): void {
  cachedAccount = null;
  inflight = null;
}

/**
 * GET /api/auth/me, cached module-level for the life of the page. Concurrent
 * callers share one request; a failure resolves null and is retried on the
 * next call rather than cached.
 */
export async function fetchAccount(
  fetchFn: FetchLike = fetch,
): Promise<Account | null> {
  if (cachedAccount) return cachedAccount;
  if (!inflight) {
    inflight = (async () => {
      try {
        const res = await fetchFn('/api/auth/me', { cache: 'no-store' });
        if (!res.ok) return null;
        const parsed = parseAccount(await res.json());
        if (parsed) cachedAccount = parsed;
        return parsed;
      } catch {
        return null;
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}

/* ---------------------------------------------------------------- logout */

/**
 * POST /api/auth/logout, wipe what we can locally, then hard-navigate to
 * /login. The navigation happens even when the POST fails: offline, the
 * cookie cannot be revoked anyway, and staying "signed in" would be a lie.
 */
export async function performLogout(
  fetchFn: FetchLike = fetch,
  navigate: (url: string) => void = (url) => window.location.assign(url),
): Promise<void> {
  try {
    await fetchFn('/api/auth/logout', { method: 'POST' });
  } catch {
    // Offline — the server session survives, but this browser leaves anyway.
  }
  // The local wipe lives in lib/history.clearActiveUserData (per-user
  // IndexedDB + localStorage scoping) — awaited, so the next person at this
  // browser cannot open the previous account's cached conversations.
  try {
    const { clearActiveUserData } = await import('@/lib/history');
    await clearActiveUserData();
  } catch {
    // Wipe failed — logout proceeds regardless; the server session is dead.
  }
  clearAccountCache();
  navigate('/login');
}

/* ------------------------------------------------------------ local icons */

/*
 * Glyphs the shared set (icons.tsx, owned elsewhere this round) does not
 * have yet — same recipe: 24px viewBox, stroke currentColor, width 2, round
 * caps/joins, aria-hidden.
 */

function glyphBase(size: number) {
  return {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  };
}

const IconBriefcase = ({ size = 14 }: { size?: number }) => (
  <svg {...glyphBase(size)}>
    <rect x="2" y="7" width="20" height="14" rx="2" />
    <path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
  </svg>
);

const IconSliders = ({ size = 14 }: { size?: number }) => (
  <svg {...glyphBase(size)}>
    <path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6" />
  </svg>
);

const IconGear = ({ size = 14 }: { size?: number }) => (
  <svg {...glyphBase(size)}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
);

const IconHelpCircle = ({ size = 14 }: { size?: number }) => (
  <svg {...glyphBase(size)}>
    <circle cx="12" cy="12" r="10" />
    <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3M12 17h.01" />
  </svg>
);

/* ------------------------------------------------------------- component */

const MENU_WIDTH = 264;

/** useLayoutEffect warns during SSR; the measurement is client-only anyway. */
const useMeasureEffect =
  typeof window === 'undefined' ? useEffect : useLayoutEffect;

interface MenuItem {
  id: string;
  label: string;
  icon: ReactNode;
  /** Link items navigate (Workspace settings → /admin). */
  href?: string;
  /** Button items run. */
  run?: () => void;
}

interface AccountMenuProps {
  /** Injectable for tests — same idiom as lib/auth.fetchMe. */
  fetchFn?: FetchLike;
  /** Injectable for tests; defaults to window.location.assign. */
  navigate?: (url: string) => void;
}

export function AccountMenu({ fetchFn = fetch, navigate }: AccountMenuProps) {
  const [account, setAccount] = useState<Account | null>(() => cachedAccount);
  const [open, setOpen] = useState(false);
  const [focusIndex, setFocusIndex] = useState(0);
  const [position, setPosition] = useState<{
    bottom: number;
    left: number;
  } | null>(null);
  const [settingsSection, setSettingsSection] =
    useState<SettingsSection | null>(null);

  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | HTMLAnchorElement | null)[]>([]);

  useEffect(() => {
    let alive = true;
    void fetchAccount(fetchFn).then((a) => {
      if (alive && a) setAccount(a);
    });
    return () => {
      alive = false;
    };
  }, [fetchFn]);

  const displayName = account?.user?.name ?? account?.username ?? 'Account';
  const email = account?.user?.email ?? null;
  const workspaceName = account?.workspace?.name ?? 'TechSara';
  const initial = displayName.trim().charAt(0).toUpperCase() || '?';
  const canAdmin = account?.capabilities.includes('members.read') ?? false;

  function close(restoreFocus: boolean) {
    setOpen(false);
    setPosition(null);
    if (restoreFocus) triggerRef.current?.focus();
  }

  function openMenu() {
    setFocusIndex(0);
    setPosition(null);
    setOpen(true);
  }

  function openSettings(section: SettingsSection) {
    close(false);
    setSettingsSection(section);
  }

  const items: MenuItem[] = [
    ...(canAdmin
      ? [
          {
            id: 'workspace-settings',
            label: 'Workspace settings',
            icon: <IconBriefcase />,
            href: '/admin',
          },
        ]
      : []),
    {
      id: 'personalization',
      label: 'Personalization',
      icon: <IconSliders />,
      run: () => openSettings('personalization'),
    },
    {
      id: 'settings',
      label: 'Settings',
      icon: <IconGear />,
      run: () => openSettings('profile'),
    },
    {
      id: 'help',
      label: 'Help',
      icon: <IconHelpCircle />,
      run: () => openSettings('help'),
    },
    {
      id: 'logout',
      label: 'Log out',
      icon: <IconLogout size={14} />,
      run: () => {
        close(false);
        void performLogout(fetchFn, navigate);
      },
    },
  ];

  // Anchor above the trigger (the row sits at the very bottom of the
  // sidebar), measured after mount so the first paint is already right.
  useMeasureEffect(() => {
    if (!open) return;
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPosition({
      bottom: Math.max(8, window.innerHeight - rect.top + 6),
      left: Math.min(
        Math.max(8, rect.left),
        Math.max(8, window.innerWidth - MENU_WIDTH - 8),
      ),
    });
  }, [open]);

  // Outside click / Escape / resize closes — ModelPicker's contract.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      const target = e.target as Node;
      if (
        menuRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      ) {
        return;
      }
      close(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close(true);
      }
    }
    function onResize() {
      close(false);
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('resize', onResize);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('resize', onResize);
    };
    // close() is stable in behaviour; re-binding on open is enough.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Roving focus: the focused item is the only tab stop.
  useEffect(() => {
    if (!open || !position) return;
    itemRefs.current[focusIndex]?.focus({ preventScroll: true });
  }, [open, position, focusIndex]);

  function onMenuKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    const action = menuKeyAction(e.key, focusIndex, items.length);
    if (!action) return;
    if (action.kind === 'close') {
      if (e.key !== 'Tab') e.preventDefault();
      close(true);
      return;
    }
    if (action.kind === 'move') {
      e.preventDefault();
      setFocusIndex(action.index);
      return;
    }
    // Enter already clicks native buttons/links; Space only clicks buttons,
    // so route it through click() to cover the Workspace settings link too.
    if (e.key === ' ') {
      e.preventDefault();
      itemRefs.current[focusIndex]?.click();
    }
  }

  const itemClass =
    'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-sm text-ink transition-colors duration-ts hover:bg-surface-2 focus:bg-surface-2 focus:outline-none';

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => (open ? close(true) : openMenu())}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Account"
        className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left transition-colors duration-ts hover:bg-surface-2"
      >
        <span
          aria-hidden
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent/15 text-xs font-semibold text-accent"
        >
          {initial}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-ink">{displayName}</span>
          {email && (
            <span className="block truncate text-xs text-faint">{email}</span>
          )}
        </span>
        <IconChevronDown
          size={14}
          className={`shrink-0 text-faint transition-transform duration-ts ${
            open ? 'rotate-180' : ''
          }`}
        />
      </button>

      {/* Portalled to <body>: same containing-block reasoning as the "⋯"
          menu — the sidebar wraps this in transformed/overflow ancestors. */}
      {open &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label="Account"
            onKeyDown={onMenuKeyDown}
            style={{
              position: 'fixed',
              width: MENU_WIDTH,
              bottom: position?.bottom ?? 0,
              left: position?.left ?? 0,
              visibility: position ? 'visible' : 'hidden',
            }}
            className="menu-pop z-50 rounded-ts border border-border bg-surface p-1 shadow-xl"
          >
            <div className="px-2.5 pb-1.5 pt-2">
              <p className="truncate text-sm font-semibold text-ink">
                {workspaceName}
              </p>
              <p className="text-[11px] font-medium uppercase tracking-wide text-faint">
                Enterprise
              </p>
            </div>

            <div role="separator" className="mx-1 my-1 border-t border-border" />

            <div className="flex items-center gap-2 px-2.5 py-1.5">
              <span
                aria-hidden
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent/15 text-xs font-semibold text-accent"
              >
                {initial}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-sm text-ink">
                  {displayName}
                </span>
                {email && (
                  <span className="block truncate text-xs text-faint">
                    {email}
                  </span>
                )}
              </span>
            </div>

            <div role="separator" className="mx-1 my-1 border-t border-border" />

            {items.map((item, index) => {
              const shared = {
                role: 'menuitem' as const,
                tabIndex: index === focusIndex ? 0 : -1,
                onMouseEnter: () => setFocusIndex(index),
                className: itemClass,
              };
              const inner = (
                <>
                  <span className="text-muted" aria-hidden>
                    {item.icon}
                  </span>
                  {item.label}
                </>
              );
              return (
                <span key={item.id} className="contents">
                  {item.id === 'help' && (
                    <div
                      role="separator"
                      className="mx-1 my-1 border-t border-border"
                    />
                  )}
                  {item.href ? (
                    <a
                      ref={(el) => {
                        itemRefs.current[index] = el;
                      }}
                      href={item.href}
                      {...shared}
                    >
                      {inner}
                    </a>
                  ) : (
                    <button
                      ref={(el) => {
                        itemRefs.current[index] = el;
                      }}
                      type="button"
                      onClick={item.run}
                      {...shared}
                    >
                      {inner}
                    </button>
                  )}
                </span>
              );
            })}
          </div>,
          document.body,
        )}

      <SettingsDialog
        open={settingsSection !== null}
        initialSection={settingsSection ?? 'profile'}
        account={account}
        fetchFn={fetchFn}
        onClose={() => {
          setSettingsSection(null);
          triggerRef.current?.focus();
        }}
      />
    </>
  );
}
