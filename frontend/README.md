# TechSara frontend

ChatGPT-class Next.js UI for the TechSara Local AI Analysis Platform
(spec §9), speaking the §10 SSE contract to the orchestrator.

## Stack

- Next.js 15 (App Router, `output: 'standalone'`) + React 19 + TypeScript
- Tailwind CSS 3 with the §9 design tokens as CSS variables
  (dark theme primary, light theme via `html.light`)
- Recharts 3 for the proof-drawer charts
- react-markdown + remark-gfm for assistant messages
- Self-hosted fonts via `@fontsource` (IBM Plex Sans, JetBrains Mono) —
  zero runtime CDN requests
- Vitest for the SSE parser and history-module tests

## Streaming choice (per §9 "Tech")

We use a **small hand-rolled SSE reader** (`lib/sse.ts`), not the Vercel AI
SDK. The orchestrator's custom `meta` event (sql / data / chart / citations
/ report_files) does not map cleanly onto the AI SDK's data-stream
protocol; the spec-compliant parser is ~60 lines and fully unit-tested,
including events split across network chunks.

## Environment

| Variable | Meaning | Default |
| --- | --- | --- |
| `ORCHESTRATOR_URL` | Orchestrator base URL for `/chat` + `/reports` proxying | `http://localhost:8080` |
| `MOCK_MODE` | `true` streams canned §10 fixtures (`lib/fixtures.ts`), one per engine, so the UI demos with no models | unset (proxy) |
| `NEXT_PUBLIC_APP_NAME` | Header / title branding | `TechSara AI` |

## Commands

```bash
npm install
npm run dev        # local dev (try MOCK_MODE=true npm run dev)
npm run lint       # eslint (next/core-web-vitals + next/typescript)
npx tsc --noEmit   # typecheck
npm test           # vitest run (SSE parser + history)
npm run build      # production build (standalone)
```

## Layout

- `app/api/chat/route.ts` — SSE endpoint: MOCK_MODE fixtures with realistic
  token pacing, or a byte-for-byte pipe of the orchestrator stream
- `app/api/reports/…` — list + sanitized download proxy
- `lib/history.ts` — server-backed history (V2 §4b) behind the original
  v1 interface: localStorage is the offline cache, writes push to the
  orchestrator `/history` API in the background (dirty-retry on refresh),
  one-time migration uploads pre-auth local conversations after first
  login; QuotaExceeded still drops the oldest conversation and toasts
- `components/ProofDrawer.tsx` — the signature element: engine badge +
  View SQL / Sources / Data / Chart / Files sections

## V2 additions (V2-DESIGN §4)

- **Auth (§4a)** — `/login` (Sign in / Create account tabs, inline errors),
  `middleware.ts` gates everything except `/login` + static assets +
  `/api/auth/*` on the presence of the `ts_session` cookie (validity is the
  orchestrator's job): pages redirect to `/login`, non-auth `/api/*` routes
  get 401 JSON so unauthenticated clients cannot reach `/api/chat` or
  `/api/reports/*`,
  `app/api/auth/[...path]` + `app/api/history/[...path]` proxy cookies BOTH
  directions, sidebar footer shows the user menu with Log out.
- **Composer controls (§4c)** — Salesforce toggle pill (ON default, per-
  conversation persistence in `lib/prefs.ts`; OFF switches the placeholder
  to "Ask anything…" and dims the trust footer), model picker
  (Smart · GPT-OSS 120B / Fast · Qwen3 4B with a Low/Medium/High reasoning
  submenu under Smart) and the Agent toggle. `mode`/`model`/`effort`/`agent`
  ride on every `/api/chat` call (V2 §1).
- **Reasoning UI (§4d)** — `reasoning` SSE deltas render a "Thinking…"
  shimmer accordion with a live last-line preview, collapsing to
  "Thought for N s" (client-measured); the text persists via
  `meta.reasoning` / `meta.reasoning_seconds`.
- **Agent timeline (§4e)** — `step` SSE events drive a live plan card
  (spinner / check / cross, expandable details), persisted via `meta.steps`.
- **SSE v2 (§2)** — `lib/sse.ts` parses `reasoning` + `step`; UNKNOWN event
  types are ignored without breaking the stream (unit-tested; the mock
  stream even emits a `ping` event to prove it).
- **MOCK_MODE** — also mocks `/api/auth` + `/api/history` in-memory
  (`lib/mockApi.ts`) and adds `chat`/`agent` fixtures with reasoning and
  step animation, so the entire v2 flow (login → migrate → chat) demos
  with no backend.
