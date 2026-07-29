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
