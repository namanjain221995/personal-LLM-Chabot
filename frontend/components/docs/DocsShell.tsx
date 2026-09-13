'use client';

/**
 * The /docs frame: header, one sidebar, and the column the page renders into.
 *
 * ONE NAV ELEMENT, NOT TWO. The obvious build is a desktop column plus a
 * mobile drawer, and it is how the chat sidebar was first written — which
 * put every id in the document twice and pointed every `aria-labelledby` at
 * whichever copy happened to come first (M-12). Here the same node is the
 * column at `lg` and up and the drawer below it; CSS decides which, so there
 * is exactly one of each id no matter the viewport.
 *
 * The documentation is for signed-in people. `/docs` is not in the public
 * page list in lib/auth.ts, so a signed-out visitor is bounced to /login by
 * the edge gate like every other page, and this component never has to think
 * about it.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { TechSaraMark } from '@/components/TechSaraMark';
import { DOC_SECTIONS, docHref } from '@/content/docs';
import { DocsNav } from './DocsNav';

export function DocsShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? '/docs';
  const [open, setOpen] = useState(false);
  const toggleRef = useRef<HTMLButtonElement | null>(null);

  const close = useCallback(() => setOpen(false), []);

  // Escape closes the drawer and hands focus back to the control that opened
  // it. A drawer you can open with the keyboard and not close with it is a
  // trap, and returning focus is what stops a keyboard user from starting
  // again at the top of the document.
  useEffect(() => {
    if (!open) return undefined;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setOpen(false);
        toggleRef.current?.focus();
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open]);

  return (
    <div className="min-h-dvh bg-bg text-ink">
      {/* The first thing in the tab order, and invisible until it has focus:
          without it every keyboard reader walks the whole table of contents
          before reaching the page they asked for. Ink on the page ground,
          inverted: the two theme tokens with the most contrast between them
          in every theme. It was white on `accent`, which in the dark theme is
          a light blue that white text fails AA on (2026-09-13). */}
      <a
        href="#docs-content"
        className="sr-only left-4 top-4 z-50 rounded-md bg-ink px-4 py-2 text-sm font-medium text-bg focus:not-sr-only focus:absolute"
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-30 border-b border-border bg-[color-mix(in_srgb,var(--ts-bg)_95%,transparent)] backdrop-blur">
        <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-3 px-4">
          <button
            ref={toggleRef}
            type="button"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
            aria-controls="docs-nav-panel"
            className="rounded-md border border-border px-2.5 py-1.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink lg:hidden"
          >
            {open ? 'Close' : 'Menu'}
          </button>

          <Link href="/docs" className="flex items-center gap-2 no-underline">
            <TechSaraMark size={22} />
            <span className="text-sm font-semibold text-ink">
              TechSara <span className="text-muted">API docs</span>
            </span>
          </Link>

          <div className="ml-auto flex items-center gap-1 text-sm">
            <Link
              href={docHref('status')}
              className="rounded-md px-2.5 py-1.5 text-muted no-underline transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
            >
              API status
            </Link>
            {/* Hidden on a phone, where the brand, the menu button and the
                status link already fill 400px. They are in the drawer
                instead, so nothing is unreachable — only moved. */}
            <Link
              href="/api"
              className="hidden rounded-md px-2.5 py-1.5 text-muted no-underline transition-colors duration-ts hover:bg-surface-2 hover:text-ink sm:block"
            >
              Console
            </Link>
            <Link
              href="/"
              className="hidden rounded-md px-2.5 py-1.5 text-muted no-underline transition-colors duration-ts hover:bg-surface-2 hover:text-ink sm:block"
            >
              Back to chat
            </Link>
          </div>
        </div>
      </header>

      {/* The scrim only exists while the drawer is open, and only below lg.
          It is a mouse convenience; Escape is the keyboard route out.
          `bg-black/50` is the ONE palette literal in these files, on purpose:
          a scrim dims whatever is behind it in both themes, which is what
          every dialog in the product does (ConfirmDialog, AdminDialog,
          Sidebar), and a theme token would turn it white on paper. The
          design-system test allowlists exactly this class. */}
      {open && (
        <div
          onClick={close}
          aria-hidden="true"
          className="fixed inset-0 z-30 bg-black/50 lg:hidden"
        />
      )}

      <div className="mx-auto flex max-w-[1400px] gap-8 px-4 py-8">
        <div
          id="docs-nav-panel"
          className={`${
            open
              ? 'fixed inset-y-0 left-0 z-40 w-72 overflow-y-auto border-r border-border bg-sidebar p-4 pt-20'
              : 'hidden'
          } lg:sticky lg:top-20 lg:z-auto lg:block lg:h-[calc(100dvh-6rem)] lg:w-60 lg:shrink-0 lg:overflow-y-auto lg:border-0 lg:bg-transparent lg:p-0 lg:pt-0`}
        >
          <DocsNav
            sections={DOC_SECTIONS}
            currentHref={pathname}
            onNavigate={close}
          />

          {/* The two header links that step aside on a narrow screen. Shown
              only where the header hides them, so nothing appears twice to
              anyone actually looking at the page. */}
          <div className="mt-6 border-t border-border pt-4 text-sm sm:hidden">
            <Link
              href="/api"
              onClick={close}
              className="block rounded-md px-3 py-1.5 text-muted no-underline hover:bg-surface-2 hover:text-ink"
            >
              Console
            </Link>
            <Link
              href="/"
              onClick={close}
              className="block rounded-md px-3 py-1.5 text-muted no-underline hover:bg-surface-2 hover:text-ink"
            >
              Back to chat
            </Link>
          </div>
        </div>

        <main id="docs-content" className="min-w-0 flex-1">
          {children}
        </main>
      </div>
    </div>
  );
}
