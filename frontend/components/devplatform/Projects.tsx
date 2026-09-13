'use client';

/**
 * Projects — the unit a key, a quota, a log line and a webhook all belong to.
 *
 * Straight copy of the members roster's shape (AdminTable + AdminToolbar +
 * RowMenu + ConfirmDialog + a toast on both outcomes), because a second table
 * idiom in the same product is a second thing to learn for no gain.
 *
 * Disabling is a ConfirmDialog rather than a menu item that just fires: a
 * disabled project 401s every key pointing at it on the next request
 * (CONTRACT §4 resolves the project on every call, with no cached state), so
 * it is an action that stops a customer's integration inside a second.
 */

import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { useToast } from '@/components/Providers';
import { IconBan, IconEye } from '@/components/admin/icons';
import { IconPlus } from '@/components/icons';
import { formatDay, formatWhen } from '@/lib/format';
import { can, type Me } from '@/components/admin/api';
import type { AdminColumn } from '@/components/admin/AdminTable';
import {
  AdminDialog,
  FIELD_INPUT,
  Field,
  PRIMARY_BUTTON,
  SECONDARY_BUTTON,
} from '@/components/admin/AdminDialog';
import { RowMenu, type RowMenuItem } from '@/components/admin/RowMenu';
import { StatusChip } from '@/components/admin/chips';
import {
  ADMIN_PRIMARY_BUTTON,
  AdminToolbar,
} from '@/components/admin/controls';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { Loader } from '@/components/Loader';
import { IconAlert } from '@/components/icons';
import { consolePatch, consolePost, messageOf } from './api';
import { consolePaths } from './paths';
import {
  ConsoleEmpty,
  ConsoleTable,
  EnvironmentChip,
  MonoValue,
  limitsEnforcement,
  usageLimitText,
  useProjects,
} from './shared';
import { useConsoleStatus } from './status';
import type { Project } from './types';

export function ProjectsPanel({ me }: { me: Me }) {
  const { data, loading, error, reload } = useProjects();
  const { toast } = useToast();
  const { announce } = useConsoleStatus();
  const [createOpen, setCreateOpen] = useState(false);
  const [disableTarget, setDisableTarget] = useState<Project | null>(null);
  const [detail, setDetail] = useState<Project | null>(null);

  const projects = data?.projects ?? [];
  const manage = can(me, 'api.projects.manage');

  useEffect(() => {
    if (loading) announce('Loading projects.');
    else if (error) announce(error);
    else announce(`${projects.length} project${projects.length === 1 ? '' : 's'}.`);
  }, [loading, error, projects.length, announce]);

  async function setStatus(project: Project, disabled: boolean) {
    try {
      await consolePatch(consolePaths.project(project.id), {
        status: disabled ? 'disabled' : 'active',
      });
      toast(
        disabled
          ? `${project.name} disabled — its keys stop working immediately.`
          : `${project.name} re-enabled.`,
      );
      reload();
    } catch (err) {
      toast(messageOf(err, 'The project could not be changed.'), 'error');
    }
  }

  function menuFor(project: Project): RowMenuItem[] {
    const items: RowMenuItem[] = [
      { id: 'detail', label: 'View settings', icon: <IconEye size={15} /> },
    ];
    if (manage) {
      const disabled = project.status === 'disabled';
      items.push({
        id: 'status',
        label: disabled ? 'Enable' : 'Disable',
        icon: <IconBan size={15} />,
        danger: !disabled,
      });
    }
    return items;
  }

  const columns: AdminColumn<Project>[] = useMemo(
    () => [
      {
        key: 'name',
        label: 'Project',
        render: (p) => (
          <div className="min-w-0">
            <div className="truncate font-medium text-ink" title={p.name}>
              {p.name}
            </div>
            <MonoValue value={p.id} />
          </div>
        ),
      },
      {
        key: 'environment',
        label: 'Environment',
        width: '140px',
        // Below lg its 140px would leave the project name 52px on a phone
        // (visual QA, 2026-09-13); status and the row menu matter more there.
        hideBelowLg: true,
        render: (p) => <EnvironmentChip environment={p.environment} />,
      },
      {
        key: 'status',
        label: 'Status',
        width: '120px',
        render: (p) => <StatusChip status={p.status} />,
      },
      {
        key: 'created',
        label: 'Created',
        width: '140px',
        hideBelowLg: true,
        render: (p) =>
          p.created_at ? (
            <span className="text-muted" title={formatWhen(p.created_at)}>
              {formatDay(p.created_at)}
            </span>
          ) : (
            <span className="text-faint">—</span>
          ),
      },
      {
        key: 'actions',
        label: '',
        width: '56px',
        align: 'right',
        render: (p) => (
          <RowMenu
            label={`Actions for ${p.name}`}
            items={menuFor(p)}
            onSelect={(action) => {
              if (action === 'detail') setDetail(p);
              else if (p.status === 'disabled') void setStatus(p, false);
              else setDisableTarget(p);
            }}
          />
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [manage],
  );

  return (
    <div>
      <ConsoleHeader
        title="Projects"
        description="A project owns its keys, its model allowlist, its limits and its request log. Keep production and experiments apart by giving them separate projects."
      />

      <AdminToolbar
        action={
          manage ? (
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              className={ADMIN_PRIMARY_BUTTON}
            >
              <IconPlus size={15} />
              New project
            </button>
          ) : undefined
        }
      >
        <span className="text-xs text-faint">
          {projects.length.toLocaleString()} project
          {projects.length === 1 ? '' : 's'}
        </span>
      </AdminToolbar>

      <div className="mt-5">
        {!loading && !error && projects.length === 0 ? (
          <ConsoleEmpty
            title="No projects yet"
            body="Every API key belongs to a project, so this is the first thing to create. A project carries the model allowlist, its limits and the request log for everything done with its keys."
            action={
              manage ? (
                <button
                  type="button"
                  onClick={() => setCreateOpen(true)}
                  className={ADMIN_PRIMARY_BUTTON}
                >
                  <IconPlus size={15} />
                  New project
                </button>
              ) : undefined
            }
          />
        ) : (
          <ConsoleTable
            columns={columns}
            minWidth={740}
            rows={projects}
            rowKey={(p) => p.id}
            loading={loading && data === null}
            empty="No projects yet."
            error={error}
            onRetry={reload}
          />
        )}
      </div>

      <CreateProjectDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(project) => {
          toast(`${project.name} created.`);
          reload();
        }}
      />

      <ProjectDetailDialog
        project={detail}
        enforcement={limitsEnforcement(detail?.limits, detail, data)}
        onClose={() => setDetail(null)}
      />

      <ConfirmDialog
        open={disableTarget !== null}
        title="Disable this project?"
        body={
          disableTarget
            ? `Every key in ${disableTarget.name} stops working on its next request. Nothing is deleted and the project can be enabled again.`
            : ''
        }
        confirmLabel="Disable"
        onConfirm={() => {
          const target = disableTarget;
          setDisableTarget(null);
          if (target) void setStatus(target, true);
        }}
        onCancel={() => setDisableTarget(null)}
      />
    </div>
  );
}

/** Name and environment only — everything else has a sensible default. */
function CreateProjectDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (project: Project) => void;
}) {
  const [name, setName] = useState('');
  const [environment, setEnvironment] = useState<'test' | 'live'>('test');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName('');
    setEnvironment('test');
    setBusy(false);
    setError(null);
  }, [open]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await consolePost<{ project: Project }>(consolePaths.projects(), {
        name: name.trim(),
        environment,
      });
      onCreated(created.project);
      onClose();
    } catch (err) {
      setError(messageOf(err, 'The project could not be created.'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AdminDialog open={open} title="New project" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder="Billing assistant"
            autoComplete="off"
            className={FIELD_INPUT}
          />
        </Field>
        <Field label="Environment">
          <select
            value={environment}
            onChange={(e) => setEnvironment(e.target.value as 'test' | 'live')}
            className={FIELD_INPUT}
          >
            <option value="test">Test</option>
            <option value="live">Live</option>
          </select>
        </Field>
        <p className="text-xs leading-relaxed text-faint">
          The environment fixes the prefix of every key minted here
          (<code className="font-mono">tsk_test_</code> or{' '}
          <code className="font-mono">tsk_live_</code>) so a test key is
          recognisable at a glance in a log or a scanner.
        </p>
        {error && (
          <p role="alert" className="flex items-start gap-1.5 text-sm text-danger">
            <IconAlert size={15} className="mt-0.5 shrink-0" />
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim()}
            className={PRIMARY_BUTTON}
          >
            {busy && <Loader size={16} />}
            Create project
          </button>
        </div>
      </form>
    </AdminDialog>
  );
}

/** Read-only settings, so a person can check a limit without leaving the tab. */
function ProjectDetailDialog({
  project,
  enforcement,
  onClose,
}: {
  project: Project | null;
  /** `limitsEnforcement` of the project and its list; null = pre-switch server. */
  enforcement: boolean | null;
  onClose: () => void;
}) {
  return (
    <AdminDialog
      open={project !== null}
      title={project ? project.name : 'Project'}
      size="md"
      onClose={onClose}
    >
      {project && (
        <dl className="space-y-2 text-sm">
          <Row label="Project id" value={project.id} mono />
          <Row label="Environment" value={project.environment} />
          <Row label="Requests / minute" value={usageLimitText(project.limits.rpm, enforcement)} />
          <Row
            label="Input tokens / minute"
            value={usageLimitText(project.limits.input_tpm, enforcement)}
          />
          <Row
            label="Output tokens / minute"
            value={usageLimitText(project.limits.output_tpm, enforcement)}
          />
          <Row
            label="Concurrent requests"
            value={usageLimitText(project.limits.max_concurrency, enforcement)}
          />
          <Row
            label="Daily token quota"
            value={usageLimitText(project.limits.daily_token_quota, enforcement)}
          />
          <Row
            label="Log retention"
            value={
              project.retention_days === null
                ? 'Platform default'
                : `${project.retention_days} days`
            }
          />
          <Row
            label="Models"
            value={
              project.allowed_models.length
                ? project.allowed_models.join(', ')
                : 'Every model this platform publishes'
            }
          />
          <Row
            label="Browser origins"
            value={
              project.allowed_origins.length
                ? project.allowed_origins.join(', ')
                : 'Not restricted'
            }
          />
        </dl>
      )}
      <div className="mt-4 flex justify-end">
        <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>
          Done
        </button>
      </div>
    </AdminDialog>
  );
}

function Row({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-[var(--admin-separator)] pb-2">
      <dt className="text-xs text-faint">{label}</dt>
      <dd className={`text-right text-sm text-ink ${mono ? 'break-all font-mono text-xs' : ''}`}>
        {value}
      </dd>
    </div>
  );
}
