'use client';

/**
 * The proof drawer — the signature element (§9).
 * A slim bar under any assistant message with meta: collapsible sections
 * (View SQL / Sources / Data / Chart / Files).
 * The chart section auto-opens when present — the one orchestrated moment.
 */

import { useState } from 'react';
import type { Meta } from '@/lib/types';
import { csvFilenameFor } from '@/lib/csv';
import { SqlBlock } from './SqlBlock';
import { DataTable } from './DataTable';
import { ChartView } from './ChartView';
import { CitationChips } from './CitationChips';
import { CodeCitations } from './CodeCitations';
import { FileCards } from './FileCards';
import { IconChevronDown } from './icons';

type SectionId =
  | 'sql'
  | 'sources'
  | 'code'
  | 'data'
  | 'chart'
  | 'files';

interface Section {
  id: SectionId;
  label: string;
}

export function ProofDrawer({ meta }: { meta: Meta }) {
  const sections: Section[] = [];
  if (meta.sql) sections.push({ id: 'sql', label: 'View SQL' });
  if (meta.citations?.length) {
    sections.push({ id: 'sources', label: `Sources (${meta.citations.length})` });
  }
  // Web-search sources (meta.sources) are deliberately NOT a section here
  // (owner request 2026-08-05): they live in the right-side ActivityPanel
  // behind the action row's "Sources" book button, ChatGPT-style. This box
  // is the Salesforce proof trail only.
  if (meta.code_sources?.length) {
    sections.push({ id: 'code', label: `Code (${meta.code_sources.length})` });
  }
  if (meta.data?.length) {
    sections.push({
      id: 'data',
      label: `Data (${meta.data.length}${meta.truncated ? '+' : ''})`,
    });
  }
  // `chart_data` carries rows the chart draws that are not `data`
  // verbatim (histogram bins; a funnel in trusted stage order). Old
  // payloads have no such key and fall back to `data`, which is exactly
  // what they always rendered.
  const chartRows = meta.chart_data?.length ? meta.chart_data : meta.data;
  if (meta.chart && chartRows?.length) {
    sections.push({ id: 'chart', label: 'Chart' });
  }
  if (meta.report_files?.length) {
    sections.push({ id: 'files', label: `Files (${meta.report_files.length})` });
  }

  // The full-result CSV, when the engine wrote one. The Data section's rows
  // are a preview; this is every row, already on disk and served by
  // /api/reports. Only CSV — an .xlsx cannot stand in for a CSV download.
  const fullCsvFile = meta.report_files?.find(
    (f) => f.filename.toLowerCase().endsWith('.csv'),
  );
  const fullCsv = fullCsvFile
    ? {
        href: `/api/reports/${encodeURIComponent(fullCsvFile.filename)}`,
        rows: meta.export_rows,
      }
    : undefined;

  const [open, setOpen] = useState<Set<SectionId>>(
    () =>
      new Set<SectionId>(
        meta.chart && chartRows?.length
          ? ['chart']
          : meta.report_files?.length
            ? ['files']
            : [],
      ),
  );

  // No sections → no box at all (owner request 2026-08-05). A bar holding
  // nothing but the engine badge — every plain chat answer — was an empty
  // frame under every message; the badge is only worth a box when there is
  // something to prove next to it.
  if (sections.length === 0) return null;

  function toggle(id: SectionId) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="mt-3 rounded-ts border border-border bg-surface/60">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        {/* The engine badge that used to open this bar is gone (owner
            request 2026-09-03) — see components/ChatApp.tsx for the same
            removal in the header. Its divider went with it: a rule whose only
            job was separating the badge from these buttons would now be a
            stray line before the first one. `meta.route` still decides
            everything it ever decided; it simply is not announced. */}
        {sections.map((s) => {
          const isOpen = open.has(s.id);
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => toggle(s.id)}
              aria-expanded={isOpen}
              aria-controls={`drawer-${s.id}`}
              className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors duration-ts ${
                isOpen
                  ? 'bg-accent/15 text-accent'
                  : 'text-muted hover:bg-surface-2 hover:text-ink'
              }`}
            >
              {s.label}
              <IconChevronDown
                size={12}
                className={`transition-transform duration-ts ${
                  isOpen ? 'rotate-180' : ''
                }`}
              />
            </button>
          );
        })}
      </div>

      {sections
        .filter((s) => open.has(s.id))
        .map((s) => (
          <div
            key={s.id}
            id={`drawer-${s.id}`}
            className="drawer-panel border-t border-border px-3 py-3"
          >
            {s.id === 'sql' && meta.sql && <SqlBlock sql={meta.sql} />}
            {s.id === 'sources' && meta.citations && (
              <CitationChips citations={meta.citations} />
            )}
            {s.id === 'code' && meta.code_sources && (
              <CodeCitations sources={meta.code_sources} />
            )}
            {s.id === 'data' && meta.data && (
              <DataTable
                rows={meta.data}
                truncated={meta.truncated}
                // How many records MATCHED, which is not always how many came
                // back — so a truncated table can say "2,000 of 28,230".
                totalRows={meta.salesforce_sources?.record_count}
                // Derived from the data (object + query time), so a folder
                // of downloads is no longer N copies of techsara-data.csv.
                csvName={csvFilenameFor(meta)}
                // Present only when the orchestrator wrote the complete
                // result to disk; then the button hands over THAT, not the
                // preview these rows are.
                fullCsvHref={fullCsv?.href}
                fullCsvRows={fullCsv?.rows}
              />
            )}
            {s.id === 'chart' && meta.chart && chartRows && (
              <ChartView spec={meta.chart} data={chartRows} />
            )}
            {s.id === 'files' && meta.report_files && (
              <FileCards files={meta.report_files} />
            )}
          </div>
        ))}
    </div>
  );
}
