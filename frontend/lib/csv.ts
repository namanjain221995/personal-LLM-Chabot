/** Client-side CSV building + download for the proof drawer Data section. */

import type { DataRow, Meta } from './types';

function escapeCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  const s =
    typeof value === 'object' ? JSON.stringify(value) : String(value);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function rowsToCsv(rows: DataRow[]): string {
  if (rows.length === 0) return '';
  const columns = Array.from(
    rows.reduce<Set<string>>((set, row) => {
      Object.keys(row).forEach((k) => set.add(k));
      return set;
    }, new Set()),
  );
  const lines = [columns.map(escapeCell).join(',')];
  for (const row of rows) {
    lines.push(columns.map((c) => escapeCell(row[c])).join(','));
  }
  return lines.join('\r\n') + '\r\n';
}

export function downloadCsv(rows: DataRow[], filename: string): void {
  const blob = new Blob([rowsToCsv(rows)], {
    type: 'text/csv;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename.endsWith('.csv') ? filename : `${filename}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Give the browser a tick to start the download before dropping the blob.
  // Revoking synchronously here raced the download in Safari and Firefox,
  // which resolve the object URL after the click's task returns — the same
  // reason exportMarkdown and MermaidBlock defer theirs.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * A download name that describes the ANSWER, not the app.
 *
 * Every Data-section download used to be `techsara-data.csv`, so a folder of
 * them was indistinguishable (owner report 2026-08-31) and each new one
 * overwrote — or " (1)"-suffixed — the last. The name now comes from the data
 * itself: the Salesforce object queried, then the moment it was queried.
 *
 *   Interview__c, fetched 14:32   ->  interview-2026-08-31-1432.csv
 *
 * The timestamp is the query's own `query_timestamp`, so two questions about
 * the same object in one chat still land in two different files, and the file
 * says WHICH pull it came from. Falls back through route to the old name, so a
 * payload carrying none of this still downloads.
 */
export function csvFilenameFor(meta: Meta, fallback = 'techsara-data'): string {
  const objects = meta.salesforce_sources?.objects ?? [];
  const base =
    objects
      .slice(0, 2)
      .map((o) => slugifyName(o))
      .filter(Boolean)
      .join('-') ||
    slugifyName(meta.route ?? '') ||
    fallback;

  const when = new Date(meta.salesforce_sources?.query_timestamp ?? Date.now());
  const stamp = Number.isNaN(when.getTime()) ? null : when;
  if (!stamp) return `${base}.csv`;

  const pad = (n: number) => String(n).padStart(2, '0');
  const date = `${stamp.getFullYear()}-${pad(stamp.getMonth() + 1)}-${pad(stamp.getDate())}`;
  const time = `${pad(stamp.getHours())}${pad(stamp.getMinutes())}`;
  return `${base}-${date}-${time}.csv`;
}

/** Salesforce API names to file-safe words: `Interview__c` -> `interview`. */
function slugifyName(name: string): string {
  return name
    .replace(/__c$/i, '')
    .replace(/[_\s]+/g, '-')
    .replace(/[^A-Za-z0-9-]/g, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .toLowerCase();
}
