'use client';

/**
 * User settings surface (enterprise auth retrofit) — no settings UI existed
 * before this, so this dialog establishes the pattern: the ConfirmDialog
 * portal recipe on a wider panel, with a section nav on the left (top on
 * mobile). Sections: Profile (identity — the display name is editable here
 * since 2026-09-21, everything else is the admin's), Personalization (theme),
 * Memory (what the assistant saved about you, B11), Security (change
 * password), Sessions (everywhere you're signed in), Help.
 *
 * Portalled to <body> — a transformed ancestor would otherwise become the
 * containing block for position:fixed (the bug that hit the ⋯ menu and the
 * diagram viewer). z-[70] matches ConfirmDialog; the session-revoke confirm
 * portals later into <body>, so it still paints above this panel.
 *
 * Escape is handled on the panel, not on document (SearchPalette's pattern).
 * A nested ConfirmDialog portals outside the panel, but React still bubbles
 * its keys through this component, so the panel acts only on keys whose
 * target is inside its own DOM (ownModalKey); the confirm's own
 * document-level handler closes it and this panel stays up. The panel takes
 * tabIndex -1 and useModalKeyGuard runs while it is open, so a click on
 * plain text followed by Escape closes Settings instead of reaching ChatApp's
 * window shortcut and stopping the answer streaming behind it (QA,
 * 2026-09-18).
 */

import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { createPortal } from 'react-dom';
import type { FetchLike } from '@/lib/auth';
import { withDisplayName, type Account } from './AccountMenu';
import { Loader } from './Loader';
import { MemoryPanel, ownModalKey, trapTab, useModalKeyGuard } from './MemoryPanel';
import { useTheme, useToast } from './Providers';
import { PasswordSection, SessionsSection } from './SecuritySettings';
import { IconAlert, IconX } from './icons';

export type SettingsSection =
  | 'profile'
  | 'personalization'
  | 'memory'
  | 'security'
  | 'sessions'
  | 'help';

const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: 'profile', label: 'Profile' },
  { id: 'personalization', label: 'Personalization' },
  { id: 'memory', label: 'Memory' },
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
  /**
   * The identity changed in here (Profile → Your name). The owner of the
   * Account object re-renders with it, so the sidebar row behind the dialog
   * shows the new name immediately.
   */
  onAccountChange?: (account: Account | null) => void;
}

export function SettingsDialog({
  open,
  initialSection = 'profile',
  account,
  onClose,
  fetchFn,
  onAccountChange,
}: SettingsDialogProps) {
  const [section, setSection] = useState<SettingsSection>(initialSection);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) setSection(initialSection);
  }, [open, initialSection]);

  // Focus lands on Close so Escape/Enter are harmless and the tab order
  // starts at the top of the panel.
  useEffect(() => {
    if (open) closeRef.current?.focus({ preventScroll: true });
  }, [open]);

  useModalKeyGuard(open, panelRef, onClose);

  if (!open || typeof document === 'undefined') return null;

  function onPanelKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (!ownModalKey(e, panelRef.current)) return;
    if (e.key === 'Escape') {
      onClose();
      return;
    }
    trapTab(e, panelRef.current);
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onPanelKeyDown}
        className="palette-panel flex max-h-[85dvh] min-h-[320px] w-full max-w-2xl flex-col overflow-hidden rounded-ts border border-border bg-surface shadow-2xl focus:outline-none"
      >
        <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">Settings</h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close settings"
            className="inline-flex items-center justify-center rounded-md p-1 text-faint max-sm:h-8 max-sm:w-8 max-sm:p-0 [@media(pointer:coarse)]:h-8 [@media(pointer:coarse)]:w-8 [@media(pointer:coarse)]:p-0 transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconX size={15} />
          </button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col sm:flex-row">
          <nav
            aria-label="Settings sections"
            // Wraps on a phone (fe audit 2026-09-13): a sideways-scrolling
            // row hid "Help" (and half of "Sessions" at 360 px) with nothing
            // to say more tabs existed.
            className="flex shrink-0 flex-wrap gap-1 border-b border-border p-2 sm:w-44 sm:flex-col sm:flex-nowrap sm:gap-0.5 sm:border-b-0 sm:border-r"
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
            {section === 'profile' && (
              <ProfileSection
                account={account}
                fetchFn={fetchFn}
                onAccountChange={onAccountChange}
              />
            )}
            {section === 'personalization' && <PersonalizationSection />}
            {section === 'memory' && <MemoryPanel fetchFn={fetchFn} />}
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

/**
 * Mirrors orchestrator `authn/display_name.MAX_LENGTH` (422 above it), the
 * same courtesy `MIN_PASSWORD_LENGTH` pays in SecuritySettings. The server
 * stays the authority: it also refuses control characters, prompt punctuation
 * and instruction-shaped prose, and its sentence is what gets shown.
 */
const MAX_NAME_LENGTH = 64;

/**
 * What the server will count. It trims, then collapses runs of horizontal
 * whitespace to one space — so counting the raw string here would refuse a
 * name the server would have accepted, which is the one way a client-side
 * length check can be worse than no check at all.
 */
function normalizeName(value: string): string {
  return value.trim().replace(/[\t  ]+/g, ' ');
}

function ProfileSection({
  account,
  fetchFn = fetch,
  onAccountChange,
}: {
  account: Account | null;
  fetchFn?: FetchLike;
  onAccountChange?: (account: Account | null) => void;
}) {
  const { toast } = useToast();
  const fieldId = useId();
  const errorId = `${fieldId}-error`;
  const hintId = `${fieldId}-hint`;

  const name = account?.user?.name ?? account?.username ?? '—';
  const initial = name.trim().charAt(0).toUpperCase() || '?';
  const editable = Boolean(account?.user);

  // Seeded once per opening: the dialog unmounts when it closes, so there is
  // no stale draft to reconcile and — the part that matters — a refused save
  // leaves the text the person typed in place for them to fix.
  const [draft, setDraft] = useState(name === '—' ? '' : name);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const candidate = normalizeName(draft);
  const dirty = candidate !== name && candidate.length > 0;

  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy || !dirty || !account) return;
    if (candidate.length > MAX_NAME_LENGTH) {
      setError(`A name can be at most ${MAX_NAME_LENGTH} characters.`);
      return;
    }
    setError(null);
    setBusy(true);

    // Optimistic: the row behind the dialog, the avatar and the header all
    // change now. `rollBack` is what we do if the server disagrees — not a
    // re-fetch, which would race with whatever else is on the page. It goes
    // back through withDisplayName rather than handing the old object over,
    // because the optimistic write also updated the module-level identity
    // cache and only another write can undo that one.
    const previous = account;
    const rollBack = () => onAccountChange?.(withDisplayName(previous, name));
    onAccountChange?.(withDisplayName(account, candidate));
    try {
      const res = await fetchFn('/api/auth/profile', {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ display_name: draft }),
      });
      if (res.ok) {
        // The server cleans (NFC, whitespace), so the name it returns is the
        // one to show — not the one we guessed.
        const body = (await res.json()) as { display_name?: unknown };
        const saved =
          typeof body.display_name === 'string' && body.display_name
            ? body.display_name
            : candidate;
        onAccountChange?.(withDisplayName(previous, saved));
        setDraft(saved);
        toast('Name updated');
        return;
      }
      rollBack();
      if (res.status === 401) {
        setError('Your session ended. Sign in again to change your name.');
        return;
      }
      setError((await readProfileDetail(res)) ?? 'Could not save the name.');
    } catch {
      rollBack();
      setError('Network error — the name was not saved.');
    } finally {
      setBusy(false);
    }
  }

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

      {editable ? (
        <form onSubmit={save} className="mt-5 max-w-sm">
          <label htmlFor={fieldId} className="mb-1 block text-xs font-medium text-muted">
            Your name
          </label>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start">
            <input
              id={fieldId}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                if (error) setError(null);
              }}
              onKeyDown={(e) => {
                // Escape reverts the edit instead of closing Settings —
                // losing a half-typed name to a key meant to undo it would
                // be the opposite of what the key says. With nothing to
                // revert it bubbles and the dialog closes as usual.
                if (e.key === 'Escape' && draft !== name) {
                  e.stopPropagation();
                  setDraft(name);
                  setError(null);
                }
              }}
              disabled={busy}
              autoComplete="name"
              spellCheck={false}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? errorId : hintId}
              className="w-full flex-1 rounded-lg border border-border bg-bg px-3 py-2 text-sm text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none disabled:opacity-60"
            />
            <button
              type="submit"
              disabled={busy || !dirty}
              className="inline-flex shrink-0 items-center justify-center gap-2 rounded-md bg-accent-strong px-4 py-2 text-sm font-medium text-white transition-all duration-ts hover:brightness-110 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-35"
            >
              {busy && <Loader size={16} />}
              Save
            </button>
          </div>
          {error ? (
            <p
              id={errorId}
              role="alert"
              className="mt-2 flex items-start gap-1.5 text-sm text-danger"
            >
              <IconAlert size={14} className="mt-0.5 shrink-0" />
              {error}
            </p>
          ) : (
            <p id={hintId} className="mt-2 text-xs text-faint">
              This is what TechSara calls you in chat.
            </p>
          )}
        </form>
      ) : (
        <div className="mt-5">
          <Field label="Name" value={name} />
        </div>
      )}

      <div className="mt-5 space-y-3">
        <Field label="Email" value={account?.user?.email ?? '—'} />
        <Field label="Workspace" value={account?.workspace?.name ?? '—'} />
        <Field label="Role" value={roleLabel(account?.workspace?.role)} />
      </div>
      <p className="mt-5 text-xs text-faint">
        Email, workspace and role are managed by your workspace admin.
      </p>
    </section>
  );
}

/** The server's own sentence, when it sent one. */
async function readProfileDetail(res: Response): Promise<string | null> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    return typeof body.detail === 'string' ? body.detail : null;
  } catch {
    return null;
  }
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
