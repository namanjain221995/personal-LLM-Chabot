import { beforeEach, describe, expect, it } from 'vitest';
import {
  attachmentsForResend,
  base64FromDataUrl,
  clearAttachments,
  rememberAttachments,
} from '../lib/attachments';

beforeEach(() => clearAttachments());

describe('base64FromDataUrl', () => {
  it('strips the data: prefix', () => {
    expect(base64FromDataUrl('data:image/png;base64,AAAB')).toBe('AAAB');
  });
  it('is null for non-data URLs and empties', () => {
    expect(base64FromDataUrl('https://x/y.png')).toBeNull();
    expect(base64FromDataUrl('data:image/png;base64,')).toBeNull();
    expect(base64FromDataUrl(undefined)).toBeNull();
  });
});

describe('attachmentsForResend (multi-image, 2026-08-05)', () => {
  it('returns the remembered payload for a PDF turn', () => {
    rememberAttachments('m1', [
      { kind: 'pdf', name: 'report.pdf', base64: 'JVBER' },
    ]);
    const out = attachmentsForResend({ id: 'm1', pdfName: 'report.pdf' });
    expect(out.missing).toBe(false);
    expect(out.attachments).toEqual([
      { kind: 'pdf', name: 'report.pdf', base64: 'JVBER' },
    ]);
  });

  it('remembers all images of a multi-image turn', () => {
    rememberAttachments('m5', [
      { kind: 'image', name: 'a.png', base64: 'AAA' },
      { kind: 'image', name: 'b.png', base64: 'BBB' },
    ]);
    const out = attachmentsForResend({ id: 'm5' });
    expect(out.missing).toBe(false);
    expect(out.attachments.map((a) => a.base64)).toEqual(['AAA', 'BBB']);
  });

  it('rebuilds a single image from the persisted preview after a reload', () => {
    // Nothing remembered (fresh tab), but the data URL IS the payload.
    const out = attachmentsForResend({
      id: 'm2',
      imageDataUrl: 'data:image/png;base64,IMGDATA',
    });
    expect(out.missing).toBe(false);
    expect(out.attachments).toHaveLength(1);
    expect(out.attachments[0].kind).toBe('image');
    expect(out.attachments[0].base64).toBe('IMGDATA');
  });

  it('rebuilds EVERY image from imageDataUrls after a reload', () => {
    const out = attachmentsForResend({
      id: 'm6',
      imageDataUrl: 'data:image/png;base64,ONE',
      imageDataUrls: [
        'data:image/png;base64,ONE',
        'data:image/jpeg;base64,TWO',
        'data:image/png;base64,THREE',
      ],
    });
    expect(out.missing).toBe(false);
    expect(out.attachments.map((a) => a.base64)).toEqual([
      'ONE',
      'TWO',
      'THREE',
    ]);
  });

  it('reports MISSING for a PDF turn after a reload rather than silently dropping it', () => {
    const out = attachmentsForResend({ id: 'm3', pdfName: 'report.pdf' });
    expect(out.attachments).toEqual([]);
    expect(out.missing).toBe(true);
  });

  it('reports MISSING when any preview of a multi-image turn is unusable', () => {
    // One preview corrupted → resending only part of the images would
    // silently change the question; report missing instead.
    const out = attachmentsForResend({
      id: 'm7',
      imageDataUrls: ['data:image/png;base64,ONE', 'https://not-a-data-url'],
    });
    expect(out.attachments).toEqual([]);
    expect(out.missing).toBe(true);
  });

  it('reports nothing missing for a plain text turn', () => {
    const out = attachmentsForResend({ id: 'm4' });
    expect(out.attachments).toEqual([]);
    expect(out.missing).toBe(false);
  });
});
