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
  },
});
