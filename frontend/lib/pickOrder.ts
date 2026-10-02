/**
 * Attachments keep the order they were PICKED in (C6, 2026-10-03).
 *
 * The composer prepares each picked file on its own: a photo is shrunk (three
 * decodes at a time, lib/images MAX_PARALLEL_DECODES), a small document is
 * read whole, and a file that streams gets its chip at once. Appending each
 * chip when its own work finished put them in COMPLETION order — a
 * real-browser run picked mid.jpg, small.jpg, b100.jpg and got small, b100,
 * mid; a 4 KB document picked first landed last; with 100 photos the first
 * two swapped — and the bubble, the /chat body and `meta.images` /
 * `meta.attachments` all follow the chips. So each file takes a number when it
 * is picked, and its chip is placed by that number whenever it lands.
 */

export interface PickOrdered {
  /** When the file was picked, counted per composer. Absent = after everything. */
  pickOrder?: number;
}

/** `list` with `item` placed before the first entry that was picked after it. */
export function inPickOrder<T extends PickOrdered>(list: readonly T[], item: T): T[] {
  const order = item.pickOrder;
  const at =
    order === undefined
      ? -1
      : list.findIndex((other) => other.pickOrder !== undefined && other.pickOrder > order);
  return at < 0 ? [...list, item] : [...list.slice(0, at), item, ...list.slice(at)];
}
