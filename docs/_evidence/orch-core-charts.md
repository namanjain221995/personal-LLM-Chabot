# Evidence — orch-core-charts

Assignment: `orchestrator/app/core/{chart_spec,chart_data,chart_decision,chart_pipeline,chart_profile,charts_png,exports,pdf}.py`
All eight files read in full, top to bottom. Total 1856 LOC.

Verification method: `Read` for full-file comprehension, `rg -n` for caller cross-reference, and
direct execution of the pure modules under `orchestrator/.venv/bin/python` from a scratchpad
(read-only; no repo file outside `docs/_evidence/` was written, no app process started, no git
mutation). Executed checks are labelled **VERIFIED BY EXECUTION** and reproduce below.

---

## Cross-cutting map (verified with `rg`)

```
frontend  ──(SSE meta.chart / meta.chart_data)──┐
                                                 │
engines/sql.py:214 attach_chart ─────────────────┤
engines/agent.py:255,269 (imports attach_chart) ─┼─→ core/chart_pipeline.py:94 build_chart
engines/report.py:167 _section_chart ────────────┘        │
                                                          ├─→ chart_profile.profile_columns
                                                          ├─→ chart_decision.decide / build_spec
                                                          ├─→ chart_data.build_histogram
                                                          ├─→ chart_spec.parse_chart_spec
                                                          └─→ (ask_model → llm.chat_completion)

engines/report.py:190 render_chart_png ──────────→ core/charts_png.py:69   (matplotlib Agg)
engines/sql.py:407-409 export_csv/export_xlsx ───→ core/exports.py:53,96   (→ settings.reports_dir)
engines/document.py:34 run_pdf_engine ───────────→ core/pdf.py:27 render_pdf
core/extract.py:58-61 _extract_pdf_text ─────────→ core/pdf.py:27 render_pdf
main.py:257-271 GET /reports, GET /reports/{filename}  (AUTH-FREE, main.py:55-56)
```

Nothing in this assignment is ever offloaded to a thread or process:
`rg -n 'to_thread|run_in_executor|ThreadPool|threading' orchestrator/app/engines/report.py
orchestrator/app/engines/sql.py orchestrator/app/main.py` returns **only**
`report.py:114 asyncio.create_subprocess_exec` (pandoc). Every call into `charts_png`, `exports`
and `pdf` therefore runs inline on the single asyncio event loop.

---

### orchestrator/app/core/chart_spec.py  (220 LOC)

**Purpose** — Pydantic model for a renderer-independent chart description, plus the parser that
turns raw LLM text into a validated spec or `None`. Owns the SSE wire shape (§10).

**Public surface**
- `ChartType` — `Literal[...]` of 9 members, `chart_spec.py:40-52` (`bar, line, scatter, pie, area, horizontal_bar, donut, funnel, histogram`).
- `CHART_TYPES: tuple` — same 9 as data, `chart_spec.py:56-66`. Consumed by `charts_png.py:227`.
- `PART_TO_WHOLE_TYPES = frozenset({"pie","donut"})` — `chart_spec.py:70`.
- `MIN_BINS = 2`, `MAX_BINS = 50` — `chart_spec.py:74-75`. Imported by `chart_data.py:17`.
- `class ChartSpec(BaseModel)` — `chart_spec.py:83`. `model_config = ConfigDict(extra="forbid", populate_by_name=True)` at `:95`.
  - fields: `type: ChartType` `:97`; `x_key: str` with `AliasChoices("x_key","x")` `:98`; `y_keys: List[str]` with `AliasChoices("y_keys","y")` `:99`; `title: str = ""` `:100`; `stacked: bool = False` `:101`; `bins: Optional[int] = Field(default=None, ge=MIN_BINS, le=MAX_BINS)` `:107`; `show_legend: bool = True` `:108`; `show_values: bool = False` `:109`.
  - validators: `_x_non_empty` `:111-117`; `_y_coerce` (mode="before", str→[str]) `:119-123`; `_y_non_empty` `:125-131`; `_title_str` (mode="before", `None`→`""`, else `str(v)`) `:133-136`; `_normalize_options` (model_validator mode="after") `:138-150`.
  - `wire_dump(self) -> Dict[str, Any]` — `chart_spec.py:154-175`.
- `parse_chart_spec(raw: object, columns: Optional[Sequence[str]] = None) -> Optional[ChartSpec]` — `chart_spec.py:189-220`.
- module-private: `_extract_json(text) -> Optional[str]` `:178-186`; `_FENCE_RE` `:79`; `_THINK_RE` `:80`; `_LEGACY_WIRE_KEYS` `:77`.

**Control flow** (`parse_chart_spec`)
1. `:197` `payload = raw`.
2. `:198-199` if `raw` is already a `ChartSpec`, skip parsing entirely (`spec = raw`).
3. `:201-208` if `str`/`bytes`: `_extract_json` strips `<think>…</think>` (`:179`), unwraps a ```` ```json ```` fence (`:180-182`), then takes the outermost `{ … }` by `find("{")`/`rfind("}")` (`:183-186`). `json.loads` failure → `None`.
4. `:209-210` non-dict payload → `None`.
5. `:211-214` `ChartSpec.model_validate(payload)`; `ValidationError` → `None`.
6. `:216-219` **column binding**: if `columns` given, `x_key` and every `y_keys` entry must be in `set(columns)`, else `None`. This is the trust boundary for model output.
7. `:220` return spec.

`_normalize_options` (`:138-150`) mutates via `object.__setattr__`: drops `bins` on non-histograms (`:143-144`), truncates `y_keys` to one element for pie/donut (`:148-149`).

**State & side effects** — none. No I/O, no DB, no network, no env reads, no global mutation. Pure
stdlib + pydantic (`:26-38`).

**Dependencies**
- inbound: `chart_data.py:17` (MIN_BINS/MAX_BINS), `chart_decision.py:31` (ChartSpec), `chart_pipeline.py:27` (ChartSpec, parse_chart_spec), `charts_png.py:23` (CHART_TYPES, ChartSpec), `tests/test_chart_spec.py:5`, `tests/test_chart_routes.py:312`, `tests/test_charts_png.py:11`, `tests/test_imports.py:8`.
- outbound: `json`, `re`, `typing`, `pydantic`.

**Config** — none.

**Failure modes**
- `_x_non_empty` `:116` and `_y_non_empty` `:130` raise `ValueError` inside pydantic → surfaces as `ValidationError`.
- `parse_chart_spec` swallows `json.JSONDecodeError`/`ValueError` (`:207`) and `ValidationError` (`:213`) and returns `None` — **silently**, with no log line. A model that keeps emitting malformed specs is invisible.
- Direct construction (`ChartSpec(...)` in `chart_pipeline.py:147` and `chart_decision.py:615`) does **not** go through `parse_chart_spec` and therefore raises `ValidationError` to the caller. See Finding 5.
- `_extract_json` `rfind("}")` will happily span two adjacent JSON objects in one reply; the result then fails `json.loads` → `None`. No bound on input length; `_FENCE_RE`/`_THINK_RE` are non-greedy and linear, no ReDoS.

**Concurrency** — fully synchronous, no shared mutable module state (all module constants are
immutable tuples/frozensets/compiled regexes).

**Complexity hotspots** — none. Largest function is `parse_chart_spec` at 32 LOC (`:189-220`).

**Notable**
- `_LEGACY_WIRE_KEYS` (`:77`) is **dead** — `rg -n '_LEGACY_WIRE_KEYS' orchestrator/` matches only its definition.
- `wire_dump` (`:162-175`) deliberately omits optional keys at their default so the five original types serialise byte-identically to the pre-ECharts payload — backward-compat contract with persisted conversations.
- `bins` bound `[2,50]` (`:107`) conflicts with `chart_data`'s legitimate `bin_count == 1` return. See Finding 5.
- No TODO/FIXME/HACK.

---

### orchestrator/app/core/chart_data.py  (113 LOC)

**Purpose** — Deterministic, trusted histogram binning over already-returned rows, so the browser
(ECharts) and the report (matplotlib) draw identical bars. The model never chooses bin edges.

**Public surface**
- `BIN_COLUMN = "bin"`, `COUNT_COLUMN = "count"` — `chart_data.py:21-22`.
- `_DEFAULT_MIN_BINS = 5`, `_DEFAULT_MAX_BINS = 20` — `chart_data.py:27-28`.
- `default_bin_count(n: int) -> int` — `chart_data.py:31-36`. `ceil(sqrt(n))` clamped to `[5,20]`; returns `1` when `n <= 1`.
- `clamp_bins(bins: Optional[int], n: int) -> int` — `chart_data.py:39-47`. Clamps to `[MIN_BINS, MAX_BINS]` = `[2,50]`.
- `_fmt_edge(v: float, integral: bool) -> str` — `chart_data.py:50-55`.
- `build_histogram(columns, rows, value_column, bins=None) -> Optional[Tuple[List[str], List[List[object]], int]]` — `chart_data.py:58-113`.

**Control flow** (`build_histogram`)
1. `:72-75` locate `value_column` in `columns`; missing → `None`.
2. `:77-86` collect finite numeric values via `chart_profile._as_number` + `math.isfinite`; per-row `IndexError/KeyError/TypeError` swallowed with `continue` (`:81-82`).
3. `:86-87` no numeric values → `None`.
4. `:89-90` `lo, hi = min, max`; `integral = all(float(v).is_integer() ...)`.
5. `:92-94` degenerate `lo == hi` → one row, **bin_count = 1**.
6. `:96-98` `k = clamp_bins(bins, len(values))`; `width = (hi - lo) / k`; `counts = [0]*k`.
7. `:99-104` assign each value to `slot = int((v - lo) / width)`, clamped to `k-1` (right-closed last bin).
8. `:106-113` emit `[f"{lo_edge} - {hi_edge}", count]` rows; last bin's upper edge is `hi` exactly (`:109`).

**State & side effects** — none. Pure. No I/O, no env, no globals.

**Dependencies**
- inbound: `chart_pipeline.py:24,143`; `tests/test_chart_data.py:8-15`.
- outbound: `math`; `chart_profile._as_number` (`:16`) — a **private** import across modules; `chart_spec.MIN_BINS/MAX_BINS` (`:17`).

**Config** — none.

**Failure modes**
- `width = (hi - lo) / k` at `:97` divides by zero if `(hi - lo)` underflows to `0.0` while `lo != hi` (only reachable with subnormal float inputs). `ZeroDivisionError` propagates to `chart_pipeline.build_chart`'s blanket `except` (`chart_pipeline.py:115`).
- `hi - lo` can overflow to `inf` for values near ±1.5e308: `width = inf`, every value lands in bin 0, the last label reads `"… - inf"`. No crash, silently wrong chart.
- Row-shape errors swallowed at `:81-82` with no counter/log.
- The `bins` parameter is **never supplied by any caller** — `chart_pipeline.py:143` calls `build_histogram(columns, rows, decision.histogram_source)` with no `bins`. `clamp_bins`'s non-default branch is unreachable in production.

**Concurrency** — synchronous, pure, no shared state.

**Complexity hotspots** — `build_histogram` `chart_data.py:58` = 56 LOC, straight-line, cyclomatic ≈ 9.

**Notable**
- `_fmt_edge` with `integral=True` (`:51-52`) rounds edges to `int`, so when `k` exceeds the integer range the bins collapse into duplicate labels. **VERIFIED BY EXECUTION**:
  `build_histogram(['amount'], [[i%2+1] for i in range(100)], 'amount')` →
  `[['1 - 1', 50], ['1 - 1', 0], ['1 - 1', 0], ['1 - 1', 0], ['1 - 2', 0], ['2 - 2', 0], ['2 - 2', 0], ['2 - 2', 0], ['2 - 2', 0], ['2 - 2', 50]]` — four bins named `"1 - 1"` and five named `"2 - 2"`.
  Also `build_histogram(['amount'], [[1],[1],[2],[2],[3]], 'amount')` → `[['1 - 1',2],['1 - 2',0],['2 - 2',2],['2 - 3',0],['3 - 3',1]]`.
- The `1` returned at `:94` is incompatible with `ChartSpec.bins` (`ge=2`). See Finding 5.
- Docstring at `:26-28` claims "≤500 preview rows"; consistent with `exports.PREVIEW_ROW_CAP`.
- No TODO/FIXME/HACK.

---

### orchestrator/app/core/chart_decision.py  (623 LOC)

**Purpose** — The trusted, deterministic engine that decides *whether* and *how* to chart a result
set from user wording plus column metadata. The only module that may say "ask the model".

**Public surface**
- Regexes: `LEGACY_CHART_RE` `:40-42`; `_NATURAL_RE` `:45-52`; `_NAMED_CHART_RE` `:53-57`; `_BARE_NAMED_RE` `:58`; `_FALSE_POSITIVE_RE` `:62-70`; `_MODIFIER_RE` `:80-89`; `_SUPPRESS_RE` `:94-104`; `_TYPE_PHRASES` (15 entries) `:132-151`; `_STACK_RE` `:163`.
- `_strip_false_positives(message) -> str` `:73-74`.
- `chart_suppressed(message) -> bool` `:107-109`.
- `explicit_chart_request(message) -> bool` `:112-127`.
- `requested_chart_type(message) -> Optional[str]` `:154-160`.
- `requested_stacked(message) -> bool` `:166-167`.
- `STANDARD_STAGE_ORDERS: Dict[str, Tuple[str, ...]]` `:184-207` — keys `opportunity` (10 stages), `lead` (4), `case` (4).
- `_load_custom_orders() -> Dict[str, Tuple[str,...]]` `:210-227`.
- `stage_orders() -> Dict[str, Tuple[str,...]]` `:230-234`.
- `trusted_stage_order(labels) -> Optional[List[str]]` `:237-252`.
- `can_funnel(profile, labels) -> bool` `:255-256`.
- Constants: `VERTICAL_BAR_MAX_CATEGORIES = 8` `:265`; `LONG_LABEL_CHARS = 16` `:267`; `MAX_CATEGORIES = 40` `:269`; `MAX_PART_TO_WHOLE_CATEGORIES = 6` `:271`.
- `@dataclass ChartDecision` `:274-298` — fields `should_chart, chart_type, reason, confidence, x_key, y_keys, stacked, use_model, histogram_source`; method `as_dict()` `:292-298`.
- `_NO = ChartDecision(should_chart=False, reason="no_chart")` `:301` — module-level singleton.
- `_pick_numeric` `:304`, `_pick_dimension` `:308`, `_pick_dates` `:317`, `_bar_flavour` `:321`, `_labels_of` `:327`.
- `decide(message, columns, rows, mode="explicit", profiles=None, explicit_override=None) -> ChartDecision` `:341-387`.
- `_decide_explicit(...)` `:390-479`.
- `_unambiguous_shape(...)` `:481-523`.
- `_decide_hybrid(...)` `:526-596`.
- `build_spec(decision, columns, title="") -> Optional[ChartSpec]` `:599-623`.

**Control flow** (`decide`)
1. `:361-362` materialise `columns`/`rows`.
2. `:363-367` `explicit = explicit_chart_request(message)` unless `explicit_override` is not None.
3. `:369-370` empty columns or rows → `should_chart=False, reason="empty_result"`.
4. `:375-376` `chart_suppressed(message)` → `False, "chart_suppressed_by_request"`. Outranks `explicit_override` (report `chart: true`).
5. `:378` profile columns (or use the caller's profiles).
6. `:379-381` split into `numeric` / `dims` (`categorical`+`boolean`) / `dates`.
7. `:383-384` explicit → `_decide_explicit`.
8. `:385-386` `mode == "hybrid"` → `_decide_hybrid`. Any other mode string → `False, "not_requested"` (`:387`).

`_decide_explicit` (`:390-479`):
- `:399-400` `named = requested_chart_type(message)`, `stacked = requested_stacked(message)`.
- `:403-414` histogram → requires ≥1 numeric; **`histogram_source = numeric[0].name`** (first numeric column in result order, *not* the column the user named).
- `:416-434` funnel → needs a dim + a numeric; picks the first `stage_named` dim else `dims[0]` (`:420`); `trusted_stage_order` unknown → downgrade to `horizontal_bar` confidence 0.6 (`:424-431`).
- `:436-443` scatter → needs ≥2 numeric; uses `numeric[0]` as x, `numeric[1]` as y (column order, not user intent).
- `:445-458` pie/donut → needs dim + numeric; negative metric downgrades to a bar (`:450-455`). **No category-count cap.**
- `:460-469` bar/horizontal_bar/line/area → x is `dates[0]` for line/area when a date exists, else `dims[0]`, else `dates[0]`; **`y_keys = [n.name for n in numeric]` — every numeric column** (`:468`). **No category-count cap.**
- `:472-475` unnamed "chart" → `_unambiguous_shape`.
- `:478` otherwise → `use_model=True, confidence 0.0`.

`_unambiguous_shape` (`:481-523`): no numeric → `None` (`:495`); one date and no dims → `line` 0.9 (`:498-502`); one dim and no dates → `None` if `dim.unique > MAX_CATEGORIES` (`:507-508`), funnel when `stage_named` + single metric + trusted order (`:510-518`), else `_bar_flavour(dim)` 0.85 (`:519-522`).

`_decide_hybrid` (`:526-596`): `n_rows < 2 or > 500` → refuse (`:541-542`); no numeric → refuse (`:543-544`); `>1 numeric and no dates` → refuse (`:545-549`); one date, no dims → `line` 0.9 (`:552-556`); must be exactly one dim, no dates, one numeric (`:558-559`); `dim.unique != n_rows` → refuse "result_not_aggregated_by_category" (`:562-565`); stage + trusted order → `funnel` (`:569-573`); `≤6` categories and non-negative → `donut` (`:576-583`); `2..40` categories → bar/horizontal_bar (`:586-595`).

`build_spec` (`:599-623`): rejects when not charting / no type / no `x_key` (`:606`); `x_key` must be a real column (`:608-610`); filters `y_keys` to real columns and rejects if empty (`:611-613`); constructs `ChartSpec` inside a **bare `except Exception: return None`** (`:622-623`).

**State & side effects** — one env read (`os.getenv("CHART_FUNNEL_STAGE_ORDER")` at `:211`), re-read
**on every call** to `stage_orders()` → `trusted_stage_order()`. No DB, no filesystem, no network,
no GPU. `_NO` (`:301`) is a module-level mutable dataclass instance but is never returned or
mutated — dead.

**Dependencies**
- inbound: `chart_pipeline.py:25` (`ChartDecision, build_spec, decide`), `chart_pipeline.py:226` (`trusted_stage_order`, function-local import), `engines/sql.py:20,33` (`chart_decision.LEGACY_CHART_RE` → `CHART_RE`), `tests/test_chart_decision.py:9,395`.
- outbound: `json`, `os`, `re`, `dataclasses`, `typing`; `chart_profile.ColumnProfile/profile_columns` (`:30`); `chart_spec.ChartSpec` (`:31`).

**Config**
- `CHART_FUNNEL_STAGE_ORDER` — `chart_decision.py:211`. Read directly from `os.getenv`, **not** via
  `app/config.py` (unlike `CHART_TRIGGER_MODE`, validated at `config.py:230-231`). Declared in
  `.env.example:77-78`.
- `CHART_TRIGGER_MODE` — consumed indirectly as the `mode` argument; validated at
  `config.py:230-231` against `CHART_TRIGGER_MODES = ("explicit","hybrid")` (`config.py:38`);
  `.env.example:69`.

**Failure modes**
- `trusted_stage_order` `:251` raises **`KeyError`** for blank/whitespace labels — `distinct` excludes them at `:245` but the `sorted(seen, ...)` key at `:251` indexes every element of `seen`. **VERIFIED BY EXECUTION**: `trusted_stage_order(['Prospecting','Closed Won',''])` → `KeyError('')`.
- `_load_custom_orders` swallows `json.JSONDecodeError`/`ValueError` at `:216-217` and returns `{}` with **no log** — a typo'd `CHART_FUNNEL_STAGE_ORDER` silently disables the operator's custom order.
- `build_spec` `:622` bare `except Exception: return None` hides every `ChartSpec` validation error.
- `_labels_of` `:336-337` swallows per-row indexing errors.
- No timeouts/retries/bounds apply (no I/O).

**Concurrency** — fully synchronous. `stage_orders()` rebuilds a dict from env on every call; no
caching, no lock, no shared mutable state.

**Complexity hotspots**
- `_decide_explicit` `chart_decision.py:390` — **91 LOC**, ~14 decision points (largest function in the whole assignment).
- `_decide_hybrid` `chart_decision.py:526` — **73 LOC**, ~13 decision points.
- `decide` `chart_decision.py:341` — 49 LOC.
- `_unambiguous_shape` `chart_decision.py:481` — 43 LOC.

**Notable**
- `can_funnel` (`:255-256`) is **dead** — zero callers, including tests (`rg -n 'can_funnel' orchestrator/` matches only the definition). Its `profile: ColumnProfile` parameter is unused inside the body.
- `ChartDecision.as_dict` (`:292-298`) is **dead** — no caller.
- `_NO` (`:301`) is **dead** — never referenced.
- `engines/sql.py:33 CHART_RE = chart_decision.LEGACY_CHART_RE` is **dead** — `rg -n 'CHART_RE' orchestrator/` shows no use of `CHART_RE`.
- `_MODIFIER_RE` `:82` alternative `an?\s+\w+` matches any "make it a <word>". **VERIFIED BY EXECUTION**: `explicit_chart_request` returns `True` for `"make it a table"`, `"make it a summary"`, `"make it a list"`, `"make it a bullet list"`, `"make that a paragraph"`, `"make it a csv"`, `"make it an export"`; none of these is caught by `_SUPPRESS_RE`.
- **VERIFIED BY EXECUTION**: `decide("make it a table", ['stage','total'], [['Prospecting',1],['Closed Won',2],['Qualification',3]])` → `ChartDecision(should_chart=True, chart_type='funnel', reason='stage_column_with_trusted_order', confidence=0.9, ...)`.
- **VERIFIED BY EXECUTION**: `decide("give me a pie chart of revenue by account", ['account','revenue'], 300 rows)` → `chart_type='pie'` with no category cap.
- **VERIFIED BY EXECUTION**: `decide("bar chart please", ['stage','cnt','amount','avg_days'], …)` → `y_keys=['cnt','amount','avg_days']` (10 / 9 000 000 / 90 on one linear axis).
- **VERIFIED BY EXECUTION**: `decide("show a histogram of amount", ['stage','cnt','amount','avg_days'], …)` → `histogram_source='cnt'` — the user's named column is ignored.
- Magic numbers: `8`/`16`/`40`/`6` (`:265,267,269,271`); the `{0,40}` char window in `_NATURAL_RE` (`:48`); the `500`-row hybrid ceiling (`:541`); confidences `1.0/0.9/0.85/0.8/0.6/0.5/0.0` scattered through `_decide_*` — no shared constant.
- Hybrid mode is far stricter than explicit mode: `:545-549` refuses multi-metric outright, while the explicit named-type path (`:468`) accepts all metrics.
- No TODO/FIXME/HACK.

---

### orchestrator/app/core/chart_pipeline.py  (247 LOC)

**Purpose** — The single entry point (`build_chart`) shared by the SQL engine, the agent route and
the report engine: decide → optionally ask the model → validate → prepare data. Guarantees it never
raises.

**Public surface**
- `log = logging.getLogger(__name__)` — `chart_pipeline.py:29`.
- `AskModel = Callable[[List[dict]], Awaitable[str]]` — `chart_pipeline.py:32`.
- `@dataclass ChartResult` — `chart_pipeline.py:35-46`: `spec, columns, rows, reason="", confidence=0.0, derived=False`.
- `chart_prompt(question, profiles, types) -> List[dict]` — `chart_pipeline.py:49-76`.
- `MODEL_CHART_TYPES: Tuple[str, ...]` — `chart_pipeline.py:83-91` (7 types; `histogram` and `funnel` deliberately excluded, `:79-82`).
- `async build_chart(message, columns, rows, *, mode="explicit", ask_model=None, title="", force=False) -> Optional[ChartResult]` — `chart_pipeline.py:94-117`.
- `async _build_chart(...)` — `chart_pipeline.py:120-186`.
- `_auto_title(decision) -> str` — `chart_pipeline.py:189-191`.
- `_repair(spec, profiles) -> Optional[ChartSpec]` — `chart_pipeline.py:194-211`.
- `_order_rows(spec, columns, rows) -> List[List[object]]` — `chart_pipeline.py:214-247`.

**Control flow** (`build_chart` → `_build_chart`)
1. `:110-114` delegate to `_build_chart` inside `try`.
2. `:115-117` **blanket `except Exception`** → `log.warning(..., exc_info=True)`, return `None`.
3. `:130-131` empty columns/rows → `None`.
4. `:133` `profile_columns(columns, rows)`.
5. `:134-137` `decide(..., explicit_override=True if force else None)`.
6. `:138-139` `should_chart` false → `None`.
7. `:142-158` **histogram branch**: `build_histogram(columns, rows, decision.histogram_source)` (`:143`, no `bins` argument); `None` → `None` (`:144-145`); construct `ChartSpec(type="histogram", x_key="bin", y_keys=["count"], title=..., bins=k, show_legend=False)` (`:147-154`); return `ChartResult(..., derived=True)` (`:155-158`).
8. `:161-173` **model branch** (`decision.use_model`): `ask_model is None` → `None`; `await ask_model(chart_prompt(message, profiles, MODEL_CHART_TYPES))` (`:164`); `parse_chart_spec(raw, columns=columns)` (`:165`); `_repair` (`:168`); return `ChartResult(spec, columns, rows, "model_spec", 0.5)` (`:171-173`, `derived` defaults False).
9. `:176-186` **deterministic branch**: `build_spec(decision, columns, title=title or _auto_title(decision))`; `_order_rows` (`:179`); `derived = ([list(r) for r in rows] != ordered)` (`:183`); return `ChartResult` (`:184-186`).

`_repair` (`:194-211`): keeps only `y_keys` whose profile `is_numeric` (`:202`); empty → `None`
(`:203-204`); for `scatter` the `x_key` must also be numeric (`:205-207`); returns
`spec.model_copy(update={"y_keys": numeric})` (`:211`) — **`model_copy` bypasses validators**,
including `_normalize_options`.

`_order_rows` (`:214-247`): function-local imports of `trusted_stage_order` (`:226`) and
`profile_column, _column_values` (`:227`, a **private** symbol). `x_key` missing → passthrough
(`:230-231`). `funnel` → sort by trusted stage rank, unknown labels to the end (`:235-241`).
`line`/`area` → re-profile the x column; if `is_date and not monotonic`, sort by `str(r[xi])`
(lexicographic) (`:243-246`).

**State & side effects**
- **Network / GPU egress**: `await ask_model(...)` at `:164`. In production `ask_model` is
  `engines/sql.py:210-211 _ask_chart_model` → `llm.chat_completion(messages, temperature=0.0,
  max_tokens=2500)` → the local vLLM server. No timeout is set at this layer.
- Logging: `log.warning` at `:116`.
- No filesystem, no DB, no env reads, no module-level mutable state.

**Dependencies**
- inbound: `engines/sql.py:21` (`ChartResult, build_chart`) → `attach_chart` `sql.py:214-243`, called at `sql.py:390` (live SOQL) and `sql.py:426` (warehouse); `engines/agent.py:255,269` (imports `attach_chart` from `.sql`); `engines/report.py:22,167` (`build_chart`); `tests/test_chart_pipeline.py:8`, `tests/test_report_charts.py:92`.
- outbound: `json`, `logging`, `dataclasses`, `typing`; `chart_data.build_histogram` (`:24`); `chart_decision.{ChartDecision, build_spec, decide}` (`:25`); `chart_profile.{ColumnProfile, profile_columns}` (`:26`); `chart_spec.{ChartSpec, parse_chart_spec}` (`:27`).

**Config** — none read directly. `mode` is supplied by callers from `settings.chart_trigger_mode`
(`sql.py:234`, `report.py:171`).

**Failure modes**
- The blanket `except Exception` at `:115` converts **every** internal defect into a silent "no chart" plus one WARNING line. It currently masks at least two ordinary-data bugs (Findings 5 and 6).
- `ask_model` has no timeout, no retry and no cancellation guard at this layer; a hung vLLM call stalls `attach_chart` and therefore the whole `/chat` SSE stream (`sql.py:426` is awaited before the narrative stream starts at `sql.py:428+`).
- `parse_chart_spec` returning `None` is indistinguishable from "model unreachable" — both produce `None` at `:166-167` with no log.
- `_order_rows` `:238` calls `trusted_stage_order`, which can raise `KeyError` (Finding 6); caught by the blanket handler → the chart is lost, not just the ordering.

**Concurrency** — `build_chart`/`_build_chart` are `async def`; the only `await` is `ask_model`
(`:164`). Everything else (profiling, deciding, binning, row materialisation, sorting) is
CPU-bound work executed inline on the event loop. For the SQL path this runs over up to 500 preview
rows (`sql.py:397,426`); for reports over `rows[:50]` (`report.py:169`). No shared mutable
module-level state; no locks needed.

**Complexity hotspots** — `_build_chart` `chart_pipeline.py:120` = **69 LOC**, three mutually
exclusive branches, cyclomatic ≈ 12.

**Notable**
- `chart_prompt` (`:49-76`) is a genuinely tight prompt-injection boundary: only `p.to_prompt_dict()` (aggregate metadata) is serialised (`:59`); no cell value reaches the model. The user's own `question` is the only free text (`:75`). For reports the "question" is `sec["instruction"]` (`report.py:168`), which is itself LLM-generated planner output.
- `_build_chart` `:143` never forwards a requested bin count, so `ChartSpec.bins` / `clamp_bins`'s user path is unreachable.
- `derived` carries two different meanings — "re-binned" (`:157`) and "re-ordered" (`:185`) — reconciled downstream by `sql.py:241-242` emitting `meta["chart_data"]`.
- `:183` materialises a full second copy of every row purely to compute a boolean.
- Local imports at `:226-227` exist only to avoid a circular import with `chart_decision`; `_column_values` is a private symbol of another module.
- No TODO/FIXME/HACK.

---

### orchestrator/app/core/chart_profile.py  (230 LOC)

**Purpose** — Infer a per-column "shape" (kind, cardinality, range, label length) from returned
rows, emitting **aggregate metadata only** so no Salesforce cell value can reach a model prompt.

**Public surface**
- `ColumnKind = str` — `chart_profile.py:26`.
- Regexes: `_BOOL_TOKENS` `:31`; `_SF_ID_RE` `:36`; `_ID_NAME_RE` `:37`; `_DATE_RE` `:39-43`; `_PERIOD_RE` `:45`; `_TIME_NAME_RE` `:47-50`; `_STAGE_NAME_RE` `:54`.
- `_is_missing(v) -> bool` `:57-58`.
- `_as_number(v) -> Optional[float]` `:61-80` — bools and `'true'/'false'` are **not** numbers (`:62-63`, `:74-75`).
- `_is_datelike(v) -> bool` `:83-89`.
- `_is_boolish(v) -> bool` `:92-95`.
- `@dataclass ColumnProfile` `:98-151` — fields `name, kind, total, non_null, unique, minimum, maximum, has_negative, max_label_len, all_distinct, monotonic, time_named, stage_named`; properties `is_numeric` `:120-122`, `is_date` `:124-126`, `is_categorical` `:128-130`, `nulls` `:132-134`; `to_prompt_dict()` `:136-151`.
- `_column_values(rows, index) -> List[object]` `:154-161`.
- `profile_column(name, values) -> ColumnProfile` `:164-216`.
- `profile_columns(columns, rows) -> List[ColumnProfile]` `:219-226`.
- `profile_index(profiles) -> dict` `:229-230`.

**Control flow** (`profile_column`) — inference falls through in this order:
1. `:165-179` build the profile shell: `total`, `present` (non-missing), `non_null`, `distinct = {str(v) …}`, `max_label_len`, `all_distinct`, `time_named`, `stage_named`.
2. `:180-182` no present values → `kind = "categorical"`.
3. `:184-186` all bool-ish → `"boolean"`.
4. `:190-193` name matches `_ID_NAME_RE` **and** every value matches `_SF_ID_RE` → `"identifier"`.
5. `:195-199` all date-like → `"date"`, and `monotonic = (labels == sorted(labels))` over the *compacted* present list.
6. `:201-208` all numeric → `"identifier"` if id-named else `"numeric"`; records `minimum`, `maximum`, `has_negative`.
7. `:212-215` `all_distinct and max_label_len > 40` → `"text"`, else `"categorical"`.

**State & side effects** — none. Pure, stdlib only (`:19-23`). No I/O, no env, no globals.

**Dependencies**
- inbound: `chart_data.py:16` (`_as_number`, private), `chart_decision.py:30` (`ColumnProfile, profile_columns`), `chart_pipeline.py:26` and `:227` (`profile_column`, `_column_values` — private), `tests/test_chart_decision.py:19`, `tests/test_chart_pipeline.py:9`.
- outbound: `datetime`, `re`, `dataclasses`, `decimal`, `typing`.

**Config** — none.

**Failure modes**
- `_column_values` `:158-160` swallows `IndexError/KeyError/TypeError` and substitutes `None` — a short row silently becomes a null.
- `_as_number` swallows `InvalidOperation/ValueError` (`:69-70`, `:77-79`).
- `profile_column` is O(n) per column but calls `str(v)` three separate times per value (`:168`, `:175`, `:197`). For a 500-row × 20-column preview that is 30 000 `str()` calls per `attach_chart`; acceptable, but it is on the event loop.
- Nothing raises. Nothing has a timeout (no I/O).

**Concurrency** — synchronous and pure; safe to call from anywhere. Module-level regexes are
immutable.

**Complexity hotspots** — `profile_column` `chart_profile.py:164` = **55 LOC**, ~14 branches.

**Notable**
- The no-cell-values invariant is real and enforced by construction: `to_prompt_dict` (`:136-151`) emits only `name/kind/rows/non_null/distinct` plus `min/max/has_negative` for numeric or `max_label_len` otherwise. This is the single shape that reaches `chart_prompt` (`chart_pipeline.py:59`).
- `profile_index` (`:229-230`) is **dead** — `rg -n 'profile_index' orchestrator/` matches only the definition. `chart_pipeline._repair:201` builds the same dict inline.
- `monotonic` (`:198`) compares the *string* forms, and is computed over `present` (nulls removed) while `_order_rows` sorts the full row list — the two disagree when the date column has nulls.
- Magic numbers: `40` (free-text label threshold, `:212`); `15`/`18` Salesforce Id lengths (`:36`).
- `_BOOL_TOKENS` comment (`:28-30`) documents the DuckDB `'true'/'false'` TEXT quirk that also breaks `WHERE IsWon = 'True'`.
- No TODO/FIXME/HACK.

---

### orchestrator/app/core/charts_png.py  (231 LOC)

**Purpose** — Render an already-validated `ChartSpec` to a PNG with matplotlib/Agg for pandoc report
embedding. The only server-side renderer; the browser uses ECharts from the same spec.

**Public surface**
- `class UnsupportedChartType(ValueError)` — `charts_png.py:26-27`.
- `class EmptyChartData(ValueError)` — `charts_png.py:30-31`.
- `PNG_SUPPORTED: frozenset` — `charts_png.py:41-43` (8 types; `funnel` excluded).
- `PNG_TABLE_ONLY = frozenset({"funnel"})` — `charts_png.py:48`.
- `_MAX_PIE_SLICES = 8` — `charts_png.py:50`.
- `_num(value) -> float` — `charts_png.py:53-61`.
- `supports(chart_type) -> bool` — `charts_png.py:64-66`.
- `render_chart_png(spec, columns, rows, out_path) -> Path` — `charts_png.py:69-133`.
- `_draw_part_to_whole(ax, spec, cols, rows, xs)` — `charts_png.py:136-153`.
- `_draw_horizontal_bar(ax, spec, cols, rows, xs)` — `charts_png.py:156-179`.
- `_draw_cartesian(ax, spec, cols, rows, xs)` — `charts_png.py:182-221`.
- Import-time guard `_UNDECIDED` — `charts_png.py:227-231`, raises `RuntimeError` if a `ChartType` has no report policy.

**Control flow** (`render_chart_png`)
1. `:83-84` reject anything that is not a `ChartSpec` instance → `TypeError`.
2. `:85-88` type not in `PNG_SUPPORTED` → `UnsupportedChartType`.
3. `:90-95` `x_key` and every `y_key` must be a real column → `EmptyChartData`.
4. `:96-98` no rows → `EmptyChartData`.
5. `:100-103` **lazy** `import matplotlib`; `matplotlib.use("Agg", force=True)`; `import matplotlib.pyplot as plt`.
6. `:105-106` `xs = [str(r[xi]) for r in rows]`.
7. `:108` `fig, ax = plt.subplots(figsize=(8, 4.5))` — **outside** the `try`.
8. `:109-130` inside `try`: dispatch to one of three drawing helpers (`:110-115`), set title (`:117-118`), set axis labels except for pie/donut (`:119-127`), `fig.tight_layout()` (`:128`), `fig.savefig(out_path, dpi=144)` (`:129-130`).
9. `:131-132` `finally: plt.close(fig)`.
10. `:133` return `Path(out_path)`.

`_draw_part_to_whole` (`:136-153`): coerces negatives to 0 (`:138`), **sorts descending by value**
(`:139`), collapses everything past slice 7 into `"Other"` (`:140-143`), raises `EmptyChartData` if
the total is ≤ 0 (`:146-147`), draws `ax.pie(..., autopct="%1.1f%%")` with `wedgeprops={"width":0.42}`
for donut (`:151-152`).

`_draw_horizontal_bar` (`:156-179`): stacked or single-series → `barh` with running `left`
(`:164-166`); otherwise grouped with `height = 0.8/n_series` (`:168-170`); tick offsets `:171-174`;
`invert_yaxis()` (`:177`); legend only when `n_series > 1 and spec.show_legend` (`:178-179`).

`_draw_cartesian` (`:182-221`): histogram → `ax.bar(idx, ys, width=1.0, align="edge",
edgecolor="white")` (`:196`); stacked bar → running `bottom` (`:197-199`); grouped bar → `width =
0.8/n_series` (`:200-203`); line → `plot(marker="o")` (`:204-205`); area → `plot` + `fill_between(alpha=0.3)`
(`:206-208`); scatter → `ax.scatter(idx, ys)` — **x is the row index, not the x column value**
(`:209-210`); tick placement `:211-217`; label rotation 45° when any label exceeds 8 chars (`:218-219`).

**State & side effects**
- **Filesystem write**: `fig.savefig(out_path, dpi=144)` at `:130`. `out_path` comes from
  `report.py:189` = `tmp_dir / f"chart-{index}.png"` inside a `tempfile.TemporaryDirectory`
  (`report.py:234`) — no user-controlled path component, no traversal risk. The directory is
  removed by the `with` block at `report.py:234`.
- **Global mutation**: `matplotlib.use("Agg", force=True)` at `:102` mutates the process-wide
  matplotlib backend on **every** call; `plt.subplots` at `:108` registers the figure in pyplot's
  global `Gcf` manager.
- No network, no DB, no env reads, no GPU.

**Dependencies**
- inbound: `engines/report.py:23` (`PNG_SUPPORTED, render_chart_png`), called at `report.py:190`; `tests/test_charts_png.py:12`, `tests/test_imports.py:9`.
- outbound: `pathlib`, `typing`; `chart_spec.{CHART_TYPES, ChartSpec}` (`:23`); lazily `matplotlib`, `matplotlib.pyplot` (`:100-103`).

**Config** — none.

**Failure modes**
- Raises `TypeError` (`:84`), `UnsupportedChartType` (`:86`), `EmptyChartData` (`:92`, `:95`, `:98`, `:147`). All are caught by `report.py:194-195`'s blanket `except Exception` → the section keeps its prose and table and loses only the image.
- `_num` (`:53-61`) returns **`0.0`** for `None` and for anything `float()` rejects (`:59-61`). A NULL metric therefore draws as a real zero bar in the report while ECharts draws a gap.
- **No figure leak** — `plt.close(fig)` is in a `finally` (`:131-132`) and `plt.subplots` at `:108` cannot leave a half-registered figure because it precedes the `try`. Verified by reading; no other `plt.figure`/`subplots` call exists in the module.
- No bound on `len(rows)`; a 500-category bar chart is drawn at 8×4.5 in / 144 dpi with 500 tick labels.
- No timeout on the render itself.

**Concurrency**
- `render_chart_png` is a **synchronous** function called from `async def _section_chart`
  (`report.py:190`) with **no** `to_thread`/executor — it blocks the event loop for its full
  duration. **VERIFIED BY EXECUTION** on this machine (`orchestrator/.venv`, aarch64):
  first-call `import matplotlib` + `pyplot` = **0.203 s**; a 50-row bar chart (the report's slice
  size, `report.py:169`) = **0.113 s** per call; a 500-row bar chart = **0.700 s**.
- `matplotlib.pyplot` is **not thread-safe**; the module uses the global pyplot API
  (`plt.subplots`/`plt.close`) rather than the object-oriented `Figure` + `FigureCanvasAgg`. Today
  there is no race because everything is on one thread, but the natural fix for the blocking
  problem (`asyncio.to_thread(render_chart_png, …)`) would introduce one.
- `matplotlib.use(..., force=True)` on every call is a global write; harmless single-threaded,
  a race under any future threading.

**Complexity hotspots** — `render_chart_png` `charts_png.py:69` = **67 LOC** (>60), cyclomatic ≈ 12
counting the label/axis branches at `:117-127`. `_draw_cartesian` `charts_png.py:182` = 50 LOC with
7 type branches.

**Notable**
- `supports()` (`:64-66`) is **dead in production** — only `tests/test_charts_png.py:18,49` call it; `report.py:180` inlines `result.spec.type not in PNG_SUPPORTED`.
- Renderer divergence between report and browser, all inside this file: slices re-sorted descending (`:139`) and truncated to 8 with an "Other" bucket (`:140-143`) — ECharts does neither; `scatter` plots against the row index rather than the x value (`:210`) — a scatter of `revenue` vs `headcount` is drawn as `headcount` vs `0..n`, which is not a scatter plot at all; NULL → 0.0 (`:60-61`).
- Magic numbers: `figsize=(8, 4.5)` `:108`; `dpi=144` `:130`; `_MAX_PIE_SLICES = 8` `:50`; wedge `width=0.42` `:151`; group width `0.8` `:168`, `:200`; `alpha=0.3` `:208`; label-rotation threshold `8` chars `:218`.
- The import-time `_UNDECIDED` guard (`:227-231`) is a good invariant: adding a `ChartType` without a report policy fails at import.
- The docstring at `:10-16` accurately describes a previously-fixed blank-titled-PNG bug.
- No TODO/FIXME/HACK.

---

### orchestrator/app/core/exports.py  (125 LOC)

**Purpose** — Write a result set to `.xlsx` (openpyxl) or `.csv` in a given directory under a
`<slug>-<timestamp>.<ext>` filename, capped at 100 000 rows.

**Public surface**
- `PREVIEW_ROW_CAP = 500` — `exports.py:15`. Imported by `config.py:11`, used at `config.py:234`.
- `EXPORT_ROW_CAP = 100_000` — `exports.py:16`. Imported by `config.py:11`, used at `config.py:235`.
- `_SLUG_RE = re.compile(r"[^a-z0-9]+")` — `exports.py:18`.
- `slugify(text, max_len=40, fallback="export") -> str` — `exports.py:21-24`.
- `timestamped_filename(slug, ext) -> str` — `exports.py:27-29`.
- `cap_rows(rows, cap) -> Tuple[list, bool]` — `exports.py:32-38`.
- `apply_export_cap(rows, cap=EXPORT_ROW_CAP) -> Tuple[list, bool]` — `exports.py:41-43`.
- `_cell_value(value) -> object` — `exports.py:46-50`.
- `export_xlsx(columns, rows, directory, slug, cap=EXPORT_ROW_CAP) -> Tuple[Path, bool]` — `exports.py:53-93`.
- `export_csv(columns, rows, directory, slug, cap=EXPORT_ROW_CAP) -> Tuple[Path, bool]` — `exports.py:96-113`.
- `__all__` — `exports.py:116-125`.

**Control flow** (`export_xlsx`)
1. `:64-66` lazy `from openpyxl import Workbook`, `Font`, `get_column_letter`.
2. `:68` `apply_export_cap(rows, cap)` → truncated flag.
3. `:69-71` `Path(directory)`, `mkdir(parents=True, exist_ok=True)`, build path from `timestamped_filename`.
4. `:73-79` `Workbook()` (in-memory, **not** `write_only`), sheet titled `"Data"`, header row appended and bolded.
5. `:80-81` append every data row through `_cell_value`.
6. `:84-90` auto column widths from the header plus the first 1000 rows, `min(longest + 2, 60)`.
7. `:92-93` `wb.save(path)`; return `(path, truncated)`.

(`export_csv` `:104-113`: same cap/mkdir/path, then `csv.writer` with `newline=""`, `encoding="utf-8"`,
`None` → `""` at `:112`.)

**State & side effects**
- **Filesystem writes**: `directory.mkdir(parents=True, exist_ok=True)` (`:70`, `:106`) and file
  creation at `:92` / `:108`. The production `directory` is `settings.reports_dir`
  (`engines/sql.py:409`), default `/reports` (`config.py:100`, `docker-compose.yml:248`
  `REPORTS_DIR: /reports`).
- No network, no DB, no env reads, no GPU, no globals.

**Dependencies**
- inbound: `config.py:11` (`EXPORT_ROW_CAP, PREVIEW_ROW_CAP`); `engines/sql.py:22` (`cap_rows, export_csv, export_xlsx, slugify`) used at `sql.py:397` (`cap_rows`) and `sql.py:407-409` (exporters); `engines/report.py` uses `slugify` at `report.py:229`; `tests/test_exports.py:7`, `tests/test_row_caps.py:2`, `tests/test_imports.py:11`.
- outbound: `csv`, `re`, `time`, `pathlib`, `typing`; lazily `openpyxl` (`:64-66`).

**Config** — none read here. `EXPORT_ROW_CAP` / `SQL_PREVIEW_ROW_CAP` are read in
`config.py:234-235` from these module constants as defaults.

**Failure modes**
- `cap_rows` raises `ValueError` for a negative cap (`:34-35`). Nothing else raises deliberately; `wb.save` / `open` propagate `OSError` (disk full, permission) to `sql.py:408`, which is inside the `/chat` streaming worker.
- `timestamped_filename` has **second** granularity (`:28`) and no entropy: two exports of the same question in the same second silently overwrite one another.
- No cap on total `REPORTS_DIR` size and no retention/cleanup anywhere — `rg -n 'retention|cleanup|prune|unlink|rmtree|max_age|purge' orchestrator/app/` returns hits only in `archive.py`, `repo.py`, `uploads.py`; nothing touches `reports_dir`.
- `export_xlsx` builds the entire workbook in RAM. **VERIFIED BY EXECUTION** (100 000 rows × 10 string columns): **4.8 s** wall, **433 MB** peak RSS (rows alone are 92 MB), 3.5 MB output.
- Values are written **verbatim**. **VERIFIED BY EXECUTION**: `export_xlsx(['Name','Amt'], [["=cmd|'/c calc.exe'!A0", 1]], …)` produces a cell with `data_type == 'f'` (a live formula); `export_csv` writes the line `=cmd|'/c calc.exe'!A0,1`.
- `slugify` (`:22-23`) strips everything outside `[a-z0-9]`, so no path separator, `..` or NUL can survive into the filename — path traversal via `slug` is not possible.
- `_cell_value` (`:46-50`) stringifies `Decimal`, `datetime` etc.; a `datetime` therefore lands in xlsx as text, not a date cell.

**Concurrency** — both exporters are **synchronous** and are awaited-adjacent inside `async def`
code: `engines/sql.py:408` calls `exporter(...)` inline on the event loop. The measured 4.8 s
(100k × 10) is 4.8 s during which no other SSE stream, health check or request progresses.

**Complexity hotspots** — none over 60 LOC. Largest is `export_xlsx` `exports.py:53` = 43 LOC.

**Notable**
- **Caller bug**: `sql.py:292` sizes the DuckDB fetch with `settings.export_row_cap` (`config.py:235`, env-overridable) but `sql.py:408-409` calls the exporter **without** `cap=`, so `exports.py` falls back to the hard-coded `EXPORT_ROW_CAP = 100_000`. Setting `EXPORT_ROW_CAP=250000` fetches 250 001 rows and silently writes 100 000.
- **Caller bug**: `sql.py:408` binds the truncation flag to `_export_truncated` and never uses it; `meta` (`sql.py:414-421`) carries only `{filename, type, size}`.
- Magic numbers: `max_len=40` `:21`; sample size `1000` `:84`; padding `+2` and width cap `60` `:90`.
- Files land in the same flat directory that `main.py:257-271` serves without authentication (`main.py:55-56`: "/chat and /reports* remain auth-free"). See Finding 1.
- No TODO/FIXME/HACK.

---

### orchestrator/app/core/pdf.py  (67 LOC)

**Purpose** — Turn a base64 PDF into page PNG data-URLs plus its text layer, for the multimodal
model. Uses pypdfium2 (self-contained arm64 wheel).

**Public surface**
- `MAX_PDF_PAGES = 6` — `pdf.py:18`.
- `RENDER_SCALE = 2.0` — `pdf.py:19` ("~144 DPI").
- `MAX_TEXT_CHARS = 24000` — `pdf.py:20`.
- `_strip_data_url(b64) -> str` — `pdf.py:23-24`.
- `render_pdf(pdf_base64, max_pages=MAX_PDF_PAGES) -> Tuple[List[str], str, int]` — `pdf.py:27-67`.

**Control flow** (`render_pdf`)
1. `:35` lazy `import pypdfium2 as pdfium`.
2. `:37` `base64.b64decode(_strip_data_url(pdf_base64))` — no `validate=True`, no size check.
3. `:38` `pdfium.PdfDocument(pdf_bytes)` — native PDFium parse of untrusted bytes.
4. `:40-42` `total = len(pdf)`; `n = min(total, max_pages)`.
5. `:44-58` per page: `page.get_textpage()` → `get_text_range()` → `textpage.close()` (`:46-48`);
   `page.render(scale=RENDER_SCALE)` (`:50`); `bitmap.to_pil().convert("RGB")` (`:51`);
   `pil.save(buf, format="PNG")` (`:52-53`); base64 data-URL appended to `images` (`:54-57`);
   `page.close()` (`:58`).
6. `:60-64` join per-page text with `--- Page N ---` headers, truncate to `MAX_TEXT_CHARS`.
7. `:65` return `(images, text, total)`.
8. `:66-67` `finally: pdf.close()`.

**State & side effects**
- Native memory allocation by PDFium for the page bitmaps; Pillow copies; base64 strings.
- No filesystem, no network, no DB, no env reads, no globals, no GPU (the *result* is fed to the
  vLLM model by `engines/document.py:66-71`).

**Dependencies**
- inbound: `engines/document.py:13` (`from ..core.pdf import render_pdf`), called at `document.py:34` inside `async def run_pdf_engine`, reached from `main.py:551-557` for `POST /chat` with a `pdf` field (`main.py:196-203`); `core/extract.py:58-61` `_extract_pdf_text` (web-fetch / URL ingestion), reached from `extract_readable` (`extract.py:73-75`) which `engines/search.py:25` and `engines/url.py` use; `tests/test_extract.py:53-55`.
- outbound: `base64`, `io`, `typing`; lazily `pypdfium2` and (transitively) Pillow.

**Config** — none. `MAX_PDF_PAGES`, `RENDER_SCALE`, `MAX_TEXT_CHARS` are hard-coded constants with
no env override.

**Failure modes**
- `base64.b64decode` without `validate=True` (`:37`) silently drops non-alphabet characters; bad padding raises `binascii.Error`, which propagates out of `run_pdf_engine` (`document.py:34`) — there is no `try` there.
- `pdfium.PdfDocument` (`:38`) parses attacker-supplied bytes in a C library; any `PdfiumError` propagates the same way.
- **No bound on the decoded PDF size** and **no bound on rendered pixel dimensions.** `page.render(scale=2.0)` (`:50`) allocates `ceil(w_pt*2) × ceil(h_pt*2) × 4` bytes. The PDF format permits a MediaBox up to 14 400 × 14 400 pt, giving 28 800 × 28 800 × 4 ≈ **3.3 GiB per page**, and `MAX_PDF_PAGES = 6` pages are rendered.
- `page.close()` at `:58` is **not** in a `finally`: if `render`/`to_pil`/`save` raises, the page handle leaks. `bitmap` is never closed at all (no `bitmap.close()`), so its native buffer is only reclaimed by pypdfium2's finaliser.
- All `n` rendered pages are held simultaneously in `images` (`:42`, `:54-57`) as base64 strings (4/3 blowup) and then all appended to one model message (`document.py:50-51`). There is no aggregate byte cap — only `MAX_TEXT_CHARS` bounds the *text*.
- No timeout on parsing or rendering.

**Concurrency** — `render_pdf` is **synchronous** and CPU/memory-heavy, called with no
`to_thread`/executor from `async def run_pdf_engine` (`document.py:34`). It blocks the event loop
for the whole parse + raster + PNG-encode + base64 of up to 6 pages. No shared mutable state.

**Complexity hotspots** — `render_pdf` `pdf.py:27` = **41 LOC**, cyclomatic ≈ 6. Under threshold.

**Notable**
- `core/extract.py:58-61` calls `render_pdf(..., max_pages=10)` — overriding `MAX_PDF_PAGES=6` — and then **discards the images**: `_images, text, _total = render_pdf(...)`. Ten pages are rasterised, PIL-converted, PNG-encoded and base64-encoded purely to be thrown away; only `text` is used. There is no text-only mode in this module.
- `RENDER_SCALE = 2.0` is a fixed multiplier of the *page's own* size, so the output resolution is entirely attacker/document controlled — the "~144 DPI" comment (`:19`) holds only for Letter/A4 pages.
- File header says "V8, 2026-07-23"; unlike the chart modules this file is **not** in the uncommitted set (`git status` lists no `core/pdf.py`).
- No TODO/FIXME/HACK.

---

## Uncommitted-work note

`git status` at session start shows `chart_data.py`, `chart_decision.py`, `chart_pipeline.py`,
`chart_profile.py` as **untracked** and `chart_spec.py`, `charts_png.py` as **modified** — the whole
decision/spec pipeline is new, unreviewed work. `exports.py` and `pdf.py` are unmodified. Test files
`test_chart_data.py`, `test_chart_decision.py`, `test_chart_pipeline.py`, `test_chart_routes.py`,
`test_charts_png.py`, `test_report_charts.py` are likewise untracked.

Coverage gaps observed while reading the tests alongside the source:
- `tests/test_chart_data.py:66` exercises the constant-column case at the `build_histogram` level
  (`bin_count == 1`) but nothing exercises it through `chart_pipeline.build_chart`, which is where it
  breaks (Finding 5).
- No test feeds a blank/empty stage label to `trusted_stage_order` (Finding 6).
- No test asserts that a table-oriented follow-up ("make it a table") does *not* trigger a chart
  (Finding 7).
- No test bounds the category count on the explicit named-type path (Finding 9).

## Reproduction commands used

```
cd /home/techsphere/Documents/projects/saleforce-LLM/orchestrator
.venv/bin/python -c "from app.core.chart_decision import trusted_stage_order; trusted_stage_order(['Prospecting','Closed Won',''])"
.venv/bin/python -c "import asyncio; from app.core.chart_pipeline import build_chart; print(asyncio.run(build_chart('show me a histogram of amount', ['amount'], [[100]]*4)))"
.venv/bin/python -c "from app.core.chart_decision import explicit_chart_request as e; print(e('make it a table'))"
.venv/bin/python -c "from app.core.chart_data import build_histogram; print(build_histogram(['amount'],[[i%2+1] for i in range(100)],'amount'))"
```
