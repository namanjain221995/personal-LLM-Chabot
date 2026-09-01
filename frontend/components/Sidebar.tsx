'use client';

/**
 * Left sidebar (§9 + V2 §4a + V3 §2 + V4 §2): 260px, collapsible (mobile:
 * slide-over drawer). Header (mark · search icon · collapse icon) · New chat
 * · conversation list in ChatGPT's sections — Pinned, Recents, and a collapsed Archived
 * disclosure that lazily pulls `?archived=true` — each row carrying the "⋯"
 * menu (rename / pin / archive / export / delete) · theme toggle · account
 * row (enterprise auth retrofit — AccountMenu fetches its own identity, so
 * no user props flow through here).
 *
 * V4 §2 replaced the inline filter box with the search ICON below: filtering
 * only ever matched titles in the already-loaded list, while the palette it
 * opens searches message content server-side too.
 *
 * THE PANEL IS RENDERED TWICE — once as the desktop column, once inside the
 * mobile drawer — and CSS picks which one is visible. Three consequences that
 * the code below is shaped around:
 *
 * 1. Every id must be scoped per copy, or `sidebar-pinned` and friends appear
 *    twice and `aria-labelledby` / `aria-controls` resolve to whichever the
 *    document happens to hold first (M-12). `panel()` takes the scope and
 *    builds every id from one `useId()`.
 * 2. The collapsed desktop column is `w-0`, not unmounted, so its controls
 *    stayed in the tab order behind a zero-width edge — and `aria-hidden`
 *    over focusable content is itself invalid. `inert` closes both (L-02).
 * 3. The drawer is a modal overlay and now says so: dialog semantics, initial
 *    focus, a Tab trap, Escape, and focus restored to the toggle (L-03).
 */

import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from 'react';
import { conversationMenuHandlers } from '@/lib/conversationMenu';
import { focusableWithin, focusTrapNext } from '@/lib/focusTrap';
import { NEW_CHAT_SHORTCUT_LABEL } from '@/lib/searchPalette';
import type { ConversationSummary } from '@/lib/types';
import { AccountMenu } from './AccountMenu';
import { ConversationMenu } from './ConversationMenu';
import { TechSaraMark } from './TechSaraMark';
import { useTheme } from './Providers';
import {
  IconChevronRight,
  IconMoon,
  IconPin,
  IconPlus,
  IconSearch,
  IconSidebar,
  IconSun,
  IconX,
} from './icons';

interface SidebarProps {
  open: boolean;
  onClose: () => void;
  /** Active (non-archived) conversations, pinned first. */
  conversations: ConversationSummary[];
  /** Archived conversations (V3 §2) — empty hides the disclosure entirely. */
  archived: ConversationSummary[];
  activeId: string | null;
  /** Chats with a generation in progress — show a spinner on their row. */
  streamingIds?: string[];
  onNewChat: () => void;
  /** V4 §2: opens the centered search palette. */
  onOpenSearch: () => void;
  onSelect: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onSetPinned: (id: string, pinned: boolean) => void;
  onSetArchived: (id: string, archived: boolean) => void;
  onExport: (id: string) => void;
  /** First expand of the Archived disclosure (lazy `?archived=true` pull). */
  onLoadArchived: () => void;
  /**
   * The control that opens the sidebar (ChatApp's header toggle). Closing the
   * mobile drawer hands focus back to it (L-03).
   *
   * It has to arrive as a ref rather than be read from `document.activeElement`
   * when the drawer opens: the toggle only renders while the sidebar is
   * CLOSED, so by the time the drawer has mounted the opener is already gone
   * and the active element is <body>. The ref re-populates when the toggle
   * comes back, which is the same commit the drawer unmounts in.
   */
  restoreFocusRef?: RefObject<HTMLElement | null>;
}

export function Sidebar({
  open,
  onClose,
  conversations,
  archived,
  activeId,
  streamingIds = [],
  onNewChat,
  onOpenSearch,
  onSelect,
  onRename,
  onDelete,
  onSetPinned,
  onSetArchived,
  onExport,
  onLoadArchived,
  restoreFocusRef,
}: SidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [archivedOpen, setArchivedOpen] = useState(false);
  const archivedLoaded = useRef(false);
  const { theme, toggleTheme } = useTheme();

  /** One generated prefix for both copies; `panel()` scopes it per copy. */
  const baseId = useId();

  const drawerRef = useRef<HTMLElement>(null);
  /** Whatever had focus when the drawer opened — the fallback restore target. */
  const openerRef = useRef<HTMLElement | null>(null);
  /**
   * Did focus ever actually land inside the drawer?
   *
   * This is what keeps the desktop untouched without asking the viewport any
   * questions. The drawer's JSX is mounted at every width — `md:hidden` only
   * hides it — so on desktop the `focus()` below lands on a `display:none`
   * subtree and does nothing, React's onFocus never fires, and this stays
   * false, which means collapsing the desktop column restores nothing and
   * leaves the caret exactly where the user left it.
   */
  const drawerOwnedFocus = useRef(false);

  useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement as HTMLElement | null;
    drawerOwnedFocus.current = false;
    // preventScroll for the same reason the palette does it: focusing inside a
    // fixed overlay otherwise makes the browser scroll the thread behind it.
    drawerRef.current?.focus({ preventScroll: true });
    return () => {
      if (drawerOwnedFocus.current) {
        // Reading the ref LATE is the whole point here. react-hooks would have
        // us copy `.current` into a variable while the effect runs, but that
        // captures null: the toggle does not exist while the drawer is open,
        // and React re-mounts it and re-attaches this ref in the very commit
        // whose passive cleanup this is. See the prop's doc comment above.
        // eslint-disable-next-line react-hooks/exhaustive-deps
        const target = restoreFocusRef?.current ?? openerRef.current;
        if (target?.isConnected) target.focus({ preventScroll: true });
      }
      drawerOwnedFocus.current = false;
    };
  }, [open, restoreFocusRef]);

  function onDrawerKeyDown(e: ReactKeyboardEvent<HTMLElement>) {
    const drawer = drawerRef.current;
    if (!drawer) return;
    // A row's "⋯" menu portals its popup to <body>, and a React portal still
    // bubbles through the REACT tree — so its keystrokes arrive here. Escape
    // and Tab belong to the innermost layer that is open, so anything whose
    // real DOM home is outside the drawer is left to the layer that owns it.
    if (!drawer.contains(e.target as Node)) return;

    if (e.key === 'Escape') {
      e.preventDefault();
      // The window-level map must not ALSO see this Escape: behind the drawer
      // it would read as "stop the generation".
      e.stopPropagation();
      onClose();
      return;
    }

    if (e.key !== 'Tab') return;
    const nodes = focusableWithin(drawer);
    if (nodes.length === 0) return;
    e.preventDefault();
    focusTrapNext(nodes, document.activeElement, e.shiftKey)?.focus({
      preventScroll: true,
    });
  }

  const pinned = conversations.filter((c) => c.pinned);
  const recents = conversations.filter((c) => !c.pinned);

  function commitRename() {
    if (editingId && draftTitle.trim()) onRename(editingId, draftTitle);
    setEditingId(null);
  }

  function toggleArchived() {
    const next = !archivedOpen;
    setArchivedOpen(next);
    if (next && !archivedLoaded.current) {
      archivedLoaded.current = true;
      onLoadArchived();
    }
  }

  function row(c: ConversationSummary) {
    return (
      <li key={c.id} className="group relative">
        {editingId === c.id ? (
          <input
            autoFocus
            value={draftTitle}
            onChange={(e) => setDraftTitle(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitRename();
              if (e.key === 'Escape') {
                e.stopPropagation();
                setEditingId(null);
              }
            }}
            aria-label="Rename conversation"
            className="w-full rounded-lg border border-accent/60 bg-bg px-2.5 py-1.5 text-sm focus:outline-none"
          />
        ) : (
          <>
            <button
              type="button"
              onClick={() => onSelect(c.id)}
              aria-current={activeId === c.id ? 'true' : undefined}
              className={`flex w-full items-center gap-1.5 rounded-lg py-1.5 pl-2.5 pr-9 text-left text-sm transition-colors duration-ts ${
                activeId === c.id
                  ? 'bg-surface-2 text-ink'
                  // Full --ts-text, same as the active row: a conversation
                  // title is the primary content of this list, not secondary
                  // metadata, and --ts-text-muted (#b3b3b3) on the #0a0a0a
                  // sidebar read as a wall of grey. The ACTIVE row is already
                  // distinguished by its background, so brightness does not
                  // need to carry that job as well — which is exactly how
                  // ChatGPT's sidebar does it.
                  : 'text-ink hover:bg-surface-2/60'
              }`}
            >
              {c.pinned && (
                <IconPin size={11} className="shrink-0 text-faint" aria-hidden />
              )}
              <span className="min-w-0 truncate">{c.title}</span>
              {/* Busy indicator sits IN the flex flow, right after the title.
                  Absolute-positioning it collided with the "⋯" menu (always
                  visible on the active row) and, worse, its -translate-y-1/2
                  fought `animate-spin`: CSS interpolates translateY(-50%) →
                  rotate(360deg) as matrices, so the spinner slid up and down
                  every cycle. In-flow + rotation-only = rock steady. */}
              {streamingIds.includes(c.id) && (
                <span
                  aria-label="Answer in progress"
                  title="Answer in progress"
                  className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-muted/40 border-t-accent"
                />
              )}
            </button>
            <span className="absolute right-1.5 top-1/2 flex -translate-y-1/2">
              <ConversationMenu
                title={c.title}
                pinned={c.pinned === true}
                archived={c.archived === true}
                active={activeId === c.id}
                {...conversationMenuHandlers(c, {
                  rename: (id) => {
                    setEditingId(id);
                    setDraftTitle(c.title);
                  },
                  setPinned: onSetPinned,
                  setArchived: onSetArchived,
                  exportChat: onExport,
                  remove: onDelete,
                })}
              />
            </span>
          </>
        )}
      </li>
    );
  }

  /**
   * One copy of the panel. `scope` distinguishes the desktop column from the
   * drawer so the two mounted copies never share an id (M-12) — every id and
   * every reference to it below is built from `id()`, so they cannot drift.
   */
  function panel(scope: 'desktop' | 'mobile') {
    const id = (name: string) => `${baseId}${scope}-${name}`;

    return (
    <div className="flex h-full w-sidebar flex-col bg-sidebar">
      <div className="flex items-center gap-2 px-3 pb-2 pt-3">
        <TechSaraMark size={28} />
        <span className="flex-1 truncate text-sm font-semibold">TechSara</span>
        <button
          type="button"
          onClick={onOpenSearch}
          aria-label="Search chats"
          title="Search chats (Ctrl K)"
          className="rounded-lg p-1.5 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
        >
          <IconSearch size={16} />
        </button>
        {/* Collapse lives HERE on desktop, ChatGPT-style — right of the search
            icon, inside the panel it hides. The main header only re-grows a
            toggle once the sidebar is gone (see ChatApp), so there is never
            a second copy of this control on screen. */}
        <button
          type="button"
          onClick={onClose}
          aria-label="Hide sidebar"
          title="Hide sidebar"
          className="hidden rounded-lg p-1.5 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink md:block"
        >
          <IconSidebar size={16} />
        </button>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close sidebar"
          className="rounded-lg p-1.5 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink md:hidden"
        >
          <IconX size={16} />
        </button>
      </div>

      <div className="px-3">
        <button
          type="button"
          onClick={onNewChat}
          className="flex w-full items-center gap-2 rounded-ts border border-border bg-surface-2/60 px-3 py-2 text-sm font-medium transition-colors duration-ts hover:bg-surface-2"
        >
          <IconPlus size={15} className="text-accent" />
          New chat
          <kbd className="ml-auto rounded border border-border px-1.5 py-px font-mono text-[10px] text-faint">
            {NEW_CHAT_SHORTCUT_LABEL}
          </kbd>
        </button>
      </div>

      <nav
        aria-label="Conversations"
        className="mt-2 min-h-0 flex-1 overflow-y-auto px-2 pb-2"
      >
        {conversations.length === 0 && (
          <p className="px-3 py-4 text-xs text-faint">
            {archived.length > 0
              ? 'No active conversations.'
              : 'No conversations yet.'}
          </p>
        )}

        {pinned.length > 0 && (
          <section aria-labelledby={id('pinned')}>
            <h2
              id={id('pinned')}
              className="px-2.5 pb-1 pt-1.5 text-[11px] font-medium uppercase tracking-wide text-faint"
            >
              Pinned
            </h2>
            <ul className="space-y-0.5">{pinned.map(row)}</ul>
          </section>
        )}

        {recents.length > 0 && (
          <section aria-labelledby={pinned.length > 0 ? id('recents') : undefined}>
            {pinned.length > 0 && (
              <h2
                id={id('recents')}
                className="px-2.5 pb-1 pt-3 text-[11px] font-medium uppercase tracking-wide text-faint"
              >
                Recents
              </h2>
            )}
            <ul className="space-y-0.5">{recents.map(row)}</ul>
          </section>
        )}

        {archived.length > 0 && (
          <section className="mt-2 border-t border-border pt-2">
            <h2>
              <button
                type="button"
                onClick={toggleArchived}
                aria-expanded={archivedOpen}
                aria-controls={id('archived-list')}
                className="flex w-full items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-left text-xs text-icon transition-colors duration-ts hover:bg-surface-2/60 hover:text-ink"
              >
                <IconChevronRight
                  size={13}
                  className={`shrink-0 text-faint transition-transform duration-ts ${
                    archivedOpen ? 'rotate-90' : ''
                  }`}
                />
                Archived
                <span className="ml-auto text-faint">{archived.length}</span>
              </button>
            </h2>
            <ul
              id={id('archived-list')}
              hidden={!archivedOpen}
              className="mt-0.5 space-y-0.5"
            >
              {archived.map(row)}
            </ul>
          </section>
        )}
      </nav>

      <div className="border-t border-border p-2">
        <button
          type="button"
          onClick={toggleTheme}
          aria-label={
            theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'
          }
          className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-sm text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
        >
          {theme === 'dark' ? <IconSun size={15} /> : <IconMoon size={15} />}
          {theme === 'dark' ? 'Light theme' : 'Dark theme'}
        </button>

        <AccountMenu />
      </div>
    </div>
    );
  }

  return (
    <>
      {/* Desktop: collapsible column */}
      <aside
        className={`hidden shrink-0 overflow-hidden border-r border-border transition-[width] duration-200 md:block ${
          open ? 'w-sidebar' : 'w-0 border-r-0'
        }`}
        aria-label="Sidebar"
        aria-hidden={!open}
        // L-02. The column collapses to w-0 rather than unmounting — that is
        // what animates the width — so every control inside it stayed a tab
        // stop behind a zero-width edge, and `aria-hidden` wrapped around
        // focusable content is an invalid combination in its own right.
        // `inert` is the container-level answer to both: it drops the whole
        // subtree from the tab order AND from the accessibility tree, with no
        // per-control tabIndex bookkeeping to keep in sync, and it costs
        // nothing visually.
        inert={!open}
      >
        {panel('desktop')}
      </aside>

      {/* Mobile: slide-over drawer — a modal overlay, and now says so (L-03) */}
      {open && (
        <div className="fixed inset-0 z-50 md:hidden">
          {/* Click-outside-to-close. It is deliberately NOT a tab stop: a
              full-screen button ahead of the panel is a phantom stop for
              keyboard users, and the panel's own "Close sidebar" ✕ already
              provides that action to them. */}
          <button
            type="button"
            aria-hidden
            tabIndex={-1}
            onClick={onClose}
            className="absolute inset-0 h-full w-full bg-black/50"
          />
          <aside
            ref={drawerRef}
            role="dialog"
            aria-modal="true"
            aria-label="Sidebar"
            // Programmatically focusable so opening the drawer can move focus
            // into it and announce the dialog; -1 keeps it out of the Tab
            // cycle, and the ring is suppressed because this is never a stop
            // the user chose to land on.
            tabIndex={-1}
            onFocus={() => {
              drawerOwnedFocus.current = true;
            }}
            onKeyDown={onDrawerKeyDown}
            className="absolute inset-y-0 left-0 border-r border-border shadow-2xl focus:outline-none"
          >
            {panel('mobile')}
          </aside>
        </div>
      )}
    </>
  );
}
