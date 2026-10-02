/**
 * The id of the build this server is running — SERVER ONLY (it reads a file).
 * STORE-ALWAYS §3, 2026-10-03.
 *
 * Why it exists: a tab opened before a deploy kept running the old JavaScript.
 * On 2026-10-03 such a tab sent a photo 14 minutes after the deploy that
 * started storing photos, and the old code neither sent its id nor wrote the
 * reference. Every page now carries the id of the build that served it
 * (app/layout.tsx), and GET /api/version answers the id of the build serving
 * NOW; the chat page compares the two and reloads when they differ.
 *
 * Where it comes from: `next build` writes `.next/BUILD_ID`, and Next lists
 * that file among the standalone server's required files, so the production
 * image (frontend/Dockerfile copies `.next/standalone` to /app) has it at
 * /app/.next/BUILD_ID. The standalone `server.js` chdirs to its own folder
 * and `next start` runs from the project, so it is read from the working
 * directory either way. Read once per process: a new build is a new process.
 *
 * null outside production (`next dev` has no build to name) and when the
 * file cannot be read — which turns the client's check off, never on.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';

/** What Next writes (a nanoid), bounded so a page can carry it as-is. */
const BUILD_ID_SHAPE = /^[A-Za-z0-9_-]{1,64}$/;

let cached: string | null | undefined;

/** The build id under `dir`, or null. */
export function readBuildId(dir: string): string | null {
  try {
    const id = readFileSync(path.join(dir, '.next', 'BUILD_ID'), 'utf8').trim();
    return BUILD_ID_SHAPE.test(id) ? id : null;
  } catch {
    return null;
  }
}

/** This process's build id, or null outside production. */
export function serverBuildId(): string | null {
  if (cached === undefined) {
    cached = process.env.NODE_ENV === 'production' ? readBuildId(process.cwd()) : null;
  }
  return cached;
}
