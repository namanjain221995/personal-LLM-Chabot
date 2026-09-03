'use client';

/**
 * The tool-access switch list, shared by the workspace defaults page and the
 * per-member dialog — one component so both places phrase the same decision
 * the same way.
 *
 * Each row is a real <button role="switch">: a checkbox would be smaller
 * than a finger and would not read as "on/off" to a screen reader. A row
 * whose parent is off is disabled and says which parent it needs, because
 * the orchestrator enforces that dependency anyway (features.py) and a
 * toggle that silently reverts on save is worse than one that explains.
 */

import type { FeatureSpec } from './api';

export function Switch({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-5 w-9 shrink-0 rounded-full transition-colors duration-ts focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40 ${
        checked ? 'bg-accent-strong' : 'bg-surface-2'
      }`}
    >
      <span
        aria-hidden
        className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform duration-ts ${
          checked ? 'translate-x-[18px]' : 'translate-x-0.5'
        }`}
      />
    </button>
  );
}

export function FeatureToggles({
  catalog,
  values,
  onChange,
  disabled = false,
  /** Per-feature note under the hint — e.g. "Workspace default: on". */
  note,
}: {
  catalog: FeatureSpec[];
  values: Record<string, boolean>;
  onChange: (id: string, next: boolean) => void;
  disabled?: boolean;
  note?: (spec: FeatureSpec) => string | null;
}) {
  const labelOf = (id: string) =>
    catalog.find((f) => f.id === id)?.label ?? id;

  return (
    <ul className="divide-y divide-border rounded-ts border border-border bg-surface">
      {catalog.map((spec) => {
        const parentOff = Boolean(spec.requires && !values[spec.requires]);
        const extra = note?.(spec) ?? null;
        return (
          <li key={spec.id} className="flex items-start gap-3 p-3">
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-ink">{spec.label}</div>
              <p className="mt-0.5 text-xs leading-relaxed text-muted">
                {spec.hint}
              </p>
              {parentOff ? (
                <p className="mt-1 text-xs text-faint">
                  Needs {labelOf(spec.requires as string)}, which is off.
                </p>
              ) : (
                extra && <p className="mt-1 text-xs text-faint">{extra}</p>
              )}
            </div>
            <Switch
              checked={Boolean(values[spec.id]) && !parentOff}
              disabled={disabled || parentOff}
              label={spec.label}
              onChange={(next) => onChange(spec.id, next)}
            />
          </li>
        );
      })}
    </ul>
  );
}
