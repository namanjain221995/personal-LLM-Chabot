import { describe, expect, it } from 'vitest';
import {
  ZOOM_MAX,
  ZOOM_MIN,
  clampZoom,
  diagramFileName,
  fitZoom,
  isMermaidLanguage,
  looksRenderable,
  prepareSvgForExport,
  svgNaturalSize,
} from '../lib/mermaid';

describe('isMermaidLanguage', () => {
  it('matches mermaid fences (case-insensitive) and the mmd alias', () => {
    expect(isMermaidLanguage('mermaid')).toBe(true);
    expect(isMermaidLanguage('Mermaid')).toBe(true);
    expect(isMermaidLanguage('mmd')).toBe(true);
  });

  it('leaves other languages as code blocks', () => {
    expect(isMermaidLanguage('python')).toBe(false);
    expect(isMermaidLanguage('sql')).toBe(false);
    expect(isMermaidLanguage(undefined)).toBe(false);
    expect(isMermaidLanguage(null)).toBe(false);
  });
});

describe('looksRenderable (streaming guard)', () => {
  it('is false while the block is still arriving', () => {
    expect(looksRenderable('')).toBe(false);
    expect(looksRenderable('flowchart LR')).toBe(false); // header only
    expect(looksRenderable('  ')).toBe(false);
  });

  it('is true once a known diagram type has a body', () => {
    expect(looksRenderable('flowchart LR\n  A[Start] --> B[End]')).toBe(true);
    expect(looksRenderable('sequenceDiagram\n  A->>B: hi')).toBe(true);
    expect(looksRenderable('erDiagram\n  USER ||--o{ ORDER : places')).toBe(true);
    expect(looksRenderable('graph TD\n  A-->B')).toBe(true);
  });

  it('ignores %% comment lines when judging', () => {
    // comments are filtered, so the diagram header still counts as the head
    expect(looksRenderable('%% a note\nflowchart LR\n  A --> B')).toBe(true);
    expect(looksRenderable('flowchart LR\n%% note\n  A --> B')).toBe(true);
    // …but comments alone are not a diagram
    expect(looksRenderable('%% just a note\n%% another')).toBe(false);
  });

  it('is false for prose that is not a diagram', () => {
    expect(looksRenderable('hello world\nthis is not mermaid')).toBe(false);
  });
});

describe('diagramFileName', () => {
  it('slugs the first meaningful line', () => {
    expect(diagramFileName('flowchart LR\n A-->B')).toBe('flowchart-lr.png');
    expect(diagramFileName('sequenceDiagram\n A->>B: x', 'svg')).toBe(
      'sequencediagram.svg',
    );
  });

  it('falls back for empty input', () => {
    expect(diagramFileName('')).toBe('diagram.png');
  });
});

describe('clampZoom', () => {
  it('clamps to the supported range', () => {
    expect(clampZoom(0.1)).toBe(ZOOM_MIN);
    expect(clampZoom(99)).toBe(ZOOM_MAX);
    expect(clampZoom(1.5)).toBe(1.5);
    expect(clampZoom(Number.NaN)).toBe(1);
  });
});

describe('prepareSvgForExport', () => {
  const svg = '<svg width="100%" height="auto" viewBox="0 0 400 300"><g/></svg>';

  it('sets concrete pixel dimensions', () => {
    const out = prepareSvgForExport(svg, 400, 300, '#fff');
    const svgTag = out.slice(0, out.indexOf('>') + 1);
    expect(svgTag).toContain('width="400"');
    expect(svgTag).toContain('height="300"');
    expect(svgTag).not.toContain('width="100%"'); // % only on the bg rect
  });

  it('adds an xmlns when missing and a background rect', () => {
    const out = prepareSvgForExport('<svg><g/></svg>', 10, 10, '#123456');
    expect(out).toContain('xmlns="http://www.w3.org/2000/svg"');
    expect(out).toContain('fill="#123456"');
    // the rect must sit immediately after the opening tag, before content
    expect(out.indexOf('<rect')).toBeLessThan(out.indexOf('<g/>'));
  });
});

describe('svgNaturalSize', () => {
  it('reads the natural size from the viewBox', () => {
    expect(
      svgNaturalSize('<svg viewBox="0 0 800 600" width="100%"><g/></svg>'),
    ).toEqual({ width: 800, height: 600 });
  });

  it('is null without a usable viewBox', () => {
    expect(svgNaturalSize('<svg width="100%"><g/></svg>')).toBeNull();
    expect(svgNaturalSize('<svg viewBox="0 0 0 0"><g/></svg>')).toBeNull();
    expect(svgNaturalSize('')).toBeNull();
  });
});

describe('fitZoom', () => {
  it('shrinks a huge diagram to fit the viewport', () => {
    // 4000x3000 diagram in a 1200x800 viewport → limited by height (0.27)
    expect(fitZoom({ width: 4000, height: 3000 }, 1200, 800)).toBeCloseTo(
      0.27,
      2,
    );
  });

  it('scales a small diagram up, but never past 1.5x', () => {
    expect(fitZoom({ width: 200, height: 100 }, 1200, 800)).toBe(1.5);
  });

  it('never goes below the zoom floor and defaults to 1 without a size', () => {
    expect(fitZoom({ width: 100000, height: 100000 }, 1200, 800)).toBe(ZOOM_MIN);
    expect(fitZoom(null, 1200, 800)).toBe(1);
  });
});
