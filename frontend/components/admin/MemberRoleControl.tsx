'use client';

/**
 * The Role cell of the roster: a label for everyone, a control for the
 * people who can change it.
 *
 * The role was previously reachable only through the ⋯ menu, which is where
 * destructive actions live — so the most ordinary administrative act in the
 * product (promote someone) sat next to "Remove". Here it is what it looks
 * like: the value, with a chevron when you are allowed to change it, opening
 * the SAME dialog as before. That dialog is where the orchestrator's 409s
 * ("the workspace must keep one active super admin") are read and shown, so
 * none of that logic moves.
 */

import { RoleChip } from './chips';
import { IconChevronDown } from '@/components/icons';

export function MemberRoleControl({
  role,
  name,
  editable,
  onEdit,
}: {
  role: string;
  /** Whose role this is — the button needs a name of its own. */
  name: string;
  editable: boolean;
  onEdit: () => void;
}) {
  if (!editable) return <RoleChip role={role} />;
  return (
    <button
      type="button"
      onClick={(e) => {
        // The row itself navigates to the member; changing a role must not.
        e.stopPropagation();
        onEdit();
      }}
      aria-label={`Change role for ${name}`}
      className="-ml-2 inline-flex items-center gap-1 rounded-lg px-2 py-1 transition-colors duration-ts hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
    >
      <RoleChip role={role} />
      <IconChevronDown size={13} className="text-faint" />
    </button>
  );
}
