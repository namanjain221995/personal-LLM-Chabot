'use client';

/**
 * NEW-09A: the in-app preview for an attachment on a sent message.
 *
 * This dialog exists because the browser is not steerable and this is.
 *
 * The previous fix previewed by navigating a new tab to a `blob:` URL. Chrome
 * treats that as a download instruction for every type it cannot render inline,
 * so "preview" and "download" were the same gesture with different outcomes
 * depending on the file — and the formats it could not render were downloaded
 * on purpose, through a synthesised `<a download>`. Manual testing found files
 * landing in the Downloads tray from a click that promised to open them.
 *
 * So nothing in this component navigates, opens a tab, or saves a file. There
 * is no anchor, no `download` attribute, no `application/octet-stream`, and no
 * fallback that turns a failed preview into a saved file. A format we cannot
 * render gets an honest card naming it; a file whose bytes are gone gets an
 * honest sentence; both stay inside the app.
 *
 * The portal, the z-index, the panel chrome and the Escape handling follow
 * SettingsDialog, which is the app's established modal recipe — this adds no
 * new visual language of its own.
 */

import {
  useEffect,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { createPortal } from 'react-dom';
import {
  fileBadgeFor,
  MAX_TEXT_PREVIEW_BYTES,
  previewMimeFor,
  readBlobText,
  type ResolvedAttachment,
} from '@/lib/attachments';
import { formatBytes } from '@/lib/format';
import { IconX } from './icons';

export function AttachmentPreview({
  source,
  onClose,
}: {
  source: ResolvedAttachment;
  onClose: () => void;
}) {
  /** Only ever set for the two kinds that render from a URL. */
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [failed, setFailed] = useState(false);

  const { blob, kind, name, mime, size } = source;

  /**
   * The object URL lives exactly as long as the preview does.
   *
   * Created when the dialog opens, revoked by this effect's cleanup when it
   * closes — not on a timer, and above all not on the line after it is handed
   * to the renderer, which is the race that produces blank previews.
   */
  useEffect(() => {
    if (!blob || (kind !== 'image' && kind !== 'pdf')) return;
    // Forced from the allowlist rather than copied off the file, so a
    // mislabelled upload cannot choose how it is rendered.
    const safeMime = previewMimeFor(name, mime) ?? blob.type;
    let url: string;
    try {
      url = URL.createObjectURL(new Blob([blob], { type: safeMime }));
    } catch {
      setFailed(true);
      return;
    }
    setObjectUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [blob, kind, name, mime]);

  /**
   * Text is read, not linked — and only the first MAX_TEXT_PREVIEW_BYTES of it.
   * A dropped dataset can be 200 MB; decoding all of it to fill one scroll pane
   * would freeze the tab. The upload itself is untouched by this slice.
   */
  useEffect(() => {
    if (!blob || kind !== 'text') return;
    let alive = true;
    readBlobText(blob, MAX_TEXT_PREVIEW_BYTES).then(
      (body) => {
        if (!alive) return;
        setText(body);
        setTruncated(blob.size > MAX_TEXT_PREVIEW_BYTES);
      },
      () => {
        if (alive) setFailed(true);
      },
    );
    return () => {
      alive = false;
    };
  }, [blob, kind]);

  if (typeof document === 'undefined') return null;

  function onPanelKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose();
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Preview of ${name}`}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onPanelKeyDown}
        className="flex max-h-[85dvh] w-full max-w-3xl flex-col overflow-hidden rounded-ts border border-border bg-surface shadow-2xl"
      >
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            {/* The filename is React text and stays React text. An uploaded
                name is data; it never becomes markup. */}
            <h2 className="truncate text-sm font-semibold text-ink">{name}</h2>
            <p className="mt-0.5 text-xs text-muted">
              {fileBadgeFor(name)}
              {size !== null ? ` · ${formatBytes(size)}` : ''}
            </p>
          </div>
          <button
            // eslint-disable-next-line jsx-a11y/no-autofocus
            autoFocus
            type="button"
            onClick={onClose}
            aria-label="Close preview"
            title="Close preview"
            className="shrink-0 rounded-md p-1 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconX size={15} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          <PreviewBody
            source={source}
            objectUrl={objectUrl}
            text={text}
            truncated={truncated}
            failed={failed}
          />
        </div>
      </div>
    </div>,
    document.body,
  );
}

const NOTE = 'text-sm leading-relaxed text-muted';

function PreviewBody({
  source,
  objectUrl,
  text,
  truncated,
  failed,
}: {
  source: ResolvedAttachment;
  objectUrl: string | null;
  text: string | null;
  truncated: boolean;
  failed: boolean;
}) {
  const { kind, name } = source;

  if (failed) {
    // A failed preview is a message, never a download. That fallback is what
    // put files on disk without anyone asking.
    return <p className={NOTE}>Unable to preview this file.</p>;
  }

  if (kind === 'unavailable') {
    return (
      <p className={NOTE}>
        This file is no longer available in this browser session. Re-attach it
        to preview it.
      </p>
    );
  }

  if (kind === 'image') {
    return objectUrl ? (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={objectUrl}
        alt={name}
        className="mx-auto max-h-[65dvh] max-w-full rounded-ts object-contain"
      />
    ) : null;
  }

  if (kind === 'pdf') {
    return objectUrl ? (
      // <object> over <iframe> for its built-in fallback: when the browser has
      // no PDF viewer it renders the child below instead of prompting a save.
      <object
        data={objectUrl}
        type="application/pdf"
        aria-label={`PDF preview of ${name}`}
        className="h-[65dvh] w-full rounded-ts border border-border"
      >
        <p className={NOTE}>Preview could not be displayed.</p>
      </object>
    ) : null;
  }

  if (kind === 'text') {
    return (
      <>
        {truncated && (
          <p className="mb-2 text-xs text-faint">
            Preview truncated — showing the first {formatBytes(MAX_TEXT_PREVIEW_BYTES)}.
          </p>
        )}
        {/* Plain React text. Never dangerouslySetInnerHTML: this is the
            content of a file someone else may have written. */}
        <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-ink">
          {text ?? ''}
        </pre>
      </>
    );
  }

  return (
    <p className={NOTE}>
      Preview is not available for this file type.
    </p>
  );
}
