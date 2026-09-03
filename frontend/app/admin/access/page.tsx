'use client';

/**
 * /admin/access — what every member of this workspace may use by default.
 *
 * This page sets the DEFAULT only. A member whose access was set
 * individually (Members → ⋯ → Manage access) keeps their setting; that is
 * what an override is for, and the copy says so rather than leaving an
 * administrator to discover it.
 *
 * Layout (2026-09-04): a reading-width column, one settings card, and every
 * switch on the same grid track — see FeatureToggles for why that is one
 * grid rather than five flex rows. Save is state-aware: it does nothing to
 * look at until something actually differs from what the server holds.
 */

import { useCallback, useEffect, useState } from 'react';
import { Loader } from '@/components/Loader';
import { useToast } from '@/components/Providers';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import {
  AdminApiError,
  adminJson,
  adminPut,
  applyFeatureRules,
  can,
  type AccessSettings,
} from '@/components/admin/api';
import { ADMIN_PRIMARY_BUTTON } from '@/components/admin/controls';
import { FeatureToggles } from '@/components/admin/FeatureToggles';
import { ErrorPanel, PageHeader, SkeletonLine } from '@/components/admin/ui';

export default function AdminAccessPage() {
  const me = useAdminMe();
  const { toast } = useToast();
  const [data, setData] = useState<AccessSettings | null>(null);
  const [values, setValues] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const editable = can(me, 'settings.manage');

  useEffect(() => {
    let cancelled = false;
    setError(null);
    adminJson<AccessSettings>('access')
      .then((res) => {
        if (cancelled) return;
        setData(res);
        setValues({ ...res.resolved });
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof AdminApiError
              ? err.message
              : 'The access settings could not be loaded.',
          );
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const save = useCallback(async () => {
    if (!data) return;
    // Store only what differs from the built-in default, so a later change
    // to a built-in still reaches a workspace that never touched that tool.
    const overrides: Record<string, boolean> = {};
    for (const spec of data.catalog) {
      const want = Boolean(values[spec.id]);
      if (want !== spec.default) overrides[spec.id] = want;
    }
    setSaving(true);
    try {
      const res = await adminPut<{
        workspace_defaults: Record<string, boolean>;
        resolved: Record<string, boolean>;
      }>('access', { features: overrides });
      // Re-seed from the SERVER's answer, not from the local guess: that is
      // what makes the button go quiet again, and what shows the dependency
      // rules the orchestrator applied on the way in.
      setData({ ...data, ...res });
      setValues({ ...res.resolved });
      toast('Workspace access updated.');
    } catch (err) {
      toast(
        err instanceof AdminApiError
          ? err.message
          : 'The access settings could not be saved.',
        'error',
      );
    } finally {
      setSaving(false);
    }
  }, [data, values, toast]);

  const dirty =
    data !== null &&
    data.catalog.some(
      (spec) => Boolean(values[spec.id]) !== Boolean(data.resolved[spec.id]),
    );

  return (
    <div className="max-w-3xl">
      <PageHeader
        title="Access"
        subtitle="Which tools members of this workspace can use"
        actions={
          editable ? (
            <button
              type="button"
              onClick={save}
              disabled={!dirty || saving}
              className={ADMIN_PRIMARY_BUTTON}
            >
              {saving && <Loader size={14} />}
              {saving ? 'Saving…' : 'Save changes'}
            </button>
          ) : undefined
        }
      />

      {error ? (
        <div className="mt-6">
          <ErrorPanel message={error} onRetry={() => setAttempt((n) => n + 1)} />
        </div>
      ) : !data ? (
        <div className="mt-6 space-y-4 rounded-xl border border-border bg-surface p-5">
          <SkeletonLine className="w-1/2" />
          <SkeletonLine className="w-2/3" />
          <SkeletonLine className="w-1/3" />
        </div>
      ) : (
        <>
          <p className="mt-6 text-sm leading-relaxed text-muted">
            This is the default for the whole workspace. A member whose access
            was set individually keeps their own setting — change that under{' '}
            <span className="text-ink">Members → ⋯ → Manage access</span>.
            {!editable && ' Only a super admin can change these.'}
          </p>

          <div className="mt-5">
            <FeatureToggles
              catalog={data.catalog}
              values={values}
              disabled={!editable || saving}
              note={(spec) =>
                Boolean(values[spec.id]) === spec.default
                  ? null
                  : `Changed from the built-in default (${spec.default ? 'on' : 'off'})`
              }
              onChange={(featureId, next) =>
                setValues((prev) =>
                  applyFeatureRules(
                    data.catalog,
                    { ...prev, [featureId]: next },
                    featureId,
                  ),
                )
              }
            />
          </div>

          <p className="mt-4 text-xs leading-relaxed text-faint">
            Turning a tool off hides it in the composer and refuses it on the
            server — an old browser tab cannot use what was taken away. Super
            admins always keep every tool.
          </p>
        </>
      )}
    </div>
  );
}
