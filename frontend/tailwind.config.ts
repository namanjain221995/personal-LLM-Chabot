import type { Config } from 'tailwindcss';

/** `100vh` then `100dvh` — see the height/minHeight note below. */
const FALLBACK_DVH = ['100vh', '100dvh'] as unknown as string;

/**
 * TechSara design tokens (§9) are defined as CSS variables in app/globals.css
 * (dark theme is primary; light theme overrides under html.light).
 * Tailwind maps semantic names onto those variables so components never
 * hard-code a hex value.
 */
const config: Config = {
  darkMode: 'class',
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        bg: 'var(--ts-bg)',
        sidebar: 'var(--ts-sidebar)',
        bubble: 'var(--ts-bubble)',
        surface: 'var(--ts-surface)',
        'surface-2': 'var(--ts-surface-2)',
        border: 'var(--ts-border)',
        ink: 'var(--ts-text)',
        muted: 'var(--ts-text-muted)',
        faint: 'var(--ts-text-faint)',
        icon: 'var(--ts-text-icon)',
        // rgb()+<alpha-value> so `accent/NN` opacity modifiers actually
        // compile — as a bare var() Tailwind silently dropped them all.
        accent: 'rgb(var(--ts-accent-rgb) / <alpha-value>)',
        'accent-strong': 'var(--ts-accent-strong)',
        navy: 'var(--ts-navy)',
        boardroom: 'var(--ts-boardroom)',
        slate: 'var(--ts-slate)',
        paper: 'var(--ts-paper)',
        'engine-sql': 'var(--ts-engine-sql)',
        'engine-rag': 'var(--ts-engine-rag)',
        'engine-vision': 'var(--ts-engine-vision)',
        'engine-report': 'var(--ts-engine-report)',
        // Same alpha treatment as `accent`: `danger/NN` classes (toast
        // borders, file-chip tints) silently compiled to nothing before.
        danger: 'rgb(var(--ts-danger-rgb) / <alpha-value>)',
        warn: 'var(--ts-warn)',
        // Same alpha treatment as `accent` and `danger`: as a bare var() the
        // `ok/NN` opacity modifiers compiled to nothing, so the analytics
        // console's positive-change badge had green text on no background.
        ok: 'rgb(var(--ts-ok-rgb) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      fontSize: {
        // §9 scale: 13 / 14 / 16 / 20 / 28
        xs: ['13px', '1.5'],
        sm: ['14px', '1.55'],
        base: ['16px', '1.6'],
        lg: ['20px', '1.4'],
        xl: ['28px', '1.25'],
      },
      borderRadius: {
        ts: '10px',
      },
      transitionDuration: {
        ts: '150ms',
      },
      maxWidth: {
        thread: '768px',
        /* The admin content column. 1180px holds the roster's six columns
           without compressing them and still stops the eye from travelling
           the full width of a 27" display — settings pages are read, not
           scanned edge to edge. */
        admin: '1180px',
      },
      width: {
        sidebar: '260px',
      },
      /* `h-dvh` / `min-h-dvh` emitted `100dvh` ALONE. An engine that does not
         know the unit drops the whole declaration and the shell falls back to
         `height: auto` — the sidebar shortens and the composer stops being
         pinned. An array emits both declarations in order, so `100dvh` still
         wins wherever it is understood and `100vh` catches everything else.
         Centralised here rather than at each call site, so every h-dvh in the
         app (chat shell, admin layout, auth pages) is covered by one edit.

         The cast is a TYPE gap, not a behaviour one: Tailwind v3 resolves an
         array to successive declarations, but types the scale as
         KeyValuePair<string, string>. Verified against the compiled utility —
         `.h-dvh{height:100vh;height:100dvh}`. */
      height: {
        dvh: FALLBACK_DVH,
      },
      minHeight: {
        dvh: FALLBACK_DVH,
      },
    },
  },
  plugins: [],
};

export default config;
