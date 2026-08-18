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
  useRef,
  useState,
} from 'react';

/** useLayoutEffect on the client, useEffect on the server (no SSR warning). */
const useIsomorphicLayoutEffect =
  typeof window !== 'undefined' ? useLayoutEffect : useEffect;
import { fetchMe } from '@/lib/auth';
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
import { latestUsage, meterView } from '@/lib/contextMeter';
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
import { MessageRow } from './MessageRow';
import { ClarificationCard } from './ClarificationCard';
import { SearchPalette } from './SearchPalette';
import { Sidebar } from './Sidebar';
import { useToast } from './Providers';
import { IconAlert, IconArrowDown, IconRefresh, IconSidebar } from './icons';

const APP_NAME =
  process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

export function ChatApp() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [archived, setArchived] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [unreachable, setUnreachable] = useState(false);
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
  /** Debounced draft text — the meter's only estimated component. */
  const [draft, setDraft] = useState('');
  const [compacting, setCompacting] = useState(false);
  /** Set by "Compact now" so the ring drops at once, cleared on the next reply. */
  const [compactedAt, setCompactedAt] = useState<number | null>(null);
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
        setCompactedAt(null);
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
  }, [messages, atBottom, scrollToBottom]);

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

  /** "Compact now" from the meter popover. */
  const compactNow = useCallback(() => {
    const id = activeIdRef.current;
    if (!id || compacting) return;
    setCompacting(true);
    void (async () => {
      try {
        const res = await fetch('/api/chat/compact', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            conversation_id: id,
            messages: messagesRef.current
              .filter((m) => m.content)
              .map((m) => ({ role: m.role, content: m.content })),
          }),
        });
        const body = (await res.json()) as {
          compacted?: boolean;
          folded_turns?: number;
          reason?: string;
        };
        if (!res.ok) throw new Error('compact failed');
        toast(
          body.compacted
            ? `Compacted ${body.folded_turns} earlier message${
                body.folded_turns === 1 ? '' : 's'
              } into the summary.`
            : (body.reason ?? 'Nothing to compact yet.'),
        );
        // The next request will be smaller; reflect that immediately rather
        // than waiting for the following reply's meta.
        if (body.compacted) {
          // The next request will be much smaller; reflect that immediately
          // instead of waiting for the following reply's meta.
          setCompactedAt(Date.now());
        }
      } catch {
        toast('Could not compact this conversation.', 'error');
      } finally {
        setCompacting(false);
      }
    })();
  }, [compacting, toast]);

  /* --------------------------------- Salesforce Intelligence Mode */

  // The question this thread is waiting on, read from the LAST assistant
  // message. Deriving it from the thread rather than holding it in its own
  // state is what makes it survive a reload for free: the message comes back
  // from history with `meta.clarification` on it, and the card rebuilds.
  const pending = pendingClarification(messages);
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
        meta: pasted.length ? { route: 'chat', pasted } : undefined,
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
      const turns = [...messagesRef.current, userMessage];
      persist(conversationId, turns);
      setAtBottom(true);
      setUnreachable(false);
      setStreaming(true);

      if (isDataset && first?.file) {
        // Datasets stream to their own endpoint and are then referenced by the
        // conversation, so the chat request itself stays small.
        void (async () => {
          try {
            const form = new FormData();
            form.append('file', first.file as File);
            form.append('conversation_id', conversationId);
            const res = await fetch('/api/upload', { method: 'POST', body: form });
            const body = (await res.json()) as { detail?: string; files?: number };
            if (!res.ok) throw new Error(body.detail ?? 'upload failed');
            toast(
              `Profiled ${body.files ?? 0} file${body.files === 1 ? '' : 's'} from ${first.name}.`,
            );
          } catch (err) {
            toast(
              err instanceof Error ? err.message : 'That dataset could not be read.',
              'error',
            );
          } finally {
            void startStream({
              conversationId,
              turns,
              prefs: prefsRef.current,
            });
          }
        })();
        return;
      }

      void startStream({
        conversationId,
        turns,
        prefs: prefsRef.current,
        images: isPdf ? null : images.map((i) => i.base64).filter(Boolean),
        pdf: isPdf ? first?.base64 ?? null : null,
        pdfName: isPdf ? first?.name ?? null : null,
        clarification: clarification ?? null,
      });
    },
    [activeId, persist, refreshList, setUrlConversation, toast],
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
      const msgs = messagesRef.current;
      const idx = msgs.findIndex((m) => m.id === messageId);
      if (idx === -1) return;
      let userIdx = idx - 1;
      while (userIdx >= 0 && msgs[userIdx].role !== 'user') userIdx--;
      if (userIdx < 0) return;
      const turns = msgs.slice(0, userIdx + 1);

      // Re-send the SAME question, attachments included. Without this the
      // model was re-asked "what's in this invoice?" with no invoice attached.
      const { attachments, missing } = attachmentsForResend(msgs[userIdx]);
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

      // Regenerating an OLDER answer really does discard the turns after it.
      // The sync path cannot shrink a thread (that guard is what stops a
      // stale cache from destroying history), so this intentional shrink goes
      // through the dedicated truncate endpoint — reached ONLY from here,
      // and only after the user confirmed.
      if (msgs.length > turns.length + 1) {
        try {
          await getHistoryStore().truncateMessages(id, turns.length);
        } catch {
          // The server refused (the thread changed elsewhere, or it is gone).
          // Show truth rather than streaming into a thread we misread.
          toast(
            'This conversation changed elsewhere — reloaded it instead of regenerating.',
            'error',
          );
          const conv = await getHistoryStore().load(id, { force: true });
          if (conv && activeIdRef.current === id) setMessages(conv.messages);
          return;
        }
        setMessages(turns);
      }

      setStreaming(true);
      void startStream({
        conversationId: id,
        turns,
        prefs: prefsRef.current,
        images: resendImages,
        pdf: resendPdf?.base64 ?? null,
        pdfName: resendPdf?.name ?? null,
      });
    },
    [toast],
  );

  /**
   * Regenerating an OLDER answer restarts the thread from that point and
   * discards every later turn. That is destructive and irreversible, so it
   * asks first; regenerating the last answer runs straight away.
   */
  const regenerate = useCallback(
    (messageId: string) => {
      const discarded = messagesDiscardedByRegenerate(
        messagesRef.current,
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

  /** Banner retry: re-send the last user turn, attachment included. */
  const retryLastTurn = useCallback(() => {
    const id = activeIdRef.current;
    if (!id || isStreaming(id)) return;
    const msgs = messagesRef.current;
    let userIdx = msgs.length - 1;
    while (userIdx >= 0 && msgs[userIdx].role !== 'user') userIdx--;
    if (userIdx < 0) {
      setUnreachable(false);
      return;
    }
    const { attachments, missing } = attachmentsForResend(msgs[userIdx]);
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
    void startStream({
      conversationId: id,
      turns: msgs.slice(0, userIdx + 1),
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
      setCompactedAt(null);
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

  const activeTitle =
    conversations.find((c) => c.id === activeId)?.title ?? 'New chat';
  const lastEngine: Engine | undefined = [...messages]
    .reverse()
    .find((m) => m.role === 'assistant' && m.meta)?.meta?.route;

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

        {unreachable && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 border-b border-danger/40 bg-danger/10 px-4 py-2.5"
          >
            <IconAlert size={16} className="shrink-0 text-danger" />
            <span className="min-w-0 flex-1 text-sm">
              The orchestrator is unreachable — your message was kept and can
              be re-sent.
            </span>
            <button
              type="button"
              onClick={retryLastTurn}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs font-medium transition-colors duration-ts hover:bg-surface-2"
            >
              <IconRefresh size={13} />
              Retry
            </button>
          </div>
        )}

        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="relative min-h-0 flex-1 overflow-y-auto"
        >
          {messages.length === 0 ? (
            <EmptyState />
          ) : (
            <div className="mx-auto w-full max-w-thread space-y-6 px-4 py-6">
              {messages.map((m, i) => {
                // Only the question the thread is WAITING on is a live control;
                // every earlier card is a record of a decision already made.
                const card = cardState(messages, i);
                return (
                <MessageRow
                  key={m.id}
                  message={m}
                  isLast={i === messages.length - 1 && m.role === 'assistant'}
                  onRegenerate={() => regenerate(m.id)}
                  onRetry={() => regenerate(m.id)}
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

        {!atBottom && messages.length > 0 && (
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
          onDraftChange={handleDraftChange}
          meter={
            <ContextMeter
              view={meterView(compactedAt ? null : latestUsage(messages), draft)}
              compacting={compacting}
              onCompactNow={compactNow}
              compactDisabled={!activeId || streaming}
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
              messageCount: messages.length,
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
