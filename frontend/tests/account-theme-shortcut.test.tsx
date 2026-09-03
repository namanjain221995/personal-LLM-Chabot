// @vitest-environment jsdom
/**
 * The duplicate Light/Dark shortcut above the account row is gone
 * (owner request 2026-09-03).
 *
 * One preference had two controls: a toggle button pinned to the bottom of the
 * sidebar, immediately above the account name, and a proper Theme radiogroup
 * in Settings → Personalization. The shortcut went; the preference did not.
 *
 * That distinction is the whole point of this file, so it is asserted from
 * both ends: the sidebar no longer offers the control ANYWHERE (by role, by
 * label and by text), and Settings still switches the theme for real —
 * through the same `useTheme` context the shortcut used to call, with the same
 * `localStorage` persistence and the same `html` class that the pre-hydration
 * script reads back on the next load.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Sidebar } from '@/components/Sidebar';
import { SettingsDialog } from '@/components/SettingsDialog';
import { clearAccountCache } from '@/components/AccountMenu';
import { Providers } from '@/components/Providers';
import type { ConversationSummary } from '@/lib/types';

const noop = () => undefined;

const conv = (id: string, title: string): ConversationSummary => ({
  id,
  title,
  createdAt: 1,
  updatedAt: 1,
});

const ME = {
  username: 'naman',
  user: { id: 1, name: 'Naman Jain', email: 'naman@techsara.test' },
  workspace: { id: 'ws1', name: 'TechSara Solutions', role: 'member' },
  capabilities: [] as string[],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function renderSidebar() {
  return render(
    <Providers>
      <Sidebar
        open
        onClose={noop}
        conversations={[conv('a', 'Recent chat')]}
        archived={[]}
        activeId="a"
        onNewChat={noop}
        onOpenSearch={noop}
        onSelect={noop}
        onRename={noop}
        onDelete={noop}
        onSetPinned={noop}
        onSetArchived={noop}
        onExport={noop}
        onLoadArchived={noop}
      />
    </Providers>,
  );
}

beforeEach(() => {
  clearAccountCache();
  window.localStorage.clear();
  document.documentElement.className = 'dark';
  HTMLMediaElement.prototype.play =
    HTMLMediaElement.prototype.play ?? (async () => undefined);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/auth/me') return json(ME);
      return json({}, 500);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/* ------------------------------------------------- the shortcut is gone */

describe('ACCOUNT-THEME · the sidebar shortcut', () => {
  it('ACCOUNT-THEME-01 · renders no Light/Dark toggle', async () => {
    renderSidebar();
    await screen.findAllByText('Naman Jain');
    // The button carried this exact accessible name in both directions.
    expect(
      screen.queryByRole('button', { name: /switch to (light|dark) theme/i }),
    ).toBeNull();
    // …and this exact visible label.
    expect(screen.queryByText(/^(Light|Dark) theme$/)).toBeNull();
  });

  it('ACCOUNT-THEME-02 · no theme shortcut sits in the account footer region', async () => {
    const { container } = renderSidebar();
    const name = (await screen.findAllByText('Naman Jain'))[0];
    // The footer is the bordered block the account row lives in. Whatever it
    // holds, none of it may be a theme control.
    const footer = name.closest('.border-t') as HTMLElement;
    expect(footer).toBeTruthy();
    for (const button of within(footer).queryAllByRole('button')) {
      const label = `${button.getAttribute('aria-label') ?? ''} ${button.textContent ?? ''}`;
      expect(label).not.toMatch(/light|dark|theme/i);
    }
    // Belt and braces: the sun/moon glyphs the toggle used are not in the tree.
    expect(container.querySelectorAll('svg circle[cx="12"][cy="12"][r="4"]').length).toBe(0);
  });

  it('ACCOUNT-THEME-03/04/05 · identity, Settings and Log out all survive', async () => {
    renderSidebar();
    const trigger = (await screen.findAllByText('Naman Jain'))[0]
      .closest('button') as HTMLButtonElement;
    expect(trigger).toBeTruthy();
    // Identity is still rendered on the row itself.
    expect(screen.getAllByText('naman@techsara.test').length).toBeGreaterThan(0);

    fireEvent.click(trigger);
    const menu = await screen.findByRole('menu');
    expect(within(menu).getByRole('menuitem', { name: /^Settings$/ })).toBeTruthy();
    expect(within(menu).getByRole('menuitem', { name: /log out/i })).toBeTruthy();
    // And the menu did not inherit the shortcut either.
    expect(within(menu).queryByText(/light|dark/i)).toBeNull();
  });

  it('the account footer keeps its border and padding — no gap where the button was', async () => {
    renderSidebar();
    const name = (await screen.findAllByText('Naman Jain'))[0];
    const footer = name.closest('.border-t') as HTMLElement;
    expect(footer.className).toContain('border-t');
    expect(footer.className).toContain('p-2');
    // Exactly one child: the account row. No leftover spacer or divider.
    expect(footer.children).toHaveLength(1);
  });
});

/* --------------------------------------------- the preference still works */

describe('ACCOUNT-THEME · Settings still owns the theme', () => {
  /**
   * Open Settings and navigate to Personalization the way a user does — by
   * clicking the section in the dialog's own nav. Passing `initialSection`
   * would jump straight there and prove nothing about the route still being
   * reachable now that the sidebar shortcut is gone.
   */
  function openSettings() {
    const view = render(
      <Providers>
        <SettingsDialog open account={ME} onClose={noop} />
      </Providers>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Personalization' }));
    return view;
  }

  it('ACCOUNT-THEME-06 · the Appearance/Theme control is still there', () => {
    openSettings();
    const group = screen.getByRole('radiogroup', { name: 'Theme' });
    expect(within(group).getByRole('radio', { name: 'Dark' })).toBeTruthy();
    expect(within(group).getByRole('radio', { name: 'Light' })).toBeTruthy();
  });

  it('ACCOUNT-THEME-07 · choosing Light from Settings changes the theme', async () => {
    openSettings();
    const group = screen.getByRole('radiogroup', { name: 'Theme' });
    const dark = within(group).getByRole('radio', { name: 'Dark' });
    const light = within(group).getByRole('radio', { name: 'Light' });
    expect(dark.getAttribute('aria-checked')).toBe('true');

    fireEvent.click(light);

    await waitFor(() =>
      expect(
        within(screen.getByRole('radiogroup', { name: 'Theme' }))
          .getByRole('radio', { name: 'Light' })
          .getAttribute('aria-checked'),
      ).toBe('true'),
    );
    // The real effect, not just the chip: the html class the app renders from.
    expect(document.documentElement.classList.contains('light')).toBe(true);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  it('ACCOUNT-THEME-08 · and persists it exactly as before', async () => {
    openSettings();
    fireEvent.click(
      within(screen.getByRole('radiogroup', { name: 'Theme' }))
        .getByRole('radio', { name: 'Light' }),
    );
    await waitFor(() =>
      expect(window.localStorage.getItem('techsara.theme')).toBe('light'),
    );

    // …and back again, so the switch is not one-way.
    fireEvent.click(
      within(screen.getByRole('radiogroup', { name: 'Theme' }))
        .getByRole('radio', { name: 'Dark' }),
    );
    await waitFor(() =>
      expect(window.localStorage.getItem('techsara.theme')).toBe('dark'),
    );
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });
});
