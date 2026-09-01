'use client';

/**
 * User settings surface (enterprise auth retrofit) — no settings UI existed
 * before this, so this dialog establishes the pattern: the ConfirmDialog
 * portal recipe on a wider panel, with a section nav on the left (top on
 * mobile). Sections: Profile (read-only identity), Personalization (theme),
 * Security (change password), Sessions (everywhere you're signed in), Help.
 *
 * Portalled to <body> — a transformed ancestor would otherwise become the
 * containing block for position:fixed (the bug that hit the ⋯ menu and the
 * diagram viewer). z-[70] matches ConfirmDialog; the session-revoke confirm
 * portals later into <body>, so it still paints above this panel.
 *
 * Escape is handled on the panel, not on document (SearchPalette's pattern):
 * when the nested ConfirmDialog is open its own document-level handler
 * closes it, and this panel — which no longer contains the focus — stays up.
 */

import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { createPortal } from 'react-dom';
import type { FetchLike } from '@/lib/auth';
import type { Account } from './AccountMenu';
import { useTheme } from './Providers';
import { PasswordSection, SessionsSection } from './SecuritySettings';
import { IconX } from './icons';

export type SettingsSection =
  | 'profile'
  | 'personalization'
  | 'security'
  | 'sessions'
  | 'help';

const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: 'profile', label: 'Profile' },
  { id: 'personalization', label: 'Personalization' },
  { id: 'security', label: 'Security' },
  { id: 'sessions', label: 'Sessions' },
  { id: 'help', label: 'Help' },
];

interface SettingsDialogProps {
  open: boolean;
  /** Section shown when the dialog (re)opens; the nav switches after that. */
  initialSection?: SettingsSection;
  account: Account | null;
  onClose: () => void;
  /** Injectable for tests — same idiom as lib/auth.fetchMe. */
  fetchFn?: FetchLike;
}

export function SettingsDialog({
  open,
  initialSection = 'profile',
  account,
  onClose,
  fetchFn,
}: SettingsDialogProps) {
  const [section, setSection] = useState<SettingsSection>(initialSection);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) setSection(initialSection);
  }, [open, initialSection]);

  // Focus lands on Close so Escape/Enter are harmless and the tab order
  // starts at the top of the panel.
  useEffect(() => {
    if (open) closeRef.current?.focus({ preventScroll: true });
  }, [open]);

  if (!open || typeof document === 'undefined') return null;

  function onPanelKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose();
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onPanelKeyDown}
        className="palette-panel flex max-h-[85dvh] min-h-[320px] w-full max-w-2xl flex-col overflow-hidden rounded-ts border border-border bg-surface shadow-2xl"
      >
        <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">Settings</h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close settings"
            className="rounded-md p-1 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconX size={15} />
          </button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col sm:flex-row">
          <nav
            aria-label="Settings sections"
            className="flex shrink-0 gap-1 overflow-x-auto border-b border-border p-2 sm:w-44 sm:flex-col sm:gap-0.5 sm:border-b-0 sm:border-r"
          >
            {SECTIONS.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => setSection(s.id)}
                aria-current={section === s.id ? 'true' : undefined}
                className={`shrink-0 rounded-lg px-2.5 py-2 text-left text-sm transition-colors duration-ts sm:w-full ${
                  section === s.id
                    ? 'bg-surface-2 text-ink'
                    : 'text-muted hover:bg-surface-2/60 hover:text-ink'
                }`}
              >
                {s.label}
              </button>
            ))}
          </nav>

          <div className="min-w-0 flex-1 overflow-y-auto p-4">
            {section === 'profile' && <ProfileSection account={account} />}
            {section === 'personalization' && <PersonalizationSection />}
            {section === 'security' && <PasswordSection fetchFn={fetchFn} />}
            {section === 'sessions' && <SessionsSection fetchFn={fetchFn} />}
            {section === 'help' && <HelpSection />}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/* ---------------------------------------------------------------- profile */

function roleLabel(role: string | undefined): string {
  if (!role) return '—';
  const words = role.replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs font-medium text-muted">{label}</p>
      <p className="mt-0.5 truncate text-sm text-ink">{value}</p>
    </div>
  );
}

function ProfileSection({ account }: { account: Account | null }) {
  const name = account?.user?.name ?? account?.username ?? '—';
  const initial = name.trim().charAt(0).toUpperCase() || '?';
  return (
    <section aria-label="Profile">
      <h3 className="text-sm font-semibold text-ink">Profile</h3>
      <div className="mt-4 flex items-center gap-3">
        <span
          aria-hidden
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent/15 text-sm font-semibold text-accent"
        >
          {initial}
        </span>
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-ink">{name}</p>
          {account?.user?.email && (
            <p className="truncate text-xs text-muted">{account.user.email}</p>
          )}
        </div>
      </div>
      <div className="mt-5 space-y-3">
        <Field label="Name" value={name} />
        <Field label="Email" value={account?.user?.email ?? '—'} />
        <Field label="Workspace" value={account?.workspace?.name ?? '—'} />
        <Field label="Role" value={roleLabel(account?.workspace?.role)} />
      </div>
      <p className="mt-5 text-xs text-faint">
        Name and email are managed by your workspace admin.
      </p>
    </section>
  );
}

/* -------------------------------------------------------- personalization */

function PersonalizationSection() {
  const { theme, toggleTheme } = useTheme();
  function pick(next: 'dark' | 'light') {
    if (theme !== next) toggleTheme();
  }
  return (
    <section aria-label="Personalization">
      <h3 className="text-sm font-semibold text-ink">Personalization</h3>
      <p className="mt-4 text-xs font-medium text-muted">Theme</p>
      <div className="mt-1.5 flex gap-2" role="radiogroup" aria-label="Theme">
        {(['dark', 'light'] as const).map((t) => (
          <button
            key={t}
            type="button"
            role="radio"
            aria-checked={theme === t}
            onClick={() => pick(t)}
            className={`rounded-lg border px-3 py-1.5 text-sm transition-colors duration-ts ${
              theme === t
                ? 'border-accent/60 bg-surface-2 text-ink'
                : 'border-border text-muted hover:bg-surface-2 hover:text-ink'
            }`}
          >
            {t === 'dark' ? 'Dark' : 'Light'}
          </button>
        ))}
      </div>
      <p className="mt-2 text-xs text-faint">Applies to this browser.</p>
    </section>
  );
}

/* ------------------------------------------------------------------- help */

const SHORTCUTS: [string, string][] = [
  ['Search chats', 'Ctrl K'],
  ['New chat', 'Ctrl ⇧ O'],
  ['Stop generating', 'Esc'],
];

function HelpSection() {
  return (
    <section aria-label="Help">
      <h3 className="text-sm font-semibold text-ink">Help</h3>
      <p className="mt-4 text-xs font-medium text-muted">Keyboard shortcuts</p>
      <ul className="mt-1.5 space-y-1.5">
        {SHORTCUTS.map(([label, keys]) => (
          <li
            key={label}
            className="flex items-center justify-between gap-3 text-sm text-ink"
          >
            {label}
            <kbd className="rounded border border-border px-1.5 py-px font-mono text-[10px] text-faint">
              {keys}
            </kbd>
          </li>
        ))}
      </ul>
      <p className="mt-5 text-xs text-faint">
        TechSara runs on your organization&apos;s own hardware — conversations
        never leave it. For account or access questions, contact your
        workspace admin.
      </p>
    </section>
  );
}
