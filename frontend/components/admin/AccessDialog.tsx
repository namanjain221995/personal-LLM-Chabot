'use client';

/**
 * "Manage access" for one member — which tools they may use.
 *
 * The dialog edits the RESOLVED state, not the raw override map, because
 * that is what an administrator is actually deciding ("can Bob use Deep
 * Research?"). On save it sends only the features that differ from what the
 * workspace grants by default, so a later change to the workspace default
 * still reaches everyone who was never explicitly set — the whole point of
 * having two layers (orchestrator authn/features.py).
 */

import { useCallback, useEffect, useState } from 'react';
import { Loader } from '@/components/Loader';
import { useToast } from '@/components/Providers';
import {
  AdminApiError,
  adminJson,
  adminPut,
  applyFeatureRules,
  type MemberAccess,
} from './api';
import { AdminDialog } from './AdminDialog';
import { ADMIN_PRIMARY_BUTTON, ADMIN_SECONDARY_BUTTON } from './controls';
import { FeatureToggles } from './FeatureToggles';
import { SkeletonLine } from './ui';

export function AccessDialog({
  member,
  onClose,
  onSaved,
}: {
  member: { id: number; name: string } | null;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const { toast } = useToast();
  const [data, setData] = useState<MemberAccess | null>(null);
  const [values, setValues] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const id = member?.id;

  useEffect(() => {
    if (id === undefined) {
      setData(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setData(null);
    setError(null);
    adminJson<MemberAccess>(`members/${id}/access`)
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
              : 'That member’s access could not be loaded.',
          );
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const save = useCallback(async () => {
    if (!data || id === undefined) return;
    // Only what DIFFERS from the workspace default is stored: everything
    // else stays inherited and follows a later default change.
    const overrides: Record<string, boolean> = {};
    for (const spec of data.catalog) {
      const want = Boolean(values[spec.id]);
      if (want !== Boolean(data.workspace_resolved[spec.id])) {
        overrides[spec.id] = want;
      }
    }
    setSaving(true);
    try {
      await adminPut(`members/${id}/access`, { features: overrides });
      toast(`Access updated for ${member?.name ?? 'this member'}.`);
      onSaved?.();
      onClose();
    } catch (err) {
      toast(
        err instanceof AdminApiError
          ? err.message
          : 'The access could not be saved.',
        'error',
      );
    } finally {
      setSaving(false);
    }
  }, [data, id, values, member, toast, onSaved, onClose]);

  const inherited = (specId: string) =>
    data && Boolean(values[specId]) === Boolean(data.workspace_resolved[specId])
      ? `Workspace default: ${data.workspace_resolved[specId] ? 'on' : 'off'}`
      : 'Set for this member';

  return (
    <AdminDialog
      open={member !== null}
      title={`Access · ${member?.name ?? ''}`}
      size="md"
      onClose={onClose}
    >
      {error ? (
        <p className="text-sm text-danger">{error}</p>
      ) : !data ? (
        <div className="space-y-2 py-2">
          <SkeletonLine className="w-full" />
          <SkeletonLine className="w-3/4" />
          <SkeletonLine className="w-2/3" />
        </div>
      ) : (
        <>
          <p className="mb-3 text-xs leading-relaxed text-muted">
            {data.locked
              ? 'Super admins always have every tool — this is what keeps a workspace from being locked out of its own settings.'
              : 'Turning a tool off hides it in the composer and refuses it on the server, even from an older browser tab.'}
          </p>
          <FeatureToggles
            catalog={data.catalog}
            values={values}
            disabled={data.locked || saving}
            note={(spec) => inherited(spec.id)}
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
          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className={ADMIN_SECONDARY_BUTTON}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={save}
              disabled={saving || data.locked}
              className={ADMIN_PRIMARY_BUTTON}
            >
              {saving && <Loader size={14} />}
              {saving ? 'Saving…' : 'Save access'}
            </button>
          </div>
        </>
      )}
    </AdminDialog>
  );
}
