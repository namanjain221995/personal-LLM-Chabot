# TechSara frontend

Next.js 16/React 19 user interface for the TechSara local Salesforce analytics
and chat platform. The normal full-platform entrypoint is `../techsara`; the npm
commands on this page are for frontend development and verification.

## Runtime model

The frontend does not select a hard-coded model. The launcher publishes the
selected backend/model/capability contract to the orchestrator, and the UI
offers four effort ceilings for that serving model:

- **Fast** — direct answer, no reasoning pass or tools;
- **Low** — no reasoning pass, with web search only when needed;
- **Medium** — reasoning plus model-driven multi-step planning/search;
- **High** — longer reasoning with the same tool surface as Medium.

There is no separate Agent toggle. At Medium/High effort the orchestrator's
model decides whether a request needs a plan. A degraded hardware profile may
hide or disable behavior its probed backend does not support.

## Stack

- Next.js 16 App Router with standalone output (`next ^16.3.3`);
- React 19 and TypeScript;
- Tailwind CSS 3 with dark/light TechSara tokens;
- Apache ECharts through `echarts-for-react`;
- `react-markdown`, GFM, syntax highlighting, and Mermaid;
- self-hosted IBM Plex Sans and JetBrains Mono through `@fontsource`;
- Vitest in a Node environment for pure contract/state modules.

## Streaming contract

`lib/sse.ts` is a small streaming parser for the orchestrator's custom SSE
events. It understands token, reasoning, status, research, step, metadata,
done, and error frames and ignores unknown event types. `lib/streams.ts`
maintains generation/reattachment state, persists final metadata, and handles
abort/stop behavior.

The Next.js API routes proxy the browser contract to the orchestrator. Report
and history proxies use explicit path/method allowlists rather than open
passthrough behavior.

## Identity and history

This application HAS sign-in and route-gating. `app/login/page.tsx` is the
sign-in page, `middleware.ts` decides whether a request is let through or
bounced to `/login`, and every `/api/*` proxy forwards the `ts_session` cookie
upstream — validity is the orchestrator's decision, not the frontend's.
`/api/auth/me` proxies to `/auth/me` and passes the status through honestly,
401 included; it does not synthesise a local identity. `MOCK_MODE=true` is the
only path that still answers from canned data.

(This section described the pre-retrofit frontend — no sign-in, no session
cookie, a stable single local identity — until 2026-09-28. It was already
false when the enterprise auth retrofit landed.)

Conversation history is server-backed. The browser keeps a synchronous
in-memory mirror persisted write-behind as one IndexedDB record per
conversation. On first boot after the cache migration, the old localStorage
blob is imported and deleted. Browsers without usable IndexedDB fall back to
the legacy localStorage persister with its bounded quota/eviction behavior.

Writes update the cache immediately and synchronize to the orchestrator.
Conflict/truncation rules prevent stale clients from shrinking conversation
history; explicit regenerate is the sanctioned truncation path. Pin/archive,
search, feedback, export, generated titles, detached-stream reattachment, and
dirty retry all share this store.

## Environment

| Variable | Meaning | Default/source |
|---|---|---|
| `ORCHESTRATOR_URL` | server-side proxy destination | `http://orchestrator:8080` in Compose; `http://localhost:8080` in route fallback |
| `MOCK_MODE` | `true` serves local canned chat/auth/history behavior for UI development | `false` in `.env.example` |
| `NEXT_PUBLIC_APP_NAME` | application name shown in the document/UI | `TechSara AI` |

In the launcher flow, Compose reads `.runtime/generated.env` for the frontend
and sets `ORCHESTRATOR_URL` explicitly. Do not put model endpoints or model IDs
in frontend configuration.

## Development commands

```bash
cd frontend
npm ci

npm run dev                 # local Next.js development server
MOCK_MODE=true npm run dev  # UI-only demo without the orchestrator/models
npm test                    # vitest run, Node environment
npx tsc --noEmit            # TypeScript check
npm run lint                # package script; verify toolchain support
npm run build               # production standalone build
```

`package-lock.json` is committed; use `npm ci` rather than generating a new
dependency resolution for verification.

The Vitest suite matches `tests/**/*.test.ts` and `tests/**/*.test.tsx`
(`vitest.config.mts`). The `.ts` half covers state and wire contracts in the
node environment; the `.test.tsx` files opt into jsdom with a
`// @vitest-environment jsdom` docblock and do mount React components. Browser
end-to-end behavior is still out of scope. Measured on this branch on
2026-09-28: `182 passed (182)` files, `3531 passed | 11 skipped (3542)` tests.

## Layout

| Area | Key files |
|---|---|
| App/API | `app/page.tsx`, `app/api/chat/*`, `app/api/history/[...path]`, `app/api/auth/me`, `app/api/upload` |
| Shell/composer | `components/ChatApp.tsx`, `Sidebar.tsx`, `Composer.tsx`, `ModelPicker.tsx` |
| Streaming/reasoning | `lib/sse.ts`, `lib/streams.ts`, `ReasoningAccordion.tsx`, `AgentTimeline.tsx` |
| History | `lib/history.ts`, `historyApi.ts`, `historyRoutes.ts`, `idbCache.ts` |
| Proof/data | `ProofDrawer.tsx`, `DataTable.tsx`, `EChart.tsx`, `MermaidBlock.tsx`, citations/source components |
| Tests | `tests/*.test.ts`, configured by `vitest.config.mts` |

For platform startup, profiles, data preservation, and security boundaries,
see [`../docs/PORTABLE-RUNTIME.md`](../docs/PORTABLE-RUNTIME.md).

## Initial setup for frontend changes

Install into **your own** working copy, and only into it:

```bash
cd <your checkout>/frontend
npm ci
```

Never run `npm ci` or `npm install` in the deploy root: it is a shared
production checkout that several engineers work in at once, and the images
running on this box are built from it.

Do not seed `node_modules` by copying it out of another checkout unless you
run `npm ci` afterwards. A copied tree is only as fresh as the day that
checkout was last installed, and nothing about it looks wrong. On 2026-09-27
the deploy root's tree was from 2026-09-09 — nine days older than its own
`package.json` — so it was missing `remark-breaks`, which was added on
2026-09-18 and is imported at module scope by `components/Markdown.tsx`. The
suite answered with 39 of 181 files failing on `Failed to resolve import
"remark-breaks"`, and, worse, collected 2790 tests instead of 3523 while still
printing a tidy summary. (3523 is what that branch point collects with a clean
tree; 3542 is the total once this guard's own tests are counted.)

`npm run check:deps` answers that question on its own: it compares every
`dependency` and `devDependency` against what is installed and names what is
missing or out of range, with the command that fixes it. The same check runs
as Vitest's `globalSetup` (`scripts/check-node-modules.mjs`), so `npm test`
stops with that one message rather than blaming the branch you are on.
