'use client';

/**
 * Pinned composer (§9 + V2 §4c): auto-growing textarea (1→10 rows),
 * paperclip image upload (png/jpg ≤10 MB) with thumbnail chip + remove, and
 * a ChatGPT-style controls row — Salesforce toggle pill (cloud icon, ON by
 * default) and the effort picker (Fast/Low/Medium/High on the one model) —
 * plus the send button that morphs to Stop while streaming. There is no Agent
 * toggle: the model decides when to plan steps or search (2026-07-28).
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
import {
  imageExtFromMime,
  makePastedText,
  shouldAttachPaste,
} from '@/lib/pasted';
import type { PastedText } from '@/lib/types';
import { ModelPicker } from './ModelPicker';
import { PastedChip } from './PastedChip';
import { useToast } from './Providers';
import {
  IconCloud,
  IconFileText,
  IconPaperclip,
  IconSend,
  IconStop,
  IconX,
} from './icons';

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const LINE_HEIGHT = 24;
const MAX_ROWS = 10;

export interface ComposerHandle {
  focus: () => void;
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
  /** Datasets keep the File itself: they stream to /api/upload, never base64. */
  file?: File;
}

const MAX_PDF_BYTES = 25 * 1024 * 1024;
// Datasets are streamed to their own endpoint, not base64'd into the chat
// body, so they can be far larger than an image or PDF.
const MAX_DATASET_BYTES = 200 * 1024 * 1024;
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
  /** Phase C: live context meter shown next to the send button. */
  meter?: React.ReactNode;
  /** Draft text changes drive the meter's live estimate (debounced). */
  onDraftChange?: (text: string) => void;
  prefs: ChatPrefs;
  onPrefsChange: (next: ChatPrefs) => void;
  onSend: (
    text: string,
    attachment: Attachment | null,
    pasted: PastedText[],
  ) => void;
  onStop: () => void;
}

export const Composer = forwardRef<ComposerHandle, ComposerProps>(
  function Composer(
    {
      streaming,
      disabled = false,
      meter,
      onDraftChange,
      prefs,
      onPrefsChange,
      onSend,
      onStop,
    },
    ref,
  ) {
    const [text, setText] = useState('');
    const [attachment, setAttachment] = useState<Attachment | null>(null);
    const [pastedTexts, setPastedTexts] = useState<PastedText[]>([]);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const pasteSeq = useRef(0);
    const { toast } = useToast();

    useImperativeHandle(ref, () => ({
      focus: () => textareaRef.current?.focus(),
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

    const hasContent = Boolean(
      text.trim() || attachment || pastedTexts.length,
    );

    function submit() {
      const trimmed = text.trim();
      if (streaming || disabled) return;
      if (!trimmed && !attachment && pastedTexts.length === 0) return;
      onSend(trimmed, attachment, pastedTexts);
      setText('');
      onDraftChange?.(''); // the draft is gone — drop it from the meter
      setAttachment(null);
      setPastedTexts([]);
    }

    function handleFile(file: File) {
      const isImage = file.type.startsWith('image/');
      const isPdf =
        file.type === 'application/pdf' ||
        file.name.toLowerCase().endsWith('.pdf');
      const isDataset = !isImage && !isPdf && isDatasetName(file.name);
      if (!isImage && !isPdf && !isDataset) {
        const ext = file.name.split('.').pop()?.toUpperCase() ?? 'that type';
        toast(
          `${file.name || 'That file'} is ${ext} — attach an image, a PDF, or a dataset (.zip, .csv, .xlsx, .parquet).`,
          'error',
        );
        return;
      }
      const limit = isDataset
        ? MAX_DATASET_BYTES
        : isPdf
          ? MAX_PDF_BYTES
          : MAX_IMAGE_BYTES;
      if (file.size > limit) {
        const mb = (file.size / (1024 * 1024)).toFixed(1);
        const cap = isDataset ? '200 MB' : isPdf ? '25 MB' : '10 MB';
        toast(
          `${file.name || 'That file'} is ${mb} MB — the limit is ${cap}.`,
          'error',
        );
        return;
      }
      if (isDataset) {
        // Never read a 200 MB archive into memory: keep the File handle and
        // stream it to /api/upload when the message is sent.
        setAttachment({
          name: file.name,
          kind: 'dataset',
          dataUrl: '',
          base64: '',
          file,
        });
        return;
      }
      const name =
        file.name && file.name.trim()
          ? file.name
          : `pasted-image-${Date.now()}.${imageExtFromMime(file.type)}`;
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = String(reader.result);
        setAttachment({
          name,
          kind: isPdf ? 'pdf' : 'image',
          dataUrl,
          base64: dataUrl.slice(dataUrl.indexOf(',') + 1),
        });
      };
      reader.readAsDataURL(file);
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

    return (
      <div className="bg-bg px-4 pb-3 pt-2">
        <div className="mx-auto w-full max-w-thread">
          {(attachment || pastedTexts.length > 0) && (
            <div className="mb-2 flex flex-wrap items-start gap-2">
              {attachment && (
                <div className="inline-flex items-center gap-2 rounded-ts border border-border bg-surface p-1.5 pr-2">
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
                    onClick={() => setAttachment(null)}
                    aria-label={`Remove attachment ${attachment.name}`}
                    className="rounded-md p-1 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                  >
                    <IconX size={13} />
                  </button>
                </div>
              )}
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
              (ChatGPT-style composer, owner request 2026-07-23). */}
          <div className="rounded-[26px] bg-surface px-4 py-3">
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
                  : prefs.salesforce
                    ? 'Ask about your Salesforce data…'
                    : 'Ask anything…'
              }
              aria-label="Message"
              className="max-h-[240px] min-h-[24px] w-full resize-none bg-transparent px-1.5 py-1.5 text-[15px] leading-6 placeholder:text-faint focus:outline-none disabled:cursor-not-allowed"
              style={{ height: 24 }}
            />

            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*,application/pdf,.pdf,.zip,.tar,.tar.gz,.tgz,.csv,.tsv,.parquet,.xlsx,.json,.jsonl,.ndjson"
                className="sr-only"
                aria-hidden
                tabIndex={-1}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) handleFile(f);
                  e.target.value = '';
                }}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                aria-label="Attach an image or PDF"
                title="Attach image or PDF"
                disabled={streaming}
                className="shrink-0 rounded-lg p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:opacity-40"
              >
                <IconPaperclip size={16} />
              </button>

              {/* Salesforce toggle (V2 §4c) — ON is the v1 behavior. */}
              <button
                type="button"
                onClick={() =>
                  onPrefsChange({ ...prefs, salesforce: !prefs.salesforce })
                }
                aria-pressed={prefs.salesforce}
                title="Answers computed from synced Salesforce data"
                className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors duration-ts ${
                  prefs.salesforce
                    ? 'border-accent/50 bg-accent/10 text-accent'
                    : 'border-border text-faint hover:bg-surface-2 hover:text-ink'
                }`}
              >
                <IconCloud size={13} />
                Salesforce
              </button>

              {/* The Web search toggle was REMOVED (2026-07-28), like the
                  Agent toggle below it. Both are now decided by the level:
                  Fast never searches, Low/Medium/High search when the question
                  needs it, and Medium/High also plan multi-step work. One
                  control instead of three, and no way to pick a combination
                  that contradicts itself. */}

              <ModelPicker
                model={prefs.model}
                effort={prefs.effort}
                onChange={(model, effort) =>
                  onPrefsChange({ ...prefs, model, effort })
                }
              />

              {/* The Agent toggle was REMOVED (2026-07-28). Deciding when a
                  request needs multi-step work is the model's job, not a
                  switch the user has to understand: at medium/high effort the
                  orchestrator classifies each request and escalates by itself.
                  Escalation stays visible through the step timeline and the
                  status line, so it is automatic, not hidden. */}

              <span className="ml-auto flex items-center gap-1.5">
                {meter}
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
                    disabled={disabled || !hasContent}
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

          <p
            className={`mt-2 text-center text-xs transition-opacity duration-ts ${
              prefs.salesforce ? 'text-faint' : 'text-faint opacity-50'
            }`}
          >
            {/* Salesforce ON means the web is NOT used at any level — the
                server refuses to auto-search in that mode, and the agent's
                web steps are downgraded. Saying "web search is on" here was
                simply untrue, and this line is the only place the privacy
                promise is made. */}
            {prefs.salesforce
              ? 'Answers come from your synced Salesforce data · no web search · nothing leaves this machine.'
              : 'Salesforce is off — answers may use the web, and search queries are sent to the internet.'}
          </p>
        </div>
      </div>
    );
  },
);
