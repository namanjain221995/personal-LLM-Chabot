'use client';

/**
 * Chat search palette (V4 §2) — ChatGPT's Ctrl/Cmd+K modal: a centered panel
 * over a dimmed backdrop with an auto-focused input, "New chat" as the first
 * row, and hits grouped Today / Yesterday / Previous 7 Days / Older. Title
 * hits show the title; content hits show the matched snippet underneath.
 *
 * Every decision — reading the payload, date bucketing, row order and
 * numbering, snippet choice, the keyboard map, the debounce — comes from the
 * pure helpers in lib/searchPalette.ts; this file is the rendering shell.
 * Styling mirrors ConversationMenu / ModelPicker: surface panel, 1px border,
 * 10px radius, xl shadow, surface-2 highlight.
 *
 * Two hard-won constraints from the V3 popover, both load-bearing here:
 *
 * 1. It is PORTALLED to <body>. The sidebar wraps its rows in transformed
 *    elements, and a transformed ancestor becomes the containing block for
 *    position:fixed — a modal rendered in that tree is offset and painted
 *    behind the thread column.
 * 2. Every focus() passes `preventScroll`. Focusing inside a fixed overlay
 *    otherwise makes the browser scroll ancestors to "reveal" it, which
 *    yanks the conversation thread underneath the palette.
 *
 * Focus itself never leaves the input: the highlight is expressed with
 * aria-activedescendant, which is what lets ArrowDown and typing coexist.
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { createPortal } from 'react-dom';
import { searchConversations } from '@/lib/historyApi';
import {
  buildPaletteModel,
  createDebounce,
  normalizeQuery,
  paletteKeyAction,
  parseSearchResults,
  resultsFromSummaries,
  rowSnippet,
  SEARCH_DEBOUNCE_MS,
  SEARCH_MAX_QUERY,
  trapFocusIndex,
  type PaletteRow,
  type SearchResult,
} from '@/lib/searchPalette';
import type { ConversationSummary } from '@/lib/types';
import { IconMessage, IconPin, IconPlus, IconSearch, IconX } from './icons';

/** Elements Tab may land on inside the modal. */
const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

type SearchStatus = 'idle' | 'loading' | 'error';

export interface SearchPaletteProps {
  open: boolean;
  onClose: () => void;
  /** Already-loaded conversations, shown while the query is empty (V4 §2). */
  recents: ConversationSummary[];
  onSelect: (id: string) => void;
  onNewChat: () => void;
  /** Injection seam for tests; defaults to the real proxy call. */
  searchFn?: (query: string, signal: AbortSignal) => Promise<unknown>;
}

function defaultSearch(query: string, signal: AbortSignal): Promise<unknown> {
  return searchConversations(query, { signal });
}

export function SearchPalette({
  open,
  onClose,
  recents,
  onSelect,
  onNewChat,
  searchFn = defaultSearch,
}: SearchPaletteProps) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [status, setStatus] = useState<SearchStatus>('idle');
  const [activeIndex, setActiveIndex] = useState(0);

  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  /** Activating a row hands focus to the shell — do not yank it back. */
  const restoreFocusRef = useRef(true);

  const baseId = useId();
  const titleId = `${baseId}-title`;
  const listId = `${baseId}-list`;
  const rowId = (index: number) => `${baseId}-row-${index}`;

  const trimmed = normalizeQuery(query);

  /* ------------------------------------------------------------ fetching */

  const runSearch = useCallback(
    (q: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setStatus('loading');
      void (async () => {
        try {
          const body = await searchFn(q, controller.signal);
          if (controller.signal.aborted) return;
          setResults(parseSearchResults(body));
          setActiveIndex(0);
          setStatus('idle');
        } catch {
          // A superseded search aborts; anything else is a real failure and
          // gets an inline line rather than an empty palette.
          if (controller.signal.aborted) return;
          setResults([]);
          setStatus('error');
        } finally {
          if (abortRef.current === controller) abortRef.current = null;
        }
      })();
    },
    [searchFn],
  );

  // One debouncer for the lifetime of the palette, reading the latest
  // runSearch through a ref — recreating it per keystroke would defeat the
  // collapsing it exists for.
  const runSearchRef = useRef(runSearch);
  runSearchRef.current = runSearch;
  const debounceRef = useRef<ReturnType<
    typeof createDebounce<[string]>
  > | null>(null);
  if (debounceRef.current === null) {
    debounceRef.current = createDebounce<[string]>(
      (q) => runSearchRef.current(q),
      SEARCH_DEBOUNCE_MS,
    );
  }

  useEffect(() => {
    const debounce = debounceRef.current;
    if (!open) {
      // Closing must not leave a keystroke in flight.
      debounce?.cancel();
      abortRef.current?.abort();
      abortRef.current = null;
      return;
    }
    if (!trimmed) {
      // Back to empty: drop the pending request and fall through to recents.
      debounce?.cancel();
      abortRef.current?.abort();
      abortRef.current = null;
      setResults(null);
      setStatus('idle');
      setActiveIndex(0);
      return;
    }
    debounce?.run(trimmed);
  }, [open, trimmed]);

  // Closing (or unmounting) must not leave a timer or a request behind.
  useEffect(
    () => () => {
      debounceRef.current?.cancel();
      abortRef.current?.abort();
      abortRef.current = null;
    },
    [],
  );

  /* --------------------------------------------------------- open/close */

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    restoreFocusRef.current = true;
    setQuery('');
    setResults(null);
    setStatus('idle');
    setActiveIndex(0);
    inputRef.current?.focus({ preventScroll: true });
    return () => {
      if (restoreFocusRef.current) previous?.focus?.({ preventScroll: true });
    };
  }, [open]);

  /* ------------------------------------------------------------- model */

  const recentResults = useMemo(
    () => resultsFromSummaries(recents),
    [recents],
  );
  // Empty query → the list the shell already has. Otherwise the newest server
  // answer, keeping the previous one on screen while the next lands so the
  // palette does not blink between keystrokes.
  const model = useMemo(
    () => buildPaletteModel(trimmed === '' ? recentResults : (results ?? [])),
    [trimmed, recentResults, results],
  );

  // Results change under the highlight (a slower request lands, the query is
  // cleared) — never point past the end.
  const highlighted = Math.min(Math.max(activeIndex, 0), model.rows.length - 1);

  // Keep the highlight visible by scrolling the LIST ONLY. scrollIntoView
  // would happily scroll the page behind the modal as well.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const row = list.querySelector<HTMLElement>(
      `[data-row-index="${highlighted}"]`,
    );
    if (!row) return;
    const top = row.offsetTop;
    const bottom = top + row.offsetHeight;
    if (top < list.scrollTop) {
      list.scrollTop = top;
    } else if (bottom > list.scrollTop + list.clientHeight) {
      list.scrollTop = bottom - list.clientHeight;
    }
  }, [highlighted, model]);

  /* -------------------------------------------------------- activation */

  const activate = useCallback(
    (row: PaletteRow | undefined) => {
      if (!row) return;
      restoreFocusRef.current = false;
      if (row.kind === 'new-chat') onNewChat();
      else onSelect(row.result.id);
      onClose();
    },
    [onClose, onNewChat, onSelect],
  );

  /* ---------------------------------------------------------- keyboard */

  function trapTab(e: ReactKeyboardEvent<HTMLDivElement>) {
    const panel = panelRef.current;
    if (!panel) return;
    const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (nodes.length === 0) return;
    e.preventDefault();
    const current = nodes.indexOf(document.activeElement as HTMLElement);
    nodes[trapFocusIndex(current, nodes.length, e.shiftKey)]?.focus({
      preventScroll: true,
    });
  }

  function onPanelKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Tab') {
      trapTab(e);
      return;
    }
    const action = paletteKeyAction(e.key, highlighted, model.rows.length);
    if (!action) return;
    e.preventDefault();
    if (action.kind === 'close') {
      onClose();
      return;
    }
    if (action.kind === 'move') {
      setActiveIndex(action.index);
      return;
    }
    activate(model.rows[highlighted]);
  }

  /* ------------------------------------------------------------ render */

  if (!open) return null;

  const rowClass = (selected: boolean) =>
    `flex w-full cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors duration-ts ${
      selected ? 'bg-surface-2 text-ink' : 'text-muted'
    }`;

  function renderRow(row: PaletteRow) {
    const selected = row.index === highlighted;

    if (row.kind === 'new-chat') {
      return (
        <div
          key="new-chat"
          id={rowId(row.index)}
          data-row-index={row.index}
          role="option"
          aria-selected={selected}
          onMouseMove={() => setActiveIndex(row.index)}
          onClick={() => activate(row)}
          className={rowClass(selected)}
        >
          <IconPlus size={15} className="shrink-0 text-accent" />
          <span className="text-sm font-medium text-ink">New chat</span>
        </div>
      );
    }

    const { result } = row;
    const snippet = rowSnippet(result);
    return (
      <div
        key={result.id}
        id={rowId(row.index)}
        data-row-index={row.index}
        role="option"
        aria-selected={selected}
        onMouseMove={() => setActiveIndex(row.index)}
        onClick={() => activate(row)}
        className={rowClass(selected)}
      >
        <IconMessage size={15} className="shrink-0 text-faint" />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            {result.pinned && (
              <IconPin size={11} className="shrink-0 text-faint" />
            )}
            <span className="min-w-0 truncate text-sm text-ink">
              {result.title}
            </span>
            {result.archived && (
              <span className="shrink-0 rounded border border-border px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-faint">
                Archived
              </span>
            )}
          </span>
          {snippet && (
            <span className="mt-0.5 block truncate text-xs text-muted">
              {snippet}
            </span>
          )}
        </span>
      </div>
    );
  }

  // "Waiting for the first answer to this query" — covers both the debounce
  // window and the request, so no "nothing found" flashes while typing.
  const pending = trimmed !== '' && results === null && status !== 'error';
  const noHits =
    trimmed !== '' && status !== 'error' && results !== null && results.length === 0;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center px-4 pb-10 pt-[12vh]">
      {/* Backdrop: click-outside closes. Escape is the keyboard path, so this
          stays out of the tab order rather than adding a phantom stop. */}
      <div
        aria-hidden
        onMouseDown={onClose}
        className="palette-backdrop absolute inset-0 bg-black/60"
      />

      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onPanelKeyDown}
        className="palette-panel relative flex max-h-[70vh] w-full max-w-[640px] flex-col overflow-hidden rounded-ts border border-border bg-surface shadow-xl"
      >
        <h2 id={titleId} className="sr-only">
          Search chats
        </h2>

        <div className="flex shrink-0 items-center gap-2.5 border-b border-border px-4">
          <IconSearch size={16} className="shrink-0 text-faint" />
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={
              model.rows.length > 0 ? rowId(highlighted) : undefined
            }
            aria-label="Search chats"
            placeholder="Search chats…"
            value={query}
            maxLength={SEARCH_MAX_QUERY}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setQuery(e.target.value)}
            className="min-w-0 flex-1 bg-transparent py-3.5 text-sm text-ink placeholder:text-faint focus:outline-none"
          />
          <button
            type="button"
            onClick={onClose}
            aria-label="Close search"
            className="shrink-0 rounded-md p-1 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconX size={15} />
          </button>
        </div>

        <div
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label="Chats"
          className="relative min-h-0 flex-1 overflow-y-auto p-1.5"
        >
          {model.sections.map((section) => (
            <div
              key={section.label ?? 'actions'}
              role="group"
              aria-label={section.label ?? 'Actions'}
            >
              {section.label && (
                <div
                  aria-hidden
                  className="px-2.5 pb-1 pt-3 text-[11px] font-medium uppercase tracking-wide text-faint"
                >
                  {section.label}
                </div>
              )}
              {section.rows.map(renderRow)}
            </div>
          ))}

          {pending && (
            <p className="px-2.5 py-3 text-xs text-faint">Searching…</p>
          )}
          {status === 'error' && (
            <p className="px-2.5 py-3 text-xs text-faint">
              Search is unavailable right now.
            </p>
          )}
          {noHits && (
            <p className="px-2.5 py-3 text-xs text-faint">
              No chats match “{trimmed}”
            </p>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
