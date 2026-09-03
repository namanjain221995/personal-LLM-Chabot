// @vitest-environment jsdom
/**
 * L-14: a draft attachment chip's identity is its own, not its position.
 *
 * The chip list was keyed `${attachment.name}-${index}`. Attach two files
 * called report.png and remove the first: the survivor slides to index 0 and
 * inherits the removed chip's key, so React reuses that component instance for
 * a different file rather than unmounting it. Removal had the same shape —
 * `filter((_, i) => i !== idx)` — which is correct only while the closure's
 * index still describes the array it is filtering.
 *
 * Every draft attachment now carries a `clientId`, minted once when the chip
 * is created. That is a COMPOSER-LOCAL identity and has nothing to do with the
 * server's durable `upload_id`: this one exists before any upload does, never
 * goes over the wire, and dies with the send. The tests that cover the server
 * id are tests/upload-id-persistence.test.ts and tests/multi-document*.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Composer, type Attachment } from '../components/Composer';
import { DEFAULT_PREFS } from '@/lib/prefs';

/** Distinct bytes per file, so a reused instance shows the WRONG preview. */
const png = (name: string, marker: string) =>
  new File([marker], name, { type: 'image/png' });

let sent: Attachment[][];

beforeEach(() => {
  sent = [];
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
  // The composer reads every accepted image with a FileReader; jsdom's
  // implementation is real, so the chips appear asynchronously.
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount() {
  render(
    <Composer
      streaming={false}
      prefs={DEFAULT_PREFS}
      onPrefsChange={() => undefined}
      onSend={(_t, atts) => {
        sent.push(atts);
      }}
      onStop={() => undefined}
    />,
  );
}

/** Attach files through the picker and wait for every chip to land.
 *  Counts from what is ALREADY on screen, so a second call really waits. */
async function attach(files: File[]) {
  const before = screen.queryAllByRole('button', { name: /^Remove attachment/ }).length;
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files } });
  });
  await waitFor(() =>
    expect(
      screen.queryAllByRole('button', { name: /^Remove attachment/ }).length,
    ).toBe(before + files.length),
  );
}

const removeButtons = () =>
  screen.getAllByRole('button', { name: /^Remove attachment/ });

const chipImages = () =>
  Array.from(document.querySelectorAll<HTMLImageElement>('img[alt^="Attached:"]'));

const chipNames = () =>
  Array.from(document.querySelectorAll('span.max-w-\\[200px\\]')).map(
    (el) => el.textContent,
  );

describe('L-14 · two attachments with the SAME filename', () => {
  it('both exist at once, as two independent chips', async () => {
    mount();
    await attach([png('report.png', 'RED'), png('report.png', 'BLUE')]);

    expect(removeButtons()).toHaveLength(2);
    expect(chipNames()).toEqual(['report.png', 'report.png']);
    // Distinct bytes ⇒ distinct data URLs. If these were equal the test could
    // not tell a correct survivor from a reused instance.
    const [first, second] = chipImages();
    expect(first.src).not.toBe(second.src);
  });

  it('removing the FIRST leaves the SECOND, with its own preview intact', async () => {
    mount();
    await attach([png('report.png', 'RED'), png('report.png', 'BLUE')]);

    const secondSrc = chipImages()[1].src;

    await act(async () => {
      fireEvent.click(removeButtons()[0]);
    });

    expect(removeButtons()).toHaveLength(1);
    // The survivor is the SECOND file — not the first wearing its name.
    expect(chipImages()).toHaveLength(1);
    expect(chipImages()[0].src).toBe(secondSrc);
  });

  it('the survivor’s own remove button still targets the survivor', async () => {
    mount();
    await attach([png('report.png', 'RED'), png('report.png', 'BLUE')]);

    await act(async () => {
      fireEvent.click(removeButtons()[0]);
    });
    expect(removeButtons()).toHaveLength(1);

    await act(async () => {
      fireEvent.click(removeButtons()[0]);
    });
    // The stale-index bug would have filtered the wrong slot (or nothing).
    expect(screen.queryAllByRole('button', { name: /^Remove attachment/ })).toHaveLength(0);
  });

  it('sends the file that is still attached, not the one that was removed', async () => {
    mount();
    await attach([png('report.png', 'RED'), png('report.png', 'BLUE')]);

    const survivingSrc = chipImages()[1].src;
    await act(async () => {
      fireEvent.click(removeButtons()[0]);
    });

    await act(async () => {
      fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
        target: { value: 'go' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });

    expect(sent).toHaveLength(1);
    expect(sent[0]).toHaveLength(1);
    expect(survivingSrc).toContain(sent[0][0].base64);
  });
});

describe('L-14 · A, B, C — removing the middle one', () => {
  it('leaves A and C, each still paired with its own preview', async () => {
    mount();
    await attach([png('a.png', 'AAA'), png('b.png', 'BBB'), png('c.png', 'CCC')]);
    expect(removeButtons()).toHaveLength(3);

    const [srcA, , srcC] = chipImages().map((img) => img.src);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove attachment b.png' }));
    });

    expect(chipNames()).toEqual(['a.png', 'c.png']);
    expect(chipImages().map((img) => img.src)).toEqual([srcA, srcC]);
  });
});

describe('L-14 · the identity itself', () => {
  it('every attachment carries a clientId, and no two share one', async () => {
    mount();
    await attach([png('report.png', 'RED'), png('report.png', 'BLUE')]);

    await act(async () => {
      fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
        target: { value: 'go' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });

    const atts = sent[0];
    expect(atts).toHaveLength(2);
    for (const a of atts) {
      expect(typeof a.clientId).toBe('string');
      expect(a.clientId.length).toBeGreaterThan(0);
    }
    expect(new Set(atts.map((a) => a.clientId)).size).toBe(2);
  });

  it('is COMPOSER-LOCAL: it is not the server upload_id and carries no upload state', async () => {
    mount();
    await attach([png('report.png', 'RED')]);

    await act(async () => {
      fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
        target: { value: 'go' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });

    const att = sent[0][0];
    // Namespaced so it can never be mistaken for a 32-hex server upload id.
    expect(att.clientId.startsWith('att-')).toBe(true);
    expect(att.clientId).not.toMatch(/^[0-9a-f]{32}$/);
  });

  it('an attachment keeps its clientId across unrelated state changes', async () => {
    mount();
    await attach([png('keep.png', 'KEEP')]);

    // Typing re-renders the composer; identity must not be re-minted.
    const before = chipImages()[0].src;
    await act(async () => {
      fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
        target: { value: 'some text' },
      });
    });
    await attach([png('second.png', 'SECOND')]);

    expect(chipImages()[0].src).toBe(before);
    expect(chipNames()).toEqual(['keep.png', 'second.png']);
  });
});
