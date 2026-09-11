// @vitest-environment jsdom
/**
 * PagesViewer: pages are fetched only when they come into view, every
 * object URL is released on unmount, and switching artifacts aborts the
 * fetches of the old one — the three things a page-image viewer gets
 * wrong when it grows in a hurry.
 *
 * jsdom has no IntersectionObserver and no object URLs, so both are
 * stubbed: the observer stub records what is observed and lets a test
 * declare "page 3 is now visible"; the URL stub counts what was minted and
 * what was revoked.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { mostVisible, PagesViewer, ZOOM_STOPS } from '@/components/artifacts/PagesViewer';

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const ID2 = 'b3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';

/** Every observer instance created, so a test can fire intersections. */
let observers: FakeObserver[] = [];

class FakeObserver {
  static instances: FakeObserver[] = [];
  readonly targets = new Set<Element>();
  readonly options: IntersectionObserverInit | undefined;
  constructor(
    public readonly callback: IntersectionObserverCallback,
    options?: IntersectionObserverInit,
  ) {
    this.options = options;
    observers.push(this);
  }
  observe(el: Element) {
    this.targets.add(el);
  }
  unobserve(el: Element) {
    this.targets.delete(el);
  }
  disconnect() {
    this.targets.clear();
  }
  takeRecords() {
    return [];
  }
  /** Declare some of the observed targets visible. */
  show(predicate: (el: Element) => boolean, ratio = 1) {
    const entries = Array.from(this.targets)
      .filter(predicate)
      .map((target) => ({ target, isIntersecting: ratio > 0, intersectionRatio: ratio }));
    this.callback(entries as unknown as IntersectionObserverEntry[], this as unknown as IntersectionObserver);
  }
}

let minted: string[] = [];
let revoked: string[] = [];
let fetchMock: ReturnType<typeof vi.fn>;
/** Controllers seen by fetch, so a test can check what was aborted. */
let signals: AbortSignal[] = [];

beforeEach(() => {
  observers = [];
  minted = [];
  revoked = [];
  signals = [];
  vi.stubGlobal('IntersectionObserver', FakeObserver);
  let n = 0;
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: vi.fn(() => {
      n += 1;
      const url = `blob:test/${n}`;
      minted.push(url);
      return url;
    }),
    revokeObjectURL: vi.fn((url: string) => {
      revoked.push(url);
    }),
  });
  fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.signal) signals.push(init.signal);
    return { ok: true, status: 200, blob: async () => new Blob(['png']) } as unknown as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

const pageObserver = () => observers.find((o) => o.options?.rootMargin === '600px 0px')!;
const thumbObserver = () => observers.find((o) => o.options?.rootMargin === '300px')!;
const fetchedUrls = () => fetchMock.mock.calls.map((c) => c[0] as string);

describe('PagesViewer — lazy loading', () => {
  it('fetches page 1 up front and the rest only as they come into view, at 1400 px', async () => {
    render(<PagesViewer artifactId={ID} version={1} pages={5} title="Brief" />);
    await flush();
    expect(fetchedUrls()).toEqual([`/api/artifacts/${ID}/v/1/preview/1.png?w=1400`]);
    expect(screen.getByAltText('Page 1 of Brief').getAttribute('src')).toBe('blob:test/1');

    // Page 3 scrolls into view; only page 3 is asked for.
    await act(async () => {
      pageObserver().show((el) => (el as HTMLElement).dataset.page === '3');
    });
    await flush();
    expect(fetchedUrls()).toEqual([
      `/api/artifacts/${ID}/v/1/preview/1.png?w=1400`,
      `/api/artifacts/${ID}/v/1/preview/3.png?w=1400`,
    ]);
    // Seeing it again does not fetch it again.
    await act(async () => {
      pageObserver().show((el) => (el as HTMLElement).dataset.page === '3');
    });
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('fetches thumbnails at 240 px as the strip scrolls', async () => {
    render(<PagesViewer artifactId={ID} version={1} pages={5} title="Brief" />);
    await flush();
    await act(async () => {
      thumbObserver().show((el) => (el as HTMLElement).dataset.thumb === '2');
    });
    await flush();
    expect(fetchedUrls()).toContain(`/api/artifacts/${ID}/v/1/preview/2.png?w=240`);
    expect(fetchedUrls()).not.toContain(`/api/artifacts/${ID}/v/1/preview/2.png?w=1400`);
  });

  it('shows a retry for a page that failed, and the retry fetches again', async () => {
    fetchMock.mockImplementationOnce(async () => ({ ok: false, status: 500 }) as unknown as Response);
    render(<PagesViewer artifactId={ID} version={1} pages={2} title="Brief" />);
    await flush();
    const retry = screen.getByRole('button', { name: /Page 1 could not be loaded/ });
    fireEvent.click(retry);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByAltText('Page 1 of Brief')).toBeTruthy();
  });
});

describe('PagesViewer — memory and cancellation', () => {
  it('revokes every object URL on unmount', async () => {
    const { unmount } = render(<PagesViewer artifactId={ID} version={1} pages={3} title="Brief" />);
    await flush();
    await act(async () => {
      pageObserver().show(() => true);
      thumbObserver().show(() => true);
    });
    await flush();
    expect(minted.length).toBeGreaterThanOrEqual(3);
    expect(revoked).toEqual([]);
    unmount();
    expect(new Set(revoked)).toEqual(new Set(minted));
  });

  it('aborts the old artifact\'s fetches and releases its bitmaps when switched', async () => {
    // Page 1 of the first artifact never finishes on its own; `release`
    // lets the test deliver its bytes late, after the switch.
    const held: { release: () => void } = { release: () => undefined };
    fetchMock.mockImplementationOnce(
      (_url: string, init?: RequestInit) =>
        new Promise((resolve, reject) => {
          if (init?.signal) {
            signals.push(init.signal);
            init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
          }
          held.release = () =>
            resolve({ ok: true, status: 200, blob: async () => new Blob(['x']) } as unknown as Response);
        }),
    );
    const { rerender } = render(<PagesViewer artifactId={ID} version={1} pages={3} title="A" />);
    await flush();
    expect(signals.length).toBe(1);
    expect(signals[0].aborted).toBe(false);

    rerender(<PagesViewer artifactId={ID2} version={1} pages={2} title="B" />);
    await flush();
    // The pending fetch for A was aborted; B's page 1 was requested fresh.
    expect(signals[0].aborted).toBe(true);
    expect(fetchedUrls()).toContain(`/api/artifacts/${ID2}/v/1/preview/1.png?w=1400`);
    expect(screen.getByAltText('Page 1 of B')).toBeTruthy();
    // Even if A's bytes arrive late, nothing is minted for them.
    const before = minted.length;
    held.release();
    await flush();
    expect(minted.length).toBe(before);
  });
});

describe('PagesViewer — navigation and zoom', () => {
  it('moves with the arrow keys and the toolbar, and never past the ends', async () => {
    render(<PagesViewer artifactId={ID} version={1} pages={3} title="Brief" />);
    await flush();
    const region = screen.getByLabelText('Pages of Brief');
    expect(screen.getByText('Page 1 / 3')).toBeTruthy();
    fireEvent.keyDown(region, { key: 'ArrowRight' });
    expect(screen.getByText('Page 2 / 3')).toBeTruthy();
    fireEvent.keyDown(region, { key: 'End' });
    expect(screen.getByText('Page 3 / 3')).toBeTruthy();
    fireEvent.keyDown(region, { key: 'ArrowRight' });
    expect(screen.getByText('Page 3 / 3')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Previous page' }));
    expect(screen.getByText('Page 2 / 3')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Go to page 1' }));
    expect(screen.getByText('Page 1 / 3')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Previous page' }).hasAttribute('disabled')).toBe(true);
  });

  it('zooms by CSS width only — no new fetch', async () => {
    render(<PagesViewer artifactId={ID} version={1} pages={1} title="Brief" />);
    await flush();
    const calls = fetchMock.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(screen.getByText(`${Math.round(ZOOM_STOPS[3] * 100)}%`)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Fit to width' }));
    expect(screen.getByText('100%')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    expect(screen.getByText('75%')).toBeTruthy();
    expect(fetchMock.mock.calls.length).toBe(calls);
  });

  it('picks the most visible page as current', () => {
    expect(mostVisible(new Map([[1, 0.2], [2, 0.9], [3, 0.1]]), 1)).toBe(2);
    expect(mostVisible(new Map([[1, 0], [2, 0]]), 4)).toBe(4);
    expect(mostVisible(new Map(), 7)).toBe(7);
  });
});
