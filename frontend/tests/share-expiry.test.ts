/**
 * deriveExpiryChoice — which option describes a link that already exists.
 *
 * The server stores a DATE, not the choice that made it, so this is a best
 * fit and the dialog prints the real date beneath it. What must never happen
 * is the control claiming MORE time than the link has left: an owner reading
 * "90 days" on a link that dies tomorrow would not think to extend it.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';

import { deriveExpiryChoice, expiryLabel } from '@/lib/share';

const CHOICES = ['24h', '7d', '30d', '90d'];
const WITH_NEVER = [...CHOICES, 'never'];

function inHours(h: number): string {
  return new Date(Date.now() + h * 3_600_000).toISOString();
}

afterEach(() => vi.useRealTimers());

describe('deriveExpiryChoice', () => {
  it('picks the smallest option that still covers the time left', () => {
    expect(deriveExpiryChoice(inHours(20), CHOICES)).toBe('24h');
    expect(deriveExpiryChoice(inHours(100), CHOICES)).toBe('7d');
    expect(deriveExpiryChoice(inHours(500), CHOICES)).toBe('30d');
    expect(deriveExpiryChoice(inHours(2000), CHOICES)).toBe('90d');
  });

  it('never claims more time than the link actually has', () => {
    for (const hours of [1, 23, 25, 167, 169, 719, 721, 2159]) {
      const chosen = deriveExpiryChoice(inHours(hours), CHOICES);
      const ceiling = { '24h': 24, '7d': 168, '30d': 720, '90d': 2160 }[chosen]!;
      expect(ceiling).toBeGreaterThanOrEqual(hours);
    }
  });

  it('says never only when the workspace offers it', () => {
    expect(deriveExpiryChoice(null, WITH_NEVER)).toBe('never');
    // Policy forbids it: fall back to a real option rather than showing a
    // choice the server would refuse.
    expect(deriveExpiryChoice(null, CHOICES)).toBe('24h');
  });

  it('falls back to a real option for an expired or unparseable date', () => {
    expect(CHOICES).toContain(deriveExpiryChoice(inHours(-50), CHOICES));
    expect(CHOICES).toContain(deriveExpiryChoice('not-a-date', CHOICES));
  });

  it('degrades to the widest offered option past every ceiling', () => {
    expect(deriveExpiryChoice(inHours(9000), CHOICES)).toBe('90d');
  });
});

describe('expiryLabel', () => {
  it('reads as time remaining, and says so when there is none', () => {
    expect(expiryLabel(inHours(6 * 24 + 1))).toBe('expires in 6 days');
    expect(expiryLabel(inHours(25))).toBe('expires in 1 day');
    expect(expiryLabel(inHours(3))).toBe('expires in 3 hours');
    expect(expiryLabel(inHours(-1))).toBe('expired');
    expect(expiryLabel(null)).toBeNull();
  });
});
