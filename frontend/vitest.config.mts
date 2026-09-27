import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

/**
 * Two kinds of test live here.
 *
 * `.test.ts`  — pure logic (parsers, contracts, state machines) in the node
 *               environment. This is the bulk of the suite by design: behaviour
 *               that can be tested without a DOM is faster and far more precise
 *               to test that way, which is why so much of the app's logic lives
 *               in `lib/` rather than inside components.
 *
 * `.test.tsx` — component behaviour that only exists in a DOM: focus, roving
 *               tabindex, ARIA wiring, reduced motion. Each of those files opts
 *               into jsdom with a `// @vitest-environment jsdom` docblock, so
 *               the node default stays cheap for everything else.
 */
export default defineConfig({
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('.', import.meta.url)),
    },
  },
  // Next.js compiles JSX with the automatic runtime (tsconfig `jsx: preserve`
  // plus its own transform), so component files never import React. Vitest has
  // its own esbuild and defaults to the classic transform, which would need an
  // import that does not exist in the source.
  esbuild: { jsx: 'automatic' },
  test: {
    include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'],
    environment: 'node',
    // Refuse to run against a node_modules that disagrees with package.json.
    //
    // This runs once, before the first file is collected, and it exists
    // because the alternative is what actually happened: a tree copied from
    // the deploy root was missing `remark-breaks`, and the suite answered
    // with 39 `Failed to resolve import` errors in files that have nothing to
    // do with markdown — while quietly collecting 2790 tests instead of 3523
    // and still printing a tidy summary. One accurate message up front is
    // worth more than 39 misleading ones, and far more than a green run that
    // skipped a fifth of the suite. See scripts/check-node-modules.mjs.
    globalSetup: ['./scripts/check-node-modules.mjs'],
  },
});
