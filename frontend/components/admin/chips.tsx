/**
 * Role and status chips for the admin area — the EngineBadge recipe (rounded
 * pill, color-mix 45% border / 12% background, 1.5px dot) on NEW base colors
 * so the engine palette keeps its established meaning: super_admin amber
 * (--ts-warn), admin the teal accent, member neutral slate. Statuses reuse
 * the same three families plus danger.
 */

import { ROLE_LABEL, type InviteStatus, type Role } from './api';

function Chip({
  label,
  color,
  ink,
}: {
  label: string;
  color: string;
  /** Text color — a theme-aware var readable on both surfaces. */
  ink?: string;
}) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium"
      style={{
        color: ink ?? color,
        borderColor: `color-mix(in srgb, ${color} 45%, transparent)`,
        background: `color-mix(in srgb, ${color} 12%, transparent)`,
      }}
    >
      <span
        aria-hidden
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: ink ?? color }}
      />
      {label}
    </span>
  );
}

const ROLE_STYLE: Record<Role, { color: string; ink?: string }> = {
  super_admin: { color: 'var(--ts-warn)' },
  admin: { color: 'var(--ts-accent)' },
  member: { color: 'var(--ts-slate)', ink: 'var(--ts-text-muted)' },
};

export function RoleChip({ role }: { role: Role | string }) {
  const style = ROLE_STYLE[role as Role];
  // An unknown future role degrades to the neutral member style, raw text kept.
  if (!style) {
    return <Chip label={role} color="var(--ts-slate)" ink="var(--ts-text-muted)" />;
  }
  return <Chip label={ROLE_LABEL[role as Role]} {...style} />;
}

type Status = 'active' | 'disabled' | InviteStatus;

const STATUS_STYLE: Record<Status, { label: string; color: string; ink?: string }> = {
  active: { label: 'Active', color: 'var(--ts-accent)' },
  disabled: { label: 'Disabled', color: 'var(--ts-danger)' },
  pending: { label: 'Pending', color: 'var(--ts-warn)' },
  accepted: { label: 'Accepted', color: 'var(--ts-accent)' },
  revoked: { label: 'Revoked', color: 'var(--ts-danger)' },
  expired: {
    label: 'Expired',
    color: 'var(--ts-slate)',
    ink: 'var(--ts-text-muted)',
  },
};

export function StatusChip({ status }: { status: Status | string }) {
  const style = STATUS_STYLE[status as Status];
  if (!style) {
    return <Chip label={status} color="var(--ts-slate)" ink="var(--ts-text-muted)" />;
  }
  return <Chip label={style.label} color={style.color} ink={style.ink} />;
}
