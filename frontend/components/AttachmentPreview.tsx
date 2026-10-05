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
 *
 * 2026-10-02 (chat media, CONTRACT §10): a video or audio file now PLAYS
 * here, from the server, by URL — the browser's own <video>/<audio> with
 * `controlsList="nodownload"`, so the one native control that would save the
 * file is not offered either. And a file whose bytes are gone still shows
 * what the server kept of it (a document's text, a spreadsheet's summary),
 * saying plainly that the file itself is not there.
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
  previewKindFor,
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
 *
 * 2026-10-02: a loader is also the FALLBACK for a file that does render from
 * bytes (a PDF, a text file) once those bytes turn out to be gone. It runs
 * only then — the dialog never asks for the text of a PDF it can show.
 */
export interface ServerPreviewLoaders {
  loadWorkbook?: (signal: AbortSignal) => Promise<Kept<WorkbookPreview>>;
  loadDocumentText?: (signal: AbortSignal) => Promise<Kept<DocumentText>>;
}

/**
 * What a server-backed loader found (2026-10-02).
 *
 * `value` is what the dialog draws, null when the server kept nothing usable.
 * `expired` is the server's own word that the file's BYTES are gone (its
 * upload listing says `expired`). It used to be answered by returning null,
 * which left the dialog with nothing to go on but "no bytes in this tab", so
 * a swept workbook said "This file is no longer available in this browser
 * session" — about a file no browser session had anything to do with. The
 * flag lets the dialog say what happened: over the summary the server kept,
 * or instead of a preview when it kept nothing.
 */
export interface Kept<T> {
  value: T | null;
  expired?: boolean;
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
  /** A loader reported the file's bytes swept (`Kept.expired`). */
  const [keptExpired, setKeptExpired] = useState(false);
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
    // So does `expired` (2026-10-02): the server swept a PDF's bytes and kept
    // the text the chat read from it, which is worth more than a sentence.
    if (kind !== 'none' && kind !== 'unavailable' && kind !== 'expired') return;
    if (!wantsWorkbook && !wantsDocText) return;
    const controller = new AbortController();
    let alive = true;
    setLoading(true);
    void (async () => {
      try {
        const { loadWorkbook: wb, loadDocumentText: dt } = loaders.current;
        if (wb) {
          const found = await wb(controller.signal);
          if (!alive) return;
          setWorkbook(found.value);
          setKeptExpired(Boolean(found.expired));
        } else if (dt) {
          const found = await dt(controller.signal);
          if (!alive) return;
          setDocText(found.value);
          setKeptExpired(Boolean(found.expired));
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
            keptExpired={keptExpired}
            loading={loading}
          />
        </div>
      </div>
    </div>,
    document.body,
  );
}

const NOTE = 'text-sm leading-relaxed text-muted';
/** The line over what the server kept, when it stands in for the file. */
const KEPT_NOTE = 'mb-3 text-xs leading-relaxed text-muted';

/**
 * 2026-10-02: the sentence over a document's text or a spreadsheet's summary
 * when it is shown INSTEAD of the file, or null when it is simply the
 * preview (a .docx always reads as its text; that is not a fallback).
 *
 * "Expired" only when the server said so — a 410 for the bytes, or a loader's
 * `expired`. A PDF whose bytes merely could not be fetched (no upload id, a
 * 404, offline) is not called expired: that would be a guess.
 */
function keptNote(
  source: ResolvedAttachment,
  keptExpired: boolean,
  what: 'text' | 'summary',
): string | null {
  const kept =
    what === 'text' ? 'This is the text the chat read from it.' : 'This is the summary the chat made of it.';
  if (source.kind === 'expired' || keptExpired) {
    return `The file itself has expired and is no longer stored. ${kept}`;
  }
  if (source.kind === 'unavailable' && previewKindFor(source.name, source.mime) !== 'none') {
    return `The file itself can’t be opened here. ${kept}`;
  }
  return null;
}

function PreviewBody({
  source,
  objectUrl,
  text,
  truncated,
  failed,
  workbook,
  docText,
  keptExpired,
  loading,
}: {
  source: ResolvedAttachment;
  objectUrl: string | null;
  text: string | null;
  truncated: boolean;
  failed: boolean;
  workbook: WorkbookPreview | null;
  docText: DocumentText | null;
  keptExpired: boolean;
  loading: boolean;
}) {
  const { kind, name } = source;

  if (failed) {
    // A failed preview is a message, never a download. That fallback is what
    // put files on disk without anyone asking.
    return <p className={NOTE}>Unable to preview this file.</p>;
  }

  // 2026-10-02: a player streams from the server by URL, so it is decided
  // before anything about bytes (it never has any).
  if ((kind === 'video' || kind === 'audio') && source.url) {
    return <MediaPlayer kind={kind} url={source.url} name={name} />;
  }

  // 4C: a server-backed preview outranks "no bytes" — the profile and the
  // extracted text are database rows, and they outlive the file itself.
  if (loading) return <p className={NOTE}>Loading preview…</p>;
  if (workbook) {
    const note = keptNote(source, keptExpired, 'summary');
    return (
      <>
        {note && <p className={KEPT_NOTE}>{note}</p>}
        <WorkbookView workbook={workbook} />
      </>
    );
  }
  if (docText) {
    const note = keptNote(source, keptExpired, 'text');
    return (
      <>
        {note && <p className={KEPT_NOTE}>{note}</p>}
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

  if (kind === 'expired' || keptExpired) {
    // Distinct from `unavailable` on purpose: the server HAD this file and its
    // workspace TTL swept it, which is a fact the user can act on.
    //
    // `keptExpired` (2026-10-02): a swept workbook whose stored summary could
    // not be drawn. It used to fall through to `unavailable` below and blame
    // "this browser session" for what the server's sweep did.
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

  if (kind === 'missing') {
    // 2026-10-02: a stored photo whose route answered 404/410. Not "expired"
    // (photos are kept for the life of the chat) and not "this browser
    // session" (the server is where it was looked for).
    return <p className={NOTE}>This photo is no longer stored on the server.</p>;
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

/* ---------------------------------------------------------- the players */

/** Why a player could not play, once the server has been asked. */
type PlayerProblem = 'checking' | 'expired' | 'not_found' | 'unreachable' | 'format';

/**
 * A sent video or audio file, played from the server (2026-10-02).
 *
 * `src` is the streaming proxy's URL and nothing is fetched by this
 * component: the browser's player asks for byte ranges as it needs them.
 * `preload="metadata"` makes opening the dialog cost the file's header (the
 * duration and the first frame), never the file; playing and seeking fetch
 * what they reach. `playsInline` keeps an iPhone from leaping to full screen
 * on the first tap. `controlsList="nodownload"` removes Chrome's "Download"
 * from the player's menu, the one way this dialog could otherwise save a file
 * (NEW-09A).
 *
 * A media element reports a failure without its cause: a swept file, a 404
 * and a codec this browser lacks all arrive as the same `error` event. So on
 * error the server is asked once, for a single byte, and the answer picks the
 * sentence. Only then — a playing file costs no extra request.
 */
function MediaPlayer({ kind, url, name }: { kind: 'video' | 'audio'; url: string; name: string }) {
  const [problem, setProblem] = useState<PlayerProblem | null>(null);
  const probe = useRef<AbortController | null>(null);
  useEffect(() => () => probe.current?.abort(), []);

  async function explain() {
    if (problem) return;
    setProblem('checking');
    const controller = new AbortController();
    probe.current = controller;
    try {
      const res = await fetch(url, {
        headers: { range: 'bytes=0-0' },
        cache: 'no-store',
        signal: controller.signal,
      });
      // One byte at most, but read nothing: the status is the whole answer.
      void res.body?.cancel().catch(() => undefined);
      if (controller.signal.aborted) return;
      if (res.status === 410) setProblem('expired');
      else if (res.status === 404) setProblem('not_found');
      else if (!res.ok) setProblem('unreachable');
      else setProblem('format');
    } catch {
      if (!controller.signal.aborted) setProblem('unreachable');
    }
  }

  if (problem && problem !== 'checking') {
    const sentence = {
      expired:
        'This upload has expired and is no longer stored. Attach the file again to play it.',
      not_found: 'This file is no longer on the server.',
      unreachable:
        'The file couldn’t be loaded from the server. Check your connection and try again.',
      format: `This browser can’t play ${fileBadgeFor(name)} files.`,
    }[problem];
    return <p className={NOTE}>{sentence}</p>;
  }

  const label = `${kind === 'video' ? 'Video' : 'Audio'}: ${name}`;
  return kind === 'video' ? (
    <video
      controls
      playsInline
      preload="metadata"
      controlsList="nodownload"
      src={url}
      aria-label={label}
      onError={() => void explain()}
      className="mx-auto block max-h-[65dvh] w-full rounded-ts bg-black"
    />
  ) : (
    <audio
      controls
      preload="metadata"
      controlsList="nodownload"
      src={url}
      aria-label={label}
      onError={() => void explain()}
      className="block w-full"
    />
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
