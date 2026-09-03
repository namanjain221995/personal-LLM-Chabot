'use client';

/**
 * The tool-access settings list, shared by the workspace Access page and the
 * per-member dialog — one component so both places phrase the same decision
 * the same way, and so both inherit any fix to either.
 *
 * ONE GRID, NOT FIVE FLEX ROWS (2026-09-04). Every row is
 * `minmax(0,1fr) auto`: the text takes the slack, the switch sits in its own
 * track, and `items-center` centres it against the row. That is what makes
 * the switches share an exact X — and keeps sharing it when one row grows a
 * third line of helper text, which is precisely where a per-row flex layout
 * drifts. The row is also the only place padding is declared, so no card
 * can disagree with another about its own inset.
 *
 * A row whose parent is off is disabled and SAYS which parent it needs. The
 * orchestrator enforces that dependency anyway (authn/features.py), so a
 * toggle that silently reverted on save would be the worse lie.
 */

import { useId, type ReactNode } from 'react';
import type { FeatureSpec } from './api';
import { Switch } from './Switch';

export { Switch };

export function AccessSettingRow({
  title,
  description,
  helperText,
  enabled,
  disabled = false,
  onChange,
}: {
  title: string;
  description: string;
  /** Muted line under the description: inheritance, or the missing parent. */
  helperText?: ReactNode;
  enabled: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}) {
  const descriptionId = useId();
  return (
    <li className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-6 gap-y-1 px-5 py-4">
      <div className="min-w-0">
        <div
          className={`text-sm font-medium ${disabled ? 'text-muted' : 'text-ink'}`}
        >
          {title}
        </div>
        <p id={descriptionId} className="mt-1 text-xs leading-relaxed text-muted">
          {description}
        </p>
        {helperText ? (
          <p className="mt-1.5 text-xs leading-relaxed text-faint">{helperText}</p>
        ) : null}
      </div>
      <Switch
        checked={enabled}
        disabled={disabled}
        label={title}
        describedBy={descriptionId}
        onChange={onChange}
      />
    </li>
  );
}

export function AccessSettingsCard({ children }: { children: ReactNode }) {
  return (
    <ul className="divide-y divide-[var(--admin-separator)] overflow-hidden rounded-xl border border-border bg-surface">
      {children}
    </ul>
  );
}

export function FeatureToggles({
  catalog,
  values,
  onChange,
  disabled = false,
  /** Per-feature note under the description — e.g. "Workspace default: on". */
  note,
}: {
  catalog: FeatureSpec[];
  values: Record<string, boolean>;
  onChange: (id: string, next: boolean) => void;
  disabled?: boolean;
  note?: (spec: FeatureSpec) => string | null;
}) {
  const labelOf = (id: string) => catalog.find((f) => f.id === id)?.label ?? id;

  return (
    <AccessSettingsCard>
      {catalog.map((spec) => {
        const parentOff = Boolean(spec.requires && !values[spec.requires]);
        return (
          <AccessSettingRow
            key={spec.id}
            title={spec.label}
            description={spec.hint}
            helperText={
              parentOff
                ? `Needs ${labelOf(spec.requires as string)}, which is off.`
                : (note?.(spec) ?? null)
            }
            enabled={Boolean(values[spec.id]) && !parentOff}
            disabled={disabled || parentOff}
            onChange={(next) => onChange(spec.id, next)}
          />
        );
      })}
    </AccessSettingsCard>
  );
}
