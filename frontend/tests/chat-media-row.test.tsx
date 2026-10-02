// @vitest-environment jsdom
/**
 * A user turn's photos in the bubble (2026-10-02, docs/chat-media/CONTRACT.md
 * §10 "Render").
 *
 *   · the device that sent the photo shows its own bytes, instantly;
 *   · any other device shows the stored thumbnail, and opens the full photo
 *     in the existing preview;
 *   · a photo the server cannot find is an "Image unavailable" tile of the
 *     same size, never a broken-image icon;
 *   · a turn sent before photos were stored says so, once, quietly — and only
 *     when there is genuinely nothing to show;
 *   · RC-3a: a photo and a document on one turn are two separate cards that
 *     each open their own file.
 */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageRow } from '@/components/MessageRow';
import {
  clearAttachments,
  INTERNAL_ATTACHMENT_MIME,
  rememberAttachmentFiles,
} from '@/lib/attachments';
import type { ChatMessage } from '@/lib/types';

const PNG =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';
const CONV = 'conv-media-1';

const turn = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'u1',
  role: 'user',
  content: 'is this healthy?',
  createdAt: 0,
  ...over,
});
const stored = { images: [{ attachment_id: 'img-aaaa-0001', name: 'leaf.jpg', width: 800, height: 600 }] };

function renderRow(message: ChatMessage, extra: Record<string, unknown> = {}) {
  return render(
    <MessageRow
      message={message}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      conversationId={CONV}
      {...extra}
    />,
  );
}

let created: string[] = [];
beforeEach(() => {
  created = [];
  let seq = 0;
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: (blob: Blob) => {
      const url = `blob:mock/${blob.type || 'none'}/${seq++}`;
      created.push(url);
      return url;
    },
    revokeObjectURL: () => undefined,
  });
  clearAttachments();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('the device that sent it', () => {
  it('shows its own bytes at once and asks the server for nothing', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const { container } = renderRow(turn({ imageDataUrl: PNG, meta: stored }));
    const imgs = container.querySelectorAll('img');
    expect(imgs).toHaveLength(1);
    expect(imgs[0].getAttribute('src')).toBe(PNG);
    expect(container.innerHTML).not.toContain('/api/chat-media/');
    expect(fetchMock).not.toHaveBeenCalled();
    // The name the turn recorded, where this tab holds no File.
    expect(screen.getByRole('button', { name: /leaf\.jpg/ })).toBeTruthy();
  });
});

describe('another device', () => {
  it('opens the FULL photo in the existing preview', async () => {
    const fetchMock = vi.fn<(url: string) => Promise<Response>>(
      async () =>
        new Response(new Uint8Array([0xff, 0xd8, 0xff]), {
          status: 200,
          headers: { 'content-type': 'image/jpeg' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);
    renderRow(turn({ meta: stored }));

    fireEvent.click(screen.getByRole('button', { name: /leaf\.jpg/ }));
    const dialog = await screen.findByRole('dialog');
    await vi.waitFor(() =>
      expect(dialog.querySelector('img')?.getAttribute('src')).toMatch(/^blob:mock\/image\/jpeg/),
    );
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/chat-media/${CONV}/img-aaaa-0001?size=full`);
  });

  it('says so honestly when the full photo is gone (410)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ code: 'media_missing' }, { status: 410 })));
    renderRow(turn({ meta: stored }));
    fireEvent.click(screen.getByRole('button', { name: /leaf\.jpg/ }));
    const dialog = await screen.findByRole('dialog');
    await vi.waitFor(() =>
      expect(dialog.textContent).toContain('This photo is no longer stored on the server.'),
    );
  });

  it('turns a thumbnail that cannot load into an "Image unavailable" tile — after one retry', () => {
    vi.useFakeTimers();
    const { container } = renderRow(turn({ meta: stored }));
    const first = container.querySelector('img') as HTMLImageElement;
    expect(first.getAttribute('src')).toBe(`/api/chat-media/${CONV}/img-aaaa-0001?size=thumb`);

    fireEvent.error(first);
    // Hidden while it waits, never a broken-image icon; the box stays.
    expect(first.className).toContain('opacity-0');
    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    const second = container.querySelector('img') as HTMLImageElement;
    expect(second.getAttribute('src')).toBe(
      `/api/chat-media/${CONV}/img-aaaa-0001?size=thumb&retry=1`,
    );
    fireEvent.error(second);

    const tile = screen.getByTestId('stored-image-unavailable');
    expect(tile.textContent).toContain('Image unavailable');
    expect(tile.style.width).toBe('213px');
    expect(tile.style.aspectRatio).toBe('800 / 600');
    expect(container.querySelector('img')).toBeNull();
  });

  it('a thumbnail that loads stays as it is', () => {
    const { container } = renderRow(turn({ meta: stored }));
    const img = container.querySelector('img') as HTMLImageElement;
    fireEvent.load(img);
    expect(img.className).not.toContain('opacity-0');
    expect(screen.queryByTestId('stored-image-unavailable')).toBeNull();
  });

  it('uses a fixed square when the turn recorded no size — still no shift', () => {
    const { container } = renderRow(turn({ meta: { images: [{ attachment_id: 'img-aaaa-0001' }] } }));
    const img = container.querySelector('img') as HTMLImageElement;
    expect(img.style.width).toBe('160px');
    expect(img.style.height).toBe('160px');
  });

  it('renders nothing for stored photos outside a conversation, as before', () => {
    const { container } = render(
      <MessageRow message={turn({ meta: stored })} isLast={false} onRegenerate={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(container.querySelector('img')).toBeNull();
  });

  it('drags a photo card as a PHOTO reference', () => {
    renderRow(turn({ meta: stored }), { onReuseAttachment: vi.fn() });
    const card = screen.getByRole('button', { name: /leaf\.jpg/ });
    const data: Record<string, string> = {};
    const dt = { setData: (k: string, v: string) => void (data[k] = v), effectAllowed: 'all' };
    fireEvent.dragStart(card, { dataTransfer: dt });
    expect(JSON.parse(data[INTERNAL_ATTACHMENT_MIME])).toEqual({
      messageId: 'u1',
      index: 0,
      space: 'image',
    });
  });
});

describe('a photo sent before photos were stored', () => {
  const NOTE =
    'Photo not stored on the server (sent before photos were saved), so it only shows on the device that sent it.';

  it('says so in one muted line when the host says the turn is one', () => {
    renderRow(turn(), { legacyPhoto: true });
    expect(screen.getByTestId('legacy-photo-note').textContent?.replace(/\s+/g, ' ').trim()).toBe(NOTE);
  });

  it('never when the photo can be shown, from either source', () => {
    renderRow(turn({ imageDataUrl: PNG }), { legacyPhoto: true });
    expect(screen.queryByTestId('legacy-photo-note')).toBeNull();
    cleanup();
    renderRow(turn({ meta: stored }), { legacyPhoto: true });
    expect(screen.queryByTestId('legacy-photo-note')).toBeNull();
    cleanup();
    renderRow(turn());
    expect(screen.queryByTestId('legacy-photo-note')).toBeNull();
  });
});

describe('RC-3a — a photo and a document on one turn', () => {
  it('render as two cards, and each opens its OWN file', async () => {
    rememberAttachmentFiles(
      'u1',
      [{ name: 'leaf.png', mime: 'image/png', blob: new Blob(['img'], { type: 'image/png' }) }],
      'image',
    );
    rememberAttachmentFiles('u1', [
      { name: 'report.txt', mime: 'text/plain', blob: new Blob(['the report'], { type: 'text/plain' }) },
    ]);
    renderRow(
      turn({
        imageDataUrl: PNG,
        pdfName: 'report.txt',
        meta: {
          attachments: [{ name: 'report.txt', kind: 'pdf', attachment_id: 'doc-aaaa-0001' }],
          images: [{ attachment_id: 'img-aaaa-0001' }],
        },
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: /leaf\.png/ }));
    let dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('leaf.png');
    expect(dialog.querySelector('img')).toBeTruthy();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    cleanup();

    renderRow(
      turn({
        imageDataUrl: PNG,
        pdfName: 'report.txt',
        meta: {
          attachments: [{ name: 'report.txt', kind: 'pdf', attachment_id: 'doc-aaaa-0001' }],
          images: [{ attachment_id: 'img-aaaa-0001' }],
        },
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: /report\.txt/ }));
    dialog = await screen.findByRole('dialog');
    await vi.waitFor(() => expect(dialog.textContent).toContain('the report'));
  });
});
