// @vitest-environment jsdom
/**
 * Download cards (FileCards) keep the END of a long file name visible.
 *
 * A video answer's files are named `<source video>-<hash>-u<id>.transcript.vtt`.
 * When the source video was named by a phone — a UUID — every card began with
 * the same thirty-six characters and a right-hand `truncate` cut off the only
 * part that differed, so seven cards read as seven copies of one file
 * (owner screenshot, 2026-09-11). The name is clipped from the left now, and
 * the full name rides on `title` for the hover.
 *
 * 2026-09-12 (CONTRACT-2 §9): the cards are the SAME FileCard the Artifact
 * Studio files use, by way of the legacy adapter. The visible name is the
 * filename minus its suffix (the format is said on the facts line instead),
 * the tooltip is still the full name, the clip is still from the left, and
 * the whole card is still the download link.
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { FileCards } from '@/components/FileCards';

afterEach(cleanup);

const UUID_NAME =
  'f2c39b35-b713-422d-aed9-1df901ebf62b-8f7f23eb-u11.transcript.vtt';

describe('FileCards', () => {
  it('clips a long name from the left so the extension stays readable', () => {
    render(<FileCards files={[{ filename: UUID_NAME, type: 'vtt', size: 46_000 }]} />);
    const name = screen.getByTitle(UUID_NAME);
    // Right-to-left layout on the clipping box puts the ellipsis at the
    // front; the inner span is pinned back to left-to-right so the characters
    // themselves are not reversed.
    expect(name.className).toContain('[direction:rtl]');
    expect(name.className).toContain('truncate');
    // CONTRACT-2 §9: "title from the filename minus suffix" — the suffix
    // moved to the facts line ("VTT · 45 KB") under the name.
    expect(name.querySelector('span[dir="ltr"]')?.textContent).toBe(
      'f2c39b35-b713-422d-aed9-1df901ebf62b-8f7f23eb-u11.transcript',
    );
    expect(screen.getByTestId('file-card-meta').textContent).toBe('VTT · 45 KB');
  });

  it('links each file through the reports proxy with a download name', () => {
    render(<FileCards files={[{ filename: UUID_NAME, type: 'vtt', size: 46_000 }]} />);
    const link = screen.getByRole('link');
    expect(link.getAttribute('href')).toBe(`/api/reports/${encodeURIComponent(UUID_NAME)}`);
    expect(link.getAttribute('download')).toBe(UUID_NAME);
    expect(link.textContent).toContain('VTT');
    // The same component as every other generated file (CONTRACT-2 §9).
    expect(screen.getAllByTestId('file-card').length).toBe(1);
  });
});
