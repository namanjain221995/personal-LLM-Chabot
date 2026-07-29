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

  const toast = useCallback((text: string, tone: 'info' | 'error' = 'info') => {
    const id = nextToastId.current++;
    setToasts((prev) => [...prev, { id, text, tone }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 5200);
  }, []);

  return (
    <ThemeContext.Provider value={{ theme, toggleTheme }}>
      <ToastContext.Provider value={{ toast }}>
        {children}
        <div
          aria-live="polite"
          role="status"
          className="pointer-events-none fixed bottom-5 left-1/2 z-[70] flex w-full max-w-md -translate-x-1/2 flex-col items-center gap-2 px-4"
        >
          {toasts.map((t) => (
            <div
              key={t.id}
              className={`pointer-events-auto w-full rounded-ts border px-4 py-2.5 text-sm shadow-lg ${
                t.tone === 'error'
                  ? 'border-danger/40 bg-surface text-ink'
                  : 'border-border bg-surface text-ink'
              }`}
            >
              {t.tone === 'error' && (
                <span className="mr-2 font-semibold text-danger">!</span>
              )}
              {t.text}
            </div>
          ))}
        </div>
      </ToastContext.Provider>
    </ThemeContext.Provider>
  );
}
