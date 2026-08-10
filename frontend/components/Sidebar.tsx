'use client';

/**
 * Left sidebar (§9 + V2 §4a + V3 §2 + V4 §2): 260px, collapsible (mobile:
 * slide-over drawer). Header (mark · search icon · collapse icon) · New chat
 * · conversation list in ChatGPT's sections — Pinned, Recents, and a collapsed Archived
 * disclosure that lazily pulls `?archived=true` — each row carrying the "⋯"
 * menu (rename / pin / archive / export / delete) · theme toggle.
 * No account UI at all: this app has no users to show.
 *
 * V4 §2 replaced the inline filter box with the search ICON below: filtering
 * only ever matched titles in the already-loaded list, while the palette it
 * opens searches message content server-side too.
 */

import { useEffect, useRef, useState } from 'react';
import { conversationMenuHandlers } from '@/lib/conversationMenu';
import type { ConversationSummary } from '@/lib/types';
import { ConversationMenu } from './ConversationMenu';
import { TechSaraMark } from './TechSaraMark';
import { useTheme } from './Providers';
import {
  IconChevronDown,
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
}: SidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [archivedOpen, setArchivedOpen] = useState(false);
  const archivedLoaded = useRef(false);
  const { theme, toggleTheme } = useTheme();

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
                  : 'text-muted hover:bg-surface-2/60 hover:text-ink'
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

  const body = (
    <div className="flex h-full w-sidebar flex-col bg-sidebar">
      <div className="flex items-center gap-2 px-3 pb-2 pt-3">
        <TechSaraMark size={28} />
        <span className="flex-1 truncate text-sm font-semibold">TechSara</span>
        <button
          type="button"
          onClick={onOpenSearch}
          aria-label="Search chats"
          title="Search chats (Ctrl K)"
          className="rounded-lg p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
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
          className="hidden rounded-lg p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink md:block"
        >
          <IconSidebar size={16} />
        </button>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close sidebar"
          className="rounded-lg p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink md:hidden"
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
            Ctrl ⇧ O
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
          <section aria-labelledby="sidebar-pinned">
            <h2
              id="sidebar-pinned"
              className="px-2.5 pb-1 pt-1.5 text-[11px] font-medium uppercase tracking-wide text-faint"
            >
              Pinned
            </h2>
            <ul className="space-y-0.5">{pinned.map(row)}</ul>
          </section>
        )}

        {recents.length > 0 && (
          <section aria-labelledby="sidebar-recents">
            {pinned.length > 0 && (
              <h2
                id="sidebar-recents"
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
                aria-controls="sidebar-archived-list"
                className="flex w-full items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-left text-xs text-muted transition-colors duration-ts hover:bg-surface-2/60 hover:text-ink"
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
              id="sidebar-archived-list"
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
          className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
        >
          {theme === 'dark' ? <IconSun size={15} /> : <IconMoon size={15} />}
          {theme === 'dark' ? 'Light theme' : 'Dark theme'}
        </button>

      </div>
    </div>
  );

  return (
    <>
      {/* Desktop: collapsible column */}
      <aside
        className={`hidden shrink-0 overflow-hidden border-r border-border transition-[width] duration-200 md:block ${
          open ? 'w-sidebar' : 'w-0 border-r-0'
        }`}
        aria-label="Sidebar"
        aria-hidden={!open}
      >
        {body}
      </aside>

      {/* Mobile: slide-over drawer */}
      {open && (
        <div className="fixed inset-0 z-50 md:hidden">
          <button
            type="button"
            aria-label="Close sidebar"
            onClick={onClose}
            className="absolute inset-0 h-full w-full bg-black/50"
          />
          <aside
            aria-label="Sidebar"
            className="absolute inset-y-0 left-0 border-r border-border shadow-2xl"
          >
            {body}
          </aside>
        </div>
      )}
    </>
  );
}
