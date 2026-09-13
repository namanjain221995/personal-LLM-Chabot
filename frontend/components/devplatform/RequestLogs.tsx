'use client';

/**
 * The request log: metadata about calls, and nothing else.
 *
 * CONTRACT §16 is the whole design of this table — "prompt and output content
 * are NOT stored by default. Request logs keep metadata only: request id,
 * time, project, model, status, tokens, duration, stream/background flags,
 * error code, key prefix." Every column below is on that list and there is no
 * row-expand, no "view prompt", no detail drawer that could grow one, because
 * the data to fill it does not exist and must not be made to.
 *
 * The page size is the router's own bound (`console_api.request_logs`,
 * `limit` 1…200, newest first). It has no offset and no total, so this
 * table has no pager: a pager over a list that cannot be paged would be a
 * control that silently shows page one forever — which is what the previous
 * `offset=` did until 2026-09-13.
 *
 * The request id is the point of the table: it is what a developer quotes in a
 * support message, and what ties a line here to the `X-Request-Id` their own
 * client logged. So it is copyable, in full, at any width.
 */

import { useEffect, useMemo, useState } from 'react';
import { AdminSelect, AdminToolbar } from '@/components/admin/controls';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { NOT_MEASURED, compact, duration } from '@/components/admin/analytics/format';
import { CopyButton } from '@/components/CopyButton';
import { formatRelative, formatWhen } from '@/lib/format';
import {
  ConsoleEmpty,
  ConsoleTable,
  ProjectSelect,
  ProjectsLoadError,
  useProjects,
  type ConsoleColumn,
} from './shared';
import { useConsole } from './useConsole';
import { consolePaths } from './paths';
import { useConsoleStatus } from './status';
import type { RequestLogPage, RequestLogRow } from './types';

/** The most recent requests shown; console_api caps `limit` at 200. */
export const LOG_LIMIT = 100;

/** Status words, with the tone carried by text as well as colour. */
export function StatusCell({ row }: { row: RequestLogRow }) {
  // `error_code` is "" on a request that did not fail — the router sends a
  // string, never null — so emptiness is the test, not null.
  const bad = row.status === 'failed' || row.error_code !== '';
  const word = row.error_code || row.status;
  // An error code is wider than the 88px a 120px track leaves inside its
  // padding ("model_unavailable" is 111px), and the cell is nowrap, so it ran
  // 23px into the next column — or past the table's edge between md and lg,
  // where Status is the last column (responsive audit, 2026-09-13). It wraps
  // after an underscore first, and anywhere only if a part is still too long.
  const parts = word.split('_');
  return (
    <span
      data-testid="log-status"
      className={`block whitespace-normal text-xs [overflow-wrap:anywhere] ${bad ? 'text-danger' : 'text-muted'}`}
    >
      {parts.map((part, i) => (
        <span key={i}>
          {part}
          {i < parts.length - 1 && (
            <>
              _<wbr />
            </>
          )}
        </span>
      ))}
    </span>
  );
}

export function RequestLogsPanel() {
  const projectsQuery = useProjects();
  const projects = projectsQuery.data?.projects ?? [];
  const [projectId, setProjectId] = useState('');
  const [status, setStatus] = useState('');
  const selected = projects.find((p) => p.id === projectId) ?? projects[0] ?? null;

  const logs = useConsole<RequestLogPage>(
    consolePaths.logs(selected?.id ?? ''),
    { status, limit: LOG_LIMIT },
    selected !== null,
  );
  const { announce } = useConsoleStatus();
  const rows = logs.data?.requests ?? [];
  // Before the project list arrives the log request has not been sent, so
  // "no requests yet" is not known — the table's skeleton says so instead.
  const projectsPending = projectsQuery.loading && projectsQuery.data === null;

  useEffect(() => {
    if (projectsQuery.error) announce(projectsQuery.error);
    else if (projectsPending || logs.loading) announce('Loading request logs.');
    else if (logs.error) announce(logs.error);
    else announce(`${rows.length} request${rows.length === 1 ? '' : 's'} listed.`);
  }, [projectsQuery.error, projectsPending, logs.loading, logs.error, rows.length, announce]);

  const columns: ConsoleColumn<RequestLogRow>[] = useMemo(
    () => [
      {
        key: 'request',
        label: 'Request',
        render: (r) => (
          // VISUAL QA, 2026-09-13: at 1440px the eight fixed columns left
          // this one ~146px, the cell is whitespace-nowrap, so the id could
          // not break and ran under "When" with the Copy chip on top of it.
          // The id now wraps in its own cell (break-all needs
          // whitespace-normal to break at all), the button is the icon form,
          // and the other tracks were narrowed to give the id room.
          <div className="flex min-w-0 items-center gap-1">
            <code className="min-w-0 flex-1 whitespace-normal break-all font-mono text-xs text-ink">
              {r.request_id}
            </code>
            <CopyButton
              text={r.request_id}
              label="Copy request id"
              variant="icon"
              className="shrink-0"
            />
          </div>
        ),
      },
      {
        key: 'when',
        label: 'When',
        width: '120px',
        hideBelowLg: true,
        render: (r) =>
          r.created_at ? (
            <span className="text-muted" title={formatWhen(r.created_at)}>
              {formatRelative(r.created_at)}
            </span>
          ) : (
            <span className="text-faint">{NOT_MEASURED}</span>
          ),
      },
      {
        key: 'model',
        label: 'Model',
        width: '140px',
        hideBelowLg: true,
        render: (r) => <span className="block truncate text-muted" title={r.model}>{r.model}</span>,
      },
      {
        key: 'status',
        label: 'Status',
        width: '120px',
        render: (r) => <StatusCell row={r} />,
      },
      {
        key: 'tokens',
        label: 'Tokens',
        width: '110px',
        align: 'right',
        // On a phone a line is the id (copyable) and its status; the rest is
        // one tap wider at lg (visual QA, 2026-09-13).
        hideBelowLg: true,
        render: (r) => (
          <span className="tabular-nums text-muted">
            {r.input_tokens == null && r.output_tokens == null
              ? NOT_MEASURED
              : `${compact(r.input_tokens)} → ${compact(r.output_tokens)}`}
          </span>
        ),
      },
      {
        key: 'key',
        label: 'Key',
        width: '140px',
        // Key and Duration fold below xl so the id and its status fit the
        // 720px a 1024px laptop leaves (responsive audit, 2026-09-13).
        hideBelow: 'xl',
        render: (r) =>
          r.key ? (
            // The last four are what tell two keys apart, so the NAME is what
            // truncates: "smoke key · …7ON6" lost its "7ON6" at 108px.
            <span className="flex min-w-0 text-xs text-muted" title={r.key.name}>
              <span className="min-w-0 truncate">{r.key.name}</span>
              <span className="shrink-0">&nbsp;· …{r.key.last_four}</span>
            </span>
          ) : (
            <span className="text-faint">{NOT_MEASURED}</span>
          ),
      },
      {
        key: 'duration',
        label: 'Duration',
        width: '90px',
        align: 'right',
        hideBelow: 'xl',
        render: (r) => (
          <span className="tabular-nums text-muted">{duration(r.duration_ms)}</span>
        ),
      },
      {
        key: 'shape',
        label: 'Shape',
        width: '100px',
        // At 1280px all eight tracks left the id 156px — three lines of a
        // 36-character id. Shape is the cheapest thing to fold until 1536px.
        hideBelow: '2xl',
        render: (r) => (
          <span className="text-xs text-faint">
            {r.background ? 'Background' : r.streamed ? 'Streamed' : 'Sync'}
          </span>
        ),
      },
    ],
    [],
  );

  if (!projectsQuery.loading && !projectsQuery.error && projects.length === 0) {
    return (
      <div>
        <ConsoleHeader title="Request logs" />
        <ConsoleEmpty
          title="No projects to log"
          body="Requests are logged per project. Create one, call /v1 with its key, and each call appears here as a line of metadata — never its prompt or its answer."
        />
      </div>
    );
  }

  return (
    <div>
      <ConsoleHeader
        title="Request logs"
        description="Metadata for every /v1 call: id, time, model, status, tokens and duration. Prompts and completions are not stored, so they are not here."
      />

      <AdminToolbar>
        <ProjectSelect
          projects={projects}
          value={selected?.id ?? ''}
          onChange={setProjectId}
        />
        <AdminSelect
          value={status}
          onChange={setStatus}
          label="Filter by status"
          options={[
            { value: '', label: 'All statuses' },
            { value: 'completed', label: 'Completed' },
            { value: 'failed', label: 'Failed' },
            { value: 'cancelled', label: 'Cancelled' },
            { value: 'in_progress', label: 'In progress' },
            { value: 'queued', label: 'Queued' },
          ]}
        />
      </AdminToolbar>

      <div className="mt-5">
        {projectsQuery.error ? (
          <ProjectsLoadError query={projectsQuery} />
        ) : !projectsPending && !logs.loading && !logs.error && rows.length === 0 ? (
          <ConsoleEmpty
            title={status ? 'No requests match this filter' : 'No requests yet'}
            body={
              status
                ? 'Nothing in this project has that status in the retained window.'
                : 'This project has not been called yet. Every request through /v1 appears here, with its request id, for as long as the project retains logs.'
            }
          />
        ) : (
          <>
            <ConsoleTable
              columns={columns}
              // The fixed tracks shown from xl (720px) plus a 220px id column:
              // no more than the ~960px a 1280px window gives the table once
              // the page's scrollbar is counted.
              minWidth={940}
              rows={rows}
              rowKey={(r) => r.id}
              loading={projectsPending || (logs.loading && logs.data === null)}
              empty="No requests yet."
              error={logs.error}
              onRetry={logs.reload}
            />
            {rows.length >= LOG_LIMIT && (
              <p className="mt-3 text-xs text-faint">
                Showing the {LOG_LIMIT.toLocaleString()} most recent requests.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
