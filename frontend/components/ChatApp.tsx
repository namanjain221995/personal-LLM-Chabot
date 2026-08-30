'use client';

/**
 * The chat shell (§9 + V2 §4): sidebar + bare header (engine badge only) +
 * centered 768px thread + pinned composer. Owns streaming state (token /
 * reasoning / step events), server-backed history (offline cache +
 * one-time migration), per-conversation composer prefs (Salesforce toggle,
 * model, effort, agent), the V4 §2 search
 * palette and the keyboard shortcuts (Ctrl/Cmd+K search · Ctrl/Cmd+Shift+O
 * new chat · Esc close palette / stop · "/" focus composer).
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

/** useLayoutEffect on the client, useEffect on the server (no SSR warning). */
const useIsomorphicLayoutEffect =
  typeof window !== 'undefined' ? useLayoutEffect : useEffect;
import { fetchMe } from '@/lib/auth';
import { toClientError, type ClientError } from '@/lib/errorTypes';
import { downloadMarkdown } from '@/lib/exportMarkdown';
import { getHistoryStore, newId, setEvictListener } from '@/lib/history';
import {
  adoptDraftPrefs,
  DEFAULT_PREFS,
  loadPrefs,
  removePrefs,
  savePrefs,
  type ChatPrefs,
} from '@/lib/prefs';
import { attachmentsForResend, rememberAttachments } from '@/lib/attachments';
import {
  branchForAppend,
  branchForVersion,
  buildThread,
  hasBranches,
  metaWithBranch,
  ROOT,
  versionMap,
  selectVersion,
  type BranchSelection,
} from '@/lib/branching';
import { truncateFailure } from '@/lib/historyApi';
import ChatErrorPage from './ChatErrorPage';
import { shortcutAction } from '@/lib/searchPalette';
import {
  attachStream,
  clarificationAlreadySubmitted,
  fetchServerActive,
  getLiveStream,
  isStreaming,
  markClarificationSubmitted,
  messagesDiscardedByRegenerate,
  startStream,
  stopStream,
  streamingIds,
  subscribeStreams,
} from '@/lib/streams';
import {
  buildResponse,
  cardState,
  pendingClarification,
  type ClarificationRequest,
  type ClarificationResponse,
} from '@/lib/clarification';
import {
  cancelClarification,
  fetchSalesforceContext,
  shouldShowStarter,
  type StarterOption,
} from '@/lib/salesforceApi';
import {
  latestUsage,
  meterView,
  readFoldableCounts,
} from '@/lib/contextMeter';
import { isCompacting, requestCompact } from '@/lib/compact';
import type {
  ChatMessage,
  ConversationSummary,
  Engine,
  PastedText,
} from '@/lib/types';
import { Composer, type Attachment, type ComposerHandle } from './Composer';
import { SalesforceStarterCard } from './SalesforceStarterCard';
import { ConfirmDialog } from './ConfirmDialog';
import { ContextMeter } from './ContextMeter';
import { SummaryPanel } from './SummaryPanel';
import { EmptyState } from './EmptyState';
import { EngineBadge } from './EngineBadge';
import { MessageRow, type UploadStatus } from './MessageRow';
import { ClarificationCard } from './ClarificationCard';
import { SearchPalette } from './SearchPalette';
import { Sidebar } from './Sidebar';
import { useToast } from './Providers';
import { IconArrowDown, IconSidebar } from './icons';

const APP_NAME =
  process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

export function ChatApp() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [archived, setArchived] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [unreachable, setUnreachable] = useState(false);
  /**
   * H-01: the dataset turn whose /api/upload is still in flight, or failed.
   *
   * Deliberately NOT `streaming`. A dataset uploads before any generation
   * exists, so treating the two as one state made the composer offer Stop for
   * a model that was not running — and pressing it did nothing, because no
   * stream had been registered to abort.
   */
  const [datasetUpload, setDatasetUpload] = useState<{
    /** The chat it belongs to — a first message CREATES this id mid-send. */
    conversationId: string;
    messageId: string;
    status: UploadStatus;
  } | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [searchOpen, setSearchOpen] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  const [prefs, setPrefs] = useState<ChatPrefs>(DEFAULT_PREFS);
  /** Conversations the SERVER is still generating for (polled; survives reloads). */
  const [serverActive, setServerActive] = useState<string[]>([]);
  /** Bumped on every stream notification so sidebar spinners re-render. */
  const [, setStreamTick] = useState(0);
  /**
   * True until the mount effect has determined whether the restored chat has
   * a generation still running. The composer stays locked meanwhile: a send
   * in that window replaces (and destroys) the in-flight generation.
   * Only a ?c= restore can collide with one, so a bare "/" never waits.
   */
  const [reconciling, setReconciling] = useState(false);

  // Set BEFORE first paint, so the composer is never briefly interactive.
  // A useState initializer cannot do this: it runs during SSR where there is
  // no location, and hydration keeps the server's value.
  useIsomorphicLayoutEffect(() => {
    if (new URLSearchParams(window.location.search).has('c')) {
      setReconciling(true);
    }
  }, []);
  /** Armed regenerate awaiting confirmation (it would discard later turns). */
  const [pendingRegenerate, setPendingRegenerate] = useState<{
    messageId: string;
    discarded: number;
  } | null>(null);
  /**
   * The user turn open for in-place editing, if any (ChatGPT-style).
   *
   * Owned HERE rather than inside the row so at most one editor can be open —
   * two boxes competing to add a version of the same turn is confusion with
   * no upside.
   */
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  /**
   * Which alternative is live at each fork of the conversation tree.
   *
   * Editing a turn no longer replaces it — it adds a version beside it — so
   * `messages` holds EVERY branch and this says which single path is on
   * screen. Empty means "the newest everywhere", which is what a freshly
   * opened conversation shows and what an edit selects.
   */
  const [branchSelection, setBranchSelection] = useState<BranchSelection>({});
  /** Debounced draft text — the meter's only estimated component. */
  const [draft, setDraft] = useState('');
  const [compacting, setCompacting] = useState(false);
  /**
   * Turns a compaction would fold RIGHT NOW, from the server. null means the
   * question has not been asked yet or could not be answered — the meter then
   * leaves the button enabled, which is the behaviour that predates this.
   */
  const [foldableTurns, setFoldableTurns] = useState<number | null>(null);
  /** Turns the last successful compaction folded — a lasting popover line. */
  const [lastFoldedTurns, setLastFoldedTurns] = useState<number | null>(null);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const draftTimer = useRef<number | null>(null);
  /** Salesforce starter-card suggestions for the OPEN chat (server-filtered). */
  const [starterOptions, setStarterOptions] = useState<StarterOption[]>([]);
  /**
   * The clarification whose answer is in flight, by id — NOT a boolean.
   *
   * A boolean was a latch: it was set on submit and only cleared when the
   * conversation changed, so the SECOND question in a chat rendered
   * permanently disabled and could not be clicked at all. Keying on the id
   * makes it self-healing — a new question has a new id, so it is never
   * covered by the previous answer's lock.
   */
  const [submittingClarificationId, setSubmittingClarificationId] = useState<
    string | null
  >(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<ComposerHandle>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  messagesRef.current = messages;
  /**
   * The conversation as READ: one path down the tree.
   *
   * `messages` is everything stored, sibling branches included, and is what
   * gets persisted. `thread` is what the user sees AND what the model is
   * sent — the two were the same list until edits stopped being destructive.
   * Anything about the conversation ON SCREEN reads this one.
   */
  const thread = useMemo(
    () => buildThread(messages, branchSelection),
    [messages, branchSelection],
  );
  const threadRef = useRef<ChatMessage[]>([]);
  threadRef.current = thread;
  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;
  const prefsRef = useRef<ChatPrefs>(prefs);
  prefsRef.current = prefs;
  const serverActiveRef = useRef<string[]>([]);
  serverActiveRef.current = serverActive;

  /** Keep the URL pointing at the open chat so a reload lands back on it. */
  const setUrlConversation = useCallback((id: string | null) => {
    window.history.replaceState(null, '', id ? `/?c=${id}` : '/');
  }, []);

  const { toast } = useToast();

  const refreshList = useCallback(() => {
    const store = getHistoryStore();
    setConversations(store.list());
    setArchived(store.listArchived());
  }, []);

  // Initial load: cached history immediately, then auth check → one-time
  // migration → server refresh (V2 §4a/§4b); evict-toast wiring, ?c= deep
  // link, responsive sidebar default.
  useEffect(() => {
    // Only the no-IndexedDB fallback cache can still hit the localStorage
    // quota; when it trims, say so once — calmly — instead of the old
    // red-pill-per-conversation cascade. Server copies are unaffected.
    let lastEvictToast = 0;
    setEvictListener(() => {
      const now = Date.now();
      if (now - lastEvictToast > 60_000) {
        lastEvictToast = now;
        toast(
          'Browser cache is full — oldest cached conversations were trimmed locally. Your chats are safe on the server.',
        );
      }
      refreshList();
    });
    refreshList();

    // Reload keeps the open chat: adopt the ?c= id immediately; its cached
    // messages render right after the cache hydrates (below), and server
    // truth (plus any still-running generation) is reconciled after that.
    const wanted = new URLSearchParams(window.location.search).get('c');
    if (wanted) {
      setActiveId(wanted);
      activeIdRef.current = wanted;
      setPrefs(loadPrefs(window.localStorage, wanted));
    }

    if (window.matchMedia('(max-width: 767px)').matches) {
      setSidebarOpen(false);
    }

    let cancelled = false;
    void (async () => {
      const store = getHistoryStore();
      // IndexedDB hydration (single-digit ms; instant for the fallback).
      await store.ready();
      if (cancelled) return;
      refreshList();
      if (wanted && activeIdRef.current === wanted) {
        const cached = store.get(wanted);
        if (cached && cached.messages.length > 0 && !isStreaming(wanted)) {
          setMessages(cached.messages);
        }
      }
      // Whatever happens with auth/refresh below, the running-generation
      // check MUST run before the composer unlocks — sending during that gap
      // cancels the detached generation and silently loses its answer.
      const settleReconcile = () => {
        if (!cancelled) setReconciling(false);
      };
      const me = await fetchMe();
      if (cancelled) return;
      if (!me.ok) {
        // There is no login to bounce to any more, so ANY failure here (the
        // orchestrator still booting, a network blip) is handled the same way:
        // carry on, but never leave a running generation unguarded.
        if (wanted) {
          const active = await fetchServerActive();
          if (!cancelled) setServerActive(active);
        }
        settleReconcile();
        return;
      }
      store.setActiveUser(me.username);
      try {
        const migrated = await store.migrateLocalConversations();
        if (migrated > 0) {
          toast(
            `Moved ${migrated} local conversation${migrated === 1 ? '' : 's'} to your account.`,
          );
        }
      } catch {
        // Migration retries on the next sign-in; nothing is lost locally.
      }
      await store.refresh();
      if (cancelled) return;
      refreshList();

      try {
        if (wanted) {
        // Still generating server-side? Re-join the live stream — it replays
        // the partial answer instantly, then keeps streaming. Otherwise load
        // server truth; FORCED when the chat ends on a user message, because
        // a detached generation may have finished and saved its answer while
        // this tab was closed or reloading.
          const active = await fetchServerActive();
          if (cancelled) return;
          setServerActive(active);
          if (active.includes(wanted)) {
            setStreaming(true);
            void attachStream(wanted).then((ok) => {
              if (!ok && activeIdRef.current === wanted) {
                // Finished during the reload gap — its answer is in history.
                setStreaming(false);
                void store.load(wanted, { force: true }).then((conv) => {
                  if (conv && activeIdRef.current === wanted && !isStreaming(wanted)) {
                    setMessages(conv.messages);
                  }
                });
              }
            });
            return;
          }
          const cached = store.get(wanted);
          const force = cached?.messages.at(-1)?.role === 'user';
          const conv = await store.load(wanted, { force });
          if (conv && !cancelled && activeIdRef.current === wanted) {
            setMessages(conv.messages);
          }
        }
      } finally {
        settleReconcile();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshList, toast]);

  const persist = useCallback(
    (conversationId: string, msgs: ChatMessage[]) => {
      getHistoryStore().saveMessages(conversationId, msgs);
      refreshList();
    },
    [refreshList],
  );

  // Mirror live streams into the view + sidebar (lib/streams.ts): tokens for
  // the OPEN chat update the thread; every chat's spinner state re-renders.
  useEffect(() => {
    return subscribeStreams((id) => {
      setStreamTick((t) => t + 1);
      const s = getLiveStream(id);
      if (!s) return;
      if (s.status !== 'streaming') {
        refreshList(); // finished → reorder list
        // Name the chat from the exchange that just completed. Fired here,
        // after the answer has fully streamed, rather than from the server:
        // nothing pulls a server-side title change into this cache except a
        // full refresh, so a title written behind our back would stay
        // invisible until the next page load. The store no-ops unless the
        // conversation still has its auto-derived title.
        if (s.status === 'done') {
          void getHistoryStore()
            .generateTitle(id)
            .then(refreshList)
            .catch(() => undefined);
        }
        // Clear it from the polled set NOW: waiting for the next 8s poll left
        // the sidebar spinner turning for seconds after the answer landed.
        setServerActive((prev) =>
          prev.includes(id) ? prev.filter((x) => x !== id) : prev,
        );
      }
      if (id !== activeIdRef.current) return;
      if (s.status !== 'streaming') {
        // The continuation this answer belongs to is over — however it ended.
        // The lock was only ever cleared when the CONVERSATION changed, so a
        // card whose run failed, was stopped, or lost its connection sat with
        // "Continuing your request…" under it and every control disabled,
        // permanently, with no way back except switching chats.
        setSubmittingClarificationId(null);
      }
      setMessages([...s.messages]);
      setStreaming(s.status === 'streaming');
      if (s.status === 'unreachable') setUnreachable(true);
    });
  }, [refreshList]);

  // Poll for generations still running server-side: powers the sidebar
  // spinner across reloads and pulls in answers that finished while this
  // conversation wasn't on screen.
  useEffect(() => {
    let stopped = false;
    async function tick() {
      if (document.hidden) return;
      const active = await fetchServerActive();
      if (stopped) return;
      setServerActive(active);
      const id = activeIdRef.current;
      if (
        id &&
        !isStreaming(id) &&
        !active.includes(id) &&
        messagesRef.current.at(-1)?.role === 'user'
      ) {
        // The open chat's detached generation finished — fetch its answer.
        const conv = await getHistoryStore().load(id, { force: true });
        if (!stopped && conv && activeIdRef.current === id && !isStreaming(id)) {
          setMessages(conv.messages);
          refreshList();
        }
      }
    }
    void tick();
    const timer = window.setInterval(() => void tick(), 8000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [refreshList]);

  // H-01: an upload indicator belongs to the chat it was started in; leaving
  // it on screen in another conversation would describe nothing.
  //
  // Compared rather than cleared outright: the FIRST message of a new chat
  // creates the conversation inside the same send that starts the upload, so
  // an unconditional reset here fired on that brand-new id and wiped the
  // indicator a moment after it was set.
  useEffect(() => {
    setDatasetUpload((prev) =>
      prev && prev.conversationId === activeId ? prev : null,
    );
  }, [activeId]);

  const stopStreaming = useCallback(() => {
    stopStream(activeIdRef.current);
  }, []);

  const scrollToBottom = useCallback((smooth: boolean) => {
    const el = scrollRef.current;
    if (!el) return;
    const reduced = window.matchMedia(
      '(prefers-reduced-motion: reduce)',
    ).matches;
    el.scrollTo({
      top: el.scrollHeight,
      behavior: smooth && !reduced ? 'smooth' : 'auto',
    });
  }, []);

  // Auto-scroll while content grows, unless the user scrolled up (§9).
  useEffect(() => {
    if (atBottom) scrollToBottom(false);
  }, [thread, atBottom, scrollToBottom]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  }

  /** Debounced (300 ms) so typing doesn't re-render the meter per keystroke. */
  const handleDraftChange = useCallback((text: string) => {
    if (draftTimer.current !== null) window.clearTimeout(draftTimer.current);
    draftTimer.current = window.setTimeout(() => setDraft(text), 300);
  }, []);

  /**
   * Ask the server what a compaction would fold right now.
   *
   * Called only when the meter popover OPENS and after a compaction — never
   * per keystroke. Any failure is treated as "unknown" rather than "nothing":
   * an unreachable orchestrator must not disable a control that still works.
   */
  const refreshFoldable = useCallback(async () => {
    const id = activeIdRef.current;
    if (!id) {
      setFoldableTurns(null);
      return;
    }
    try {
      const res = await fetch(
        `/api/history/conversations/${encodeURIComponent(id)}/summary`,
        { cache: 'no-store' },
      );
      if (!res.ok) throw new Error(String(res.status));
      const counts = readFoldableCounts(await res.json());
      // The chat may have been switched while this was in flight.
      if (activeIdRef.current !== id) return;
      setFoldableTurns(counts ? counts.foldableTurns : null);
    } catch {
      if (activeIdRef.current === id) setFoldableTurns(null);
    }
  }, []);

  /** The meter popover opened or closed; only opening costs a request. */
  const handleMeterOpenChange = useCallback(
    (open: boolean) => {
      if (open) void refreshFoldable();
    },
    [refreshFoldable],
  );

  /**
   * "Compact now" from the meter popover.
   *
   * Two things this deliberately does NOT do any more:
   *
   * - It does not touch the context meter. The ring used to be forced to zero
   *   the instant this returned, on the assumption that the next request had
   *   to be smaller. It does not have to be, and measurably often is not — so
   *   the reading stayed a fiction until the following reply replaced it with
   *   a number that could be HIGHER than before the user pressed the button.
   *   The last server-measured usage now simply stands until the server sends
   *   another one.
   * - It does not believe `compacted: true` on its own. `requestCompact`
   *   confirms it against the summary endpoint first, because the server
   *   reports a fold as successful even when the summary it stored is empty.
   *
   * Duplicate presses are suppressed in `lib/compact` (per conversation), so
   * the guarantee holds even if a click gets past the disabled button.
   */
  const compactNow = useCallback(() => {
    const id = activeIdRef.current;
    if (!id || isCompacting(id)) return;
    setCompacting(true);
    void (async () => {
      try {
        const run = await requestCompact(
          id,
          messagesRef.current.filter((m) => m.content),
        );
        // null = another compaction for this chat was already in flight.
        // Nothing happened, so say nothing.
        if (!run) return;
        // The chat may have been switched while this was in flight; its
        // result belongs to the conversation that asked for it.
        if (activeIdRef.current !== id) return;
        setFoldableTurns(run.foldableTurns);
        toast(run.outcome.message, run.outcome.tone);
        // The lasting popover line, and the way into the summary, appear only
        // for a compaction whose summary was actually seen.
        setLastFoldedTurns(
          run.outcome.kind === 'compacted' ? run.outcome.foldedTurns : null,
        );
      } finally {
        setCompacting(false);
      }
    })();
  }, [toast]);

  /* --------------------------------- Salesforce Intelligence Mode */

  // The question this thread is waiting on, read from the LAST assistant
  // message. Deriving it from the thread rather than holding it in its own
  // state is what makes it survive a reload for free: the message comes back
  // from history with `meta.clarification` on it, and the card rebuilds.
  const pending = pendingClarification(thread);
  const pendingRef = useRef<ClarificationRequest | null>(null);
  pendingRef.current = pending;

  // Set by "Something else": the NEXT composer submit is the answer to this
  // question rather than a new message.
  const [customAnswerFor, setCustomAnswerFor] =
    useState<ClarificationRequest | null>(null);
  const customAnswerRef = useRef<ClarificationRequest | null>(null);
  customAnswerRef.current = customAnswerFor;

  // A pending question belongs to ONE conversation. Without this, opening
  // another chat left the composer waiting to answer a question that is not on
  // screen — and the next thing typed there would resume the wrong intent.
  useEffect(() => {
    setCustomAnswerFor(null);
    setSubmittingClarificationId(null);
    // An open editor belongs to a turn in the chat being left behind, and its
    // id means nothing in the next one. Neither does a branch selection.
    setEditingMessageId(null);
    setBranchSelection({});
  }, [activeId]);

  // Starter card + server-side pending state. Loaded when the chat changes and
  // when Salesforce is switched on — the options are filtered server-side to
  // what this connection can actually query, so they cannot be computed here.
  useEffect(() => {
    if (!activeId || !prefs.salesforce) {
      setStarterOptions([]);
      return;
    }
    let cancelled = false;
    void fetchSalesforceContext(activeId).then((context) => {
      if (!cancelled) setStarterOptions(context.options);
    });
    return () => {
      cancelled = true;
    };
  }, [activeId, prefs.salesforce]);

  const updatePrefs = useCallback(
    (next: ChatPrefs) => {
      const id = activeIdRef.current;
      // Switching the source OFF with a question on screen must cancel it
      // SERVER-side. The card disappearing is not what ends the question: the
      // orchestrator would still be waiting, and the next Salesforce message
      // here would be read as an answer to something the user dismissed.
      if (prefsRef.current.salesforce && !next.salesforce && id && pendingRef.current) {
        setCustomAnswerFor(null);
        void cancelClarification(id);
      }
      setPrefs(next);
      savePrefs(window.localStorage, id, next);
    },
    [],
  );

  // Deep Research is a ONE-SHOT command, not a mode: it applies to the send
  // that armed it and then disarms. Without this the pill stayed lit and the
  // user's next ordinary question silently became another multi-minute
  // report (review, 2026-08-30). prefs.deepResearch is also dropped on load,
  // so a reload cannot resurrect it either.
  const disarmDeepResearch = useCallback(() => {
    if (!prefsRef.current.deepResearch) return;
    const next = { ...prefsRef.current, deepResearch: false };
    setPrefs(next);
    savePrefs(window.localStorage, activeIdRef.current, next);
  }, []);

  const send = useCallback(
    (
      text: string,
      attachments: Attachment[],
      pasted: PastedText[],
      clarification?: ClarificationResponse | null,
    ) => {
      // Up to 5 images OR exactly one PDF/dataset (2026-08-05) — the
      // Composer enforces the shape; `first` covers the exclusive kinds.
      const first = attachments[0] ?? null;
      const isPdf = first?.kind === 'pdf';
      const isDataset = first?.kind === 'dataset';
      const images = attachments.filter((a) => a.kind === 'image');
      let conversationId = activeId;
      if (!conversationId) {
        const title =
          text || (pasted.length ? 'Pasted text' : '') || first?.name || '';
        const conv = getHistoryStore().create(title);
        conversationId = conv.id;
        setActiveId(conv.id);
        activeIdRef.current = conv.id;
        // The draft prefs become this conversation's prefs (V2 §4c).
        setPrefs(adoptDraftPrefs(window.localStorage, conv.id));
        refreshList();
      }
      setUrlConversation(conversationId);
      // A send continues the path ON SCREEN, so a follow-up asked while an
      // older version is selected belongs to that version's branch — not to
      // whichever branch happens to be newest.
      const userBranch = branchForAppend(
        messagesRef.current,
        threadRef.current,
      );
      const userMessage: ChatMessage = {
        id: newId(),
        role: 'user',
        content: text,
        imageDataUrl: images[0]?.dataUrl,
        imageDataUrls:
          images.length > 1 ? images.map((i) => i.dataUrl) : undefined,
        // V8: a PDF attachment shows a chip (filename) in the bubble.
        pdfName: isPdf || isDataset ? first?.name : undefined,
        // V5: pasted blocks ride on meta so they round-trip through server
        // history and are folded into the model input at request time.
        // 2026-08-21: attachments ride the same way, so the file card can be
        // rendered by any browser from server history — pdfName alone never
        // left this browser's cache.
        meta: metaWithBranch(
          pasted.length || isPdf || isDataset
            ? {
                route: 'chat',
                ...(pasted.length ? { pasted } : {}),
                ...(isPdf || isDataset
                  ? {
                      attachments: [
                        {
                          name: first?.name ?? 'file',
                          kind: isDataset
                            ? ('dataset' as const)
                            : ('pdf' as const),
                        },
                      ],
                    }
                  : {}),
              }
            : undefined,
          userBranch,
        ),
        createdAt: Date.now(),
      };
      // Keep the payloads in memory so regenerate/retry re-send the same
      // question WITH its attachments (never persisted — see lib/attachments).
      if (!isDataset) {
        rememberAttachments(
          userMessage.id,
          isPdf && first?.base64
            ? [{ kind: 'pdf', name: first.name, base64: first.base64 }]
            : images
                .filter((i) => i.base64)
                .map((i) => ({
                  kind: 'image' as const,
                  name: i.name,
                  base64: i.base64,
                })),
        );
      }
      // `turns` is everything STORED (sibling branches included); `context`
      // is the single path the model is sent.
      const turns = [...messagesRef.current, userMessage];
      const context = [...threadRef.current, userMessage];
      const answerBranch = branchForAppend(turns, context);
      persist(conversationId, turns);
      // H-01: the turn goes on screen HERE, not as a side effect of opening a
      // stream. `persist` only writes the store and the sidebar, so a dataset
      // send — which cannot start its stream until the upload finishes — used
      // to clear the composer and then show nothing at all, for as long as a
      // 200 MB file took. Rendering is not generation's job.
      setMessages(turns);
      setAtBottom(true);
      setUnreachable(false);
      // A dataset has to be uploaded before a stream can exist, so say that
      // instead of claiming the model is already running (see datasetUpload).
      if (isDataset && first?.file) {
        setDatasetUpload({
          conversationId,
          messageId: userMessage.id,
          status: 'uploading',
        });
      } else {
        setStreaming(true);
      }
      // Sending moves the conversation on; an editor left open behind it is
      // about to be arguing with a thread that has changed underneath it.
      setEditingMessageId(null);

      if (isDataset && first?.file) {
        // Datasets stream to their own endpoint and are then referenced by the
        // conversation, so the chat request itself stays small.
        void (async () => {
          try {
            const form = new FormData();
            form.append('file', first.file as File);
            form.append('conversation_id', conversationId);
            const res = await fetch('/api/upload', { method: 'POST', body: form });
            const body = (await res.json()) as {
              detail?: string;
              files?: number;
              upload_id?: string;
            };
            if (!res.ok) throw new Error(body.detail ?? 'upload failed');
            // Link the turn to the server's durable uploads row, so the
            // persisted message names the exact attachment it was asked about.
            if (body.upload_id && userMessage.meta?.attachments?.[0]) {
              userMessage.meta.attachments[0].id = body.upload_id;
              persist(conversationId, turns);
            }
            toast(
              `Profiled ${body.files ?? 0} file${body.files === 1 ? '' : 's'} from ${first.name}.`,
            );
            // H-01: the upload is done; the stream opened below owns the
            // "busy" story from here.
            setDatasetUpload(null);
          } catch (err) {
            toast(
              err instanceof Error ? err.message : 'That dataset could not be read.',
              'error',
            );
            // The dataset never made it in, so generating would answer from a
            // context that doesn't exist. H-01: the turn STAYS on screen with
            // its prompt and its file, now marked failed — the user's words
            // must not vanish because a request did.
            if (conversationId === activeIdRef.current) {
              setDatasetUpload({
                conversationId,
                messageId: userMessage.id,
                status: 'failed',
              });
            }
            // Un-persist the attachment metadata: other devices must not see
            // a card for a dataset the server never accepted. (The local
            // pdfName chip stays, next to the error toast, as before.)
            if (userMessage.meta?.attachments) {
              delete userMessage.meta.attachments;
              persist(conversationId, turns);
            }
            return;
          }
          void startStream({
            conversationId,
            turns,
            context,
            assistantBranch: answerBranch,
            prefs: prefsRef.current,
          });
          disarmDeepResearch();
        })();
        return;
      }

      void startStream({
        conversationId,
        turns,
        context,
        assistantBranch: answerBranch,
        prefs: prefsRef.current,
        images: isPdf ? null : images.map((i) => i.base64).filter(Boolean),
        pdf: isPdf ? first?.base64 ?? null : null,
        pdfName: isPdf ? first?.name ?? null : null,
        clarification: clarification ?? null,
      });
      disarmDeepResearch();
    },
    [activeId, disarmDeepResearch, persist, refreshList, setUrlConversation, toast],
  );

  /**
   * Answer the pending question from the card.
   *
   * The chosen label is appended as a normal user turn so the transcript reads
   * as a conversation, while the structured response rides alongside it — that
   * is what lets the server resume the ORIGINAL request instead of treating
   * "This quarter" as a question of its own.
   */
  const answerClarification = useCallback(
    (response: ClarificationResponse, summary: string) => {
      if (clarificationAlreadySubmitted(response.client_message_id)) return;
      markClarificationSubmitted(response.client_message_id);
      setSubmittingClarificationId(response.clarification_id);
      setCustomAnswerFor(null);
      send(summary, [], [], response);
    },
    [send],
  );

  /**
   * Answer in your own words instead: arm the composer and focus it.
   *
   * `seed` is the character that triggered the hand-over when the user simply
   * started typing over the panel. The panel takes focus so the number keys
   * work the moment a question appears; carrying the keystroke here is what
   * stops that from eating the first letter of a typed answer.
   */
  const answerInComposer = useCallback(
    (request: ClarificationRequest, seed?: string) => {
      setCustomAnswerFor(request);
      composerRef.current?.insert(seed);
    },
    [],
  );

  /**
   * Skip: answer with "no preference" so the server states its assumption.
   *
   * Not a dismissal. The question lives on the server and only one may be
   * pending per conversation, so a panel that merely disappeared would leave it
   * open and block every later question in this chat.
   */
  const skipClarification = useCallback(
    (request: ClarificationRequest) => {
      const response = buildResponse(request, { skipped: true });
      if (response) {
        answerClarification(response, 'No preference — use your best judgement.');
      }
    },
    [answerClarification],
  );

  /**
   * A composer submit. When "Something else" armed a question, this text IS the
   * answer and carries its clarification_id; otherwise it is an ordinary
   * message — and the SERVER still decides whether it happens to answer a
   * pending question, because a user who ignores the card and just types
   * "last 90 days" means exactly that.
   */
  const sendFromComposer = useCallback(
    (text: string, attachments: Attachment[], pasted: PastedText[]) => {
      const armed = customAnswerRef.current;
      if (armed && text.trim() && attachments.length === 0) {
        const response = buildResponse(armed, { customText: text });
        if (response && !clarificationAlreadySubmitted(response.client_message_id)) {
          markClarificationSubmitted(response.client_message_id);
          setSubmittingClarificationId(response.clarification_id);
          setCustomAnswerFor(null);
          send(text, [], pasted, response);
          return;
        }
      }
      send(text, attachments, pasted);
    },
    [send],
  );

  /** Re-run the turn that produced the assistant message at `messageId`. */
  const runRegenerate = useCallback(
    async (messageId: string) => {
      const id = activeIdRef.current;
      if (!id || isStreaming(id)) return;
      const all = messagesRef.current;
      // Located in the VISIBLE path: "the answer above this one" means the
      // one on screen, not whichever message happens to sit there in storage
      // once a conversation has more than one branch.
      const view = threadRef.current;
      const idx = view.findIndex((m) => m.id === messageId);
      if (idx === -1) return;
      let userIdx = idx - 1;
      while (userIdx >= 0 && view[userIdx].role !== 'user') userIdx--;
      if (userIdx < 0) return;
      const context = view.slice(0, userIdx + 1);

      // Re-send the SAME question, attachments included. Without this the
      // model was re-asked "what's in this invoice?" with no invoice attached.
      const { attachments, missing } = attachmentsForResend(view[userIdx]);
      if (missing) {
        toast(
          'Re-attach the file to regenerate this answer — its contents are no longer in memory.',
          'error',
        );
        return;
      }
      const resendImages = attachments
        .filter((a) => a.kind === 'image')
        .map((a) => a.base64);
      const resendPdf = attachments.find((a) => a.kind === 'pdf') ?? null;

      // In a conversation that has versions, truncating would delete the
      // OTHER branches too — they live in the same flat list. So the retry is
      // appended as a newer answer to the same question instead, and the one
      // it supersedes stays reachable. A conversation that has never been
      // edited has no branches to protect and keeps the original behaviour
      // exactly: confirmed truncate, then re-stream.
      let turns = context;
      if (hasBranches(all)) {
        turns = all;
      } else if (all.length > context.length + 1) {
        // The sync path cannot shrink a thread (that guard is what stops a
        // stale cache from destroying history), so this intentional shrink
        // goes through the dedicated truncate endpoint — reached ONLY from
        // here, and only after the user confirmed.
        try {
          await getHistoryStore().truncateMessages(id, context.length);
        } catch (err) {
          const { message, reload } = truncateFailure(err);
          toast(message, 'error');
          if (reload) {
            const conv = await getHistoryStore()
              .load(id, { force: true })
              .catch(() => null);
            if (conv && activeIdRef.current === id) setMessages(conv.messages);
          }
          return;
        }
        setMessages(context);
      }

      setStreaming(true);
      setEditingMessageId(null);
      void startStream({
        conversationId: id,
        turns,
        context,
        assistantBranch: branchForAppend(turns, context),
        prefs: prefsRef.current,
        images: resendImages,
        pdf: resendPdf?.base64 ?? null,
        pdfName: resendPdf?.name ?? null,
      });
    },
    [toast],
  );

  /**
   * "Edit" on one of the user's own messages: open the in-place editor.
   *
   * This used to hand the text to the COMPOSER instead, which put the prompt
   * at the bottom of the screen — far from the message it belonged to, on top
   * of whatever was already typed there, and re-openable so many times that
   * the box filled with copies of the same sentence. The rewrite now happens
   * where the message is.
   */
  const startEdit = useCallback((messageId: string) => {
    // Editing rewrites the thread from that turn, which cannot be done to a
    // thread that is still being written. The pencil simply does nothing.
    if (isStreaming(activeIdRef.current)) return;
    setEditingMessageId(messageId);
  }, []);

  const cancelEdit = useCallback(() => setEditingMessageId(null), []);

  /**
   * Switch which version of a turn is live.
   *
   * Pure view selection: no request, no generation, no history change. Every
   * version is already loaded — the arrows only choose which path down the
   * tree is rendered and, from then on, which one a follow-up continues.
   */
  const selectBranch = useCallback((parent: string, id: string) => {
    setBranchSelection((prev) =>
      selectVersion(messagesRef.current, prev, parent, id),
    );
  }, []);

  /**
   * Commit an edit: add the rewrite as a NEW VERSION beside the original.
   *
   * Nothing is deleted. The previous implementation truncated the original
   * turn, its answer and every turn after it through the server's truncate
   * endpoint, so asking a question a second way destroyed the first answer.
   *
   * The version is a sibling of the original — same parent, appended to the
   * end of the stored list — which is what makes both readable and why the
   * original needs nothing written to it. `< 1 / 2 >` under the message picks
   * which one is live, and the answer is generated for THIS version only.
   */
  const runEdit = useCallback(
    async (messageId: string, text: string) => {
      const id = activeIdRef.current;
      if (!id || isStreaming(id)) return;
      const all = messagesRef.current;
      const original = all.find((m) => m.id === messageId);
      if (!original || original.role !== 'user') return;

      // Re-ask the question WITH whatever was attached to it. Images survive
      // as their own previews; a PDF's bytes do not outlive a reload and a
      // dataset only ever lived server-side, so both report `missing` and the
      // edit stops rather than silently re-asking with nothing attached.
      const { attachments, missing } = attachmentsForResend(original);
      if (missing) {
        toast(
          'Re-attach the file to edit this message — its contents are no longer in memory.',
          'error',
        );
        return;
      }

      const version = branchForVersion(all, original);
      const edited: ChatMessage = {
        id: newId(),
        role: 'user',
        content: text,
        // Attachments and pasted blocks belong to the TURN, not to its
        // wording, so this version inherits them. `serverId` and `feedback`
        // deliberately do not: they identify the original's stored row, which
        // is still there and still its own message.
        imageDataUrl: original.imageDataUrl,
        imageDataUrls: original.imageDataUrls,
        pdfName: original.pdfName,
        meta: metaWithBranch(original.meta, version),
        createdAt: Date.now(),
      };

      // Stored: everything that was there, plus the new version. Sent to the
      // model: the turns BEFORE the edited one on screen, then the edit —
      // never the version it is an alternative to.
      const turns = [...all, edited];
      const at = threadRef.current.findIndex((m) => m.id === messageId);
      const context = [
        ...(at === -1 ? [] : threadRef.current.slice(0, at)),
        edited,
      ];
      const answerBranch = branchForAppend(turns, context);

      setEditingMessageId(null);
      rememberAttachments(edited.id, attachments);
      persist(id, turns);
      setMessages(turns);
      // Show the version just written, the way ChatGPT lands you on 2 / 2.
      setBranchSelection((prev) =>
        selectVersion(turns, prev, version.parent ?? ROOT, version.self),
      );
      setAtBottom(true);
      setUnreachable(false);
      setStreaming(true);
      const editedPdf = attachments.find((a) => a.kind === 'pdf') ?? null;
      void startStream({
        conversationId: id,
        turns,
        context,
        assistantBranch: answerBranch,
        prefs: prefsRef.current,
        images: attachments
          .filter((a) => a.kind === 'image')
          .map((a) => a.base64),
        pdf: editedPdf?.base64 ?? null,
        pdfName: editedPdf?.name ?? null,
      });
    },
    [persist, toast],
  );

  /**
   * Send from the editor.
   *
   * There is nothing to confirm any more: an edit adds a version and removes
   * nothing, so the dialog that used to warn about discarded turns would be
   * describing something that no longer happens.
   */
  const submitEdit = useCallback(
    (messageId: string, text: string) => {
      const all = messagesRef.current;
      const original = all.find((m) => m.id === messageId);
      if (!original || original.role !== 'user') return;
      const next = text.trim();
      // Unchanged text is not an edit — a second identical version would add
      // a navigator with nothing to navigate between. Re-asking as-is is what
      // Regenerate is for.
      if (!next || next === (original.content ?? '').trim()) {
        setEditingMessageId(null);
        return;
      }
      void runEdit(messageId, next);
    },
    [runEdit],
  );

  /**
   * Regenerating an OLDER answer restarts the thread from that point and
   * discards every later turn. That is destructive and irreversible, so it
   * asks first; regenerating the last answer runs straight away.
   */
  const regenerate = useCallback(
    (messageId: string) => {
      // Once a conversation has versions, a retry adds an answer beside the
      // old one and removes nothing — so there is nothing to warn about.
      if (hasBranches(messagesRef.current)) {
        void runRegenerate(messageId);
        return;
      }
      const discarded = messagesDiscardedByRegenerate(
        threadRef.current,
        messageId,
      );
      if (discarded > 0) {
        setPendingRegenerate({ messageId, discarded });
        return;
      }
      void runRegenerate(messageId);
    },
    [runRegenerate],
  );

  /**
   * The fatal request-level failure currently on screen, if any.
   *
   * `unreachable` is the gate — every send that never became a stream sets it
   * (lib/streams.ts markUnreachable) and every fresh action clears it, so it
   * already tracks exactly "a request failed and the user has not moved on".
   * The STATUS and category come off the failed message, which is persisted,
   * so the page survives a reload and a trip to another chat and back.
   */
  const fatalError = useMemo<ClientError | null>(() => {
    if (!unreachable) return null;
    for (let i = thread.length - 1; i >= 0; i -= 1) {
      const m = thread[i];
      if (m.role === 'assistant' && m.status === 'error' && m.errorCode) {
        return toClientError(m.errorStatus ?? null, m.errorCode);
      }
    }
    // The stream reported a failure but left no classified message — treat it
    // as what it certainly was: a request that never reached a response.
    return toClientError(null, 'NETWORK_ERROR');
  }, [unreachable, thread]);

  /** Retry: re-send the last user turn, attachment included. */
  const retryLastTurn = useCallback(() => {
    const id = activeIdRef.current;
    if (!id || isStreaming(id)) return;
    const all = messagesRef.current;
    const view = threadRef.current;
    let userIdx = view.length - 1;
    while (userIdx >= 0 && view[userIdx].role !== 'user') userIdx--;
    if (userIdx < 0) {
      setUnreachable(false);
      return;
    }
    const { attachments, missing } = attachmentsForResend(view[userIdx]);
    if (missing) {
      toast(
        'Re-attach the file to retry this message — its contents are no longer in memory.',
        'error',
      );
      return;
    }
    const retryPdf = attachments.find((a) => a.kind === 'pdf') ?? null;
    setUnreachable(false);
    setStreaming(true);
    const context = view.slice(0, userIdx + 1);
    void startStream({
      conversationId: id,
      // Retrying must not drop the other branches from storage, so a branched
      // conversation keeps its whole list and the answer is appended.
      turns: hasBranches(all) ? all : context,
      context,
      assistantBranch: branchForAppend(hasBranches(all) ? all : context, context),
      prefs: prefsRef.current,
      images: attachments
        .filter((a) => a.kind === 'image')
        .map((a) => a.base64),
      pdf: retryPdf?.base64 ?? null,
      pdfName: retryPdf?.name ?? null,
    });
  }, [toast]);

  // Leaving a chat NEVER stops its generation (ChatGPT behavior): it keeps
  // streaming in the background with a spinner on its sidebar row, and its
  // answer is saved to history when it finishes.
  const newChat = useCallback(() => {
    setSearchOpen(false);
    setActiveId(null);
    activeIdRef.current = null;
    setMessages([]);
    setFoldableTurns(null);
    setLastFoldedTurns(null);
    setUnreachable(false);
    setStreaming(false);
    setPrefs(DEFAULT_PREFS);
    savePrefs(window.localStorage, null, DEFAULT_PREFS);
    setUrlConversation(null);
    composerRef.current?.focus();
  }, [setUrlConversation]);

  const selectConversation = useCallback(
    (id: string) => {
      const store = getHistoryStore();
      setActiveId(id);
      activeIdRef.current = id;
      setPrefs(loadPrefs(window.localStorage, id));
      setUnreachable(false);
      setAtBottom(true);
      // Both belong to the chat being left, not the one being opened.
      setFoldableTurns(null);
      setLastFoldedTurns(null);
      setUrlConversation(id);

      const live = getLiveStream(id);
      if (live) {
        // This chat is generating in the background — adopt the live thread.
        setMessages([...live.messages]);
        setStreaming(live.status === 'streaming');
      } else {
        const cached = store.get(id);
        if (cached) setMessages(cached.messages);
        setStreaming(false);
        if (serverActiveRef.current.includes(id)) {
          // Still generating server-side (started before a reload) — re-join.
          setStreaming(true);
          void attachStream(id).then((ok) => {
            if (!ok && activeIdRef.current === id) {
              setStreaming(isStreaming(id));
              void store.load(id, { force: true }).then((conv) => {
                if (conv && activeIdRef.current === id && !isStreaming(id)) {
                  setMessages(conv.messages);
                }
              });
            }
          });
        } else {
          // Server truth may be newer / not cached yet (V2 §4b); force a
          // refetch when the chat ends on a user message — a detached
          // generation may have saved its answer while we were away.
          const force = cached?.messages.at(-1)?.role === 'user';
          void store.load(id, { force }).then((conv) => {
            if (conv && activeIdRef.current === id && !isStreaming(id)) {
              setMessages(conv.messages);
            }
          });
        }
      }
      if (window.matchMedia('(max-width: 767px)').matches) {
        setSidebarOpen(false);
      }
    },
    [setUrlConversation],
  );

  const renameConversation = useCallback(
    (id: string, title: string) => {
      getHistoryStore().rename(id, title);
      refreshList();
    },
    [refreshList],
  );

  const deleteConversation = useCallback(
    (id: string) => {
      stopStream(id); // a deleted chat must not keep generating
      getHistoryStore().remove(id);
      removePrefs(window.localStorage, id);
      refreshList();
      if (activeIdRef.current === id) {
        setActiveId(null);
        activeIdRef.current = null;
        setMessages([]);
        setUrlConversation(null);
      }
      toast('Conversation deleted.');
    },
    [refreshList, setUrlConversation, toast],
  );

  /* ------------------------------------------------ V3 §2: row menu */

  const pinConversation = useCallback(
    (id: string, pinned: boolean) => {
      getHistoryStore().setPinned(id, pinned);
      refreshList();
      toast(pinned ? 'Conversation pinned.' : 'Conversation unpinned.');
    },
    [refreshList, toast],
  );

  const archiveConversation = useCallback(
    (id: string, archive: boolean) => {
      getHistoryStore().setArchived(id, archive);
      refreshList();
      // An archived chat leaves Recents; if it was the open one, land on a
      // fresh chat rather than showing a thread that is no longer listed.
      if (archive && activeIdRef.current === id) newChat();
      toast(archive ? 'Conversation archived.' : 'Conversation unarchived.');
    },
    [newChat, refreshList, toast],
  );

  const loadArchived = useCallback(() => {
    void getHistoryStore()
      .refreshArchived()
      .then(() => refreshList());
  }, [refreshList]);

  const exportConversation = useCallback(
    (id: string) => {
      void (async () => {
        const file = await getHistoryStore().exportMarkdown(id);
        if (!file) {
          toast('That conversation could not be exported.', 'error');
          return;
        }
        downloadMarkdown(file);
        toast(`Downloaded ${file.filename}`);
      })();
    },
    [toast],
  );

  // Keyboard shortcuts (§9 + V4 §2). The map itself is pure and unit-tested
  // in lib/searchPalette.ts; this only supplies the live context and runs the
  // action it names.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null;
      const action = shortcutAction(e, {
        paletteOpen: searchOpen,
        streaming: isStreaming(activeIdRef.current),
        typing:
          target?.tagName === 'INPUT' ||
          target?.tagName === 'TEXTAREA' ||
          target?.isContentEditable === true,
      });
      if (!action) return;
      e.preventDefault();
      switch (action) {
        case 'open-search':
          setSearchOpen(true);
          return;
        case 'close-palette':
          setSearchOpen(false);
          return;
        case 'new-chat':
          newChat();
          return;
        case 'stop-streaming':
          stopStreaming();
          return;
        case 'focus-composer':
          composerRef.current?.focus();
      }
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [newChat, searchOpen, stopStreaming]);

  /** Which visible turns have alternatives — one walk, not one per row. */
  const versions = useMemo(() => versionMap(messages), [messages]);

  const activeTitle =
    conversations.find((c) => c.id === activeId)?.title ?? 'New chat';
  const lastEngine: Engine | undefined = [...thread]
    .reverse()
    .find((m) => m.role === 'assistant' && m.meta?.route)?.meta?.route;

  // Chats generating right now — in this tab OR server-side (after reload).
  const busyIds = Array.from(new Set([...streamingIds(), ...serverActive]));

  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        conversations={conversations}
        archived={archived}
        activeId={activeId}
        streamingIds={busyIds}
        onNewChat={newChat}
        onOpenSearch={() => setSearchOpen(true)}
        onSelect={selectConversation}
        onRename={renameConversation}
        onDelete={deleteConversation}
        onSetPinned={pinConversation}
        onSetArchived={archiveConversation}
        onExport={exportConversation}
        onLoadArchived={loadArchived}
      />

      <SummaryPanel
        conversationId={activeId}
        open={summaryOpen}
        onClose={() => setSummaryOpen(false)}
      />

      <ConfirmDialog
        open={pendingRegenerate !== null}
        title="Regenerate this response?"
        body={`This will delete all messages after this point (${
          pendingRegenerate?.discarded ?? 0
        } message${pendingRegenerate?.discarded === 1 ? '' : 's'}).`}
        confirmLabel="Regenerate"
        onConfirm={() => {
          const target = pendingRegenerate;
          setPendingRegenerate(null);
          if (target) void runRegenerate(target.messageId);
        }}
        onCancel={() => setPendingRegenerate(null)}
      />

      {/* Portals to <body> — see the note in SearchPalette.tsx. */}
      <SearchPalette
        open={searchOpen}
        onClose={() => setSearchOpen(false)}
        recents={conversations}
        onSelect={selectConversation}
        onNewChat={newChat}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        {/* ChatGPT-parity header: no app name, no chat title. The sidebar owns
            its own collapse button, so this one only appears once the sidebar
            is hidden — it is the only way back. The title stays as sr-only
            text so screen readers still announce which chat is open. */}
        <header className="flex h-[52px] shrink-0 items-center gap-2 px-3">
          {!sidebarOpen && (
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              aria-label="Show sidebar"
              aria-expanded={false}
              title="Show sidebar"
              className="rounded-lg p-2 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
            >
              <IconSidebar size={17} />
            </button>
          )}
          <h1 className="sr-only">{activeId ? activeTitle : APP_NAME}</h1>
          {lastEngine && (
            <span className="ml-auto">
              <EngineBadge engine={lastEngine} size="xs" />
            </span>
          )}
        </header>

        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="relative min-h-0 flex-1 overflow-y-auto"
        >
          {fatalError ? (
            <ChatErrorPage
              error={fatalError}
              onRetry={retryLastTurn}
              // Dismiss the page only. The conversation, the failed user
              // message and its error all stay exactly where they are, and
              // nothing is re-sent.
              onReturn={() => setUnreachable(false)}
            />
          ) : thread.length === 0 ? (
            <EmptyState />
          ) : (
            <div className="mx-auto w-full max-w-thread space-y-6 px-4 py-6">
              {thread.map((m, i) => {
                // Only the question the thread is WAITING on is a live control;
                // every earlier card is a record of a decision already made.
                const card = cardState(thread, i);
                return (
                <MessageRow
                  key={m.id}
                  message={m}
                  isLast={i === thread.length - 1 && m.role === 'assistant'}
                  onRegenerate={() => regenerate(m.id)}
                  onRetry={() => regenerate(m.id)}
                  uploadStatus={
                    datasetUpload?.messageId === m.id
                      ? datasetUpload.status
                      : null
                  }
                  versions={versions.get(m.id) ?? null}
                  onSelectVersion={selectBranch}
                  onEditStart={() => startEdit(m.id)}
                  editing={editingMessageId === m.id}
                  onEditCancel={cancelEdit}
                  onEditSubmit={(text) => submitEdit(m.id, text)}
                  onShowSummary={() => setSummaryOpen(true)}
                  clarificationPending={card.pending}
                  clarificationAnswer={card.answeredWith}
                  onFeedback={(feedback) => {
                    if (!activeId) return;
                    // Fire-and-forget: the store updates its cache first and
                    // swallows a failed request, so a thumb never blocks the
                    // UI or raises an error pill.
                    void getHistoryStore().setMessageFeedback(
                      activeId,
                      m.id,
                      feedback,
                    );
                  }}
                />
                );
              })}
            </div>
          )}
        </div>

        {!atBottom && thread.length > 0 && (
          <div className="pointer-events-none relative">
            <button
              type="button"
              onClick={() => scrollToBottom(true)}
              className="pointer-events-auto absolute -top-12 left-1/2 z-10 inline-flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-surface px-3.5 py-1.5 text-xs font-medium shadow-lg transition-colors duration-ts hover:bg-surface-2"
            >
              Jump to latest
              <IconArrowDown size={13} />
            </button>
          </div>
        )}

        <Composer
          ref={composerRef}
          streaming={streaming}
          disabled={reconciling}
          // H-01: a dataset upload blocks a second send without pretending a
          // generation is running — that is what `streaming` would claim.
          busy={datasetUpload?.status === 'uploading'}
          onDraftChange={handleDraftChange}
          meter={
            <ContextMeter
              // The last reading the SERVER measured, plus the live draft —
              // never adjusted by anything the browser assumes a compaction
              // saved. If the value is stale it is stale honestly; a made-up
              // smaller number is worse than an old true one.
              view={meterView(latestUsage(thread), draft)}
              compacting={compacting}
              onCompactNow={compactNow}
              compactDisabled={!activeId || streaming}
              foldableTurns={foldableTurns}
              lastFoldedTurns={lastFoldedTurns}
              onOpenChange={handleMeterOpenChange}
              onSeeSummary={() => setSummaryOpen(true)}
            />
          }
          prefs={prefs}
          onPrefsChange={updatePrefs}
          onSend={sendFromComposer}
          onStop={stopStreaming}
          clarificationPlaceholder={
            customAnswerFor?.custom_placeholder ??
            (pending ? pending.custom_placeholder : undefined)
          }
          clarification={
            // The LIVE question, and only while it is live. It renders inside
            // the composer's own container rather than in the transcript: it
            // is a temporary control, not a message, so it must stay at the
            // bottom of a conversation of any length, must not scroll away
            // while it is being answered, and must leave nothing behind.
            pending ? (
              <ClarificationCard
                request={pending}
                submitting={
                  submittingClarificationId === pending.clarification_id
                }
                onSubmit={answerClarification}
                onUseComposer={(seed) => answerInComposer(pending, seed)}
                onSkip={() => skipClarification(pending)}
              />
            ) : null
          }
          starter={
            shouldShowStarter({
              salesforceEnabled: prefs.salesforce,
              messageCount: thread.length,
              streaming,
              hasPendingClarification: Boolean(pending),
              optionCount: starterOptions.length,
            }) ? (
              <SalesforceStarterCard
                options={starterOptions}
                onPick={(prompt) => void send(prompt, [], [])}
                onUseComposer={() => composerRef.current?.focus()}
              />
            ) : null
          }
        />
      </div>
    </div>
  );
}
