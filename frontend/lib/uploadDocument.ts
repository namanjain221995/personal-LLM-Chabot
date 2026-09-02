/**
 * Streaming document upload (2026-09-02).
 *
 * Documents used to travel as base64 inside the chat JSON, which is what
 * capped them at 25 MB — the limit belonged to the transport, not to
 * anything the engine needs. Big documents now stream to the same rail
 * datasets use (`purpose=document`: the server keeps the original bytes and
 * extracts nothing), and the chat request carries a reference.
 *
 * Cloudflare's edge caps a single request body at 100 MB on this plan, so a
 * file bigger than CHUNK_THRESHOLD_BYTES is sliced into CHUNK_PART_BYTES
 * pieces and reassembled server-side — that is what makes a 512 MB upload
 * work on ai.techsarasolutions.com and not just on the LAN.
 */

export const CHUNK_THRESHOLD_BYTES = 90 * 1024 * 1024;
export const CHUNK_PART_BYTES = 64 * 1024 * 1024;

export interface DocumentRef {
  upload_id: string;
  name: string;
}

async function jsonOrThrow(res: Response, fallback: string): Promise<any> {
  let body: any = null;
  try {
    body = await res.json();
  } catch {
    /* an HTML error page from a proxy — the status carries the story */
  }
  if (!res.ok) throw new Error(body?.detail ?? body?.message ?? fallback);
  return body;
}

async function uploadSingle(
  file: File,
  conversationId: string,
): Promise<DocumentRef> {
  const form = new FormData();
  form.append('file', file);
  form.append('conversation_id', conversationId);
  form.append('purpose', 'document');
  const res = await fetch('/api/upload', { method: 'POST', body: form });
  const body = await jsonOrThrow(res, 'upload failed');
  return { upload_id: body.upload_id, name: body.filename ?? file.name };
}

async function uploadChunked(
  file: File,
  conversationId: string,
): Promise<DocumentRef> {
  const form = new FormData();
  form.append('conversation_id', conversationId);
  form.append('filename', file.name);
  form.append('purpose', 'document');
  const init = await jsonOrThrow(
    await fetch('/api/upload/chunked/init', { method: 'POST', body: form }),
    'upload could not start',
  );
  const uploadId: string = init.upload_id;

  const parts = Math.ceil(file.size / CHUNK_PART_BYTES);
  for (let i = 0; i < parts; i += 1) {
    const slice = file.slice(i * CHUNK_PART_BYTES, (i + 1) * CHUNK_PART_BYTES);
    const res = await fetch(
      `/api/upload/chunked/${encodeURIComponent(conversationId)}/${uploadId}/part/${i}`,
      { method: 'PUT', body: slice },
    );
    await jsonOrThrow(res, `part ${i + 1} of ${parts} failed`);
  }

  const done = await fetch(
    `/api/upload/chunked/${encodeURIComponent(conversationId)}/${uploadId}/complete`,
    { method: 'POST' },
  );
  const body = await jsonOrThrow(done, 'upload could not be assembled');
  return { upload_id: body.upload_id, name: body.filename ?? file.name };
}

/** Stream one document to the server; → the reference the chat request sends. */
export async function uploadDocumentFile(
  file: File,
  conversationId: string,
): Promise<DocumentRef> {
  return file.size > CHUNK_THRESHOLD_BYTES
    ? uploadChunked(file, conversationId)
    : uploadSingle(file, conversationId);
}
