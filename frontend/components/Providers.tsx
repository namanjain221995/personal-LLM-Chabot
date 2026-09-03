'use client';

/**
 * Theme + toast context for the whole app.
 * Theme: dark primary; persisted to localStorage('techsara.theme'); the
 * html class is stamped pre-hydration by the inline script in layout.tsx.
 * Toasts: small stack, bottom-center, aria-live polite.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';

type Theme = 'dark' | 'light';

interface ThemeContextValue {
  theme: Theme;
  toggleTheme: () => void;
}

interface Toast {
  id: number;
  text: string;
  tone: 'info' | 'error';
  /** tone + normalised text: what makes two toasts "the same one". */
  key: string;
}

/** How long a toast stays. Re-triggering an identical one restarts this. */
const TOAST_MS = 5200;

/**
 * The identity a toast is deduplicated on. Whitespace differences are not a
 * different message; a different TONE is — the same words as an error and as
 * an info line are two notifications with two meanings, and the type rules
 * decide that, not this.
 */
function toastKey(text: string, tone: 'info' | 'error'): string {
  return `${tone}:${text.trim().replace(/\s+/g, ' ')}`;
}

interface ToastContextValue {
  toast: (text: string, tone?: 'info' | 'error') => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: 'dark',
  toggleTheme: () => undefined,
});

const ToastContext = createContext<ToastContextValue>({
  toast: () => undefined,
});

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}

export function useToast(): ToastContextValue {
  return useContext(ToastContext);
}

export function Providers({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>('dark');
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextToastId = useRef(1);
  /** Dismissal timers by toast id, so re-triggering can restart one. */
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  useEffect(() => {
    // Read the class the pre-hydration script stamped.
    setTheme(
      document.documentElement.classList.contains('light') ? 'light' : 'dark',
    );
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next: Theme = prev === 'dark' ? 'light' : 'dark';
      const root = document.documentElement;
      root.classList.remove('dark', 'light');
      root.classList.add(next);
      root.style.colorScheme = next;
      try {
        localStorage.setItem('techsara.theme', next);
      } catch {
        // storage unavailable — theme still applies for this session
      }
      return next;
    });
  }, []);

  /**
   * Show a notification — ONCE per identical active message (owner request
   * 2026-09-03).
   *
   * Clicking a failing action five times used to stack five copies of the
   * same error. Deduplication lives HERE, in the provider, rather than as a
   * guard at each of the call sites: a toast that is already on screen with
   * the same tone and text is not appended again, its timer simply restarts
   * so it stays as long as the latest trigger wants it to. Once it has gone
   * — timed out or otherwise — the same message may appear again; nothing is
   * suppressed for good. Different messages, and the same words with a
   * different tone, are different toasts and stack as before.
   */
  const toast = useCallback((text: string, tone: 'info' | 'error' = 'info') => {
    const key = toastKey(text, tone);
    const arm = (id: number) => {
      const old = timers.current.get(id);
      if (old) clearTimeout(old);
      timers.current.set(
        id,
        setTimeout(() => {
          timers.current.delete(id);
          setToasts((prev) => prev.filter((t) => t.id !== id));
        }, TOAST_MS),
      );
    };
    setToasts((prev) => {
      const active = prev.find((t) => t.key === key);
      if (active) {
        arm(active.id);
        return prev;
      }
      const id = nextToastId.current++;
      arm(id);
      return [...prev, { id, text, tone, key }];
    });
  }, []);

  // Nothing may fire into an unmounted provider.
  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const t of pending.values()) clearTimeout(t);
      pending.clear();
    };
  }, []);

  return (
    <ThemeContext.Provider value={{ theme, toggleTheme }}>
      <ToastContext.Provider value={{ toast }}>
        {children}
        {/* Top-center, ChatGPT-style (owner request 2026-08-05). Bottom-center
            hid them behind the composer and its attachment chips.

            Errors wear the SAME neutral surface as everything else since
            2026-09-03 (owner request): the red pill read as an alarm over a
            message like "re-attach the file", and the app's danger red is for
            destructive actions and inline failures, not for a passing note.
            What separates an error is now its marker and its semantics —
            `role="alert"` on the row, so a screen reader still hears it as
            one — rather than a colour. */}
        <div
          aria-live="polite"
          role="status"
          className="pointer-events-none fixed left-1/2 top-5 z-[70] flex w-full max-w-md -translate-x-1/2 flex-col items-center gap-2 px-4"
        >
          {toasts.map((t) => (
            <div
              key={t.id}
              role={t.tone === 'error' ? 'alert' : undefined}
              data-tone={t.tone}
              className="pointer-events-auto rounded-full border border-border bg-surface px-4 py-2 text-sm text-ink shadow-lg"
            >
              {t.tone === 'error' && (
                <span aria-hidden className="mr-2 font-semibold text-muted">
                  !
                </span>
              )}
              {t.text}
            </div>
          ))}
        </div>
      </ToastContext.Provider>
    </ThemeContext.Provider>
  );
}
