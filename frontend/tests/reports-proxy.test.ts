/**
 * H-03: the /api/reports/[filename] download proxy.
 *
 * Only the filename guard is unit-tested here — it is the security-relevant
 * half and the half with no network in it. Next.js has already percent-decoded
 * the segment by the time it reaches the handler, so both the decoded and the
 * still-encoded shapes are checked.
 */
import { describe, expect, it } from 'vitest';

import { isSafeReportName } from '@/app/api/reports/[filename]/route';

describe('isSafeReportName', () => {
  it('accepts the filenames the orchestrator generates', () => {
    expect(isSafeReportName('data-report-sample-opportunities-csv-20260824-170305.pdf')).toBe(true);
    expect(isSafeReportName('q3-review-20260101-000000.docx')).toBe(true);
    expect(isSafeReportName('query-export-20260101-000000.xlsx')).toBe(true);
  });

  it.each([
    ['empty', ''],
    ['whitespace only', '   '],
    ['dot segment', '..'],
    ['decoded traversal', '../../etc/passwd'],
    ['forward slash', 'sub/file.pdf'],
    ['backslash', 'sub\\file.pdf'],
    ['absolute path', '/etc/passwd'],
    ['hidden file', '.env'],
    ['null byte', 'a\0b.pdf'],
    // Decoded once by Next; a double-encoded attempt still reads as %2e here
    // and must not travel upstream as literal text.
    ['still-encoded dot', '%2e%2e/passwd'],
    ['still-encoded slash', 'a%2fb.pdf'],
    ['untrimmed', ' report.pdf'],
  ])('rejects %s', (_label, name) => {
    expect(isSafeReportName(name)).toBe(false);
  });
});
