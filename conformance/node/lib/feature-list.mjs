// The planned-feature list as plain data, with the scopes each feature's tests
// need. Kept free of node:test and of env.mjs so scripts/provision-key.mjs can
// import it to request exactly the scopes the suite uses (2026-09-13).

/** Scopes every stack's live suite needs, whatever is planned. */
export const BASE_SCOPES = ['models.read', 'responses.read', 'responses.write'];

export const FEATURES = {
  'six-models': {
    status: 'planned',
    scopes: ['models.read', 'responses.write'],
    contract: 'CONTRACT-3 §7, §15',
    what: 'GET /v1/models lists techsara-35b, techsara-8b-vision, techsara-ocr, techsara-embed, techsara-rerank, techsara-whisper; the two extra chat models answer on /v1/responses',
  },
  'one-million-output': {
    status: 'planned',
    scopes: ['models.read', 'responses.write'],
    contract: 'CONTRACT-3 §8.3, §9',
    what: 'techsara-35b ceiling of 1,000,000 output tokens; max_output_tokens and incomplete_details on every response object',
  },
  'max-completion-tokens': {
    status: 'planned',
    scopes: ['responses.write'],
    contract: 'CONTRACT-3 §8.2',
    what: 'max_completion_tokens accepted as an alias of max_tokens on /v1/chat/completions; both together is 400',
  },
  'image-input': {
    status: 'planned',
    scopes: ['responses.write'],
    contract: 'CONTRACT-3 §8.1, §8.2',
    what: 'data: URL images as input_image (Responses) and image_url (Chat Completions) parts',
  },
  embeddings: {
    status: 'planned',
    scopes: ['embeddings.write'],
    contract: 'CONTRACT-3 §8.4',
    what: 'POST /v1/embeddings with techsara-embed',
  },
  rerank: {
    status: 'planned',
    scopes: ['rerank.write'],
    contract: 'CONTRACT-3 §8.5',
    what: 'POST /v1/rerank with techsara-rerank (no SDK method; raw HTTP)',
  },
  'audio-transcriptions': {
    status: 'planned',
    scopes: ['audio.write'],
    contract: 'CONTRACT-3 §8.6',
    what: 'POST /v1/audio/transcriptions with techsara-whisper',
  },
  'files-api': {
    status: 'planned',
    scopes: ['files.read', 'files.write'],
    contract: 'Files API design (not yet in CONTRACT-3)',
    what: 'POST/GET/DELETE /v1/files and GET /v1/files/{id}/content',
  },
  'uploads-chunked': {
    status: 'planned',
    scopes: ['files.read', 'files.write'],
    contract: 'Files API design (not yet in CONTRACT-3)',
    what: 'POST /v1/uploads, /parts, /complete, with a resume read of the upload after a client restart',
  },
  'file-input': {
    status: 'planned',
    scopes: ['files.write', 'responses.write'],
    contract: 'Files API design (not yet in CONTRACT-3)',
    what: 'an uploaded file referenced as input_file { file_id } on /v1/responses',
  },
  'no-usage-limits': {
    status: 'planned',
    scopes: ['models.read'],
    contract: 'CONTRACT-3 §12.1',
    what: 'no RateLimit / RateLimit-Policy headers and no 429 for volume on /v1',
  },
};

/** Every scope the suite can use: the base set plus each feature's. */
export const SUITE_SCOPES = [...new Set([...BASE_SCOPES, ...Object.values(FEATURES).flatMap((f) => f.scopes ?? [])])];
