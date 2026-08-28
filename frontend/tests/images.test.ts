import { describe, expect, it } from 'vitest';
import { MAX_IMAGE_EDGE, fitWithin, outputMime } from '../lib/images';

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
