/**
 * Mermaid number charts → ChartModel (charts design v2 §10).
 *
 * A model that answers with ```mermaid `xychart-beta` or `pie` gets Mermaid's
 * own renderer today, which overlaps long category labels into an unreadable
 * band (the owner's complaint, 2026-11-25). This strict parser turns those two
 * diagram types into the same ChartModel a `meta.chart` produces, so they draw
 * as real, interactive charts with full labels.
 *
 * STRICT BY DESIGN: anything this grammar does not know returns null, and the
 * block takes the existing Mermaid path unchanged. The grammar follows the
 * live blocks (bare items with spaces, untitled x-axis, quoted y title with a
 * range, unnamed series, `pie title …` on the head line) and is lenient only
 * where that cannot change a number (curly quotes, a trailing `%%` comment, a
 * list continued on the next line, `area` series).
 *
 * SECURITY: the parser yields only strings and numbers. Nothing in a block
 * becomes an ECharts option; labels reach the canvas as text (pre-broken, no
 * `rich`), the HTML as React text nodes, the tooltip through `escapeHtml`, and
 * the CSV through the formula guard (lib/csv.ts).
 *
 * Pure module: no DOM, no mermaid import.
 */

import type { DataRow } from './types';
import { withoutPreamble } from './mermaid';
import {
  type ChartModel,
  type ModelSeries,
  type SeriesKind,
  computeFlags,
  detectTime,
  timeBreaks,
} from './chartModel';
import { formatDateByGrain } from './chartFormat';

export const MERMAID_CHART_LIMITS = {
  maxSeries: 8,
  maxPoints: 500,
  maxLabel: 300,
  maxSource: 20_000,
} as const;

const NUMBER = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/;

/** Curly double quotes count as straight ones (lenient: mermaid itself would fail). */
function straightQuotes(s: string): string {
  return s.replace(/[“”„‟]/g, '"');
}

/** Drop a trailing `%% …` comment that sits outside double quotes. */
function stripComment(line: string): string {
  let inQuote = false;
  for (let i = 0; i < line.length - 1; i++) {
    const ch = line[i];
    if (ch === '"') inQuote = !inQuote;
    else if (!inQuote && ch === '%' && line[i + 1] === '%') return line.slice(0, i);
  }
  return line;
}

/**
 * Open `[` count outside quotes (to continue a list onto the next line),
 * carried line by line: review R8 — re-scanning the whole pending statement
 * for every continued line was quadratic.
 */
interface BracketState {
  depth: number;
  inQuote: boolean;
}
function scanBrackets(s: string, st: BracketState): void {
  for (const ch of s) {
    if (ch === '"') st.inQuote = !st.inQuote;
    else if (!st.inQuote && ch === '[') st.depth += 1;
    else if (!st.inQuote && ch === ']') st.depth -= 1;
  }
}

const NUMBER_CHAR = /[0-9.eE+-]/;
const MAX_NUMBER_CHARS = 40;
const RANGE_NUMBER = /^-?[\d.]+(?:[eE][+-]?\d+)?$/;

/**
 * The number at the very end of `s` (before trailing spaces), found by
 * scanning backwards over at most 40 number characters — linear, unlike the
 * lazy `^(.*?)(number)` regex it replaces (review R8). Null when there is
 * none or it is longer than 40 characters.
 */
function trailingNumber(s: string): { before: string; num: string } | null {
  let end = s.length;
  while (end > 0 && /\s/.test(s[end - 1])) end -= 1;
  let start = end;
  while (start > 0 && end - start < MAX_NUMBER_CHARS && NUMBER_CHAR.test(s[start - 1])) start -= 1;
  if (start > 0 && end - start >= MAX_NUMBER_CHARS && NUMBER_CHAR.test(s[start - 1])) return null;
  for (let i = start; i < end; i++) {
    const tok = s.slice(i, end);
    if (RANGE_NUMBER.test(tok)) return { before: s.slice(0, i), num: tok };
  }
  return null;
}

/** `<before> lo --> hi` (and, when `allowTo`, `lo to hi`), parsed from the end. */
function trailingRange(
  rest: string,
  allowTo: boolean,
): { before: string; lo: string; hi: string } | null {
  const hi = trailingNumber(rest);
  if (!hi) return null;
  let left = hi.before.replace(/\s+$/, '');
  if (left.endsWith('-->')) left = left.slice(0, -3);
  else if (allowTo && /to$/i.test(left)) left = left.slice(0, -2);
  else return null;
  const lo = trailingNumber(left);
  if (!lo) return null;
  return { before: lo.before.replace(/\s+$/, ''), lo: lo.num, hi: hi.num };
}

/** Statements, one per logical line, with comments, blanks and continuations handled. */
function statements(code: string): string[] | null {
  const lines = straightQuotes(withoutPreamble(code)).split(/\r?\n/);
  const out: string[] = [];
  let pending = '';
  const brackets: BracketState = { depth: 0, inQuote: false };
  let inAccBlock = false;
  let acc = '';
  for (const rawLine of lines) {
    const trimmed = rawLine.trim();
    if (inAccBlock) {
      const close = trimmed.indexOf('}');
      if (close >= 0) {
        acc += ` ${trimmed.slice(0, close)}`;
        out.push(`accDescr: ${acc.trim()}`);
        inAccBlock = false;
        acc = '';
      } else {
        acc += ` ${trimmed}`;
      }
      continue;
    }
    if (!trimmed || trimmed.startsWith('%%')) continue;
    const line = stripComment(trimmed).trim();
    if (!line) continue;
    const block = /^accDescr\s*\{(.*)$/.exec(line);
    if (block && !pending) {
      const rest = block[1];
      const close = rest.indexOf('}');
      if (close >= 0) out.push(`accDescr: ${rest.slice(0, close).trim()}`);
      else {
        inAccBlock = true;
        acc = rest;
      }
      continue;
    }
    pending = pending ? `${pending} ${line}` : line;
    scanBrackets(line, brackets);
    if (brackets.depth > 0) continue;
    out.push(pending);
    pending = '';
    brackets.depth = 0;
    brackets.inQuote = false;
  }
  if (pending || inAccBlock) return null; // a list (or block) still open at the end
  return out;
}

/** The first statement names a number chart (used while streaming, design §10 C10). */
export function isChartHead(code: string): boolean {
  // Review R14: called on every streaming render, so only the head of the
  // block is read (a chart's first statement is in its first lines).
  const head = straightQuotes(withoutPreamble(code.slice(0, HEAD_SCAN)));
  let from = 0;
  while (from <= head.length) {
    const nl = head.indexOf('\n', from);
    const line = head.slice(from, nl < 0 ? head.length : nl).trim();
    if (line && !line.startsWith('%%')) return /^(xychart(-beta)?|pie)\b/i.test(line);
    if (nl < 0) break;
    from = nl + 1;
  }
  return false;
}
const HEAD_SCAN = 4096;

/** `"quoted"` → quoted; bare → trimmed. Returns null for an unterminated quote. */
function unquote(s: string): string | null {
  const t = s.trim();
  if (t.startsWith('"')) {
    if (t.length < 2 || !t.endsWith('"')) return null;
    return t.slice(1, -1);
  }
  return t;
}

/** Items of a `[a, "b, c", d]` list: commas outside double quotes split. */
function listItems(inner: string): string[] | null {
  const items: string[] = [];
  let cur = '';
  let inQuote = false;
  for (const ch of inner) {
    if (ch === '"') {
      inQuote = !inQuote;
      cur += ch;
    } else if (ch === ',' && !inQuote) {
      items.push(cur);
      cur = '';
    } else cur += ch;
  }
  if (inQuote) return null;
  items.push(cur);
  const out: string[] = [];
  for (const raw of items) {
    const v = unquote(raw);
    if (v === null) return null;
    out.push(v);
  }
  if (out.length === 1 && out[0] === '') return [];
  if (out.some((v) => v === '')) return null;
  return out;
}

/** `[ … ]` at the end of `rest`: the text before it and the items. */
function trailingList(rest: string): { before: string; items: string[] } | null {
  const open = rest.indexOf('[');
  if (open < 0 || !rest.trimEnd().endsWith(']')) return null;
  const inner = rest.trimEnd().slice(open + 1, -1);
  const items = listItems(inner);
  if (!items) return null;
  return { before: rest.slice(0, open).trim(), items };
}

function numbers(items: string[]): number[] | null {
  const out: number[] = [];
  for (const it of items) {
    if (!NUMBER.test(it.trim())) return null;
    const n = Number(it.trim());
    if (!Number.isFinite(n)) return null;
    out.push(n);
  }
  return out;
}

/** An optional title before a list or range: `"Quoted words"` or one bare word. */
function optionalTitle(s: string): string | null | undefined {
  const t = s.trim();
  if (!t) return undefined;
  if (t.startsWith('"')) return unquote(t);
  return /^\S+$/.test(t) ? t : null;
}

interface ParsedSeries {
  kind: SeriesKind;
  name?: string;
  values: number[];
}

export interface MermaidChartResult {
  model: ChartModel;
}

/**
 * Parse a Mermaid `xychart-beta` / `xychart` / `pie` source into a ChartModel,
 * or null when it is anything else, malformed, or outside the limits.
 */
export function parseMermaidChart(code: string): MermaidChartResult | null {
  if (typeof code !== 'string' || code.length > MERMAID_CHART_LIMITS.maxSource) return null;
  const stmts = statements(code);
  if (!stmts || stmts.length < 2) return null;
  const head = stmts[0];
  try {
    if (/^xychart(-beta)?\b/i.test(head)) return parseXy(head, stmts.slice(1));
    if (/^pie\b/i.test(head)) return parsePie(head, stmts.slice(1));
  } catch {
    return null;
  }
  return null;
}

function tooLong(s: string | undefined): boolean {
  return !!s && s.length > MERMAID_CHART_LIMITS.maxLabel;
}

function parseXy(head: string, body: string[]): MermaidChartResult | null {
  const hm = /^xychart(?:-beta)?(?:\s+(horizontal|vertical))?$/i.exec(head);
  if (!hm) return null;
  const horizontal = (hm[1] ?? '').toLowerCase() === 'horizontal';
  let title = '';
  let xTitle: string | undefined;
  let yTitle: string | undefined;
  let cats: string[] | null = null;
  let range: [number, number] | null = null;
  const series: ParsedSeries[] = [];
  const a11y: { title?: string; description?: string } = {};

  for (const st of body) {
    let m: RegExpExecArray | null;
    if ((m = /^title\s+(.+)$/i.exec(st))) {
      const t = unquote(m[1]);
      if (t === null) return null;
      title = t;
    } else if ((m = /^x-axis\b(.*)$/i.exec(st))) {
      const rest = m[1].trim();
      const list = trailingList(rest);
      if (list) {
        const t = optionalTitle(list.before);
        if (t === null) return null;
        xTitle = t;
        cats = list.items;
      } else {
        const rm = trailingRange(rest, false);
        if (!rm) return null;
        const t = optionalTitle(rm.before);
        if (t === null) return null;
        xTitle = t;
        const lo = Number(rm.lo);
        const hi = Number(rm.hi);
        if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
        range = [lo, hi];
      }
    } else if ((m = /^y-axis\b(.*)$/i.exec(st))) {
      const rest = m[1].trim();
      // A range is accepted and ignored: our value axis never clips data.
      // Leniently, `0 to 100` too (2 of the 4 live blocks wrote it that way;
      // mermaid itself refuses it). Ignored either way, so it cannot change
      // a number.
      const rm = trailingRange(rest, true);
      const t = optionalTitle(rm ? rm.before : rest);
      if (t === null) return null;
      yTitle = t;
    } else if ((m = /^(bar|line|area)\b(.*)$/i.exec(st))) {
      const list = trailingList(m[2]);
      if (!list) return null;
      const nm = optionalTitle(list.before);
      if (nm === null) return null;
      const vals = numbers(list.items);
      if (!vals || vals.length === 0) return null;
      series.push({ kind: m[1].toLowerCase() as SeriesKind, name: nm, values: vals });
    } else if ((m = /^accTitle\s*:\s*(.*)$/i.exec(st))) {
      a11y.title = m[1].trim();
    } else if ((m = /^accDescr\s*:\s*(.*)$/i.exec(st))) {
      a11y.description = m[1].trim();
    } else {
      return null; // unknown statement
    }
  }

  if (series.length === 0 || series.length > MERMAID_CHART_LIMITS.maxSeries) return null;
  const n = series[0].values.length;
  if (series.some((s) => s.values.length !== n)) return null;
  if (n > MERMAID_CHART_LIMITS.maxPoints) return null;
  if (!cats) {
    if (!range) return null;
    const [lo, hi] = range;
    cats = Array.from({ length: n }, (_, i) => {
      const v = n === 1 ? lo : lo + ((hi - lo) * i) / (n - 1);
      return String(Number(v.toPrecision(6)));
    });
  }
  if (cats.length !== n) return null;
  if ([title, xTitle, yTitle, ...cats, ...series.map((s) => s.name)].some((s) => tooLong(s))) return null;

  const kinds = new Set(series.map((s) => s.kind));
  const anyBar = kinds.has('bar');
  const anyLineish = kinds.has('line') || kinds.has('area');
  const requested: ChartModel['requested'] =
    anyBar && anyLineish
      ? 'combo'
      : anyBar
        ? horizontal
          ? 'horizontal_bar'
          : 'bar'
        : kinds.has('line')
          ? 'line'
          : 'area';

  // Series names: "Series 1, 2…" when unnamed; a lone unnamed series is named
  // by the y-axis title (it has no legend; the chart title names it).
  const used = new Map<string, number>();
  const modelSeries: ModelSeries[] = series.map((s, i) => {
    let label = s.name ?? (series.length === 1 ? yTitle || 'Value' : `Series ${i + 1}`);
    const seen = used.get(label) ?? 0;
    used.set(label, seen + 1);
    if (seen > 0) label = `${label} (${seen + 1})`;
    return {
      id: `s${i}`,
      key: label,
      label,
      kind: s.kind,
      slot: i,
      values: s.values,
    };
  });

  // Dates as x on a line or bar chart get a real time axis, as for specs.
  const time = requested === 'combo' ? null : detectTime(cats);
  let catList = cats.map((label, i) => ({ id: `c${i}`, label, present: true as boolean, t: undefined as number | undefined }));
  let order = cats.map((_, i) => i);
  if (time) {
    order.sort((a, b) => time.values[a]!.t - time.values[b]!.t || a - b);
    catList = order.map((src, i) => ({
      id: `c${i}`,
      label: formatDateByGrain(time.values[src]!.t, time.grain, time.values[src]!.zone),
      t: time.values[src]!.t,
      present: true,
    }));
  } else {
    order = cats.map((_, i) => i);
  }
  const orderedSeries = modelSeries.map((s) => ({ ...s, values: order.map((i) => s.values[i]) }));

  // Review R9: Table/CSV column names are unique (a series named like the x
  // title, or two of them, would overwrite a column) and rows are built from
  // own entries, so a column called "__proto__" is a column, not a prototype.
  const xKey = xTitle || 'Category';
  const columns = uniqueNames([xKey, ...modelSeries.map((s) => s.label)]);
  const tableRows: DataRow[] = cats.map(
    (label, i) =>
      Object.fromEntries([
        [columns[0], label],
        ...modelSeries.map((s, k) => [columns[k + 1], s.values[i]] as const),
      ]) as DataRow,
  );

  const model: ChartModel = {
    origin: 'mermaid',
    requested,
    title,
    x: {
      key: xKey,
      label: xTitle || 'Category',
      kind: time ? 'time' : 'category',
      grain: time?.grain,
      cats: catList,
      nullRows: 0,
      breaks:
        time && (requested === 'line' || requested === 'area')
          ? timeBreaks(
              catList.map((c) => c.t ?? 0),
              time.grain,
            )
          : [],
    },
    series: orderedSeries,
    stacked: false,
    showValues: false,
    hideLegend: false,
    valueFlags: computeFlags(orderedSeries),
    axisCaptions: xTitle || yTitle ? { x: xTitle, y: yTitle } : undefined,
    a11y: a11y.title || a11y.description ? a11y : undefined,
    notes: [],
    tableRows,
  };
  return { model };
}

function uniqueNames(names: string[]): string[] {
  const seen = new Set<string>();
  return names.map((name) => {
    let out = name;
    for (let k = 2; seen.has(out); k++) out = `${name} (${k})`;
    seen.add(out);
    return out;
  });
}

function parsePie(head: string, body: string[]): MermaidChartResult | null {
  const hm = /^pie(?:\s+showData)?(?:\s+title\s+(.+))?$/i.exec(head);
  if (!hm) return null;
  let title = hm[1] ? (unquote(hm[1]) ?? '') : '';
  const a11y: { title?: string; description?: string } = {};
  const slices: Array<{ label: string; value: number }> = [];
  for (const st of body) {
    let m: RegExpExecArray | null;
    if ((m = /^title\s+(.+)$/i.exec(st))) {
      const t = unquote(m[1]);
      if (t === null) return null;
      title = t;
    } else if ((m = /^showData$/i.exec(st))) {
      // A display switch for mermaid's own legend; our legend always shows values.
    } else if ((m = /^"([^"]*)"\s*:\s*(\S+)$/.exec(st))) {
      if (!NUMBER.test(m[2])) return null;
      const v = Number(m[2]);
      if (!Number.isFinite(v) || v < 0) return null;
      slices.push({ label: m[1], value: v });
    } else if ((m = /^accTitle\s*:\s*(.*)$/i.exec(st))) {
      a11y.title = m[1].trim();
    } else if ((m = /^accDescr\s*:\s*(.*)$/i.exec(st))) {
      a11y.description = m[1].trim();
    } else {
      return null;
    }
  }
  if (slices.length === 0 || slices.length > MERMAID_CHART_LIMITS.maxPoints) return null;
  // Mermaid keys slices by label; with duplicates we would have to guess which
  // one it keeps, so the block stays with mermaid.
  if (new Set(slices.map((s) => s.label)).size !== slices.length) return null;
  if ([title, ...slices.map((s) => s.label)].some((s) => tooLong(s))) return null;
  const total = slices.reduce((a, s) => a + s.value, 0);
  if (!(total > 0)) return null;

  const sorted = slices
    .map((s, i) => ({ ...s, i }))
    .sort((a, b) => b.value - a.value || a.i - b.i);
  const series: ModelSeries[] = [
    {
      id: 's0',
      key: 'Value',
      label: 'Value',
      kind: 'bar',
      slot: 0,
      values: sorted.map((s) => s.value),
    },
  ];
  const flags = computeFlags(series);
  const percentWhole = Math.abs(total - 100) <= 0.5;
  const model: ChartModel = {
    origin: 'mermaid',
    requested: 'pie',
    title,
    x: {
      key: 'Label',
      label: 'Label',
      kind: 'category',
      cats: sorted.map((s, i) => ({ id: `c${i}`, label: s.label, present: true })),
      nullRows: 0,
      breaks: [],
    },
    series,
    stacked: false,
    showValues: false,
    hideLegend: false,
    valueFlags: { ...flags, percentWhole },
    a11y: a11y.title || a11y.description ? a11y : undefined,
    notes: [],
    tableRows: slices.map((s) => ({ Label: s.label, Value: s.value })),
  };
  return { model };
}
