/**
 * Mermaid helpers (diagram rendering) — pure functions, no DOM/mermaid import,
 * so they are unit-testable and the heavy mermaid bundle stays lazy-loaded.
 */

/** Languages we render as a diagram rather than a code block. */
const MERMAID_LANGS = new Set(['mermaid', 'mmd']);

export function isMermaidLanguage(language?: string | null): boolean {
  return !!language && MERMAID_LANGS.has(language.toLowerCase());
}

/**
 * A cheap "does this look like a complete diagram yet?" check used while the
 * answer is still streaming: mermaid throws on half-written code, so we only
 * attempt a render once the first line names a known diagram type AND there is
 * at least one body line.
 */
const DIAGRAM_HEADS = [
  'flowchart', 'graph', 'sequencediagram', 'classdiagram', 'statediagram',
  'erdiagram', 'journey', 'gantt', 'pie', 'quadrantchart', 'requirementdiagram',
  'gitgraph', 'mindmap', 'timeline', 'sankey', 'xychart', 'block', 'packet',
  'architecture', 'kanban', 'radar', 'treemap', 'c4context',
];

export function looksRenderable(code: string): boolean {
  const lines = (code || '')
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('%%'));
  if (lines.length < 2) return false;
  const head = lines[0].toLowerCase().replace(/[\s-].*$/, '');
  return DIAGRAM_HEADS.some((d) => head.startsWith(d));
}

/** Slug used for the downloaded file name. */
export function diagramFileName(code: string, ext = 'png'): string {
  const first =
    (code || '')
      .split('\n')
      .map((l) => l.trim())
      .find((l) => l && !l.startsWith('%%')) ?? 'diagram';
  const slug =
    first
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 40) || 'diagram';
  return `${slug}.${ext}`;
}

/** Clamp a zoom factor to the supported range. Large architecture diagrams
 * need to shrink well below 25% to fit a laptop screen, hence the low floor. */
export const ZOOM_MIN = 0.1;
export const ZOOM_MAX = 4;

export function clampZoom(z: number): number {
  if (!Number.isFinite(z)) return 1;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(z * 100) / 100));
}

/** Natural pixel size of a mermaid SVG, read from its viewBox. */
export function svgNaturalSize(
  svg: string,
): { width: number; height: number } | null {
  const m = /viewBox="([\d.\s eE+-]+)"/.exec(svg || '');
  if (!m) return null;
  const parts = m[1].trim().split(/\s+/).map(Number);
  if (parts.length !== 4 || !(parts[2] > 0) || !(parts[3] > 0)) return null;
  return { width: parts[2], height: parts[3] };
}

/**
 * The zoom that makes the whole diagram visible in the given viewport
 * ("fit to screen" — what the fullscreen viewer opens at). Small diagrams may
 * scale UP, but never beyond 1.5× (text gets comically large past that).
 */
export function fitZoom(
  natural: { width: number; height: number } | null,
  viewportWidth: number,
  viewportHeight: number,
): number {
  if (!natural || viewportWidth <= 0 || viewportHeight <= 0) return 1;
  const fit = Math.min(
    viewportWidth / natural.width,
    viewportHeight / natural.height,
  );
  return clampZoom(Math.min(fit, 1.5));
}

/**
 * Prepare a mermaid-produced SVG string for rasterization: ensure an xmlns,
 * explicit pixel width/height (mermaid often emits `max-width` styles only),
 * and a solid background so the PNG isn't transparent.
 */
export function prepareSvgForExport(
  svg: string,
  width: number,
  height: number,
  background: string,
): string {
  let out = svg;
  if (!/xmlns=/.test(out)) {
    out = out.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"');
  }
  // Replace any existing width/height attributes with concrete pixels.
  out = out
    .replace(/\swidth="[^"]*"/, ` width="${Math.round(width)}"`)
    .replace(/\sheight="[^"]*"/, ` height="${Math.round(height)}"`);
  if (!/\swidth="/.test(out)) {
    out = out.replace(
      '<svg',
      `<svg width="${Math.round(width)}" height="${Math.round(height)}"`,
    );
  }
  // Inject a background rect right after the opening <svg ...> tag.
  const open = out.indexOf('>');
  if (open !== -1) {
    const rect = `<rect width="100%" height="100%" fill="${background}"/>`;
    out = out.slice(0, open + 1) + rect + out.slice(open + 1);
  }
  return out;
}
