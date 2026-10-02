/**
 * lib/pickOrder (C6, 2026-10-03): a chip is placed where its file was picked,
 * whenever its shrink or read lands. The composer path is pinned end to end
 * in legacy-note-and-pick-order.test.tsx; this pins the placement rule.
 */

import { describe, expect, it } from 'vitest';
import { inPickOrder } from '@/lib/pickOrder';

const chip = (name: string, pickOrder?: number) => ({ name, pickOrder });
const names = (list: Array<{ name: string }>) => list.map((c) => c.name);

describe('inPickOrder', () => {
  it('lands every chip in pick order, whatever order the reads finish in', () => {
    const finished = [chip('small', 2), chip('b100', 3), chip('mid', 1)];
    const list = finished.reduce<Array<ReturnType<typeof chip>>>(
      (acc, c) => inPickOrder(acc, c),
      [],
    );
    expect(names(list)).toEqual(['mid', 'small', 'b100']);
  });

  it('puts a chip with no pick number after everything, and never moves the others', () => {
    const list = [chip('a', 1), chip('b', 3)];
    expect(names(inPickOrder(list, chip('reused')))).toEqual(['a', 'b', 'reused']);
    expect(names(inPickOrder(list, chip('between', 2)))).toEqual(['a', 'between', 'b']);
    expect(names(list)).toEqual(['a', 'b']);
  });

  it('places by the numbered chips only, past any chip without one', () => {
    const list = [chip('reused'), chip('late', 5)];
    expect(names(inPickOrder(list, chip('early', 4)))).toEqual(['reused', 'early', 'late']);
    expect(names(inPickOrder(list, chip('last', 6)))).toEqual(['reused', 'late', 'last']);
  });
});
