/**
 * Split-screen shell for the auth pages (/login, /accept-invite).
 *
 * Left: the form column on white. Right: an inset rounded card holding a
 * workspace illustration — the reference design's shape, where the artwork
 * sits on a tinted card floating on the page rather than bleeding to the edge.
 *
 * WHITE ON PURPOSE, IN BOTH THEMES. `auth-light` (app/globals.css) applies the
 * same token overrides as light mode to this subtree, so every class below is
 * still the ordinary design-system vocabulary — bg-bg, text-ink, text-muted,
 * accent — it just always resolves to the paper palette. The illustrations are
 * drawn on white; a dark sign-in page would have framed them in a black box.
 *
 * Below lg: the card is dropped entirely rather than stacked, so a phone gets
 * a clean single-column sign-in instead of scrolling past decoration to reach
 * the form.
 */

import type { ReactNode } from 'react';
import { IllustrationPanel } from './IllustrationPanel';

export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="auth-light flex min-h-dvh bg-bg text-ink">
      <main className="flex w-full flex-col justify-center px-6 py-12 sm:px-14 lg:w-[46%] lg:min-w-[440px] lg:px-16 xl:w-[42%]">
        <div className="auth-card-in mx-auto w-full max-w-[380px]">
          {children}
        </div>
      </main>

      <aside className="hidden p-4 lg:block lg:flex-1">
        <IllustrationPanel />
      </aside>
    </div>
  );
}
