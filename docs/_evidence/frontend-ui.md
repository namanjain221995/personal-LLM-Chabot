# Evidence — frontend-ui

Scope: every `.tsx` under `frontend/components/` (**32 files**, not 35 as the assignment stated — verified
with `find frontend -name '*.tsx' -not -path '*/node_modules/*'`; the only other `.tsx` in the tree are
`frontend/app/layout.tsx` and `frontend/app/page.tsx`, read here as context), plus the 14 assigned
`frontend/lib/*.ts` modules and 10 config/build files.

All line numbers below were produced by the Read tool (`cat -n` numbering) on the file as it exists on disk
at the time of reading. Files outside the assignment that were read to verify a claim are marked
**(context, read in full)**: `frontend/lib/types.ts`, `frontend/lib/history.ts`, `frontend/lib/streams.ts`,
`frontend/lib/historyApi.ts`, `frontend/lib/sse.ts` (NOT read — see below), `frontend/lib/errors.ts`,
`frontend/lib/auth.ts`, `frontend/lib/contextMeter.ts`, `frontend/lib/proxy.ts`, `frontend/app/layout.tsx`,
`frontend/app/page.tsx`.

**UNVERIFIED — not read**: `frontend/lib/sse.ts`, `frontend/lib/orchestrator.ts`,
`frontend/app/api/**/route.ts` (except the first 60 lines of `app/api/chat/route.ts`),
`frontend/tests/**`, `frontend/README.md`, `frontend/public/**`.

Aggregate LOC for the assigned set: **9,113** (components 5,618 · assigned lib 2,766 · config 729).

---

## Cross-cutting verified facts (used repeatedly below)

1. **The orchestrator URL is NOT exposed to the client.** `rg -n "NEXT_PUBLIC"` over the whole repo returns
   exactly four hits: `docker-compose.yml:350`, `frontend/app/layout.tsx:15`, `frontend/README.md:31`,
   `frontend/components/ChatApp.tsx:68` — all `NEXT_PUBLIC_APP_NAME`. The orchestrator base URL is read
   server-side only, in `frontend/lib/proxy.ts:10` (`process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080'`).
   The browser only ever talks to same-origin `/api/*`.
2. **Markdown is NOT an XSS surface.** `frontend/components/Markdown.tsx:77` mounts `ReactMarkdown` with
   `remarkPlugins={[remarkGfm]}` and **no** `rehype-raw`, no `rehypePlugins` at all. In the installed
   `react-markdown@10.1.0` (`package-lock.json:8487-8489`), `node_modules/react-markdown/lib/index.js:359-368`
   converts every `raw` (HTML) node to a **text** node, and `:373-385` runs `urlTransform`
   (default `defaultUrlTransform`, `:421-438`, allowlist `/^(https?|ircs?|mailto|xmpp)$/i` at `:124`) over
   every URL attribute **before** the custom `a` component at `Markdown.tsx:67-71` receives `href`.
3. **`javascript:` hrefs are blocked by React itself.** `react-dom@19.2.8`
   (`node_modules/react-dom/package.json:3`) rewrites them —
   `node_modules/react-dom/cjs/react-dom-client.development.js:3167-3168`
   (`"javascript:throw new Error('React has blocked a javascript: URL as a security precaution.')"`).
   So `CitationChips.tsx:15`, `WebSources.tsx:22`, `ResearchPanel.tsx:137` rendering backend-supplied
   `href` values is not script execution.
4. **Mermaid output IS sanitized.** `MermaidBlock.tsx:58` sets `securityLevel: 'strict'`; in the installed
   `mermaid@11.16.0` (`package-lock.json:7013-7015`),
   `node_modules/mermaid/dist/mermaid.core.mjs:1326-1332` runs
   `DOMPurify.sanitize(svgCode, {ADD_TAGS: ["foreignobject"], ADD_ATTR: ["dominant-baseline"], …})`
   for every non-`loose`, non-`sandbox` security level. `dompurify@3.4.12`
   (`package-lock.json:4439-4441`). The two `dangerouslySetInnerHTML` sites
   (`MermaidBlock.tsx:311`, `MermaidBlock.tsx:420`) therefore receive sanitized markup.
5. **No CSP, no security headers.** `frontend/next.config.mjs:1-8` defines only
   `output`, `reactStrictMode`, `poweredByHeader`. There is no `headers()`, no `img-src`, no
   `frame-ancestors`. This is what turns fact (2) from "safe" into "safe against script, open to
   egress" — see Findings.
6. **No test covers any component.** `frontend/vitest.config.mts:4-7` — `include: ['tests/**/*.test.ts']`,
   `environment: 'node'`. `.test.tsx` is not matched and there is no DOM environment, so all 5,618 LOC of
   `components/` are untested. `ls frontend/tests/` shows 16 `.ts` files, zero `.tsx`.
7. **No TODO/FIXME/HACK/XXX markers** anywhere under `frontend/` (excluding `node_modules` and
   `package-lock.json`) — verified with `rg -n "TODO|FIXME|HACK|XXX"`.

---

# COMPONENTS

### frontend/components/ChatApp.tsx  (916 LOC)
**Purpose** — The single god component: the whole chat shell (sidebar + header + thread + composer),
owner of streaming state, server-backed history, per-conversation prefs, keyboard shortcuts.

**Public surface**
- `export function ChatApp()` — `ChatApp.tsx:70`. No props.
- `const APP_NAME` — `ChatApp.tsx:67-68`, `process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI'`.
- module-local `useIsomorphicLayoutEffect` — `ChatApp.tsx:22-23`.

**Control flow**
1. 14 `useState` hooks declared `ChatApp.tsx:71-111`; 6 `useRef` `:112-123`. Refs `messagesRef`,
   `activeIdRef`, `prefsRef`, `serverActiveRef` are assigned **during render** (`:117`, `:119`, `:121`, `:123`).
2. Pre-paint layout effect `:96-100` sets `reconciling=true` when the URL carries `?c=`, so the composer is
   never briefly interactive during a restore.
3. Mount effect `:141-242`: registers the eviction toast listener (`:142-148`), reads `?c=` and restores the
   cached thread + prefs (`:153-160`), collapses the sidebar under 768 px (`:162-164`), then the async IIFE
   `:167-238`: `fetchMe()` (`:175`) → on failure only `fetchServerActive()` (`:182`) and unlock (`:185`);
   on success `store.setActiveUser` (`:188`) → `migrateLocalConversations()` (`:190`) →
   `store.refresh()` (`:199`) → `fetchServerActive()` (`:210`) → either `attachStream(wanted)` (`:215`) or
   `store.load(wanted,{force})` (`:230`). `settleReconcile()` always runs in `finally` (`:235-237`).
4. Stream mirror effect `:254-273`: `subscribeStreams` callback bumps `setStreamTick` (`:256`) for **every**
   notification of **every** conversation, then, only for the open chat (`:267`), `setMessages([...s.messages])`
   and `setStreaming(...)` (`:269-271`).
5. Server-active poll `:278-306`: `tick()` skips while `document.hidden` (`:281`), `fetchServerActive()`
   (`:282`), and force-reloads the open chat when its detached generation finished (`:286-298`).
   `window.setInterval(..., 8000)` at `:301`.
6. `send` `:391-476`: creates the conversation when needed (`:394-404`), builds the user `ChatMessage`
   (`:408-420`) — note `imageDataUrl: attachment?.dataUrl` at `:412-413` — remembers the raw payload in
   memory (`:423-429`), `persist()` (`:431`), then either the dataset branch
   (`POST /api/upload`, `:436-464`) or `startStream(...)` (`:466-473`).
7. `runRegenerate` `:479-535`: locates the preceding user turn (`:486-489`), recovers the attachment
   (`:493`), calls `truncateMessages` when later turns exist (`:507-522`), then `startStream` (`:525-532`).
8. `regenerate` `:542-555` gates on `messagesDiscardedByRegenerate` and opens `ConfirmDialog`.
9. Keyboard effect `:728-760` delegates to `shortcutAction` and dispatches the five actions `:741-756`.
10. Render `:771-915`: `Sidebar` (`:773`), `SummaryPanel` (`:791`), `ConfirmDialog` (`:797`),
    `SearchPalette` (`:813`), header (`:822-836`), unreachable banner (`:838-857`), scroll container
    (`:859-880`) mapping `MessageRow` (`:868-877`), jump-to-latest (`:882-893`), `Composer` (`:895-912`).

**State & side effects**
- localStorage (via `lib/prefs` and `lib/history`): `savePrefs` `:388`, `:599`; `removePrefs` `:668`;
  `loadPrefs` `:157`, `:609`; `adoptDraftPrefs` `:402`; every `getHistoryStore()` mutation
  (`create` `:397`, `saveMessages` via `persist` `:246`, `rename` `:658`, `remove` `:667`,
  `setPinned` `:685`, `setArchived` `:694`, `truncateMessages` `:509`).
- Network egress (all same-origin `/api/*`): `POST /api/chat/compact` `:348`;
  `POST /api/upload` (multipart) `:444`; indirectly `POST /api/chat`, `GET /api/chat/attach/{id}`,
  `POST /api/chat/stop`, `GET /api/chat/active` through `lib/streams`, and `/api/auth/me`,
  `/api/history/*` through `lib/auth` / `lib/history`.
- Global mutation: `window.history.replaceState` `:127`; `window.setInterval` `:301`;
  `window.setTimeout` `:338`; `window.addEventListener('keydown')` `:758`.
- Filesystem: browser download of the exported Markdown via `downloadMarkdown` `:718`.
- GPU/model: none directly; all model work is behind `/api/chat`.
- Env reads: `process.env.NEXT_PUBLIC_APP_NAME` `:68` (build-time inlined).

**Dependencies**
- Inbound: `frontend/app/page.tsx:1` only (verified with rg).
- Outbound: `@/lib/auth` `:24`, `@/lib/exportMarkdown` `:25`, `@/lib/history` `:26`, `@/lib/prefs` `:27-34`,
  `@/lib/attachments` `:35`, `@/lib/searchPalette` `:36`, `@/lib/streams` `:37-47`, `@/lib/contextMeter` `:48`,
  `@/lib/types` `:49-54`, and components `Composer` `:55`, `ConfirmDialog` `:56`, `ContextMeter` `:57`,
  `SummaryPanel` `:58`, `EmptyState` `:59`, `EngineBadge` `:60`, `MessageRow` `:61`, `SearchPalette` `:62`,
  `Sidebar` `:63`, `Providers(useToast)` `:64`, `icons` `:65`.

**Config** — `NEXT_PUBLIC_APP_NAME` at `ChatApp.tsx:68`.

**Failure modes**
- `compactNow` `:346-383` — bare `catch {}` at `:378` collapses every failure (parse error, 4xx, 5xx) into one
  toast; `res.json()` is awaited **before** `res.ok` is checked (`:358` vs `:363`), so a non-JSON 502 throws
  into the same catch. No timeout, no `AbortController`, no retry.
- The dataset upload `:439-462` swallows everything into a toast and **still** starts the stream in `finally`
  (`:455-461`) — a failed profiling run silently produces an answer without the dataset.
- `migrateLocalConversations` failure is swallowed with a comment-only `catch {}` `:196-198`.
- `truncateMessages` failure `:511-520` reloads server truth and aborts the regenerate (correct).
- No timeout/abort on `fetch('/api/chat/compact')` `:348` or `fetch('/api/upload')` `:444` — a hung
  orchestrator leaves `compacting` true forever only if the promise never settles (the `finally` at `:381`
  otherwise clears it).
- `handleDraftChange` `:336-339` schedules `window.setTimeout` that is never cleared on unmount.

**Concurrency**
- Async: mount IIFE `:167`, poll `tick` `:280`, `compactNow` `:346`, dataset upload `:439`,
  `runRegenerate` `:479`, `exportConversation` `:712`.
- Cancellation is by boolean flag only (`cancelled` `:166`, `stopped` `:279`) — the in-flight `fetch`
  is never aborted, so a slow `/api/chat/active` can still resolve after unmount (guarded, but the socket
  stays open).
- Shared mutable module state lives in `lib/streams` (`streams` Map, `listeners` Set) and
  `lib/history` (`browserStore` singleton), both mutated from here.
- Race window: `selectConversation` `:604-654` fires `attachStream(id)` / `store.load(id)` and only
  re-checks `activeIdRef.current` when the promise resolves (`:628`, `:631`, `:643`) — two rapid
  conversation switches can interleave, but the id check makes the loser a no-op.
- Race window: the 8 s poll `:286-298` and `selectConversation` can both call
  `store.load(id, {force:true})` concurrently; `loadConversation` in `lib/history.ts:542-591` is not
  serialized, so the second write wins.

**Complexity hotspots**
- `ChatApp` itself: `ChatApp.tsx:70-916` = **847 LOC**, 14 `useState`, 6 `useRef`, 5 `useEffect`,
  1 `useLayoutEffect`, 14 `useCallback`. The largest function in the assigned set.
- `send` `:391-476` = 86 LOC. `runRegenerate` `:479-535` = 57 LOC. The mount effect `:141-242` = 102 LOC.
  `selectConversation` `:604-654` = 51 LOC.

**Notable**
- Magic numbers: `8000` ms poll `:301`; `300` ms draft debounce `:338`; `80` px "at bottom" threshold `:332`;
  `767 px` mobile breakpoint `:162`, `:649`; `52px` header height `:822`.
- Duplication: the "re-send this turn with its attachment" block is written twice —
  `runRegenerate` `:493-500`+`:525-532` and `retryLastTurn` `:568-585`.
- Duplicated comment (copy-paste) at `:371-372` and `:374-375` — the same sentence twice.
- Dead-ish: `onRegenerate` and `onRetry` are wired to the same handler `:873-874`.
- No TODO/FIXME.

### frontend/components/Markdown.tsx  (82 LOC)
**Purpose** — Renders assistant message text as GFM markdown, routing ```mermaid fences to `MermaidBlock`.

**Public surface**
- `export const Markdown = memo(function Markdown({ text }: { text: string }))` — `Markdown.tsx:74`.
- module-local `extractText(node: ReactNode): string` `:15`; `CodeBlock({children})` `:26`;
  `const components: Components` `:55-72`.

**Control flow**
1. `Markdown` `:74-82` wraps `<div className="md">` around `ReactMarkdown` with `remarkGfm` and the
   `components` map (`:77`).
2. `components.pre` `:56` delegates to `CodeBlock`.
3. `CodeBlock` `:26-53` reads the language from the child `<code>`'s `className` via
   `/language-([\w-]+)/` `:31`, flattens children to text `:33`, and returns `<MermaidBlock>` when
   `isMermaidLanguage(language)` `:36-38`; otherwise a bordered `<pre><code>` with a `CopyButton` `:40-52`.
4. `components.a` `:67-71` forces `target="_blank" rel="noopener noreferrer"`.

**State & side effects** — none. Pure render.

**Dependencies**
- Inbound: `components/MessageRow.tsx:13` (only).
- Outbound: `react-markdown` `:9`, `remark-gfm` `:10`, `@/lib/mermaid` `:11`, `./CopyButton` `:12`,
  `./MermaidBlock` `:13`.

**Config** — none.

**Failure modes** — `extractText` `:15-24` recurses through arbitrary React children with no depth guard;
a pathological nesting would blow the stack (not reachable from markdown output in practice).
`memo` compares only `text` — a changed `components` identity is irrelevant because `components` is
module-level (`:55`).

**Concurrency** — sync.

**Complexity hotspots** — none > 60 LOC.

**Notable** — **`img` is not in the `components` map and there is no `disallowedElements`/`allowedElements`**
(`:55-72`), so a model-authored `![](https://host/x.png)` renders a real `<img>` and the browser fetches it.
Combined with the absent CSP (`next.config.mjs:1-8`) this is the egress hole described in the Findings.

### frontend/components/MermaidBlock.tsx  (429 LOC)
**Purpose** — Renders a ```mermaid fence as a diagram with Code/Preview toggle, fullscreen zoom viewer,
copy, and PNG (SVG fallback) download.

**Public surface**
- `export function MermaidBlock({ code }: { code: string })` — `MermaidBlock.tsx:107`.
- module-local `type View = 'preview' | 'code'` `:43`; `let mermaidPromise` `:45`; `let renderSeq = 0` `:46`;
  `async function getMermaid(dark: boolean)` `:49`.

**Control flow**
1. Render effect `:120-145`: bails when `looksRenderable(code)` is false `:122-125`; otherwise
   `getMermaid(dark)` `:128`, `id = mmd-${renderSeq+=1}` `:129`, `await mermaid.render(id, code)` `:130`,
   `setSvg(out)` `:132`. Errors set `error` `:135-140`. Cleanup flips `cancelled` `:142-144`.
2. Auto-switch to preview `:148-150`. Escape closes fullscreen `:153-160`.
3. `downloadPng` `:162-224`: measures the live `<svg>` `:164-169`, `prepareSvgForExport` `:171`,
   Blob + object URL `:172-173`, rasterizes through an `Image` onto a 2× canvas `:188-215`,
   `canvas.toBlob` → `save()` `:201-207`; on any failure saves the SVG instead `:216-220`;
   always revokes the source URL `:221-223`.
4. Card render `:295-330`: header controls `:244-293`, preview via `dangerouslySetInnerHTML` `:306-312`,
   otherwise source `<pre>` `:313-329`.
5. Fullscreen portal `:332-426` into `document.body`, zoom buttons `:347-367`, fit `:368-375`,
   size = natural × zoom `:406-416`, second `dangerouslySetInnerHTML` `:417-421`.

**State & side effects**
- Global module state: `mermaidPromise` `:45` (single lazy import), `renderSeq` `:46` (monotonic id).
- Global config mutation: `mermaid.initialize({...})` is called on **every** `getMermaid` call `:54-103`,
  i.e. once per diagram per render pass — a process-wide setting mutated by each block.
- DOM: `document.createElement('a')` + `document.body.appendChild` + `click()` + `remove()` `:177-183`;
  `URL.createObjectURL` `:173`, `:177`; `revokeObjectURL` `:184` (10 s later) and `:222`.
- `document.addEventListener('keydown')` `:158`. `createPortal` to `document.body` `:334`, `:425`.
- Network egress: **none** — mermaid is bundled (`package.json:17`), no CDN.

**Dependencies**
- Inbound: `components/Markdown.tsx:13`.
- Outbound: `react-dom` `createPortal` `:19`, `@/lib/mermaid` `:20-29`, `./CopyButton` `:30`,
  `./Providers` `useTheme` `:31`, `./icons` `:32-41`, dynamic `import('mermaid')` `:51`.

**Config** — none.

**Failure modes**
- `catch` at `:135-140` shows a quiet error and the source — good.
- `downloadPng` `:216-220` swallows every rasterization failure and silently downloads an `.svg` instead of
  the promised `.png`; the user is not told the format changed.
- `img.onerror` `:213` rejects; no timeout — a `blob:` image that never loads leaves the promise pending
  forever and no file is produced (the `finally` at `:221` never runs).
- `suppressErrorRendering: true` `:62` prevents mermaid's DOM bomb.

**Concurrency**
- `getMermaid` is async; `mermaidPromise` guards a single import but **`mermaid.initialize` races**: N blocks
  on screen each call `initialize` with the current theme before `render`. Same theme ⇒ benign, but a theme
  toggle mid-render can configure one theme and render another.
- `renderSeq` `:46` is a shared mutable module counter — no id collisions, but concurrent
  `mermaid.render` calls share mermaid's internal temp-element handling.
- `cancelled` `:121` prevents state updates after unmount; the underlying render is not abortable.

**Complexity hotspots**
- `MermaidBlock` `:107-429` = **323 LOC** (largest component function after `ChatApp`).
- `getMermaid` `:49-105` = 57 LOC, dominated by the 28-key `themeVariables` literal `:73-102`.
- `downloadPng` `:162-224` = 63 LOC.

**Notable** — magic numbers: `480px` preview cap `:309`; `scale = 2` `:191`; `10_000` ms revoke delay `:184`;
`1.25` zoom step `:349`, `:361`; `window.innerWidth - 96` / `innerHeight - 140` fit insets `:235`;
`z-[60]` `:339`. 28 hard-coded hex colors `:73-102` duplicate the design tokens in `app/globals.css:13-81`
instead of reading them.

### frontend/components/ChartView.tsx  (92 LOC)
**Purpose** — Proof-drawer chart section: validate a `ChartSpec`, resolve the theme palette, build the
ECharts option, and render it behind an error boundary.

**Public surface**
- `export function ChartView({ spec, data }: { spec: ChartSpec; data: DataRow[] })` — `ChartView.tsx:81`.
- module-local `ChartUnavailable({reason})` `:39`; `const MESSAGES: Record<string,string>` `:43-52`;
  `ChartCanvas({spec,data})` `:54`.

**Control flow**
1. `ChartView` `:81-92` renders `<figure>`, optional `<figcaption>` from `spec.title` `:84-86`, then
   `ChartErrorBoundary` wrapping `ChartCanvas` `:87-89`.
2. `ChartCanvas` `:54-79`: `useTheme()` `:55`, palette state seeded with `fallbackPalette(theme)` `:60`,
   re-resolved to real CSS tokens in an effect `:61-63`.
3. `validateChart(spec, data)` on **every render** `:65`; `buildChartOption` memoized `:66-69`.
4. Problem → `ChartUnavailable` with the mapped message `:71-73`; null option → generic message `:74-76`;
   else `<EChart option height={300} ariaLabel={spec.title || 'Chart'} />` `:78`.

**State & side effects** — `useState` palette `:60`; `useEffect` `:61-63` calls
`resolvePalette` which reads `window.getComputedStyle(document.documentElement)`
(`lib/chartTheme.ts:87-103`). No network, no storage.

**Dependencies**
- Inbound: `components/ProofDrawer.tsx:15`.
- Outbound: `next/dynamic` `:26`, `@/lib/types` `:27`, `@/lib/chartOption` `:28`, `@/lib/chartTheme` `:29`,
  `./Providers` `:30`, `./ChartErrorBoundary` `:31`, dynamic `./EChart` `:34-37` (`ssr:false`).

**Config** — none.

**Failure modes** — a throw inside ECharts is caught by `ChartErrorBoundary` `:87`; `buildChartOption`
itself never throws (`lib/chartOption.ts:320-326` wraps in try/catch and returns null).

**Concurrency** — sync render + one effect.

**Complexity hotspots** — none.

**Notable** — `validateChart` is called outside `useMemo` (`:65`) and walks every row × every key
(`lib/chartOption.ts:99-116`) on every re-render; with the streaming re-render storm (see Findings) that is
O(rows) work per SSE token while a Data/Chart section is open. The 300 px height is hard-coded `:36`, `:78`.

### frontend/components/EChart.tsx  (103 LOC)
**Purpose** — The Apache ECharts canvas renderer, isolated so it is code-split and never SSR'd.

**Public surface**
- `export default function EChart({ option, height = 300, ariaLabel }: { option: EChartsOption; height?: number; ariaLabel?: string })` — `EChart.tsx:57-65`.
- module-local `type ChartInstance = { resize: () => void }` `:42`.

**Control flow**
1. Module scope registers exactly five series types and four components + `CanvasRenderer`
   via `echarts.use([...])` `:44-55`.
2. `useEffect` `:72-78` attaches a `ResizeObserver` on the wrapper that calls `chart.current?.resize()`.
3. Renders a `role="img"` wrapper `:80-87` and `ReactEChartsCore` with `notMerge` + `lazyUpdate`
   `:88-100`, capturing the instance in `onChartReady` `:97-99`.

**State & side effects** — `ResizeObserver` `:75-77` (disconnected on unmount `:77`). Canvas drawing only;
no network.

**Dependencies**
- Inbound: **dynamic only** — `components/ChartView.tsx:34` (`dynamic(() => import('./EChart'))`).
  `rg` finds no static import, which is the point.
- Outbound: `echarts-for-react/lib/core` `:23`, `echarts/core` `:24`, `echarts/charts` `:25-31`,
  `echarts/components` `:32-37`, `echarts/renderers` `:38`, `@/lib/chartOption` (type only) `:39`.

**Config** — none.

**Failure modes** — `typeof ResizeObserver === 'undefined'` guard `:74`. If `onChartReady` never fires,
resize is a no-op. No error handling around ECharts itself — that is `ChartErrorBoundary`'s job.

**Concurrency** — sync; the ResizeObserver callback is not debounced, so a drag-resize of the proof drawer
issues a `resize()` per animation frame.

**Complexity hotspots** — none.

**Notable** — `height` default `300` duplicated at `:59` and at the call site `ChartView.tsx:78`.

### frontend/components/ChartErrorBoundary.tsx  (57 LOC)
**Purpose** — The app's **only** React error boundary, scoped to one chart.

**Public surface**
- `export class ChartErrorBoundary extends Component<Props, State>` — `ChartErrorBoundary.tsx:28`.
- `interface Props { children: ReactNode; fallback?: ReactNode }` `:18-22`;
  `interface State { failed: boolean }` `:24-26`.
- `static getDerivedStateFromError(): State` `:31`; `componentDidCatch(error, info)` `:35`;
  `render()` `:47`.

**Control flow** — throw → `getDerivedStateFromError` sets `failed` `:31-33` → `render` returns
`this.props.fallback` or the default notice `:48-55`.

**State & side effects** — `console.error` only when `process.env.NODE_ENV !== 'production'` `:41-44`.

**Dependencies** — Inbound: `components/ChartView.tsx:31`. Outbound: `react` `:16`.

**Config** — `NODE_ENV` at `ChartErrorBoundary.tsx:41`.

**Failure modes** — **no reset path**: once `failed` is true it never returns to `false`
(`:29`, `:47-48`). A transient failure (e.g. a resize race) permanently blanks that chart until the
message unmounts. No telemetry by design `:36-40`.

**Concurrency** — sync.

**Complexity hotspots** — none.

**Notable** — the fallback copy at `:51-53` duplicates `ChartView.tsx:75` verbatim.

### frontend/components/DataTable.tsx  (147 LOC)
**Purpose** — Proof-drawer Data section: sortable HTML table over `DataRow[]` with a client-side CSV export.

**Public surface**
- `export function DataTable({ rows, truncated, csvName }: { rows: DataRow[]; truncated?: boolean; csvName: string })` — `DataTable.tsx:24-32`.
- module-local `type SortDir = 'asc' | 'desc'` `:13`; `compareValues(a, b)` `:15`.

**Control flow**
1. `columns` = union of every row's keys, memoized `:36-45`.
2. `sorted` `:47-51`: `[...rows].sort(compareValues)` then `.reverse()` for descending.
3. `toggleSort` `:53-60` flips direction on the same key, else selects the new key ascending.
4. Empty guard `:62-64`. Header row `:85-116` with `aria-sort` `:92-98` and a full-width sort `<button>`.
5. Body `:117-142`: numeric-looking cells right-aligned monospace `:122-130`; null/undefined → em dash `:132-134`;
   everything else `String(v)` `:135` (React-escaped, no HTML injection).

**State & side effects** — `downloadCsv(rows, csvName)` `:76` → Blob + object URL + synthetic anchor
click (`lib/csv.ts:27-39`). No network.

**Dependencies** — Inbound: `components/ProofDrawer.tsx:14`. Outbound: `@/lib/types` `:9`,
`@/lib/csv` `:10`, `./icons` `:11`.

**Config** — none.

**Failure modes** — none thrown. `Number(v)` coercion at `:20` and `:124` means numeric-looking **strings**
(Salesforce 18-char ids are alphanumeric, but zero-padded numeric codes are not) sort and align as numbers.

**Concurrency** — sync.

**Complexity hotspots** — none (`DataTable` `:24-147` = 124 LOC but almost entirely JSX).

**Notable**
- Descending sort is implemented as ascending + `.reverse()` `:49-50`, which inverts the relative order of
  equal keys — not a stable descending sort.
- `Number(true) === 1`, so a boolean cell renders right-aligned monospace `:122-124`.
- No virtualization: the orchestrator can send 500 rows (`truncated` copy at `:70-72`) and every one is a
  DOM `<tr>` re-rendered whenever `ChatApp` re-renders.
- `csvName` is hard-coded to `"techsara-data"` by the only caller (`ProofDrawer.tsx:147`), so every export
  overwrites the same filename.

### frontend/components/SearchPalette.tsx  (449 LOC)
**Purpose** — Ctrl/Cmd+K modal search over conversations; a thin rendering shell over `lib/searchPalette`.

**Public surface**
- `export interface SearchPaletteProps { open; onClose; recents: ConversationSummary[]; onSelect(id); onNewChat(); searchFn? }` — `SearchPalette.tsx:63-72`.
- `export function SearchPalette({...})` — `SearchPalette.tsx:78-85`.
- module-local `const FOCUSABLE` `:58-59`; `type SearchStatus` `:61`;
  `defaultSearch(query, signal)` `:74-76`.

**Control flow**
1. `runSearch` `:107-132`: aborts the previous controller `:109`, creates a new one `:110-111`,
   `setStatus('loading')` `:112`, awaits `searchFn` `:115`, parses via `parseSearchResults` `:117`;
   an abort is ignored `:118`/`:123`, any other error sets `status='error'` with empty results `:124-125`.
2. A single `createDebounce` instance is created lazily and kept for the palette's life `:139-147`,
   reading the latest `runSearch` through `runSearchRef` `:137-138`.
3. Query effect `:149-169`: closing cancels the debounce and aborts `:151-157`; an empty query resets
   `:158-167`; otherwise `debounce.run(trimmed)` `:168`.
4. Unmount cleanup `:172-179`.
5. Open effect `:183-195`: remembers `document.activeElement` `:185`, resets state `:187-190`, focuses the
   input with `preventScroll` `:191`, restores focus on close unless a row was activated `:192-194`.
6. Model `:199-213`: empty query → `resultsFromSummaries(recents)`; otherwise the latest server results;
   `highlighted` clamped `:213`.
7. Highlight scrolling touches **only** the list element `:217-231`.
8. `activate` `:235-244`; keyboard `:248-277` (Tab trap `:248-258`, `paletteKeyAction` dispatch `:265-276`).
9. Render `:281-448`: returns null when closed `:281`, otherwise a portal to `document.body` `:353`,
   backdrop `:357-361`, `role="dialog"` panel `:363-370`, combobox input `:377-395`,
   `role="listbox"` `:406-444`.

**State & side effects**
- Network: `searchConversations` → `GET /api/history/search?q=…` (`lib/historyApi.ts:237`).
- DOM: `createPortal(document.body)` `:353`, `focus({preventScroll:true})` `:191`, `:193`, `:256`;
  direct `list.scrollTop` writes `:227`, `:229`.
- `AbortController` per search `:110`.

**Dependencies** — Inbound: `components/ChatApp.tsx:62`. Outbound: `react-dom` `:38`,
`@/lib/historyApi` `:39`, `@/lib/searchPalette` `:40-53`, `@/lib/types` `:54`, `./icons` `:55`.

**Config** — none.

**Failure modes** — every search failure collapses to one inline line `:434-438`; the HTTP status is
discarded. No retry. `SEARCH_MAX_QUERY` (100) is enforced both by `maxLength` `:390` and
`normalizeQuery` `:103`.

**Concurrency** — one `AbortController` at a time `:94`, `:109`; superseded requests abort. The
`finally` at `:126-128` only nulls the ref when it is still the current controller — correct.

**Complexity hotspots** — `SearchPalette` `:78-449` = **372 LOC**; `renderRow` `:288-345` = 58 LOC.

**Notable** — `onMouseMove` on every row (`:299`, `:318`) calls `setActiveIndex` on each mouse-move event,
re-rendering the whole palette; there is no equality guard. Magic numbers: `12vh` top offset `:354`,
`70vh` max height `:369`, `640px` panel `:369`.

### frontend/components/Composer.tsx  (423 LOC)
**Purpose** — The pinned composer: auto-growing textarea, attachment handling (image/PDF/dataset), paste
capture, Salesforce toggle, effort picker, send/stop.

**Public surface**
- `export interface ComposerHandle { focus: () => void }` `:46-48`.
- `export interface Attachment { name; kind: 'image'|'pdf'|'dataset'; dataUrl; base64; file? }` `:50-63`.
- `export const Composer = forwardRef<ComposerHandle, ComposerProps>` `:96-97`.
- `interface ComposerProps { streaming; disabled?; meter?; onDraftChange?; prefs; onPrefsChange; onSend; onStop }` `:79-94`.
- Constants: `MAX_IMAGE_BYTES = 10*1024*1024` `:42`; `LINE_HEIGHT = 24` `:43`; `MAX_ROWS = 10` `:44`;
  `MAX_PDF_BYTES = 25*1024*1024` `:65`; `MAX_DATASET_BYTES = 200*1024*1024` `:68`;
  `DATASET_SUFFIXES` `:69-72`; `isDatasetName` `:74-77`.

**Control flow**
1. `useImperativeHandle` exposes `focus` `:118-120`; `autogrow` clamps height to 240 px `:122-129`,
   run on every text change `:131`.
2. `submit` `:137-146`: no-op while `streaming || disabled` `:139` or with no content `:140`;
   calls `onSend(trimmed, attachment, pastedTexts)` `:141` and clears local state `:142-145`.
3. `handleFile` `:148-203`: classifies image / PDF / dataset `:149-153`, rejects unknown types with a toast
   `:154-161`, enforces the per-kind size cap `:162-175`, keeps a dataset as a `File` handle only
   `:176-187`, otherwise `FileReader.readAsDataURL` `:192-202` and splits the base64 payload at the first
   comma `:199`.
4. `handlePaste` `:207-228`: an image item becomes the attachment `:210-220`; text past
   `shouldAttachPaste` becomes a `PastedChip` `:221-227`.
5. Render `:230-421`: attachment/pasted chip row `:233-279`, rounded container `:283-403` with textarea
   `:284-310` (Enter submits `:292-297`), hidden file input `:313-325`, paperclip `:326-335`,
   Salesforce pill `:338-353`, `ModelPicker` `:362-368`, meter + send/stop `:377-401`,
   trust footer `:405-418`.

**State & side effects**
- `FileReader` reads up to 25 MB (PDF) into a base64 data URL in memory `:192-202`.
- `toast()` via `useToast` `:116`.
- Direct DOM style writes on the textarea `:125-128`.
- `fileInputRef.current?.click()` `:328`; `e.target.value = ''` reset `:323`.
- No network, no storage — the parent owns both.

**Dependencies** — Inbound: `components/ChatApp.tsx:55`. Outbound: `@/lib/prefs` (type) `:23`,
`@/lib/pasted` `:24-28`, `@/lib/types` `:29`, `./ModelPicker` `:30`, `./PastedChip` `:31`,
`./Providers` `:32`, `./icons` `:33-40`.

**Config** — none.

**Failure modes**
- `FileReader` has **no `onerror` handler** `:192-202`: a read failure (permission, removed file) leaves the
  chip absent with no toast and no log.
- `accept="image/*,…"` `:316` plus `file.type.startsWith('image/')` `:149` admits `image/svg+xml`, which is
  then base64'd and shipped to the vision path. Rendered only inside `<img>` (`:243`, and
  `MessageRow.tsx:43`), where scripting is disabled — not XSS, but a guaranteed model failure.
- The 200 MB dataset cap `:68` is enforced client-side only.

**Concurrency** — `FileReader.onload` `:193` fires asynchronously and unconditionally calls
`setAttachment` — a second file picked while the first is still reading will have its result overwritten by
whichever `onload` lands last. `pasteSeq` ref `:115`, `:224` keeps paste ids unique.

**Complexity hotspots** — `Composer` `:97-423` = **327 LOC**; `handleFile` `:148-203` = 56 LOC.

**Notable**
- The trust footer `:415-417` states *"nothing leaves this machine"* when Salesforce mode is on — the
  claim the missing CSP / markdown-image hole undermines (see Findings).
- Two large blocks of removed-feature commentary `:355-360` and `:370-375` (dead documentation).
- Magic numbers: `26px` radius `:283`, `240px` max textarea `:308`, `200px`/`220px` name truncation
  `:250`, `10MB/25MB/200MB` caps `:42`, `:65`, `:68`.

### frontend/components/MessageRow.tsx  (232 LOC)
**Purpose** — Renders one chat message: user bubble (image/PDF/pasted chips) or the full-width assistant
row (reasoning, research, steps, markdown, notices, proof drawer, hover actions).

**Public surface**
- `export function MessageRow({ message, isLast, onRegenerate, onShowSummary, onRetry }: { message: ChatMessage; isLast: boolean; onRegenerate: () => void; onShowSummary?: () => void; onRetry: () => void })` — `MessageRow.tsx:21-34`.

**Control flow**
1. User branch `:35-80`: optional `<img src={message.imageDataUrl}>` `:43-47`, PDF chip `:50-66`,
   `PastedChip` per `meta.pasted` `:67-71`, then the bubble `:72-76`.
2. Assistant branch `:82-231`: reads reasoning/steps/research from the live message **or** persisted meta
   `:85-90`; computes `showShimmer` `:91-99`.
3. Search status line `:103-111`; shimmer `:112-117`; otherwise
   `ReasoningAccordion` `:120-126` → `ResearchPanel` `:128` → `AgentTimeline` `:130` →
   `Markdown` + caret `:132-137` → compaction button `:139-150` → trim notice `:152-157` →
   "Stopped" `:159-163` → error block with `friendlyError` `:165-203` → `ProofDrawer` `:205` →
   hover actions `:207-227`.

**State & side effects** — none (stateless function component, **not** `memo`ised).

**Dependencies** — Inbound: `components/ChatApp.tsx:61`. Outbound: `@/lib/types` `:10`,
`./AgentTimeline` `:11`, `./ResearchPanel` `:12`, `./Markdown` `:13`, `./PastedChip` `:14`,
`./ProofDrawer` `:15`, `./CopyButton` `:16`, `./ReasoningAccordion` `:17`, `@/lib/errors` `:18`,
`./icons` `:19`.

**Config** — none.

**Failure modes** — none thrown. `message.meta.input_trimmed` is passed to `trimNotice` without a
null check at `:155` (guarded by the `&&` at `:152`).

**Concurrency** — sync.

**Complexity hotspots** — `MessageRow` `:21-232` = 212 LOC, of which the assistant branch `:101-231`
is 131 LOC with ~10 conditional sub-sections. An IIFE is used to scope the error block `:166-203`.

**Notable** — not wrapped in `React.memo`, so every ChatApp re-render (once per SSE token) re-renders
every message row and everything under it. `ProofDrawer` at `:205` renders for **every** assistant message
that has meta, and its `DataTable` (up to 500 rows) re-renders with it once opened.

### frontend/components/ProofDrawer.tsx  (160 LOC)
**Purpose** — The signature "proof" bar under an assistant answer: engine badge + collapsible
SQL / Sources / Web sources / Code / Data / Chart / Files sections.

**Public surface**
- `export function ProofDrawer({ meta }: { meta: Meta })` — `ProofDrawer.tsx:36`.
- module-local `type SectionId` `:22-29`; `interface Section` `:31-34`.

**Control flow**
1. `sections` built imperatively from `meta` `:37-67`, with counts in the labels.
2. `chartRows = meta.chart_data?.length ? meta.chart_data : meta.data` `:61` (back-compat fallback).
3. `open` state initialized once — chart auto-opens, else files `:69-78`.
4. Early return when there is nothing to show `:80`.
5. `toggle` `:82-89`. Header `:93-123`. Panels `:125-157` dispatching to `SqlBlock` `:133`,
   `CitationChips` `:135`, `WebSources` `:138`, `CodeCitations` `:141`, `DataTable` `:144-148`,
   `ChartView` `:151`, `FileCards` `:154`.

**State & side effects** — `useState<Set<SectionId>>` only. No network, no storage.

**Dependencies** — Inbound: `components/MessageRow.tsx:15`. Outbound: `@/lib/types` `:11`,
`./EngineBadge` `:12`, `./SqlBlock` `:13`, `./DataTable` `:14`, `./ChartView` `:15`,
`./CitationChips` `:16`, `./WebSources` `:17`, `./CodeCitations` `:18`, `./FileCards` `:19`, `./icons` `:20`.

**Config** — none.

**Failure modes** — none thrown; unknown `meta.route` degrades in `EngineBadge` (`EngineBadge.tsx:63-64`).

**Concurrency** — sync.

**Complexity hotspots** — `ProofDrawer` `:36-160` = 124 LOC; the section-building block `:37-78` is 42 LOC
of straight-line conditionals.

**Notable** — two different section ids both render the label `Sources (n)` (`:40` for Salesforce citations,
`:44-46` for web sources); when a message has both, the drawer shows two identically-labelled buttons.
`csvName="techsara-data"` hard-coded `:147`. The `open` initializer `:69-78` runs once, so a message whose
meta arrives in two steps keeps the first computation.

### frontend/components/Sidebar.tsx  (325 LOC)
**Purpose** — 260 px conversation rail: pinned / recents / archived sections, inline rename, per-row
"⋯" menu, theme toggle; collapsible on desktop and a slide-over on mobile.

**Public surface**
- `interface SidebarProps { open; onClose; conversations; archived; activeId; streamingIds?; onNewChat; onOpenSearch; onSelect; onRename; onDelete; onSetPinned; onSetArchived; onExport; onLoadArchived }` — `Sidebar.tsx:33-54`.
- `export function Sidebar({...})` — `Sidebar.tsx:56-72`.

**Control flow**
1. Local state: `editingId`, `draftTitle`, `archivedOpen` `:73-75`; `archivedLoaded` ref `:76`.
2. `pinned` / `recents` partition `:79-80`.
3. `commitRename` `:82-85`; `toggleArchived` `:87-94` fires `onLoadArchived()` exactly once `:90-93`.
4. `row(c)` `:96-167`: inline `<input>` while editing `:99-114`, else the select button `:117-144`
   (spinner when `streamingIds.includes(c.id)` `:137-143`) plus the `ConversationMenu` `:145-162`
   wired through `conversationMenuHandlers` `:151-160`.
5. `body` `:169-291`: header `:171-191`, New chat `:193-205`, `<nav>` `:207-274` with Pinned `:219-229`,
   Recents `:231-243`, Archived disclosure `:245-273`, theme toggle `:276-289`.
6. Return `:293-324`: desktop `<aside>` `:296-304` **and** the mobile slide-over `:307-322` — both render
   `body`.

**State & side effects** — `toggleTheme()` from `useTheme` `:77`, `:279` (writes
`localStorage['techsara.theme']` in `Providers.tsx:74`). Nothing else.

**Dependencies** — Inbound: `components/ChatApp.tsx:63`. Outbound: `@/lib/conversationMenu` `:17`,
`@/lib/types` `:18`, `./ConversationMenu` `:19`, `./TechSaraMark` `:20`, `./Providers` `:21`, `./icons` `:22-31`.

**Config** — none.

**Failure modes** — `commitRename` `:83` passes the **untrimmed** `draftTitle` to `onRename`
(the store trims at `lib/history.ts:259-264`, so no data issue, only a redundant guard).

**Concurrency** — sync.

**Complexity hotspots** — `Sidebar` `:56-325` = **270 LOC**; `row` `:96-167` = 72 LOC;
the `body` expression `:169-291` = 123 LOC.

**Notable**
- **`body` is rendered twice whenever `open` is true** (`:303` and `:319`) — the desktop aside is only
  CSS-hidden (`hidden … md:block` `:297`) and the drawer is only CSS-hidden (`md:hidden` `:308`), so both
  subtrees are in the DOM simultaneously on every viewport. That duplicates the ids
  `sidebar-pinned` `:222`, `sidebar-recents` `:235`, `sidebar-archived-list` `:266`, and duplicates every
  row's `ConversationMenu`.
- `aria-hidden={!open}` on the desktop aside `:301` while the collapsed aside (`w-0` `:298`) still contains
  tabbable buttons — an `aria-hidden` subtree with focusable descendants.
- The Archived disclosure only renders when `archived.length > 0` `:245`, and `archived` is populated by
  `listArchived()` from the local cache (`ChatApp.tsx:135` → `lib/history.ts:237-239`); `onLoadArchived`
  (which pulls `?archived=true`) can only fire from inside that section `:250`. A conversation archived on
  another device is therefore invisible until some other code path caches it.

### frontend/components/ConversationMenu.tsx  (327 LOC)
**Purpose** — The per-row "⋯" popover (Rename · Pin · Archive · Export · Delete with inline confirm),
portalled and fixed-positioned.

**Public surface**
- `export interface ConversationMenuProps { title; pinned; archived; active?; onRename; onTogglePin; onToggleArchive; onExport; onDelete; onOpenChange? }` — `ConversationMenu.tsx:59-73`.
- `export function ConversationMenu({...})` — `ConversationMenu.tsx:98-109`.
- module-local `MENU_WIDTH = 208` `:50`; `useMeasureEffect` `:53-54`; `CONFIRM_FOCUS_INDEX = 1` `:57`;
  `itemIcon(id, pinned, archived)` `:75-96`.

**Control flow**
1. `items = conversationMenuItems({pinned, archived}, confirmingDelete)` `:119`.
2. `close(restoreFocus)` `:121-130`; `openMenu()` `:132-138`.
3. Placement measured before paint `:143-161` using `placeMenu` (`lib/conversationMenu.ts:208-236`).
4. Dismissal effect `:165-199`: `pointerdown` outside **both** menu and trigger `:167-176`,
   Escape `:177-179`, resize/scroll (capture) `:180-192`.
5. Roving focus `:202-207` with `preventScroll`.
6. `activate(id)` `:209-231` maps the pure outcome to state; rename and delete-confirm deliberately do not
   restore focus `:230`.
7. Keyboard `:233-249`. Render `:251-326`: trigger `:253-268`, portal `:275-324`.

**State & side effects** — `createPortal(document.body)` `:276`; document/window listeners `:188-192`
(removed on cleanup `:193-198`); `focus()` calls `:127`, `:206`.

**Dependencies** — Inbound: `components/Sidebar.tsx:19`. Outbound: `react-dom` `:28`,
`@/lib/conversationMenu` `:29-36`, `./icons` `:37-48`.

**Config** — none.

**Failure modes** — none thrown. `itemRefs.current` `:117` is never truncated when the item list shrinks
from 5 to 2 `:296-321`, leaving stale detached refs at indices 2–4 (harmless because `focusIndex` is reset).

**Concurrency** — sync + effects. Shared mutable state: none at module level.

**Complexity hotspots** — `ConversationMenu` `:98-327` = **230 LOC**.

**Notable** — this is the **correct** outside-click pattern (checks the portalled panel *and* the trigger,
`:169-175`) that `ContextMeter.tsx:62-64` fails to follow. Magic number `MENU_WIDTH = 208` `:50`.

### frontend/components/Providers.tsx  (118 LOC)
**Purpose** — App-wide theme + toast context.

**Public surface**
- `export function useTheme(): ThemeContextValue` `:46`; `export function useToast(): ToastContextValue` `:50`;
  `export function Providers({ children }: { children: ReactNode })` `:54`.
- Types `Theme` `:20`, `ThemeContextValue` `:22-25`, `Toast` `:27-31`, `ToastContextValue` `:33-35`.

**Control flow**
1. Contexts created with dark/no-op defaults `:37-44`.
2. Mount effect reads the class stamped by the pre-hydration script `:59-64`.
3. `toggleTheme` `:66-80` swaps `html` classes, sets `colorScheme`, writes
   `localStorage['techsara.theme']` `:74`.
4. `toast(text, tone)` `:82-88` pushes and auto-removes after 5,200 ms `:85-87`.
5. Renders both providers plus the `aria-live="polite"` toast stack `:90-117`.

**State & side effects** — `localStorage.setItem('techsara.theme', …)` `:74` inside try/catch `:73-77`;
`document.documentElement` class + style mutation `:69-72`; `setTimeout` `:85`.

**Dependencies** — Inbound: `app/layout.tsx:13` (provider), plus `useToast` in `ChatApp.tsx:64`,
`Composer.tsx:32`; `useTheme` in `ChartView.tsx:30`, `Sidebar.tsx:21`, `MermaidBlock.tsx:31`.
Outbound: `react` only `:10-18`.

**Config** — none.

**Failure modes** — storage failure swallowed `:75-77`. `setTimeout` at `:85` is never cleared, so a toast
scheduled just before unmount still fires `setToasts` (harmless in React 18+, but unbounded during a burst:
20 toasts in 5 s all stack in the fixed container `:94-98`).

**Concurrency** — `nextToastId` ref `:57`, `:83` is monotonic; concurrent toasts are safe.

**Complexity hotspots** — none.

**Notable** — magic number `5200` ms `:87`; `z-[70]` `:97` (the highest z-index in the app, above
`ConfirmDialog`'s `z-[70]` `ConfirmDialog.tsx:52` — they tie).

### frontend/components/ContextMeter.tsx  (202 LOC)
**Purpose** — The context-usage ring next to the send button, with a portalled breakdown popover and
"Compact now".

**Public surface**
- `interface ContextMeterProps { view: MeterView; compacting: boolean; onCompactNow: () => void; compactDisabled?: boolean }` — `ContextMeter.tsx:28-33`.
- `export function ContextMeter({...})` — `ContextMeter.tsx:35-40`.
- Constants `SIZE = 18` `:23`, `STROKE = 2.5` `:24`, `RADIUS` `:25`, `CIRCUMFERENCE` `:26`.

**Control flow**
1. `useLayoutEffect` `:46-55` measures the trigger and positions the popover
   (`left = max(12, rect.right - 280)`, `bottom = innerHeight - rect.top + 8`).
2. Dismissal effect `:57-71`: Escape `:59-61` and `pointerdown` `:62-64`.
3. Ring `:99-127` with `strokeDasharray` from `view.fraction` `:73`.
4. Popover portal `:133-199`: breakdown rows `:148-178`, total `:179-185`, "Compact now" `:186-196`.

**State & side effects** — `createPortal(document.body)` `:136`; document listeners `:65-66`.

**Dependencies** — Inbound: `components/ChatApp.tsx:57`. Outbound: `react-dom` `:16`,
`@/lib/contextMeter` `:17-21`.

**Config** — none.

**Failure modes** — **the outside-click guard checks only `buttonRef`** `:62-64`; the popover is portalled
to `<body>` and is therefore never "inside" the button, so any `pointerdown` within the popover — including
on "Compact now" `:186` — sets `open=false` and unmounts the panel between `pointerdown` and `click`.
The `onClick={(e) => e.stopPropagation()}` at `:142` is a React synthetic handler and cannot stop the
native `document` listener. See Findings.

**Concurrency** — sync.

**Complexity hotspots** — `ContextMeter` `:35-202` = 168 LOC.

**Notable** — magic numbers `280` px popover width `:51`, `:140`; `12`/`8` px insets `:51-52`;
`z-[60]` `:140`.

### frontend/components/ResearchPanel.tsx  (213 LOC)
**Purpose** — Collapsible panel showing the searches behind an answer: source count, elapsed time,
top domains, and every query with its results.

**Public surface**
- `export function formatElapsed(ms: number): string` `:25`.
- `export function countSources(research: Research): number` `:33`.
- `export function rankDomains(research: Research): { domain: string; count: number }[]` `:42-44`.
- `export function ResearchPanel({ research }: { research: Research })` `:157`.
- module-local `TOP_DOMAINS = 4` `:60`; `DomainBars({research})` `:62`; `QueryGroup({query, results})` `:104`.

**Control flow**
1. `ResearchPanel` `:157-213`: hides itself when there are no sources and no active work `:160`;
   header button `:167-197`; expanded body `:199-210` renders `DomainBars` `:201` then one `QueryGroup`
   per query `:206-208`.
2. `DomainBars` `:62-102`: memoized ranking `:63`, top 4 `:65`, remainder summarized `:94-99`.
3. `QueryGroup` `:104-155`: local open state `:111`, result list with external links `:132-152`.

**State & side effects** — none beyond local `useState`. Links are `target="_blank" rel="noopener noreferrer"` `:138-139`.

**Dependencies** — Inbound: `components/MessageRow.tsx:12`. Outbound: `@/lib/types` `:16`, `./icons` `:17-22`.

**Config** — none.

**Failure modes** — `ResearchPanel` assumes `research.queries` is an array (`:159`, `:203`, `:206`); it
comes from `meta.research` which round-trips through server history untyped (`lib/types.ts:151`,
`lib/history.ts:570` casts `m.meta as Meta`), so a malformed stored payload throws inside the render — and
there is **no error boundary anywhere in the message tree** (`ChartErrorBoundary` covers charts only,
`ChartView.tsx:87`), so it would blank the whole app.
`QueryGroup` keys on `q.query` `:207` — two identical query strings collide.

**Concurrency** — sync.

**Complexity hotspots** — `ResearchPanel` `:157-213` = 57 LOC; `QueryGroup` `:104-155` = 52 LOC.

**Notable** — `href={r.url}` `:137` is backend/model-supplied; safe from `javascript:` because of React 19
(see cross-cutting fact 3), but it is a real outbound navigation target.

### frontend/components/ModelPicker.tsx  (144 LOC)
**Purpose** — The composer's effort picker (Fast / Low / Medium / High) on the single model.

**Public surface**
- `export function ModelPicker({ model, effort, onChange }: { model: ModelChoice; effort: ReasoningEffort; onChange: (model: ModelChoice, effort: ReasoningEffort) => void })` — `ModelPicker.tsx:47-55`.
- module-local `EFFORTS` `:23`; `EFFORT_LABEL` `:25-30`; `EFFORT_SHORT` `:32-37`; `EFFORT_HELP` `:40-45`.

**Control flow**
1. Dismissal effect `:61-78` (pointerdown outside `rootRef` `:63-65`, Escape `:66-71`).
2. `pick(nextEffort)` `:80-85` always emits `onChange('smart', nextEffort)`.
3. `void model;` `:87` — the prop is accepted and deliberately ignored.
4. Trigger `:91-106`; non-portalled absolute menu `:108-141`.

**State & side effects** — document listeners `:72-73`, removed on cleanup `:74-77`.

**Dependencies** — Inbound: `components/Composer.tsx:30`. Outbound: `@/lib/types` `:20`, `./icons` `:21`.

**Config** — none.

**Failure modes** — none.

**Concurrency** — sync.

**Complexity hotspots** — none.

**Notable** — `model` is dead API surface kept for back-compat `:87`; the popover is **not** portalled
(`:112`, `absolute bottom-full`), unlike every other popover in the app — it works only because the
composer has no transformed ancestor above it. Magic number `288px` menu width `:112`.

### frontend/components/SummaryPanel.tsx  (122 LOC)
**Purpose** — Read-only modal showing the rolling compaction summary for a conversation.

**Public surface**
- `interface SummaryPanelProps { conversationId: string | null; open: boolean; onClose: () => void }` `:17-21`.
- `export function SummaryPanel({...})` `:23-27`.

**Control flow**
1. Fetch effect `:32-54`: `GET /api/history/conversations/{id}/summary` with `cache:'no-store'` `:38-41`,
   `!res.ok` → throw `:42`, body parsed `:43`, state set `:44-46`; any failure → `{kind:'error'}` `:47-49`.
2. Escape handler `:56-63`.
3. Portal `:67-121` with backdrop click-to-close `:71` and `stopPropagation` on the panel `:76`.

**State & side effects** — network egress to `/api/history/...` `:38`. `createPortal(document.body)` `:67`.

**Dependencies** — Inbound: `components/ChatApp.tsx:58`. Outbound: `react-dom` `:14`, `./icons` `:15`.

**Config** — none.

**Failure modes** — bare `catch {}` `:47` collapses 404 / 500 / network into one message `:98-100`.
No `AbortController` — a slow request from a previous open is only neutralized by the `cancelled` flag
`:51-53`, the socket stays open. No timeout, no retry.

**Concurrency** — one in-flight request; `cancelled` guard `:34`, `:44`, `:48`.

**Complexity hotspots** — `SummaryPanel` `:23-122` = 100 LOC.

**Notable** — this is the only component that talks to `/api/history` directly instead of going through
`lib/historyApi.ts`. `z-[65]` `:69` sits between `ContextMeter` (`z-[60]`) and `ConfirmDialog` (`z-[70]`).

### frontend/components/AgentTimeline.tsx  (118 LOC)
**Purpose** — The numbered agent step list (running / done / failed), expandable per step.

**Public surface**
- `export function AgentTimeline({ steps }: { steps: AgentStep[] })` — `AgentTimeline.tsx:38`.
- module-local `StatusIcon({status})` `:14`.

**Control flow** — empty guard `:42`; `toggle(id)` over a `Set<number>` `:44-51`; header `:55-61`;
`<ol>` mapping each step `:62-115` with an expandable `<button>` when `step.detail` exists `:83-98`
and a plain `<span>` otherwise `:99-103`; the detail paragraph `:104-111`.

**State & side effects** — `useState<Set<number>>` `:39`, `useId` `:40`. None external.

**Dependencies** — Inbound: `components/MessageRow.tsx:11`. Outbound: `@/lib/types` `:11`, `./icons` `:12`.

**Config** — none.

**Failure modes** — `key={step.id}` `:82` assumes unique ids; duplicate ids from a malformed
`meta.steps` produce React key warnings and mis-toggled panels.

**Concurrency** — sync.

**Complexity hotspots** — `AgentTimeline` `:38-118` = 81 LOC (JSX-heavy).

**Notable** — uses the CSS var `--ts-engine-agent-ink` `:58` which is defined in
`app/globals.css:56` / `:105`.

### frontend/components/ConfirmDialog.tsx  (91 LOC)
**Purpose** — Portalled confirmation modal for destructive actions (used only by the regenerate guard).

**Public surface**
- `interface ConfirmDialogProps { open; title; body; confirmLabel?; onConfirm; onCancel }` `:15-22`.
- `export function ConfirmDialog({...})` `:24-31`.

**Control flow** — focus lands on Cancel `:37`; Escape cancels with `stopPropagation` `:38-43`;
returns null when closed or without a document `:48`; portal `:50-90`; backdrop click cancels `:53`;
panel stops propagation `:59`.

**State & side effects** — `document.addEventListener('keydown')` `:44`; `createPortal` `:50`.

**Dependencies** — Inbound: `components/ChatApp.tsx:56`. Outbound: `react-dom` `:12`, `./icons` `:13`.

**Config** — none.

**Failure modes** — no focus trap: Tab can leave the dialog into the page behind it (unlike
`SearchPalette.tsx:248-258` which traps). `role="alertdialog" aria-modal="true"` `:56-58` claims a trap
that does not exist.

**Concurrency** — sync.

**Complexity hotspots** — none.

**Notable** — `confirmLabel` defaults to `'Delete'` `:28` although the only caller passes
`"Regenerate"` (`ChatApp.tsx:803`).

### frontend/components/EngineBadge.tsx  (89 LOC)
**Purpose** — The engine identity chip (SQL / Records / Vision / Report / Chat / Agent / Web / Page / Repo).

**Public surface**
- `export function engineAccent(engine: Engine): string` `:50`.
- `export function EngineBadge({ engine, size = 'sm' }: { engine: Engine; size?: 'xs' | 'sm' })` `:54-60`.
- module-local `ENGINE_LABEL` `:8-18`; `ENGINE_STYLE` `:20-48`.

**Control flow** — unknown routes fall back to the Chat style and print the raw route `:63-64`;
chip rendered with `color-mix()` borders/backgrounds `:66-87`; the agent dot gets a gradient `:80-84`.

**State & side effects** — none. **Not** a client component (no `'use client'`), so it can render on the server.

**Dependencies** — Inbound: `components/ProofDrawer.tsx:12`, `components/ChatApp.tsx:60`.
Outbound: `@/lib/types` `:1`.

**Config** — none.

**Failure modes** — none.

**Concurrency** — sync.

**Complexity hotspots** — none.

**Notable** — `engineAccent` `:50-52` is exported but **never imported anywhere** (verified with
`rg -n "engineAccent"` → only this file) — dead code. `search`/`url`/`repo` reuse the rag/report/vision
colors `:32-43`, so three engine pairs are visually indistinguishable.

### frontend/components/ReasoningAccordion.tsx  (87 LOC)
**Purpose** — The "Thinking… / Thought for N s" disclosure above an assistant answer.

**Public surface**
- `export function ReasoningAccordion({ text, seconds, thinking }: { text: string; seconds?: number; thinking: boolean })` — `ReasoningAccordion.tsx:25-34`.
- module-local `lastLine(text)` `:16-23`.

**Control flow** — label chosen from `thinking`/`seconds` `:38-42`; live preview of the last non-empty
reasoning line while collapsed `:43`; trigger `:47-74`; expanded panel `:76-84` with a caret while thinking.

**State & side effects** — `useState` + `useId` only.

**Dependencies** — Inbound: `components/MessageRow.tsx:17`. Outbound: `./icons` `:14`.

**Config** — none.

**Failure modes** — `lastLine` `:16-23` splits the entire reasoning text on every render while streaming
(no memo); reasoning traces can be tens of KB and this runs once per token.

**Concurrency** — sync.

**Complexity hotspots** — none.

**Notable** — magic number `340px` preview truncation `:63`.

### frontend/components/SqlBlock.tsx  (76 LOC)
**Purpose** — Renders the generated DuckDB SQL with a hand-rolled tokenizer and a copy button.

**Public surface**
- `export function SqlBlock({ sql }: { sql: string })` — `SqlBlock.tsx:43`.
- module-local `KEYWORDS` `:12-20`; `type Token` `:22`; `tokenizeSql(sql): Token[]` `:24-41`.

**Control flow** — `tokenizeSql` runs a single global regex `:26-27` classifying comments / strings /
numbers / identifiers / everything else `:29-39`; `useMemo` maps tokens to spans `:44-56`;
rendered inside `.code-block` `:58-75` with the fixed trust caption `:71-73`.

**State & side effects** — none.

**Dependencies** — Inbound: `components/ProofDrawer.tsx:13`. Outbound: `./CopyButton` `:10`.

**Config** — none.

**Failure modes** — the regex alternatives are mutually exclusive on their first character, so no
catastrophic backtracking. Token text is rendered as React children (escaped) — no injection.

**Concurrency** — sync.

**Complexity hotspots** — none.

**Notable** — the keyword list `:14-18` is DuckDB/SQLite-flavoured and hard-coded; the caption
"This exact query produced the numbers above" `:72` is a trust claim the frontend cannot verify.

### frontend/components/PastedChip.tsx  (68 LOC)
**Purpose** — The "PASTED" attachment chip for long pasted text, with an expandable read-only preview.

**Public surface**
- `export function PastedChip({ pasted, onRemove }: { pasted: PastedText; onRemove?: () => void })` — `PastedChip.tsx:15-21`.
- module-local `PREVIEW_CAP = 5000` `:13`.

**Control flow** — preview truncated at 5,000 chars `:23-26`; chip `:30-60`; expanded `<pre>` `:61-65`.

**State & side effects** — `useState` only.

**Dependencies** — Inbound: `components/MessageRow.tsx:14`, `components/Composer.tsx:31`.
Outbound: `@/lib/types` `:10`, `./icons` `:11`.

**Config** — none.

**Failure modes** — none.

**Concurrency** — sync.

**Complexity hotspots** — none.

**Notable** — the preview slice `:24-26` is recomputed on every render (no memo) for a string that can be
hundreds of KB.

### frontend/components/CitationChips.tsx  (35 LOC)
**Purpose** — Salesforce record chips (`{object} · {record_id}`) linking to Lightning.

**Public surface** — `export function CitationChips({ citations }: { citations: Citation[] })` `:9`.

**Control flow** — one `<li><a>` per citation `:12-32`, `key={`${c.object}-${c.record_id}`}` `:13`,
`href={c.url}` `:15`, `target="_blank" rel="noopener noreferrer"` `:16-17`.

**State & side effects** — outbound navigation on click to whatever `c.url` says (backend-built).

**Dependencies** — Inbound: `components/ProofDrawer.tsx:16`. Outbound: `@/lib/types` `:6`, `./icons` `:7`.

**Config** — none. **Failure modes** — duplicate `(object, record_id)` pairs collide on the key `:13`.
**Concurrency** — sync. **Complexity hotspots** — none.

**Notable** — no `'use client'` directive; it is a pure server-renderable component.

### frontend/components/WebSources.tsx  (43 LOC)
**Purpose** — Numbered `[n]` web-source rows behind a search answer.

**Public surface** — `export function WebSources({ sources }: { sources: WebSource[] })` `:13`.

**Control flow** — a scroll-capped `<ul>` (`max-h-[22rem]` `:18`) of `<a href={s.url}>` rows `:19-40`,
keyed by `s.n` `:20`.

**State & side effects** — outbound navigation only; the comment at `:5-7` records the deliberate decision
**not** to fetch remote favicons.

**Dependencies** — Inbound: `components/ProofDrawer.tsx:17`. Outbound: `@/lib/types` `:10`, `./icons` `:11`.

**Config** — none. **Failure modes** — duplicate `s.n` collides on the key `:20`.
**Concurrency** — sync. **Complexity hotspots** — none.

**Notable** — the no-favicon decision `:5-7` is the same egress concern the markdown `img` hole reopens.

### frontend/components/CodeCitations.tsx  (49 LOC)
**Purpose** — `path:Lstart-Lend` code excerpts behind a repo answer, expandable to the snippet.

**Public surface** — `export function CodeCitations({ sources }: { sources: CodeSource[] })` `:13`.

**Control flow** — single-open accordion via `useState<number|null>` `:14`; label built at `:18`;
row button `:22-38`; snippet `<pre>` `:39-43`.

**State & side effects** — none.

**Dependencies** — Inbound: `components/ProofDrawer.tsx:18`. Outbound: `@/lib/types` `:10`, `./icons` `:11`.

**Config** — none. **Failure modes** — none (snippet rendered as escaped children `:41`).
**Concurrency** — sync. **Complexity hotspots** — none. **Notable** — `max-h-72` snippet cap `:40`.

### frontend/components/FileCards.tsx  (47 LOC)
**Purpose** — Download cards for generated report files, proxied through `/api/reports/[filename]`.

**Public surface** — `export function FileCards({ files }: { files: ReportFile[] })` `:10`.

**Control flow** — for each file: `fileKind(f.filename)` `:13`, anchor to
`/api/reports/${encodeURIComponent(f.filename)}` with `download={f.filename}` `:18-19`, badge `:22-27`,
name + size `:28-36`.

**State & side effects** — download egress to the same-origin proxy `:18`.

**Dependencies** — Inbound: `components/ProofDrawer.tsx:19`. Outbound: `@/lib/types` `:6`,
`@/lib/format` `:7`, `./icons` `:8`.

**Config** — none.

**Failure modes** — `f.type.toUpperCase()` `:33` throws if the backend omits `type` (declared optional-free
in `lib/types.ts:74-78` — `type: string` is required, so a malformed payload is the only route).
`key={f.filename}` `:16` collides on duplicate filenames.

**Concurrency** — sync. **Complexity hotspots** — none.

**Notable** — `encodeURIComponent` `:18` is the frontend's only guard against a `../` filename; the real
check must live in the (unread) `app/api/reports/[filename]/route.ts`.

### frontend/components/CopyButton.tsx  (49 LOC)
**Purpose** — Copy-to-clipboard button with a 1.6 s "Copied" state and a `execCommand` fallback.

**Public surface** — `export function CopyButton({ text, label, className = '' }: { text: string; label: string; className?: string })` `:6-14`.

**Control flow** — `navigator.clipboard.writeText` `:19`; on failure a hidden `<textarea>` +
`document.execCommand('copy')` `:21-28`; `setCopied(true)` then a 1,600 ms reset `:29-30`.

**State & side effects** — clipboard write; transient DOM node `:22-27`; `setTimeout` `:30`
(never cleared).

**Dependencies** — Inbound: `components/MessageRow.tsx:16`, `components/SqlBlock.tsx:10`,
`components/MermaidBlock.tsx:30`, `components/Markdown.tsx:12`. Outbound: `./icons` `:4`.

**Config** — none.

**Failure modes** — `setCopied(true)` runs at `:29` **even when both paths failed** — `document.execCommand`
returns a boolean that is ignored `:26`, so the user is told "Copied" when nothing was copied.

**Concurrency** — sync + timer. **Complexity hotspots** — none.

**Notable** — magic number `1600` ms `:30`; the transient textarea is appended to `document.body` `:24`
without `readonly`/off-screen positioning, so it can scroll the page on `select()` `:25`.

### frontend/components/EmptyState.tsx  (19 LOC)
**Purpose** — The empty-thread greeting (mark + "What can I help with?").

**Public surface** — `export function EmptyState()` `:10`.

**Control flow** — static JSX `:11-18`.

**State & side effects** — none. **Dependencies** — Inbound: `components/ChatApp.tsx:59`;
Outbound: `./TechSaraMark` `:8`. **Config** — none. **Failure modes** — none. **Concurrency** — sync.
**Complexity hotspots** — none.

**Notable** — declares `'use client'` `:1` despite having no interactivity or hooks;
the removal of the suggestion chips is documented at `:4-5`.

### frontend/components/TechSaraMark.tsx  (19 LOC)
**Purpose** — The brand mark as a plain `<img>` (no `next/image`, so the standalone build needs no
image-optimization server).

**Public surface** — `export function TechSaraMark({ size = 56 }: { size?: number })` `:7`.

**Control flow** — renders `<img src="/techsara-mark.png">` `:10-17` with an eslint-disable for
`@next/next/no-img-element` `:9`.

**State & side effects** — same-origin asset request for `/techsara-mark.png`.

**Dependencies** — Inbound: `components/EmptyState.tsx:8`, `components/Sidebar.tsx:20`. Outbound: none.

**Config** — none. **Failure modes** — a missing `public/techsara-mark.png` yields a broken image with the
alt text. **Concurrency** — sync. **Complexity hotspots** — none.

**Notable** — no `'use client'`; server-renderable.

### frontend/components/icons.tsx  (287 LOC)
**Purpose** — The entire inline SVG icon set (34 icons), stroke-following-currentColor on a 24×24 grid.

**Public surface** — `interface IconProps { size?: number; className?: string }` `:3-6`;
`function base(size, className)` `:8-21`; and 34 exported components:
`IconPlus` `:23`, `IconSearch` `:29`, `IconPencil` `:36`, `IconTrash` `:42`, `IconMessage` `:48`,
`IconSun` `:54`, `IconMoon` `:61`, `IconMenu` `:67`, `IconSidebar` `:73`, `IconPaperclip` `:80`,
`IconSend` `:86`, `IconStop` `:92`, `IconCopy` `:98`, `IconCheck` `:105`, `IconRefresh` `:111`,
`IconChevronDown` `:117`, `IconArrowDown` `:123`, `IconDownload` `:129`, `IconExternal` `:135`,
`IconAlert` `:141`, `IconX` `:147`, `IconSort` `:153`, `IconCloud` `:159`, `IconSparkles` `:165`,
`IconLogout` `:172`, `IconBulb` `:178`, `IconDots` `:187`, `IconPin` `:195`, `IconPinOff` `:202`,
`IconArchive` `:210`, `IconUnarchive` `:218`, `IconChevronRight` `:226`, `IconFileText` `:232`,
`IconGlobe` `:239`, `IconCode` `:248`, `IconPlay` `:254`, `IconExpand` `:260`, `IconZoomIn` `:266`,
`IconZoomOut` `:273`, `IconDiagram` `:280`.

**Control flow** — every icon spreads `base(size, className)` onto an `<svg>` `:8-21` with
`aria-hidden: true` `:19`.

**State & side effects** — none. Pure.

**Dependencies** — Inbound: 20 component files (verified with rg — `SearchPalette:55`, `PastedChip:11`,
`ChatApp:65`, `FileCards:8`, `CitationChips:7`, `ConfirmDialog:13`, `CodeCitations:11`, `CopyButton:4`,
`MessageRow:19`, `WebSources:11`, `SummaryPanel:15`, `ConversationMenu:48`, `ProofDrawer:20`,
`AgentTimeline:12`, `Sidebar:31`, `DataTable:11`, `ModelPicker:21`, `ReasoningAccordion:14`,
`MermaidBlock:41`, `ResearchPanel:22`). Outbound: none.

**Config** — none. **Failure modes** — none. **Concurrency** — sync. **Complexity hotspots** — none.

**Notable** — `IconMenu` `:67`, `IconSort` `:153`, `IconLogout` `:172` are exported but imported by no
component (verified with `rg -n "IconMenu|IconSort|IconLogout" frontend --glob '!node_modules'` — only
this file) — dead code, `IconLogout` a leftover from the removed login. No `'use client'`, so the module is
server-renderable and tree-shakeable.

---

# LIB

### frontend/lib/attachments.ts  (79 LOC)
**Purpose** — In-memory (never persisted) map of the raw attachment payload sent with each user turn, so
regenerate/retry re-send the same question **with** its file.

**Public surface**
- `export interface SentAttachment { kind: 'image' | 'pdf'; name: string; base64: string }` `:17-22`.
- `export function rememberAttachment(messageId: string, attachment: SentAttachment): void` `:27-32`.
- `export function base64FromDataUrl(dataUrl?: string | null): string | null` `:35-41`.
- `export interface AttachmentLookup { attachment: SentAttachment | null; missing: boolean }` `:43-48`.
- `export function attachmentForResend(message: { id; imageDataUrl?; pdfName? }): AttachmentLookup` `:58-74`.
- `export function clearAttachments(): void` `:77-79` (test seam).

**Control flow** — `attachmentForResend` first consults the in-memory map `:63-64`, then falls back to
decoding the persisted `imageDataUrl` `:66-72`, and finally reports `missing` when the turn *had* an
attachment we can no longer rebuild `:73`.

**State & side effects** — module-level `const sent = new Map<string, SentAttachment>()` `:24` — shared
mutable state for the tab's lifetime, never bounded and never evicted (a session that attaches ten 25 MB
PDFs holds ~250 MB of base64 in JS heap until reload).

**Dependencies** — Inbound: `components/ChatApp.tsx:35`, `tests/attachments.test.ts:7`. Outbound: none.

**Config** — none.

**Failure modes** — `missing` is computed **only** from `pdfName || imageDataUrl` `:73`. Any code path that
rebuilds a thread from server messages drops both fields (`lib/history.ts:566-573` constructs
`ChatMessage`s with only `id/role/content/meta/status/createdAt`), so the lookup returns
`{attachment: null, missing: false}` and the caller silently re-sends a vision question with no image.
See Findings.

**Concurrency** — sync; the Map is not keyed by conversation, so ids must be globally unique
(`newId()` uses `crypto.randomUUID` — `lib/history.ts:114-119`).

**Complexity hotspots** — none.

**Notable** — the module docstring `:9-14` claims the payload is deliberately kept out of localStorage to
avoid quota eviction, while `ChatApp.tsx:412-413` persists the full `dataUrl` of an image (up to 10 MB
raw ⇒ ~13.6 MB base64) anyway.

### frontend/lib/chartFormat.ts  (138 LOC)
**Purpose** — Application-owned number/date/label formatting for charts; nothing here can be supplied by
the backend.

**Public surface**
- `export type Cell = string | number | boolean | null | undefined` `:21`.
- `export function isNumeric(v: Cell): boolean` `:26`.
- `export function toNumber(v: Cell): number | null` `:40`.
- `export function formatInteger(v: number): string` `:46`.
- `export function formatDecimal(v: number, places = 2): string` `:50`.
- `export function formatPercent(v: number, places = 1): string` `:57`.
- `export function formatCompact(v: number): string` `:65`.
- `export function formatNumber(v: number): string` `:85`.
- `export function formatCurrency(v: number, currency?: string | null): string` `:95`.
- `export function formatDate(v: Cell): string` `:108`.
- `export function formatDateTime(v: Cell): string` `:117`.
- `export function formatCell(v: Cell): string` `:125`.
- `export function truncateLabel(label: string, max = 24): string` `:136`.
- Constants: `COMPACT_THRESHOLD = 10_000` `:23`; `ISO_DATE` `:105`; `ISO_DATETIME` `:106`.

**Control flow** — `formatCell` `:125-133` dispatches by JS type then by ISO-date regex;
`formatCompact` `:65-82` scales through a T/B/M/k table `:68-73`.

**State & side effects** — none. Pure. Uses the ambient locale via `toLocaleString` `:47`, `:51`, `:99`,
`:114`, `:121`.

**Dependencies** — Inbound: `lib/chartOption.ts:34-42` only. Outbound: none.

**Config** — none.

**Failure modes** — `formatCurrency` `:95-103` guards a bad ISO code with a regex `:97` and wraps
`toLocaleString` in try/catch `:98-102`. `formatDate`/`formatDateTime` return the raw string when parsing
fails `:112-114`, `:121`. Nothing throws.

**Concurrency** — sync, pure.

**Complexity hotspots** — none.

**Notable** — `formatPercent` `:57`, `formatCurrency` `:95`, `formatInteger` `:46` are exported but
**never imported by any module** (`rg` shows `lib/chartOption.ts:34-42` imports only
`Cell, formatCell, formatCompact, formatNumber, isNumeric, toNumber, truncateLabel`) — dead code kept
for the documented "no backend-supplied format string" policy `:1-18`.
`toLocaleString` with no explicit locale makes output host-locale dependent, so the browser and the
server-rendered report PNG can disagree on thousands separators.

### frontend/lib/chartOption.ts  (602 LOC)
**Purpose** — The trusted `ChartSpec` → ECharts option adapter and the documented security boundary
between backend-supplied data and the chart renderer.

**Public surface**
- `export type EChartsOption = Record<string, unknown>` `:45`.
- `export const CHART_TYPES: readonly ChartType[]` `:48-58` (9 types).
- `export function isChartType(value: unknown): value is ChartType` `:68`.
- `export type ChartProblem` `:76-83` (7 members).
- `export function validateChart(spec, rows): ChartProblem | null` `:93-117`.
- `export function partToWholeData(spec, rows, key): Array<{name; value}>` `:138-153`.
- `export function escapeHtml(text: string): string` `:176-178`.
- `export function buildChartOption(spec, rows, palette): EChartsOption | null` `:315-326`.
- `export const CATEGORY_TICK_LIMIT` `:602`.
- module-local: `TYPE_SET` `:60`, `PART_TO_WHOLE` `:63`, `MAX_SLICES = 6` `:65`,
  `MAX_CATEGORY_TICKS = 40` `:66`, `usableYKeys` `:123`, `categoriesOf` `:129`, `valuesOf` `:133`,
  `ESCAPES` `:159-165`, `swatch` `:180`, `tooltipRow` `:187`, `TooltipParam` `:191-198`,
  `numberFrom` `:200`, `axisTooltipFormatter` `:205`, `itemTooltipFormatter` `:218`,
  `scatterTooltipFormatter` `:227`, `valueLabel` `:240`, `baseOption` `:249`, `legendOf` `:266`,
  `categoryAxis` `:278`, `valueAxis` `:294`, `labelOption` `:304`, `buildOption` `:328`,
  `barOption` `:354`, `horizontalBarOption` `:386`, `lineOption` `:423`, `partToWholeOption` `:458`,
  `funnelOption` `:494`, `histogramOption` `:535`, `scatterOption` `:566`.

**Control flow**
1. `buildChartOption` `:315-326`: re-runs `validateChart` `:320`, then `buildOption` inside try/catch
   returning null on any throw `:321-325`.
2. `buildOption` `:328-352` switches on the **whitelisted** `spec.type` `:333-351`; the `default` arm
   falls through to `barOption` `:348-350`.
3. `validateChart` `:93-117`: type whitelist `:97`, non-empty rows `:98`, x column present `:101`,
   at least one present y column `:102-103`, scatter needs numeric x `:105-108`, at least one numeric y
   `:109-110`, pie/donut needs a positive total `:112-115`.
4. Every per-type builder spreads `baseOption` and then sets only fields defined in this file
   (`:364-383`, `:395-420`, `:433-455`, `:468-491`, `:506-532`, `:546-563`, `:578-598`).

**State & side effects** — none. Pure module, no DOM, no `echarts` import (`:26-29`).

**Dependencies** — Inbound: `components/ChartView.tsx:28`, `components/EChart.tsx:39` (type only),
`tests/chartOption.test.ts:19`. Outbound: `./types` `:32`, `./chartTheme` `:33`, `./chartFormat` `:34-42`.

**Config** — none.

**Failure modes** — `buildChartOption` is total (`:321-325`). The **tooltip formatters are the one HTML
sink**: `tooltipRow` `:187-189` escapes name and value, `swatch` `:180-185` escapes the color, and
`axisTooltipFormatter` escapes the header `:215`. `escapeHtml` `:177` calls `.replace` on its argument
and would throw if ECharts ever passed a non-string `name`/`axisValueLabel` — the `?? ''` fallbacks
`:208`, `:212`, `:224` only cover null/undefined, not a number.

**Concurrency** — sync, pure.

**Complexity hotspots** — no single function exceeds 60 LOC; the file's cyclomatic weight is concentrated
in `validateChart` `:93-117` (9 branches) and `buildOption` `:328-352` (10 arms). Total 602 LOC in one
module.

**Notable** — the security rationale is documented at `:1-30` and is accurate as written: `type` is
whitelist-checked `:97`, `x_key`/`y_keys` are used only as property lookups `:130`, `:134`, and every
`formatter` is a local function (`:289`, `:299`, `:306`, `:366`, `:397`, `:435`, `:470`, `:508`, `:548`,
`:583`). Magic numbers: `MAX_SLICES = 6` `:65`, `MAX_CATEGORY_TICKS = 40` `:66`, `animationDuration: 350`
`:252`, `barMaxWidth` 36/28 `:376`, `:413`, `showSymbol` cutoff `rows.length <= 60` `:445`,
pie radii `:467`, funnel insets `:521-526`.

### frontend/lib/chartTheme.ts  (121 LOC)
**Purpose** — Resolves the chart palette from the design-system CSS custom properties, with literal
fallbacks for SSR/tests.

**Public surface**
- `export interface ChartPalette { series: string[]; text; axis; grid; surface; tooltipBg; tooltipText }` `:24-33`.
- `export const SERIES_FALLBACK` `:35-41` (5 hexes).
- `export type ThemeName = keyof typeof CHROME` `:62`.
- `export function resolveSeriesColors(root?: Element | null): string[]` `:87-103`.
- `export function resolvePalette(theme: ThemeName, root?: Element | null): ChartPalette` `:106-109`.
- `export function fallbackPalette(theme: ThemeName): ChartPalette` `:112-115`.
- `export function seriesColor(palette: ChartPalette, i: number): string` `:118-121`.
- module-local `CHROME` `:43-60`; `TOKEN_NAMES` `:64-70`; `isUsableColor` `:73-79`.

**Control flow** — `resolveSeriesColors` returns fallbacks with no DOM `:88-91`, wraps
`getComputedStyle` in try/catch `:93-98`, then maps each token through `isUsableColor` `:99-102`.

**State & side effects** — reads `document.documentElement` + `window.getComputedStyle` `:89`, `:95`.
No writes.

**Dependencies** — Inbound: `components/ChartView.tsx:29`, `lib/chartOption.ts:33`,
`tests/chartOption.test.ts:20`. Outbound: none.

**Config** — none (reads CSS vars `--ts-chart-1..5`, defined at `app/globals.css:60-64`).

**Failure modes** — total; every path returns a palette.

**Concurrency** — sync, pure apart from the computed-style read.

**Complexity hotspots** — none.

**Notable** — `SERIES_FALLBACK` `:36-40` duplicates `--ts-chart-1..5` from `app/globals.css:60-64`
verbatim; the file documents this as a deliberate safety net `:11-15`. `CHROME` `:43-60` hard-codes 12
more colors that also exist as tokens (`--ts-border`, `--ts-surface`, `--ts-text-muted`) — those are **not**
resolved from CSS and will drift if the theme changes.

### frontend/lib/conversationMenu.ts  (236 LOC)
**Purpose** — Headless, unit-testable model for the sidebar row menu: items, activation semantics,
keyboard map, popover placement.

**Public surface**
- `export type ConversationMenuItemId` `:11-18`.
- `export interface ConversationMenuItem { id; label; danger? }` `:20-25`.
- `export interface ConversationMenuFlags { pinned; archived }` `:27-30`.
- `export function conversationMenuItems(flags, confirmingDelete = false): ConversationMenuItem[]` `:40-57`.
- `export interface ConversationMenuHandlers` `:59-65`; `export interface ConversationMenuActions` `:68-75`.
- `export function conversationMenuHandlers(conversation, actions): ConversationMenuHandlers` `:82-95`.
- `export type ConversationMenuOutcome` `:98-104`.
- `export function activateMenuItem(id, handlers): ConversationMenuOutcome` `:110-135`.
- `export type MenuKeyAction` `:137-143`; `export function menuKeyAction(key, current, count)` `:150-172`.
- `export interface MenuRect | MenuSize | MenuViewport | MenuPosition` `:176-197`.
- `export function placeMenu(trigger, menu, viewport, gap = 6, margin = 8): MenuPosition` `:208-236`.

**Control flow** — `activateMenuItem` `:110-135` is an exhaustive switch where `'delete'` returns
`confirm-delete` **without** calling `onDelete` `:127-128`; `placeMenu` `:208-236` prefers below, flips
above when it would overflow, and clamps both axes `:215-234`.

**State & side effects** — none. Pure.

**Dependencies** — Inbound: `components/Sidebar.tsx:17`, `components/ConversationMenu.tsx:36`,
`tests/conversation-menu.test.ts:18`. Outbound: none.

**Config** — none.

**Failure modes** — `menuKeyAction` returns `{kind:'close'}` for Escape/Tab **before** the
`count === 0` guard `:155-156`, which is intended. Nothing throws.

**Concurrency** — sync, pure. **Complexity hotspots** — none.

**Notable** — magic defaults `gap = 6`, `margin = 8` `:213`.

### frontend/lib/csv.ts  (39 LOC)
**Purpose** — Client-side CSV construction and download for the Data section.

**Public surface**
- `export function rowsToCsv(rows: DataRow[]): string` `:12-25`.
- `export function downloadCsv(rows: DataRow[], filename: string): void` `:27-39`.
- module-local `escapeCell(value: unknown): string` `:5-10`.

**Control flow** — `rowsToCsv` unions all keys `:14-19`, emits a header then one line per row
with CRLF separators `:20-24`; `downloadCsv` creates a Blob `:28-30`, an object URL `:31`, a synthetic
anchor `:32-36`, and revokes immediately `:38`.

**State & side effects** — DOM mutation (`document.body.appendChild` `:35`), object URL create/revoke
`:31`/`:38`, browser download.

**Dependencies** — Inbound: `components/DataTable.tsx:10`. Outbound: `./types` `:3`.

**Config** — none.

**Failure modes** — `URL.revokeObjectURL(url)` is called **synchronously** right after `a.click()` `:37-38`;
`lib/exportMarkdown.ts:110-111` and `MermaidBlock.tsx:184` both defer the revoke precisely because an
immediate revoke can abort the download in some browsers. This file is the inconsistent one.
No CSV-injection guard: a cell beginning `=`, `+`, `-` or `@` is written verbatim `:5-10` and will be
evaluated as a formula when the file is opened in Excel.

**Concurrency** — sync. **Complexity hotspots** — none.

**Notable** — `escapeCell` `JSON.stringify`s objects `:8`, so a nested Salesforce record becomes a quoted
JSON blob in the cell.

### frontend/lib/exportMarkdown.ts  (112 LOC)
**Purpose** — Builds (and downloads) a conversation as a Markdown file entirely in the browser.

**Public surface**
- `export function slugifyTitle(title: string): string` `:21-30`.
- `export function exportFilename(title: string, id: string): string` `:33-35`.
- `export function buildConversationMarkdown(conversation: Conversation): string` `:70-74`.
- `export interface ExportedConversation { filename; markdown }` `:76-79`.
- `export function buildConversationExport(conversation): ExportedConversation` `:81-88`.
- `export function downloadMarkdown({filename, markdown}): void` `:95-112`.
- module-local `ASSISTANT_LABEL = 'TechSara'` `:17`; `SLUG_MAX = 48` `:18`;
  `messageSection(message)` `:37-64`.

**Control flow** — `messageSection` `:37-64` emits `## You` / `## TechSara` `:38-40`, the content or a
stopped/error placeholder `:42-49`, then for assistant turns a fenced ```sql block `:52-53` and a
`**Records:**` line `:55-60`; `buildConversationMarkdown` joins them under an H1 `:71-73`.

**State & side effects** — `downloadMarkdown` `:95-112`: Blob, object URL, anchor click, deferred revoke
`:111`. No network.

**Dependencies** — Inbound: `components/ChatApp.tsx:25`, `lib/history.ts:35-38`,
`tests/export-markdown.test.ts:12`. Outbound: `./types` `:14`.

**Config** — none.

**Failure modes** — none thrown. The export deliberately omits attachments, charts, reasoning, steps,
research and web sources — only content, SQL and record ids survive `:37-64`.

**Concurrency** — sync. **Complexity hotspots** — none.

**Notable** — the builder/downloader split `:90-94` is documented as a testability decision.
`SLUG_MAX = 48` `:18` and the deferred `setTimeout(..., 0)` revoke `:111`.

### frontend/lib/fixtures.ts  (397 LOC)
**Purpose** — MOCK_MODE canned SSE responses, one per engine, matching the real meta contract.

**Public surface**
- `export interface Fixture { text; meta: Meta; reasoning?; steps? }` `:14-21`.
- `export const MOCK_MODEL_IDS` `:24-27`.
- `export const FIXTURES: Record<Engine, Fixture>` `:333-343`.
- `export function pickFixtureEngine(lastUserMessage, hasImage, options?): Engine` `:351-375`.
- `export const MOCK_REPORTS` `:378-397`.
- module-local fixtures: `sqlFixture` `:36`, `ragFixture` `:76`, `visionFixture` `:126`,
  `reportFixture` `:155`, `chatFixture` `:173`, `agentFixture` `:190`, `searchFixture` `:274`,
  `urlFixture` `:298`, `repoFixture` `:315`; data arrays `MONTHS` `:29-32`, `CREATED` `:33`, `CLOSED` `:34`.

**Control flow** — `pickFixtureEngine` `:351-375` is a fixed precedence chain: agent → assistant mode →
image → greeting regex `:361-366` → report regex `:368` → rag regex `:371` → sql default `:374`.

**State & side effects** — none. Pure data.

**Dependencies** — Inbound: `app/api/chat/route.ts:12` **only** (a server route). Outbound: `./types` `:12`.

**Config** — none directly; the module is only reached when `MOCK_MODE=true` (per the docstring `:6-7`;
the gate itself lives in the unread `app/api/chat/route.ts`).

**Failure modes** — none.

**Concurrency** — n/a (frozen literals, but the objects are **not** `Object.freeze`d, so a route handler
that mutates `FIXTURES[...].meta` would corrupt the module for the process lifetime; `app/api/chat/route.ts:53-59`
spreads rather than mutates).

**Complexity hotspots** — none (the file is 90% string literals).

**Notable** — `MOCK_MODEL_IDS` `:24-27` names `openai/gpt-oss-120b` and `Qwen/Qwen3-4B-Instruct-2507`,
which contradict the current single-model story in `components/ModelPicker.tsx:6` (Qwen3.6-35B-A3B) and
`lib/types.ts:11-15` — stale fixture metadata. Fake Salesforce ids and
`https://techsara.lightning.force.com/...` URLs appear at `:98-121` and `:259-270`.

### frontend/lib/format.ts  (46 LOC)
**Purpose** — Small shared formatters: byte sizes, timestamps, file-kind badges.

**Public surface**
- `export function formatBytes(bytes: number | undefined): string` `:3-15`.
- `export function formatWhen(input: number | string): string` `:17-30`.
- `export function fileKind(nameOrType: string): { label; className }` `:40-46`.
- module-local `FILE_KIND` `:32-38`.

**Control flow** — `formatBytes` walks a KB/MB/GB ladder `:6-14`; `formatWhen` tolerates unix seconds
`:20`; `fileKind` maps the last dot-segment through a table with a FILE fallback `:44-45`.

**State & side effects** — none.

**Dependencies** — Inbound: `components/FileCards.tsx:7` only. Outbound: none.

**Config** — none.

**Failure modes** — `formatBytes(NaN)` returns the em dash `:4`; `formatWhen` returns the raw input on an
unparseable date `:22`.

**Concurrency** — sync, pure. **Complexity hotspots** — none.

**Notable** — `formatWhen` `:17` is exported but **imported by nothing** (`rg -n "formatWhen"` → this file
only) — dead code. The class names it returns (`file-icon-docx` etc.) are defined at
`app/globals.css:443-462`.

### frontend/lib/mermaid.ts  (123 LOC)
**Purpose** — Pure mermaid helpers (language detection, streaming-safe "is it renderable yet", filename
slug, zoom math, SVG export prep) kept free of the heavy mermaid import.

**Public surface**
- `export function isMermaidLanguage(language?: string | null): boolean` `:9-11`.
- `export function looksRenderable(code: string): boolean` `:26-34`.
- `export function diagramFileName(code: string, ext = 'png'): string` `:37-50`.
- `export const ZOOM_MIN = 0.1` `:54`; `export const ZOOM_MAX = 4` `:55`.
- `export function clampZoom(z: number): number` `:57-60`.
- `export function svgNaturalSize(svg: string): {width; height} | null` `:63-71`.
- `export function fitZoom(natural, viewportWidth, viewportHeight): number` `:78-89`.
- `export function prepareSvgForExport(svg, width, height, background): string` `:96-123`.
- module-local `MERMAID_LANGS` `:7`; `DIAGRAM_HEADS` (23 entries) `:19-24`.

**Control flow** — `looksRenderable` requires ≥2 non-comment lines and a known head keyword `:27-33`;
`prepareSvgForExport` injects `xmlns` `:103-105`, rewrites width/height `:107-115`, and splices a
background `<rect>` after the first `>` `:117-121`.

**State & side effects** — none. Pure string manipulation.

**Dependencies** — Inbound: `components/MermaidBlock.tsx:20-29`, `components/Markdown.tsx:11`,
`tests/mermaid.test.ts:12`. Outbound: none.

**Config** — none.

**Failure modes**
- `prepareSvgForExport` `:107-109` replaces the **first** `width="…"`/`height="…"` occurrence in the whole
  string. If mermaid ever emits an SVG whose root lacks a width but whose first child has one, the child's
  geometry is clobbered.
- `:117-121` splices at the first `>` character, which is assumed to close the `<svg>` tag; an attribute
  value containing `>` would break the output (mermaid escapes those, so not reachable today).
- `svgNaturalSize` `:66` returns null on any unexpected viewBox format, and the caller silently falls back
  to an unsized wrapper (`MermaidBlock.tsx:409-415`).

**Concurrency** — sync, pure. **Complexity hotspots** — none.

**Notable** — `ZOOM_MIN = 0.1` `:54` is documented as deliberately low for architecture diagrams `:52-53`;
`fitZoom` caps upscaling at 1.5× `:88`; the slug is capped at 40 chars `:48`.

### frontend/lib/mockApi.ts  (291 LOC)
**Purpose** — Server-only in-memory implementation of the orchestrator's `/auth` and `/history` contracts
for MOCK_MODE.

**Public surface**
- `export async function handleMockAuth(req: Request, path: string[]): Promise<Response>` `:50-58`.
- `export async function handleMockHistory(req: Request, path: string[]): Promise<Response>` `:151-291`.
- module-local `MockMessage` `:13-17`, `MockConversation` `:19-28`, `convsByUser` `:30`,
  `MOCK_LOCAL_USER = 'local'` `:33`, `json()` `:35-42`, `nowIso()` `:62-64`, `PATCHABLE` `:67`,
  `summaryOf()` `:69-78`, `userConvs()` `:80-87`, `MockSearchResult` `:90-98`,
  `SEARCH_LIMIT_DEFAULT = 50` `:100`, `SEARCH_LIMIT_MAX = 100` `:101`, `mockSearch()` `:109-149`.

**Control flow** — `handleMockHistory` `:151-291` routes by path/verb: `search` `:157-159`,
list `:167-187`, create `:190-219`, append message `:225-243`, get `:246-253`, PUT patch `:255-282`,
DELETE `:283-287`, else 404 `:290`.

**State & side effects** — **module-level mutable `Map` `convsByUser` `:30`** persisting for the Node
process lifetime. No DB, no filesystem, no network.

**Dependencies** — Inbound: `app/api/history/[...path]/route.ts:9` only (`handleMockHistory`).
`handleMockAuth` `:50` is exported but **imported nowhere** (`rg -n "handleMockAuth"` → this file only) —
dead code. Outbound: `./searchPalette` `:11` (`buildSnippet`, `SEARCH_MAX_QUERY`).

**Config** — none directly; gated by `MOCK_MODE` in the unread route handler.

**Failure modes** — every JSON parse is wrapped `:192-196`, `:228-232`, `:258-262`.
Unknown PUT fields are rejected `:263-268`. The search path is a plain JS substring match, so `%`/`_`
need no escaping `:106-108`.

**Concurrency** — the Map is mutated from concurrent request handlers with no locking `:236-241`,
`:269-280`, `:285`; Node's single-threaded event loop makes each synchronous block atomic, but a
request that `await req.json()` and then mutates `:227-241` interleaves with others.

**Complexity hotspots** — `handleMockHistory` `:151-291` = **141 LOC** with 8 route arms.

**Notable** — imports a **client-side** module (`./searchPalette`) into a server-only file `:11`, coupling
the mock backend to the palette's snippet rule; that is the documented intent `:200-203` of
`lib/searchPalette.ts`.

### frontend/lib/pasted.ts  (65 LOC)
**Purpose** — Rules for turning a long paste into a "PASTED" chip and folding chips back into the model
input.

**Public surface**
- `export const PASTE_MIN_CHARS = 1200` `:16`; `export const PASTE_MIN_LINES = 12` `:17`.
- `export function countLines(text: string): number` `:19-22`.
- `export function shouldAttachPaste(text: string): boolean` `:25-28`.
- `export function makePastedText(content: string, id: string): PastedText` `:30-32`.
- `export function foldModelContent(content: string, pasted?: PastedText[] | null): string` `:40-48`.
- `export function imageExtFromMime(mime: string): string` `:51-65`.

**Control flow** — `foldModelContent` `:40-48` puts pasted blocks first, then the typed instruction,
dropping blank parts `:44-46`.

**State & side effects** — none. Pure.

**Dependencies** — Inbound: `lib/streams.ts:22`, `components/Composer.tsx:24-28`,
`tests/pasted.test.ts:10`. Outbound: `./types` `:13`.

**Config** — none. **Failure modes** — none.

**Concurrency** — sync, pure. **Complexity hotspots** — none.

**Notable** — `imageExtFromMime` `:52-63` allocates its 10-entry map on **every call**; it is called once
per pasted image, so this is cosmetic. Thresholds `1200`/`12` `:16-17`.

### frontend/lib/prefs.ts  (138 LOC)
**Purpose** — Per-conversation composer preferences persisted in localStorage under a draft slot + one
entry per conversation.

**Public surface**
- `export type WebSearchMode = 'off' | 'auto' | 'on'` `:20`.
- `export interface ChatPrefs { salesforce; model; effort; agent; webSearch }` `:22-29`.
- `export const DEFAULT_PREFS: ChatPrefs` `:31-37`.
- `export function loadPrefs(storage: StorageLike, conversationId: string | null): ChatPrefs` `:98-105`.
- `export function savePrefs(storage, conversationId, prefs): void` `:108-116`.
- `export function adoptDraftPrefs(storage, conversationId): ChatPrefs` `:119-129`.
- `export function removePrefs(storage, conversationId): void` `:132-138`.
- module-local `STORAGE_KEY = 'techsara.chatprefs.v1'` `:39`, `DRAFT_SLOT = '__draft__'` `:40`,
  `MAX_ENTRIES = 200` `:42`, `sanitize()` `:44-67`, `readMap()` `:69-80`, `writeMap()` `:82-95`.

**Control flow** — `sanitize` `:44-67` forces `agent: false` `:64` and downgrades any stored
`webSearch !== 'on'` to `'auto'` `:65`, migrating settings whose UI controls were removed `:59-63`.
`writeMap` `:82-95` drops the oldest non-draft entries above 200 `:84-89`.

**State & side effects** — localStorage read/write through the injected `StorageLike` `:71`, `:91`.

**Dependencies** — Inbound: `components/ChatApp.tsx:27-34`, `components/Composer.tsx:23` (type),
`lib/streams.ts:23` (type), `tests/prefs.test.ts:9`. Outbound: `./types` `:9`, `./history` (type) `:10`.

**Config** — none (env); storage key `:39`.

**Failure modes** — both read `:77-79` and write `:92-94` swallow every error, so a full quota silently
loses preferences (documented as acceptable `:93`).

**Concurrency** — every operation is a full read-modify-write of one JSON blob `:113-115`, `:124-127`,
`:134-137`; two tabs writing concurrently lose one another's changes (last-write-wins).

**Complexity hotspots** — none.

**Notable** — `WebSearchMode` `'off'` is unreachable from the UI (`sanitize` `:65` maps it to `'auto'`),
but the value is still forwarded to the backend as `web_search` (`lib/streams.ts:327`).
`MAX_ENTRIES = 200` `:42`.

### frontend/lib/searchPalette.ts  (379 LOC)
**Purpose** — Headless model for the search palette: wire parsing, date bucketing, row model, snippets,
keyboard maps, debounce, and the app-wide shortcut table.

**Public surface**
- `export type SearchMatch = 'title' | 'message'` `:18`; `export interface SearchResult` `:21-31`.
- `export function parseSearchResults(body: unknown, fallbackTime = Date.now()): SearchResult[]` `:39-70`.
- `export function resultsFromSummaries(conversations: ConversationSummary[]): SearchResult[]` `:77-89`.
- `export type DateGroupLabel` `:93`; `export const DATE_GROUP_ORDER` `:96-101`.
- `export function dateGroup(updatedAt: number, now = Date.now()): DateGroupLabel` `:116-122`.
- `export type PaletteRow` `:126-128`; `export interface PaletteSection` `:130-134`;
  `export interface PaletteModel` `:136-140`.
- `export function buildPaletteModel(results, now = Date.now()): PaletteModel` `:151-179`.
- `export function rowSnippet(result: SearchResult): string | null` `:186-188`.
- `export const SNIPPET_WIDTH = 120` `:193`;
  `export function buildSnippet(content, query, width = SNIPPET_WIDTH): string | null` `:204-224`.
- `export type PaletteKeyAction` `:228-234`;
  `export function paletteKeyAction(key, current, count): PaletteKeyAction | null` `:243-260`.
- `export function trapFocusIndex(current, count, backwards): number` `:263-271`.
- `export const SEARCH_MAX_QUERY = 100` `:276`; `export function normalizeQuery(raw: string): string` `:278-280`.
- `export const SEARCH_DEBOUNCE_MS = 150` `:283`; `export interface Debounced<Args>` `:285-288`;
  `export function createDebounce<Args>(fn, delayMs = SEARCH_DEBOUNCE_MS): Debounced<Args>` `:295-315`.
- `export type ShortcutAction` `:319-324`; `export interface ShortcutEvent` `:326-331`;
  `export interface ShortcutContext` `:333-339`;
  `export function shortcutAction(event, ctx): ShortcutAction | null` `:353-379`.
- module-local `startOfDay` `:103-107`.

**Control flow** — `parseSearchResults` `:39-70` accepts either `{results:[…]}` or a bare array `:43-47`
and skips rows without a string `id` `:51-53`; `buildPaletteModel` `:151-179` buckets by date then
assigns indices in **render** order `:167-176`; `shortcutAction` `:353-379` handles the modifier chords
first `:361-367`, then Escape `:369-372`, then `/` `:374-376`.

**State & side effects** — `createDebounce` `:295-315` owns a `setTimeout` handle in a closure. Nothing else.

**Dependencies** — Inbound: `components/SearchPalette.tsx:40-53`, `components/ChatApp.tsx:36`,
`lib/mockApi.ts:11`. Outbound: `./historyApi` `:12`, `./types` `:13`.

**Config** — none.

**Failure modes** — every parse path degrades to `[]` rather than throwing `:43-47`, `:51-53`.
`createDebounce`'s timer is only cleared by an explicit `cancel()` `:307-314` — the caller
(`SearchPalette.tsx:172-179`) does this on unmount.

**Concurrency** — sync, pure apart from the debounce timer.

**Complexity hotspots** — none > 60 LOC; `buildPaletteModel` `:151-179` = 29 LOC.

**Notable** — `dateGroup` `:116-122` rounds the day delta explicitly to survive 23/25-hour DST days `:109-115`.
`SNIPPET_WIDTH = 120` `:193`, `SEARCH_DEBOUNCE_MS = 150` `:283`, `SEARCH_MAX_QUERY = 100` `:276`.
`buildSnippet` `:204` is used only by `lib/mockApi.ts:137` (the real snippets come from the orchestrator).

---

# CONFIG & BUILD

### frontend/app/globals.css  (530 LOC)
**Purpose** — Tailwind entry plus the entire design-token system and every hand-written animation/
component class.

**Public surface** (CSS custom properties and classes referenced from TSX)
- `:root` token block `:13-81` — brand `:15-19`, dark surfaces `:26-42`, engine identities `:44-56`,
  chart palette `--ts-chart-1..5` `:60-64`, type scale `:67-71`, radius/hover `:73-74`, SQL syntax `:77-80`.
- `html.light` overrides `:83-111`.
- Base: `html,body` `:115-118`, `body` `:120-126`, `:focus-visible` `:129-133`, `::selection` `:135-137`,
  `a` `:139-141`, universal scrollbar `:144-147`.
- Animation classes: `.stream-caret` `:162-171`, `.shimmer-line` `:182-193`, `.thinking-shimmer` `:207-219`,
  `.ts-spinner` `:228-235`, `.menu-pop` `:250-253`, `.palette-panel` `:268-270`,
  `.palette-backdrop` `:281-283`, `.drawer-panel` `:287-290`, `.ctx-pulse` `:528-530`.
- Markdown block `.md …` `:305-411` (incl. `.md-table-wrap` `:373-377`, `code.inline-code` `:404-411`).
- Code block `.code-block pre` `:415-421`; SQL token colors `.tok-kw|str|num|com` `:423-439`.
- File badges `.file-icon-*` `:443-462`.
- `@media (prefers-reduced-motion: reduce)` `:466-494`.
- Mermaid hosts `.mermaid-host svg` `:498-503`, `.mermaid-full svg` `:508-512`.

**Control flow** — n/a (declarative). Tailwind layers imported at `:1-3`.

**State & side effects** — none.

**Dependencies** — Inbound: `app/layout.tsx:12`. Consumed indirectly by every component through the
Tailwind token mapping in `tailwind.config.ts:18-40`.

**Config** — none.

**Failure modes** — `color-mix(in srgb, …)` is used in TSX (`EngineBadge.tsx:72-73`,
`MessageRow.tsx:175`) but not here; browsers without `color-mix` render a transparent border.

**Concurrency** — n/a.

**Complexity hotspots** — the `:root` block `:13-81` is 69 lines of tokens.

**Notable** — `--ts-teal` `:19`, `--ts-danger-soft` `:39`/`:97`, `--ts-accent-soft` `:37`/`:95`,
`--ts-fs-*` `:67-71` are defined but only partially used; `--ts-engine-agent-ink` `:56` is consumed by
`AgentTimeline.tsx:58`. The reduced-motion block `:466-494` correctly disables the four bespoke
animations. `.md th/.md td { white-space: nowrap }` `:385-391` relies on `.md-table-wrap`'s
`overflow-x:auto` `:374` so wide model tables scroll rather than break the 768 px thread.

### frontend/tailwind.config.ts  (70 LOC)
**Purpose** — Maps semantic Tailwind color/size names onto the CSS variables in `globals.css`.

**Public surface** — `const config: Config` `:9-68`, `export default config` `:70`.
- `darkMode: 'class'` `:10`; `content` globs `:11-15` (`./app`, `./components`, `./lib`);
  `colors` `:18-40`; `fontFamily` `:41-44`; `fontSize` `:45-52`; `borderRadius.ts` `:53-55`;
  `transitionDuration.ts` `:56-58`; `maxWidth.thread = 768px` `:59-61`; `width.sidebar = 260px` `:62-64`;
  `plugins: []` `:67`.

**Control flow** — n/a. **State & side effects** — none.

**Dependencies** — Inbound: `postcss.config.mjs:4`. Outbound: `tailwindcss` type `:1`.

**Config** — none. **Failure modes** — none. **Concurrency** — n/a. **Complexity hotspots** — none.

**Notable** — `content` `:11-15` does **not** include `./app/api/**` (correct, no classes there) but also
omits any `./tests` path; Tailwind's JIT therefore cannot see classes used only in tests. Every color is a
`var()` reference `:19-39`, so Tailwind's opacity modifiers (`bg-accent/10`, used at
`ProofDrawer.tsx:109`) only work because the vars are plain hex — an `oklch()` token would silently break
them.

### frontend/next.config.mjs  (8 LOC)
**Purpose** — Next.js build configuration.

**Public surface** — `const nextConfig` `:2-6`: `output: 'standalone'` `:3`, `reactStrictMode: true` `:4`,
`poweredByHeader: false` `:5`; `export default nextConfig` `:8`.

**Control flow** — n/a. **State & side effects** — `output:'standalone'` produces
`.next/standalone/server.js`, which the Dockerfile copies (`Dockerfile:26`, `:30`).

**Dependencies** — Inbound: the Next.js CLI. Outbound: none.

**Config** — none read here; `ORCHESTRATOR_URL` is read at request time in `lib/proxy.ts:10`.

**Failure modes** — **no `headers()` function**: the app ships with no `Content-Security-Policy`,
no `X-Frame-Options`/`frame-ancestors`, no `Referrer-Policy`, no `X-Content-Type-Options`,
no `Permissions-Policy`. `reactStrictMode: true` `:4` double-invokes effects in development, which is why
the mount effect at `ChatApp.tsx:141` has to be idempotent.

**Concurrency** — n/a. **Complexity hotspots** — none.

**Notable** — no `images` config (the app avoids `next/image` entirely — `TechSaraMark.tsx:9`,
`Composer.tsx:242`, `MessageRow.tsx:42`), no `experimental` flags, no `eslint`/`typescript`
build-error suppression (so `next build` will fail on a type or lint error).

### frontend/tsconfig.json  (27 LOC)
**Purpose** — TypeScript configuration.

**Public surface** — `compilerOptions` `:2-24`: `target: ES2022` `:3`, `lib` `:4`, `allowJs: true` `:5`,
`skipLibCheck: true` `:6`, **`strict: true`** `:7`, `noEmit` `:8`, `esModuleInterop` `:9`,
`module: esnext` `:10`, `moduleResolution: bundler` `:11`, `resolveJsonModule` `:12`,
`isolatedModules` `:13`, `jsx: preserve` `:14`, `incremental` `:15`, next plugin `:16-20`,
path alias `@/* → ./*` `:21-23`; `include` `:25`; `exclude: ["node_modules"]` `:26`.

**Control flow** — n/a. **State & side effects** — `incremental: true` `:15` writes
`frontend/tsconfig.tsbuildinfo` (present on disk, 187 KB, not in `.dockerignore`).

**Dependencies** — Inbound: `next build`, `vitest`, the editor. Outbound: none.

**Config** — none. **Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.

**Notable** — `strict: true` `:7` is on, but the stricter switches are **not**:
`noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`,
`noPropertyAccessFromIndexSignature`, `noFallthroughCasesInSwitch`, `noUnusedLocals`,
`noUnusedParameters`. That is why `ESCAPES[c]` (`lib/chartOption.ts:177`) and `SERIES_FALLBACK[i]`
(`lib/chartTheme.ts:101`) type-check as `string` rather than `string | undefined`.
`skipLibCheck: true` `:6` hides type errors in dependency `.d.ts` files.

### frontend/package.json  (36 LOC)
**Purpose** — Package manifest.

**Public surface** — `name: techsara-frontend` `:2`, `private: true` `:4`;
scripts `dev/build/start/lint/test` `:5-11`;
dependencies `:12-23` — `@fontsource/ibm-plex-sans` `:13`, `@fontsource/jetbrains-mono` `:14`,
`echarts ^5.6.0` `:15`, `echarts-for-react ^3.0.6` `:16`, `mermaid ^11.16.0` `:17`, `next ^15.5.0` `:18`,
`react ^19.1.0` `:19`, `react-dom ^19.1.0` `:20`, `react-markdown ^10.1.0` `:21`, `remark-gfm ^4.0.1` `:22`;
devDependencies `:24-35` — `@types/*`, `autoprefixer` `:28`, `eslint ^8.57.1` `:29`,
`eslint-config-next ^15.5.0` `:30`, `postcss` `:31`, `tailwindcss ^3.4.17` `:32`, `typescript ^5.6.3` `:33`,
`vitest ^3.2.0` `:34`.

**Control flow** — n/a. **State & side effects** — none.

**Dependencies** — resolved versions in `package-lock.json`: `next@15.5.21` `:7702`, `react-dom@19.2.8`,
`mermaid@11.16.0` `:7014`, `dompurify@3.4.12` `:4440`, `echarts@5.6.0` `:4464`,
`react-markdown@10.1.0` `:8488`.

**Config** — none. **Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.

**Notable** — `eslint ^8.57.1` `:29` is the end-of-life ESLint 8 line (ESLint 9 has been the supported
line since 2024) and pairs with the legacy `.eslintrc.json` format. There is **no** testing-library,
jsdom, `@vitest/coverage-*`, or Playwright dependency — consistent with the total absence of component
tests. No `typecheck` script; type checking only happens implicitly during `next build`.
Everything is bundled locally (fonts via `@fontsource` `:13-14`), so the app makes no CDN requests —
verified at `app/layout.tsx:4-9`.

### frontend/vitest.config.mts  (8 LOC)
**Purpose** — Vitest configuration.

**Public surface** — `export default defineConfig({ test: { include: ['tests/**/*.test.ts'], environment: 'node' } })` `:3-8`.

**Control flow** — n/a. **State & side effects** — none.

**Dependencies** — Inbound: `npm test` (`package.json:10`). Outbound: `vitest/config` `:1`.

**Config** — none.

**Failure modes** — **`include` matches only `.test.ts`** `:5`, and **`environment: 'node'`** `:6` means
there is no DOM. Both together make it impossible for any of the 32 components (5,618 LOC) to be tested,
and they are not: `ls frontend/tests/` returns 16 `.ts` files and zero `.tsx`.

**Concurrency** — n/a. **Complexity hotspots** — none.

**Notable** — no `coverage` config, no `setupFiles`, no path alias for `@/*` (tests therefore import with
relative paths — e.g. `tests/chartOption.test.ts:19` uses `'../lib/chartOption'`).

### frontend/.eslintrc.json  (3 LOC)
**Purpose** — ESLint configuration.

**Public surface** — `{ "extends": ["next/core-web-vitals", "next/typescript"] }` `:2`.

**Control flow** — n/a. **State & side effects** — none.

**Dependencies** — Inbound: `next lint` (`package.json:9`). Outbound: `eslint-config-next`
(`package.json:30`).

**Config** — none. **Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.

**Notable** — legacy `.eslintrc` format (ESLint 9 flat config is not used). No custom rules, no
`react-hooks/exhaustive-deps` escalation to `error`, no `jsx-a11y` beyond what `core-web-vitals` bundles,
no `no-console` rule (there is exactly one `console.error`, at `ChartErrorBoundary.tsx:43`, already
suppressed with an inline disable `:42`). Three other inline disables exist:
`Composer.tsx:242`, `MessageRow.tsx:42`, `TechSaraMark.tsx:9`, all `@next/next/no-img-element`.

### frontend/Dockerfile  (30 LOC)
**Purpose** — Three-stage arm64-compatible image producing a Next.js standalone server.

**Public surface**
- `deps` stage `:4-7`: `node:20-alpine`, `COPY package.json package-lock.json` `:6`,
  `RUN npm ci --no-audit --no-fund` `:7`.
- `build` stage `:10-15`: `ENV NEXT_TELEMETRY_DISABLED=1` `:12`, copies `node_modules` `:13`,
  `COPY . .` `:14`, `RUN npm run build` `:15`.
- `run` stage `:18-30`: `ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0` `:20-23`,
  non-root user `:24`, copies `public` `:25`, `.next/standalone` `:26`, `.next/static` `:27`,
  `USER nextjs` `:28`, `EXPOSE 3000` `:29`, `CMD ["node","server.js"]` `:30`.

**Control flow** — deps → build → run, standard multi-stage.

**State & side effects** — no secrets are referenced; nothing is written outside the image.

**Dependencies** — Inbound: `docker-compose.yml` `frontend: build: ./frontend` (verified at
`docker-compose.yml:346-347`). Outbound: `node:20-alpine`.

**Config** — build-time env: only `NEXT_TELEMETRY_DISABLED` `:12`. Runtime env: `NODE_ENV`, `PORT`,
`HOSTNAME` `:20-23`; `ORCHESTRATOR_URL` and `NEXT_PUBLIC_APP_NAME` are supplied by
`docker-compose.yml:348-350`.

**Failure modes**
- **`NEXT_PUBLIC_APP_NAME` is never passed as a build ARG** `:9-15`, so `process.env.NEXT_PUBLIC_APP_NAME`
  at `ChatApp.tsx:68` and `app/layout.tsx:15` is inlined as `undefined` at build time and the fallback
  `'TechSara AI'` always wins. Setting it in `docker-compose.yml:350` has no effect.
- No `HEALTHCHECK`; compose only uses `condition: service_started` for the orchestrator
  (`docker-compose.yml:353-355`).
- `COPY --from=build /app/public ./public` `:25` runs without `--chown`, leaving it root-owned
  (read-only for `nextjs`, which is fine).
- `npm ci` `:7` has no `--omit=dev`, but the standalone output already excludes dev deps.

**Concurrency** — n/a. **Complexity hotspots** — none.

**Notable** — pinned only to `node:20-alpine` (a floating tag), so builds are not reproducible.
The image runs as a non-root user `:24`, `:28` — good.

### frontend/.dockerignore  (8 LOC)
**Purpose** — Build-context exclusions.

**Public surface** — `node_modules` `:1`, `.next` `:2`, `.git` `:3`, `Dockerfile` `:4`,
`.dockerignore` `:5`, `npm-debug.log` `:6`, `tests` `:7`, `*.md` `:8`.

**Control flow** — n/a. **State & side effects** — none. **Dependencies** — Inbound: the Docker build.

**Config** — none.

**Failure modes** — it does **not** exclude `.env`, `.env.local`, `.env.*`, or `tsconfig.tsbuildinfo`.
No `frontend/.env*` currently exists (verified with `ls -la frontend/` — the directory contains no dotenv
file), so nothing is leaked today, but a future `.env.local` would be copied into the build layer by
`Dockerfile:14` and any `NEXT_PUBLIC_*` value in it would be inlined into the client bundle.

**Concurrency** — n/a. **Complexity hotspots** — none.

**Notable** — excluding `tests` `:7` means the image cannot run `npm test`; that is intentional for a
runtime image.

### frontend/postcss.config.mjs  (9 LOC)
**Purpose** — PostCSS pipeline for Tailwind.

**Public surface** — `const config = { plugins: { tailwindcss: {}, autoprefixer: {} } }` `:2-7`;
`export default config` `:9`.

**Control flow** — n/a. **State & side effects** — none.

**Dependencies** — Inbound: the Next.js CSS pipeline. Outbound: `tailwindcss` `:4`, `autoprefixer` `:5`
(`package.json:28`, `:32`).

**Config** — none. **Failure modes** — none. **Concurrency** — n/a. **Complexity hotspots** — none.

**Notable** — no `cssnano`; Next.js minifies CSS itself in production builds.

---

# CONTEXT FILES (read to verify claims, not part of the assignment)

- `frontend/app/layout.tsx` (44 LOC) — `metadata` `:17-25`; the pre-hydration theme script injected with
  `dangerouslySetInnerHTML` `:31`, `:37` (a static, author-controlled string — safe, but it is the reason a
  CSP would need `'unsafe-inline'` or a nonce); `Providers` wraps the tree `:40`;
  `NEXT_PUBLIC_APP_NAME` at `:15`.
- `frontend/app/page.tsx` (5 LOC) — renders `<ChatApp />` `:4`.
- `frontend/lib/proxy.ts` (64 LOC) — `orchestratorUrl()` `:9-11` reads `ORCHESTRATOR_URL` **server-side**;
  `proxyToOrchestrator` `:21-64` forwards cookies both ways `:26-27`, `:56-58`, `cache:'no-store'` `:40`,
  and returns a 502 JSON body on a network failure `:43-48`. **No timeout on the upstream `fetch`** `:33`.
- `frontend/lib/history.ts` (851 LOC) — the quota policy that matters here:
  `createCache().writeAll` `:157-180` retries `setItem` and, on `QuotaExceededError`, deletes the
  **oldest** conversation and loops (`:164-178`); when `current.length` reaches 1 it still splices, so the
  loop can empty the cache entirely. `saveMessages` `:273-280` stores the **whole** `ChatMessage[]`
  including `imageDataUrl`. `toServerMessage` `:394-396` sends only `{role, content, meta}`.
  `loadConversation` `:566-573` rebuilds messages from server rows with ids `srv-{id}-{i}` and **no**
  `imageDataUrl`/`pdfName`. `syncConversation` `:501-509` no-ops when the conversation is missing locally.
- `frontend/lib/streams.ts` (393 LOC) — module-level `streams` Map `:50` and `listeners` Set `:51`;
  `notify(s.conversationId)` fires **inside the per-event loop** `:262`, i.e. once per SSE token;
  `register()` `:271-295` overwrites any existing stream for the same conversation **without aborting it**
  `:292`; `attachStream` `:355-393` always force-loads server truth `:364` before registering.
- `frontend/lib/contextMeter.ts` (147 LOC), `frontend/lib/errors.ts` (104 LOC),
  `frontend/lib/auth.ts` (29 LOC), `frontend/lib/historyApi.ts` (256 LOC),
  `frontend/lib/types.ts` (262 LOC) — read in full; no defects that belong to this assignment.

---

# FINDINGS SUMMARY (detail in the returned JSON)

| # | Sev | Where | What |
|---|-----|-------|------|
| 1 | P1 | `Composer.tsx:42` + `ChatApp.tsx:412` + `history.ts:157-180` | A ≤10 MB image attachment is persisted as a ~13.6 MB base64 data URL into localStorage; the quota loop evicts **every** conversation and the turn never reaches the server |
| 2 | P1 | `Markdown.tsx:55-72` + `next.config.mjs:1-8` | Model-authored markdown images cause silent outbound HTTP requests; no `img` override and no CSP — breaks "nothing leaves this machine" and enables prompt-injection exfiltration |
| 3 | P2 | `ContextMeter.tsx:57-71` | Outside-click guard checks only the trigger, not the portalled panel — any pointerdown inside the popover dismisses it and "Compact now" is unreachable by mouse |
| 4 | P2 | `attachments.ts:58-74` + `history.ts:566-573` | After any forced server reload the thread loses `imageDataUrl`/`pdfName`, so regenerate silently re-asks a vision question with no attachment and reports nothing |
| 5 | P2 | `ChatApp.tsx:84,255-272` + `MessageRow.tsx:21` | Every SSE token from **any** conversation re-renders the entire ChatApp tree; no component memoization below `Markdown` |
| 6 | P2 | `vitest.config.mts:4-7` | Zero component tests are even possible: `.test.ts` only, `environment: 'node'` |
| 7 | P3 | `Sidebar.tsx:296-322` | The sidebar body renders twice whenever open — duplicate DOM ids and doubled work; plus `aria-hidden` over a focusable collapsed aside |
| 8 | P3 | `ChartErrorBoundary.tsx:28-48` | The boundary never resets; one transient chart throw blanks that chart permanently |
| 9 | P3 | `Dockerfile:9-15` + `docker-compose.yml:350` | `NEXT_PUBLIC_APP_NAME` is set at runtime but inlined at build time — the setting silently does nothing |
| 10 | P3 | `Composer.tsx:192-202` | `FileReader` has no `onerror`: a failed read produces no chip, no toast, no log |
| 11 | P3 | `csv.ts:5-10` | CSV cells beginning `=`/`+`/`-`/`@` are written verbatim — formula injection when opened in Excel |
