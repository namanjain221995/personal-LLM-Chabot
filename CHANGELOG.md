# Changelog

## Unlimited-OCR — dedicated document OCR in the model stack (2026-08-06)

New resident model: **baidu/Unlimited-OCR** (3.3B document-OCR VLM, MIT,
`vllm-ocr` service on port 8004). The vLLM nightly already on this box has
native `UnlimitedOCRForCausalLM` support, so it is the same runtime — no new
image. Weights live in `vllm_models/unlimited-ocr` (~6.7 GB); the unified
memory budget moves from ≈0.53 to ≈0.61, the accepted ceiling.

What it does: every **uploaded image** and every **rendered PDF page** is
transcribed by the OCR model first ("document parsing" prompt, detection
markup stripped), and the transcript is handed to the main model alongside
the pixels — scans, invoices, tables and handwriting now read reliably
instead of depending on the general VLM's OCR. The PDF path emits a
"Reading N pages with OCR…" status line.

Failure policy: OCR is an enhancer, never a gate (`engines/ocr.py`). Service
down, still loading, or `OCR_ENABLED=false` → empty transcript, pixels-only
answer, exactly the pre-OCR behavior. Everything stays on this machine.

Boot tuning (measured through two crash-loops): 0.08 + CUDA graphs left no
KV memory at all; the working config is **0.09 + `--enforce-eager` +
`--max-model-len 8192`** (client output capped at 6000). While the first
config crash-looped (21 restarts) it thrashed the box enough that ordinary
chats timed out — if "the model server did not respond" ever appears again,
check `docker compose ps` for a restarting model container FIRST. The
transcript cleaner strips the live output format ("type [bbox]Content"
lines), verified against the real served model.

## Small files ship whole — CSV/tables the model can actually compute on (2026-08-06)

Uploaded datasets were profile-only by design (5 sample rows + statistics),
so "sum the salary column" was honestly refused. Now a table at or under
**PROFILE_FULL_ROWS_MAX=200 rows** (and under PROFILE_FULL_CHARS=60000 once
serialized) ships to the model IN FULL as `full_rows` — cells still clipped,
still delimited as untrusted data — and the system prompt splits the honesty
rule: full-content files may be aggregated exactly; larger files keep the
profile-only limits and say so. The canary leak tests (row-500 secret, long
top-value) still pass untouched — those files are far over the threshold.
Also fixed: the user-bubble chip labelled every non-image attachment "PDF";
it now shows the real extension (CSV, XLSX, ZIP…).

## Multi-image upload — up to 5 images per message (2026-08-05)

The composer accepted exactly one attachment; now it takes **up to 5 images**
in one turn (multi-select in the picker, or add one by one — paste included),
each with its own removable chip. A PDF or dataset still stands alone: they
run different server paths, so picking one replaces the images and vice
versa. The user bubble shows every thumbnail.

Contract: the frontend sends `images: [...]` alongside the legacy
`image_base64` (kept as the first image, so single-image requests produce the
exact v1 key set). `ChatRequest` validates the ceiling (`MAX_IMAGES = 5` —
five 10 MB base64 uploads ≈ 67 MB of JSON, a deliberate cap) and the vision
engine sends one `image_url` part per image to the multimodal model.
Regenerate/retry re-send ALL images of a turn (`lib/attachments.ts` now
remembers arrays; if any preview is unrecoverable after a reload it reports
missing rather than silently re-asking with fewer images).

## Message rows — ChatGPT-style actions, Activity panel, no empty box (2026-08-05)

Four owner requests in one pass:

- **The empty "• Chat" box is gone.** The proof drawer only rendered an
  engine badge for plain chat answers — an empty frame under every message.
  It now renders nothing unless there is something to prove (SQL, sources,
  data, a chart, files).
- **ChatGPT-style action row.** Copy · 👍 · 👎 · try again as quiet icon
  buttons instead of labelled chips. Thumbs feedback persists client-side
  per message (`lib/feedback.ts`, localStorage; there is no server feedback
  endpoint yet).
- **Activity panel.** A "Sources" book button appears on any finished answer
  that thought or searched; it opens a right-side drawer (ChatGPT's
  Activity) showing the thinking with its duration and the web research —
  sources, domains, every search, elapsed time. Inline reasoning/research
  now show only WHILE streaming; finished messages stay clean.
- **Pure white ink.** Dark theme `--ts-text` was #f5f5f5 and read as dull;
  now #ffffff (muted/faint lifted a step to keep the hierarchy).

## Composer "+" menu — ChatGPT-style attach & tools popover (2026-08-05)

The bare paperclip is gone. The composer now opens with a **"+" button** whose
popover offers three things, exactly like ChatGPT's attach menu:

- **Add photos & files** — the old paperclip path, unchanged: images (≤10 MB),
  PDFs (≤25 MB) and datasets (≤200 MB) through the same hidden input.
- **Web search** — a FORCE toggle, shown **only while Salesforce is off**.
  Auto ("the level decides", 2026-07-28) is still the default and needs no
  checkmark; checking this sends `web_search: "on"`. While forced, a
  dismissible **Web search pill** appears next to the "+" so the active tool
  is visible, and clicking it returns to auto.
- **Salesforce** — the toggle now lives HERE, not as a second always-visible
  pill (owner request: no double controls). A dismissible Salesforce pill
  appears next to the "+" only while the mode is ON — click it (or the menu
  item) to turn off. Turning it ON also drops a forced web search to auto.

**Salesforce mode never searches the web — at any effort level (Fast, Low,
Medium, High).** That was already true for auto-detection (2026-07-28); now
the server refuses even an explicit `web_search: "on"` in Salesforce mode
(previously an escape hatch), the menu hides the toggle there, and stored
prefs that combine both are normalized on load. The trust footer's
"no web search · nothing leaves this machine" line is therefore
unconditional again. All menu decisions (items, toggle transitions, footer
wording) live in `frontend/lib/composerMenu.ts` as pure functions with
vitest coverage; `AttachMenu.tsx` is a thin shell following the ModelPicker
popover pattern, and both the pill and the menu flip Salesforce through the
same tested transition.

Review fallout, all fixed: **every `accent/NN` Tailwind class in the app
compiled to no CSS at all** (the `accent` color was a bare `var()`, so
opacity modifiers were silently dropped — the Salesforce pill's teal tint,
citation hover borders and the research progress fill never rendered; now
`rgb(var(--ts-accent-rgb) / <alpha-value>)`). Escape closing any composer
popover (AttachMenu, ModelPicker, ContextMeter) no longer ALSO stops a
streaming answer — the popover consumes the key. The "+" menu moves focus
into itself on open, honours Tab-to-close, and its arrow keys skip the
disabled upload row while streaming. The trust footer no longer dims its
strongest warning (Salesforce off + search forced on).

## Login removed — single-user local mode (2026-07-28)

No sign-in, no sign-up, no password, no session cookie. Open the app and it is
your assistant. Chat history is completely unaffected.

Removed: `/auth/login`, `/auth/register`, `/auth/logout` (all 404 now), the
`/login` page, `middleware.ts` route gating, argon2 hashing, the signed
`ts_session` cookie, and the session-secret file. A stale cookie left in a
browser is ignored rather than rejected. `GET /auth/me` survives as a plain
"who am I running as", because the frontend history cache is keyed by that name.

**What deliberately did NOT change: ownership.** Conversations, uploads,
documents and repo chunks are still keyed by `user_id`, and `history.py` still
takes the user as a dependency — every request just resolves to ONE local
account. Ripping `user_id` out of every query would have been the edit that
turns "which chats are mine?" into "all rows in the table". Collapsing the
identity instead is small, reversible, and leaves existing history working.

Which account: `LOCAL_USERNAME` if set, otherwise **the oldest existing
account** — so an install that already has history adopts it instead of
starting an empty one. A fresh install creates it on first use.

> **This machine has four accounts.** The app now runs as `naman` (83
> conversations). `namanj` (36 conversations / 181 messages), `heet2910` (3) and
> an empty test account are no longer visible. Nothing was deleted — set
> `LOCAL_USERNAME` to switch, or merge them under one owner.

> **Security:** there is now no authentication whatsoever, and compose publishes
> port 3000 on `0.0.0.0`. Anyone who can reach the port can read every
> conversation and query the Salesforce data. Bind to `127.0.0.1:3000` if the
> network is not fully trusted.

## Research panel — show the searches behind an answer (2026-07-28)

Research is no longer a spinner and a number you have to trust. Every answer
that searched the web now carries a panel showing exactly what was done.

Collapsed it is one line — how many sources, how many were read, and how long
it took, with a live spinner and a running count while the work happens
("Researching… · 82 sources and counting… · 3m 21s"). Expanded it shows a
domain leaderboard with proportional bars, then every search the model ran as
its own group, each expanding to the results it returned with title, domain and
a link.

It is driven by a new `research` SSE event streamed as the work happens —
`{phase: "query", query, results[]}` per search, then `{phase: "reading"|"read",
count}` around the fetch — so the panel fills in live rather than appearing at
the end. Agent web steps feed the same panel, so a multi-step plan reads as one
combined research effort. Measured live: 11 searches, 82 sources, streaming.

The elapsed clock and the panel state are folded onto `meta.research` when the
answer finishes, so reopening a stored conversation replays the panel instead
of losing it.

**Gotcha worth recording:** `sse_event()` raises on any event type not in its
allowlist, and it runs *inside* the streaming response — so emitting an
unlisted event does not degrade a feature, it kills the whole answer mid-stream
with no error event. A test now walks every `emit("…")` in the codebase and
asserts the name is on the allowlist.

## Deep research at High + two citation/stability fixes (2026-07-28)

High-effort research now reads **~90 sources across ~67 distinct domains** on a
research question (measured), up from 10. Medium reads ~28, Low 10.

**Why it was stuck at 10.** `_collect_results` ran every query and then
head-sliced the *combined* list to `SEARCH_MAX_RESULTS=10`. Query 1 alone could
fill that slice, so High's six different searches were thrown away and it
answered from the first one — which is why "high" never read more than
"medium" no matter how many queries it ran. The merge is now round-robin (rank
1 of every query, then rank 2 of every query), so each angle contributes before
any angle contributes twice.

| Level | Queries | Sources/search | Max per domain |
|---|---|---|---|
| Fast | 0 | 0 | — |
| Low | 2 | 10 | 3 |
| Medium | 3 | 15 | 3 |
| High | 6 | 60 | 4 |

Supporting changes: URL normalization (the same page over http/https, a
trailing slash, or `utm_*` tracking params was being read up to three times);
a per-domain cap so a large result set is genuinely many publishers rather than
one SEO-heavy site; tiered text budgets (top 10 sources keep the full 8,000
chars, the long tail is cut to 2,500) so 60 sources is a sane prompt instead of
480k chars of prefill; and an answer prompt that asks the model to use the full
set and surface disagreement between sources.

**Fixed: agent answers cited the wrong sources.** Each web step numbers its own
sources from `[1]`, and `merge_step_meta` renumbered the *metadata* — but never
the `[n]` markers in the step prose the synthesizer actually reads. Step 2's
`[1]` became plan-wide source 4 while the answer still said `[1]`, so any
multi-step research answer cited pages unrelated to the claim. Renumbering now
rewrites prose and metadata together, before synthesis.

**Fixed: DNS blocked the whole event loop.** `socket.getaddrinfo` in the SSRF
check is synchronous and was called directly from `async def safe_fetch`, so
every URL lookup froze the orchestrator — stalling SSE token delivery for every
other user, not just the one searching. Now off-loop, with a split
connect/read timeout. Extraction moved off-loop too, onto a **single-worker**
pool: trafilatura shares module-level compiled lxml objects that are not
thread-safe, so the obvious `asyncio.to_thread` fix can abort the interpreter.

**Search supply was the real ceiling, not the code.** Only four general engines
ship enabled; two CAPTCHA almost immediately and Brave suspends after a few
queries, after which search returned **zero** results and the app quietly
answered from model knowledge. `searxng/settings.yml` now enables ten
no-API-key engines, so load spreads across independent quotas and a suspension
costs a fraction of the results instead of all of them. Measured immediately
after: 0 results → 36/113/18 results on three test queries, with 5–7 engines
still suspended.

## Model layer — one model, four levels (2026-07-28)

The two-model picker is gone. **Qwen3.6-35B-A3B-NVFP4** is now the only model
users talk to, served at its full **262,144-token** context, with
**Qwen3-VL-8B-Instruct-FP8** behind it as the agent/classifier model.

**The picker now chooses effort, not a model** — the separate "Fast" box was
removed. All four levels run the same weights; what changes is how much work
the model is allowed to do:

| Level | Reasoning | Searches | Plan steps | Answer room |
|---|---|---|---|---|
| Fast | no | none | no | 8k |
| Low | no | up to 2 | no | 8k |
| Medium | yes | up to 3 | up to 5 | 8k / 6k synth |
| High | yes | up to 6 | up to 8 | 16k / 12k synth |

**High is guaranteed to do at least what Medium does.** The classifier is a
small model and its answer varies run to run — live, the same question came
back `{agent, search}` at Medium and `{search}` at High, so the level picked
for hard work answered with a one-shot search while the level below it planned.
At High, anything worth searching for is now also worth planning. A question
needing no tools is still answered directly: High does not mean "always slow".

Fast is fast because the reasoning pass is switched off
(`chat_template_kwargs.enable_thinking`), not because it is a smaller model.

**Both the Agent and Web-search toggles were removed from the composer.** The
level decides. The 8B classifies each message and decides whether it needs
planning and/or the web; each level is a **ceiling** the classifier can narrow
but never exceed (`engines/orchestrate.py`). Escalation is announced in the
status line, so it is automatic but never silent. Prefs saved by the old
toggles are migrated on read — a stored "search off" or "agent on" had no
control left to undo it.

**The two models now split the work.** Query rewriting moved from the main
model to the 8B: turning a question into search phrases is a mechanical
rewrite, and spending a 35B reasoning pass on it delayed every search by
seconds before the first fetch even started.

**Better code answers.** A shared `CODE_INSTRUCTION` (fenced blocks with a
language tag, real imports, runnable files over fragments, comments that say
*why*) now goes to the assistant and to agent steps. The thinking levels also
drop to temperature 0.3 — 0.6 invents plausible-looking API names.

**The agent can now search inside its own plan** — a new `web` step kind runs
the Phase 1 pipeline per step and merges the sources into one renumbered
citation list. Previously search was checked first in the route chain, so a
request needing both planning and the web silently lost its plan.

**Fix — every request 400d on the new model.** Qwen3.6's chat template accepts
exactly one system message, at index 0, and this app injects several (engine
prompt, rolling summary, semantic recall, search sources). `llm.normalize_system`
folds them into one leading block on all five send paths.

Measured on the DGX Spark: Fast ~1.2 s, Low ~1.3 s, Medium ~4.8 s, High ~8.7 s
on a one-line question; a research task at Medium plans 4 steps, reads 10
sources, and cites them. Memory: 88 GB used of 121 GB with both models resident.

## Phase 1 — Web Search (2026-07-23)

ChatGPT-style web search with citations, behind config flags (off by default).

**Enable it**
1. In `.env`: `SEARCH_ENABLED=true`, `SEARCH_PROVIDER=searxng`, and a random
   `SEARXNG_SECRET`. (Or `SEARCH_PROVIDER=tavily|brave` with the matching API key.)
2. Start the self-hosted search engine: `docker compose --profile search up -d searxng`
3. Rebuild + restart the orchestrator: `docker compose up -d --no-deps --build orchestrator`
4. In the composer, click the **Web search** pill to cycle Off → Auto → On.

- **Off**: never searches — zero outbound calls (enforced + tested).
- **Auto**: the model decides when fresh/web info is needed.
- **On**: every message searches.

**How it works**: rewrite the question into 1–3 queries → SearXNG → fetch the top
sources through an SSRF-safe path (private/reserved IPs and cloud-metadata are
blocked, redirects re-validated) → extract readable text (trafilatura) → stream a
cited [n] answer with a Sources panel. Results cache for 15 min; per-user rate limit.

> Note: enabling web search sends your **search queries** to the web — the
> "nothing leaves this machine" guarantee applies only to your Salesforce data,
> and the composer footer says so while search is on.

**New env** (see `.env.example`): `SEARCH_ENABLED`, `SEARCH_PROVIDER`,
`SEARXNG_URL`, `SEARXNG_SECRET`, `TAVILY_API_KEY`, `BRAVE_API_KEY`,
`SEARCH_MAX_RESULTS`, `FETCH_TIMEOUT_MS`, `FETCH_MAX_BYTES`, `MODEL_MAX_CONTEXT`,
`MODEL_MAX_OUTPUT`.

## Model window + sources bump (2026-07-23)

- Raised the main model's context window **65536 → 131072** (Qwen3-VL-30B-A3B
  natively supports 262144; GQA with 4 KV heads makes the bigger window cheap).
  `MODEL_MAX_CONTEXT=131072`, `--gpu-memory-utilization 0.50`. Still ~33 GB free.
- Web search sources **6 → 10** (`SEARCH_MAX_RESULTS`, configurable), fetch
  concurrency raised to 10 — the larger window comfortably holds them.

## Phase 2 — URL / Website Analysis (2026-07-23)

Paste one or more links; the page is fetched (SSRF-safe), its readable text
extracted (HTML/PDF/plain), and stored on the conversation so follow-up
questions are answered from stored content — no re-fetch. Large pages are
reduced to the parts relevant to the question. `URL_ANALYSIS_ENABLED=true`
(default), `URL_MAX_PAGES=5`. Answers cite the page [n] and show it in the
Sources panel (route "url" → "Page" badge). Unreadable/403/blocked links fail
with a friendly status line, never a crash. Verified live: Wikipedia summary +
cited follow-up with zero refetch; 403 and the cloud-metadata IP both handled
gracefully.

## Fix — Agent respects the Salesforce toggle (2026-07-23)

The agent engine ignored the Salesforce toggle: it always planned sql/rag steps
against Salesforce, and its llm steps couldn't see the conversation context. So
a follow-up about a shared URL (Agent on, Salesforce off) went digging in
Salesforce Case records instead of the page. Fixed: `run_agent_engine` now takes
`salesforce=(mode != "assistant")`; with Salesforce off the planner offers only
llm steps (any sql/rag is coerced), and every llm step receives the conversation
history so it uses shared URLs/documents. Verified live.

## Phase 3 — GitHub Repository Analysis (2026-07-23)

Paste a public GitHub repo URL → it's shallow-cloned (`--depth 1`) into an
isolated per-conversation workspace, indexed, and an auto-overview is streamed
(languages, entry points, key configs, README summary). Follow-up questions
search the indexed code and answer with `path/to/file.py:Lstart-Lend` citations
that expand into snippets in a "Code" panel.

**Security**: public GitHub only; repo size checked via the GitHub API and hard
file-count/on-disk caps enforced (`REPO_MAX_MB=300`, `REPO_MAX_FILES=20000`) —
an oversized repo is rejected before/after clone with a clear message; git hooks
disabled and credentials/prompts turned off; **repository code is never executed
and its dependencies are never installed** — the clone is read-only data.
Workspaces have a global quota (`WORKSPACE_QUOTA_GB=20`) and TTL cleanup
(`WORKSPACE_TTL_HOURS=24`). Blob (single-file) URLs are recognized.

Retrieval: keyword search over code chunks with stem expansion (so
"authentication" finds `auth.py`) and a doc-file penalty (so real source beats
README/HISTORY prose) — a clean interface to swap in embeddings in Phase 6.
Verified live: psf/requests overview; "where is authentication handled?" cited
`src/requests/auth.py:L1-L60`; torvalds/linux (~6 GB) rejected without cloning.

New env: `REPO_ANALYSIS_ENABLED`, `REPO_MAX_MB`, `REPO_MAX_FILES`,
`WORKSPACE_DIR`, `WORKSPACE_TTL_HOURS`, `WORKSPACE_QUOTA_GB`.

## Mermaid diagrams (2026-07-23)

Diagrams now render like ChatGPT's. The model is instructed (every prose engine)
to emit diagrams as ```mermaid code blocks instead of ASCII art, and the UI
renders those blocks as real diagrams.

**Diagram block UI** (`components/MermaidBlock.tsx`): header bar with
Code / Preview toggle, fullscreen viewer (zoom 25–400% + Reset), **Download
PNG** (2× scale, solid background; falls back to SVG if rasterization is ever
blocked) and Copy source. mermaid (~1 MB) is lazy-loaded only when a diagram
appears, re-renders on theme change, and is initialized with
`securityLevel: 'strict'` — model output never becomes raw HTML.

Two implementation notes worth keeping:
- `htmlLabels: false` is REQUIRED. Mermaid's default draws labels in
  `<foreignObject>`, which taints the canvas and makes PNG export fail with
  "Tainted canvases may not be exported".
- While an answer streams, the mermaid source is incomplete and would throw, so
  rendering waits until `looksRenderable()` sees a known diagram header plus a
  body; until then the code is shown.

Also fixed: the chat engine's token budget was still 2048, so longer asks
("draw a flowchart of …") burned the whole budget on reasoning and returned an
EMPTY answer. Raised to 8000 (assistant) / 6000 (Salesforce chat), matching the
other engines.

## Diagram quality + ChatGPT-style background generation (2026-07-27)

**Mermaid v2** (user feedback: unreadable colors, broken fullscreen zoom,
"Syntax error" bombs, diagrams appearing in normal chats):
- `suppressErrorRendering: true` — invalid mermaid no longer appends red
  "Syntax error in text" bomb elements to the page; the block quietly shows
  the source with an error note instead.
- Full dark-theme variable set (node/cluster/edge-label/note/actor colors) —
  readable contrast on pure black; the model is now also forbidden from using
  style/classDef color overrides that broke dark mode.
- Fullscreen viewer reworked: opens at **fit-to-screen** (computed from the
  SVG viewBox vs. the viewport), zoom is ×1.25 steps over a 10%–400% range
  with Fit and 100% buttons, and zooming resizes the real layout (crisp
  vectors + working scrollbars) instead of a CSS transform.
- The diagram prompt is now conservative: diagrams ONLY when asked or for
  genuinely complex explanations, at most one, small, followed by a plain
  1–2 sentence explanation. Normal chat answers stay diagram-free (verified).

**Background generation (ChatGPT behavior)**:
- The orchestrator now runs every generation DETACHED (`LiveGeneration`):
  closing the browser/tab/stream no longer kills the model call. New
  endpoints: `GET /chat/active` (what's running), `POST /chat/stop`
  (explicit cancel — the Stop button uses this now), and
  `GET /chat/attach/{id}` (re-join with full replay of the partial answer).
- If a generation finishes while nobody is connected, the orchestrator saves
  the answer into the conversation itself, so it's waiting in history.
- Frontend: streams moved out of the view into `lib/streams.ts` (one live
  stream per conversation). Switching chats or starting a new chat leaves
  generations running; the sidebar shows a spinner on busy rows (hover swaps
  it for the ⋯ menu); the app polls `/api/chat/active` to keep spinners
  correct across reloads.
- Reload fix: the open chat now writes `?c=<id>` into the URL, so a reload
  restores the same conversation — and if it was still generating, the view
  re-attaches to the live stream mid-answer.

Verified live end-to-end: reload restores the chat; spinner shows while a
backgrounded chat generates; a reload mid-generation re-attaches and
completes with no duplicate messages; fullscreen opens at 84% fit for an
oversized flowchart; PNG export works; "capital of France" gets a one-line
answer with no diagram. Tests: 338 backend / 147 frontend.

### Sidebar busy-spinner fixes (2026-07-27)

Three defects in the spinner shipped earlier today:

1. **It slid up and down.** The element combined `-translate-y-1/2` (for
   vertical centering) with `animate-spin`. Tailwind's spin keyframe is
   `to { transform: rotate(360deg) }`, which REPLACES the whole transform;
   CSS then interpolates `translateY(-50%)` → `rotate(360deg)` as matrices,
   so the spinner slid down ~7px and snapped back every cycle. Never combine
   a positioning transform with a transform animation on one element.
2. **It overlapped the "⋯" menu** on the ACTIVE row, where the menu is
   permanently visible — which is exactly the row a running chat is usually on.
3. **It kept spinning after the answer arrived** (up to 8s), because the
   polled `serverActive` set was only refreshed on the next interval.

Fix: the spinner moved INTO the row's flex flow (right after the title, inside
the existing `pr-9` gutter) — no absolute positioning, no transform conflict,
no overlap in any state, and it stays visible on hover. Finished streams now
drop out of `serverActive` immediately. Verified live: center drift 0.000px on
both axes over 2s, no overlap with the menu, cleared 3ms after completion.

**Security:** `/chat/stop`, `/chat/active` and `/chat/attach/{id}` are now
owner-scoped (`_owns`) — previously any caller could attach to and read
another user's in-progress answer, cancel their generation, or enumerate
active conversation ids. The three Next proxies now forward the session
cookie (without it the ownership check would 404 legitimate users).

## Phase 0-critical — conversation integrity + authorization (2026-07-27)

Four defects found by an adversarial review of the detached-generation work,
fixed before any context-system changes were built on top of them.

**#1 — a sync could permanently DELETE a conversation.** `attachStream` seeded
the re-joined thread from the LOCAL cache. When that cache entry was empty —
a chat the server listed but this browser had never opened, or one dropped by
a localStorage quota purge — the replayed answer became the entire thread, and
the sync's diverged-tail path did `DELETE` + recreate from it, cascading away
every earlier message.
- Server invariant: new `PUT /history/conversations/{id}/messages` replaces a
  thread atomically and **refuses (409) any replace that would reduce the
  message count**. `db.replace_messages` raises `MessageCountWouldShrink`.
- The client no longer deletes-and-recreates at all: `pushAll` upserts and
  calls replace. On 409 it adopts server truth instead of overwriting it.
- `attachStream` now seeds from server truth (`load(id, {force:true})`), never
  from whatever this browser happened to have cached.

**#2 — reloading mid-answer, then typing, silently killed the generation.**
The composer was live for 1–3s before the app learned a generation was still
running; a send in that gap cancelled it, and cancelled generations don't
persist, so the partial answer vanished with no error. The composer is now
locked (with a "Restoring this chat…" placeholder) until the check completes,
and the check runs even when `/api/auth/me` fails with a non-401 — previously
that left the guard permanently un-run. The lock is applied in a layout effect:
a `useState` initializer cannot do it, because it runs during SSR where there
is no `location` and hydration keeps the server's value.

**#3 — Regenerate on an older answer silently truncated the chat.** It is
offered on every assistant row and restarts from that turn, discarding all
later ones. It now asks first ("This will delete all messages after this
point (N messages)") via a new portalled `ConfirmDialog`; regenerating the
last answer still runs immediately.

**#4 — per-conversation stores were not authorized.** `url_documents` and
`repo_chunks` are keyed by conversation id alone, so a guessed id could pull
another account's fetched pages and indexed source into a prompt. `POST /chat`
now verifies the caller owns the conversation (404 otherwise, indistinguishable
from missing). Conversations with no history row (bare API calls) are
unaffected.

Tests: **348 backend** (+8: no-shrink invariant, atomicity, cross-user 404,
owner helper) / **150 frontend** (+3: the empty-cache destruction path,
refused-shrink recovery, and that a legitimate same-length rebuild still
works). Verified live against the running stack: shrink → 409 with the thread
intact, same-length replace → 200, cross-user PUT → 404, cross-user chat →
blocked, composer locked-then-released, regenerate confirmation.

## Phase 0 — context budgeting, no more 400s (2026-07-27)

The reported failure was `This model's maximum context length is 8192 tokens…
you requested 8000 output tokens and your prompt contains at least 193 input
tokens` on a bare "hi". Diagnosis: the **model picker was set to "Fast"**,
which routes to `vllm-router` (Qwen3-4B-Instruct-2507) at `--max-model-len
8192`, while `chat.py` asked for a fixed `max_tokens=8000` — leaving 192
tokens for the prompt. The main model was never the problem; it has been at
131072 all along.

**0.1 — router window.** `vllm-router`: `--max-model-len 8192 → 32768`,
`--gpu-memory-utilization 0.08 → 0.12`, `--kv-cache-dtype fp8` (~2.3 GB KV).
No rope scaling needed — Qwen3-4B-Instruct-2507 is natively 262144.
`--gpu-memory-utilization 0.90` was NOT applied to the main model: this box
has ~27 GB of 121 GB free and its "GPU memory" is system memory, so 0.90 would
have claimed ~109 GB and taken the machine down.

**0.2/0.3 — `app/context.py`, enforced at the client boundary.** Every model
call is now sized against the window of the model that will actually serve it,
read from that server via `POST /tokenize` (which returns both the exact
chat-template token count and `max_model_len`, so the app's view can never
drift from the running config). `max_tokens = min(requested, window −
prompt − CONTEXT_SAFETY_MARGIN)`. Doing this in `llm.py` fixes all ~20
hard-coded call sites at once instead of editing each engine.

When a request still does not fit, in order: drop the oldest turns (pinned
system blocks and the newest message always survive), then — if a *single*
message is bigger than the whole window — clip that message's **middle**,
keeping the head and the tail. Tail-keeping matters: a long paste usually ends
with the actual instruction, and head-only truncation would throw it away.
Both are logged.

**0.4 — small-model inputs clipped.** Router/classification calls
(`ROUTER_INPUT_CHAR_CAP`) and embedding inputs (`EMBED_INPUT_CHAR_CAP`) are
clipped first. The embedding path was a second, independent 400 source at
4096 tokens that — unlike the router's — was caught nowhere.

**0.5 — friendly errors.** `lib/errors.ts` maps raw upstream payloads to a
plain sentence ("This conversation is too long for the selected model. Switch
the model picker to Smart, or start a new chat.") with the original payload
behind a "Technical details" disclosure.

**0.6 — system blocks survive history slicing.** Engines sliced `history[-6:]`,
which silently discarded the cross-chat-recall and shared-page blocks that
`main.py` prepends as system messages. New `recent_turns(history, n)` keeps
every system block and counts only real turns; applied at all 12 slice sites.

Tests: **367 backend** (+19 budget/trim/clip/recent_turns) / **159 frontend**
(+9 error formatting). Verified live against the still-8192 router: "hi" works
on Fast and Smart, a 147k-character prompt on Fast and an 869k-character
prompt on Smart both answer correctly instead of 400ing — and both return
"ACKNOWLEDGED", proving the trailing instruction survived clipping.

### Phase 0 follow-ups (2026-07-27)

**The one sanctioned shrink.** The no-shrink invariant would have broken a
CONFIRMED regenerate of an older answer, which legitimately discards the turns
after it: the sync would have been refused (409) and the recovery would have
resurrected the discarded messages, silently throwing away the new answer.
Rather than an "allow shrink" flag on the sync path (which a future bug could
set), truncation gets its own narrow endpoint:
`POST /history/conversations/{id}/truncate {keep, expected_total}`.
- It accepts **two integers and nothing else** — it cannot write content, so a
  bug there can only shorten a thread, never rewrite one.
- `expected_total` is optimistic concurrency: if another tab appended turns
  since the caller last looked, it 409s instead of deleting them.
- It is reached from exactly one place: the confirmed-regenerate branch. The
  ordinary save/sync path still cannot shrink at all — proved by a test that
  simulates a regenerate which "forgot" to truncate and asserts the server
  thread is untouched.

**Double-persist race (pulled forward from Phase 0.9).** Two clients attached
to one detached generation both finalize and both push, storing the same reply
twice — reproduced live with two browsers. Each generation now carries a
`generation_id`, echoed in the final meta and stored on the message row.
The first fix (select-then-insert in `add_message`) still duplicated under a
real race, so the constraint moved into the database: a **partial unique index**
on `(conversation_id, generation_id)`, with the insert failure turned into a
no-op that returns the winning row. `migrate()` deletes duplicates left by the
earlier race before building the index — which repaired the live database.

Tests: **379 backend** (+7: truncate semantics/concurrency/authz/shape, an
8-thread append race, and migration repair) / **162 frontend** (+3: truncate
path, refusal keeps local intact, and "saveMessages can never shrink").

## Phase 0.9 — the deferred lower-severity items (2026-07-27)

**1. Attachments were dropped on regenerate/retry.** The persisted message
keeps only a preview (`imageDataUrl`) or a filename (`pdfName`), never the
payload, so re-sending silently changed the question — "what's in this
invoice?" re-asked with no invoice. New `lib/attachments.ts` keeps the payload
in MEMORY (deliberately not localStorage: a 25 MB PDF as base64 would blow the
quota, and quota eviction is exactly what caused the conversation-destroying
bug). Images additionally survive a reload because the stored data URL *is*
the payload. A PDF turn that can no longer be reconstructed reports as missing
and the user is asked to re-attach, instead of quietly getting a different
answer.

**2. A background send failure was invisible.** `markUnreachable` dropped the
placeholder and relied on a banner that only renders for the chat on screen —
a send that failed while the user was in another chat left no trace at all.
The failure is now written onto the message and persisted, so it is visible
whenever the chat is opened, with Retry.

**3. `decodeURIComponent` 500.** A malformed attach id ("%") threw URIError
and surfaced as an unhandled 500; it is now a 400.

**4. Migrations run at STARTUP.** `connect()` migrated lazily, so a broken
migration surfaced on the first request that happened to touch app.sqlite3 —
long after the deploy looked healthy. A FastAPI lifespan now opens (and
therefore migrates) the database before the app serves anything, and /health
gained an `app_db` check that reads a migrated column. Tests got a conftest
giving each test its own database directory rather than weakening this.

**5. Trimming is no longer silent.** When the budget module drops turns or
clips an oversized message, the request records it in a ContextVar, /chat adds
`input_trimmed` to the final meta, and the answer carries an inline amber
notice — "Input was shortened to fit the model's limit — part of a long
message was left out." A new `--ts-warn` theme token backs it (Phase C's
meter will reuse it).

Tests: **385 backend** / **171 frontend**. Verified live: 1.3 M-character paste
→ answer plus the notice; regenerate re-sent the identical image payload;
a failed background send stayed visible after leaving the chat AND after a
reload with the question intact; malformed attach ids → 400; /health reports
`app_db: ok` with zero startup errors.

## Phases A + B + C — per-session context management (2026-07-27)

A session's stored history is unbounded; the context window is a per-REQUEST
limit. Every request is now assembled fresh from only that session's data —
system → rolling summary → retrieved snippets → recent turns verbatim →
current message — always reserving that request's own output budget.

### Phase A — TokenBudget, assembly, auto-compact

`app/compaction.py` owns the budget maths. Two points matter:
- the reservation is **this request's** output budget, not a global constant
  (an engine asking for 12000 output tokens genuinely has less prompt room
  than one asking for 2000);
- the reservation never falls below `MIN_OUTPUT_FLOOR` (1024). A thinking
  model squeezed below that burns its budget reasoning and returns NOTHING,
  so input is trimmed further instead — a shorter history beats an empty
  answer.

`app/summarize.py` rewrites the rolling summary INCREMENTALLY (previous
summary + only the newly folded turns), so one compaction costs the same at
turn 20 and turn 2000, and re-condenses the summary if it nears its own cap.

Two compaction paths share one idempotent mechanism — `covers_through`, a
count of leading turns already folded, so only later turns are ever folded:
- **background**, after a turn completes, above `CONTEXT_BG_COMPACT_THRESHOLD`
  (0.70). The normal path; the user never waits. It finishes after the
  stream's `done`, so its notice rides on the NEXT reply's meta.
- **synchronous**, before a request, above `CONTEXT_COMPACT_THRESHOLD` (0.80),
  with a visible "Compacting conversation…" status. The hard guarantee that
  covers a burst of very long turns.
Both take a per-conversation lock, so they cannot double-fold or race.
Compaction failure is non-fatal — the request falls back to turn trimming.

Storage is `conversation_summaries` (its own table, so a summary write never
touches the row the sidebar orders by). Truncating a thread clears its
summary: the summary described turns the user deliberately removed.

The "Conversation compacted" notice is clickable and opens a read-only panel
(`SummaryPanel`) showing exactly what the assistant still remembers.

### Phase B — semantic recall inside the session

Folded turns are also embedded (`app/recall.py`) and the most relevant chunks
come back under a labelled "Relevant earlier messages" block, behind
`SEMANTIC_RECALL_ENABLED`. This is what recovers detail the summarizer chose
to drop.

**Deviation, deliberately:** the vectors live in SQLite (`conversation_chunks`)
rather than the existing LanceDB `chunks` table. That table is the SALESFORCE
corpus — the RAG engine searches it and renders hits as sourced Salesforce
citations, so putting chat text there would let private conversation content
surface as if it came from the CRM, and would break the per-session boundary
the design requires. Reuse is at the model level instead: the same
Qwen3-Embedding-0.6B service on :8003. Brute-force cosine over ONE
conversation's chunks is microseconds, and isolation becomes structural — the
only read path loads a single conversation's rows.

A related fix found by its own test: the chunker's 40-character floor silently
dropped short factual turns ("the codename is ORION-7") — exactly what recall
exists to preserve. Lowered to 15, which still skips bare acknowledgements.

### Phase C — context meter

A ring in the composer next to the send button: gray under 60%, amber 60–84%,
red at 85%+, gentle pulse from 95%. The value is how full the NEXT request
will be (tokens ÷ usable budget), exact from each reply's `meta.context` and
augmented by a debounced character estimate for the unsent draft — the one
place an estimate is acceptable. Click for a breakdown that sums to the shown
total, plus "Compact now". Values are kept per conversation, so switching
chats shows that chat's own reading. The Phase A status chip sits beside it.

Tests: **415 backend** (+30: budget maths, fold idempotency, concurrent
compaction, a 200-turn simulation proving a turn-3 fact survives, session
isolation for both summaries and retrieved chunks) / **183 frontend** (+12
meter thresholds, colour states, breakdown totals, per-session values).

### Corrections found by live testing (2026-07-27)

Three defects the unit tests could not have caught, all fixed and redeployed:

1. **Background compaction was not actually in the background.** It ran inside
   the request's worker task, and the SSE stream stays open until that task
   returns — so the user waited for the very thing designed to be invisible
   (turns took 40–100 s). It is now a detached `asyncio` task, with a strong
   reference held so it cannot be garbage-collected mid-run.
2. **The output reservation could consume an entire small window.** Reserving
   the default 8192 against the 8192-token "fast" model left *nothing* for the
   prompt — usable went negative and clamped to 1 token, so that model
   reported a full context on an empty chat. The reservation is now bounded by
   half the window, with the thinking-model floor still winning under that.
3. **The meter was not per-session across reloads.** It read an in-memory map
   filled only by streams in the current tab, so a chat opened fresh showed
   0%. It now derives from the conversation's own last reply
   (`latestUsage`) — history already persists `meta`, so any chat shows its
   real value immediately, in any tab, after any reload.

Also corrected while testing: the recall chunker's 40-character floor silently
dropped short factual turns (exactly what recall exists to preserve) → 15.

### Closing #4 + race proof + adaptive keep (2026-07-28)

**Recall proven end-to-end, user-visible.** The planted fact was surgically
removed from the stored summary (simulating a summarizer that drops it),
leaving it in neither the summary nor the verbatim window. Asked through the
normal chat flow, the model still answered "ORION-7" — and the assembled
prompt contains it in exactly ONE message: a system message inside the
labelled retrieval block, with the summary block carrying nothing.
The automated equivalent (`test_a_fact_the_summarizer_omits_is_recovered_by_
retrieval`) uses a summarizer that deliberately omits the fact and asserts the
same three things.

**Background/synchronous race.** Now that background compaction is a detached
task it can genuinely overlap the next request's synchronous pass. A new test
fires both at the same instant (with the summarizer yielding mid-call to force
interleaving) and asserts: the summary is written exactly once, every folded
turn appears exactly once, and `covers_through` matches the number of turns
actually folded. Writing it exposed a real defect — when a concurrent fold won
the lock, `compact()` returned None and the adaptive loop kept the STALE
pre-fold measurement, so it shrank the keep-window and folded a second time
(boundaries 32 then 36). `prepare` now re-reads the stored summary and
re-measures before deciding to shrink further.

**Adaptive keep-recent.** When the KEEP_RECENT_TURNS verbatim turns alone
exceed the budget (a handful of huge pastes), compaction now keeps fewer turns
— halving down to a floor of 2 — instead of folding once and falling through
to mid-message clipping, which silently drops part of what the user wrote.

**README.** A "Why chat embeddings live in SQLite, not LanceDB" note under the
trust principles, so the separation is not "cleaned up" later: `chunks` is the
Salesforce corpus whose hits render as sourced CRM citations, and a
per-conversation table makes session isolation unreachable-by-construction
rather than dependent on remembering a WHERE clause at every call site.

Tests: **422 backend** (+5) / **189 frontend**.

---

# v1.0-context-system — milestone (2026-07-28)

Per-session context management, end to end: a session's history is unbounded,
the window is a per-request limit, and every request is assembled fresh from
only that session's data.

**Phase 0-critical — conversation integrity.** A stale-cache sync could DELETE
an entire conversation server-side; the sync path can no longer shrink a
thread at all (409), and the one sanctioned shrink — a user-confirmed
regenerate — goes through a dedicated truncate endpoint that accepts two
integers and cannot write content. Reloading mid-answer no longer kills the
generation. Per-conversation stores (`url_documents`, `repo_chunks`) are
owner-scoped.

**Phase 0 — no more 400s.** The reported `maximum context length is 8192`
came from the picker's "Fast" model, not the main one: a fixed
`max_tokens=8000` against an 8192 window left 192 tokens of prompt room. Every
call is now sized against the window the SERVING model reports via
`/tokenize`, enforced at the client boundary. Overflow drops oldest turns,
then clips the middle of an oversized message (head+tail kept, because a long
paste's instruction is usually at the end). The router runs at 32768 with an
fp8 KV cache.

**Phase 0.9.** Attachments survive regenerate; background send failures are
recorded instead of silent; malformed attach ids 400 instead of 500;
migrations run at startup with an `app_db` health check; trimming is reported
inline rather than done silently.

**Phase A — rolling summary + auto-compact.** Incremental summary (previous
summary + only new turns, so cost is constant), two compaction paths
(background at 0.70, synchronous at 0.80) sharing one idempotent
`covers_through` under a per-conversation lock, adaptive keep-recent when the
verbatim tail alone will not fit, and a read-only panel showing what the
assistant remembers.

**Phase B — semantic recall.** Folded turns are embedded and retrieved back
under a labelled block. Vectors live in SQLite, deliberately NOT the LanceDB
Salesforce corpus — see the README note.

**Phase C — context meter.** A ring in the composer: gray / amber 60% / red
85% / pulse 95%, exact from each reply's meta plus a debounced draft estimate,
per-session, with a breakdown popover and "Compact now".

Verified live on the real windows: Smart 131072 → usable 122,368; Fast 32768 →
usable 24,064. Conversations that previously read >100% on the old 8192 window
now read 4%.

Suites at this tag: **422 backend / 189 frontend**, lint + typecheck clean.

## Phase 4 — ZIP & dataset uploads (2026-07-28)

Upload an archive or a data file; the model answers from a **profile** of it
and never from the file. Sandboxed execution (4b) is deliberately NOT built.

**Upload path.** Datasets get their own streamed multipart endpoint rather
than riding as base64 in the chat body — a 200 MB archive would otherwise sit
in memory twice. `/chat` afterwards carries nothing but the conversation id.
Extraction lands under the Phase 3 workspace (`WORKSPACE_TTL_HOURS`,
`WORKSPACE_QUOTA_GB`) and the upload is owner-scoped on arrival.

**`core/archive.py`** — manual member loop, never `extractall`:
zip-slip blocked by path inspection AND a realpath containment re-check;
symlinks/hardlinks/devices refused (a symlink escapes *after* a clean path
check); four bomb caps (total, per-file, per-member ratio, member count)
enforced from the header **and** re-counted while streaming, because the
central directory is a claim; nested archives listed but never opened;
`.pkl`/`.pickle` refused outright since `read_pickle` executes code.

**`.xlsx` is a ZIP.** A spreadsheet uploaded directly now faces the same
container caps *before* it is stored or read. Live testing caught this: the
caps did stop openpyxl from ever opening a bomb workbook, but the upload
reported success with a quiet "skipped" note — it now fails with a clear
reason, like any other bomb.

**`core/profile.py`** — DuckDB counts and describes without loading the file
(network filesystems and extension autoloading disabled; `enable_external_
access=false` cannot be used, as it also blocks the local file we were asked
to read). Rows, columns, dtypes, null %, distinct counts, numeric/date ranges.

**The PROFILE-only rule.** Exactly two things reach the prompt raw — 5 sample
rows and 5 top values — both truncated. **String min/max was a third until the
canary test caught it:** the alphabetically-first `note` value was the secret
planted at row 500, and truncation cannot help when a secret is short. String
columns now report min/max **length** instead. The dual-canary test asserts
one canary 500 rows deep and one past the truncation point inside a
low-cardinality column both stay out of the assembled prompt.

**Profile text is untrusted.** Column names and cell values come from a user's
file and can be instruction-shaped, so the whole profile is wrapped in
delimiters with a system instruction to treat everything inside as data.

**Expiry fails soft.** Bytes and profile have different lifetimes: after the
TTL sweeps the workspace, the stored profile keeps answering and anything
needing bytes says "This dataset expired — please upload it again."

Tests: **458 backend** (+36: hostile archives incl. bomb-xlsx and a lying
header, profiling, dual canary, delimiting, expiry, isolation) / **189
frontend**. Verified live: a 2-file ZIP profiled (5,000 rows, 14.3% nulls),
every malicious fixture rejected with a clear reason and nothing written
outside the workspace, and "which column has the most missing values?"
answered correctly through the normal chat flow (route `dataset`).

---

## Charts: Apache ECharts in the browser, nine types, and three real bugs

The chart system was extended, not replaced. `ChartSpec` is still the
contract, `meta.chart` is still the transport, there is still exactly one
`meta` frame per turn, and reports still render server-side with matplotlib.
What changed is the browser renderer, the number of chart types, and three
things that were quietly broken.

**Recharts → Apache ECharts.** The migration happens entirely behind the
spec: `lib/chartOption.ts` is a trusted adapter that builds every ECharts
option from a validated `ChartSpec`, and the component never hands `spec` to
ECharts. This matters more than it sounds — an ECharts `formatter` may be a
*function*, so a passthrough of unknown keys would be code execution. There
is no passthrough, and `ChartSpec` has no field that could carry one.
ECharts loads through `next/dynamic` with `ssr: false` and registers only the
five chart types and four components this app draws, so it sits in a 588 KB
on-demand chunk: a conversation with no chart never downloads it.

**One vulnerability found and closed on the way.** ECharts renders a string
returned from a tooltip formatter as HTML, and the labels are Salesforce
values — an Account named `<img src=x onerror=…>` is a legal record. Every
value interpolated into tooltip markup is escaped.

**Nine types.** bar, line, area, pie, scatter (unchanged) plus
horizontal_bar, donut, funnel, histogram. Two of the new ones need something
a model cannot be trusted to supply, so it is not asked:

- **Histogram bins** are computed in Python over the full result and shipped
  as an already-binned (label, count) table, so the browser and the report
  PNG cannot disagree about where the bars are.
- **Funnel order** asserts a sequence. A funnel is drawn only when every
  stage belongs to one trusted list — Salesforce's standard Opportunity,
  Lead and Case picklists, or an operator's own via
  `CHART_FUNNEL_STAGE_ORDER`. One unrecognised stage and it degrades to a
  ranked horizontal bar instead of inventing an order. `sort: 'none'` in the
  adapter keeps ECharts from re-sorting it back.

**THE BUG (agent route).** `merge_step_meta` carried only `sql` forward from
a sql step, so an agent-routed answer never had `meta.data` — and a chart is
drawn over `meta.data`. The identical question answered by the direct SQL
route drew a chart; answered through the agent it drew nothing, silently.
The whole sql payload now travels as a unit from the one step that produced
it, so a chart spec can never end up rendered over a different query's rows.

**THE BUG (blank report images).** `render_chart_png` fell past every drawing
branch for a type it did not handle, then still set the title and saved —
embedding a captioned, completely empty PNG in a Word/PDF report. That looks
deliberate and says nothing. Unsupported types now raise, every `ChartType`
must appear in `PNG_SUPPORTED` or `PNG_TABLE_ONLY` or the module refuses to
import, and a zero-byte file is never embedded.

**THE BUG (a chart could destroy its container).** In reports, a matplotlib
exception propagated out of `_sql_section` and took the section's prose,
table and heading with it. In the browser there was no error boundary
anywhere, so a chart render throw unmounted the entire React tree and left a
white page. Both are now scoped: `ChartErrorBoundary` for the chart,
try/except around chart creation only for the report. A chart is the least
important thing on screen and must not be able to take the most important
thing with it.

**Trigger modes.** `CHART_TRIGGER_MODE=explicit` (default) is bit-for-bit the
old behaviour. `hybrid` adds four deterministic shapes — time series,
single-metric category comparison, trusted stage funnel, small part-to-whole
— decided in Python with no extra model call. There is no `automatic` mode,
and an unrecognised value falls back to `explicit`: the failure mode of
guessing is charts appearing where nobody wanted them.

**One fewer model call, and a safer one.** Unambiguous shapes now build their
spec deterministically. The model is consulted only for an explicit request
whose result has no single obvious reading — and it is shown column
*metadata* (`ColumnProfile.to_prompt_dict`), never a row. Salesforce record
values are data, not instructions, and they no longer reach that prompt at
all.

**Theme.** `--ts-chart-1..5` existed in `globals.css` since the design system
landed but nothing consumed them — Recharts needs literal colors, so the same
five hexes were duplicated in the component and kept in sync by hand. They
are now resolved with `getComputedStyle` and re-resolved on theme change,
with the literals kept only as an SSR/test fallback.

Tests: **800 backend** (+163) / **237 frontend** (+37). Lint, `tsc --noEmit`
and `next build` all pass; `First Load JS` for `/` is 185 kB with ECharts
outside it.

**Known limitation, not worked around:** the backend receives history as
`{role, content}` pairs with no `meta`, so the *previous* chart spec is not
recoverable server-side. Follow-ups work when the message says what to change
("make it a line chart", "make it horizontal", "show the table instead"), and
every follow-up produces a new assistant response rather than mutating a
chart in browser memory — so what you see survives a reload.
