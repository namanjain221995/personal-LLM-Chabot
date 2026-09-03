// @vitest-environment jsdom
/**
 * The orchestrator's 409 refusals must be READ, not swallowed: demoting the
 * last active super admin comes back as a {detail} sentence, and the change
 * role dialog renders it inline while staying open for a different choice.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChangeRoleDialog } from '@/components/admin/ChangeRoleDialog';
import { AdminMeProvider } from '@/components/admin/AdminMeContext';
import type { Me } from '@/components/admin/api';

const SUPER: Me = {
  user: { id: 1, name: 'Grace Hopper', email: 'grace@corp.com' },
  workspace: { id: 'w1', name: 'Corp Workspace', role: 'super_admin' },
  capabilities: ['members.read', 'roles.manage'],
  features: {},
};

const LAST_SUPER_ADMIN =
  'The workspace must keep at least one active super admin.';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('change role — 409 surfaces', () => {
  it('shows the {detail} sentence and keeps the dialog open', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return {
          ok: false,
          status: 409,
          json: async () => ({ detail: LAST_SUPER_ADMIN }),
        };
      }),
    );
    const onChanged = vi.fn();
    render(
      <AdminMeProvider me={SUPER}>
        <ChangeRoleDialog
          member={{ id: 1, name: 'Grace Hopper', role: 'super_admin' }}
          open
          onClose={() => undefined}
          onChanged={onChanged}
        />
      </AdminMeProvider>,
    );

    fireEvent.click(screen.getByLabelText('Member'));
    fireEvent.click(screen.getByText('Save role'));

    await waitFor(() => expect(screen.getByText(LAST_SUPER_ADMIN)).toBeTruthy());
    expect(calls).toEqual(['/api/admin/members/1/role']);
    expect(onChanged).not.toHaveBeenCalled();
    // Dialog still open — the refusal is recoverable, not terminal.
    expect(screen.getByText('Save role')).toBeTruthy();
  });
});
