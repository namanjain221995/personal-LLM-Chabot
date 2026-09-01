'use client';

/**
 * The brand panel on /login and /accept-invite: one of the workspace
 * illustrations on a soft tinted card, with a caption and dot indicators.
 *
 * WHY IT CHANGES. The owner asked for a different picture per person and a
 * different one after signing out and back in. There is no identity to key on
 * at the sign-in screen — that is the whole point of the page — so the choice
 * is RANDOM PER PAGE LOAD: two people opening /login see different art, and so
 * does the same person returning after a logout (logout is a full navigation
 * to /login, which re-runs this).
 *
 * The random pick happens in an effect, never during render: the server has no
 * idea which one the client will choose, and choosing during render would be a
 * hydration mismatch. The panel fades in once mounted, so the swap from the
 * server's first frame is never visible.
 *
 * Then it rotates gently, like the carousel in the reference design — unless
 * the visitor asked for reduced motion, in which case the art is still random
 * but stays put (auto-advancing content is exactly what that preference is
 * about). The dots stay clickable either way.
 *
 * Artwork lives in public/illustrator/ (the owner's folder). The .webp files
 * are trimmed, downscaled derivatives of the .png originals beside them —
 * 2000x2000 PNGs at ~470 KB each would otherwise be the heaviest thing on a
 * page whose job is to load instantly.
 */

import { useEffect, useRef, useState } from 'react';

interface Illustration {
  slug: string;
  /** Empty: the art repeats the caption beside it, so it is decorative. */
  alt: '';
  title: string;
  body: string;
  /** Card wash, sampled from each drawing so the panel belongs to its art. */
  tint: string;
  /** Second stop of the wash, warmer/deeper, for a soft diagonal. */
  tintDeep: string;
}

export const ILLUSTRATIONS: Illustration[] = [
  {
    slug: 'login',
    alt: '',
    title: 'Your workspace, secured.',
    body: 'Every conversation, file and report belongs to the person who made it.',
    tint: '#fdf6e3',
    tintDeep: '#f8e7bd',
  },
  {
    slug: 'team',
    alt: '',
    title: 'Built for the whole team.',
    body: 'Invite colleagues, set their role, and keep everyone in one workspace.',
    tint: '#fdf7e6',
    tintDeep: '#f6e6c4',
  },
  {
    slug: 'good-team',
    alt: '',
    title: 'Answers your team can trust.',
    body: 'Every answer shows its sources — the data, the query, the documents.',
    tint: '#fdefeb',
    tintDeep: '#f9dcd4',
  },
  {
    slug: 'discussion',
    alt: '',
    title: 'Ask in plain language.',
    body: 'Salesforce, your documents, the web — one place to ask, one thread to follow.',
    tint: '#eef4f6',
    tintDeep: '#dce9ec',
  },
  {
    slug: 'idea',
    alt: '',
    title: 'Insight from your own data.',
    body: 'Deep research and analysis that run on your machines, not someone else’s.',
    tint: '#fdf6e0',
    tintDeep: '#f7e6b8',
  },
  {
    slug: 'company',
    alt: '',
    title: 'Everything your company knows.',
    body: 'Records, files and knowledge packs, searchable from a single question.',
    tint: '#fdf7e8',
    tintDeep: '#f6e8c8',
  },
  {
    slug: 'company-team',
    alt: '',
    title: 'One workspace, many people.',
    body: 'Private by default, with the oversight an enterprise workspace needs.',
    tint: '#fdf0ec',
    tintDeep: '#f8ded6',
  },
];

/** How long each illustration is shown before the next one fades in. */
const ROTATE_MS = 8000;

export function IllustrationPanel() {
  // Index 0 on the server AND on the client's first paint — identical markup,
  // so hydration is clean. The effect below replaces it immediately.
  const [index, setIndex] = useState(0);
  // Which illustrations have an <img> in the DOM. Only these are fetched, so
  // a visit costs ONE image (~100 KB), not all seven (~700 KB).
  //
  // They used to be mounted all at once with loading="lazy" on every one but
  // the first — and a lazy image that is stacked, transparent and off-screen
  // is not fetched, so landing on a random NON-ZERO index rendered an empty
  // panel until something else woke the loader. Mounting on demand and
  // loading eagerly is both lighter and correct.
  const [mounted, setMounted] = useState<number[]>([0]);
  // Which mounted images have actually finished loading. The panel only
  // switches to one that HAS — otherwise a picture that is still arriving
  // leaves the card blank under its own caption, which is what a slow
  // connection (and a headless capture) reliably produced.
  const [loaded, setLoaded] = useState<number[]>([]);
  // An illustration whose file will not load is dropped from the rotation
  // rather than left to freeze the panel on it forever.
  const [failed, setFailed] = useState<number[]>([]);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  /** The last index actually painted — what stays on screen while waiting. */
  const shownRef = useRef(0);

  useEffect(() => {
    const reduced =
      typeof window.matchMedia === 'function' &&
      // Optional chaining, not a bare `.matches`: some embedded webviews ship
      // a matchMedia that returns nothing for an unrecognised query.
      window.matchMedia('(prefers-reduced-motion: reduce)')?.matches === true;

    setIndex(Math.floor(Math.random() * ILLUSTRATIONS.length));

    if (reduced) return;
    timer.current = setInterval(
      () => setIndex((i) => (i + 1) % ILLUSTRATIONS.length),
      ROTATE_MS,
    );

    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, []);

  // Mount whatever is showing, plus the one after it, so the next cross-fade
  // has its image decoded before it is asked to appear.
  useEffect(() => {
    const next = (index + 1) % ILLUSTRATIONS.length;
    setMounted((prev) =>
      prev.includes(index) && prev.includes(next)
        ? prev
        : [...new Set([...prev, index, next])],
    );
  }, [index]);

  /** A dot press takes over: stop the drift so it cannot yank the choice back. */
  function select(next: number) {
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
    setIndex(next);
  }

  // Everything the reader sees — art, caption, tint, active dot — is keyed to
  // the illustration that is genuinely on screen, so the three can never
  // disagree (a caption describing a picture that has not arrived is worse
  // than a slightly late swap).
  if (loaded.includes(index)) shownRef.current = index;
  const shown = loaded.includes(index) ? index : shownRef.current;
  // A broken file must not hold the rotation: once it errors, move along.
  useEffect(() => {
    if (failed.includes(index)) {
      setIndex((i) => (i + 1) % ILLUSTRATIONS.length);
    }
  }, [failed, index]);
  const current = ILLUSTRATIONS[shown];

  return (
    <div
      className="auth-illustration-card relative flex h-full flex-col items-center justify-center overflow-hidden rounded-[20px] px-10 py-12 text-center"
      style={{
        background: `linear-gradient(150deg, ${current.tint} 0%, ${current.tintDeep} 100%)`,
      }}
    >
      {/* Every frame is mounted and cross-faded, so switching never reflows the
          column and the browser has already decoded the next image. */}
      <div className="relative flex w-full flex-1 items-center justify-center">
        {ILLUSTRATIONS.map((art, i) =>
          mounted.includes(i) ? (
            <img
              key={art.slug}
              src={`/illustrator/${art.slug}.webp`}
              alt={art.alt}
              aria-hidden="true"
              draggable={false}
              // Everything mounted here is either on screen now or about to
              // be — there is nothing to defer.
              loading="eager"
              decoding="async"
              onLoad={() =>
                setLoaded((prev) => (prev.includes(i) ? prev : [...prev, i]))
              }
              onError={() =>
                setFailed((prev) => (prev.includes(i) ? prev : [...prev, i]))
              }
              className={`auth-illustration max-h-[58vh] w-auto max-w-[560px] select-none object-contain ${
                i === shown ? 'is-current' : ''
              }`}
            />
          ) : null,
        )}
      </div>

      {/* Keyed on the slug so React swaps the node and the CSS entrance
          replays — and, critically, the copy is VISIBLE without JavaScript
          having run. An opacity gate here meant a slow (or blocked) hydration
          left the panel captionless. */}
      <div key={current.slug} className="auth-illustration-copy mt-9 max-w-md">
        <p className="text-lg font-semibold leading-snug tracking-tight text-[#12212e]">
          {current.title}
        </p>
        <p className="mt-2 text-sm leading-relaxed text-[#12212e] opacity-70">
          {current.body}
        </p>
      </div>

      <div className="mt-8 flex items-center justify-center gap-2">
        {ILLUSTRATIONS.map((art, i) => (
          <button
            key={art.slug}
            type="button"
            onClick={() => select(i)}
            aria-label={`Show illustration ${i + 1} of ${ILLUSTRATIONS.length}`}
            aria-current={i === shown}
            className={`auth-dot ${i === shown ? 'is-current' : ''}`}
          />
        ))}
      </div>
    </div>
  );
}
