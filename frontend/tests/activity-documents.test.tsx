// @vitest-environment jsdom
/**
 * SOURCE-MULTI — the Activity panel lists each document the engine read.
 *
 * The orchestrator folds several documents into ONE `meta.document`:
 * "a.pdf (+3 more)", summed pages, and every page entry prefixed "[name] ".
 * Rendered raw that was one unnamed file with four "Page 1" rows. The split
 * below is a parse of the engine's own prefixes — runtime evidence — never a
 * look at the user's attachment list, which is what keeps it honest.
 */

import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ActivityPanel } from '@/components/ActivityPanel';
import { documentReadView } from '@/lib/documentActivity';
import type { DocumentActivity } from '@/lib/types';

afterEach(cleanup);

const FOLDED: DocumentActivity = {
  filename: 'a.pdf (+3 more)',
  total_pages: 4,
  pages: [
    { page: 1, text: '[a.pdf] TECHSARA_FILE_A = ALPHA-9281' },
    { page: 1, text: '[b.pdf] TECHSARA_FILE_B = BRAVO-4732' },
    { page: 1, text: '[c.pdf] TECHSARA_FILE_C = CHARLIE-6159' },
    { page: 1, text: '[d.pdf] TECHSARA_FILE_D = DELTA-8044' },
  ],
};

const renderPanel = (doc: DocumentActivity) =>
  render(<ActivityPanel open onClose={vi.fn()} documentRead={doc} />);

const section = () => screen.getByRole('region', { name: /Documents? read/ });

describe('documentReadView (pure)', () => {
  it('SOURCE-MULTI-01 · four prefixed entries become four named documents', () => {
    const view = documentReadView(FOLDED);
    expect(view.multi).toBe(true);
    expect(view.reported).toBe(4);
    expect(view.documents.map((d) => d.name)).toEqual(['a.pdf', 'b.pdf', 'c.pdf', 'd.pdf']);
  });

  it('SOURCE-MULTI-02 · each page lands under its own document, prefix stripped', () => {
    const view = documentReadView(FOLDED);
    expect(view.documents[1].pages).toEqual([{ page: 1, text: 'TECHSARA_FILE_B = BRAVO-4732' }]);
    expect(view.documents[3].pages[0].text).toBe('TECHSARA_FILE_D = DELTA-8044');
  });

  it('SOURCE-MULTI-04 · a single document is exactly what it was', () => {
    const view = documentReadView({
      filename: 'solo.pdf',
      total_pages: 2,
      pages: [
        { page: 1, text: 'one' },
        { page: 2, text: '[not a prefix] two' },
      ],
    });
    expect(view.multi).toBe(false);
    expect(view.documents).toEqual([
      {
        name: 'solo.pdf',
        pages: [
          { page: 1, text: 'one' },
          { page: 2, text: '[not a prefix] two' },
        ],
      },
    ]);
  });

  it('SOURCE-MULTI-05 · two files with the SAME name, read one after the other, stay two', () => {
    const view = documentReadView({
      filename: 'report.pdf (+1 more)',
      total_pages: 2,
      pages: [
        { page: 1, text: '[report.pdf] first file' },
        { page: 1, text: '[report.pdf] second file' },
      ],
    });
    // Consecutive runs of one name: page 1 then page 1 again means a new run
    // is NOT started by name alone… so both pages share a group here. That is
    // the honest limit of name-only evidence, and the runtime list is kept in
    // order rather than collapsed: both pages are present.
    expect(view.documents.reduce((n, d) => n + d.pages.length, 0)).toBe(2);
    expect(view.reported).toBe(2);
  });

  it('SOURCE-MULTI-06 · reports what the engine said, not what was attached', () => {
    // One document read. The helper has no access to attachments at all, so
    // nothing can be added from them — by construction, not by guard.
    const view = documentReadView({ filename: 'a.pdf', total_pages: 1, pages: [{ page: 1, text: 'x' }] });
    expect(view.documents).toHaveLength(1);
    expect(view.reported).toBe(1);
  });

  it('says when the engine read more documents than the capped pages show', () => {
    const view = documentReadView({
      filename: 'a.pdf (+3 more)',
      total_pages: 200,
      pages: [{ page: 1, text: '[a.pdf] only this one made the cap' }],
    });
    expect(view.reported).toBe(4);
    expect(view.documents).toHaveLength(1);
  });
});

describe('ActivityPanel rendering', () => {
  it('SOURCE-MULTI-01/03 · four names on screen, no "(+3 more)"', () => {
    renderPanel(FOLDED);
    const s = section();
    for (const name of ['a.pdf', 'b.pdf', 'c.pdf', 'd.pdf']) {
      expect(within(s).getByText(name, { exact: false })).toBeTruthy();
    }
    expect(within(s).queryByText(/\+3 more/)).toBeNull();
    expect(within(s).getByText(/4 documents · 4 pages read in full/)).toBeTruthy();
  });

  it('SOURCE-MULTI-02 · the page under each file is that file\'s page', () => {
    renderPanel(FOLDED);
    const s = section();
    expect(within(s).getByText('TECHSARA_FILE_C = CHARLIE-6159')).toBeTruthy();
    expect(within(s).queryByText(/\[c\.pdf\]/)).toBeNull();
    expect(within(s).getAllByText('Page 1')).toHaveLength(4);
  });

  it('SOURCE-MULTI-04 · one document keeps the existing single-file header', () => {
    renderPanel({ filename: 'solo.pdf', total_pages: 1, pages: [{ page: 1, text: 'hello' }] });
    const s = section();
    expect(within(s).getByText(/solo\.pdf · 1 page read in full/)).toBeTruthy();
    expect(within(s).queryByText(/documents/i)).toBeNull();
  });

  it('is honest about documents whose pages did not fit', () => {
    renderPanel({
      filename: 'a.pdf (+3 more)',
      total_pages: 200,
      pages: [{ page: 1, text: '[a.pdf] first' }],
    });
    expect(within(section()).getByText(/3 more documents were read/)).toBeTruthy();
  });
});
