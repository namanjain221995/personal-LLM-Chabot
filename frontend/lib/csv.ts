/** Client-side CSV building + download for the proof drawer Data section. */

import type { DataRow, Meta } from './types';
import { isNumeric } from './chartFormat';

/**
 * CSV formula-injection guard (charts design v2 §8.6, C18; owner question 7).
 *
 * A text cell that starts with `=`, `+`, `-`, `@`, TAB or CR is run as a
 * formula when the file is opened in Excel or Sheets. Salesforce values are
 * user-controlled text, and chart labels converted from Mermaid are written
 * by the model (which in assistant mode may have copied them from a web
 * page), so such cells get a leading `'`, which both programs treat as "this
 * is text". A real number (`-5`, `-5.2`, `1e3`) is left alone: `isNumeric` is
 * the app's one numeric predicate.
 */
const FORMULA_LEAD = /^[=+\-@\t\r]/;
/**
 * Review R11: number-like text — a sign, an optional currency symbol, digits
 * with grouping or decimal separators, an optional % — is left as it is
 * (`-1,000`, `-12.5%`, `-$5`, `+1 555 0100`, a lone `-` placeholder). With no
 * letters, no `(` and no `!` it cannot name a function or a cell, so it cannot
 * run anything; a quote would only make the cell stop being a number.
 */
const NUMBER_LIKE = /^(?:[+-]\s?[$€£¥₹]?\s?\d[\d,.\s]*%?|-)$/;

export function neutraliseFormula(s: string): string {
  if (!FORMULA_LEAD.test(s)) return s;
  if (isNumeric(s)) return s;
  if (NUMBER_LIKE.test(s)) return s;
  return `'${s}`;
}

function escapeCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  const raw =
    typeof value === 'object' ? JSON.stringify(value) : String(value);
  const s = typeof value === 'number' ? raw : neutraliseFormula(raw);
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
 * A live pull is stamped with its own `query_timestamp`, so the file says WHICH
 * pull it came from. A synced answer's timestamp is when the snapshot was
 * published, which many questions share, so that one is stamped when it is
 * rendered: two questions about the same object in one chat still land in two
 * different files. Falls back through route to the old name, so a payload
 * carrying none of this still downloads.
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

  const sources = meta.salesforce_sources;
  const when = new Date(
    sources?.source === 'live' ? (sources.query_timestamp ?? Date.now()) : Date.now(),
  );
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
