'use client';

/**
 * Figure toolbar (charts design v2 §8.8): Zoom out · Zoom in · Reset zoom ·
 * Table · Download PNG. Real buttons with names; 32 px targets, 44 px and
 * 8 px apart on a coarse pointer (CSS). The ECharts Toolbox is not used: its
 * icons have no focus or label.
 *
 * Review fixes (2026-11-25):
 *   - A7: a zoomable chart gets single-tap Zoom in / Zoom out buttons, the
 *     alternative to dragging the slider's narrow handles (WCAG 2.5.7/2.5.8).
 *   - A1/A3: no button ever unmounts or becomes `disabled` while it may hold
 *     focus. Unavailable zoom steps, Reset at the first window and Download
 *     while saving use `aria-disabled` and ignore the click, so focus stays
 *     where the reader left it.
 *   - A11: no Table toggle when the table IS the chart (it cannot be closed).
 */

import { IconDownload, IconRefresh, IconTable, IconZoomIn, IconZoomOut } from '../icons';

export interface ToolbarZoom {
  canZoomIn: boolean;
  canZoomOut: boolean;
  /** The window differs from the one the chart opened on. */
  zoomed: boolean;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onReset: () => void;
}

function guarded(enabled: boolean, run: () => void) {
  return () => {
    if (enabled) run();
  };
}

export function ChartToolbar({
  tableOpen,
  tableId,
  onToggleTable,
  showTable = true,
  canExport,
  exporting,
  onExport,
  zoom,
}: {
  tableOpen: boolean;
  tableId: string;
  onToggleTable: () => void;
  /** False when the figure is a table already. */
  showTable?: boolean;
  canExport: boolean;
  exporting: boolean;
  onExport: () => void;
  /** Present only for a chart with a zoom window. */
  zoom?: ToolbarZoom | null;
}) {
  return (
    <div className="chart-toolbar flex shrink-0 flex-wrap items-center" role="toolbar" aria-label="Chart tools">
      {zoom && (
        <>
          <button
            type="button"
            className="chart-btn"
            aria-label="Zoom out"
            title="Zoom out"
            aria-disabled={!zoom.canZoomOut}
            onClick={guarded(zoom.canZoomOut, zoom.onZoomOut)}
          >
            <IconZoomOut size={14} />
          </button>
          <button
            type="button"
            className="chart-btn"
            aria-label="Zoom in"
            title="Zoom in"
            aria-disabled={!zoom.canZoomIn}
            onClick={guarded(zoom.canZoomIn, zoom.onZoomIn)}
          >
            <IconZoomIn size={14} />
          </button>
          <button
            type="button"
            className="chart-btn"
            title="Back to the first window"
            aria-disabled={!zoom.zoomed}
            onClick={guarded(zoom.zoomed, zoom.onReset)}
          >
            <IconRefresh size={14} />
            <span>Reset zoom</span>
          </button>
        </>
      )}
      {showTable && (
        <button
          type="button"
          className="chart-btn"
          aria-expanded={tableOpen}
          aria-controls={tableId}
          onClick={onToggleTable}
          title={tableOpen ? 'Hide the table' : 'Show the data as a table'}
        >
          <IconTable size={14} />
          <span>Table</span>
        </button>
      )}
      {canExport && (
        <button
          type="button"
          className="chart-btn"
          onClick={guarded(!exporting, onExport)}
          aria-disabled={exporting}
          aria-busy={exporting}
          aria-label="Download chart as PNG"
          title="Download PNG"
        >
          <IconDownload size={14} />
        </button>
      )}
    </div>
  );
}
