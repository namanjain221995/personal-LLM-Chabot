/**
 * Role and status marks for the admin area.
 *
 * Restrained on purpose (2026-09-04). These used to be full pills — tinted
 * background, 45% border, a dot — repeated down every row of the roster,
 * which made the loudest thing on the page the fact that everyone is a
 * "Member". In a settings table the person's NAME is the subject; role and
 * status are metadata and should read as metadata.
 *
 * So: role is quiet text, tinted only where the distinction earns attention
 * (super admin, admin); status is a 6px dot beside a word. Colour still
 * carries meaning, but it is never the only carrier — every mark states its
 * value in words, so nothing here depends on telling amber from blue.
 */

import { ROLE_LABEL, type InviteStatus, type Role } from './api';

const ROLE_TONE: Record<Role, string> = {
  // The one role that can change everyone else's access: worth a tint.
  super_admin: 'border-warn/35 bg-warn/10 text-warn',
  admin: 'border-accent/35 bg-accent/10 text-accent',
  // The default. A border-less, tint-less label — 90% of rows are this one.
  member: 'border-transparent text-muted',
};

export function RoleChip({ role }: { role: Role | string }) {
  const tone = ROLE_TONE[role as Role] ?? ROLE_TONE.member;
  const label = ROLE_LABEL[role as Role] ?? role;
  return (
    <span
      className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium ${tone}`}
    >
      {label}
    </span>
  );
}

type Status = 'active' | 'disabled' | InviteStatus;

const STATUS_STYLE: Record<Status, { label: string; dot: string; text?: string }> = {
  active: { label: 'Active', dot: 'bg-ok' },
  disabled: { label: 'Disabled', dot: 'bg-danger', text: 'text-danger' },
  pending: { label: 'Pending', dot: 'bg-warn', text: 'text-warn' },
  accepted: { label: 'Accepted', dot: 'bg-ok' },
  revoked: { label: 'Revoked', dot: 'bg-danger', text: 'text-danger' },
  expired: { label: 'Expired', dot: 'bg-faint' },
};

export function StatusChip({ status }: { status: Status | string }) {
  const style = STATUS_STYLE[status as Status];
  if (!style) {
    return (
      <span className="inline-flex items-center gap-2 text-xs text-muted">
        <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-faint" />
        {status}
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center gap-2 text-xs font-medium ${style.text ?? 'text-muted'}`}
    >
      <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      {style.label}
    </span>
  );
}
