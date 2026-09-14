// Preflight for every npm script (2026-09-13):
// * openai-node 7.x declares engines.node >= 22 and uses APIs Node 20 lacks, so
//   fail with one clear line instead of an obscure import error;
// * the table reporter writes into results/, which is git-ignored and therefore
//   absent on a fresh clone, and node:test does not create it.
import { mkdirSync } from 'node:fs';

const major = Number(process.versions.node.split('.')[0]);
if (major < 22) {
  console.error(`conformance/node needs Node.js >= 22 (openai-node 7.15.0); this is ${process.version}`);
  process.exit(1);
}
mkdirSync(new URL('../results/', import.meta.url), { recursive: true });
