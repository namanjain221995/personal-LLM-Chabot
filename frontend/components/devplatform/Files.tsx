'use client';

/**
 * A project's files: the list, its processing, uploads and deletes.
 *
 * WHAT THE TAB IS FOR (Files design §14.1, 2026-09-13). A developer who sent
 * a 900-page PDF to /v1/files wants to know whether it is usable yet, why it
 * failed if it did, and what the platform made of it — without writing a
 * polling loop to find out. So every row says its state in words (Queued,
 * Processing 3/6, Processed, Failed, Unsupported), a row still moving follows
 * the file's own event stream, and the detail view lists the stage timeline,
 * the measured facts and the derived outputs by name.
 *
 * WHAT IT WILL NOT SHOW OR DO.
 *  · Serve bytes. No download link, for the original or a derived output
 *    (owner decision D6: no byte route rides a session cookie). The detail
 *    view names the /v1 call that fetches them with a key instead.
 *  · Say anything the server did not. A failure is the File object's own
 *    fixed sentence; facts and derived names pass the allowlists in
 *    files-api.ts; a number the route did not send is an em dash, never 0.
 *  · Hold a file's name or content in browser storage — see files-api.ts.
 *
 * UPLOADS go through the Uploads flow in 8 MiB parts whatever the size, so a
 * dropped connection costs one part and every upload can be resumed: after a
 * retry budget the tray pauses the upload with its parts kept, the browser's
 * `online` event resumes it, and re-picking the same file after a reload
 * continues from the parts the server already holds. At most
 * MAX_CONCURRENT_UPLOADS run at once; the rest wait in the tray.
 *
 * Capabilities: `api.projects.read` opens the tab (nav.ts); uploading and
 * deleting need `api.projects.manage`, and the orchestrator checks both again
 * on every call — the buttons hidden here are a courtesy, not the control.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ReactNode,
} from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { CopyButton } from '@/components/CopyButton';
import { useToast } from '@/components/Providers';
import { IconAlert, IconCloud, IconRefresh, IconTrash } from '@/components/icons';
import { can, type Me } from '@/components/admin/api';
import { AdminDialog, SECONDARY_BUTTON } from '@/components/admin/AdminDialog';
import { RowMenu, type RowMenuItem } from '@/components/admin/RowMenu';
import {
  ADMIN_PRIMARY_BUTTON,
  ADMIN_SECONDARY_BUTTON,
  AdminSelect,
  AdminToolbar,
} from '@/components/admin/controls';
import { ErrorPanel, StatTile } from '@/components/admin/ui';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { formatBytes, formatDay, formatWhen } from '@/lib/format';
import { AdminApiError, consoleDelete, consoleJson, messageOf } from './api';
import {
  ConsoleEmpty,
  ConsoleTable,
  MonoValue,
  ProjectSelect,
  ProjectsLoadError,
  SELECT_FIT,
  useProjects,
  type ConsoleColumn,
} from './shared';
import { useConsole } from './useConsole';
import { useConsoleStatus } from './status';
import {
  FILE_KINDS,
  FILE_STATUS_FILTERS,
  FileUploader,
  KIND_LABEL,
  STATUS_FILTER_LABEL,
  kindLabel,
  pruneStaleResumeRecords,
  browserStore,
  derivedLabel,
  fileFacts,
  fileStatus,
  filesListQuery,
  filesPaths,
  followFileEvents,
  formatDuration,
  isTerminal,
  kindOf,
  stageLabel,
  type ConsoleFile,
  type DerivedOutput,
  type FileList,
  type FileStage,
  type FileStatusKey,
  type FollowOptions,
  type ProjectStorage,
  type UploadSnapshot,
  type UploaderDeps,
} from './files-api';

/** Uploading and deleting; reading is `api.projects.read`, which opens the tab. */
export const FILES_MANAGE_CAPABILITY = 'api.projects.manage';

/**
 * How many files follow their event stream at once. Each is an open request;
 * a browser on HTTP/1.1 has six per host, and the chat and the rest of the
 * console need some. The file in the detail view goes first, then this tab's
 * uploads, then the newest unfinished rows.
 */
export const MAX_LIVE_FILES = 4;

/**
 * How many uploads send parts at once. Each part is read whole into memory to
 * be hashed here, and the BFF holds each relayed part whole before passing it
 * on, so N uploads at once is N × 8 MiB in the browser AND in the frontend
 * process. Picking a hundred files must not become 800 MiB in the Next.js
 * container, nor take every HTTP/1.1 connection the event streams and the rest
 * of the console need. Two keeps a large batch moving without either.
 */
export const MAX_CONCURRENT_UPLOADS = 2;

/** Test seams: the uploader's clock and part size, the follower's sleep. */
export interface FilesPanelDeps {
  uploader?: Partial<UploaderDeps>;
  follow?: Pick<FollowOptions, 'sleep' | 'pollMs'>;
}

interface UploadItem {
  key: string;
  projectId: string;
  projectName: string;
  uploader: FileUploader;
  snapshot: UploadSnapshot;
  /** Waiting for a free upload slot (MAX_CONCURRENT_UPLOADS). */
  waiting: boolean;
}

const TONE: Record<FileStatusKey, { dot: string; text: string }> = {
  assembling: { dot: 'bg-accent', text: 'text-ink' },
  queued: { dot: 'bg-faint', text: 'text-muted' },
  processing: { dot: 'bg-accent', text: 'text-ink' },
  processed: { dot: 'bg-ok', text: 'text-muted' },
  failed: { dot: 'bg-danger', text: 'text-danger' },
  unsupported: { dot: 'bg-warn', text: 'text-warn' },
  deleted: { dot: 'bg-faint', text: 'text-faint' },
};

const SMALL_BUTTON =
  'inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-[var(--admin-control)] px-2.5 text-xs font-medium text-muted transition-colors duration-ts hover:bg-[var(--admin-control-hover)] hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40';

const ACTIVE_UPLOAD = new Set(['preparing', 'uploading', 'retrying', 'completing']);

function ProgressBar({ value, label }: { value: number | null; label: string }) {
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value ?? undefined}
      className="mt-1.5 h-1 w-full max-w-[160px] overflow-hidden rounded-full bg-surface-2"
    >
      <div
        className="h-full rounded-full bg-accent-strong transition-[width] duration-ts"
        style={{ width: `${value ?? 0}%` }}
      />
    </div>
  );
}

export function FileStatusBadge({ file, bar = true }: { file: ConsoleFile; bar?: boolean }) {
  const view = fileStatus(file);
  const tone = TONE[view.key];
  const moving = view.key === 'processing' || view.key === 'assembling';
  return (
    <div className="min-w-0">
      <span
        className={`inline-flex max-w-full items-center gap-2 text-xs font-medium ${tone.text}`}
        title={moving && view.percent !== null ? `${view.label} · ${view.percent}%` : view.label}
      >
        <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} />
        <span className="truncate">{view.label}</span>
      </span>
      {bar && moving && <ProgressBar value={view.percent} label={`Processing ${file.filename}`} />}
    </div>
  );
}

function day(seconds: number | null | undefined, empty: string) {
  if (typeof seconds !== 'number') return <span className="text-faint">{empty}</span>;
  return (
    <span className="text-muted" title={formatWhen(seconds)}>
      {formatDay(seconds)}
    </span>
  );
}

/** Follows one file; renders nothing. */
function LiveFile({
  projectId,
  fileId,
  onFile,
  onGone,
  follow,
}: {
  projectId: string;
  fileId: string;
  onFile: (projectId: string, file: ConsoleFile) => void;
  onGone: (fileId: string) => void;
  follow?: FilesPanelDeps['follow'];
}) {
  // The callbacks are read through a ref so a parent render does not reopen
  // the stream; only a different file does.
  const handlers = useRef({ onFile, onGone, follow });
  handlers.current = { onFile, onGone, follow };
  useEffect(() => {
    const controller = new AbortController();
    void followFileEvents(projectId, fileId, {
      signal: controller.signal,
      onFile: (file) => handlers.current.onFile(projectId, file),
      onGone: () => handlers.current.onGone(fileId),
      ...handlers.current.follow,
    }).catch(() => undefined);
    return () => controller.abort();
  }, [projectId, fileId]);
  return null;
}

function uploadSentence(item: UploadItem, file: ConsoleFile | null): string {
  const s = item.snapshot;
  if (item.waiting) {
    const verb = s.state === 'paused' || s.state === 'failed' ? 'resume' : 'start';
    return `Waiting to ${verb}. Uploads run ${MAX_CONCURRENT_UPLOADS} at a time.`;
  }
  switch (s.state) {
    case 'preparing':
      return 'Checking what the server already holds…';
    case 'uploading':
    case 'retrying': {
      const progress =
        s.partsTotal > 0
          ? `${formatBytes(s.bytesConfirmed)} of ${formatBytes(s.bytesTotal)} · part ${s.partsDone} of ${s.partsTotal}`
          : 'Starting…';
      const resumed = s.resumed ? ' · resumed' : '';
      if (s.state === 'retrying') {
        const why = s.retryReason === 'busy' ? 'the server is busy' : 'connection trouble';
        return `${progress}${resumed} · ${why}, trying again in ${s.retryInS ?? 1} s`;
      }
      return `${progress}${resumed}`;
    }
    case 'paused':
      return s.message ?? 'Paused.';
    case 'completing':
      return 'All parts sent. Finishing the upload…';
    case 'uploaded': {
      if (!file) return 'Uploaded.';
      const view = fileStatus(file);
      if (view.key === 'failed' || view.key === 'unsupported') {
        return file.status_details ?? `Uploaded · ${view.label}`;
      }
      return `Uploaded · ${view.label}`;
    }
    case 'failed':
      return s.message ?? 'The upload could not be completed.';
    case 'cancelled':
      // The uploader's message says what the server still holds, when anything.
      return s.message ?? 'Cancelled. Nothing was kept.';
    default:
      return '';
  }
}

function UploadTray({
  items,
  live,
  currentProjectId,
  onResume,
  onCancel,
  onDismiss,
  onDetails,
}: {
  items: UploadItem[];
  live: Record<string, ConsoleFile>;
  currentProjectId: string;
  onResume: (key: string) => void;
  onCancel: (key: string) => void;
  onDismiss: (key: string) => void;
  onDetails: (fileId: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <section aria-label="Uploads" className="mt-5">
      <ul className="space-y-2">
        {items.map((item) => {
          const s = item.snapshot;
          const file = s.file ? live[s.file.id] ?? s.file : null;
          const sentence = uploadSentence(item, file);
          const failed =
            s.state === 'failed' ||
            (file !== null && ['failed', 'unsupported'].includes(fileStatus(file).key));
          let percent: number | null = null;
          if (s.state === 'uploaded' && file) {
            // Every byte is on the server; until processing measures a
            // percent of its own, the bar stays full rather than dropping to 0.
            const view = fileStatus(file);
            percent = view.percent ?? (view.key === 'failed' || view.key === 'unsupported' ? null : 100);
          } else if (s.bytesTotal > 0) percent = Math.floor((s.bytesConfirmed * 100) / s.bytesTotal);
          else if (s.state === 'completing') percent = 100;
          const busy = item.waiting || ACTIVE_UPLOAD.has(s.state);
          return (
            <li
              key={item.key}
              data-testid="upload-item"
              className="rounded-ts border border-border bg-surface px-3 py-2.5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0 flex-[1_1_14rem]">
                  <p className="truncate text-sm font-medium text-ink" title={s.filename}>
                    {s.filename}
                  </p>
                  <p
                    data-testid="upload-sentence"
                    className={`text-xs [overflow-wrap:anywhere] ${failed ? 'text-danger' : 'text-muted'}`}
                  >
                    {sentence}
                    {item.projectId !== currentProjectId ? ` · in ${item.projectName}` : ''}
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                  {!item.waiting && s.state === 'paused' && (
                    <button type="button" className={SMALL_BUTTON} onClick={() => onResume(item.key)}>
                      Resume
                    </button>
                  )}
                  {!item.waiting && s.state === 'failed' && (
                    <button type="button" className={SMALL_BUTTON} onClick={() => onResume(item.key)}>
                      Try again
                    </button>
                  )}
                  {(busy || s.state === 'paused') && s.state !== 'completing' && (
                    <button type="button" className={SMALL_BUTTON} onClick={() => onCancel(item.key)}>
                      Cancel
                    </button>
                  )}
                  {file && item.projectId === currentProjectId && (
                    <button type="button" className={SMALL_BUTTON} onClick={() => onDetails(file.id)}>
                      Details
                    </button>
                  )}
                  {!busy && s.state !== 'paused' && (
                    <button
                      type="button"
                      className={SMALL_BUTTON}
                      aria-label={`Dismiss ${s.filename}`}
                      onClick={() => onDismiss(item.key)}
                    >
                      Dismiss
                    </button>
                  )}
                </div>
              </div>
              <div
                role="progressbar"
                aria-label={`Upload of ${s.filename}`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={percent ?? undefined}
                className="mt-2 h-1 w-full overflow-hidden rounded-full bg-surface-2"
              >
                <div
                  className={`h-full rounded-full transition-[width] duration-ts ${failed ? 'bg-danger' : 'bg-accent-strong'}`}
                  style={{ width: `${percent ?? 0}%` }}
                />
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

const STAGE_WORD: Record<FileStage['status'], string> = {
  pending: 'Waiting',
  running: 'Running',
  done: 'Done',
  skipped: 'Skipped',
  failed: 'Failed',
};

const STAGE_DOT: Record<FileStage['status'], string> = {
  pending: 'border border-border bg-transparent',
  running: 'bg-accent',
  done: 'bg-ok',
  skipped: 'bg-faint',
  failed: 'bg-danger',
};

function FileDetailDialog({
  projectId,
  file,
  onClose,
  mayManage,
  onDelete,
}: {
  projectId: string;
  file: ConsoleFile | null;
  onClose: () => void;
  mayManage: boolean;
  onDelete: (file: ConsoleFile) => void;
}) {
  const processed = file?.processing?.state === 'processed';
  const derived = useConsole<{ data: DerivedOutput[] }>(
    filesPaths.fileDerived(projectId, file?.id ?? ''),
    {},
    file !== null && processed,
  );
  if (!file) return null;
  const p = file.processing;
  const facts = fileFacts(file);
  const view = fileStatus(file);
  const outputs = (derived.data?.data ?? []).filter((d) => derivedLabel(d.name) !== null);
  const ran =
    p && typeof p.started_at === 'number' && typeof p.finished_at === 'number' && p.finished_at >= p.started_at
      ? formatDuration(p.finished_at - p.started_at)
      : null;
  const row = (label: string, value: ReactNode) => (
    <div className="grid grid-cols-[8.5rem_minmax(0,1fr)] gap-3 py-1.5">
      <dt className="text-xs text-faint">{label}</dt>
      <dd className="min-w-0 text-sm text-ink">{value}</dd>
    </div>
  );

  return (
    <AdminDialog open title={file.filename} size="md" onClose={onClose}>
      <div data-testid="file-detail" className="space-y-4">
        <dl className="divide-y divide-[var(--admin-separator)]">
          {row(
            'File id',
            <span className="flex items-center gap-2">
              <MonoValue value={file.id} />
              <CopyButton text={file.id} label="Copy file id" />
            </span>,
          )}
          {row('Status', <FileStatusBadge file={file} />)}
          {row('Kind', kindLabel(file))}
          {row('Size', formatBytes(file.bytes))}
          {row(
            'Derived size',
            typeof file.derived_bytes === 'number' ? formatBytes(file.derived_bytes) : <span className="text-faint">—</span>,
          )}
          {row('Created', file.created_at ? formatWhen(file.created_at) : <span className="text-faint">—</span>)}
          {row('Expires', file.expires_at ? formatWhen(file.expires_at) : 'Never — kept until deleted')}
          {row('Processing time', ran ?? <span className="text-faint">—</span>)}
        </dl>

        {(view.key === 'failed' || view.key === 'unsupported') && file.status_details && (
          <p role="note" className="flex items-start gap-1.5 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            <IconAlert size={15} className="mt-0.5 shrink-0" />
            {file.status_details}
          </p>
        )}
        {view.key === 'deleted' && (
          <p role="note" className="text-sm text-muted">
            This file was deleted while it was on screen.
          </p>
        )}

        {p && p.stages.length > 0 && (
          <section aria-label="Processing stages">
            <h3 className="text-[11px] font-medium uppercase tracking-wide text-faint">Stages</h3>
            <ol className="mt-2 space-y-1.5">
              {p.stages.map((stage) => (
                <li key={stage.name} className="flex items-center gap-2.5 text-sm">
                  <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${STAGE_DOT[stage.status] ?? STAGE_DOT.pending}`} />
                  <span className="min-w-0 flex-1 truncate text-ink">{stageLabel(stage.name)}</span>
                  <span className="shrink-0 text-xs tabular-nums text-muted">
                    {STAGE_WORD[stage.status] ?? 'Waiting'}
                    {stage.status === 'running' && typeof stage.percent === 'number' ? ` · ${stage.percent}%` : ''}
                  </span>
                </li>
              ))}
            </ol>
          </section>
        )}

        {facts.length > 0 && (
          <section aria-label="Facts">
            <h3 className="text-[11px] font-medium uppercase tracking-wide text-faint">What was measured</h3>
            <dl className="mt-1 divide-y divide-[var(--admin-separator)]">
              {facts.map((fact) => (
                <div key={fact.key}>{row(fact.label, <span className="tabular-nums">{fact.value}</span>)}</div>
              ))}
            </dl>
          </section>
        )}

        {processed && (
          <section aria-label="Derived outputs">
            <h3 className="text-[11px] font-medium uppercase tracking-wide text-faint">Derived outputs</h3>
            {derived.error ? (
              <div className="mt-2">
                <ErrorPanel message={derived.error} onRetry={derived.reload} />
              </div>
            ) : derived.loading && derived.data === null ? (
              <p className="mt-2 text-sm text-faint">Loading…</p>
            ) : outputs.length === 0 ? (
              <p className="mt-2 text-sm text-muted">This file has no derived outputs.</p>
            ) : (
              <ul className="mt-2 space-y-1.5">
                {outputs.map((output) => (
                  <li key={output.name} className="flex items-center justify-between gap-3 text-sm">
                    <span className="min-w-0">
                      <span className="block truncate text-ink">{derivedLabel(output.name)}</span>
                      <code className="font-mono text-xs text-faint">{output.name}</code>
                    </span>
                    <span className="shrink-0 text-xs tabular-nums text-muted">{formatBytes(output.bytes)}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-xs leading-relaxed text-faint">
              The console lists derived outputs but never serves their bytes. Fetch one with an API key that holds{' '}
              <code className="font-mono">files.read</code>:{' '}
              <code className="font-mono [overflow-wrap:anywhere]">GET /v1/files/{file.id}/derived/&lt;name&gt;</code>
            </p>
          </section>
        )}

        <div className="flex flex-wrap justify-end gap-2 pt-1">
          {mayManage && view.key !== 'deleted' && (
            <button
              type="button"
              onClick={() => onDelete(file)}
              className={`${SECONDARY_BUTTON} text-danger`}
            >
              <IconTrash size={15} />
              Delete file
            </button>
          )}
          <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>
            Close
          </button>
        </div>
      </div>
    </AdminDialog>
  );
}

export function FilesPanel({ me, deps }: { me: Me; deps?: FilesPanelDeps }) {
  const projectsQuery = useProjects();
  const projects = projectsQuery.data?.projects ?? [];
  const [projectId, setProjectId] = useState('');
  const selected = projects.find((p) => p.id === projectId) ?? projects[0] ?? null;
  const pid = selected?.id ?? '';
  const mayManage = can(me, FILES_MANAGE_CAPABILITY);

  const [status, setStatus] = useState('');
  const [kind, setKind] = useState('');
  const list = useConsole<FileList>(filesPaths.files(pid), filesListQuery({ status, kind }), selected !== null);
  const storage = useConsole<ProjectStorage>(filesPaths.storage(pid), {}, selected !== null);

  const { toast } = useToast();
  const { announce } = useConsoleStatus();

  // Pages after the first, kept only while the question is the same one.
  const identity = `${pid}|${status}|${kind}`;
  const [more, setMore] = useState<{
    identity: string;
    rows: ConsoleFile[];
    hasMore: boolean;
    lastId: string | null;
    loading: boolean;
    error: string | null;
  } | null>(null);
  const [live, setLive] = useState<Record<string, ConsoleFile>>({});
  const [gone, setGone] = useState<Record<string, true>>({});
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ file: ConsoleFile; projectId: string } | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const counter = useRef(0);

  const extra = more !== null && more.identity === identity ? more : null;
  const firstRows = list.data?.data;
  const rows = useMemo(() => {
    const out: ConsoleFile[] = [];
    const seen = new Set<string>();
    for (const row of [...(firstRows ?? []), ...(extra?.rows ?? [])]) {
      if (!row || typeof row.id !== 'string' || seen.has(row.id) || gone[row.id]) continue;
      seen.add(row.id);
      // The filters are applied to the row the SERVER listed, so a file that
      // finishes while "Processing" is selected stays on screen with its new
      // state instead of vanishing under the reader's cursor.
      if (status && row.processing?.state !== status) continue;
      if (kind && kindOf(row) !== kind) continue;
      // A live frame is the /v1 File object, which has no `derived_bytes`;
      // spreading it over the listed row keeps the console's own field.
      const frame = live[row.id];
      out.push(frame ? { ...row, ...frame } : row);
    }
    return out;
  }, [firstRows, extra, gone, live, status, kind]);
  const hasMore = extra ? extra.hasMore : Boolean(list.data?.has_more);
  const lastId = extra ? extra.lastId : list.data?.last_id ?? null;
  const projectsPending = projectsQuery.loading && projectsQuery.data === null;
  const listPending = projectsPending || (list.loading && list.data === null);

  const reloadList = useCallback(() => {
    setMore(null);
    list.reload();
  }, [list.reload]); // eslint-disable-line react-hooks/exhaustive-deps
  const reloadRef = useRef({ reloadList, reloadStorage: storage.reload });
  reloadRef.current = { reloadList, reloadStorage: storage.reload };
  const uploadsRef = useRef(uploads);
  uploadsRef.current = uploads;
  // The upload queue lives in refs, not state: `pump` must see a slot freed
  // by a run that ended a microtask ago, not the last render's picture.
  const uploadersRef = useRef(new Map<string, FileUploader>());
  const queueRef = useRef<string[]>([]);
  const runningRef = useRef(new Set<string>());
  const mountedRef = useRef(true);

  const pump = useCallback(() => {
    if (!mountedRef.current) return;
    while (runningRef.current.size < MAX_CONCURRENT_UPLOADS && queueRef.current.length > 0) {
      const key = queueRef.current.shift() as string;
      const uploader = uploadersRef.current.get(key);
      if (!uploader || runningRef.current.has(key)) continue;
      runningRef.current.add(key);
      void uploader.run().finally(() => {
        runningRef.current.delete(key);
        pump();
      });
    }
    const waiting = new Set(queueRef.current);
    setUploads((prev) =>
      prev.some((it) => it.waiting !== waiting.has(it.key))
        ? prev.map((it) => (it.waiting === waiting.has(it.key) ? it : { ...it, waiting: waiting.has(it.key) }))
        : prev,
    );
  }, []);

  const enqueue = useCallback(
    (keys: string[]) => {
      for (const key of keys) {
        if (!queueRef.current.includes(key) && !runningRef.current.has(key)) queueRef.current.push(key);
      }
      pump();
    },
    [pump],
  );

  useEffect(() => {
    if (projectsQuery.error) announce(projectsQuery.error);
    else if (listPending) announce('Loading files.');
    else if (list.error) announce(list.error);
    else announce(`${rows.length} file${rows.length === 1 ? '' : 's'}${hasMore ? ', more available' : ''}.`);
  }, [projectsQuery.error, listPending, list.error, rows.length, hasMore, announce]);

  // A different project closes the detail view: it named another project's file.
  useEffect(() => {
    setDetailId(null);
  }, [pid]);

  // The connection is back: continue every paused upload, through the queue.
  useEffect(() => {
    const onOnline = () => {
      enqueue(uploadsRef.current.filter((item) => item.snapshot.state === 'paused').map((item) => item.key));
    };
    window.addEventListener('online', onOnline);
    return () => window.removeEventListener('online', onOnline);
  }, [enqueue]);

  // Leaving the tab pauses uploads rather than letting them run unseen, and
  // drops the queue; their records stay, so re-picking the file continues them.
  useEffect(() => {
    mountedRef.current = true;
    // Resume records of uploads no server can still hold, for files nobody
    // re-picked, go when the tab opens.
    let storage: Storage | null = null;
    try {
      storage = typeof window !== 'undefined' ? window.localStorage : null;
    } catch {
      storage = null;
    }
    pruneStaleResumeRecords(storage, Date.now());
    const uploaders = uploadersRef.current;
    const queue = queueRef;
    return () => {
      mountedRef.current = false;
      queue.current = [];
      for (const uploader of uploaders.values()) uploader.pause();
    };
  }, []);

  const sending = uploads.some((u) => u.waiting || ACTIVE_UPLOAD.has(u.snapshot.state));
  useEffect(() => {
    if (!sending) return undefined;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [sending]);

  const onLiveFile = useCallback((projectIdOf: string, file: ConsoleFile) => {
    setLive((prev) => (prev[file.id] === file ? prev : { ...prev, [file.id]: file }));
    if (!isTerminal(file)) return;
    reloadRef.current.reloadStorage();
    // The terminal frame is the /v1 object, without the derived size the
    // console route adds and that only exists now; read the file once for it.
    if (file.status !== 'deleted' && typeof file.derived_bytes !== 'number') {
      consoleJson<ConsoleFile>(filesPaths.file(projectIdOf, file.id))
        .then((fresh) => {
          if (!mountedRef.current || !fresh || fresh.id !== file.id) return;
          setLive((prev) => (prev[file.id]?.status === 'deleted' ? prev : { ...prev, [file.id]: { ...prev[file.id], ...fresh } }));
        })
        .catch(() => undefined);
    }
  }, []);
  const onGone = useCallback((fileId: string) => {
    setGone((prev) => (prev[fileId] ? prev : { ...prev, [fileId]: true }));
    reloadRef.current.reloadStorage();
  }, []);

  const rowById = useMemo(() => new Map(rows.map((r) => [r.id, r])), [rows]);
  const tracked = useMemo(() => {
    const out: { projectId: string; fileId: string }[] = [];
    const add = (projectIdOf: string, file: ConsoleFile | null | undefined) => {
      if (!file || gone[file.id] || out.length >= MAX_LIVE_FILES) return;
      const current = live[file.id] ?? file;
      if (isTerminal(current) || out.some((t) => t.fileId === file.id)) return;
      out.push({ projectId: projectIdOf, fileId: file.id });
    };
    if (detailId) add(pid, rowById.get(detailId) ?? live[detailId]);
    for (const item of uploads) add(item.projectId, item.snapshot.file);
    for (const row of rows) add(pid, row);
    return out;
  }, [detailId, pid, rowById, live, uploads, rows, gone]);

  function startUploads(picked: File[]) {
    if (!selected || picked.length === 0) return;
    // A file picked again while the tray still holds its upload continues that
    // upload. A second uploader would find the first one's upload claimed and
    // start another, sending every byte twice and making a second file.
    const again: string[] = [];
    const fresh = picked.filter((file) => {
      const existing = uploadsRef.current.find(
        (item) =>
          item.projectId === selected.id &&
          item.uploader.file.name === file.name &&
          item.uploader.file.size === file.size &&
          item.uploader.file.lastModified === file.lastModified &&
          item.snapshot.state !== 'uploaded' &&
          item.snapshot.state !== 'cancelled',
      );
      if (!existing) return true;
      if (existing.snapshot.state === 'paused' || existing.snapshot.state === 'failed') again.push(existing.key);
      announce(`${file.name} is already in the upload tray.`);
      return false;
    });
    if (again.length > 0) enqueue(again);
    if (fresh.length === 0) return;
    const items: UploadItem[] = fresh.map((file) => {
      counter.current += 1;
      const key = `upload-${counter.current}`;
      const uploader = new FileUploader(
        selected.id,
        file,
        (snapshot) => {
          setUploads((prev) => prev.map((it) => (it.key === key ? { ...it, snapshot } : it)));
          if (snapshot.state === 'uploaded') {
            announce(`${snapshot.filename} uploaded. Processing has started.`);
            reloadRef.current.reloadList();
            reloadRef.current.reloadStorage();
          } else if (snapshot.state === 'failed') {
            announce(snapshot.message ?? `${snapshot.filename} could not be uploaded.`);
          } else if (snapshot.state === 'paused') {
            announce(snapshot.message ?? `${snapshot.filename} is paused.`);
          }
        },
        { store: browserStore(), ...deps?.uploader },
      );
      uploadersRef.current.set(key, uploader);
      return { key, projectId: selected.id, projectName: selected.name, uploader, snapshot: uploader.snapshot, waiting: true };
    });
    setUploads((prev) => [...items, ...prev]);
    enqueue(items.map((item) => item.key));
  }

  function cancelUpload(key: string) {
    queueRef.current = queueRef.current.filter((k) => k !== key);
    void uploadersRef.current.get(key)?.cancel();
    pump();
  }

  function dismissUpload(key: string) {
    uploadersRef.current.delete(key);
    setUploads((prev) => prev.filter((it) => it.key !== key));
  }

  function onPick(event: ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(event.target.files ?? []);
    // Cleared so picking the same file again (to resume it) fires a change.
    event.target.value = '';
    startUploads(picked);
  }

  async function loadMore() {
    if (!lastId || !pid) return;
    const asked = identity;
    setMore((prev) => ({
      identity: asked,
      rows: prev?.identity === asked ? prev.rows : [],
      hasMore: true,
      lastId,
      loading: true,
      error: null,
    }));
    const query = new URLSearchParams(
      Object.entries(filesListQuery({ status, kind, after: lastId }))
        .filter(([, v]) => v !== undefined && v !== '')
        .map(([k, v]) => [k, String(v)]),
    ).toString();
    try {
      const page = await consoleJson<FileList>(`${filesPaths.files(pid)}?${query}`);
      setMore((prev) =>
        prev && prev.identity === asked
          ? { ...prev, rows: [...prev.rows, ...(page.data ?? [])], hasMore: Boolean(page.has_more), lastId: page.last_id, loading: false }
          : prev,
      );
    } catch (err) {
      setMore((prev) =>
        prev && prev.identity === asked
          ? { ...prev, loading: false, error: messageOf(err, 'More files could not be loaded.') }
          : prev,
      );
    }
  }

  async function remove(target: { file: ConsoleFile; projectId: string }) {
    const { file } = target;
    try {
      await consoleDelete(filesPaths.file(target.projectId, file.id));
      setGone((prev) => ({ ...prev, [file.id]: true }));
      toast('File deleted.');
      announce(`${file.filename} deleted.`);
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 404) {
        // Already gone (expired, or deleted from another tab): say so, and
        // stop listing it.
        setGone((prev) => ({ ...prev, [file.id]: true }));
        toast('That file no longer exists.');
      } else {
        toast(messageOf(err, 'The file could not be deleted.'), 'error');
      }
    }
    storage.reload();
  }

  // `rows` already carries each live frame over its listed row; a file that is
  // not listed (just uploaded, or deleted while open) is its frame alone.
  const detailFile = detailId ? rowById.get(detailId) ?? live[detailId] ?? null : null;

  const columns: ConsoleColumn<ConsoleFile>[] = [
    {
      key: 'file',
      label: 'File',
      render: (f) => (
        <div className="min-w-0">
          <button
            type="button"
            onClick={() => setDetailId(f.id)}
            className="block max-w-full truncate text-left font-medium text-ink hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            title={f.filename}
          >
            {f.filename}
          </button>
          <MonoValue value={f.id} />
        </div>
      ),
    },
    { key: 'status', label: 'Status', width: '150px', render: (f) => <FileStatusBadge file={f} /> },
    {
      key: 'kind',
      label: 'Kind',
      width: '120px',
      hideBelowLg: true,
      render: (f) => <span className="text-muted">{kindLabel(f)}</span>,
    },
    {
      key: 'size',
      label: 'Size',
      width: '100px',
      align: 'right',
      hideBelowLg: true,
      render: (f) => <span className="tabular-nums text-muted">{formatBytes(f.bytes)}</span>,
    },
    { key: 'created', label: 'Created', width: '120px', hideBelow: 'xl', render: (f) => day(f.created_at, '—') },
    { key: 'expires', label: 'Expires', width: '120px', hideBelow: 'xl', render: (f) => day(f.expires_at, 'Never') },
    {
      key: 'actions',
      label: '',
      width: '56px',
      align: 'right',
      render: (f) => {
        const items: RowMenuItem[] = [
          { id: 'details', label: 'View details' },
          ...(mayManage && f.status !== 'deleted'
            ? [{ id: 'delete', label: 'Delete', icon: <IconTrash size={15} />, danger: true }]
            : []),
        ];
        return (
          <RowMenu
            label={`Actions for ${f.filename}`}
            items={items}
            onSelect={(action) => {
              if (action === 'details') setDetailId(f.id);
              else if (action === 'delete') setDeleteTarget({ file: f, projectId: pid });
            }}
          />
        );
      },
    },
  ];

  const filtered = status !== '' || kind !== '';
  const uploadButton =
    mayManage && selected ? (
      <>
        <input
          ref={inputRef}
          type="file"
          multiple
          hidden
          aria-label="Choose files to upload"
          data-testid="files-upload-input"
          onChange={onPick}
        />
        <button type="button" onClick={() => inputRef.current?.click()} className={ADMIN_PRIMARY_BUTTON}>
          <IconCloud size={15} />
          Upload files
        </button>
      </>
    ) : undefined;

  if (!projectsQuery.loading && !projectsQuery.error && projects.length === 0) {
    return (
      <div>
        <ConsoleHeader title="Files" />
        <ConsoleEmpty
          title="No projects to hold files"
          body="Files belong to a project. Create a project first, then upload files here or send them to the Files API with a key from that project."
        />
      </div>
    );
  }

  const s = storage.data;
  const storageLoading = storage.loading && storage.data === null;
  const count = (value: number | null | undefined) => (typeof value === 'number' ? value : undefined);
  const bytes = (value: number | null | undefined) => (typeof value === 'number' ? formatBytes(value) : undefined);

  return (
    <div>
      <ConsoleHeader
        title="Files"
        description="Documents, spreadsheets, images, audio and video your project can hand to a model by id. Each file is processed once — text extracted, pages read, speech transcribed, an index built — and every request that names it reuses the result."
      />

      <AdminToolbar action={uploadButton}>
        <ProjectSelect projects={projects} value={pid} onChange={setProjectId} />
        <div className={SELECT_FIT}>
          <AdminSelect
            label="Status"
            value={status}
            onChange={setStatus}
            options={[
              { value: '', label: 'All statuses' },
              ...FILE_STATUS_FILTERS.map((value) => ({ value, label: STATUS_FILTER_LABEL[value] })),
            ]}
          />
        </div>
        <div className={SELECT_FIT}>
          <AdminSelect
            label="Kind"
            value={kind}
            onChange={setKind}
            options={[
              { value: '', label: 'All kinds' },
              ...FILE_KINDS.map((value) => ({ value, label: KIND_LABEL[value] ?? value })),
            ]}
          />
        </div>
        <button
          type="button"
          onClick={() => {
            reloadList();
            storage.reload();
          }}
          aria-label="Refresh files"
          className={ADMIN_SECONDARY_BUTTON}
        >
          <IconRefresh size={15} />
        </button>
      </AdminToolbar>

      <div className="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="files-storage">
        <StatTile label="Files" value={count(s?.files)} loading={storageLoading} />
        <StatTile label="Stored" value={bytes(s?.bytes)} loading={storageLoading} />
        <StatTile label="Derived" value={bytes(s?.derived_bytes)} loading={storageLoading} />
        <StatTile label="Uploads in progress" value={count(s?.uploads_pending)} loading={storageLoading} />
      </div>

      <UploadTray
        items={uploads}
        live={live}
        currentProjectId={pid}
        onResume={(key) => enqueue([key])}
        onCancel={cancelUpload}
        onDismiss={dismissUpload}
        onDetails={(fileId) => setDetailId(fileId)}
      />

      <div className="mt-5">
        {projectsQuery.error ? (
          <ProjectsLoadError query={projectsQuery} />
        ) : !listPending && !list.error && rows.length === 0 && !hasMore ? (
          filtered ? (
            <ConsoleEmpty
              title="No files match these filters"
              body="Nothing in this project has that status and kind right now. Clear the filters to see every file."
              action={
                <button
                  type="button"
                  className={ADMIN_SECONDARY_BUTTON}
                  onClick={() => {
                    setStatus('');
                    setKind('');
                  }}
                >
                  Clear filters
                </button>
              }
            />
          ) : (
            <ConsoleEmpty
              title="No files in this project"
              body="Upload a file here, or send one to POST /v1/files with a key that holds files.write. Once it is processed, a response can name it by id."
            />
          )
        ) : (
          <ConsoleTable
            columns={columns}
            minWidth={900}
            rows={rows}
            rowKey={(f) => f.id}
            loading={listPending}
            empty="No files in this project."
            error={list.error}
            onRetry={list.reload}
          />
        )}
        {!list.error && hasMore && (
          <div className="mt-4 flex flex-col items-center gap-3">
            {extra?.error && <ErrorPanel message={extra.error} onRetry={() => void loadMore()} />}
            <button
              type="button"
              disabled={extra?.loading ?? false}
              onClick={() => void loadMore()}
              className={ADMIN_SECONDARY_BUTTON}
            >
              {extra?.loading ? 'Loading…' : 'Load more files'}
            </button>
          </div>
        )}
      </div>

      {tracked.map((t) => (
        <LiveFile
          key={`${t.projectId}:${t.fileId}`}
          projectId={t.projectId}
          fileId={t.fileId}
          onFile={onLiveFile}
          onGone={onGone}
          follow={deps?.follow}
        />
      ))}

      <FileDetailDialog
        projectId={pid}
        file={detailFile}
        onClose={() => setDetailId(null)}
        mayManage={mayManage}
        onDelete={(file) => {
          setDetailId(null);
          setDeleteTarget({ file, projectId: pid });
        }}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title={deleteTarget ? `Delete ${deleteTarget.file.filename}?` : 'Delete this file?'}
        body={
          deleteTarget
            ? `Requests that name ${deleteTarget.file.id} get a 404 from now on. Its bytes and everything made from them — extracted text, transcripts and the search index — are removed immediately, unless another file in this project was uploaded with the same content: then they stay until no file in the project holds that content.`
            : ''
        }
        confirmLabel="Delete"
        onConfirm={() => {
          const target = deleteTarget;
          setDeleteTarget(null);
          if (target) void remove(target);
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
