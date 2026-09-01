// @vitest-environment jsdom
/**
 * The sign-in artwork must differ between visitors and between sign-ins.
 *
 * Owner requirement (2026-09-01): "when 1 user see 1 and other user see
 * another — if logout and login that time it change". There is no identity at
 * the sign-in screen to key on, so the choice is random per page load; a
 * logout is a full navigation to /login, which is a fresh load.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import {
  ILLUSTRATIONS,
  IllustrationPanel,
} from '../components/auth/IllustrationPanel';

/**
 * jsdom does not fetch images, so `load` never fires on its own and the panel
 * (which deliberately waits for a picture to arrive before showing it) would
 * stay on its first frame. Firing the event is how a test says "the browser
 * finished loading these".
 */
function settleImages(container: HTMLElement) {
  act(() => {
    for (const img of container.querySelectorAll('img.auth-illustration')) {
      img.dispatchEvent(new Event('load'));
    }
  });
}

/** The slug of whichever illustration is currently faded in. */
function currentSlug(container: HTMLElement): string {
  const shown = container.querySelector('img.auth-illustration.is-current');
  const src = shown?.getAttribute('src') ?? '';
  return src.replace('/illustrator/', '').replace('.webp', '');
}

function matchMedia(reduced: boolean) {
  return vi.fn().mockImplementation((query: string) => ({
    matches: reduced && query.includes('reduce'),
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  // Without this the stubbed matchMedia leaks into the next test, where
  // restoreAllMocks has already emptied its implementation.
  vi.unstubAllGlobals();
});

describe('IllustrationPanel', () => {
  it('picks a different illustration across loads', () => {
    vi.stubGlobal('matchMedia', matchMedia(false));
    // Deterministic stand-ins for two separate page loads.
    const seen: string[] = [];
    for (const roll of [0, 0.5]) {
      vi.spyOn(Math, 'random').mockReturnValue(roll);
      const { container } = render(<IllustrationPanel />);
      settleImages(container);
      seen.push(currentSlug(container));
      cleanup();
    }
    expect(seen[0]).not.toBe(seen[1]);
  });

  it('can reach every illustration in the folder', () => {
    vi.stubGlobal('matchMedia', matchMedia(false));
    const reached = new Set<string>();
    for (let i = 0; i < ILLUSTRATIONS.length; i += 1) {
      vi.spyOn(Math, 'random').mockReturnValue(i / ILLUSTRATIONS.length);
      const { container } = render(<IllustrationPanel />);
      settleImages(container);
      reached.add(currentSlug(container));
      cleanup();
    }
    expect(reached.size).toBe(ILLUSTRATIONS.length);
  });

  it('rotates on a timer so the panel is never static for long', () => {
    vi.stubGlobal('matchMedia', matchMedia(false));
    vi.spyOn(Math, 'random').mockReturnValue(0);
    vi.useFakeTimers();
    const { container } = render(<IllustrationPanel />);
    settleImages(container);
    const first = currentSlug(container);
    act(() => {
      vi.advanceTimersByTime(8001);
    });
    settleImages(container);
    expect(currentSlug(container)).not.toBe(first);
  });

  it('does NOT auto-rotate under prefers-reduced-motion', () => {
    vi.stubGlobal('matchMedia', matchMedia(true));
    vi.spyOn(Math, 'random').mockReturnValue(0);
    vi.useFakeTimers();
    const { container } = render(<IllustrationPanel />);
    settleImages(container);
    const first = currentSlug(container);
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    // Still random, still legible — just not moving on its own.
    expect(currentSlug(container)).toBe(first);
  });

  it('lets a reader pick one with the dots, and stops the drift', () => {
    vi.stubGlobal('matchMedia', matchMedia(false));
    vi.spyOn(Math, 'random').mockReturnValue(0);
    vi.useFakeTimers();
    const { container } = render(<IllustrationPanel />);
    settleImages(container);
    const dots = screen.getAllByRole('button');
    expect(dots).toHaveLength(ILLUSTRATIONS.length);

    act(() => {
      dots[3].click();
    });
    settleImages(container);
    expect(currentSlug(container)).toBe(ILLUSTRATIONS[3].slug);

    // A chosen illustration must not be yanked away by the timer.
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(currentSlug(container)).toBe(ILLUSTRATIONS[3].slug);
  });

  it('always renders the chosen illustration, not an empty panel', () => {
    // REGRESSION (2026-09-01): every illustration was mounted at once with
    // loading="lazy" on all but the first. A lazy <img> that is stacked,
    // transparent and behind others is never fetched — so landing on a random
    // non-zero index showed a panel with a caption and no picture.
    vi.stubGlobal('matchMedia', matchMedia(false));
    for (let i = 0; i < ILLUSTRATIONS.length; i += 1) {
      vi.spyOn(Math, 'random').mockReturnValue(i / ILLUSTRATIONS.length);
      const { container } = render(<IllustrationPanel />);
      settleImages(container);
      const shown = container.querySelector('img.auth-illustration.is-current');
      expect(shown).not.toBeNull();
      expect(shown?.getAttribute('src')).toBe(
        `/illustrator/${ILLUSTRATIONS[i].slug}.webp`,
      );
      // Whatever is on screen must be fetched now, never deferred.
      expect(shown?.getAttribute('loading')).toBe('eager');
      cleanup();
    }
  });

  it('fetches only what it needs — not the whole folder', () => {
    vi.stubGlobal('matchMedia', matchMedia(false));
    vi.spyOn(Math, 'random').mockReturnValue(0);
    const { container } = render(<IllustrationPanel />);
    // The current one and the next (pre-decoded for the cross-fade); the
    // other five are ~500 KB this page never has to pay for.
    expect(container.querySelectorAll('img.auth-illustration')).toHaveLength(2);
  });

  it('never shows a caption without its picture', () => {
    // The panel waits for the chosen image to LOAD before switching to it, so
    // a slow connection sees the previous illustration rather than a card
    // that is empty under a caption describing something invisible.
    vi.stubGlobal('matchMedia', matchMedia(false));
    vi.spyOn(Math, 'random').mockReturnValue(3 / ILLUSTRATIONS.length);
    const { container } = render(<IllustrationPanel />);

    // Nothing has loaded yet: something is still on screen, and the caption
    // beside it matches THAT picture.
    const beforeLoad = currentSlug(container);
    expect(beforeLoad).not.toBe('');
    expect(
      screen.getByText(
        ILLUSTRATIONS.find((a) => a.slug === beforeLoad)!.title,
      ),
    ).toBeTruthy();

    settleImages(container);
    const afterLoad = currentSlug(container);
    expect(afterLoad).toBe(ILLUSTRATIONS[3].slug);
    expect(screen.getByText(ILLUSTRATIONS[3].title)).toBeTruthy();
  });

  it('skips an illustration whose file will not load', () => {
    // A missing or corrupt .webp must not freeze the panel on it forever.
    vi.stubGlobal('matchMedia', matchMedia(false));
    vi.spyOn(Math, 'random').mockReturnValue(3 / ILLUSTRATIONS.length);
    const { container } = render(<IllustrationPanel />);
    act(() => {
      for (const img of container.querySelectorAll('img.auth-illustration')) {
        if (img.getAttribute('src')?.includes(ILLUSTRATIONS[3].slug)) {
          img.dispatchEvent(new Event('error'));
        }
      }
    });
    settleImages(container);
    expect(currentSlug(container)).not.toBe(ILLUSTRATIONS[3].slug);
  });

  it('keeps the artwork decorative and the captions readable', () => {
    vi.stubGlobal('matchMedia', matchMedia(false));
    vi.spyOn(Math, 'random').mockReturnValue(0);
    const { container } = render(<IllustrationPanel />);
    settleImages(container);
    // Decorative: the caption beside it carries the meaning.
    for (const img of container.querySelectorAll('img')) {
      expect(img.getAttribute('alt')).toBe('');
    }
    expect(screen.getByText(ILLUSTRATIONS[0].title)).toBeTruthy();
  });
});
