'use client';

/**
 * Which models the public API publishes.
 *
 * CONTRACT §15: a code-level registry is the source of truth and the database
 * may only NARROW it — a `public_models` row can disable a model the code
 * declares, never enable one it does not. So this table lists what the code
 * declares and the only control on it is a switch that turns one off. There is
 * no "add model" button, because there is no such operation; infrastructure
 * cannot publish a model by appearing in Compose.
 *
 * Turning a model off is `api.models.manage`, which CONTRACT §6 gives to a
 * SUPER ADMIN alone: an admin may run projects, not decide what the platform
 * exposes to the world. Without the capability this page is a read-only list,
 * and the orchestrator refuses the call regardless.
 *
 * The context ceilings are read from the engine at runtime upstream, never
 * hard-coded — which is why they are `number | null` here and render as "—"
 * when the engine has not reported.
 */

import { useEffect, useMemo, useState } from 'react';
import { useToast } from '@/components/Providers';
import { can, type Me } from '@/components/admin/api';
import type { AdminColumn } from '@/components/admin/AdminTable';
import { Switch } from '@/components/admin/Switch';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import { NOT_MEASURED, compact } from '@/components/admin/analytics/format';
import { consolePut, messageOf } from './api';
import { consolePaths } from './paths';
import { ConsoleEmpty, ConsoleTable, MonoValue } from './shared';
import { useConsole } from './useConsole';
import { useConsoleStatus } from './status';
import type { ConsoleModel, ModelList } from './types';

function capabilityWords(model: ConsoleModel): string {
  const words = [
    model.capabilities?.chat ? 'Chat' : null,
    model.capabilities?.streaming ? 'Streaming' : null,
    model.capabilities?.vision ? 'Vision' : null,
    model.capabilities?.tools ? 'Tools' : null,
  ].filter(Boolean) as string[];
  return words.length ? words.join(' · ') : 'None declared';
}

export function ModelsPanel({ me }: { me: Me }) {
  const { data, loading, error, reload } = useConsole<ModelList>(consolePaths.models());
  const { toast } = useToast();
  const { announce } = useConsoleStatus();
  const [pending, setPending] = useState<string | null>(null);

  const models = data?.models ?? [];
  const mayManage = can(me, 'api.models.manage');

  useEffect(() => {
    if (loading) announce('Loading models.');
    else if (error) announce(error);
    else announce(`${models.length} model${models.length === 1 ? '' : 's'}.`);
  }, [loading, error, models.length, announce]);

  async function setEnabled(model: ConsoleModel, enabled: boolean) {
    setPending(model.id);
    try {
      // PUT, as console_api.set_model_enabled declares it (this was a PATCH
      // the router never served until 2026-09-13).
      await consolePut(consolePaths.model(model.id), { enabled });
      toast(
        enabled
          ? `${model.id} is available to API keys again.`
          : `${model.id} is no longer offered to API keys.`,
      );
      reload();
    } catch (err) {
      toast(messageOf(err, 'The model could not be changed.'), 'error');
    } finally {
      setPending(null);
    }
  }

  const columns: AdminColumn<ConsoleModel>[] = useMemo(
    () => [
      {
        key: 'model',
        label: 'Model',
        render: (m) => (
          <div className="min-w-0">
            <div className="truncate font-medium text-ink">{m.id}</div>
            <MonoValue value={capabilityWords(m)} />
          </div>
        ),
      },
      {
        key: 'input',
        label: 'Max input tokens',
        width: '170px',
        align: 'right',
        // Below lg the publish switch needs the room more than the ceiling
        // does: 170px here left the model name 48px on a phone (2026-09-13).
        hideBelowLg: true,
        render: (m) => (
          <span className="tabular-nums text-muted">
            {m.max_input_tokens == null ? NOT_MEASURED : compact(m.max_input_tokens)}
          </span>
        ),
      },
      {
        key: 'output',
        label: 'Max output tokens',
        width: '170px',
        align: 'right',
        hideBelowLg: true,
        render: (m) => (
          <span className="tabular-nums text-muted">
            {m.max_output_tokens == null
              ? NOT_MEASURED
              : compact(m.max_output_tokens)}
          </span>
        ),
      },
      {
        key: 'published',
        label: 'Published',
        width: '150px',
        align: 'right',
        render: (m) =>
          mayManage ? (
            <Switch
              checked={m.enabled}
              onChange={(next) => void setEnabled(m, next)}
              label={`Publish ${m.id} to API keys`}
              disabled={pending === m.id}
            />
          ) : (
            <span className="text-xs text-muted">
              {m.enabled ? 'Published' : 'Withdrawn'}
            </span>
          ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mayManage, pending],
  );

  return (
    <div>
      <ConsoleHeader
        title="Models"
        description="What the public API publishes. The list itself comes from code — this page can withdraw a model from the API, never add one."
      />

      <div className="mt-2">
        {!loading && !error && models.length === 0 ? (
          <ConsoleEmpty
            title="No models are published"
            body="The platform declares its public models in code and narrows them here. An empty list means the deployment has published none — nothing on this page can create one."
          />
        ) : (
          <ConsoleTable
            columns={columns}
            minWidth={860}
            rows={models}
            rowKey={(m) => m.id}
            loading={loading && data === null}
            empty="No models are published."
            error={error}
            onRetry={reload}
          />
        )}
      </div>
    </div>
  );
}
