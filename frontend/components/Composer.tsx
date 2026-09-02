'use client';

/**
 * Pinned composer (§9 + V2 §4c): auto-growing textarea (1→10 rows), a
 * ChatGPT-style "+" menu (AttachMenu, 2026-08-05: Add photos & files · Web
 * search · Salesforce) with upload chips + remove, and a controls row —
 * the toggles LIVE in the "+" menu, and a dismissible pill appears here only
 * while its tool is ON (Salesforce, which defaults on; Web search while
 * forced) — plus the effort picker (Fast/Low/Medium/High on the one model)
 * and the send button that morphs to Stop while streaming.
 * There is no Agent toggle: the model decides when to plan steps (2026-07-28).
 * Enter=send / Shift+Enter=newline. The trust footer line dims when the
 * Salesforce toggle is off.
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ClipboardEvent as ReactClipboardEvent,
} from 'react';
import type { ChatPrefs } from '@/lib/prefs';
import { downscaleImageFile } from '@/lib/images';
import {
  imageExtFromMime,
  makePastedText,
  shouldAttachPaste,
} from '@/lib/pasted';
import type { PastedText } from '@/lib/types';
import { activateComposerMenuItem, trustLine } from '@/lib/composerMenu';
import { AttachMenu } from './AttachMenu';
import { ModelPicker } from './ModelPicker';
import { PastedChip } from './PastedChip';
import { useToast } from './Providers';
import {
  IconBook,
  IconCloud,
  IconFileText,
  IconGlobe,
  IconSend,
  IconSparkles,
  IconStop,
  IconX,
} from './icons';

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
/** Up to 5 images per message (owner request 2026-08-05); the orchestrator
    enforces the same ceiling (MAX_IMAGES in main.py). */
const MAX_IMAGES = 5;
const LINE_HEIGHT = 24;
const MAX_ROWS = 10;

export interface ComposerHandle {
  focus: () => void;
  /** Focus, and append `seed` to whatever is already typed. */
  insert: (seed?: string) => void;
  /**
   * Load `text` into the input, then focus with the caret at the end.
   *
   * No longer wired to anything: "Edit" on a sent message was its one caller
   * and now rewrites the message in place instead, which is where a rewrite
   * belongs — at the bottom of the screen it landed on top of whatever was
   * already typed, and re-opening it stacked copy after copy of the same
   * prompt in the box.
   *
   * Kept because it is the only safe way to put a whole prompt into the
   * composer from outside: `insert` appends single keystrokes with no
   * separator (right for the clarification panel, wrong here — it would weld
   * a prompt onto the end of a half-typed word), while this starts the loaded
   * text on its own paragraph and never destroys an unsent draft.
   */
  prefill: (text: string) => void;
  /**
   * NEW-10: attach files that arrived from somewhere other than the hidden
   * input — today, a drag-and-drop onto the conversation column.
   *
   * This is deliberately the WHOLE entry point, not a "validate these for me"
   * helper: it runs the same type and extension checks, the same size caps,
   * the same five-image ceiling, the same PDF/dataset exclusivity and the same
   * toasts as the "+" menu, because it IS the code the "+" menu runs. A second
   * upload route may not come with a second rulebook — that is how the two
   * paths drift until one of them accepts a file the other refuses.
   *
   * It also owns the refusal: a caller cannot talk it into attaching while an
   * answer is streaming, a chat is loading, or an upload is already in flight.
   */
  acceptFiles: (files: File[]) => void;
}

export interface Attachment {
  name: string;
  /**
   * image = sent to the vision path; pdf = rendered server-side;
   * dataset = uploaded separately and referenced by id (never base64).
   */
  kind: 'image' | 'pdf' | 'dataset';
  /** Full data: URL for previews. */
  dataUrl: string;
  /** Raw base64 payload (no data: prefix) — what POST /chat expects. */
  base64: string;
  /**
   * The original browser File.
   *
   * Datasets have always needed it: they stream to /api/upload rather than
   * being base64'd into the chat body. NEW-09 keeps it for images and
   * documents too, and for one reason only — so the card on the sent message
   * can be OPENED. It never changes what is uploaded or how: images still send
   * their (possibly downscaled) base64, documents still send theirs, datasets
   * still stream. The handle is handed to lib/attachments' in-memory viewing
   * store and dropped when the tab closes.
   */
  file?: File;
}

// 2026-09-02: 512 MB, ChatGPT-class. Small documents still ride inline as
// base64 (one FileReader pass, zero extra round trips); anything above
// INLINE_DOC_BYTES keeps its File handle and STREAMS to /api/upload on send,
// exactly like a dataset — reading half a gigabyte with readAsDataURL would
// hold ~700 MB of string in the tab before the first byte left it.
const MAX_PDF_BYTES = 512 * 1024 * 1024;
const INLINE_DOC_BYTES = 25 * 1024 * 1024;
// Datasets are streamed to their own endpoint, not base64'd into the chat
// body, so they can be far larger than an image or PDF.
const MAX_DATASET_BYTES = 512 * 1024 * 1024;
const DATASET_SUFFIXES = [
  '.zip', '.tar', '.tar.gz', '.tgz', '.csv', '.tsv', '.parquet',
  '.xlsx', '.json', '.jsonl', '.ndjson',
];

function isDatasetName(name: string): boolean {
  const lower = name.toLowerCase();
  return DATASET_SUFFIXES.some((s) => lower.endsWith(s));
}

interface ComposerProps {
  streaming: boolean;
  disabled?: boolean;
  /**
   * H-01: a send is in flight but the MODEL has not started yet — a dataset is
   * still uploading.
   *
   * Distinct from `streaming` on purpose. `streaming` swaps Send for Stop, and
   * during an upload that button was a lie twice over: no generation had begun,
   * and pressing it did nothing (there is no registered stream to abort). This
   * blocks a second send without claiming the model is running.
   */
  busy?: boolean;
  /** Phase C: live context meter shown next to the send button. */
  meter?: React.ReactNode;
  /** Draft text changes drive the meter's live estimate (debounced). */
  onDraftChange?: (text: string) => void;
  prefs: ChatPrefs;
  onPrefsChange: (next: ChatPrefs) => void;
  onSend: (
    text: string,
    /** Up to MAX_IMAGES images, or exactly one PDF/dataset (2026-08-05). */
    attachments: Attachment[],
    pasted: PastedText[],
  ) => void;
  onStop: () => void;
  /**
   * Salesforce Intelligence Mode: the placeholder while a clarifying question
   * is waiting ("Enter another date range…"). Text already typed is NEVER
   * cleared by this — only the hint changes.
   */
  clarificationPlaceholder?: string;
  /**
   * The Salesforce starter card. Rendered above the input and only while the
   * composer is EMPTY, so it can suggest without ever being in the way.
   */
  starter?: React.ReactNode;
  /**
   * The live clarification panel, rendered INSIDE the composer's own container
   * so the question and the input read as one control.
   *
   * It belongs here rather than in the message list because it is not a
   * message: it is a temporary control that must stay reachable at the bottom
   * of a conversation of any length, must not scroll away mid-answer, and must
   * leave nothing clickable behind once it is answered.
   */
  clarification?: React.ReactNode;
}

export const Composer = forwardRef<ComposerHandle, ComposerProps>(
  function Composer(
    {
      streaming,
      disabled = false,
      busy = false,
      meter,
      onDraftChange,
      prefs,
      onPrefsChange,
      onSend,
      onStop,
      clarificationPlaceholder,
      starter,
      clarification,
    },
    ref,
  ) {
    const [text, setText] = useState('');
    const [attachments, setAttachments] = useState<Attachment[]>([]);
    /** Files still being read/downscaled; a send must wait for them. */
    const [pendingAttach, setPendingAttach] = useState(0);
    const [pastedTexts, setPastedTexts] = useState<PastedText[]>([]);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const pasteSeq = useRef(0);
    /** Armed by `prefill`; consumed by the effect that runs after `text` lands. */
    const caretToEnd = useRef(false);
    const { toast } = useToast();

    useImperativeHandle(ref, () => ({
      focus: () => textareaRef.current?.focus(),
      /**
       * Focus the input and append `seed`.
       *
       * The clarification panel takes focus when a question appears, so the
       * number keys work immediately. `seed` is what makes that safe: the
       * first letter of a typed answer arrives here instead of being swallowed
       * by a button that only understands digits.
       */
      insert: (seed = '') => {
        const ta = textareaRef.current;
        ta?.focus();
        if (!seed) return;
        setText((prev) => {
          const next = prev + seed;
          onDraftChange?.(next);
          return next;
        });
      },
      prefill: (seed: string) => {
        if (!seed) return;
        setText((prev) => {
          // Empty box: the text IS the draft. Occupied box: keep what is
          // there and start the loaded text on its own paragraph.
          const next = prev.trim() ? `${prev.replace(/\s+$/, '')}\n\n${seed}` : seed;
          onDraftChange?.(next);
          return next;
        });
        // Focus and caret are deferred to the effect below: `value` is React
        // state, so the textarea does not hold the new text until after this
        // render and setting the range here would clamp to the OLD length.
        caretToEnd.current = true;
      },
      acceptFiles,
    }));

    const autogrow = useCallback(() => {
      const ta = textareaRef.current;
      if (!ta) return;
      ta.style.height = 'auto';
      const max = LINE_HEIGHT * MAX_ROWS;
      ta.style.height = `${Math.min(ta.scrollHeight, max)}px`;
      ta.style.overflowY = ta.scrollHeight > max ? 'auto' : 'hidden';
    }, []);

    useEffect(autogrow, [text, autogrow]);

    // After `prefill` lands: focus the input and put the caret after the text
    // that was just loaded, so typing continues the prompt rather than
    // inserting at position 0.
    useEffect(() => {
      if (!caretToEnd.current) return;
      caretToEnd.current = false;
      const ta = textareaRef.current;
      if (!ta) return;
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    }, [text]);

    const hasContent = Boolean(
      text.trim() || attachments.length || pastedTexts.length,
    );

    function submit() {
      const trimmed = text.trim();
      if (streaming || disabled || busy) return;
      // An attachment is still being prepared — sending now would post the
      // message without it.
      if (pendingAttach > 0) return;
      if (!trimmed && attachments.length === 0 && pastedTexts.length === 0)
        return;
      onSend(trimmed, attachments, pastedTexts);
      setText('');
      onDraftChange?.(''); // the draft is gone — drop it from the meter
      setAttachments([]);
      setPastedTexts([]);
    }

    function handleFile(file: File) {
      const isImage = file.type.startsWith('image/');
      const lower = file.name.toLowerCase();
      // 2026-08-07: .docx/.txt/.md ride the document path too — the server
      // sniffs the real format (PDF magic vs zip-with-word/document.xml vs
      // plain text), so they share the `pdf` wire field and the 25 MB cap.
      const isPdf =
        file.type === 'application/pdf' ||
        lower.endsWith('.pdf') ||
        lower.endsWith('.docx') ||
        lower.endsWith('.txt') ||
        lower.endsWith('.md');
      // 2026-09-02: archives open on the DOCUMENT rail — the server unzips
      // them (same hardened extractor the dataset rail trusts), reads every
      // text-bearing member, attaches images found inside, and lists the
      // rest — ChatGPT-style. .docx/.xlsx ARE zip containers; the extension
      // has already claimed those for their own paths above.
      const isArchive =
        !isImage && !isPdf && /\.(zip|tar|tar\.gz|tgz)$/.test(lower);
      const isDataset =
        !isImage && !isPdf && !isArchive && isDatasetName(file.name);
      // Anything that is none of the above — code, logs, unknown binaries —
      // is ALSO a document now ("upload anything"): the server reads text
      // honestly and names binaries instead of rejecting them at the door.
      const isOtherDoc = !isImage && !isPdf && !isArchive && !isDataset;
      const limit = isImage ? MAX_IMAGE_BYTES : isDataset ? MAX_DATASET_BYTES : MAX_PDF_BYTES;
      if (file.size > limit) {
        const mb = (file.size / (1024 * 1024)).toFixed(1);
        const cap = isImage ? '10 MB' : '512 MB';
        toast(
          `${file.name || 'That file'} is ${mb} MB — the limit is ${cap}.`,
          'error',
        );
        return;
      }
      if (isArchive || isOtherDoc) {
        // Streamed like a big document: File handle only, referenced on send.
        setAttachments([
          { name: file.name, kind: 'pdf', dataUrl: '', base64: '', file },
        ]);
        return;
      }
      if (isDataset) {
        // Never read a 512 MB archive into memory: keep the File handle and
        // stream it to /api/upload when the message is sent. A dataset (like
        // a PDF) stands alone — it replaces whatever was attached.
        setAttachments([
          { name: file.name, kind: 'dataset', dataUrl: '', base64: '', file },
        ]);
        return;
      }
      if (isPdf && file.size > INLINE_DOC_BYTES) {
        // A BIG document takes the dataset's road: File handle only, no
        // base64, streamed on send and referenced in the chat request.
        setAttachments([
          { name: file.name, kind: 'pdf', dataUrl: '', base64: '', file },
        ]);
        return;
      }
      if (
        !isPdf &&
        attachments.filter((a) => a.kind === 'image').length >= MAX_IMAGES
      ) {
        toast(`You can attach up to ${MAX_IMAGES} images.`, 'error');
        return;
      }
      const name =
        file.name && file.name.trim()
          ? file.name
          : `pasted-image-${Date.now()}.${imageExtFromMime(file.type)}`;
      const attach = (dataUrl: string) => {
        const att: Attachment = {
          name,
          kind: isPdf ? 'pdf' : 'image',
          dataUrl,
          base64: dataUrl.slice(dataUrl.indexOf(',') + 1),
          // NEW-09: kept so the card on the sent message can be opened. The
          // request payload above is unchanged.
          file,
        };
        setAttachments((prev) => {
          // A PDF stands alone. Images stack up to MAX_IMAGES (2026-08-05)
          // — but never alongside a PDF/dataset, which use different
          // server paths; a new image replaces those instead.
          if (att.kind === 'pdf') return [att];
          const images = prev.filter((a) => a.kind === 'image');
          if (images.length >= MAX_IMAGES) return prev; // raced past the cap
          return [...images, att];
        });
      };
      const readOriginal = () =>
        new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(file);
        });
      // 2026-08-29: images larger than MAX_IMAGE_EDGE on the long edge are
      // downscaled in the browser before they become base64 — fewer prompt
      // tokens, smaller upload, faster first token; `null` = send as is.
      // Decoding + re-encoding a 4K screenshot takes tens of milliseconds, so
      // the attachment is counted as pending until it lands: `submit()` and
      // the Send button wait for it instead of posting the message without
      // the image (the classic Ctrl+V-then-Enter race).
      setPendingAttach((n) => n + 1);
      void (async () => {
        try {
          const scaled = isPdf ? null : await downscaleImageFile(file);
          attach(scaled ? scaled.dataUrl : await readOriginal());
        } catch {
          try {
            attach(await readOriginal());
          } catch {
            /* unreadable file: nothing to attach, exactly as before */
          }
        } finally {
          setPendingAttach((n) => Math.max(0, n - 1));
        }
      })();
    }

    /**
     * The ONE way a batch of files becomes attachments (NEW-10).
     *
     * Every entry point lands here — the "+" menu's hidden input, a
     * drag-and-drop onto the conversation column, and anything added later —
     * so there is exactly one place that decides what is allowed and exactly
     * one set of words for refusing it.
     *
     * The image room is counted HERE, synchronously, rather than inside
     * `handleFile`: that function's own cap check reads `attachments`, which
     * does not update until the whole batch has been dispatched, so a
     * six-image drop would otherwise sail past a five-image limit.
     */
    function acceptFiles(incoming: File[]) {
      if (!incoming.length) return;
      // A drop must not be able to do what the picker cannot.
      //
      // `streaming` is the load-bearing one: composerMenuItems marks "Add
      // photos & files" disabled mid-stream, so accepting a dropped file then
      // would be a second entry point quietly overruling the first — the exact
      // divergence this shared function exists to prevent. `disabled` is a chat
      // still being restored; `busy` is a dataset already uploading, where a
      // second file would race the request that owns the turn.
      if (streaming || disabled || busy) {
        toast(
          streaming
            ? 'Wait for the answer to finish — you can’t attach a file right now.'
            : disabled
              ? 'This chat is still loading — you can’t attach a file right now.'
              : 'An upload is already in progress — you can’t attach a file right now.',
          'error',
        );
        return;
      }
      let room =
        MAX_IMAGES - attachments.filter((a) => a.kind === 'image').length;
      let dropped = 0;
      for (const f of incoming) {
        if (f.type.startsWith('image/')) {
          if (room <= 0) {
            dropped += 1;
            continue;
          }
          room -= 1;
        }
        handleFile(f);
      }
      if (dropped > 0) {
        toast(
          `You can attach up to ${MAX_IMAGES} images — ${dropped} ${dropped === 1 ? 'file was' : 'files were'} left out.`,
          'error',
        );
      }
    }

    // Paste into the composer: an image blob becomes the attachment; a long
    // block of text/code becomes a "PASTED" chip; short text pastes normally.
    function handlePaste(e: ReactClipboardEvent<HTMLTextAreaElement>) {
      const dt = e.clipboardData;
      if (!dt) return;
      const imageItem = Array.from(dt.items).find(
        (it) => it.kind === 'file' && it.type.startsWith('image/'),
      );
      if (imageItem) {
        const file = imageItem.getAsFile();
        if (file) {
          e.preventDefault();
          handleFile(file);
          return;
        }
      }
      const clip = dt.getData('text/plain');
      if (clip && shouldAttachPaste(clip)) {
        e.preventDefault();
        pasteSeq.current += 1;
        const id = `paste-${Date.now()}-${pasteSeq.current}`;
        setPastedTexts((prev) => [...prev, makePastedText(clip, id)]);
      }
    }

    // The bottom padding clears the home indicator on a phone. `max()` rather
    // than an add: on every other device the inset is 0, and the spacing must
    // stay exactly what it was.
    return (
      <div className="bg-bg px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-2">
        <div className="mx-auto w-full max-w-thread">
          {/* Only while the composer is genuinely empty. A suggestion strip
              sitting above half-typed text is clutter, not help — and toggling
              Salesforce must never disturb what someone is writing. */}
          {!clarification && starter && !hasContent && starter}
          {(attachments.length > 0 || pastedTexts.length > 0) && (
            <div className="mb-2 flex flex-wrap items-start gap-2">
              {attachments.map((attachment, idx) => (
                <div
                  key={`${attachment.name}-${idx}`}
                  className="inline-flex items-center gap-2 rounded-ts border border-border bg-surface p-1.5 pr-2"
                >
                  {attachment.kind === 'pdf' || attachment.kind === 'dataset' ? (
                    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-md bg-danger/15 text-danger">
                      <IconFileText size={18} />
                    </span>
                  ) : (
                    /* eslint-disable-next-line @next/next/no-img-element */
                    <img
                      src={attachment.dataUrl}
                      alt={`Attached: ${attachment.name}`}
                      className="h-10 w-10 rounded-md border border-border object-cover"
                    />
                  )}
                  <span className="flex flex-col">
                    <span className="max-w-[200px] truncate text-xs text-ink">
                      {attachment.name}
                    </span>
                    {attachment.kind !== 'image' && (
                      <span className="text-[10px] uppercase tracking-wide text-faint">
                        {attachment.kind === 'pdf' ? 'PDF' : 'DATASET'}
                      </span>
                    )}
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      setAttachments((prev) => prev.filter((_, i) => i !== idx))
                    }
                    aria-label={`Remove attachment ${attachment.name}`}
                    className="rounded-md p-1 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                  >
                    <IconX size={13} />
                  </button>
                </div>
              ))}
              {pastedTexts.map((p) => (
                <PastedChip
                  key={p.id}
                  pasted={p}
                  onRemove={() =>
                    setPastedTexts((prev) => prev.filter((x) => x.id !== p.id))
                  }
                />
              ))}
            </div>
          )}

          {/* One clean rounded container — no inner box, no divider line
              (ChatGPT-style composer, owner request 2026-07-23). A live
              clarification joins it ABOVE the input, inside the same surface
              and behind the same corners, so the question and the answer field
              read as one control rather than a card that happens to be nearby.

              NEVER put `overflow-hidden` on this element. Both popups anchored
              inside it — the effort picker and the "+" menu — open UPWARD with
              `absolute bottom-full`, so clipping the box decapitates them: the
              effort list rendered with "Fast" and "Low" sheared off. Nothing
              here needs clipping anyway. The clarification panel has no
              background and no corners of its own (it inherits this surface),
              and its divider is a horizontal rule in the MIDDLE of the box,
              nowhere near the rounded corners. */}
          <div className="rounded-[26px] bg-surface">
            {clarification}
            <div className="px-4 py-3">
            <textarea
              ref={textareaRef}
              value={text}
              rows={1}
              onChange={(e) => {
                setText(e.target.value);
                onDraftChange?.(e.target.value);
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  submit();
                }
              }}
              onPaste={handlePaste}
              disabled={disabled}
              placeholder={
                disabled
                  ? 'Restoring this chat…'
                  : // A waiting question changes only the HINT. The text
                    // already in the box is untouched, so someone who was
                    // mid-sentence when the card appeared loses nothing.
                    (clarificationPlaceholder ??
                    (prefs.salesforce
                      ? 'Ask about your Salesforce data…'
                      : 'Ask anything…'))
              }
              aria-label="Message"
              className="max-h-[240px] min-h-[24px] w-full resize-none bg-transparent px-1.5 py-1.5 text-[15px] leading-6 placeholder:text-faint focus:outline-none disabled:cursor-not-allowed"
              style={{ height: 24 }}
            />

            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept="image/*,application/pdf,.pdf,.docx,.txt,.md,.zip,.tar,.tar.gz,.tgz,.csv,.tsv,.parquet,.xlsx,.json,.jsonl,.ndjson"
                className="sr-only"
                aria-hidden
                tabIndex={-1}
                onChange={(e) => {
                  // Multi-select (2026-08-05): each file goes through the
                  // same rules — images stack to MAX_IMAGES, a PDF/dataset
                  // stands alone, oversized files toast individually. Those
                  // rules live in `acceptFiles` (NEW-10) so that a
                  // drag-and-drop runs exactly this code and not a copy of it.
                  acceptFiles(Array.from(e.target.files ?? []));
                  e.target.value = '';
                }}
              />
              {/* ChatGPT-style "+" menu (2026-08-05): Add photos & files ·
                  Web search · Salesforce. It replaces the bare paperclip —
                  the file picker now lives behind its first item. */}
              <AttachMenu
                prefs={prefs}
                streaming={streaming}
                onPrefsChange={onPrefsChange}
                onPickFiles={() => fileInputRef.current?.click()}
              />

              {/* Context meter sits by the "+", effort picker by Send
                  (owner request 2026-08-05 — swapped sides). */}
              {meter}

              {/* Active-tool chips, ChatGPT-style (owner request 2026-08-05):
                  the toggles LIVE in the "+" menu — a pill here appears only
                  while its tool is ON, and clicking it turns the tool off.
                  No pill means off; there is no second always-visible toggle.
                  Turning off goes through activateComposerMenuItem, NOT a
                  bare prefs spread, so the toggle rules stay in one place
                  (lib/composerMenu.ts). */}
              {/* ONE pill per mode (owner request 2026-08-06): "Salesforce"
                  and "Live Salesforce" are the same source — the org — so
                  showing two pills for Live read as two selections. Synced
                  mode shows ☁ Salesforce; Live swaps it for ✨ Live
                  Salesforce. Closing the Live pill steps DOWN to synced
                  mode (not all the way off) — one × per level. */}
              {prefs.salesforce && !prefs.sfLive && (
                <button
                  type="button"
                  onClick={() => {
                    const out = activateComposerMenuItem('salesforce', prefs);
                    if (out.kind === 'prefs') onPrefsChange(out.prefs);
                  }}
                  aria-pressed
                  title="Salesforce mode is on — answers come from your synced data. Click to turn it off."
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-accent/50 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent transition-colors duration-ts"
                >
                  <IconCloud size={13} />
                  Salesforce
                  <IconX size={11} />
                </button>
              )}

              {prefs.salesforce && prefs.sfLive && (
                <button
                  type="button"
                  onClick={() => {
                    const out = activateComposerMenuItem('sf-live', prefs);
                    if (out.kind === 'prefs') onPrefsChange(out.prefs);
                    textareaRef.current?.focus();
                  }}
                  aria-pressed
                  title="Live Salesforce is on — every answer queries your org directly. Click to use the synced copy again."
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-accent/50 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent transition-colors duration-ts"
                >
                  <IconSparkles size={13} />
                  Live Salesforce
                  <IconX size={11} />
                </button>
              )}

              {/* The always-visible Web search toggle was REMOVED
                  (2026-07-28) — by default the level decides ("auto"). The
                  "+" menu (2026-08-05) reintroduced an explicit FORCE
                  ("on"), available only while Salesforce is OFF: Salesforce
                  mode never searches the web, so the pill (like the menu
                  item) exists only outside it. While forced, this pill shows
                  the active tool, ChatGPT-style; clicking returns to auto. */}
              {!prefs.salesforce && prefs.webSearch === 'on' && (
                <button
                  type="button"
                  onClick={() => {
                    onPrefsChange({ ...prefs, webSearch: 'auto' });
                    // This click unmounts the pill — without a handoff,
                    // keyboard focus silently drops to <body>.
                    textareaRef.current?.focus();
                  }}
                  aria-pressed
                  title="Web search is forced on — click to let the model decide again"
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-accent/50 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent transition-colors duration-ts"
                >
                  <IconGlobe size={13} />
                  Web search
                  <IconX size={11} />
                </button>
              )}

              {/* Deep Research (2026-08-30). Like the Web search pill this
                  appears only while the tool is ON, and only outside
                  Salesforce mode — research IS web work. It says "next
                  answer" because the pref is deliberately one-shot: a
                  multi-minute report the user forgot they armed would be a
                  worse surprise than an extra click. */}
              {!prefs.salesforce && prefs.deepResearch && (
                <button
                  type="button"
                  onClick={() => {
                    onPrefsChange({ ...prefs, deepResearch: false });
                    // Same focus handoff as the pills above: this click
                    // unmounts the button it is on.
                    textareaRef.current?.focus();
                  }}
                  aria-pressed
                  title="The next answer will be a researched, cited report — click to cancel"
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-accent/50 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent transition-colors duration-ts"
                >
                  <IconBook size={13} />
                  Deep research
                  <IconX size={11} />
                </button>
              )}

              {/* The Agent toggle was REMOVED (2026-07-28). Deciding when a
                  request needs multi-step work is the model's job, not a
                  switch the user has to understand: at medium/high effort the
                  orchestrator classifies each request and escalates by itself.
                  Escalation stays visible through the step timeline and the
                  status line, so it is automatic, not hidden. */}

              <span className="ml-auto flex items-center gap-1.5">
                <ModelPicker
                  model={prefs.model}
                  effort={prefs.effort}
                  onChange={(model, effort) =>
                    onPrefsChange({ ...prefs, model, effort })
                  }
                />
                {streaming ? (
                  <button
                    type="button"
                    onClick={onStop}
                    aria-label="Stop generating"
                    title="Stop (Esc)"
                    className="shrink-0 rounded-lg bg-surface-2 p-2 text-ink transition-colors duration-ts hover:bg-border"
                  >
                    <IconStop size={17} />
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={submit}
                    disabled={disabled || busy || !hasContent || pendingAttach > 0}
                    aria-label="Send message"
                    title="Send (Enter)"
                    className="shrink-0 rounded-lg bg-accent-strong p-2 text-white transition-all duration-ts hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-35"
                  >
                    <IconSend size={17} />
                  </button>
                )}
              </span>
            </div>
            </div>
          </div>

          <p
            className={`mt-2 text-center text-xs transition-opacity duration-ts ${
              // Dimming marks the RELAXED state (Salesforce off, model may
              // search). With search FORCED on this line is the strongest
              // internet warning shown anywhere — never dim that one.
              prefs.salesforce || prefs.webSearch === 'on'
                ? 'text-faint'
                : 'text-faint opacity-50'
            }`}
          >
            {/* This line is the only place the privacy promise is made, and
                it must track BOTH toggles: Salesforce ON blocks AUTO search,
                but the "+" menu can force search on, and then "nothing
                leaves this machine" would be untrue. The wording lives in
                lib/composerMenu.ts so it is unit-tested. */}
            {trustLine(prefs)}
          </p>
        </div>
      </div>
    );
  },
);
