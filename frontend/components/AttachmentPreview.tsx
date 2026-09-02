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
  useRef,
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
import {
  cellText,
  tableFromDelimited,
  type DocumentText,
  type TablePreview,
  type WorkbookPreview,
} from '@/lib/previewData';
import { formatBytes } from '@/lib/format';
import { IconX } from './icons';

/**
 * PHASE 4C — the formats a browser cannot open on its own.
 *
 * `previewKindFor` is untouched and still answers `none` for these: it decides
 * what can be rendered FROM BYTES, and a .xlsx/.docx still cannot be. What
 * changed is that bytes stopped being the only source. The orchestrator
 * profiled the workbook and extracted the document's text when the file was
 * uploaded, so the dialog asks IT instead — which is also why these previews
 * work on a device that never held the file.
 *
 * A loader is optional at every call site. Without one (a row rendered with no
 * conversation behind it — previews, tests) the honest "no preview" card is
 * exactly what it always was.
 */
export interface ServerPreviewLoaders {
  loadWorkbook?: (signal: AbortSignal) => Promise<WorkbookPreview | null>;
  loadDocumentText?: (signal: AbortSignal) => Promise<DocumentText | null>;
}

export function AttachmentPreview({
  source,
  onClose,
  loadWorkbook,
  loadDocumentText,
}: {
  source: ResolvedAttachment;
  onClose: () => void;
} & ServerPreviewLoaders) {
  /** Only ever set for the two kinds that render from a URL. */
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [failed, setFailed] = useState(false);
  const [workbook, setWorkbook] = useState<WorkbookPreview | null>(null);
  const [docText, setDocText] = useState<DocumentText | null>(null);
  /** Only while a server-backed preview is in flight. */
  const [loading, setLoading] = useState(false);

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

  /**
   * The server-backed previews (4C): a workbook's stored profile, a document's
   * extracted text.
   *
   * Reached only when the byte-based classifier already said `none`, so it can
   * never override or weaken it — an executable format still renders nothing,
   * because no loader is ever offered for one.
   *
   * The fetch is ABORTED on close. A dialog that is gone must not keep pulling
   * a large profile, and must not call setState afterwards either.
   */
  // The loaders are closures rebuilt on every render of the row that owns this
  // dialog, so they are held in a ref and NOT depended on. Listing them would
  // re-run the fetch on every parent render — which, in a chat, means once per
  // streamed token.
  const loaders = useRef({ loadWorkbook, loadDocumentText });
  loaders.current = { loadWorkbook, loadDocumentText };
  const wantsWorkbook = Boolean(loadWorkbook);
  const wantsDocText = Boolean(loadDocumentText);

  useEffect(() => {
    // `unavailable` counts: after a reload there are no bytes to classify, and
    // a workbook profile or a document's text is exactly what still exists.
    if (kind !== 'none' && kind !== 'unavailable') return;
    if (!wantsWorkbook && !wantsDocText) return;
    const controller = new AbortController();
    let alive = true;
    setLoading(true);
    void (async () => {
      try {
        const { loadWorkbook: wb, loadDocumentText: dt } = loaders.current;
        if (wb) {
          const found = await wb(controller.signal);
          if (alive) setWorkbook(found);
        } else if (dt) {
          const found = await dt(controller.signal);
          if (alive) setDocText(found);
        }
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
      controller.abort();
    };
  }, [kind, wantsWorkbook, wantsDocText]);

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
            workbook={workbook}
            docText={docText}
            loading={loading}
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
  workbook,
  docText,
  loading,
}: {
  source: ResolvedAttachment;
  objectUrl: string | null;
  text: string | null;
  truncated: boolean;
  failed: boolean;
  workbook: WorkbookPreview | null;
  docText: DocumentText | null;
  loading: boolean;
}) {
  const { kind, name } = source;

  if (failed) {
    // A failed preview is a message, never a download. That fallback is what
    // put files on disk without anyone asking.
    return <p className={NOTE}>Unable to preview this file.</p>;
  }

  // 4C: a server-backed preview outranks "no bytes" — the profile and the
  // extracted text are database rows, and they outlive the file itself.
  if (loading) return <p className={NOTE}>Loading preview…</p>;
  if (workbook) return <WorkbookView workbook={workbook} />;
  if (docText) {
    return (
      <>
        {docText.truncated && (
          <p className="mb-2 text-xs text-faint">
            Preview truncated — showing the beginning of the document.
          </p>
        )}
        {/* The document's own words, as React text in a <pre>. There is no
            HTML on this path at any point — not from the extractor, not from
            the API, and certainly not here. */}
        <pre className="whitespace-pre-wrap break-words text-xs leading-relaxed text-ink">
          {docText.text}
        </pre>
      </>
    );
  }

  if (/\.(xls|doc)$/i.test(name)) {
    // Named specifically rather than lumped in below: the fix is concrete and
    // the user can act on it, which a generic refusal does not tell them.
    return (
      <p className={NOTE}>
        Legacy {fileBadgeFor(name)} files can’t be previewed. Save the file as{' '}
        {/\.xls$/i.test(name) ? '.xlsx' : '.docx'} and attach it again.
      </p>
    );
  }

  if (kind === 'loading') return <p className={NOTE}>Loading preview…</p>;

  if (kind === 'expired') {
    // Distinct from `unavailable` on purpose: the server HAD this file and its
    // workspace TTL swept it, which is a fact the user can act on.
    return (
      <p className={NOTE}>
        This upload has expired and is no longer stored. Attach the file again
        to preview it.
      </p>
    );
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
    // 4C: delimited data reads as a TABLE. A CSV shown as raw text is
    // technically its contents and practically unreadable past four columns.
    // Everything else in the text family (.txt, .md, .json) keeps the <pre> —
    // for those, the raw form IS the content.
    const table =
      text !== null && /\.(csv|tsv)$/i.test(name)
        ? tableFromDelimited(text, name, truncated)
        : null;
    if (table) return <DelimitedTable table={table} />;
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

/* ------------------------------------------------------------- 4C views */

const CELL =
  'max-w-[22rem] truncate border-b border-border px-2 py-1 text-left align-top';

/**
 * A bounded table. Every value goes through `cellText` and lands in a React
 * text node — an uploaded file's contents never become markup, which is the
 * same rule the rest of this dialog follows.
 *
 * The wrapper scrolls on BOTH axes rather than letting the page do it: a
 * forty-column export must not widen the modal past the viewport.
 */
function DataTablePreview({
  columns,
  rows,
  caption,
}: {
  columns: string[];
  rows: string[][];
  caption: string;
}) {
  return (
    <>
      <p className="mb-2 text-xs text-faint">{caption}</p>
      <div className="max-h-[60dvh] overflow-auto rounded-ts border border-border">
        <table className="w-full border-collapse text-xs">
          <thead className="sticky top-0 bg-surface-2">
            <tr>
              {columns.map((c, i) => (
                <th key={i} scope="col" className={`${CELL} font-semibold text-ink`}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, r) => (
              <tr key={r}>
                {row.map((cell, c) => (
                  <td key={c} className={`${CELL} text-muted`}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function DelimitedTable({ table }: { table: TablePreview }) {
  const parts = [`${table.shownRows} row${table.shownRows === 1 ? '' : 's'} shown`];
  if (table.truncatedRows) parts.push('the file continues past this preview');
  if (table.truncatedColumns) parts.push('some columns are not shown');
  return (
    <DataTablePreview
      columns={table.columns}
      rows={table.rows}
      caption={parts.join(' · ')}
    />
  );
}

/**
 * A workbook, from the profile the server stored at upload time.
 *
 * The caption is the honest part and the reason the `complete` flag is carried
 * this far: for a small file the profile holds EVERY row, and for a large one
 * it holds a handful of sample rows out of hundreds. Showing five rows of a
 * 995-row sheet without saying so would be a quietly false preview.
 */
function WorkbookView({ workbook }: { workbook: WorkbookPreview }) {
  const [active, setActive] = useState(0);
  const sheet = workbook.sheets[Math.min(active, workbook.sheets.length - 1)];
  if (!sheet) return <p className={NOTE}>This workbook has no readable sheets.</p>;

  const shown = sheet.previewRows.length;
  const caption = sheet.complete
    ? `${shown} row${shown === 1 ? '' : 's'} — the complete sheet`
    : `Showing ${shown} preview row${shown === 1 ? '' : 's'} of ${sheet.rows} rows`;

  return (
    <>
      {workbook.sheets.length > 1 && (
        // Tabs, in the app's existing pill language. Only when there is a
        // choice to make — one sheet needs no tab strip.
        <div role="tablist" aria-label="Sheets" className="mb-3 flex flex-wrap gap-1.5">
          {workbook.sheets.map((s, i) => (
            <button
              key={`${s.name}-${i}`}
              role="tab"
              type="button"
              aria-selected={i === active}
              onClick={() => setActive(i)}
              className={`rounded-full border px-2.5 py-1 text-xs font-medium transition-colors duration-ts ${
                i === active
                  ? 'border-accent/50 bg-accent/10 text-accent'
                  : 'border-border text-muted hover:bg-surface-2 hover:text-ink'
              }`}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      <DataTablePreview
        columns={sheet.columns}
        rows={sheet.previewRows.map((row) =>
          sheet.columns.map((c) => cellText(row[c])),
        )}
        caption={caption}
      />
    </>
  );
}
