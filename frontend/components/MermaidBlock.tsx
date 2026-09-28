'use client';

/**
 * Mermaid diagram block — renders ```mermaid fenced code as a real diagram
 * (ChatGPT-style): a header bar with Code/Preview toggle, expand-to-fullscreen
 * with zoom, copy, and "download PNG".
 *
 * mermaid is ~1 MB, so it is imported LAZILY the first time a diagram appears —
 * chats without diagrams never pay for it. While the answer is still streaming
 * the code is incomplete and would throw, so we only attempt a render once the
 * source looks like a finished diagram (lib/mermaid.looksRenderable).
 *
 * The fullscreen viewer is portalled to <body>: a transformed ancestor would
 * otherwise become the containing block for position:fixed and both mis-place
 * it and paint it behind the thread (the bug that hit the ⋯ menu).
 *
 * COLOUR and SIZE both live in lib/mermaidTheme.ts, which carries the reasons:
 * `theme: 'base'` (the packaged themes silently discard our themeVariables),
 * the four-name role palette read from `--ts-diagram-*`, the twelve-slot
 * categorical palette that pie/timeline/gitGraph/mindmap/xychart paint from,
 * the source sanitiser that enforces the colour ban PER STATEMENT (a
 * line-anchored one was not enforcement: `C-->D; style A fill:#ff0000` walked
 * straight past it) and PER PREAMBLE (a YAML frontmatter `config: themeCSS`
 * block was covered by neither the statement filter nor mermaid's own `secure`
 * list, and repainted every node, edge, pie slice and sequence actor it was
 * pointed at), and the zoom floor. This file only applies them — plus the one
 * decision that belongs to the component: a source the guard REFUSES is never
 * handed to mermaid at all, and the reader is told so.
 *
 * The inline block is sized in real layout pixels, never with a CSS
 * transform: a transformed ancestor becomes the containing block for
 * position:fixed, which is the same trap the fullscreen viewer records above.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  ZOOM_MAX,
  ZOOM_MIN,
  clampZoom,
  diagramFileName,
  fitZoom,
  looksRenderable,
  prepareSvgForExport,
  svgNaturalSize,
} from '@/lib/mermaid';
import {
  diagramRefusal,
  diagramScale,
  mermaidTheme,
  prepareDiagramSource,
  smallestLabelPx,
  withoutRoleApplications,
} from '@/lib/mermaidTheme';
import { CopyButton } from './CopyButton';
import { useTheme } from './Providers';
import {
  IconCode,
  IconDiagram,
  IconDownload,
  IconExpand,
  IconPlay,
  IconX,
  IconZoomIn,
  IconZoomOut,
} from './icons';

type View = 'preview' | 'code';

let mermaidPromise: Promise<typeof import('mermaid').default> | null = null;
let renderSeq = 0;

/**
 * Load mermaid and apply the theme for `mode`.
 *
 * `mermaid.initialize` is GLOBAL, so there is exactly one config per theme and
 * never one per block: four diagrams in one answer with four configs would
 * race, and the last one to initialize would paint all four.
 */
async function getMermaid(dark: boolean) {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((m) => m.default);
  }
  const mermaid = await mermaidPromise;
  mermaid.initialize(mermaidTheme(dark ? 'dark' : 'light'));
  return mermaid;
}

export function MermaidBlock({ code }: { code: string }) {
  const { theme } = useTheme();
  const dark = theme === 'dark';
  const [svg, setSvg] = useState<string>('');
  /**
   * The source that actually DREW the diagram on screen.
   *
   * Normally identical to `source`; it differs only when the first render threw
   * and the role-free retry below drew instead. The Code tab, the copy button
   * and the PNG name all read this, because showing a source that was not the
   * one drawn is a lie about the diagram — the same reason the sanitised
   * source, not the model's raw text, has always been what they show.
   */
  const [drawn, setDrawn] = useState<string>('');
  const [error, setError] = useState<string>('');
  const [view, setView] = useState<View>('preview');
  const [userPicked, setUserPicked] = useState(false);
  const [full, setFull] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [overflows, setOverflows] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const fullRef = useRef<HTMLDivElement>(null);

  /**
   * What is actually drawn: the model's source with every colour-bearing
   * directive stripped and our four role classDefs appended.
   *
   * The Code tab and the copy button read this same string, so what a person
   * copies is what they were shown — copying a `style A fill:#ff0000` that
   * was never painted would be a lie about the diagram.
   */
  const source = prepareDiagramSource(code, dark ? 'dark' : 'light');

  /**
   * Why this source is not rendered AT ALL, or `''`.
   *
   * `guardDiagramSource` strips both of mermaid's in-source config channels,
   * which is safe because neither changes what is drawn. It refuses instead —
   * and this is that refusal — only where stripping would have to guess: a
   * directive with `}%%` inside its own string value leaves a fragment in the
   * middle of a statement, and both ways out (drop the line, keep the
   * fragment) can put a DIFFERENT graph on screen than the one the source
   * describes. Refusing shows the source instead, which is the same trade the
   * document exporter already makes with its "Diagram omitted" callout.
   */
  const refusal = diagramRefusal(code);

  // Render (or re-render on theme change) once the source looks complete.
  useEffect(() => {
    let cancelled = false;
    if (refusal) {
      setSvg('');
      setDrawn('');
      // Any non-empty string closes the "Rendering the diagram…" line and
      // disables Preview; the notice itself is rendered from `refusal`, so the
      // reader is told the diagram was REFUSED and not that it failed.
      setError(refusal);
      return;
    }
    // The streaming guard judges the MODEL's own output, never the prepared
    // source: `prepareDiagramSource` appends four classDef lines, and
    // `looksRenderable` only asks for a known head plus one body line, so a
    // still-streaming `flowchart LR` with nothing under it yet would look
    // finished and be rendered as an empty diagram.
    if (!looksRenderable(code)) {
      setSvg('');
      setDrawn('');
      // A refusal notice from the PREVIOUS source must not sit over a diagram
      // that is merely still streaming.
      setError('');
      return;
    }
    // A source or theme change invalidates what the previous one drew, so the
    // Code tab never shows a string that is no longer on screen. A functional
    // update keeps the no-op case from costing a render.
    setDrawn((prev) => (prev === source ? prev : ''));
    (async () => {
      /**
       * The source, then the same source with every role application removed.
       *
       * A role outside the flowchart family is FATAL, not inert: measured
       * today in Chromium 153 / mermaid 11.17, `U:::external` in a
       * sequenceDiagram is a parse error and the whole diagram is replaced by
       * the error card. The prompt tells the model roles are for
       * `flowchart`/`graph` only; this attempt is what happens when it puts one
       * somewhere else anyway. It runs only AFTER a throw, so a diagram that
       * renders is never rewritten.
       */
      const attempts = [source];
      const roleFree = withoutRoleApplications(source);
      if (roleFree !== source && roleFree.trim()) attempts.push(roleFree);
      let last: unknown = null;
      try {
        const mermaid = await getMermaid(dark);
        for (const attempt of attempts) {
          try {
            const id = `mmd-${(renderSeq += 1)}`;
            const { svg: out } = await mermaid.render(id, attempt);
            if (!cancelled) {
              setSvg(out);
              setDrawn(attempt);
              setError('');
            }
            return;
          } catch (err) {
            last = err;
          }
        }
        throw last;
      } catch (err) {
        if (!cancelled) {
          setSvg('');
          setDrawn('');
          setError(err instanceof Error ? err.message : 'Diagram failed to render.');
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, source, dark, refusal]);

  /**
   * Size the inline SVG in real layout pixels.
   *
   * mermaid's `useMaxWidth` is off, so the SVG arrives at its natural size and
   * this decides how much of it to show. `diagramScale` fits the column when
   * that keeps labels at or above 12 px and otherwise stops at the floor and
   * lets the block scroll sideways — measured, the shipped policy rendered the
   * architecture diagram's smallest label at 6.0 px beside 17 px answer text,
   * and at 2.3 px on a phone.
   *
   * Width and height are set as ATTRIBUTES, not as a CSS transform: a
   * transformed ancestor becomes the containing block for position:fixed, and
   * the fullscreen viewer above records what that costs.
   */
  useEffect(() => {
    const host = hostRef.current;
    if (!host || !svg) return;
    const natural = svgNaturalSize(svg);

    /**
     * Re-query the live <svg> on EVERY pass rather than capturing it once.
     *
     * The host is filled with dangerouslySetInnerHTML, so React owns those
     * child nodes and may replace them on a later commit. Measured: holding
     * the node from the first pass left this effect sizing a DETACHED element
     * (isConnected false) while the diagram on screen kept its natural width —
     * the architecture diagram rendered at 1773 px in a 668 px column with the
     * scale correctly computed as 0.75 and applied to nothing.
     *
     * Re-querying also makes the ResizeObserver self-healing: a commit that
     * rewrites the host changes its height, which fires the observer, which
     * sizes whatever is actually in the DOM now.
     */
    const apply = () => {
      const el = host.querySelector('svg');
      if (!el) return;
      const style = window.getComputedStyle(host);
      const pad =
        (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
      const hostWidth = host.clientWidth - pad;
      const scale = diagramScale({
        hostWidth,
        naturalWidth: natural?.width ?? 0,
        // Measured from the element in the DOM right now: a replaced node has
        // never been sized, so its labels are still at their natural size.
        smallestLabelPx: smallestLabelPx(el),
      });
      if (natural) {
        const w = Math.round(natural.width * scale);
        const h = Math.round(natural.height * scale);
        el.setAttribute('width', String(w));
        el.setAttribute('height', String(h));
        el.style.width = `${w}px`;
        el.style.height = `${h}px`;
        el.style.maxWidth = 'none';
      }
      setOverflows(host.scrollWidth > host.clientWidth + 1);
    };

    apply();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(apply);
    ro.observe(host);
    return () => ro.disconnect();
  }, [svg]);

  // A diagram that renders flips to preview unless the user chose otherwise.
  useEffect(() => {
    if (svg && !userPicked) setView('preview');
  }, [svg, userPicked]);

  // Escape closes the fullscreen viewer.
  useEffect(() => {
    if (!full) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setFull(false);
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [full]);

  /**
   * What the Code tab shows and the copy button copies: what was drawn.
   *
   * On a REFUSAL nothing was drawn and `source` is not a fallback — it is the
   * half-stripped residue, which is nobody's text. Measured on the commit
   * before this one: for `%%{init: {"themeCSS": "a}%% …"}}%%` the Code tab held
   * ` .node rect { fill: #ff0000 !important }"}}%%` with the author's first
   * line half-eaten, and for a label containing `}%%` it held a two-line
   * `flowchart LR\n  A["` that no one wrote. The notice under it says "Showing
   * the source.", the copy button hands that string on, and it names the
   * downloaded file. A refusal is the one place this component promises the
   * SOURCE instead of a picture, so it has to be the author's.
   */
  const shown = drawn || (refusal ? code : source);

  const downloadPng = useCallback(async () => {
    const host = (full ? fullRef.current : hostRef.current) ?? hostRef.current;
    const el = host?.querySelector('svg');
    if (!el) return;
    const box = el.getBoundingClientRect();
    const vb = el.viewBox?.baseVal;
    const width = Math.max(box.width || 0, vb?.width || 0, 320);
    const height = Math.max(box.height || 0, vb?.height || 0, 240);
    const background = dark ? '#1e1e1e' : '#ffffff';
    const prepared = prepareSvgForExport(el.outerHTML, width, height, background);
    const blob = new Blob([prepared], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);

    /** Save `data` as `name` — used for the PNG and the SVG fallback. */
    const save = (data: Blob, name: string) => {
      const href = URL.createObjectURL(data);
      const link = document.createElement('a');
      link.href = href;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(href), 10_000);
    };

    try {
      await new Promise<void>((resolve, reject) => {
        const img = new Image();
        img.onload = () => {
          const scale = 2; // crisp on retina / when zoomed into
          const canvas = document.createElement('canvas');
          canvas.width = Math.round(width * scale);
          canvas.height = Math.round(height * scale);
          const ctx = canvas.getContext('2d');
          if (!ctx) return reject(new Error('canvas unavailable'));
          ctx.fillStyle = background;
          ctx.fillRect(0, 0, canvas.width, canvas.height);
          ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
          try {
            canvas.toBlob((png) => {
              if (!png) return reject(new Error('export failed'));
              // The anchor MUST be in the document for Chromium to honour the
              // click, and the object URL must outlive the download start.
              save(png, diagramFileName(shown, 'png'));
              resolve();
            }, 'image/png');
          } catch (err) {
            // e.g. a tainted canvas — caller falls back to the SVG.
            reject(err instanceof Error ? err : new Error('export failed'));
          }
        };
        img.onerror = () => reject(new Error('render failed'));
        img.src = url;
      });
    } catch {
      // PNG rasterization can fail (tainted canvas, blocked image). Always
      // give the user a file: the SVG is vector, opens anywhere, and never
      // taints anything.
      save(blob, diagramFileName(shown, 'svg'));
    } finally {
      URL.revokeObjectURL(url);
    }
  }, [shown, dark, full]);

  const pick = (v: View) => {
    setUserPicked(true);
    setView(v);
  };

  const natural = svgNaturalSize(svg);

  /** Zoom that fits the whole diagram inside the fullscreen viewport. */
  const computeFit = useCallback(
    () => fitZoom(natural, window.innerWidth - 96, window.innerHeight - 140),
    [natural],
  );

  const openFullscreen = () => {
    setZoom(computeFit()); // open at "fit to screen", like ChatGPT
    setFull(true);
  };

  const controls = (
    <>
      <button
        type="button"
        onClick={() => pick('code')}
        aria-pressed={view === 'code'}
        aria-label="Show diagram source"
        title="Code"
        className={`rounded-md p-1.5 transition-colors duration-ts hover:bg-surface-2 ${
          view === 'code' ? 'bg-surface-2 text-ink' : 'text-muted'
        }`}
      >
        <IconCode size={15} />
      </button>
      <button
        type="button"
        onClick={() => pick('preview')}
        disabled={!svg}
        aria-pressed={view === 'preview'}
        aria-label="Show rendered diagram"
        title="Preview"
        className={`rounded-md p-1.5 transition-colors duration-ts hover:bg-surface-2 disabled:opacity-40 ${
          view === 'preview' && svg ? 'bg-surface-2 text-ink' : 'text-muted'
        }`}
      >
        <IconPlay size={15} />
      </button>
      <button
        type="button"
        onClick={openFullscreen}
        disabled={!svg}
        aria-label="Open diagram fullscreen"
        title="Fullscreen"
        className="rounded-md p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:opacity-40"
      >
        <IconExpand size={15} />
      </button>
      <button
        type="button"
        onClick={downloadPng}
        disabled={!svg}
        aria-label="Download diagram as PNG"
        title="Download PNG"
        className="rounded-md p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:opacity-40"
      >
        <IconDownload size={15} />
      </button>
      <CopyButton text={shown} label="Copy diagram source" />
    </>
  );

  return (
    <>
      <div className="code-block overflow-hidden rounded-ts border border-border bg-surface">
        <div className="flex items-center justify-between gap-2 border-b border-border bg-surface-2/60 px-3 py-1.5">
          <span className="inline-flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">
            <IconDiagram size={13} />
            Mermaid
          </span>
          <span className="flex items-center gap-0.5">{controls}</span>
        </div>

        {view === 'preview' && svg ? (
          <>
            {/* No max-height: the 480 px cap made a tall diagram scroll INSIDE
                the answer, a scroll area within a scroll area. The block grows
                to the diagram's height and scrolls sideways instead. */}
            <div
              ref={hostRef}
              className="mermaid-host overflow-x-auto overflow-y-hidden bg-surface p-4"
              // mermaid output is sanitized by securityLevel: 'strict'
              dangerouslySetInnerHTML={{ __html: svg }}
            />
            {overflows && (
              <p className="border-t border-border px-3 py-1.5 text-[11px] text-faint">
                This diagram is wider than the column — scroll it sideways, or
                open it fullscreen.
              </p>
            )}
          </>
        ) : (
          <div>
            {!svg && !error && (
              <p className="border-b border-border px-3 py-1.5 text-[11px] text-faint">
                Rendering the diagram…
              </p>
            )}
            {error && (
              <p className="border-b border-border px-3 py-1.5 text-[11px] text-danger">
                {refusal
                  ? `This diagram was not drawn — ${refusal}. Showing the source.`
                  : "Couldn't render this diagram — showing the source."}
              </p>
            )}
            <pre tabIndex={0}>
              <code>{shown}</code>
            </pre>
          </div>
        )}
      </div>

      {full &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Diagram viewer"
            className="fixed inset-0 z-[60] flex flex-col bg-black/80 backdrop-blur-sm"
          >
            <div className="flex items-center justify-between gap-2 px-4 py-3">
              <span className="inline-flex items-center gap-1.5 text-sm text-ink">
                <IconDiagram size={15} />
                Diagram
              </span>
              <span className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => setZoom((z) => clampZoom(z / 1.25))}
                  disabled={zoom <= ZOOM_MIN}
                  aria-label="Zoom out"
                  className="rounded-md p-2 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:opacity-40"
                >
                  <IconZoomOut size={16} />
                </button>
                <span className="w-12 text-center font-mono text-xs text-muted">
                  {Math.round(zoom * 100)}%
                </span>
                <button
                  type="button"
                  onClick={() => setZoom((z) => clampZoom(z * 1.25))}
                  disabled={zoom >= ZOOM_MAX}
                  aria-label="Zoom in"
                  className="rounded-md p-2 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:opacity-40"
                >
                  <IconZoomIn size={16} />
                </button>
                <button
                  type="button"
                  onClick={() => setZoom(computeFit())}
                  aria-label="Fit diagram to screen"
                  className="rounded-md px-2 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                >
                  Fit
                </button>
                <button
                  type="button"
                  onClick={() => setZoom(1)}
                  className="rounded-md px-2 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                >
                  100%
                </button>
                <button
                  type="button"
                  onClick={downloadPng}
                  aria-label="Download diagram as PNG"
                  title="Download PNG"
                  className="rounded-md p-2 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                >
                  <IconDownload size={16} />
                </button>
                <button
                  type="button"
                  onClick={() => setFull(false)}
                  aria-label="Close diagram viewer"
                  className="rounded-md p-2 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                >
                  <IconX size={16} />
                </button>
              </span>
            </div>
            <div className="flex-1 overflow-auto p-6">
              {/* Zoom = real layout size (not a CSS transform): the SVG
                  re-renders vector-crisp at every level and the scroll area
                  grows/shrinks with it, so panning a zoomed diagram works. */}
              <div
                className="mx-auto"
                style={
                  natural
                    ? {
                        width: Math.round(natural.width * zoom),
                        height: Math.round(natural.height * zoom),
                      }
                    : undefined
                }
              >
                <div
                  ref={fullRef}
                  className="mermaid-host mermaid-full h-full w-full"
                  dangerouslySetInnerHTML={{ __html: svg }}
                />
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
