// @vitest-environment jsdom
/**
 * The shared loading indicator.
 *
 * ONE piece of artwork for every "something is happening" state. Before this
 * there were four vocabularies for one idea — shimmer bars, a bordered spinner,
 * a shimmering label, and another spinner in the agent timeline — and a user
 * had to learn each separately.
 *
 * The artwork is an owner-supplied WebM with a real alpha channel; the SVG
 * starburst is the fallback for browsers without VP8-alpha support (Safari).
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Loader, LOADER_POSTER, LOADER_SRC } from '@/components/Loader';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** jsdom has no matchMedia; the component must survive that, and honour it. */
function stubReducedMotion(reduced: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: reduced && query.includes('prefers-reduced-motion'),
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

function loader(): HTMLVideoElement {
  return screen.getByTestId('app-loader') as HTMLVideoElement;
}

describe('the video artwork', () => {
  it('is the primary artwork, served from public/', () => {
    render(<Loader />);
    const source = loader().querySelector('source');
    expect(source?.getAttribute('src')).toBe(LOADER_SRC);
    expect(source?.getAttribute('type')).toBe('video/webm');
  });

  it('carries the attributes that make autoplay legal without a gesture', () => {
    // Missing `muted` or `playsinline` and the browser silently blocks it —
    // the indicator would simply never move.
    render(<Loader />);
    const el = loader();
    expect(el.hasAttribute('autoplay')).toBe(true);
    expect(el.hasAttribute('loop')).toBe(true);
    expect(el.hasAttribute('playsinline')).toBe(true);
    expect(el.muted).toBe(true); // React sets this as a property
  });

  it('has a poster so the first paint is not an empty box', () => {
    render(<Loader />);
    expect(loader().getAttribute('poster')).toBe(LOADER_POSTER);
  });

  it('takes a size, so one artwork serves a step row and a centred state', () => {
    render(<Loader size={14} />);
    expect(loader().getAttribute('width')).toBe('14');
    cleanup();
    render(<Loader size={40} />);
    expect(loader().getAttribute('width')).toBe('40');
  });

  it('changes tempo without swapping artwork', () => {
    // Rate is a PROPERTY, not an attribute — React will not apply it from JSX,
    // so it has to be set imperatively or it silently does nothing.
    render(<Loader rate={1.25} />);
    expect(loader().playbackRate).toBe(1.25);
    cleanup();
    render(<Loader rate={0.8} />);
    expect(loader().playbackRate).toBe(0.8);
  });
});

describe('accessibility', () => {
  it('is hidden from screen readers by default', () => {
    // The common case has an adjacent label or live region; announcing the
    // artwork too would read every state twice.
    render(<Loader />);
    expect(loader().getAttribute('aria-hidden')).toBe('true');
  });

  it('takes an accessible name when it stands alone', () => {
    render(<Loader label="Waiting for the first token" />);
    const el = screen.getByRole('img', { name: 'Waiting for the first token' });
    expect(el).toBeTruthy();
    expect(el.hasAttribute('aria-hidden')).toBe(false);
  });
});

describe('reduced motion', () => {
  it('holds the artwork on a single frame instead of looping', () => {
    stubReducedMotion(true);
    render(<Loader rate={1.25} />);
    const el = loader();
    expect(el.hasAttribute('autoplay')).toBe(false);
    expect(el.playbackRate).toBe(0);
    // Still present, still showing the artwork — an indicator that vanishes
    // entirely reads as "nothing is happening".
    expect(el.getAttribute('poster')).toBe(LOADER_POSTER);
  });

  it('plays normally when motion is not restricted', () => {
    stubReducedMotion(false);
    render(<Loader rate={1.25} />);
    expect(loader().hasAttribute('autoplay')).toBe(true);
    expect(loader().playbackRate).toBe(1.25);
  });
});

describe('the SVG fallback', () => {
  // WebM alpha is a VP8 extension Safari does not implement: the video there
  // either fails or paints an opaque rectangle over the content behind it.
  // Detected by listening for the real failure rather than sniffing browsers.
  function failVideo(props: Record<string, unknown> = {}) {
    render(<Loader {...props} />);
    fireEvent.error(loader());
  }

  it('replaces the video when it cannot play', () => {
    failVideo();
    expect(screen.queryByTestId('app-loader')).toBeNull();
    expect(document.querySelector('.reasoning-star')).toBeTruthy();
  });

  it('is twelve rays of plain SVG — no image, no animation library', () => {
    failVideo();
    expect(document.querySelectorAll('.reasoning-star__ray')).toHaveLength(12);
    expect(document.querySelector('img')).toBeNull();
  });

  it('carries each angle as an SVG attribute CSS cannot override', () => {
    // The angle was once an inline CSS transform, and a running animation's
    // transform overrides an inline one — so all twelve stacked at 0° and the
    // star rendered as a single spinning sliver.
    failVideo();
    const angles = [...document.querySelectorAll('.reasoning-star__ray')].map(
      (ray) =>
        (ray.parentElement as unknown as SVGGElement).getAttribute('transform'),
    );
    expect(angles.every((a) => a?.startsWith('rotate('))).toBe(true);
    expect(new Set(angles).size).toBe(12);
  });

  it('never puts a transform on the ray itself', () => {
    failVideo();
    for (const ray of document.querySelectorAll('.reasoning-star__ray')) {
      expect((ray as SVGElement).style.transform).toBe('');
    }
  });

  it('takes its colour from currentColor so both themes work unchanged', () => {
    failVideo();
    const svg = document.querySelector('.reasoning-star') as SVGElement;
    expect(svg.getAttribute('class')).toContain('text-accent');
  });

  it('keeps the accessible name it was given', () => {
    failVideo({ label: 'Researching' });
    expect(screen.getByRole('img', { name: 'Researching' })).toBeTruthy();
  });
});
