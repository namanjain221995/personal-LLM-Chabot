# Orchestrator — Web Search Subsystem

The provider package `orchestrator/app/search/` plus the engine that drives it,
[`orchestrator/app/engines/search.py`](../../orchestrator/app/engines/search.py).

| module | LOC | role |
|---|---|---|
| [`search/__init__.py`](../../orchestrator/app/search/__init__.py) | 0 | package marker (zero bytes) |
| [`search/base.py`](../../orchestrator/app/search/base.py) | 58 | `SearchResult` / `SearchProvider` ABC / `SearchUnavailableError` / `get_provider()` factory |
| [`search/searxng.py`](../../orchestrator/app/search/searxng.py) | 46 | default provider — self-hosted metasearch, no API key |
| [`search/tavily.py`](../../orchestrator/app/search/tavily.py) | 46 | hosted provider — requires `TAVILY_API_KEY` |
| [`search/brave.py`](../../orchestrator/app/search/brave.py) | 45 | hosted provider — requires `BRAVE_API_KEY` |
| [`engines/search.py`](../../orchestrator/app/engines/search.py) | 504 | rewrite → search → merge → SSRF-safe fetch → extract → cited streaming answer |

### Search is OFF by default, and Salesforce mode requires an explicit "on"

Two gates stand between a user request and any web egress, and both are well reasoned.

**Gate 1 — the feature flag.** `settings.search_enabled` defaults to `False`
([config.py:192](../../orchestrator/app/config.py#L192)), `.env.example:49` ships `SEARCH_ENABLED=false`, and
`docker-compose.yml:251` renders `SEARCH_ENABLED: ${SEARCH_ENABLED:-false}`. The SearXNG service itself is behind a
compose profile (`profiles: ["search"]`, `docker-compose.yml:344`) with **no host port published** — the compose
comment states the reason plainly: "Enabling search means queries leave the machine — SEARCH_ENABLED gates that"
(`docker-compose.yml:333-335`). On a fresh install the machine performs zero web egress.

**Gate 2 — mode.** In `salesforce` mode only an **explicit** `web_search: "on"` reaches the web. The gate is a
6-term conjunction at [main.py:423-434](../../orchestrator/app/main.py#L423):

```python
auto_web_search_allowed = request.mode == "assistant"        # main.py:401
...
if (settings.search_enabled
        and request.web_search != "off"
        and not request.pdf_data
        and not request.image_data
        and request.text
        and (auto_web_search_allowed or request.web_search == "on")):   # main.py:433
```

The rationale is recorded in the source ([main.py:393-400](../../orchestrator/app/main.py#L393)) and is grounded
in an observed regression: search is evaluated *before* the route chain, so an auto-classifier that fancied the
web hijacked the request and the Salesforce router never saw it — "what problems do customers describe in their
support cases?" returned web articles about IT ticketing instead of this org's cases. Making auto-detection
assistant-mode-only is the correct fix for a CRM analytics product, and it is worth crediting: the default
posture is *answer from my data*, and reaching the internet is an explicit act.

Attachments are excluded unconditionally (`not request.pdf_data and not request.image_data`) — a PDF or image
send never triggers a search.

**Decision precedence inside the gate** ([main.py:435-449](../../orchestrator/app/main.py#L435)):

| Order | Condition | Result |
|---|---|---|
| 1 | `not rate_ok(user_key)` | emit `status` "Search rate limit reached — answering from model knowledge.", `want_search` stays `False` |
| 2 | `request.web_search == "on"` | `want_search = True` (no model call) |
| 3 | `auto_plan is not None` | `want_search = auto_plan.search` — the auto-orchestration call at [main.py:412](../../orchestrator/app/main.py#L412) already judged this request; no second model call |
| 4 | else (`"auto"`) | `want_search = await should_search(request.text)` |

`want_search` is then consumed twice at dispatch: the agent engine receives it as its `web=` gate
([main.py:600](../../orchestrator/app/main.py#L600)), and only if no agent is wanted does `run_search_engine`
run standalone ([main.py:602-606](../../orchestrator/app/main.py#L602)).

### Provider matrix

| Provider | Key/URL required | Endpoint | Timeout | Egress leaves the machine | `file:line` |
|---|---|---|---|---|---|
| `searxng` (**default**) | `SEARXNG_URL` — non-empty, else `SearchUnavailableError` | `{base_url}/search` | **hardcoded 10.0 s** | via the SearXNG container, which itself reaches the public internet | [base.py:40-45](../../orchestrator/app/search/base.py#L40), [searxng.py:25-26](../../orchestrator/app/search/searxng.py#L25) |
| `tavily` | `TAVILY_API_KEY` | `https://api.tavily.com/search` (POST, key **in the JSON body**) | **hardcoded 12.0 s** | yes — direct | [base.py:46-51](../../orchestrator/app/search/base.py#L46), [tavily.py:10,27](../../orchestrator/app/search/tavily.py#L10) |
| `brave` | `BRAVE_API_KEY` | `https://api.search.brave.com/res/v1/web/search` (GET, key in `X-Subscription-Token`) | **hardcoded 12.0 s** | yes — direct | [base.py:52-57](../../orchestrator/app/search/base.py#L52), [brave.py:10,26](../../orchestrator/app/search/brave.py#L10) |
| anything else | — | — | — | — | `SearchUnavailableError(f"unknown SEARCH_PROVIDER {provider!r}")` [base.py:58](../../orchestrator/app/search/base.py#L58) |

None of the three provider timeouts uses `settings.fetch_timeout_ms`
([config.py:198](../../orchestrator/app/config.py#L198)) — that setting bounds only the *page fetch* inside
`net.safe_fetch`, not the provider query. All three are literals.

### Configuration

| Setting | Env var | Default | Defined | Read | In `.env.example` / compose? |
|---|---|---|---|---|---|
| `search_enabled` | `SEARCH_ENABLED` | `False` | [config.py:192](../../orchestrator/app/config.py#L192) | [main.py:425](../../orchestrator/app/main.py#L425) only | yes / yes |
| `search_provider` | `SEARCH_PROVIDER` | `"searxng"` | [config.py:193](../../orchestrator/app/config.py#L193) | [base.py:39](../../orchestrator/app/search/base.py#L39) | yes / yes |
| `searxng_url` | `SEARXNG_URL` | `""` (`.rstrip("/")`) | [config.py:194](../../orchestrator/app/config.py#L194) | [base.py:43,45](../../orchestrator/app/search/base.py#L43) | yes / yes |
| `tavily_api_key` | `TAVILY_API_KEY` | `""` | [config.py:195](../../orchestrator/app/config.py#L195) | [base.py:49,51](../../orchestrator/app/search/base.py#L49) | yes / yes |
| `brave_api_key` | `BRAVE_API_KEY` | `""` | [config.py:196](../../orchestrator/app/config.py#L196) | [base.py:55,57](../../orchestrator/app/search/base.py#L55) | yes / yes |
| `search_max_results` | `SEARCH_MAX_RESULTS` | **100**, no upper bound | [config.py:197](../../orchestrator/app/config.py#L197) | [search.py:266](../../orchestrator/app/engines/search.py#L266) | `.env.example:55` = 100 / compose `:-100` |
| `fetch_timeout_ms` | `FETCH_TIMEOUT_MS` | `8000` | [config.py:198](../../orchestrator/app/config.py#L198) | [search.py:315](../../orchestrator/app/engines/search.py#L315) | yes / yes |
| `fetch_max_bytes` | `FETCH_MAX_BYTES` | `5_000_000` | [config.py:199](../../orchestrator/app/config.py#L199) | [search.py:316](../../orchestrator/app/engines/search.py#L316) | yes / yes |
| `search_source_char_budget` | `SEARCH_SOURCE_CHAR_BUDGET` | `8000` | [config.py:202](../../orchestrator/app/config.py#L202) | [search.py:333](../../orchestrator/app/engines/search.py#L333) | **no / no** |
| `search_rate_per_min` | `SEARCH_RATE_PER_MIN` | `10` | [config.py:203](../../orchestrator/app/config.py#L203) | [search.py:147](../../orchestrator/app/engines/search.py#L147) | **no / no** |
| `search_cache_ttl` | `SEARCH_CACHE_TTL` | `900.0` (15 min) | [config.py:204](../../orchestrator/app/config.py#L204) | [search.py:137](../../orchestrator/app/engines/search.py#L137) | **no / no** |

The three undocumented ones are the ones that actually bound cost. They are settable via the environment (they go
through `_int`/`_float` like everything else) but appear in neither `.env.example` nor `docker-compose.yml`, so an
operator has no signal that they exist.

---

## search/\_\_init\_\_.py

**Purpose** — Package marker for `orchestrator/app/search`. The file is **zero bytes**.

**Public surface** — None. `wc -l` → 0; unlike
[`orchestrator/app/__init__.py:1`](../../orchestrator/app/__init__.py#L1) it carries not even a docstring.

**Control flow** — n/a. Importing the package executes nothing.

**State & side effects** — None.

**Dependencies** — Inbound: [engines/search.py:26](../../orchestrator/app/engines/search.py#L26)
(`from ..search.base import SearchResult, SearchUnavailableError, get_provider`), and
`orchestrator/tests/test_search_providers.py`. Outbound: none.

**Config** — None.

**Failure modes** — None. There is no code to fail.

**Concurrency** — n/a.

**Complexity hotspots** — None.

**Findings** — None. An empty package marker exhibits no listed finding.

---

## search/base.py

**Purpose** — Provider abstraction plus factory: `SEARCH_PROVIDER` → a `SearchProvider` instance, or
`SearchUnavailableError` when the required key/URL is missing.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `SearchResult` | `@dataclass{title: str, url: str, snippet: str}` | [base.py:16-20](../../orchestrator/app/search/base.py#L16) |
| `SearchProvider` | `abc.ABC` with `name: str = "base"` and abstract `async search(self, query: str, max_results: int) -> List[SearchResult]` | [base.py:23-28](../../orchestrator/app/search/base.py#L23) |
| `SearchUnavailableError` | `class(RuntimeError)` | [base.py:31](../../orchestrator/app/search/base.py#L31) |
| `get_provider` | `() -> SearchProvider` | [base.py:36-58](../../orchestrator/app/search/base.py#L36) |

**Control flow** — `get_provider`

1. `provider = (settings.search_provider or "searxng").lower()` — [base.py:39](../../orchestrator/app/search/base.py#L39).
   No `.strip()`, so `SEARCH_PROVIDER=" searxng"` fails.
2. `"searxng"` → lazy `from .searxng import SearxngProvider`; raise if `settings.searxng_url` is empty; return
   `SearxngProvider(settings.searxng_url)` — [base.py:40-45](../../orchestrator/app/search/base.py#L40).
3. `"tavily"` → lazy import; raise if `settings.tavily_api_key` is empty; return
   [base.py:46-51](../../orchestrator/app/search/base.py#L46).
4. `"brave"` → lazy import; raise if `settings.brave_api_key` is empty; return
   [base.py:52-57](../../orchestrator/app/search/base.py#L52).
5. Anything else → `SearchUnavailableError(f"unknown SEARCH_PROVIDER {provider!r}")` —
   [base.py:58](../../orchestrator/app/search/base.py#L58).

**State & side effects** — None. It constructs an object and reads `settings`. No network at import or in
`get_provider`. **No caching** — a new provider instance per call.

**Dependencies** — Inbound: [engines/search.py:26](../../orchestrator/app/engines/search.py#L26), called at
[search.py:257](../../orchestrator/app/engines/search.py#L257); `tests/test_search_providers.py:86,88,93,95,99`;
`tests/test_search_off.py:15`; `tests/test_search_engine.py:68,103`; `tests/test_search_breadth.py:36`.
Outbound: `abc`, `dataclasses`, `typing` ([base.py:9-11](../../orchestrator/app/search/base.py#L9));
`app.config.settings` ([base.py:13](../../orchestrator/app/search/base.py#L13)); lazily `.searxng` / `.tavily` /
`.brave` ([base.py:41,47,53](../../orchestrator/app/search/base.py#L41)).

**Config** — `settings.search_provider` (:39), `settings.searxng_url` (:43,45), `settings.tavily_api_key`
(:49,51), `settings.brave_api_key` (:55,57).

**Failure modes** — Raises `SearchUnavailableError` in four places
([base.py:44,50,56,58](../../orchestrator/app/search/base.py#L44)), all caught at
[engines/search.py:267](../../orchestrator/app/engines/search.py#L267) or
[:441,468](../../orchestrator/app/engines/search.py#L441). Nothing is swallowed. No timeout/retry concerns —
there is no I/O here. The error strings name the **variable** (`"TAVILY_API_KEY is not set"`), never a value.
`settings.search_provider` is **not validated at config time** ([config.py:193](../../orchestrator/app/config.py#L193)
has no whitelist, unlike `chart_trigger_mode` at [config.py:230-231](../../orchestrator/app/config.py#L230)), so a
typo like `SEARCH_PROVIDER=searxn` surfaces only at the first search request.

**Concurrency** — `get_provider` is sync and called from `async def _collect_results`
([search.py:257](../../orchestrator/app/engines/search.py#L257)). It does no I/O, so the loop block is negligible
— the first call pays a module import. No shared mutable state.

**Complexity hotspots** — None. `get_provider` is 23 LOC with a 4-way branch.

**Findings** — None of the listed IDs apply to this file. Unassigned observations for the report author:
`SEARCH_PROVIDER` is unvalidated at config time; the name is not `.strip()`ped
([base.py:39](../../orchestrator/app/search/base.py#L39)); and no provider instance is cached, so
`engines/search.py:257` rebuilds one per search batch.

---

## search/searxng.py

**Purpose** — Default provider: queries an operator-run SearXNG JSON API. Needs **no API key** — only
`SEARXNG_URL`.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `SearxngProvider` | `class(SearchProvider)`, `name = "searxng"` | [searxng.py:16-17](../../orchestrator/app/search/searxng.py#L16) |
| `__init__` | `(self, base_url: str)` — stores `base_url.rstrip("/")` | [searxng.py:19-20](../../orchestrator/app/search/searxng.py#L19) |
| `search` | `async (self, query: str, max_results: int) -> List[SearchResult]` | [searxng.py:22-45](../../orchestrator/app/search/searxng.py#L22) |

**Control flow**

1. `params = {"q": query, "format": "json", "safesearch": "1"}` — [searxng.py:23](../../orchestrator/app/search/searxng.py#L23).
   `safesearch` is hardcoded, not configurable.
2. `async with httpx.AsyncClient(timeout=httpx.Timeout(10.0))` — [searxng.py:25](../../orchestrator/app/search/searxng.py#L25),
   a **hardcoded 10 s**.
3. `GET f"{self.base_url}/search"` → `raise_for_status()` → `.json()` —
   [searxng.py:26-28](../../orchestrator/app/search/searxng.py#L26).
4. `except (httpx.HTTPError, ValueError) as exc: raise SearchUnavailableError(f"SearXNG error: {exc}")` —
   [searxng.py:29-30](../../orchestrator/app/search/searxng.py#L29).
5. Iterate `data.get("results", [])[: max_results * 2]`, skip entries without `url`, map to `SearchResult`
   (`snippet` from `item["content"]`), and `break` once `len(out) >= max_results` —
   [searxng.py:32-45](../../orchestrator/app/search/searxng.py#L32). The `* 2` over-fetch absorbs the skipped
   entries.

**State & side effects** — Network egress to `settings.searxng_url` (injected at
[base.py:45](../../orchestrator/app/search/base.py#L45)); in compose that is `http://searxng:8080`
(`docker-compose.yml:253`), an internal service that itself reaches the public internet. No DB, no filesystem, no
GPU, no global state.

**Dependencies** — Inbound: [base.py:41,45](../../orchestrator/app/search/base.py#L41). Outbound: `httpx`
([searxng.py:11](../../orchestrator/app/search/searxng.py#L11)), `.base`
([searxng.py:13](../../orchestrator/app/search/searxng.py#L13)).

**Config** — None read directly; `settings.searxng_url` is injected via the constructor. `max_results` comes from
`settings.search_max_results` at [search.py:266](../../orchestrator/app/engines/search.py#L266).

**Failure modes** — Catches `httpx.HTTPError` (covering timeouts, connect errors and `raise_for_status`) and
`ValueError` (bad JSON) at [searxng.py:29](../../orchestrator/app/search/searxng.py#L29). A JSON body where
`results` is not a list raises `TypeError` on the slice at
[searxng.py:33](../../orchestrator/app/search/searxng.py#L33) — **not** caught. **No retry, no backoff**; one
attempt at a hardcoded 10.0 s. **No SSRF guard** on `SEARXNG_URL`, documented as deliberate at
[searxng.py:3-6](../../orchestrator/app/search/searxng.py#L3) ("SEARXNG_URL is trusted infrastructure … not
routed through the SSRF guard; the RESULT pages are") — correct as long as the variable is operator-set, though it
is read from the environment with no scheme/host validation
([config.py:194](../../orchestrator/app/config.py#L194)). No auth header — the instance is assumed
unauthenticated on the compose network, which matches the compose definition (no published port,
`docker-compose.yml:336-344`).

**Concurrency** — `async`; no blocking calls; a new `AsyncClient` per call, correctly closed by `async with`.
`self.base_url` is written once ([searxng.py:20](../../orchestrator/app/search/searxng.py#L20)).

**Complexity hotspots** — None. `search` is 25 LOC.

**Findings** — `SEC-05` — provider `title`/`snippet` strings are untrusted third-party text that reaches the model
prompt verbatim via [search.py:335,341](../../orchestrator/app/engines/search.py#L335) →
[`_context_block`](../../orchestrator/app/engines/search.py#L374), with no instruction-stripping or provenance
tainting.

---

## search/tavily.py

**Purpose** — Tavily hosted search-for-LLMs provider. Requires `TAVILY_API_KEY`; egress leaves the machine.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `_ENDPOINT` | `"https://api.tavily.com/search"` | [tavily.py:10](../../orchestrator/app/search/tavily.py#L10) |
| `TavilyProvider` | `class(SearchProvider)`, `name = "tavily"` | [tavily.py:13-14](../../orchestrator/app/search/tavily.py#L13) |
| `__init__` | `(self, api_key: str)` | [tavily.py:16-17](../../orchestrator/app/search/tavily.py#L16) |
| `search` | `async (self, query: str, max_results: int) -> List[SearchResult]` | [tavily.py:19-46](../../orchestrator/app/search/tavily.py#L19) |

**Control flow**

1. `payload = {"api_key": …, "query": …, "max_results": …, "search_depth": "basic"}` —
   [tavily.py:20-25](../../orchestrator/app/search/tavily.py#L20). `search_depth` is hardcoded, not a setting.
2. `async with httpx.AsyncClient(timeout=httpx.Timeout(12.0))` — [tavily.py:27](../../orchestrator/app/search/tavily.py#L27),
   a **hardcoded 12 s**.
3. `POST _ENDPOINT` → `raise_for_status()` → `.json()` — [tavily.py:28-30](../../orchestrator/app/search/tavily.py#L28).
4. `except (httpx.HTTPError, ValueError)` → `SearchUnavailableError(f"Tavily error: {exc}")` —
   [tavily.py:31-32](../../orchestrator/app/search/tavily.py#L31).
5. Map `data.get("results", [])[:max_results]` → `SearchResult`, skipping entries without `url` —
   [tavily.py:34-46](../../orchestrator/app/search/tavily.py#L34).

**State & side effects** — Network egress to `https://api.tavily.com`
([tavily.py:10](../../orchestrator/app/search/tavily.py#L10)) with the API key **in the request body**
([tavily.py:21](../../orchestrator/app/search/tavily.py#L21)). No DB, no filesystem, no GPU, no globals, no env
reads — the key arrives via the constructor from [base.py:51](../../orchestrator/app/search/base.py#L51).

**Dependencies** — Inbound: [base.py:47,51](../../orchestrator/app/search/base.py#L47). Outbound: `httpx`
([tavily.py:6](../../orchestrator/app/search/tavily.py#L6)), `.base` ([tavily.py:8](../../orchestrator/app/search/tavily.py#L8)).

**Config** — None read directly; `settings.tavily_api_key` is injected at
[base.py:51](../../orchestrator/app/search/base.py#L51).

**Failure modes** — Same catch set as the other two ([tavily.py:31](../../orchestrator/app/search/tavily.py#L31)).
**No retry, no backoff**; hardcoded 12.0 s timeout, ignoring `settings.fetch_timeout_ms`. Key leakage: the key is
in the JSON body, and httpx's exception text includes the URL but not the body, so `str(exc)` does not disclose it
— safe. Tavily's current API expects `Authorization: Bearer`; the legacy `api_key`-in-body form used here is what
the code sends.

**Concurrency** — `async`; no blocking calls; a new `AsyncClient` per call, closed by `async with`.
`self.api_key` is written once ([tavily.py:17](../../orchestrator/app/search/tavily.py#L17)).

**Complexity hotspots** — None. `search` is 28 LOC.

**Findings** — `SEC-05` (same untrusted-text ingress as `searxng.py`).

---

## search/brave.py

**Purpose** — Brave Search API provider. Requires `BRAVE_API_KEY`; egress leaves the machine.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `_ENDPOINT` | `"https://api.search.brave.com/res/v1/web/search"` | [brave.py:10](../../orchestrator/app/search/brave.py#L10) |
| `BraveProvider` | `class(SearchProvider)`, `name = "brave"` | [brave.py:13-14](../../orchestrator/app/search/brave.py#L13) |
| `__init__` | `(self, api_key: str)` | [brave.py:16-17](../../orchestrator/app/search/brave.py#L16) |
| `search` | `async (self, query: str, max_results: int) -> List[SearchResult]` | [brave.py:19-45](../../orchestrator/app/search/brave.py#L19) |

**Control flow**

1. `headers = {"Accept": "application/json", "X-Subscription-Token": self.api_key}` —
   [brave.py:20-23](../../orchestrator/app/search/brave.py#L20).
2. `params = {"q": query, "count": max_results}` — [brave.py:24](../../orchestrator/app/search/brave.py#L24).
3. `async with httpx.AsyncClient(timeout=httpx.Timeout(12.0))` — [brave.py:26](../../orchestrator/app/search/brave.py#L26),
   a **hardcoded 12 s**.
4. `GET` → `raise_for_status()` → `.json()` — [brave.py:27-29](../../orchestrator/app/search/brave.py#L27).
5. `except (httpx.HTTPError, ValueError)` → `SearchUnavailableError(f"Brave error: {exc}")` —
   [brave.py:30-31](../../orchestrator/app/search/brave.py#L30).
6. Map `(data.get("web", {}) or {}).get("results", [])[:max_results]` → `SearchResult`, skipping entries without
   `url`; `title` falls back to the url, `snippet` to `""` — [brave.py:33-44](../../orchestrator/app/search/brave.py#L33).

**State & side effects** — Network egress to `https://api.search.brave.com`
([brave.py:10](../../orchestrator/app/search/brave.py#L10)), with the API key in the `X-Subscription-Token`
request header ([brave.py:22](../../orchestrator/app/search/brave.py#L22)). No DB, no filesystem, no GPU, no
globals, no env reads.

**Dependencies** — Inbound: [base.py:53,57](../../orchestrator/app/search/base.py#L53). Outbound: `httpx`
([brave.py:6](../../orchestrator/app/search/brave.py#L6)), `.base` ([brave.py:8](../../orchestrator/app/search/brave.py#L8)).

**Config** — None read directly; `settings.brave_api_key` is injected at
[base.py:57](../../orchestrator/app/search/base.py#L57); `max_results` comes from `settings.search_max_results`
via [search.py:266](../../orchestrator/app/engines/search.py#L266).

**Failure modes**

- Catches `httpx.HTTPError` and `ValueError` ([brave.py:30](../../orchestrator/app/search/brave.py#L30)). The
  `(data.get("web", {}) or {})` idiom ([brave.py:34](../../orchestrator/app/search/brave.py#L34)) protects against
  `None`/missing but **not** against `"web": "string"`, which raises an uncaught `AttributeError`.
- **No retry, no backoff.** Hardcoded 12.0 s, ignoring `settings.fetch_timeout_ms`.
- **`count=max_results` is unbounded.** `settings.search_max_results` defaults to **100**
  ([config.py:197](../../orchestrator/app/config.py#L197)) and `.env.example:55` ships `SEARCH_MAX_RESULTS=100`;
  Brave's Web Search API documents `count` as 1-20, so with the shipped defaults every Brave query is rejected
  upstream and the provider is permanently unavailable. Selecting `SEARCH_PROVIDER=brave` without also lowering
  `SEARCH_MAX_RESULTS` is a broken configuration that only surfaces at request time.
- Key leakage: the key is in a header, not the URL, so httpx's exception text (which prints the request URL) does
  not disclose it — safe.

**Concurrency** — `async`; no blocking calls; a new `AsyncClient` per call, closed by `async with`.
`self.api_key` is written once ([brave.py:17](../../orchestrator/app/search/brave.py#L17)).

**Complexity hotspots** — None. `search` is 27 LOC.

**Findings** — `SEC-05` (same untrusted-text ingress). Unassigned observation: the `SEARCH_MAX_RESULTS=100` vs
Brave `count ≤ 20` conflict above makes the shipped Brave configuration non-functional.

---

## engines/search.py

**Purpose** — The web-search engine: rewrite the question into N queries → provider search → round-robin merge
with a per-domain cap → SSRF-safe fetch and readable extraction → numbered-source context → cited streaming
answer, with an in-process cache, a per-user rate limit and a model-knowledge fallback.

**Public surface**

| Symbol | Signature / value | `file:line` |
|---|---|---|
| `Emit` | `Callable[[str, dict], Awaitable[None]]` | [search.py:28](../../orchestrator/app/engines/search.py#L28) |
| `_MAX_QUERIES` | `3` | [search.py:30](../../orchestrator/app/engines/search.py#L30) |
| `_QUERY_BUDGET` | `{"fast":0,"low":2,"medium":3,"high":6}` | [search.py:33](../../orchestrator/app/engines/search.py#L33) |
| `_SOURCE_BUDGET` | `{"fast":0,"low":10,"medium":15,"high":60}` | [search.py:41](../../orchestrator/app/engines/search.py#L41) |
| `_MAX_PER_DOMAIN` | `{"fast":0,"low":3,"medium":3,"high":4}` | [search.py:46](../../orchestrator/app/engines/search.py#L46) |
| `_MIN_SOURCES` | `8` | [search.py:49](../../orchestrator/app/engines/search.py#L49) |
| `_TIER_A_SOURCES` / `_TIER_B_CHARS` | `10` / `2500` | [search.py:55-56](../../orchestrator/app/engines/search.py#L55) |
| `_FETCH_CONCURRENCY` | `16` | [search.py:58](../../orchestrator/app/engines/search.py#L58) |
| `_EXTRACT_POOL` | `ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract")` | [search.py:60](../../orchestrator/app/engines/search.py#L60) |
| `source_budget` | `(effort: str) -> int` | [search.py:63-65](../../orchestrator/app/engines/search.py#L63) |
| `_normalize_url` / `_registrable_domain` | `(url: str) -> str` | [search.py:68-85](../../orchestrator/app/engines/search.py#L68), [:88-97](../../orchestrator/app/engines/search.py#L88) |
| `_JSON_ARRAY_RE` / `_FRESH_RE` | compiled regexes | [search.py:98](../../orchestrator/app/engines/search.py#L98), [:102-107](../../orchestrator/app/engines/search.py#L102) |
| `_Source` | `@dataclass(n, title, url, text)` + `.domain` property | [search.py:110-119](../../orchestrator/app/engines/search.py#L110) |
| `_cache` / `_cache_get` / `_cache_put` | module dict + accessors | [search.py:125](../../orchestrator/app/engines/search.py#L125), [:128-133](../../orchestrator/app/engines/search.py#L128), [:136-137](../../orchestrator/app/engines/search.py#L136) |
| `_rate` / `rate_ok` | module dict + `(user_key: str) -> bool` | [search.py:140](../../orchestrator/app/engines/search.py#L140), [:143-152](../../orchestrator/app/engines/search.py#L143) |
| `query_budget` | `(effort: str) -> int` | [search.py:158-165](../../orchestrator/app/engines/search.py#L158) |
| `rewrite_queries` | `async (message, history, effort="medium") -> List[str]` | [search.py:168-192](../../orchestrator/app/engines/search.py#L168) |
| `should_search` | `async (message: str) -> bool` | [search.py:195-215](../../orchestrator/app/engines/search.py#L195) |
| `_emit_query` | `async (emit, query, results) -> None` | [search.py:218-240](../../orchestrator/app/engines/search.py#L218) |
| `_collect_results` | `async (queries, effort="medium", emit=None) -> List[SearchResult]` | [search.py:243-308](../../orchestrator/app/engines/search.py#L243) |
| `_fetch_source` / `_fetch_sources` | `async (idx, r) -> Optional[_Source]` / `async (results) -> List[_Source]` | [search.py:311-342](../../orchestrator/app/engines/search.py#L311), [:345-357](../../orchestrator/app/engines/search.py#L345) |
| `_apply_char_tiers` / `_context_block` / `_answer_messages` | prompt assembly | [search.py:360-371](../../orchestrator/app/engines/search.py#L360), [:374-378](../../orchestrator/app/engines/search.py#L374), [:381-399](../../orchestrator/app/engines/search.py#L381) |
| `_fallback` | `async (message, history, emit, note) -> str` | [search.py:402-417](../../orchestrator/app/engines/search.py#L402) |
| `research_step` | `async (question, history=(), effort="medium", emit=None) -> Tuple[str, List[dict]]` — the agent-facing variant | [search.py:420-457](../../orchestrator/app/engines/search.py#L420) |
| `run_search_engine` | `async (message, history, emit, effort="medium") -> str` | [search.py:460-504](../../orchestrator/app/engines/search.py#L460) |

### Rate limiting

`rate_ok(user_key)` ([search.py:143-152](../../orchestrator/app/engines/search.py#L143)) is a sliding-window
counter over a module-level `_rate: dict` ([search.py:140](../../orchestrator/app/engines/search.py#L140)): filter
the user's timestamps to the last 60.0 s ([:146](../../orchestrator/app/engines/search.py#L146)), return `False`
if `len(window) >= settings.search_rate_per_min` ([:147](../../orchestrator/app/engines/search.py#L147), default
**10/min**, [config.py:203](../../orchestrator/app/config.py#L203)), else append `now` and return `True`.

It is called once per request from [main.py:438](../../orchestrator/app/main.py#L438) with
`user_key = str(signed_in["id"]) if signed_in is not None else "anon"`
([main.py:437](../../orchestrator/app/main.py#L437)). Because `auth.current_user` always returns the same single
local row ([auth.py:89-92](../../orchestrator/app/auth.py#L89)), **every caller shares one bucket** — the
"per-user" limit is in practice a global 10 searches/minute for the whole deployment. Also note the counter is
incremented at the *gate*, before any provider call, so it bounds requests-that-may-search rather than actual
provider queries; one `high`-effort request then issues up to 6 provider queries and 60 page fetches under that
single token.

### Cache

`_cache` ([search.py:125](../../orchestrator/app/engines/search.py#L125)) maps
`f"q:{provider.name}:{query}"` → `(expiry_monotonic, results)`. `_cache_put`
([:136-137](../../orchestrator/app/engines/search.py#L136)) stamps
`time.monotonic() + settings.search_cache_ttl` (**900.0 s = 15 min**,
[config.py:204](../../orchestrator/app/config.py#L204)). `_cache_get`
([:128-133](../../orchestrator/app/engines/search.py#L128)) returns the value on a live hit and `pop`s the key
otherwise. Keying on the provider name means switching `SEARCH_PROVIDER` at runtime does not serve stale
cross-provider results. There is **no sweeper**: an expired entry is removed only when its own key is read again,
so `_cache` and `_rate` both grow without bound for the process lifetime.

### Per-source char budget and effort tiers

| Effort | queries (`_QUERY_BUDGET`) | sources read (`_SOURCE_BUDGET`) | pages per domain (`_MAX_PER_DOMAIN`) |
|---|---|---|---|
| `fast` | **0** | **0** | 0 |
| `low` | 2 | 10 | 3 |
| `medium` | 3 | 15 | 3 |
| `high` | 6 | 60 | 4 |

Text kept per source: `settings.search_source_char_budget` (**8000**,
[config.py:202](../../orchestrator/app/config.py#L202)) applied at fetch time
([search.py:333](../../orchestrator/app/engines/search.py#L333)), then `_apply_char_tiers`
([search.py:360-371](../../orchestrator/app/engines/search.py#L360)) cuts every source ranked beyond
`_TIER_A_SOURCES = 10` down to `_TIER_B_CHARS = 2500`. The reasoning is documented at
[search.py:52-56](../../orchestrator/app/engines/search.py#L52): a flat 8000-char budget × 60 sources would be
480k chars of prefill for one step; the top-ranked pages keep the full budget so `high` is never *shallower* than
`medium` on the pages that matter, and the long tail is summarised. Worst-case prompt text at `high`:
10 × 8000 + 50 × 2500 = **205,000 chars**.

`_QUERY_BUDGET["fast"] = 0` is a trap: `rewrite_queries` returns `[]`
([:192](../../orchestrator/app/engines/search.py#L192)), `_collect_results` returns `[]`
([:276-277](../../orchestrator/app/engines/search.py#L276)), and the engine reports *"No web results found —
answering from model knowledge"* ([:473-475](../../orchestrator/app/engines/search.py#L473)) — i.e. no search was
ever attempted, but the user is told the web had nothing.

### `should_search` auto-detection

`should_search(message)` ([search.py:195-215](../../orchestrator/app/engines/search.py#L195)) is a two-stage
decision. Stage 1 is the regex `_FRESH_RE` ([:102-107](../../orchestrator/app/engines/search.py#L102)) matching
`latest|current|today|todays|this week|this month|this year|right now|news|recent|20\d\d|price|stock|weather|
release|version|who is|what is the|how much|when did|when is|score|update`; a hit returns `True` with **no model
call** ([:197-198](../../orchestrator/app/engines/search.py#L197)). Stage 2 is a `router_chat_completion` on the
small model asking for a literal yes/no at `max_tokens=5`
([:200-212](../../orchestrator/app/engines/search.py#L200)), returning `"yes" in raw.lower()`. Any exception →
`False` ([:214-215](../../orchestrator/app/engines/search.py#L214)) — fail-closed, which is the right default for
an egress decision.

In practice stage 2 rarely runs: [main.py:445-447](../../orchestrator/app/main.py#L445) prefers `auto_plan.search`
from the auto-orchestration call that already happened, and `should_search` is only reached when `auto_plan is
None` — i.e. when the request carried an attachment or `request.agent` was set, both of which the gate above has
already excluded. `should_search` is therefore reachable in practice only through direct API calls that set
`web_search="auto"` on an `assistant`-mode request where the orchestration branch did not run.

### Research SSE events

`research` is one of the 8 allowlisted SSE events ([sse.py:43-44](../../orchestrator/app/sse.py#L43)). This engine
is its **only** emitter.

| Payload | Emitted from | `file:line` |
|---|---|---|
| `{"phase": "query", "query": str, "results": [{"title", "url", "domain"}]}` | `_emit_query`, once per query **as it returns** (not batched at the end) so the panel fills in while the work happens | [search.py:226-240](../../orchestrator/app/engines/search.py#L226), called at [:263,275](../../orchestrator/app/engines/search.py#L263) |
| `{"phase": "reading", "count": int}` | before the fetch fan-out | [search.py:478](../../orchestrator/app/engines/search.py#L478) (engine), [:446](../../orchestrator/app/engines/search.py#L446) (`research_step`) |
| `{"phase": "read", "count": int}` | after extraction, with the post-drop count | [search.py:485](../../orchestrator/app/engines/search.py#L485), [:454](../../orchestrator/app/engines/search.py#L454) |

Alongside `research`, the engine emits `status` text at [:464](../../orchestrator/app/engines/search.py#L464)
("Searching the web…"), [:477](../../orchestrator/app/engines/search.py#L477) ("Reading N sources…") and
[:403](../../orchestrator/app/engines/search.py#L403) (the fallback note); `token`/`reasoning` deltas at
[:490](../../orchestrator/app/engines/search.py#L490); and exactly one terminal `meta` —
`{"route":"search","sources":[{n,title,url,domain}]}` ([:494-503](../../orchestrator/app/engines/search.py#L494))
or `{"route":"search","search_unavailable":true}` ([:416](../../orchestrator/app/engines/search.py#L416)).
`research_step` reports its searches through the same `emit` when the agent passes one, so a multi-step plan's
research shows up in the panel as one combined effort.

**Control flow** — `run_search_engine` ([search.py:460-504](../../orchestrator/app/engines/search.py#L460))

1. `emit("status", {"text": "Searching the web…"})` — [:464](../../orchestrator/app/engines/search.py#L464).
2. `rewrite_queries(message, history, effort)` — [:466](../../orchestrator/app/engines/search.py#L466): cap =
   `query_budget(effort)` (:177); system prompt "Turn the user's request into 1 to {cap} concise web-search
   queries … Respond with ONLY a JSON array of strings" (:178-182); `llm.router_chat_completion(msgs,
   temperature=0.0, max_tokens=200)` on the **small** model (:186); `_JSON_ARRAY_RE` + `json.loads` (:187-189);
   `except Exception: queries = []` (:190-191); return `(queries or [message])[:cap]` (:192).
3. `_collect_results(queries, effort, emit)` — [:467](../../orchestrator/app/engines/search.py#L467):
   - `get_provider()` (:257) picks SearXNG / Tavily / Brave and raises `SearchUnavailableError` when the required
     key/URL is missing.
   - Per query: `_cache_get(f"q:{provider.name}:{q}")` (:260) else
     `provider.search(q, settings.search_max_results)` (:266). `SearchUnavailableError` is swallowed **unless**
     this is the last query and nothing has succeeded (:267-272, identity test `q is queries[-1]`), then
     `_cache_put` (:273) and `_emit_query` (:275).
   - Nothing at all → `[]` (:276-277).
   - Round-robin merge (:279-308): rank 0 of every query, then rank 1, … (:285-289); dedup on `_normalize_url`
     (:290-293); per-domain cap via `_registrable_domain` (:294-298); early return once `len(out) >= target`
     (:300-301); overflow rescue only when `len(out) < _MIN_SOURCES` (:306-307); final `out[:target]` (:308).
4. `SearchUnavailableError` → `_fallback(..., "Web search unavailable — answering from model knowledge.")` —
   [:468-471](../../orchestrator/app/engines/search.py#L468).
5. Empty results → `_fallback(..., "No web results found …")` — [:472-475](../../orchestrator/app/engines/search.py#L472).
6. `status` "Reading N sources…" + `research {phase:"reading"}` — [:477-478](../../orchestrator/app/engines/search.py#L477).
7. `_fetch_sources(results)` — [:479](../../orchestrator/app/engines/search.py#L479) →
   [:345-357](../../orchestrator/app/engines/search.py#L345): `asyncio.Semaphore(16)` (:346),
   `asyncio.gather` over all results (:352), drop `None` (:353), renumber contiguously (:355-356). Each
   `_fetch_source` ([:311-342](../../orchestrator/app/engines/search.py#L311)) calls
   `net.safe_fetch(url, timeout_ms=settings.fetch_timeout_ms, max_bytes=settings.fetch_max_bytes,
   accept="text/html,application/pdf,text/plain")` (:313-318) — SSRF-checked, redirect-revalidated, size-capped —
   then `loop.run_in_executor(_EXTRACT_POOL, extract.extract_readable, …)` (:325-332), truncates to
   `settings.search_source_char_budget` (:333), and falls back to the provider snippet when the extraction is
   empty (:334-335).
8. `_apply_char_tiers` (:479) cuts every source ranked > 10 to 2500 chars.
9. No readable sources → `_fallback(..., "Couldn't read the sources …")` — [:480-483](../../orchestrator/app/engines/search.py#L480).
10. `research {phase:"read"}` (:485), then
    `llm.stream_chat_events(_answer_messages(...), max_tokens=12000)` (:487-492), emitting each `(kind, delta)` and
    accumulating only `token` deltas.
11. One terminal `meta` with the sources panel (:494-503); return `"".join(parts)` (:504).

`research_step` ([:420-457](../../orchestrator/app/engines/search.py#L420)) is the agent-facing variant: the same
steps 2-9, but a non-streaming `llm.chat_completion(..., max_tokens=5000)` (:450-452), and it returns `("", [])`
instead of falling back (:441-444, :448-449) so the agent can degrade to model knowledge without failing the step.

**State & side effects**

- Network egress, in order: the configured provider (SearXNG `{SEARXNG_URL}/search`, Tavily
  `https://api.tavily.com/search`, or Brave `https://api.search.brave.com/res/v1/web/search`); then **arbitrary
  public URLs** via `net.safe_fetch` ([search.py:313](../../orchestrator/app/engines/search.py#L313)); then the
  vLLM endpoints — `settings.router_base_url` for the rewrite and `should_search`
  ([:186,200](../../orchestrator/app/engines/search.py#L186)) and `settings.openai_base_url` for the answer
  ([:412,450,488](../../orchestrator/app/engines/search.py#L412)).
- GPU/model calls: 1 router call for query rewrite; 1 router call for `should_search` when the heuristic misses;
  1 answer call (streaming in the engine, non-streaming in `research_step`).
- Filesystem: none. DB: none.
- Global mutation: `_cache` ([:125](../../orchestrator/app/engines/search.py#L125), written
  [:137](../../orchestrator/app/engines/search.py#L137), read/evicted
  [:130-132](../../orchestrator/app/engines/search.py#L130)); `_rate`
  ([:140](../../orchestrator/app/engines/search.py#L140), written
  [:148,150](../../orchestrator/app/engines/search.py#L148)); `_EXTRACT_POOL`
  ([:60](../../orchestrator/app/engines/search.py#L60)) created at import and **never shut down**.
  `_apply_char_tiers` mutates `_Source.text` in place ([:370](../../orchestrator/app/engines/search.py#L370)) and
  `_fetch_sources` mutates `_Source.n` ([:356](../../orchestrator/app/engines/search.py#L356)).
- Env reads: only via `settings`.

**Dependencies** — Inbound: [main.py:435,438,449](../../orchestrator/app/main.py#L435) (`rate_ok`,
`should_search`), [main.py:604,606](../../orchestrator/app/main.py#L604) (`run_search_engine`);
[engines/agent.py:338,341](../../orchestrator/app/engines/agent.py#L338) (`research_step`); tests
`test_search_engine.py:7`, `test_search_breadth.py:13,80-210`, `test_effort_depth.py:24-118`,
`test_search_off.py:6`, `test_salesforce_toggle.py:58`.
Outbound: `.` (engines) → `DIAGRAM_INSTRUCTION`, `recent_turns`
([search.py:22](../../orchestrator/app/engines/search.py#L22)); `..llm` (:23); `..config.settings` (:24);
`..core.extract`, `..core.net` (:25); `..search.base` (:26).

**Config** — `settings.search_cache_ttl` (:137), `settings.search_rate_per_min` (:147),
`settings.search_max_results` (:266), `settings.fetch_timeout_ms` (:315), `settings.fetch_max_bytes` (:316),
`settings.search_source_char_budget` (:333). **`settings.search_enabled` is NOT read in this module** — the gate
lives entirely at [main.py:425](../../orchestrator/app/main.py#L425). `run_search_engine` and `research_step` will
happily run if called directly with search disabled; the flag is a routing decision, not an engine-level kill
switch.

**Failure modes**

| Site | Behaviour |
|---|---|
| [:190-191](../../orchestrator/app/engines/search.py#L190) | `rewrite_queries` — any failure silently falls back to the raw message |
| [:214-215](../../orchestrator/app/engines/search.py#L214) | `should_search` — any failure returns `False` (fail-closed, correct) |
| [:267-272](../../orchestrator/app/engines/search.py#L267) | a provider error on a non-final query is dropped silently |
| [:337-342](../../orchestrator/app/engines/search.py#L337) | `_fetch_source` — **every** exception, including `net.UnsafeURLError` SSRF blocks, timeouts and unsupported content types, degrades to the provider snippet with **no log**. `extract.extract_readable` also swallows all trafilatura errors (`core/extract.py:89-90`) |
| [:270](../../orchestrator/app/engines/search.py#L270) | the only raise: `SearchUnavailableError` when the last query fails and nothing has succeeded — caught at [:441](../../orchestrator/app/engines/search.py#L441) and [:468](../../orchestrator/app/engines/search.py#L468) |

There is **no deadline over the pipeline as a whole**. Worst case at `high`: 6 provider calls (10-12 s each) +
60 page fetches at 16-way concurrency with `settings.fetch_timeout_ms` each + serialized extraction on **one**
worker thread ([:60](../../orchestrator/app/engines/search.py#L60)) + a 12,000-token generation under a 300 s
client timeout with the SDK's default 2 retries. **No retry** anywhere in the module. `_cache` and `_rate` are
unbounded. `asyncio.gather` at [:352](../../orchestrator/app/engines/search.py#L352) has no
`return_exceptions=True` and is safe only because `_fetch_source` swallows everything.

**Concurrency**

- Async throughout except `_normalize_url`, `_registrable_domain`, `_cache_get`/`_cache_put`, `rate_ok`,
  `source_budget`, `query_budget`, `_apply_char_tiers`, `_context_block`, `_answer_messages` — all pure CPU, all
  trivial.
- **Deliberate, correct off-loop work**: CPU-bound extraction is pushed to a dedicated *single-worker* pool
  ([:58-60](../../orchestrator/app/engines/search.py#L58)) because trafilatura shares module-level compiled lxml
  XPath objects that are not thread-safe — the reasoning is documented inline at
  [:319-324](../../orchestrator/app/engines/search.py#L319). This keeps the event loop free **and** keeps
  extraction serial. It is the most careful piece of concurrency reasoning in the orchestrator.
- Shared mutable module state: `_cache`, `_rate`, `_EXTRACT_POOL`, all mutated without locks. Safe within a single
  event loop because every mutation site is synchronous — there is no `await` between the read and the write in
  `rate_ok` ([:145-152](../../orchestrator/app/engines/search.py#L145)).
- `_fetch_source` is fully async; the only blocking work (`getaddrinfo`) is already off-loaded inside
  `net.safe_fetch`.
- Race window: `_cache_get`/`_cache_put` are not atomic across queries, so N concurrent identical searches all
  miss and all hit the provider — a thundering herd against the provider quota.

**Complexity hotspots**

| Function | LOC | Note | `file:line` |
|---|---|---|---|
| `_collect_results` | **68** | nested loops with 6 `continue`/`return` exits and 4 accumulators (`seen`, `domains`, `out`, `overflow`); cyclomatic ≈ 13 | [search.py:243](../../orchestrator/app/engines/search.py#L243) |
| `run_search_engine` | 45 | 4 terminal fallbacks | [search.py:460](../../orchestrator/app/engines/search.py#L460) |
| `research_step` | 40 | near-duplicate of `run_search_engine` steps 2-9 | [search.py:420](../../orchestrator/app/engines/search.py#L420) |

**Findings** — `SEC-05`, `SEC-03`, `REL-03`, `PERF-02`, `COST-01`, `OBS-01`, `QUAL-01`.

- `SEC-05` — the web-source context block ([:374-378](../../orchestrator/app/engines/search.py#L374)) and answer
  system prompt ([:384-396](../../orchestrator/app/engines/search.py#L384)) contain **no data/instruction
  boundary**; fetched page text and provider snippets are concatenated straight into the user message
  ([:397](../../orchestrator/app/engines/search.py#L397)) with no instruction-stripping and no provenance
  tainting. Contrast `engines/dataset.py:27-52`, which does fence its untrusted block.
- `SEC-03` — every page fetch goes through `net.safe_fetch`
  ([:313](../../orchestrator/app/engines/search.py#L313)), so this engine is the primary consumer of the
  resolve-then-connect TOCTOU window.
- `PERF-02` — `net.safe_fetch` buffers the full response body before applying `max_bytes`; at `high` effort this
  engine issues up to 60 such fetches at 16-way concurrency with `fetch_max_bytes = 5 MB`.
- `COST-01` — combined with `SEC-01`, the `rate_ok` bucket key resolves to one shared identity
  ([main.py:437](../../orchestrator/app/main.py#L437)), so the "per-user" limit is a single global 10/min for the
  whole deployment, and each admitted request can consume 3 model calls plus up to 60 page fetches.
- `QUAL-01` — `Emit` is re-declared here at [search.py:28](../../orchestrator/app/engines/search.py#L28), a
  **fourth** copy alongside `chat.py:22`, `agent.py:34` and `graph.py:13`.

Unassigned observations for the report author:

- **Inconsistent www-stripping.** `_normalize_url` uses the correct `host.startswith("www."): host = host[4:]`
  ([:76-77](../../orchestrator/app/engines/search.py#L76)) while `_registrable_domain` uses `.lstrip("www.")`
  ([:90](../../orchestrator/app/engines/search.py#L90)), which strips a *character set*. Consequences measured in
  the evidence pass: `https://www.wired.com/story/x` → `ired.com`, `https://www.w3.org/TR/` → `3.org`,
  `https://web.mit.edu/x` → `eb.mit.edu`, `https://www.washingtonpost.com/a` → `ashingtonpost.com`. This corrupts
  the per-domain diversity cap and the `domain` field shown in the sources panel.
- **`fast` effort reports a false negative** — see `_QUERY_BUDGET["fast"] = 0` above.
- **`research_step` duplicates `run_search_engine`** ([:438-447](../../orchestrator/app/engines/search.py#L438) vs
  [:466-479](../../orchestrator/app/engines/search.py#L466)) — a change to one silently diverges from the other.
- **`SearchResult.snippet` is used as untrusted fallback text**
  ([:335,341](../../orchestrator/app/engines/search.py#L335)) with the same trust treatment as fetched page
  bodies.
- **`_EXTRACT_POOL` is never shut down** ([:60](../../orchestrator/app/engines/search.py#L60)) — a
  process-lifetime thread with no shutdown hook, matching `lifespan`'s missing shutdown branch
  ([main.py:27-38](../../orchestrator/app/main.py#L27)).
