# TechSara frontend

Next.js 15/React 19 user interface for the TechSara local Salesforce analytics
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

- Next.js 15 App Router with standalone output;
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

## Local identity and history

This application has no sign-in, sign-up, session cookie, or route-gating
flow. `/api/auth/me` returns a stable single local identity used for labeling
and history cache scoping. This matches the supported loopback, single-user
deployment; it is not suitable as public application authentication.

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

The Vitest suite matches `tests/**/*.test.ts`. It covers state and wire
contracts, not mounted React components or browser end-to-end behavior. No
current pass count is claimed here because the frontend suite was not rerun in
the portable-runtime documentation pass.

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
