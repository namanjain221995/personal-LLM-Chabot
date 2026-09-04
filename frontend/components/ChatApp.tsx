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
import { fetchMe, handleSessionEnd, userScopeKey } from '@/lib/auth';
import { toClientError, type ClientError } from '@/lib/errorTypes';
import { downloadMarkdown } from '@/lib/exportMarkdown';
import {
  getHistoryStore,
  newId,
  rebuildHistoryStore,
  setEvictListener,
} from '@/lib/history';
import {
  adoptDraftPrefs,
  DEFAULT_PREFS,
  loadPrefs,
  removePrefs,
  savePrefs,
  type ChatPrefs,
} from '@/lib/prefs';
import { prefsForFeatures } from '@/lib/composerMenu';
import {
  attachmentsForResend,
  resendOptionsFor,
  carryAttachmentFiles,
  dragHasFiles,
  dropIntent,
  dragHasInternalAttachment,
  previewMimeFor,
  rememberAttachments,
  rememberAttachmentFiles,
  resolveAttachmentAsync,
  uploadRefFor,
} from '@/lib/attachments';
import { uploadDocumentFile } from '@/lib/uploadDocument';
import type { SendOptions } from './Composer';
import {
  branchForAppend,
  branchForVersion,
  hasBranches,
  metaWithBranch,
  ROOT,
  threadIndices,
  treeShape,
  versionMap,
  selectVersion,
  type BranchSelection,
} from '@/lib/branching';
import type { MessageFeedback } from '@/lib/feedback';
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
  SelectedContext,
} from '@/lib/types';
import type { SelectionCandidate } from '@/lib/selectedContext';
import { Composer, type Attachment, type ComposerHandle } from './Composer';
import { SelectionAsk } from './SelectionAsk';
import { SalesforceStarterCard } from './SalesforceStarterCard';
import { ConfirmDialog } from './ConfirmDialog';
import { ContextMeter } from './ContextMeter';
import { SummaryPanel } from './SummaryPanel';
import { EmptyState } from './EmptyState';
import { Loader } from './Loader';
import { MessageRow, type UploadStatus } from './MessageRow';
import { ClarificationCard } from './ClarificationCard';
import { SearchPalette } from './SearchPalette';
import { Sidebar } from './Sidebar';
import { useToast } from './Providers';
import { IconArrowDown, IconShare, IconSidebar } from './icons';
import { ShareDialog } from './ShareDialog';

const APP_NAME =
  process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

/** The row callbacks ChatApp caches per message id — see `rowHandlers`. */
interface RowHandlers {
  onRegenerate: () => void;
  onRetry: () => void;
  onReuseAttachment: (index: number) => void;
  onEditStart: () => void;
  onEditCancel: () => void;
  onEditSubmit: (text: string) => void;
  onShowSummary: () => void;
  onFeedback: (feedback: MessageFeedback | null) => void;
}

export function ChatApp() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [archived, setArchived] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  /**
   * The conversation whose history is in flight with NOTHING yet to show.
   *
   * Selecting a chat this browser has never opened used to leave the previous
   * chat's messages on screen under the new chat's id, title and URL — the
   * cache seeds a server-listed conversation as `messages: []`, and the old
   * code only called setMessages when a cache entry existed. Where it did
   * fire, the empty array rendered EmptyState, so a chat with history claimed
   * to be a brand-new one.
   *
   * One id rather than a Set, deliberately: selecting anything overwrites it,
   * so a load that resolves for a conversation the user has already left
   * cannot revive a stale flag. Every write below is identity-guarded for the
   * same reason.
   */
  const [loadingId, setLoadingId] = useState<string | null>(null);
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
  /**
   * Which tools this account may use (orchestrator authn/features.py),
   * resolved by the boot probe. Empty until it lands, which `featureOn`
   * reads as "allowed" — the composer must not flicker its menu shorter for
   * a second on every load.
   */
  const [features, setFeatures] = useState<Record<string, boolean>>({});
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

  /** NEW-10: a file is being dragged over the conversation column. */
  const [dragActive, setDragActive] = useState(false);
  /** How many nested elements that drag is currently inside — see onDragEnter. */
  const dragDepth = useRef(0);

  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<ComposerHandle>(null);
  /**
   * "Ask TechSara AI" (2026-09-03), in two pieces on purpose.
   *
   * `selectionCandidate` is the live thing under the cursor — it comes and
   * goes with the browser's own selection. `selectedContext` is the COMMITTED
   * reference: once the user has clicked the action it must survive the
   * selection being cleared (which clicking anything does), the composer being
   * typed in, files being attached and modes being toggled. Only an explicit
   * ×, a sent turn, or leaving the conversation ends it.
   */
  const [selectionCandidate, setSelectionCandidate] =
    useState<SelectionCandidate | null>(null);
  const [selectedContext, setSelectedContext] =
    useState<SelectedContext | null>(null);
  /** Read at send time, where a state value would be a render behind. */
  const selectedContextRef = useRef<SelectedContext | null>(null);
  selectedContextRef.current = selectedContext;
  /** The header toggle — where focus returns when the mobile drawer closes. */
  const sidebarToggleRef = useRef<HTMLButtonElement>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  messagesRef.current = messages;
  /**
   * The conversation as READ: one path down the tree.
   *
   * `messages` is everything stored, sibling branches included, and is what
   * gets persisted. `thread` is what the user sees AND what the model is
   * sent — the two were the same list until edits stopped being destructive.
   * Anything about the conversation ON SCREEN reads this one.
   *
   * NEW-24 — the tree is re-walked when the CONVERSATION changes shape, not
   * when a token lands. `messages` gets a new array identity on every
   * streaming frame, which used to re-run the full walk here and again for
   * `versions` below: two complete rebuilds per token, for a structure that
   * had not moved. The walk reads only ids, order and branch pointers, so
   * keying it on exactly those (`treeShape`) skips both rebuilds for the
   * whole answer — and, just as importantly, keeps the objects they produce
   * identical, so the memoized rows downstream are not re-rendered by a
   * fresh-but-equal prop (M-08). Only the final index lookup, which is what
   * actually picks up the new text, runs per frame.
   */
  const treeKey = useMemo(() => treeShape(messages), [messages]);
  const threadPath = useMemo(
    () => threadIndices(messages, branchSelection),
    // `treeKey` is a complete description of everything the walk reads from
    // `messages`; content changes cannot move an index.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [treeKey, branchSelection],
  );
  const thread = useMemo(
    () => threadPath.map((i) => messages[i]),
    [threadPath, messages],
  );
  const threadRef = useRef<ChatMessage[]>([]);
  threadRef.current = thread;
  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;
  const prefsRef = useRef<ChatPrefs>(prefs);
  prefsRef.current = prefs;
  const serverActiveRef = useRef<string[]>([]);
  serverActiveRef.current = serverActive;

  /** Stop showing the loader for `id` — unless the user has moved on since. */
  const settleLoading = useCallback((id: string) => {
    setLoadingId((current) => (current === id ? null : current));
  }, []);

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
      // Before the cache has even hydrated there is an id and no messages,
      // which rendered the New Chat greeting for a conversation that has
      // history. Cleared below the moment we know what this chat holds.
      setLoadingId(wanted);
    } else {
      /**
       * No ?c= — the app opened straight onto a blank chat, and that chat's
       * prefs are DEFAULT_PREFS, because that is what `useState` above was
       * given. The STORED draft slot is not consulted for rendering, so it can
       * still hold whatever a previous session left in it; write the defaults
       * over it so storage says what the screen says.
       *
       * Not cosmetic. `send()` calls `adoptDraftPrefs` when it creates the
       * conversation, and that reads the slot from storage — so a stale draft
       * would leave the request correct (it goes through `prefsRef`) while
       * snapping the composer to Salesforce/Think the instant the first message
       * landed. Opening the app must not need a New Chat click to be neutral.
       */
      savePrefs(window.localStorage, null, DEFAULT_PREFS);
    }

    if (window.matchMedia('(max-width: 767px)').matches) {
      setSidebarOpen(false);
    }

    let cancelled = false;
    /** The ?c= restore handed off to a live stream, which now owns the screen. */
    let handedToStream = false;
    void (async () => {
      let store = getHistoryStore();
      // IndexedDB hydration (single-digit ms; instant for the fallback).
      await store.ready();
      if (cancelled) return;
      refreshList();
      if (wanted && activeIdRef.current === wanted) {
        const cached = store.get(wanted);
        if (cached && cached.messages.length > 0 && !isStreaming(wanted)) {
          setMessages(cached.messages);
          settleLoading(wanted);
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
        if (me.status === 401 || me.status === 403) {
          // Signed out. The cookie is HttpOnly, so only the server can say
          // so — hard-redirect rather than keep serving cached data to
          // whoever is at the keyboard now. A removed or deactivated
          // account is told WHY, on its own page (2026-09-03).
          void handleSessionEnd(me);
          return;
        }
        // Offline (status 0) or the orchestrator failing (5xx — still
        // booting, a network blip): carry on with the cache, but never
        // leave a running generation unguarded.
        if (wanted) {
          const active = await fetchServerActive();
          if (!cancelled) setServerActive(active);
        }
        settleReconcile();
        return;
      }
      setFeatures(me.features);
      // Prefs are sticky. A member whose Salesforce access was removed
      // yesterday must not reopen the app in Salesforce mode with a trust
      // footer promising a mode the server will not run (composerMenu).
      setPrefs((current) => {
        const corrected = prefsForFeatures(current, me.features);
        return corrected === current ? current : corrected;
      });
      let switchedAccount = false;
      if (store.setActiveUser(userScopeKey(me))) {
        // A DIFFERENT account signed in on this browser: its local data was
        // just wiped — rebind to the new account's own database before
        // anything is written, and drop the stale view (the ?c= fast path
        // above may have already painted the previous account's cache).
        switchedAccount = true;
        store = await rebuildHistoryStore();
        if (cancelled) return;
        await store.ready();
        if (cancelled) return;
        setMessages([]);
        setActiveId(null);
        activeIdRef.current = null;
        setLoadingId(null);
        setUrlConversation(null);
        setPrefs({ ...DEFAULT_PREFS });
        refreshList();
      }
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
        // After an account switch `wanted` names the PREVIOUS account's
        // conversation — nothing of it may be reconciled for this one.
        if (wanted && !switchedAccount) {
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
            handedToStream = true;
            void attachStream(wanted).then((ok) => {
              if (!ok && activeIdRef.current === wanted) {
                // Finished during the reload gap — its answer is in history.
                setStreaming(false);
                void store.load(wanted, { force: true }).then((conv) => {
                  if (conv && activeIdRef.current === wanted && !isStreaming(wanted)) {
                    setMessages(conv.messages);
                  }
                  settleLoading(wanted);
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
          if (!cancelled) settleLoading(wanted);
        }
      } finally {
        settleReconcile();
        // Every early return above (signed out, offline, account switch) also
        // ends the restore, so the loader can never outlive it. The streaming
        // hand-off is the one exception: the stream settles it on delivery.
        if (wanted && !cancelled && !handedToStream) settleLoading(wanted);
      }
    })();
    return () => {
      cancelled = true;
    };
    // settleLoading is useCallback([])-stable, so listing it keeps the effect
    // mount-only exactly as before.
  }, [refreshList, setUrlConversation, toast, settleLoading]);

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
      // The stream owns the screen now, so the history loader is done.
      setLoadingId((current) => (current === id ? null : current));
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

  /**
   * NEW-24 — follow the bottom of a growing answer, at most once per frame.
   *
   * This ran once per token, from a passive effect: `scrollHeight` forced a
   * synchronous layout immediately after the commit, the write that followed
   * invalidated it again, and the next token repeated both before the browser
   * had painted anything. Measured 502 forced layout round-trips for a
   * 500-delta answer — layout thrash in lockstep with the stream, which is a
   * large part of what the stutter actually was.
   *
   * Now the read and the write happen together inside the frame the browser
   * is about to paint, and a frame already booked is reused rather than
   * stacked. `matchMedia` is not consulted here at all: this path never
   * animates, so reduced motion has nothing to say about it (it still governs
   * the explicit "Jump to latest" below).
   */
  const followFrame = useRef<number | null>(null);
  const followBottom = useCallback(() => {
    if (followFrame.current !== null) return;
    followFrame.current = requestAnimationFrame(() => {
      followFrame.current = null;
      const el = scrollRef.current;
      if (!el) return;
      el.scrollTop = el.scrollHeight;
    });
  }, []);

  // No frame may repaint into a view that is gone.
  useEffect(
    () => () => {
      if (followFrame.current !== null) {
        cancelAnimationFrame(followFrame.current);
        followFrame.current = null;
      }
    },
    [],
  );

  const scrollToBottom = useCallback(
    (smooth: boolean) => {
      const el = scrollRef.current;
      if (!el) return;
      // Asking to go to the bottom is asking to follow again.
      followRef.current = true;
      if (!smooth) {
        followBottom();
        return;
      }
      const reduced = window.matchMedia(
        '(prefers-reduced-motion: reduce)',
      ).matches;
      el.scrollTo({
        top: el.scrollHeight,
        behavior: reduced ? 'auto' : 'smooth',
      });
    },
    [followBottom],
  );

  /**
   * NEW-25 — where the viewport is, without measuring anything on the scroll
   * path.
   *
   * This used to be an `onScroll` handler that read `scrollHeight`,
   * `scrollTop` and `clientHeight` and then called `setAtBottom`. Three
   * forced layouts per native scroll event, and a scroll gesture produces
   * them at 60-120 Hz — on a DOM that is dirty by definition, because the
   * answer changed on the frame before. That is the classic scroll-jank
   * shape: the browser cannot service the gesture because every event drags
   * a full layout of a long conversation behind it.
   *
   * Two observers on a sentinel at the end of the thread answer the same
   * question for free. They run off the scroll path, measure nothing
   * synchronously, and only report when the answer actually CHANGES — which
   * during ordinary following is never, because the sentinel stays in view.
   *
   *   `atBottom`   — 80 px of slack, the existing "Jump to latest" threshold.
   *   `nearBottom` — tight, and the only thing auto-follow is allowed to
   *                  read. Keeping it tight is what removes the band in
   *                  which the app used to drag the user back down: any real
   *                  scroll away from the bottom, by any input device, takes
   *                  the sentinel out of view and stops the follow at once.
   */
  const bottomRef = useRef<HTMLDivElement>(null);
  const nearBottomRef = useRef(true);
  /**
   * Is auto-follow armed? A ref, not state: a gesture must be able to call
   * off the follow without re-rendering the conversation it is trying to let
   * the user read.
   */
  const followRef = useRef(true);

  useEffect(() => {
    const root = scrollRef.current;
    const target = bottomRef.current;
    if (!root || !target || typeof IntersectionObserver === 'undefined') return;

    const button = new IntersectionObserver(
      (entries) => {
        const visible = entries[entries.length - 1].isIntersecting;
        setAtBottom((current) => (current === visible ? current : visible));
      },
      { root, rootMargin: '0px 0px 80px 0px', threshold: 0 },
    );
    const follow = new IntersectionObserver(
      (entries) => {
        const visible = entries[entries.length - 1].isIntersecting;
        nearBottomRef.current = visible;
        // Arriving back at the bottom is the gesture that means "follow
        // again" — whatever the user got there with.
        if (visible) followRef.current = true;
      },
      // A few pixels of tolerance for sub-pixel layout, not a band to fight in.
      { root, rootMargin: '0px 0px 8px 0px', threshold: 0 },
    );
    button.observe(target);
    follow.observe(target);
    return () => {
      button.disconnect();
      follow.disconnect();
    };
  }, []);

  /**
   * User intent, read straight off the gesture — no DOM access at all.
   *
   * Registered directly so they can be PASSIVE: neither handler calls
   * preventDefault (they only read `deltaY` and touch coordinates), so the
   * browser must never wait on them before scrolling. React would attach its
   * own listeners for these, and passive-by-default is not something to rely
   * on implicitly for the one path this bug is about.
   */
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) followRef.current = false;
      else if (nearBottomRef.current) followRef.current = true;
    };
    let lastTouchY = 0;
    const onTouchStart = (e: TouchEvent) => {
      lastTouchY = e.touches[0]?.clientY ?? 0;
    };
    const onTouchMove = (e: TouchEvent) => {
      const y = e.touches[0]?.clientY ?? 0;
      // Finger travelling DOWN drags the content down, i.e. scrolls up.
      if (y > lastTouchY + 2) followRef.current = false;
      else if (y < lastTouchY - 2 && nearBottomRef.current) {
        followRef.current = true;
      }
      lastTouchY = y;
    };

    el.addEventListener('wheel', onWheel, { passive: true });
    el.addEventListener('touchstart', onTouchStart, { passive: true });
    el.addEventListener('touchmove', onTouchMove, { passive: true });
    return () => {
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('touchstart', onTouchStart);
      el.removeEventListener('touchmove', onTouchMove);
    };
  }, []);

  // A different conversation follows again from the start.
  useEffect(() => {
    followRef.current = true;
    nearBottomRef.current = true;
  }, [activeId]);

  // Auto-scroll while content grows, unless the user scrolled up (§9).
  useEffect(() => {
    if (followRef.current && nearBottomRef.current) followBottom();
  }, [thread, followBottom]);

  /** Debounced (300 ms) so typing doesn't re-render the meter per keystroke. */
  const handleDraftChange = useCallback((text: string) => {
    if (draftTimer.current !== null) window.clearTimeout(draftTimer.current);
    draftTimer.current = window.setTimeout(() => setDraft(text), 300);
  }, []);

  /**
   * Cancel a pending draft debounce on unmount.
   *
   * The timer above is cleared on the NEXT keystroke but never when the
   * component goes away, so the last one always outlived it. It then fires,
   * calls setDraft, and React reaches for `window` on a tree that no longer
   * has one. In the browser that is a harmless update to a dead component; in
   * the suite it surfaced as an unhandled `ReferenceError: window is not
   * defined` AFTER all 961 tests had passed, failing CI with exit 1 while
   * every single test was green -- the sibling setInterval above already gets
   * this treatment.
   */
  useEffect(() => {
    return () => {
      if (draftTimer.current !== null) {
        window.clearTimeout(draftTimer.current);
        draftTimer.current = null;
      }
    };
  }, []);

  /**
   * Ask the server what a compaction would fold right now.
   *
   * Called only when the meter's tooltip OPENS (hover or focus) and after a
   * compaction — never per keystroke. Any failure is "unknown", not "nothing":
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

  /** The meter tooltip opened or closed; only opening costs a request. */
  const handleMeterOpenChange = useCallback(
    (open: boolean) => {
      if (open) void refreshFoldable();
    },
    [refreshFoldable],
  );

  /**
   * Compact, from the context ring in the composer.
   *
   * Two things this deliberately does NOT do any more:
   *
   * - It does not touch the context ring. It used to be forced to zero
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
        // The toast is now the whole report. The popover's lasting
        // "Compacted N earlier messages" line went with the popover; the way
        // into the SummaryPanel is still on the compaction notice in the
        // transcript (MessageRow's onShowSummary).
        toast(run.outcome.message, run.outcome.tone);
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
      clarification?: ClarificationResponse | null,
      options?: SendOptions,
    ) => {
      if (options?.prefs) {
        // A slash command sets the mode for THIS send (2026-09-03). The ref
        // is updated synchronously on purpose: startStream below reads
        // prefsRef.current, and a state update alone would land after the
        // request had already gone out under the old prefs.
        prefsRef.current = options.prefs;
        setPrefs(options.prefs);
        savePrefs(window.localStorage, activeIdRef.current, options.prefs);
      }
      // Read (and release) the pending reference HERE — the one point at which
      // the turn is definitely being created. Every earlier refusal (streaming,
      // an empty box, an attachment still being read) returns from the composer
      // without ever calling this, so the reference simply stays put and the
      // user can send again; and from this line on it is durable on the message
      // itself rather than pending in the UI.
      const quoted = selectedContextRef.current;
      selectedContextRef.current = null;
      setSelectedContext(null);
      // Up to 5 images OR exactly one PDF/dataset (2026-08-05) — the
      // Composer enforces the shape; `first` covers the exclusive kinds.
      const first = attachments[0] ?? null;
      const isPdf = first?.kind === 'pdf';
      const isDataset = first?.kind === 'dataset';
      // 2026-09-02: documents stack (up to five). ONE small document still
      // rides inline — byte-identical wire to every conversation before it.
      // Several documents, or any that skipped base64 for size, upload first
      // (chunked past the Cloudflare 100 MB edge cap) and the request sends
      // REFERENCES instead.
      const docAttachments = attachments.filter((a) => a.kind === 'pdf');
      const needsDocUpload =
        docAttachments.length > 1 ||
        docAttachments.some((a) => !a.base64 && !!a.file);
      // Images and documents COEXIST in a message since 2026-09-02; only a
      // dataset still stands alone (it answers through its own engine).
      const images = attachments.filter((a) => a.kind === 'image');
      let conversationId = activeId;
      if (!conversationId) {
        const title = text || first?.name || '';
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
        // 2026-08-21: attachments ride on meta, so the file card can be
        // rendered by any browser from server history — pdfName alone never
        // left this browser's cache. (`meta.pasted` rode here the same way
        // until 2026-09-04, when the composer stopped turning a long paste
        // into a chip; turns already stored with it still render and still
        // fold — this is only the write side.)
        meta: metaWithBranch(
          isPdf || isDataset || quoted
            ? {
                route: 'chat',
                // Round-trips through server history, renders from history in
                // any browser, and is folded into the model text at request
                // time (lib/streams.ts) rather than written into `content`.
                ...(quoted ? { selected_context: quoted } : {}),
                ...(isPdf || isDataset
                  ? {
                      // EVERY document, in attach order (2026-09-02). One
                      // entry used to stand for the lot, which meant the sent
                      // bubble showed one chip for five files and only the
                      // first ever received its durable server id.
                      attachments: isDataset
                        ? [{ name: first?.name ?? 'file', kind: 'dataset' as const }]
                        : docAttachments.map((a) => ({
                            name: a.name,
                            kind: 'pdf' as const,
                          })),
                    }
                  : {}),
              }
            : undefined,
          userBranch,
        ),
        createdAt: Date.now(),
      };
      // NEW-09: keep the original Files for this tab so the cards below can be
      // OPENED. Positional and keyed by message id, because two turns are
      // allowed to attach two different files both called `invoice.pdf`, and
      // the order here is exactly the order MessageRow renders them in: the
      // lone PDF/dataset, or the images in sequence.
      rememberAttachmentFiles(
        userMessage.id,
        (isPdf || isDataset ? (first ? [first] : []) : images).map((a) =>
          a.file ? { name: a.name, mime: a.file.type, blob: a.file } : null,
        ),
      );
      // Keep the payloads in memory so regenerate/retry re-send the same
      // question WITH its attachments (never persisted — see lib/attachments).
      //
      // EVERY document, not the first (2026-09-03). This line used to keep
      // `[first]` alone, which is where a four-document turn lost three files
      // on its way back through regenerate and edit: the resend read what was
      // remembered, and one document was all there was. Documents are stored
      // in attach order because `resendOptionsFor` matches them to
      // `meta.attachments` by POSITION — never by name, since two files may
      // share one.
      if (!isDataset) {
        rememberAttachments(userMessage.id, [
          ...images
            .filter((i) => i.base64)
            .map((i) => ({
              kind: 'image' as const,
              name: i.name,
              base64: i.base64,
            })),
          ...docAttachments
            .filter((d) => d.base64)
            .map((d) => ({ kind: 'pdf' as const, name: d.name, base64: d.base64 })),
        ]);
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
      if ((isDataset && first?.file) || needsDocUpload) {
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

      if ((isDataset && first?.file) || needsDocUpload) {
        // Datasets and documents stream to their own endpoint and are then
        // referenced by the conversation, so the chat request stays small
        // whatever — and however many — the files weighed.
        void (async () => {
          let docRefs: { upload_id: string; name: string }[] | null = null;
          try {
            let uploadedId: string | undefined;
            if (needsDocUpload) {
              // In PARALLEL (2026-09-03), and a document that started
              // uploading when it was attached (Composer.withEarlyUpload)
              // is only awaited, not sent twice. Five 60 MB files used to
              // upload one after another on the send's critical path.
              const refs = await Promise.all(
                docAttachments.map(async (doc) => {
                  const early = doc.uploadPromise ? await doc.uploadPromise : null;
                  if (early) return early;
                  // Reuse paths may carry only base64; the picker always
                  // keeps the File. Either way the server gets real bytes.
                  const src =
                    doc.file ??
                    new File(
                      [Uint8Array.from(atob(doc.base64), (c) => c.charCodeAt(0))],
                      doc.name,
                    );
                  return uploadDocumentFile(src, conversationId);
                }),
              );
              docRefs = refs;
              refs.forEach((r, i) => {
                const entry = userMessage.meta?.attachments?.[i];
                if (entry) entry.id = r.upload_id;
              });
              uploadedId = undefined; // ids already linked, one per document
              persist(conversationId, turns);
              toast(
                refs.length === 1
                  ? `Uploaded ${docAttachments[0].name}.`
                  : `Uploaded ${refs.length} documents.`,
              );
            } else {
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
              uploadedId = body.upload_id;
              toast(
                `Profiled ${body.files ?? 0} file${body.files === 1 ? '' : 's'} from ${first.name}.`,
              );
            }
            // Link the turn to the server's durable uploads row, so the
            // persisted message names the exact attachment it was asked about.
            if (uploadedId && userMessage.meta?.attachments?.[0]) {
              userMessage.meta.attachments[0].id = uploadedId;
              persist(conversationId, turns);
            }
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
            // NEW-14: the file itself does not travel — it is already on the
            // server, keyed by this conversation. Saying so is what lets the
            // proxy give a wordless dataset send a question to ask, instead of
            // rejecting it as an empty request (which is what produced a 400
            // immediately after a perfectly successful upload).
            ...(isDataset ? { dataset: true } : {}),
            ...(docRefs?.length
              ? {
                  pdfUploads: docRefs,
                  pdfName: docAttachments[0]?.name ?? null,
                  images: images.map((i) => i.base64).filter(Boolean),
                }
              : {}),
          });
          disarmDeepResearch();
        })();
        return;
      }

      // Durability for the inline path (2026-09-02): the answer streams NOW
      // from the inline copy, while the same bytes upload quietly so the
      // message's card can re-open in any browser, any time — the exact
      // "no longer available in this browser session" complaint. Best-effort:
      // a failed background upload costs only the re-open, never the answer.
      if (isPdf && docAttachments[0]?.file && !needsDocUpload) {
        const durableDoc = docAttachments[0];
        void (async () => {
          try {
            const ref = await uploadDocumentFile(
              durableDoc.file as File,
              conversationId,
            );
            const entry = userMessage.meta?.attachments?.[0];
            if (entry && !entry.id) {
              entry.id = ref.upload_id;
              persist(conversationId, turns);
            }
          } catch {
            /* the inline answer already has the bytes */
          }
        })();
      }
      void startStream({
        conversationId,
        turns,
        context,
        assistantBranch: answerBranch,
        prefs: prefsRef.current,
        // 2026-09-02: images accompany documents now ("compare the chart to
        // the report") — the document engine takes them as extra_images.
        images: images.map((i) => i.base64).filter(Boolean),
        pdf: isPdf ? docAttachments[0]?.base64 ?? null : null,
        pdfName: isPdf ? docAttachments[0]?.name ?? null : null,
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
      send(summary, [], response);
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
    (text: string, attachments: Attachment[], options?: SendOptions) => {
      const armed = customAnswerRef.current;
      if (armed && text.trim() && attachments.length === 0) {
        const response = buildResponse(armed, { customText: text });
        if (response && !clarificationAlreadySubmitted(response.client_message_id)) {
          markClarificationSubmitted(response.client_message_id);
          setSubmittingClarificationId(response.clarification_id);
          setCustomAnswerFor(null);
          send(text, [], response);
          return;
        }
      }
      send(text, attachments, undefined, options);
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
      // `messageId` is normally the ANSWER (the "Try again" button). Since
      // 2026-09-03 it may also be the USER turn itself — an edit submitted
      // with its text unchanged is a regenerate, and that turn may not have
      // an answer under it yet. Either way the question is the nearest user
      // turn at or above the id, and everything from here is identical.
      let userIdx = view[idx].role === 'user' ? idx : idx - 1;
      while (userIdx >= 0 && view[userIdx].role !== 'user') userIdx--;
      if (userIdx < 0) return;
      const context = view.slice(0, userIdx + 1);

      // Re-send the SAME question, attachments included — ALL of them, by
      // reference where the message carries upload ids, inline only where it
      // does not (lib/attachments resendOptionsFor). Without this the model
      // was re-asked "what's in this invoice?" with no invoice attached; and
      // until 2026-09-03 a four-document turn was re-asked with one.
      const resend = resendOptionsFor(view[userIdx]);
      if (resend.missing) {
        toast(
          'Re-attach the file to regenerate this answer — its contents are no longer in memory.',
          'error',
        );
        return;
      }

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
        images: resend.images,
        pdf: resend.pdf,
        pdfName: resend.pdfName,
        pdfUploads: resend.pdfUploads,
        // PHASE 3: a dataset turn resends no bytes, but it must still SAY it
        // is a dataset turn — otherwise a wordless one rebuilds the exact
        // NEW-14 request that has no message in it and 400s.
        dataset: resend.dataset,
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
      const resend = resendOptionsFor(original);
      if (resend.missing) {
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
      // The new version remembers what the original remembered — every image
      // and every inline document — so a later regenerate of the EDIT can
      // rebuild the same request the edit itself is about to send.
      rememberAttachments(edited.id, attachmentsForResend(original).attachments);
      // The edit is a new message carrying the SAME attachments, so the files
      // follow it — otherwise rewording a question turned its file card dead.
      carryAttachmentFiles(original.id, edited.id);
      persist(id, turns);
      setMessages(turns);
      // Show the version just written, the way ChatGPT lands you on 2 / 2.
      setBranchSelection((prev) =>
        selectVersion(turns, prev, version.parent ?? ROOT, version.self),
      );
      setAtBottom(true);
      setUnreachable(false);
      setStreaming(true);
      void startStream({
        conversationId: id,
        turns,
        context,
        assistantBranch: answerBranch,
        prefs: prefsRef.current,
        images: resend.images,
        pdf: resend.pdf,
        pdfName: resend.pdfName,
        pdfUploads: resend.pdfUploads,
        // The edit inherits the original turn's attachments, so it inherits
        // its dataset-ness too (see regenerate above).
        dataset: resend.dataset,
      });
    },
    [persist, toast],
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
      // An emptied box is Cancel, not an edit (the editor's Send is disabled
      // for it anyway, and an attachment-only turn must never be re-asked as
      // an empty request).
      if (!next) {
        setEditingMessageId(null);
        return;
      }
      // Unchanged text is not an edit — a second identical version would add
      // a `1 / 2` with nothing to navigate between. Re-asking as-is is what
      // Regenerate is for, so that is what it does (owner request 2026-09-03):
      // the SAME user turn, a new answer beside the old one, exactly as the
      // "Try again" button would — confirmation for an older answer included,
      // because the same rows are at stake. The comparison is the editor's own
      // normalisation (outer trim only): "Read these" → "Read these carefully"
      // and a moved line break are both real edits.
      if (next === (original.content ?? '').trim()) {
        setEditingMessageId(null);
        const view = threadRef.current;
        const at = view.findIndex((m) => m.id === messageId);
        const answer = at === -1 ? undefined : view.slice(at + 1).find((m) => m.role === 'assistant');
        if (answer) regenerate(answer.id);
        // No answer under it yet (a failed or stopped first attempt): there
        // is nothing to discard, so it runs straight away from the turn.
        else void runRegenerate(messageId);
        return;
      }
      void runEdit(messageId, next);
    },
    [runEdit, regenerate, runRegenerate],
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
    const resend = resendOptionsFor(view[userIdx]);
    if (resend.missing) {
      toast(
        'Re-attach the file to retry this message — its contents are no longer in memory.',
        'error',
      );
      return;
    }
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
      images: resend.images,
      pdf: resend.pdf,
      pdfName: resend.pdfName,
      pdfUploads: resend.pdfUploads,
      dataset: resend.dataset,
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
    // A new chat is genuinely empty — EmptyState is the right answer here.
    setLoadingId(null);
    setFoldableTurns(null);
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
      // Belongs to the chat being left, not the one being opened.
      setFoldableTurns(null);
      setUrlConversation(id);

      const live = getLiveStream(id);
      if (live) {
        // This chat is generating in the background — adopt the live thread.
        setMessages([...live.messages]);
        setStreaming(live.status === 'streaming');
        setLoadingId(null);
      } else {
        const cached = store.get(id);
        // ALWAYS take ownership of what is on screen. This was `if (cached)`,
        // so an uncached chat left the PREVIOUS conversation's messages
        // rendered under this one's identity.
        const cachedMessages = cached?.messages ?? [];
        setMessages(cachedMessages);
        // Nothing to show yet is "loading", not "empty".
        setLoadingId(cachedMessages.length === 0 ? id : null);
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
                // Settled either way: a chat we could not load is not still
                // loading, and must not sit under a spinner for ever.
                settleLoading(id);
              });
            }
          });
        } else {
          // Server truth may be newer / not cached yet (V2 §4b); force a
          // refetch when the chat ends on a user message — a detached
          // generation may have saved its answer while we were away.
          const force = cached?.messages.at(-1)?.role === 'user';
          void store.load(id, { force }).then((conv) => {
            // The identity guard is what makes a late answer harmless: click
            // A then B, and A's response finds activeIdRef pointing at B and
            // does nothing at all — neither its messages nor its loader.
            if (conv && activeIdRef.current === id && !isStreaming(id)) {
              setMessages(conv.messages);
            }
            settleLoading(id);
          });
        }
      }
      if (window.matchMedia('(max-width: 767px)').matches) {
        setSidebarOpen(false);
      }
    },
    [setUrlConversation, settleLoading],
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
        setLoadingId(null);
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

  /**
   * A pending "Ask TechSara AI" reference belongs to ONE conversation.
   *
   * Keyed on `activeId` rather than wired into each of newChat / open / delete
   * / the history reset, because every one of those ends in the same place —
   * a different active conversation — and four call sites is four chances for
   * the next one to be forgotten and leak a quote from someone else's chat
   * into this one. (A send that CREATES a conversation also lands here, and
   * has already released the reference onto its message by then.)
   */
  useEffect(() => {
    setSelectedContext(null);
    selectedContextRef.current = null;
    setSelectionCandidate(null);
  }, [activeId]);

  // Keyboard shortcuts (§9 + V4 §2). The map itself is pure and unit-tested
  // in lib/searchPalette.ts; this only supplies the live context and runs the
  // action it names.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null;
      const action = shortcutAction(e, {
        paletteOpen: searchOpen,
        quoteActionOpen: selectionCandidate !== null,
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
        case 'close-quote-action':
          // The floating action only. A reference already committed to the
          // composer is NOT thrown away by Escape — it took a deliberate
          // click to make, and it takes the × to unmake.
          setSelectionCandidate(null);
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
  }, [newChat, searchOpen, selectionCandidate, stopStreaming]);

  /**
   * Which visible turns have alternatives — one walk, not one per row, and
   * (NEW-24) not one per streaming token either: a `VersionInfo` is built
   * from ids and positions alone, so it is stable for as long as the shape
   * is, and a row that has one is not re-rendered by the answer growing.
   */
  const versions = useMemo(
    () => versionMap(messages),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- see `treeKey`.
    [treeKey],
  );

  const activeTitle =
    conversations.find((c) => c.id === activeId)?.title ?? 'New chat';
  const [shareOpen, setShareOpen] = useState(false);
  /* Sharing needs a conversation that EXISTS on the server and has finished
     saying something. A browser-minted id with nothing behind it has nothing
     to snapshot, and a half-streamed answer would publish a sentence that
     stops mid-word — the server refuses both, so this only avoids offering a
     button that would fail. */
  const streamingHere = Boolean(activeId && isStreaming(activeId));
  const canShare = Boolean(
    activeId &&
      thread.some((m) => m.role === 'assistant' && m.status !== 'streaming'),
  );
  /* 2026-09-03 (owner request): the header no longer carries a passive engine
     badge — "Vision" / "Chat" / "Records" in the top-right corner told the
     reader which route answered, which is a fact about the machine rather than
     about the answer. `meta.route` is untouched: it is still written, still
     persisted, still what history, regenerate and the request path read. Only
     the label is gone, and with it the `lastEngine` walk that existed solely to
     feed it. */

  /**
   * Chats generating right now — in this tab OR server-side (after reload).
   *
   * NEW-24: kept at a STABLE identity while the set is unchanged. This is a
   * fresh array on every render by construction (`streamingIds()` reads the
   * stream registry, not React state, so it cannot be a `useMemo`), and as a
   * prop it re-rendered the whole sidebar on every streaming frame — measured
   * 721 ms of a 9.3 s run with 40 conversations in the list, for a spinner
   * that had not moved. Comparing the contents costs one pass over a handful
   * of ids; not comparing them cost a full list render per frame.
   */
  const busyNow = Array.from(new Set([...streamingIds(), ...serverActive]));
  const busyRef = useRef<string[]>(busyNow);
  if (
    busyRef.current.length !== busyNow.length ||
    busyRef.current.some((id, i) => id !== busyNow[i])
  ) {
    busyRef.current = busyNow;
  }
  const busyIds = busyRef.current;
  const closeSidebar = useCallback(() => setSidebarOpen(false), []);
  const openSearch = useCallback(() => setSearchOpen(true), []);

  /**
   * PHASE 4A/4B — put a previously sent attachment back in the composer.
   *
   * ONE function behind both gestures. The internal drag and the drop that
   * receives it are different ways of asking the same question, and giving
   * them separate pipelines is how two entry points drift until one accepts
   * what the other refuses — the same reasoning that made `acceptFiles` the
   * single door for the picker and the desktop drop.
   *
   * It resolves bytes down the full ladder (this tab's File, the persisted
   * image payload, then the orchestrator by upload_id), rebuilds a real `File`
   * and hands it to the composer. It does NOT bypass validation: the caps, the
   * five-image ceiling, the PDF/dataset exclusivity and the refusals while
   * streaming or uploading all still apply, because this goes in through the
   * same front door a picked file does.
   */
  const reuseAttachment = useCallback(
    async (messageId: string, index: number) => {
      const message = messagesRef.current.find((m) => m.id === messageId);
      if (!message) return;
      const previews = message.imageDataUrls?.length
        ? message.imageDataUrls
        : message.imageDataUrl
          ? [message.imageDataUrl]
          : [];
      const source = await resolveAttachmentAsync(messageId, index, {
        name: message.meta?.attachments?.[index]?.name ?? message.pdfName,
        dataUrl: previews[index],
        upload: uploadRefFor(activeIdRef.current, message, index),
      });

      if (source.kind === 'expired') {
        toast(
          'This upload has expired and can no longer be attached again. Attach the file from your computer instead.',
          'error',
        );
        return;
      }
      if (!source.blob) {
        toast(
          'This file is no longer available in this browser session. Attach it from your computer instead.',
          'error',
        );
        return;
      }

      // The type is taken from OUR allowlist, keyed off the name — never from
      // whatever the blob claims. The composer classifies by name too, so this
      // keeps one story about what a file is.
      const type = previewMimeFor(source.name, source.mime) ?? source.blob.type;
      const file = new File([source.blob], source.name, {
        type,
        lastModified: Date.now(),
      });
      composerRef.current?.acceptFiles([file]);
    },
    [toast],
  );

  /**
   * M-08 — per-row callbacks with STABLE identity.
   *
   * Every handler a row receives used to be an inline arrow rebuilt on each
   * render, so all 100 rows of a long thread got eight brand-new function
   * props on every streaming token and re-rendered through any memo you cared
   * to add. Measured: 50,200 row renders for one 500-delta answer, ~100 of
   * them wasted per token.
   *
   * Built once per message id and cached. The cached functions never close
   * over a handler directly — they read the CURRENT one off a ref when they
   * are called — which is what makes them permanently stable AND impossible
   * to go stale: a row rendered at token 1 still runs today's `regenerate`
   * against today's `activeId`. They only ever fire from user events, long
   * after the ref has been written.
   */
  const rowApi = {
    regenerate,
    reuseAttachment,
    startEdit,
    cancelEdit,
    submitEdit,
    setSummaryOpen,
    activeId,
  };
  const rowApiRef = useRef(rowApi);
  rowApiRef.current = rowApi;
  const rowHandlersRef = useRef(new Map<string, RowHandlers>());
  // A different conversation is a different set of rows; without this the
  // cache would accumulate every message id visited in the session.
  const handlerScope = useRef<string | null>(null);
  if (handlerScope.current !== activeId) {
    handlerScope.current = activeId;
    rowHandlersRef.current = new Map();
  }
  const rowHandlers = useCallback((id: string): RowHandlers => {
    const cache = rowHandlersRef.current;
    const existing = cache.get(id);
    if (existing) return existing;
    const handlers: RowHandlers = {
      onRegenerate: () => rowApiRef.current.regenerate(id),
      onRetry: () => rowApiRef.current.regenerate(id),
      onReuseAttachment: (index) => {
        void rowApiRef.current.reuseAttachment(id, index);
      },
      onEditStart: () => rowApiRef.current.startEdit(id),
      onEditCancel: () => rowApiRef.current.cancelEdit(),
      onEditSubmit: (text) => rowApiRef.current.submitEdit(id, text),
      onShowSummary: () => rowApiRef.current.setSummaryOpen(true),
      onFeedback: (feedback) => {
        const conversationId = rowApiRef.current.activeId;
        if (!conversationId) return;
        // Fire-and-forget: the store updates its cache first and swallows a
        // failed request, so a thumb never blocks the UI or raises a pill.
        void getHistoryStore().setMessageFeedback(conversationId, id, feedback);
      },
    };
    cache.set(id, handlers);
    return handlers;
  }, []);

  /* ------------------------------------------------- NEW-10: file drop -- */

  // The same three conditions the Composer refuses on — a chat still being
  // restored, a dataset upload in flight, and an answer streaming (the "+"
  // menu greys "Add photos & files" out mid-stream). Read here only to decide
  // whether to PROMISE a drop will work: whether one is actually ACCEPTED is
  // the Composer's call, always, so a drop can never take a route the "+" menu
  // would have refused, and a rejected drop still gets the picker's own toast
  // rather than vanishing.
  const uploading = datasetUpload?.status === 'uploading';
  const canAttach = !reconciling && !uploading && !streaming;

  /**
   * Depth, not a boolean.
   *
   * `dragleave` fires when the pointer crosses into a CHILD, and it arrives
   * after that child's `dragenter`. A plain enter/leave toggle therefore
   * strobes the overlay on and off all the way down the message list. Counting
   * how many nested elements the drag is currently inside is the reliable fix.
   */
  /** Files from the desktop, or one of our own cards (4B). */
  function dragIsAttachable(dt: DataTransfer | null): boolean {
    return dragHasFiles(dt) || dragHasInternalAttachment(dt);
  }

  function onDragEnter(e: React.DragEvent<HTMLDivElement>) {
    if (!dragIsAttachable(e.dataTransfer)) return;
    dragDepth.current += 1;
    if (dragDepth.current === 1 && canAttach) setDragActive(true);
  }

  function onDragOver(e: React.DragEvent<HTMLDivElement>) {
    // ONLY for attachable drags. Left alone, a text or link drag keeps the browser's own
    // behaviour everywhere in the app.
    if (!dragIsAttachable(e.dataTransfer)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  }

  function onDragLeave(e: React.DragEvent<HTMLDivElement>) {
    if (!dragIsAttachable(e.dataTransfer)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragActive(false);
  }

  /**
   * NEW-10A — the drop, decided once by `dropIntent`.
   *
   * The previous version asked `types.includes('Files')` and, when the answer
   * was no, RETURNED WITHOUT PREVENTING THE DEFAULT. That is how a dragged file
   * turned into `file:///…` typed into the composer: sources that describe a
   * file only as `text/uri-list` fell straight through to the textarea, whose
   * default action for dropped text is to insert it.
   *
   * So the intent decides, and only `ignore` — a genuine text or web-link drag,
   * which must keep behaving exactly as the browser intends — is allowed to
   * reach the default. Everything else is prevented here, before the textarea
   * ever sees it. That is also why these are bound in the CAPTURE phase: the
   * region has to own the event on the way DOWN to the textarea, not on the way
   * back up from it.
   */
  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    const intent = dropIntent(e.dataTransfer);
    if (intent.action !== 'ignore') {
      e.preventDefault();
      e.stopPropagation();
    }
    dragDepth.current = 0;
    setDragActive(false);

    if (intent.action === 'ignore') return;

    if (intent.action === 'internal') {
      // 4B: the drag's drop resolves through the SAME handler, on purpose.
      // (The visible "Attach again" button was removed 2026-09-03; this is
      // now the only entry point, and it is unchanged.)
      void reuseAttachment(intent.ref.messageId, intent.ref.index);
      return;
    }

    if (intent.action === 'file-uri') {
      // The source handed over a LINK to a file rather than its bytes. A web
      // page may not read a local path — that is browser security, not a gap
      // to work around — so the only honest options are to say so or to paste
      // the path into the prompt, and pasting it is what the bug did.
      toast(
        'This drag source gave a file link, not the file itself. Drag the file from your file manager, or use + → Add photos & files.',
        'error',
      );
      return;
    }

    if (intent.action === 'directories') {
      toast(
        'Folders can’t be attached — drop the files inside it instead.',
        'error',
      );
      return;
    }

    // Chrome hands a dropped folder over as a 0-byte File, so a mixed drop
    // says so rather than failing validation with a baffling type complaint.
    if (intent.directories > 0) {
      toast(
        'Folders can’t be attached — drop the files inside it instead.',
        'error',
      );
    }
    // Straight into the composer's own pipeline: same validation, same caps,
    // same toasts, same refusal while a chat is loading or an upload is live.
    composerRef.current?.acceptFiles(intent.files);
  }

  return (
    <div className="flex h-dvh overflow-hidden">
      <Sidebar
        open={sidebarOpen}
        onClose={closeSidebar}
        conversations={conversations}
        archived={archived}
        activeId={activeId}
        streamingIds={busyIds}
        onNewChat={newChat}
        onOpenSearch={openSearch}
        onSelect={selectConversation}
        onRename={renameConversation}
        onDelete={deleteConversation}
        onSetPinned={pinConversation}
        onSetArchived={archiveConversation}
        onExport={exportConversation}
        onLoadArchived={loadArchived}
        restoreFocusRef={sidebarToggleRef}
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

      {/* NEW-10: the conversation column is the drop region — the thread, the
          header and the composer, but deliberately NOT the sidebar, where a
          dropped file has no meaning. The handlers ignore every drag that is
          not carrying files, so text and link dragging is untouched. */}
      <div
        data-file-drop-zone
        // CAPTURE, not bubble (NEW-10A): the region must take the event on the
        // way down, before the <textarea> inside it applies its own default of
        // typing dropped text into the prompt.
        onDragEnterCapture={onDragEnter}
        onDragOverCapture={onDragOver}
        onDragLeaveCapture={onDragLeave}
        onDropCapture={onDrop}
        className="relative flex min-w-0 flex-1 flex-col"
      >
        {dragActive && (
          /* Pointer-events-none: an overlay that swallowed the drag would fire
             leave/enter against itself and strobe. It paints over the column
             and changes no layout, so nothing behind it moves. */
          <div className="pointer-events-none absolute inset-2 z-40 flex items-center justify-center rounded-ts border-2 border-dashed border-accent/60 bg-bg/70">
            {/* Words, not just a colour — and mounted exactly once per drag,
                so a screen reader hears it once rather than on every
                dragenter the pointer generates crossing the message list. */}
            <span
              role="status"
              className="rounded-full border border-accent/40 bg-surface px-4 py-2 text-sm font-medium text-ink shadow-lg"
            >
              Drop files to attach
            </span>
          </div>
        )}
        {/* ChatGPT-parity header: no app name, no chat title. The sidebar owns
            its own collapse button, so this one only appears once the sidebar
            is hidden — it is the only way back. The title stays as sr-only
            text so screen readers still announce which chat is open. */}
        <header className="flex h-[52px] shrink-0 items-center gap-2 px-3">
          {!sidebarOpen && (
            <button
              // Closing the mobile drawer hands focus back here. The button
              // only exists while the sidebar is closed, so the drawer cannot
              // capture it as `document.activeElement` on the way in — it
              // reads this ref on the way out, by which point React has
              // re-mounted the button and re-attached it (see Sidebar).
              ref={sidebarToggleRef}
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
          {/* Share (2026-09-05). The header's one deliberate addition since
              the engine badge was removed: an ACTION, not a passive label.
              It appears only for a conversation that exists and has finished
              saying something — sharing a half-streamed answer publishes a
              sentence that stops mid-word — and the label collapses to the
              icon on a phone the same way the composer's controls do
              (Composer.tsx), because `display:none` would leave a nameless
              button rather than a compact one. */}
          {canShare && (
            <button
              type="button"
              onClick={() => setShareOpen(true)}
              aria-label="Share conversation"
              title={
                streamingHere
                  ? 'Wait for the answer to finish'
                  : 'Share conversation'
              }
              disabled={streamingHere}
              className="ml-auto inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg disabled:cursor-not-allowed disabled:opacity-40"
            >
              <IconShare size={16} />
              <span className="sr-only md:not-sr-only">Share</span>
            </button>
          )}
        </header>
        {shareOpen && activeId && (
          <ShareDialog
            conversationId={activeId}
            title={activeTitle}
            onClose={() => setShareOpen(false)}
          />
        )}

        <div
          ref={scrollRef}
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
          ) : thread.length === 0 && loadingId !== null && loadingId === activeId ? (
            /* A conversation with history that has not arrived yet. Showing
               EmptyState here claimed it was a brand-new chat; showing the
               previous chat's messages was worse. Neither is true — this is
               the third state, and it says so.

               `loadingId !== null` is load-bearing: a New Chat has a null
               activeId AND a null loadingId, and `null === null` would have
               put a spinner on the one screen that really is empty. */
            <div
              data-testid="conversation-loading"
              role="status"
              aria-live="polite"
              className="flex h-full flex-col items-center justify-center gap-3 px-4 py-10"
            >
              <Loader size={28} />
              <p className="text-sm text-muted">Loading conversation…</p>
            </div>
          ) : thread.length === 0 ? (
            <EmptyState />
          ) : (
            <div className="mx-auto w-full max-w-thread space-y-6 px-4 py-6">
              {thread.map((m, i) => {
                // Only the question the thread is WAITING on is a live control;
                // every earlier card is a record of a decision already made.
                const card = cardState(thread, i);
                // Stable per-id callbacks (M-08). Everything else below is
                // either the message object itself — which `updateAssistant`
                // leaves untouched unless it really changed — or a primitive,
                // so a memoized row re-renders when its own turn changes and
                // at no other time.
                const on = rowHandlers(m.id);
                return (
                <MessageRow
                  key={m.id}
                  message={m}
                  isLast={i === thread.length - 1 && m.role === 'assistant'}
                  onRegenerate={on.onRegenerate}
                  onRetry={on.onRetry}
                  onReuseAttachment={on.onReuseAttachment}
                  // 4C: which conversation to ask for a workbook profile or a
                  // document's extracted text.
                  conversationId={activeId}
                  uploadStatus={
                    datasetUpload?.messageId === m.id
                      ? datasetUpload.status
                      : null
                  }
                  versions={versions.get(m.id) ?? null}
                  onSelectVersion={selectBranch}
                  onEditStart={on.onEditStart}
                  editing={editingMessageId === m.id}
                  onEditCancel={on.onEditCancel}
                  onEditSubmit={on.onEditSubmit}
                  onShowSummary={on.onShowSummary}
                  clarificationPending={card.pending}
                  clarificationAnswer={card.answeredWith}
                  onFeedback={on.onFeedback}
                />
                );
              })}
            </div>
          )}
          {/* NEW-25: what the two IntersectionObservers watch. It is the last
              thing in the scroller and it is always mounted, so "is the end of
              the conversation on screen?" is answered by the browser instead
              of by measuring the document on every scroll event. */}
          <div ref={bottomRef} aria-hidden className="h-px w-full" />
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

        {/* The floating action. Fixed-positioned against the viewport, so it
            lives here rather than inside the scroller — a child of the
            scrolling column would be clipped by its overflow. */}
        <SelectionAsk
          candidate={selectionCandidate}
          onCandidateChange={setSelectionCandidate}
          onAsk={(candidate) => {
            setSelectedContext(candidate.context);
            selectedContextRef.current = candidate.context;
            setSelectionCandidate(null);
            // The next thing the user does is type the follow-up.
            composerRef.current?.focus();
          }}
        />

        <Composer
          ref={composerRef}
          streaming={streaming}
          features={features}
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
              onOpenChange={handleMeterOpenChange}
            />
          }
          prefs={prefs}
          onPrefsChange={updatePrefs}
          onSend={sendFromComposer}
          onStop={stopStreaming}
          selectedContext={selectedContext}
          onClearSelectedContext={() => {
            setSelectedContext(null);
            selectedContextRef.current = null;
          }}
          uploadConversationId={activeId}
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
                onPick={(prompt) => void send(prompt, [])}
                onUseComposer={() => composerRef.current?.focus()}
              />
            ) : null
          }
        />
      </div>
    </div>
  );
}
