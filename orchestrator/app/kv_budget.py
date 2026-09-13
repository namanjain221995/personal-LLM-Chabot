"""The engine's KV cache as a budget the admission lanes can spend (2026-09-13).

WHY THIS EXISTS. The owner's goal of 2026-09-13 lets a /v1 answer run to
1,000,000 output tokens. On the main engine that is one sequence for about
three hours that grows to 481 of the 799 usable KV blocks (60 % of the pool).
Two of them can never coexist, and a chat document that arrives beside one
can only fit if it is small. vLLM does not refuse a request it cannot hold:
it admits it and PREEMPTS a running one when the blocks run out, and with
prefix caching off a preemption is a full recompute (the 950K needle took
803.7 s to prefill). So the orchestrator must never commit more long work
than the engine can hold, and it has to decide that BEFORE the request is
sent. app/admission.py does the deciding; this module holds the arithmetic,
the pool read and the ledger, and nothing else.

THE LAYOUT, MEASURED 2026-09-13 (read-only).
- Prometheus `vllm:cache_config_info{job="vllm-main"}`: kv_cache_size_tokens
  "1663201", block_size "2096", num_gpu_blocks "800", cache_dtype "fp8",
  kv_cache_memory_bytes "8589934592", enable_prefix_caching "False",
  mamba_block_size "1000000", mamba_cache_mode "none".
- Engine log: `kv cache group sizes [1000000, 1000000, 1000000, 2096]`: three
  GDN/mamba state groups that hold one block each whatever the length, plus
  one attention group of 2096-token blocks. kv_cache_usage_perc samples are
  exact multiples of 1/799 and the minimum is 4 blocks per sequence; the
  950K needle peaked at 457 = 3 + ceil(950,000 / 2096).

THE CHARGE is projected and block-exact:

    charge = (F + ceil(min(prompt + max_tokens, window) / B)) * B

with F = ADMISSION_KV_FIXED_BLOCKS_PER_SEQ (3) and B the live block size. A
1M request costs (3 + 478) * 2096 = 1,008,176 tokens. PROJECTED, not actual:
charging what a sequence may grow to is the only way the orchestrator never
commits more than the engine holds. The price is idle reservation when a job
stops early (14 d: 53,573 requests ended on stop, 21,207 on length); a
usage-aware ledger waits for a soak that shows preemption behaviour.

THE BUDGET is `floor(pool * (1 - ADMISSION_KV_RESERVE_FRACTION))`, reserve
0.35 clamped to [0, 0.95]: 1,081,080 tokens. The reserved 582K tokens are
never granted to long work; they are the room for NORMAL traffic, the second
tenant's raw-port traffic and the canaries. Main-engine kv_cache_usage over
14 d with every bench and soak included: p99 0.133, p99.9 0.270, largest
sample below 0.30 = 0.298. One full window fits the budget alone; two
(2,016,352) never do; beside a running 1M job there are 72,904 tokens left.

THE MANAGED LIMIT (adversarial review 2026-09-13). The reserve alone does not
bound NORMAL: nine /v1 NORMAL requests with prompts just under the 131,072
LONG threshold project 9 × 70 = 630 blocks, and beside a committed 1M job
(481) that is 1,111 of 799 — a preemption, i.e. a recompute and one more
prefill beside decodes. So /v1 NORMAL requests commit their projected charge
too (kind "normal"), and everything the orchestrator commits — long work and
/v1 NORMAL — must fit `floor(pool * (1 - ADMISSION_KV_UNMANAGED_HEADROOM_FRACTION))`,
headroom 0.15 (1,413,720 tokens): the 14-day p99.5 of the whole engine's
usage was 0.153, and what is left uncharged is chat NORMAL (output p99 ~2K),
the second tenant and the canaries. Chat NORMAL is never charged: the
reserve is its room.

BACKFILL, NOT STOP (adversarial review 2026-09-13). A waiter that cannot be
granted yet keeps its place in KV through `since`: a later waiter may pass it
only if the charges admitted since that waiter arrived, plus the later one,
plus its own charge still fit. Whatever was committed before it arrived is
what it waits for; nothing admitted after it can push that wait further. A
big request is therefore never starved by small ones, and small ones are not
held for a big one's whole bound when they cannot delay it.

THE POOL is read live from the engine's own `/metrics` at most once per
ADMISSION_KV_POOL_REFRESH_S (300 s), single-flight, 2 s timeout, and only when
a long request's decision depends on it. A failed read falls back to the
last live value, else to the settings (the 2026-09-13 values, exact while the
zero-downtime rule forbids an engine config change). ADMISSION_KV_METRICS_URL
names another source, or `off` to use the settings and never read.

Every new setting is read through `_s_int`/`_s_float`/`_s_str`: the value on
`settings` when config.py defines it, else the environment parsed exactly as
config.py's `_int`/`_float` parse it (blank is the default, anything else goes
through int()/float()). They are called once at import so a malformed value
fails at boot, the way config.py fails, and again at call time so a test can
monkeypatch them.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
import weakref
from dataclasses import dataclass
from typing import Dict, Optional

from .config import settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings: the value on `settings` if config.py has it, else the environment
# ---------------------------------------------------------------------------


def _s_int(attr: str, env: str, default: int) -> int:
    v = getattr(settings, attr, None)
    if v is not None:
        return int(v)
    raw = os.environ.get(env)
    return default if raw is None or raw.strip() == "" else int(raw)


def _s_float(attr: str, env: str, default: float) -> float:
    v = getattr(settings, attr, None)
    if v is not None:
        return float(v)
    raw = os.environ.get(env)
    return default if raw is None or raw.strip() == "" else float(raw)


def _s_str(attr: str, env: str, default: str) -> str:
    v = getattr(settings, attr, None)
    if v is not None:
        return str(v).strip()
    raw = os.environ.get(env)
    return default if raw is None else raw.strip()


def reserve_fraction() -> float:
    """The share of the pool never granted to long work, clamped to [0, 0.95]:
    below 0 would grant more than the pool, above 0.95 would leave long work
    a budget too small to matter (it would then only ever run alone)."""
    return min(0.95, max(0.0, _s_float("admission_kv_reserve_fraction", "ADMISSION_KV_RESERVE_FRACTION", 0.35)))


def unmanaged_headroom_fraction() -> float:
    """The share of the pool nothing this process commits may take — chat
    NORMAL (never charged), the second tenant, the canaries. Clamped to
    [0, 0.95] like the reserve."""
    return min(0.95, max(0.0, _s_float("admission_kv_unmanaged_headroom_fraction",
                                       "ADMISSION_KV_UNMANAGED_HEADROOM_FRACTION", 0.15)))


def fixed_blocks_per_seq() -> int:
    return max(0, _s_int("admission_kv_fixed_blocks_per_seq", "ADMISSION_KV_FIXED_BLOCKS_PER_SEQ", 3))


def setting_pool_tokens() -> int:
    return max(1, _s_int("admission_kv_pool_tokens", "ADMISSION_KV_POOL_TOKENS", 1663201))


def setting_block_size() -> int:
    return max(1, _s_int("admission_kv_block_size", "ADMISSION_KV_BLOCK_SIZE", 2096))


def refresh_s() -> float:
    return max(0.0, _s_float("admission_kv_pool_refresh_s", "ADMISSION_KV_POOL_REFRESH_S", 300.0))


def metrics_url_override() -> str:
    return _s_str("admission_kv_metrics_url", "ADMISSION_KV_METRICS_URL", "")


#: ADMISSION_KV_METRICS_URL values that mean "never read, use the settings".
_OFF = frozenset({"off", "none", "false", "0", "setting", "settings"})


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pool:
    tokens: int
    block_size: int
    source: str  # "live" | "setting"
    read_at: float


_LINE = "vllm:cache_config_info{"
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_cache_config(text: str) -> Optional[Pool]:
    """The first `vllm:cache_config_info{...}` line carrying both
    kv_cache_size_tokens and block_size (as positive integers); None if no
    line does."""
    for line in (text or "").splitlines():
        if not line.startswith(_LINE):
            continue
        end = line.rfind("}")
        if end < 0:
            continue
        labels = dict(_LABEL.findall(line[len(_LINE):end]))
        try:
            tokens = int(labels["kv_cache_size_tokens"])
            block = int(labels["block_size"])
        except (KeyError, ValueError):
            continue
        if tokens > 0 and block > 0:
            return Pool(tokens=tokens, block_size=block, source="live", read_at=time.monotonic())
    return None


def metrics_url(base_url: str) -> str:
    """ADMISSION_KV_METRICS_URL when set; else the engine's own /metrics next
    to its OpenAI base URL (`http://h:8000/v1` -> `http://h:8000/metrics`)."""
    override = metrics_url_override()
    if override:
        return override
    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root + "/metrics"


def setting_pool() -> Pool:
    return Pool(tokens=setting_pool_tokens(), block_size=setting_block_size(), source="setting",
                read_at=time.monotonic())


async def _fetch_text(url: str) -> str:
    """One GET, 2 s, no proxy from the environment (trust_env=False): the
    engine is on the private network and a proxy would only add a failure."""
    import httpx

    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


class _State:
    __slots__ = ("checked_at", "live", "last_ok")

    def __init__(self) -> None:
        self.checked_at: Optional[float] = None
        self.live: Optional[Pool] = None
        self.last_ok = False


_states: Dict[str, _State] = {}
_flights: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, asyncio.Task]]" = weakref.WeakKeyDictionary()
_last_source: Dict[str, str] = {}
#: The pool the ledger's decisions last used — read synchronously by the
#: admission arbiter, which cannot await.
_current: Optional[Pool] = None


def _fallback(state: Optional[_State]) -> Pool:
    if state is not None and state.live is not None:
        return state.live
    return setting_pool()


def _note_source(url: str, found: Pool) -> None:
    global _current
    _current = found
    if _last_source.get(url) != found.source:
        _last_source[url] = found.source
        if found.source == "live":
            log.info("kv_budget: engine KV pool read from %s: %d tokens in %d-token blocks",
                     url, found.tokens, found.block_size)
        else:
            log.warning("kv_budget: engine KV pool unreadable at %s; using %d tokens in %d-token blocks "
                        "(last live value or settings)", url, found.tokens, found.block_size)


def cached() -> Pool:
    """The pool last used, without a read: the settings until one happens."""
    return _current if _current is not None else setting_pool()


async def pool(base_url: str) -> Pool:
    """The engine's KV pool: cached for ADMISSION_KV_POOL_REFRESH_S, one read
    at a time per event loop, the last live value or the settings on any
    failure. Never raises."""
    global _current
    override = metrics_url_override()
    if override.lower() in _OFF:
        found = setting_pool()
        _current = found
        return found
    url = metrics_url(base_url)
    state = _states.setdefault(url, _State())
    now = time.monotonic()
    if state.checked_at is not None and now - state.checked_at < refresh_s():
        found = state.live if (state.last_ok and state.live is not None) else _fallback(state)
        _current = found
        return found
    loop = asyncio.get_running_loop()
    flights = _flights.setdefault(loop, {})
    task = flights.get(url)
    if task is None or task.done():
        task = loop.create_task(_read(url, state))
        flights[url] = task
    # Shielded: a caller cancelled while waiting must not cancel the read the
    # other callers are sharing.
    return await asyncio.shield(task)


async def _read(url: str, state: _State) -> Pool:
    try:
        text = await _fetch_text(url)
        parsed = parse_cache_config(text)
    except Exception as exc:  # noqa: BLE001 — any failure is the fallback
        log.debug("kv_budget: pool read failed at %s: %s", url, type(exc).__name__)
        parsed = None
    state.checked_at = time.monotonic()
    if parsed is not None:
        state.live = parsed
        state.last_ok = True
        found = parsed
    else:
        state.last_ok = False
        found = _fallback(state)
    _note_source(url, found)
    return found


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def charge_tokens(prompt_tokens: int, max_tokens: int, *, block_size: int, window: Optional[int]) -> int:
    """(F + ceil(min(prompt + max_tokens, window) / B)) * B — module docstring."""
    b = max(1, int(block_size))
    total = max(0, int(prompt_tokens or 0)) + max(0, int(max_tokens or 0))
    if window:
        total = min(total, max(0, int(window)))
    return (fixed_blocks_per_seq() + math.ceil(total / b)) * b


def budget_tokens(p: Pool) -> int:
    return int(math.floor(int(p.tokens) * (1.0 - reserve_fraction())))


def managed_limit_tokens(p: Pool) -> int:
    """What long work and /v1 NORMAL together may commit (module docstring,
    THE MANAGED LIMIT)."""
    return int(math.floor(int(p.tokens) * (1.0 - unmanaged_headroom_fraction())))


def reserve_exhausted(sample: Optional[dict]) -> bool:
    """The controller's live kv_cache_usage has already eaten the reserve:
    traffic this process does not manage (the second tenant) is holding the
    room long work was promised to leave."""
    if not sample:
        return False
    usage = sample.get("kv_cache_usage")
    if usage is None:
        return False
    try:
        # 1e-9: 1 - 0.95 is 0.05000000000000004 in binary floating point.
        return float(usage) >= (1.0 - reserve_fraction()) - 1e-9
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


#: Ledger entry kinds: LONG and LONG_OUTPUT requests ("long", inside the
#: budget) and /v1 NORMAL requests ("normal", inside the managed limit only).
LONG_WORK = "long"
NORMAL_V1 = "normal"


@dataclass
class _Entry:
    charge: int
    lane: str
    origin: str
    admitted_at: float
    kind: str = LONG_WORK
    warned: bool = False


class Ledger:
    """Projected KV committed by admitted work, per event loop (it lives
    inside admission.Lanes). Commit and release are synchronous: a release
    runs from `finally` blocks and garbage collection, where awaiting is not
    an option, and a lost release would pin up to 60 % of the budget for
    hours.

    `committed` is long work only (what the budget bounds, and what /health
    and llm_admission_kv_committed_tokens have shown since the lane shipped);
    `normal_committed` is /v1 NORMAL; `managed` is both (what the managed
    limit bounds)."""

    def __init__(self) -> None:
        self._entries: Dict[object, _Entry] = {}
        self.committed = 0
        self.normal_committed = 0

    @property
    def managed(self) -> int:
        return self.committed + self.normal_committed

    def fits(self, charge: int, budget: int) -> bool:
        # A request heavier than the whole budget runs ALONE rather than never.
        return self.committed == 0 or self.committed + int(charge) <= int(budget)

    def fits_managed(self, charge: int, limit: int) -> bool:
        return self.managed == 0 or self.managed + int(charge) <= int(limit)

    def commit(self, key: object, charge: int, lane: str, origin: str, kind: str = LONG_WORK,
               at: Optional[float] = None) -> None:
        if key in self._entries:
            return
        entry = _Entry(int(charge), lane, origin, time.monotonic() if at is None else float(at), kind)
        self._entries[key] = entry
        if kind == NORMAL_V1:
            self.normal_committed += entry.charge
        else:
            self.committed += entry.charge

    def release(self, key: object) -> bool:
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        if entry.kind == NORMAL_V1:
            self.normal_committed = max(0, self.normal_committed - entry.charge)
        else:
            self.committed = max(0, self.committed - entry.charge)
        return True

    def holds(self, key: object) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def since(self, at: float, kind: Optional[str] = None) -> int:
        """Charges admitted at or after `at` (of `kind`, or all): what a
        waiter that arrived at `at` has seen admitted past it (module
        docstring, BACKFILL)."""
        return sum(e.charge for e in self._entries.values()
                   if e.admitted_at >= at and (kind is None or e.kind == kind))

    def by_origin(self, origin: str, kind: Optional[str] = None) -> int:
        return sum(e.charge for e in self._entries.values()
                   if e.origin == origin and (kind is None or e.kind == kind))

    def oldest_age_s(self, now: Optional[float] = None) -> float:
        if not self._entries:
            return 0.0
        at = time.monotonic() if now is None else now
        return max(0.0, at - min(e.admitted_at for e in self._entries.values()))

    def stale_entries(self, older_than_s: float, now: Optional[float] = None) -> list:
        """Entries older than `older_than_s` not yet warned about; marks them."""
        at = time.monotonic() if now is None else now
        found = []
        for entry in self._entries.values():
            if not entry.warned and at - entry.admitted_at > older_than_s:
                entry.warned = True
                found.append(entry)
        return found

    def describe(self) -> dict:
        by_lane: Dict[str, int] = {}
        for entry in self._entries.values():
            by_lane[entry.lane] = by_lane.get(entry.lane, 0) + entry.charge
        return {"committed_tokens": self.committed, "normal_committed_tokens": self.normal_committed,
                "entries": len(self._entries), "by_lane": by_lane}


def reset() -> None:
    """Tests only."""
    global _current
    _states.clear()
    _flights.clear()
    _last_source.clear()
    _current = None


# A malformed value fails at import — at boot, like config.py — not per request.
reserve_fraction()
unmanaged_headroom_fraction()
fixed_blocks_per_seq()
setting_pool_tokens()
setting_block_size()
refresh_s()
metrics_url_override()
