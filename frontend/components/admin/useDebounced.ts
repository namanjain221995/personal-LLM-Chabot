'use client';

/** A value that settles after `ms` of quiet — the search boxes' debounce. */

import { useEffect, useState } from 'react';

export function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}
