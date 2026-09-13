// @vitest-environment jsdom
/**
 * fe audit 2026-09-13 (responsive + no-errors pass), the parts of the chat,
 * auth and shared components that jsdom can prove. Pure layout (widths,
 * overflow) is proven by the browser re-audit; these pin the structure that
 * layout depends on, so a refactor cannot quietly undo it.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { renderToString } from 'react-dom/server';

import { ConfirmDialog } from '@/components/ConfirmDialog';
import { ConversationMenu } from '@/components/ConversationMenu';
import { ModelPicker, menuShiftIntoViewport } from '@/components/ModelPicker';
import { Sidebar } from '@/components/Sidebar';
import { AuthLayout } from '@/components/auth/AuthLayout';
import { AcceptInviteForm } from '@/components/auth/AcceptInviteForm';
import { SearchPalette } from '@/components/SearchPalette';
import { SettingsDialog } from '@/components/SettingsDialog';
import { Providers } from '@/components/Providers';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const noop = () => undefined;

describe('the phone sidebar drawer is not in the server HTML', () => {
  const props = {
    onClose: noop,
    conversations: [],
    archived: [],
    activeId: null,
    onNewChat: noop,
    onOpenSearch: noop,
    onSelect: noop,
    onRename: noop,
    onDelete: noop,
    onSetPinned: noop,
    onSetArchived: noop,
    onExport: noop,
    onLoadArchived: noop,
  };

  it('server-renders only the desktop column even while the sidebar starts open', () => {
    // ChatApp starts open and closes on phones after hydration; a drawer in
    // the SSR markup was a phone's first paint.
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })));
    const html = renderToString(<Sidebar open {...props} />);
    expect(html).toContain('aria-label="Sidebar"');
    expect(html).not.toContain('role="dialog"');
  });

  it('still mounts the drawer on the client once hydrated', () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })));
    render(<Sidebar open {...props} />);
    expect(screen.getByRole('dialog', { name: 'Sidebar' })).toBeTruthy();
  });

  it('hides the desktop keyboard shortcut below md', () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })));
    const { container } = render(<Sidebar open {...props} />);
    const kbds = Array.from(container.querySelectorAll('kbd'));
    expect(kbds.length).toBeGreaterThan(0);
    for (const kbd of kbds) {
      expect(kbd.className).toMatch(/(^|\s)hidden(\s|$)/);
      expect(kbd.className).toMatch(/(^|\s)md:inline(\s|$)/);
    }
  });
});

describe('conversation row options on touch screens', () => {
  it('is always visible on a device without hover, not only on hover or focus', () => {
    render(
      <ConversationMenu
        title="Quarterly plan"
        pinned={false}
        archived={false}
        onRename={noop}
        onTogglePin={noop}
        onToggleArchive={noop}
        onExport={noop}
        onDelete={noop}
      />,
    );
    const trigger = screen.getByRole('button', { name: 'Options for conversation: Quarterly plan' });
    expect(trigger.className).toContain('opacity-0');
    expect(trigger.className).toContain('[@media(hover:none)]:opacity-100');
    // A fingertip-sized target on a coarse pointer.
    expect(trigger.className).toContain('[@media(pointer:coarse)]:h-8');
    expect(trigger.className).toContain('[@media(pointer:coarse)]:w-8');
  });
});

describe('the effort menu stays inside the viewport', () => {
  it('moves right by exactly the overshoot past the gutter', () => {
    expect(menuShiftIntoViewport({ left: -41, width: 288 })).toBe(53);
    expect(menuShiftIntoViewport({ left: -11, width: 288 })).toBe(23);
  });

  it('leaves a menu that already fits, or has no box, alone', () => {
    expect(menuShiftIntoViewport({ left: 400, width: 288 })).toBe(0);
    expect(menuShiftIntoViewport({ left: 12, width: 288 })).toBe(0);
    expect(menuShiftIntoViewport({ left: 0, width: 0 })).toBe(0);
  });

  it('applies the shift to the open menu and caps its width to the viewport', () => {
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(function (this: HTMLElement) {
        const isMenu = this.getAttribute('role') === 'menu';
        return {
          left: isMenu ? -41 : 0,
          right: isMenu ? 247 : 0,
          width: isMenu ? 288 : 0,
          top: 0,
          bottom: 0,
          height: 0,
          x: 0,
          y: 0,
          toJSON: () => ({}),
        } as DOMRect;
      });
    render(<ModelPicker model="smart" effort="fast" onChange={noop} />);
    fireEvent.click(screen.getByRole('button', { name: 'Effort: Fast' }));
    const menu = screen.getByRole('menu', { name: 'Effort' });
    expect(menu.style.transform).toBe('translateX(53px)');
    expect(menu.className).toContain('max-w-[calc(100vw-24px)]');
    rect.mockRestore();
  });
});

describe('long words wrap instead of overflowing', () => {
  it('lets a confirm dialog body that quotes a URL wrap anywhere', () => {
    const url = 'https://hooks.example.com/services/techsara/billing-assistant/production/eu-west-1/receiver?token=xxxxxxxxxxxxxxxx';
    render(
      <ConfirmDialog
        open
        title="Remove this endpoint?"
        body={`${url} will stop receiving events.`}
        confirmLabel="Remove"
        onConfirm={noop}
        onCancel={noop}
      />,
    );
    const body = screen.getByText(/will stop receiving events/);
    expect(body.className).toContain('[overflow-wrap:anywhere]');
    // Its column may shrink below the URL's width.
    expect(body.parentElement!.className).toMatch(/(^|\s)min-w-0(\s|$)/);
  });

  it('lets the auth illustration column shrink beside the form', () => {
    const { container } = render(
      <AuthLayout>
        <p>form</p>
      </AuthLayout>,
    );
    const aside = container.querySelector('aside')!;
    expect(aside.className).toMatch(/(^|\s)min-w-0(\s|$)/);
    expect(aside.className).toMatch(/(^|\s)lg:flex-1(\s|$)/);
    for (const img of Array.from(container.querySelectorAll('img.auth-illustration'))) {
      expect(img.className).toContain('max-w-[min(560px,100%)]');
    }
  });
});

describe('a refused invitation lookup releases its body', () => {
  it('cancels the unread 404 body instead of leaving the request pending', async () => {
    window.history.replaceState({}, '', '/accept-invite?token=bogus-token-123');
    const cancel = vi.fn(async () => undefined);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 404, body: { cancel }, json: async () => ({}) })),
    );
    render(<AcceptInviteForm navigate={vi.fn()} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('This invitation is no longer valid');
    expect(cancel).toHaveBeenCalledTimes(1);
    window.history.replaceState({}, '', '/');
  });
});

describe('dialog close buttons are a full 32px target on phones', () => {
  // The re-audit measured 'Close settings' and 'Close search' at 31x31: a 15px
  // icon plus p-2 is one pixel short. A fixed box on phones cannot be.
  const phoneBox = (el: HTMLElement) => {
    expect(el.className).toMatch(/(^|\s)max-sm:h-8(\s|$)/);
    expect(el.className).toMatch(/(^|\s)max-sm:w-8(\s|$)/);
    expect(el.className).toMatch(/(^|\s)items-center(\s|$)/);
    expect(el.className).toMatch(/(^|\s)justify-center(\s|$)/);
  };

  it('sizes Close settings to 32px below sm', () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })));
    render(
      <Providers>
        <SettingsDialog open account={null} onClose={noop} />
      </Providers>,
    );
    phoneBox(screen.getByRole('button', { name: 'Close settings' }));
  });

  it('sizes Close search to 32px below sm', () => {
    render(
      <SearchPalette
        open
        onClose={noop}
        recents={[]}
        onSelect={noop}
        onNewChat={noop}
        searchFn={async () => []}
      />,
    );
    phoneBox(screen.getByRole('button', { name: 'Close search' }));
  });
});
