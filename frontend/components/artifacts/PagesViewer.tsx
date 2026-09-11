'use client';

/**
 * Page-image viewer for a version with `preview_kind: "pages"` — a PDF, or
 * the PDF twin of a DOCX/PPTX (docs/artifact-studio/CONTRACT.md §6).
 *
 * Why images and not an <iframe>/<object>: X-Frame-Options: DENY sits on
 * every frontend path (next.config.mjs), so a framed /api URL is blocked,
 * and a browser PDF plugin is useless on a phone. The server rasterises
 * pages lazily (pypdfium2) at two widths — 1400 for the page, 240 for the
 * thumbnail — and this component asks for each one exactly when it is about
 * to be seen (IntersectionObserver, 600 px ahead), never for all of them: a
 * 40-page deck at 1400 px is tens of megabytes, and most of it is never
 * scrolled to.
 *
 * Bytes are fetched, not linked, so that (a) a failed page is a state this
 * component can name and retry rather than a broken-image icon, and (b) the
 * memory is OURS to release: every object URL is revoked when the viewer
 * unmounts or switches artifacts, and every in-flight fetch for the old
 * artifact is aborted at the same moment — the AttachmentPreview lifecycle
 * (create on open, revoke in cleanup, never on a timer).
 *
 * Zoom is CSS width only. The server renders one size per width; "zoom" is
 * how much of the column that image occupies. No re-fetch, no percentages
 * invented from a bitmap.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { artifactUrls } from '@/lib/artifacts';
import { IconChevronLeft, IconChevronRight, IconExpand, IconZoomIn, IconZoomOut } from '../icons';
import { Loader } from '../Loader';

type ImageState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; url: string }
  | { status: 'error' };

/** Zoom stops. `1` is fit-to-width; the others are multiples of it. */
export const ZOOM_STOPS = [0.5, 0.75, 1, 1.25, 1.5, 2] as const;

const PAGE_WIDTH = 1400;
const THUMB_WIDTH = 240;

/** Which page is "current": the one with the largest visible share. */
export function mostVisible(ratios: Map<number, number>, fallback: number): number {
  let best = fallback;
  let bestRatio = -1;
  for (const [page, ratio] of ratios) {
    if (ratio > bestRatio) {
      bestRatio = ratio;
      best = page;
    }
  }
  return bestRatio > 0 ? best : fallback;
}

export function PagesViewer({
  artifactId,
  version,
  pages,
  title,
}: {
  artifactId: string;
  version: number;
  /** `preview_pages` — how many the server will answer for. */
  pages: number;
  title: string;
}) {
  const total = Math.max(0, Math.trunc(pages));
  const [images, setImages] = useState<Map<number, ImageState>>(() => new Map());
  const [thumbs, setThumbs] = useState<Map<number, ImageState>>(() => new Map());
  const [current, setCurrent] = useState(1);
  const [zoomIndex, setZoomIndex] = useState(2); // ZOOM_STOPS[2] === 1 (fit)

  const scrollRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef<Map<number, HTMLElement>>(new Map());
  const thumbRefs = useRef<Map<number, HTMLElement>>(new Map());
  /** One controller per (artifact, version): switching aborts everything. */
  const controllerRef = useRef<AbortController | null>(null);
  /** Every object URL minted for THIS artifact, so the cleanup can revoke them all. */
  const urlsRef = useRef<Set<string>>(new Set());
  const ratiosRef = useRef<Map<number, number>>(new Map());
  /** `page:width` slots with a fetch started or finished for THIS artifact. */
  const inflightRef = useRef<Set<string>>(new Set());

  const key = `${artifactId}:${version}`;

  // Reset per artifact: abort the old fetches, release the old bitmaps,
  // forget the old states. Runs on unmount too — that IS the release.
  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    const urls = urlsRef.current;
    ratiosRef.current = new Map();
    inflightRef.current = new Set();
    setImages(new Map());
    setThumbs(new Map());
    setCurrent(1);
    return () => {
      controller.abort();
      for (const url of urls) URL.revokeObjectURL(url);
      urls.clear();
    };
  }, [key]);

  const load = useCallback(
    (page: number, width: typeof PAGE_WIDTH | typeof THUMB_WIDTH) => {
      const controller = controllerRef.current;
      if (!controller || controller.signal.aborted) return;
      // The ref, not the state, decides who owns a fetch: a state updater
      // may run later than the call that queued it, so two observers firing
      // for the same page in one frame would both start a request.
      const slot = `${page}:${width}`;
      if (inflightRef.current.has(slot)) return;
      inflightRef.current.add(slot);
      const setter = width === PAGE_WIDTH ? setImages : setThumbs;
      const put = (state: ImageState) =>
        setter((prev) => {
          const next = new Map(prev);
          next.set(page, state);
          return next;
        });
      put({ status: 'loading' });
      const { signal } = controller;
      void (async () => {
        try {
          // `default`, not `no-store`: a page image is immutable per version
          // and the orchestrator says so (`Cache-Control: private, max-age=3600`,
          // passed through by the proxy). With no-store, every re-open of the
          // same version re-downloaded every 1400 px page it scrolled to. JSON
          // (lib/artifacts.ts getJson) stays no-store — a job's status changes.
          const res = await fetch(artifactUrls.page(artifactId, version, page, width), {
            credentials: 'same-origin',
            cache: 'default',
            signal,
          });
          if (!res.ok) throw new Error(String(res.status));
          const blob = await res.blob();
          if (signal.aborted) return;
          const url = URL.createObjectURL(blob);
          urlsRef.current.add(url);
          put({ status: 'ready', url });
        } catch {
          if (signal.aborted) return;
          // Forgotten on failure so "try again" can own a new fetch.
          inflightRef.current.delete(slot);
          put({ status: 'error' });
        }
      })();
    },
    [artifactId, version],
  );

  // The first page is always wanted; the rest wait for the observer.
  useEffect(() => {
    if (total > 0) load(1, PAGE_WIDTH);
  }, [key, total, load]);

  // Lazy loading + current-page tracking, one observer each. Without
  // IntersectionObserver (a very old browser) every page is requested up
  // front — slower, but the document still appears.
  useEffect(() => {
    if (total === 0) return;
    if (typeof IntersectionObserver === 'undefined') {
      for (let p = 1; p <= total; p += 1) load(p, PAGE_WIDTH);
      return;
    }
    const root = scrollRef.current;
    const loader = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const page = Number((entry.target as HTMLElement).dataset.page);
          if (page) load(page, PAGE_WIDTH);
        }
      },
      { root, rootMargin: '600px 0px' },
    );
    const tracker = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const page = Number((entry.target as HTMLElement).dataset.page);
          if (page) ratiosRef.current.set(page, entry.isIntersecting ? entry.intersectionRatio : 0);
        }
        setCurrent((prev) => mostVisible(ratiosRef.current, prev));
      },
      { root, threshold: [0, 0.25, 0.5, 0.75, 1] },
    );
    for (const el of pageRefs.current.values()) {
      loader.observe(el);
      tracker.observe(el);
    }
    return () => {
      loader.disconnect();
      tracker.disconnect();
    };
  }, [key, total, load]);

  // Thumbnails: their own observer against the strip, at 240 px.
  useEffect(() => {
    if (total === 0) return;
    if (typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const page = Number((entry.target as HTMLElement).dataset.thumb);
          if (page) load(page, THUMB_WIDTH);
        }
      },
      { rootMargin: '300px' },
    );
    for (const el of thumbRefs.current.values()) io.observe(el);
    return () => io.disconnect();
  }, [key, total, load]);

  const goTo = useCallback(
    (page: number) => {
      const target = Math.min(total, Math.max(1, page));
      const el = pageRefs.current.get(target);
      if (el && typeof el.scrollIntoView === 'function') {
        el.scrollIntoView({ block: 'start' });
      }
      setCurrent(target);
    },
    [total],
  );

  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    const t = e.target as HTMLElement | null;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'PageDown') {
      e.preventDefault();
      goTo(current + 1);
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'PageUp') {
      e.preventDefault();
      goTo(current - 1);
    } else if (e.key === 'Home') {
      e.preventDefault();
      goTo(1);
    } else if (e.key === 'End') {
      e.preventDefault();
      goTo(total);
    }
  }

  const zoom = ZOOM_STOPS[zoomIndex];
  const pageNumbers = useMemo(() => Array.from({ length: total }, (_, i) => i + 1), [total]);

  const toolButton =
    'inline-flex h-7 w-7 items-center justify-center rounded-md text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:opacity-40 disabled:hover:bg-transparent';

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="pages-viewer">
      <div
        role="toolbar"
        aria-label="Page controls"
        className="flex items-center gap-1 border-b border-border px-3 py-1.5 text-xs text-muted"
      >
        <button
          type="button"
          className={toolButton}
          onClick={() => goTo(current - 1)}
          disabled={current <= 1}
          aria-label="Previous page"
          title="Previous page (←)"
        >
          <IconChevronLeft size={15} />
        </button>
        <span className="tabular-nums" aria-live="polite" aria-atomic="true">
          Page {current} / {total}
        </span>
        <button
          type="button"
          className={toolButton}
          onClick={() => goTo(current + 1)}
          disabled={current >= total}
          aria-label="Next page"
          title="Next page (→)"
        >
          <IconChevronRight size={15} />
        </button>
        <span className="ml-auto flex items-center gap-1">
          <button
            type="button"
            className={toolButton}
            onClick={() => setZoomIndex((i) => Math.max(0, i - 1))}
            disabled={zoomIndex === 0}
            aria-label="Zoom out"
            title="Zoom out"
          >
            <IconZoomOut size={15} />
          </button>
          <span className="w-10 text-center tabular-nums">{Math.round(zoom * 100)}%</span>
          <button
            type="button"
            className={toolButton}
            onClick={() => setZoomIndex((i) => Math.min(ZOOM_STOPS.length - 1, i + 1))}
            disabled={zoomIndex === ZOOM_STOPS.length - 1}
            aria-label="Zoom in"
            title="Zoom in"
          >
            <IconZoomIn size={15} />
          </button>
          <button
            type="button"
            className={toolButton}
            onClick={() => setZoomIndex(2)}
            aria-label="Fit to width"
            title="Fit to width"
          >
            <IconExpand size={15} />
          </button>
        </span>
      </div>

      <div
        ref={scrollRef}
        tabIndex={0}
        onKeyDown={onKeyDown}
        aria-label={`Pages of ${title}`}
        className="min-h-0 flex-1 overflow-auto bg-bg px-4 py-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50"
      >
        <div className="mx-auto flex flex-col gap-4" style={{ width: `${zoom * 100}%`, maxWidth: zoom <= 1 ? '900px' : undefined }}>
          {pageNumbers.map((page) => {
            const state = images.get(page) ?? { status: 'idle' as const };
            return (
              <figure
                key={page}
                data-page={page}
                ref={(el) => {
                  if (el) pageRefs.current.set(page, el);
                  else pageRefs.current.delete(page);
                }}
                aria-current={page === current ? 'page' : undefined}
                className={`relative m-0 overflow-hidden rounded-md border bg-white shadow-sm ${
                  page === current ? 'border-accent/50' : 'border-border'
                }`}
              >
                {state.status === 'ready' ? (
                  /* A blob: URL — next/image cannot optimise what the
                     browser already holds in memory. */
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={state.url}
                    alt={`Page ${page} of ${title}`}
                    className="block h-auto w-full"
                    draggable={false}
                  />
                ) : (
                  <div className="flex aspect-[1/1.3] w-full items-center justify-center bg-surface-2">
                    {state.status === 'error' ? (
                      <button
                        type="button"
                        onClick={() => load(page, PAGE_WIDTH)}
                        className="rounded-md border border-border bg-surface px-2.5 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                      >
                        Page {page} could not be loaded — try again
                      </button>
                    ) : state.status === 'loading' ? (
                      <Loader size={22} label={`Loading page ${page}`} />
                    ) : (
                      <span className="text-xs text-faint">Page {page}</span>
                    )}
                  </div>
                )}
                <figcaption className="absolute bottom-1.5 right-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] tabular-nums text-white">
                  {page}
                </figcaption>
              </figure>
            );
          })}
        </div>
      </div>

      {total > 1 && (
        <nav
          aria-label="Page thumbnails"
          className="flex shrink-0 gap-2 overflow-x-auto border-t border-border bg-surface px-3 py-2"
          data-testid="thumbnail-strip"
        >
          {pageNumbers.map((page) => {
            const state = thumbs.get(page) ?? { status: 'idle' as const };
            return (
              <button
                key={page}
                type="button"
                data-thumb={page}
                ref={(el) => {
                  if (el) thumbRefs.current.set(page, el);
                  else thumbRefs.current.delete(page);
                }}
                onClick={() => goTo(page)}
                aria-label={`Go to page ${page}`}
                aria-current={page === current ? 'page' : undefined}
                className={`flex h-[72px] w-[56px] shrink-0 flex-col items-center justify-end overflow-hidden rounded border bg-surface-2 transition-colors duration-ts ${
                  page === current ? 'border-accent' : 'border-border hover:border-accent/50'
                }`}
              >
                {state.status === 'ready' ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={state.url} alt="" className="h-full w-full object-cover object-top" draggable={false} />
                ) : (
                  <span className="pb-1 text-[10px] text-faint">{page}</span>
                )}
              </button>
            );
          })}
        </nav>
      )}
    </div>
  );
}
