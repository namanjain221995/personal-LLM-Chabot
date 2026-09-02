/**
 * 512 MB documents (2026-09-02): the transport, not the engine, owned the old
 * 25 MB cap — a PDF travelled as base64 inside the chat JSON. Big documents
 * now stream to /api/upload (purpose=document) and the chat request carries a
 * reference; past Cloudflare's 100 MB edge cap they travel in parts.
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import {
  CHUNK_PART_BYTES,
  CHUNK_THRESHOLD_BYTES,
  uploadDocumentFile,
} from '@/lib/uploadDocument';
import { toOrchestratorChatRequest, PDF_ONLY_PROMPT } from '@/lib/orchestrator';

afterEach(() => vi.unstubAllGlobals());

const okJson = (body: unknown) => ({
  ok: true,
  status: 200,
  json: async () => body,
});

function fakeFile(size: number, name = 'big.pdf'): File {
  // File.slice is all the uploader touches — a real half-gigabyte Blob would
  // make the suite pay for bytes the code never reads.
  const slices: Array<[number, number]> = [];
  return {
    name,
    size,
    slice(start: number, end: number) {
      slices.push([start, end]);
      return new Blob([`part:${start}`]);
    },
  } as unknown as File;
}

describe('uploadDocumentFile', () => {
  it('small files take ONE request, with purpose=document', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return okJson({ upload_id: 'a'.repeat(32), filename: 'small.pdf' });
      }),
    );
    const ref = await uploadDocumentFile(fakeFile(1024, 'small.pdf'), 'conv-1');
    expect(ref).toEqual({ upload_id: 'a'.repeat(32), name: 'small.pdf' });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('/api/upload');
    const form = calls[0].init.body as FormData;
    expect(form.get('purpose')).toBe('document');
    expect(form.get('conversation_id')).toBe('conv-1');
  });

  it('a 512 MB file becomes init + ceil(size/part) parts + complete', async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(String(url));
        return okJson({ upload_id: 'b'.repeat(32), filename: 'big.pdf' });
      }),
    );
    const size = 512 * 1024 * 1024;
    const ref = await uploadDocumentFile(fakeFile(size), 'conv-2');
    expect(ref.upload_id).toBe('b'.repeat(32));
    const parts = Math.ceil(size / CHUNK_PART_BYTES);
    expect(urls[0]).toBe('/api/upload/chunked/init');
    expect(urls.length).toBe(parts + 2); // init + parts + complete
    expect(urls[1]).toContain('/part/0');
    expect(urls[urls.length - 2]).toContain(`/part/${parts - 1}`);
    expect(urls[urls.length - 1]).toContain('/complete');
    // Every part clears the Cloudflare edge cap with room to spare.
    expect(CHUNK_PART_BYTES).toBeLessThan(95 * 1024 * 1024);
    expect(CHUNK_THRESHOLD_BYTES).toBeLessThan(100 * 1024 * 1024);
  });

  it('a failed part surfaces the server detail, not a shrug', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        String(url).includes('/part/')
          ? { ok: false, status: 413, json: async () => ({ detail: 'part exceeds 90 MB' }) }
          : okJson({ upload_id: 'c'.repeat(32) }),
      ),
    );
    await expect(
      uploadDocumentFile(fakeFile(CHUNK_THRESHOLD_BYTES + 1), 'conv-3'),
    ).rejects.toThrow(/part exceeds 90 MB/);
  });
});

describe('the chat translator and streamed documents', () => {
  it('forwards pdf_uploads to the orchestrator', () => {
    const out = toOrchestratorChatRequest({
      session_id: 's',
      messages: [{ role: 'user', content: 'compare them' }],
      pdf_uploads: [{ upload_id: 'd'.repeat(32), name: 'contract.pdf' }],
    } as never);
    expect(out?.pdf_uploads).toEqual([
      { upload_id: 'd'.repeat(32), name: 'contract.pdf' },
    ]);
  });

  it('a wordless reference-only send is a document question, not a 400', () => {
    const out = toOrchestratorChatRequest({
      session_id: 's',
      messages: [],
      pdf_uploads: [{ upload_id: 'e'.repeat(32), name: 'x.pdf' }],
    } as never);
    expect(out).not.toBeNull();
    expect(out?.message).toBe(PDF_ONLY_PROMPT);
  });

  it('an inline-only send keeps its exact v1 key set', () => {
    const out = toOrchestratorChatRequest({
      session_id: 's',
      messages: [{ role: 'user', content: 'hi' }],
    } as never);
    expect(out && 'pdf_uploads' in out).toBe(false);
  });
});
