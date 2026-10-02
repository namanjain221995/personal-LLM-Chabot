import { describe, expect, it, vi } from 'vitest';
import {
  MAX_IMAGE_EDGE,
  MAX_PARALLEL_DECODES,
  dataUrlByteLength,
  downscaleImageFile,
  fitWithin,
  outputMime,
} from '../lib/images';

describe('fitWithin (client-side downscale, 2026-08-29)', () => {
  it('leaves images at or under the cap untouched', () => {
    expect(fitWithin(1280, 800)).toEqual({ width: 1280, height: 800, scaled: false });
    expect(fitWithin(MAX_IMAGE_EDGE, 900)).toEqual({ width: MAX_IMAGE_EDGE, height: 900, scaled: false });
  });
  it('scales the long edge to the cap and keeps the aspect ratio', () => {
    const r = fitWithin(2560, 1440);
    expect(r.scaled).toBe(true);
    expect(r.width).toBe(1600);
    expect(r.height).toBe(900);
    const portrait = fitWithin(1080, 2400);
    expect(portrait).toEqual({ width: 720, height: 1600, scaled: true });
  });
  it('honours an explicit cap and never produces a zero dimension', () => {
    expect(fitWithin(4000, 10, 1000)).toEqual({ width: 1000, height: 3, scaled: true });
    expect(fitWithin(0, 0)).toEqual({ width: 0, height: 0, scaled: false });
  });
});

describe('outputMime', () => {
  it('keeps screenshots and unknown types lossless', () => {
    expect(outputMime('image/png')).toBe('image/png');
    expect(outputMime('image/gif')).toBe('image/png');
    expect(outputMime('')).toBe('image/png');
  });
  it('re-encodes photos as JPEG', () => {
    expect(outputMime('image/jpeg')).toBe('image/jpeg');
    expect(outputMime('image/jpg')).toBe('image/jpeg');
  });
  it('keeps webp lossless so alpha is not flattened to black', () => {
    expect(outputMime('image/webp')).toBe('image/png');
  });
});

describe('dataUrlByteLength (the photo size rule, 2026-10-03)', () => {
  it('counts the decoded bytes, padding included', () => {
    expect(dataUrlByteLength('data:image/png;base64,')).toBe(0);
    expect(dataUrlByteLength('data:image/png;base64,QQ==')).toBe(1);
    expect(dataUrlByteLength('data:image/png;base64,QUI=')).toBe(2);
    expect(dataUrlByteLength('data:image/png;base64,QUJD')).toBe(3);
    const threeKiB = btoa('x'.repeat(3 * 1024));
    expect(dataUrlByteLength(`data:image/jpeg;base64,${threeKiB}`)).toBe(3 * 1024);
  });
});

describe('downscaleImageFile decodes a few photos at a time (2026-10-03)', () => {
  it('never more than MAX_PARALLEL_DECODES at once, and every photo is done', async () => {
    let open = 0;
    let peak = 0;
    const release: Array<() => void> = [];
    vi.stubGlobal('createImageBitmap', async () => {
      open += 1;
      peak = Math.max(peak, open);
      await new Promise<void>((resolve) => release.push(resolve));
      open -= 1;
      // Already fits: "send as is", so no canvas is needed.
      return { width: 100, height: 100, close: () => undefined };
    });
    vi.stubGlobal('document', {});
    try {
      const files = Array.from({ length: 12 }, (_, i) => new File(['x'], `p${i}.jpg`));
      const all = Promise.all(files.map((f) => downscaleImageFile(f)));
      for (let done = 0; done < files.length; done += 1) {
        await vi.waitFor(() => expect(release.length).toBeGreaterThan(0));
        expect(open).toBeLessThanOrEqual(MAX_PARALLEL_DECODES);
        release.shift()!();
      }
      expect(await all).toEqual(Array(12).fill(null));
      expect(peak).toBe(MAX_PARALLEL_DECODES);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
