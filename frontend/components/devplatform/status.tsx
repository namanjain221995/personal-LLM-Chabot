'use client';

/**
 * The console's one live region.
 *
 * Everything on this page happens somewhere else and arrives later: a table
 * fills in, a key is minted, a stream ends. A sighted reader sees all of it;
 * without a live region a screen reader reader is told nothing at all, because
 * a table quietly swapping its skeleton for rows is not an announcement.
 *
 * ONE region, at the shell, rather than one per panel. Several polite live
 * regions on a page queue against each other and read out in an order nobody
 * designed; a single region that the panels write into says one true sentence
 * at a time. It is visually hidden — the same information is on screen in the
 * table, the toast and the empty state, so repeating it in ink would be
 * duplication rather than help.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

interface ConsoleStatusValue {
  /** Say something once. An identical repeat is dropped, not re-read. */
  announce: (message: string) => void;
}

const ConsoleStatusContext = createContext<ConsoleStatusValue>({
  announce: () => undefined,
});

export function useConsoleStatus(): ConsoleStatusValue {
  return useContext(ConsoleStatusContext);
}

export function ConsoleStatusProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState('');
  const announce = useCallback((next: string) => {
    setMessage((prev) => (prev === next ? prev : next));
  }, []);
  const value = useMemo(() => ({ announce }), [announce]);
  return (
    <ConsoleStatusContext.Provider value={value}>
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-testid="console-status"
        className="sr-only"
      >
        {message}
      </p>
      {children}
    </ConsoleStatusContext.Provider>
  );
}
