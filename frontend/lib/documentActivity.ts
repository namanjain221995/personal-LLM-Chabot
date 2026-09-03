/**
 * Splitting the Activity panel's "Document read" payload back into the
 * documents the engine actually read (2026-09-03).
 *
 * The orchestrator reports ONE `meta.document` per answer. With several
 * documents it folds them into it deliberately — `filename` becomes
 * "first.pdf (+3 more)", `total_pages` is the sum, and every page entry is
 * prefixed with the name of the document it came from: "[b.pdf] …". Rendered
 * as-is that read as one mystery file with four "Page 1" rows.
 *
 * The prefix IS runtime evidence: the engine wrote it for exactly the pages it
 * read, in the order it read them. So grouping here is a parse of what the
 * server said, never a look at the user's attachment list — an attachment the
 * engine did not read has no prefixed page and therefore no group. That is the
 * honesty rule: nothing appears as read that was not reported read.
 *
 * Grouping is by CONSECUTIVE run of the same prefix, not by a name→group map,
 * so two different files that share a filename — read one after the other —
 * stay two entries in the order the engine reported them.
 *
 * Pure module, no React.
 */

import type { DocumentActivity } from './types';

export interface DocumentPage {
  page: number;
  text: string;
}

export interface ReadDocument {
  /** The document's own name, from the page prefix or the single filename. */
  name: string;
  /** Its pages, prefix stripped, page numbers as the engine reported them. */
  pages: DocumentPage[];
}

export interface DocumentReadView {
  /** One entry per document the engine reported pages for, in read order. */
  documents: ReadDocument[];
  /** How many documents the engine SAID it read, from the folded filename. */
  reported: number;
  /** Pages across every document, as reported. */
  totalPages: number;
  ocrPages: number;
  /** True when several documents were folded into one payload. */
  multi: boolean;
}

/** "first.pdf (+3 more)" → { name: "first.pdf", more: 3 }; else more = 0. */
const FOLDED = /^(.*) \(\+(\d+) more\)$/;
/** "[b.pdf] the page text" → name + rest. Only meaningful for a folded payload. */
const PREFIX = /^\[(.+?)\] /;

export function documentReadView(doc: DocumentActivity): DocumentReadView {
  const folded = FOLDED.exec(doc.filename ?? '');
  const reported = folded ? Number(folded[2]) + 1 : 1;
  const totalPages = doc.total_pages ?? 0;
  const ocrPages = doc.ocr_pages ?? 0;

  if (!folded) {
    // One document: its pages are its pages, exactly as they always were.
    return {
      documents: [{ name: doc.filename, pages: doc.pages ?? [] }],
      reported: 1,
      totalPages,
      ocrPages,
      multi: false,
    };
  }

  const documents: ReadDocument[] = [];
  for (const p of doc.pages ?? []) {
    const m = PREFIX.exec(p.text ?? '');
    // A page with no prefix in a folded payload cannot be attributed; keep it
    // under the first name rather than invent a document for it.
    const name = m ? m[1] : folded[1];
    const text = m ? p.text.slice(m[0].length) : p.text;
    const last = documents[documents.length - 1];
    if (last && last.name === name) last.pages.push({ page: p.page, text });
    else documents.push({ name, pages: [{ page: p.page, text }] });
  }
  return { documents, reported, totalPages, ocrPages, multi: true };
}
