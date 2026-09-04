/**
 * Number and time formatting for the analytics console.
 *
 * Two rules run through all of it:
 *
 *  1. A value that was never measured is `null`, and null renders as an em
 *     dash — never as 0. "No requests reported tokens" and "requests used no
 *     tokens" are different facts, and a dashboard that renders them the same
 *     way is lying quietly.
 *  2. Big numbers are shown compact (1.2M) and carry their exact value in a
 *     `title`, so the headline stays readable and the real figure is one
 *     hover away.
 *
 * Pure module — no React, no DOM — so every rule here is unit-testable.
 */

/** The em dash every unmeasured value renders as. */
export const NOT_MEASURED = '—';

/** 1_234_567 → "1.2M". Two significant decimals below 10, one above. */
export function compact(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return NOT_MEASURED;
  const abs = Math.abs(value);
  if (abs < 1000) return String(Math.round(value));
  const units: [number, string][] = [
    [1e12, 'T'],
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'K'],
  ];
  for (const [size, suffix] of units) {
    if (abs >= size) {
      const scaled = value / size;
      // 9.4K reads better than 9K; 94K reads better than 94.2K.
      const digits = Math.abs(scaled) < 10 ? 1 : 0;
      return `${scaled.toFixed(digits)}${suffix}`;
    }
  }
  return String(Math.round(value));
}

/** The exact value, grouped — what `compact` puts in a tooltip. */
export function exact(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return NOT_MEASURED;
  return Math.round(value).toLocaleString();
}

/** Milliseconds as the shortest honest unit: 840ms, 6.8s, 2m 14s. */
export function duration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return NOT_MEASURED;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

/** Seconds in, the same output — for sources that report seconds. */
export function durationFromSeconds(s: number | null | undefined): string {
  return s == null ? NOT_MEASURED : duration(s * 1000);
}

/** 0.734 → "73.4%". Takes a RATIO; use `percentOf` for a raw percentage. */
export function ratio(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return NOT_MEASURED;
  return `${(value * 100).toFixed(digits)}%`;
}

/** 73.42 → "73.4%". Takes an already-scaled percentage. */
export function percent(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return NOT_MEASURED;
  return `${value.toFixed(digits)}%`;
}

/** Bytes as GiB/MiB — GPU memory and RAM are always reported in bytes. */
export function bytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return NOT_MEASURED;
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let n = value;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

/** Uptime in whole days and hours — "12d 4h". */
export function uptime(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return NOT_MEASURED;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds - days * 86400) / 3600);
  if (days > 0) return `${days}d ${hours}h`;
  const minutes = Math.floor((seconds - hours * 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

/** Hertz → "1.98 GHz". */
export function hertz(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return NOT_MEASURED;
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)} GHz`;
  if (value >= 1e6) return `${Math.round(value / 1e6)} MHz`;
  return `${Math.round(value)} Hz`;
}

/** Bytes per second → "4.2 MB/s". Network rates are decimal by convention. */
export function rate(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return NOT_MEASURED;
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
  let n = value;
  let i = 0;
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000;
    i += 1;
  }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

/**
 * A bucket label for an axis or a tooltip.
 *
 * Hourly windows need the hour; daily windows do not, and repeating "00:00"
 * under thirty ticks is noise. `long` spells the date out for tooltips, where
 * there is room and ambiguity costs more than width.
 */
export function bucketLabel(
  iso: string,
  bucket: 'hour' | 'day',
  long = false,
): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  if (bucket === 'hour') {
    return long
      ? d.toLocaleString(undefined, {
          month: 'short',
          day: 'numeric',
          hour: 'numeric',
          minute: '2-digit',
        })
      : d.toLocaleTimeString(undefined, { hour: 'numeric' });
  }
  return long
    ? d.toLocaleDateString(undefined, {
        weekday: 'short',
        month: 'short',
        day: 'numeric',
      })
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** "Updated 14:05" — the freshness stamp each section carries. */
export function updatedAt(date: Date): string {
  return `Updated ${date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
  })}`;
}

/** A person's initial for the avatar. Falls back to the email, then "?". */
export function initialOf(name: string, email = ''): string {
  const source = name.trim() || email.trim();
  return source ? source[0]!.toUpperCase() : '?';
}
