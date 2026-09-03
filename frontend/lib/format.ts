/** Small formatting helpers shared across the UI. */

export function formatBytes(bytes: number | undefined): string {
  if (bytes === undefined || Number.isNaN(bytes)) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 'B';
  for (const u of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = u;
  }
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}

export function formatWhen(input: number | string): string {
  const d =
    typeof input === 'number'
      ? new Date(input < 1e12 ? input * 1000 : input) // tolerate unix seconds
      : new Date(input);
  if (Number.isNaN(d.getTime())) return String(input);
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * "Sep 1, 2026" — the DATE, without the time.
 *
 * An admin roster shows two timestamps per row; at full precision they are
 * 40 characters of digits per person and the eye has nothing to land on.
 * The precise value stays available through the cell's `title`.
 */
export function formatDay(input: number | string): string {
  const d = typeof input === 'number' ? new Date(input < 1e12 ? input * 1000 : input) : new Date(input);
  if (Number.isNaN(d.getTime())) return String(input);
  return d.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

/**
 * "3 hours ago", "yesterday", "2 months ago" — for a column whose question
 * is "is this person still here?", which an absolute timestamp answers only
 * after the reader does the subtraction themselves. Falls back to the date
 * beyond a year, where "13 months ago" stops being the useful phrasing.
 */
export function formatRelative(input: number | string, now: number = Date.now()): string {
  const d = typeof input === 'number' ? new Date(input < 1e12 ? input * 1000 : input) : new Date(input);
  if (Number.isNaN(d.getTime())) return String(input);
  const seconds = Math.round((now - d.getTime()) / 1000);
  if (seconds < 45) return 'just now';
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ['minute', 60],
    ['hour', 3600],
    ['day', 86400],
    ['month', 2_592_000],
  ];
  for (const [unit, size] of units) {
    const next = unit === 'minute' ? 3600 : unit === 'hour' ? 86400 : unit === 'day' ? 2_592_000 : 31_536_000;
    if (Math.abs(seconds) < next) {
      return rtf.format(-Math.round(seconds / size), unit);
    }
  }
  return formatDay(input);
}

const FILE_KIND: Record<string, { label: string; className: string }> = {
  docx: { label: 'DOC', className: 'file-icon-docx' },
  doc: { label: 'DOC', className: 'file-icon-docx' },
  pdf: { label: 'PDF', className: 'file-icon-pdf' },
  xlsx: { label: 'XLS', className: 'file-icon-xlsx' },
  csv: { label: 'CSV', className: 'file-icon-csv' },
};

export function fileKind(nameOrType: string): {
  label: string;
  className: string;
} {
  const ext = nameOrType.toLowerCase().split('.').pop() ?? nameOrType;
  return FILE_KIND[ext] ?? { label: 'FILE', className: 'file-icon-other' };
}
