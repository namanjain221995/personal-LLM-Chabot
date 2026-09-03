'use client';

/**
 * The composer's "+" button and its ChatGPT-style popover (owner request
 * 2026-08-05): Add photos & files · Web search · Salesforce. A thin rendering
 * shell — which items exist and what they do lives in lib/composerMenu.ts.
 * Popover mechanics follow ModelPicker (opens upward, outside click and
 * Escape close), keyboard follows the WAI-ARIA menu map via menuKeyAction.
 */

import { useEffect, useRef, useState } from 'react';
import {
  activateComposerMenuItem,
  composerMenuItems,
  nextEnabledIndex,
  type ComposerMenuItemId,
} from '@/lib/composerMenu';
import { menuKeyAction } from '@/lib/conversationMenu';
import type { ChatPrefs } from '@/lib/prefs';
import {
  IconBook,
  IconCheck,
  IconCloud,
  IconGlobe,
  IconPaperclip,
  IconPlus,
  IconSparkles,
} from './icons';

const ITEM_ICON: Record<ComposerMenuItemId, React.ComponentType<{ size?: number; className?: string }>> = {
  files: IconPaperclip,
  'web-search': IconGlobe,
  'deep-research': IconBook,
  salesforce: IconCloud,
  'sf-live': IconSparkles,
};

export function AttachMenu({
  prefs,
  streaming,
  features,
  onPrefsChange,
  onPickFiles,
}: {
  prefs: ChatPrefs;
  streaming: boolean;
  /** Resolved tool access — rows this account may not use are not listed. */
  features?: Record<string, boolean>;
  onPrefsChange: (next: ChatPrefs) => void;
  onPickFiles: () => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const chipRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const items = composerMenuItems({
    salesforce: prefs.salesforce,
    sfLive: prefs.sfLive,
    webSearchOn: prefs.webSearch === 'on',
    deepResearchOn: prefs.deepResearch,
    streaming,
    features,
  });

  // Close on outside click / Escape while open (ModelPicker pattern).
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        // CONSUME it: while an answer is streaming, ChatApp's window-level
        // shortcut maps a bare Escape to "stop generating" — and this menu
        // is built for mid-stream use. Without stopPropagation, dismissing
        // the popover would also kill the in-flight answer.
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
        chipRef.current?.focus();
      }
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  // Focus the first usable row when the popover opens (ConversationMenu
  // pattern) — focus never entering a role=menu strands keyboard and
  // screen-reader users, and the arrow-key map below would be unreachable.
  useEffect(() => {
    if (!open) return;
    itemRefs.current.find((el) => el && !el.disabled)?.focus();
  }, [open]);

  function activate(id: ComposerMenuItemId) {
    const outcome = activateComposerMenuItem(id, prefs);
    setOpen(false);
    chipRef.current?.focus();
    if (outcome.kind === 'pick-files') onPickFiles();
    else onPrefsChange(outcome.prefs);
  }

  function onMenuKeyDown(e: React.KeyboardEvent, index: number) {
    const action = menuKeyAction(e.key, index, items.length);
    if (!action) return;
    // Enter/Space already activate a focused <button>; Escape is consumed by
    // the document listener before it gets here.
    if (action.kind === 'close') {
      // Tab: retract the popover but DON'T steal focus back — the user asked
      // to move on. Left open, the stale menu floats over the thread and its
      // Escape handler would later yank focus out of the textarea.
      setOpen(false);
      return;
    }
    if (action.kind === 'move') {
      e.preventDefault();
      const forward = e.key === 'ArrowDown' || e.key === 'Home';
      itemRefs.current[nextEnabledIndex(items, action.index, forward)]?.focus();
    }
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={chipRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Add photos, files and tools"
        title="Add photos, files and tools"
        className="shrink-0 rounded-full border border-border p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
      >
        <IconPlus
          size={16}
          className={`transition-transform duration-ts ${open ? 'rotate-45' : ''}`}
        />
      </button>

      {open && (
        <div
          role="menu"
          aria-label="Add photos, files and tools"
          className="absolute bottom-full left-0 z-30 mb-2 w-[288px] rounded-ts border border-border bg-surface p-1 shadow-xl"
        >
          {items.map((item, i) => {
            const Icon = ITEM_ICON[item.id];
            const isToggle = item.checked !== undefined;
            return (
              <button
                key={item.id}
                ref={(el) => {
                  itemRefs.current[i] = el;
                }}
                type="button"
                role={isToggle ? 'menuitemcheckbox' : 'menuitem'}
                aria-checked={isToggle ? item.checked : undefined}
                disabled={item.disabled}
                onClick={() => activate(item.id)}
                onKeyDown={(e) => onMenuKeyDown(e, i)}
                className="flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors duration-ts enabled:hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Icon size={16} className="mt-0.5 shrink-0 text-muted" />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium">{item.label}</span>
                  <span className="mt-0.5 block text-xs text-muted">
                    {item.hint}
                  </span>
                </span>
                {item.checked && (
                  <IconCheck size={14} className="mt-1 shrink-0 text-accent" />
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
