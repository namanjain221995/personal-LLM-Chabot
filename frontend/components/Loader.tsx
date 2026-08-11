'use client';

/**
 * THE loading indicator. One piece of artwork for every "something is
 * happening" state in the app.
 *
 * Before this, four different things meant "working": three shimmer bars before
 * the first token, a bordered CSS spinner beside the web-search line, a
 * shimmering "Thinking…" label, and another spinner in the agent timeline. Four
 * vocabularies for one idea — a user had to learn each of them separately.
 *
 * The artwork is `public/loading.webm` (owner-supplied, 2026-08-11): 150×150
 * VP8 with a REAL alpha channel — 20,425 of its 22,500 pixels are fully
 * transparent — so it composites onto either theme with no matte box. 44 KB,
 * 2.3 s, ~15 fps.
 *
 * WebM alpha is a VP8 extension Safari does not implement, where the video
 * either fails or paints an opaque rectangle over the content behind it. Rather
 * than sniff browsers, this listens for the real `error` event and falls back
 * to an SVG starburst that takes `currentColor` and works in both themes.
 */

import { memo, useEffect, useRef, useState } from 'react';

/** Served from `public/`. */
export const LOADER_SRC = '/loading.webm';
export const LOADER_POSTER = '/loading-poster.png';

const RAY_COUNT = 12;

/** True when the viewer has asked for less motion. Re-evaluated on change. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    setReduced(query.matches);
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    query.addEventListener?.('change', onChange);
    return () => query.removeEventListener?.('change', onChange);
  }, []);
  return reduced;
}

/** The SVG starburst. Only rendered when the video cannot be. */
function Rays({ px, spin, pulse }: { px: number; spin: string; pulse: string }) {
  const center = px / 2;
  const inner = px * 0.2;
  const outer = px * 0.47;
  const halfWidth = px * 0.052;
  const pulseSeconds = parseFloat(pulse);

  return (
    <g
      className="reasoning-star__rays"
      style={{ ['--ts-star-duration' as string]: spin }}
    >
      {Array.from({ length: RAY_COUNT }, (_, i) => (
        // The angle is an SVG transform ATTRIBUTE, not a CSS transform: a
        // running animation's transform overrides an inline one, which once
        // collapsed all twelve rays onto 0° and rendered a single sliver.
        <g key={i} transform={`rotate(${(360 / RAY_COUNT) * i} ${center} ${center})`}>
          <path
            className="reasoning-star__ray"
            style={{
              ['--ts-star-pulse' as string]: pulse,
              ['--ts-star-delay' as string]: `${(
                (i / RAY_COUNT) * pulseSeconds
              ).toFixed(2)}s`,
            }}
            strokeLinejoin="round"
            strokeWidth={px * 0.03}
            stroke="currentColor"
            d={`M ${center} ${center - outer}
                L ${center + halfWidth} ${center - inner}
                L ${center - halfWidth} ${center - inner} Z`}
          />
        </g>
      ))}
    </g>
  );
}

export interface LoaderProps {
  /** Rendered size in px. 16 in a step row, 22 inline, 40 centred. */
  size?: number;
  /**
   * Playback rate. Phases use this to change tempo WITHOUT swapping artwork,
   * so moving between them does not restart the loop.
   */
  rate?: number;
  /**
   * Accessible name. Omit when an adjacent live region or label already says
   * what is happening — otherwise a screen reader hears it twice.
   */
  label?: string;
  className?: string;
}

export const Loader = memo(function Loader({
  size = 22,
  rate = 1,
  label,
  className = '',
}: LoaderProps) {
  const [videoFailed, setVideoFailed] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const reduced = useReducedMotion();

  // Set imperatively: `playbackRate` is a property, not an attribute, so React
  // will not apply it from JSX.
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.playbackRate = reduced ? 0 : rate;
    if (reduced) video.pause();
  }, [rate, reduced]);

  const shared = `reasoning-star--enter shrink-0 ${className}`;
  const a11y = label
    ? { role: 'img' as const, 'aria-label': label }
    : { 'aria-hidden': true };

  if (videoFailed) {
    return (
      <svg
        className={`reasoning-star ${shared} text-accent`}
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        focusable="false"
        {...a11y}
      >
        <Rays
          px={size}
          spin={`${(2.4 / Math.max(rate, 0.1)).toFixed(2)}s`}
          pulse={`${(1.2 / Math.max(rate, 0.1)).toFixed(2)}s`}
        />
      </svg>
    );
  }

  return (
    <video
      ref={videoRef}
      className={shared}
      width={size}
      height={size}
      style={{ width: size, height: size }}
      // `muted` + `playsInline` are what make autoplay legal without a user
      // gesture; without them the browser silently blocks it.
      autoPlay={!reduced}
      loop
      muted
      playsInline
      // Reduced motion still gets the artwork, held on one frame: an indicator
      // that vanishes entirely reads as "nothing is happening".
      poster={LOADER_POSTER}
      preload="auto"
      onError={() => setVideoFailed(true)}
      data-testid="app-loader"
      {...a11y}
    >
      <source src={LOADER_SRC} type="video/webm" />
    </video>
  );
});
