import { beforeEach, describe, expect, it } from 'vitest';
import {
  attachmentForResend,
  base64FromDataUrl,
  clearAttachments,
  rememberAttachment,
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

describe('attachmentForResend', () => {
  it('returns the remembered payload for a PDF turn', () => {
    rememberAttachment('m1', { kind: 'pdf', name: 'report.pdf', base64: 'JVBER' });
    const out = attachmentForResend({ id: 'm1', pdfName: 'report.pdf' });
    expect(out.missing).toBe(false);
    expect(out.attachment).toEqual({
      kind: 'pdf',
      name: 'report.pdf',
      base64: 'JVBER',
    });
  });

  it('rebuilds an image from the persisted preview after a reload', () => {
    // Nothing remembered (fresh tab), but the data URL IS the payload.
    const out = attachmentForResend({
      id: 'm2',
      imageDataUrl: 'data:image/png;base64,IMGDATA',
    });
    expect(out.missing).toBe(false);
    expect(out.attachment?.kind).toBe('image');
    expect(out.attachment?.base64).toBe('IMGDATA');
  });

  it('reports MISSING for a PDF turn after a reload rather than silently dropping it', () => {
    const out = attachmentForResend({ id: 'm3', pdfName: 'report.pdf' });
    expect(out.attachment).toBeNull();
    expect(out.missing).toBe(true);
  });

  it('reports nothing missing for a plain text turn', () => {
    const out = attachmentForResend({ id: 'm4' });
    expect(out.attachment).toBeNull();
    expect(out.missing).toBe(false);
  });
});
