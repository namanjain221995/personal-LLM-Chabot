"""The PDF `ocr` stage: which pages to read, and reading them (design §4.4).

THE PLAN. Only THIN pages — fewer than `engines.document.TEXT_OK_CHARS` (200)
characters of text layer, listed by the `text` stage in `thin_pages.json` —
in page order, up to PUBLIC_API_FILES_OCR_PAGE_BUDGET (1,000). Past the
budget a page stays as its text layer found it and is counted in
`ocr_skipped_pages`; the list itself is written to `ocr_skipped.json` rather
than into the blob's facts jsonb, which a 10,000-page scan would bloat.
Why 1,000: 2.5-7 s/page at two wide is ~20-60 min of worker GPU, the order of
a long video's OCR stage (design §8).

THE READ. For each batch of pages the extraction child renders PNGs at
`core/pdf.RENDER_SCALE` (`render.render_pages`), then two workers read them,
one page per unit, each unit inside

    capacity.hold("ocr", wait_s=None, yield_to_chat=True, abandon=<the job's>)

— the public side's FIFO gate, which steps aside while a chat turn is busy.
The wait has no limit (2026-09-14): until then each unit gave up after
PUBLIC_API_BACKGROUND_GATE_WAIT_S and deferred the whole PDF for a gate that
was only busy. It ends on admission, or when the job is abandoned (lease
lost, blob being deleted) —
through the chat app's own `engines.ocr.read_images(..., prompt="OCR")`.
Why the prompt "OCR": measured 2026-09-11 on the production engine, the app's
default "document parsing" looped on a real PDF page (16,775 characters at a
unique-token ratio of 0.002, 42 s of GPU) where "OCR" read the same page in
2.5 s (`engines/ocr.document_prompt`'s docstring).

WHAT A RESULT DOES TO A PAGE.
* `ok` and the text layer was empty → the page text becomes the OCR text,
  `source: "ocr"`;
* `ok` and the text layer had 1-199 characters → the OCR text is appended
  after it, `source: "ocr"` (a scanned page with a typed footer keeps both);
* `degenerate` (`engines.ocr.classify`: a repetition loop) → rejected, the
  page is unchanged, counted in `ocr_degenerate_pages`. A loop is not a
  transcript, and it must never be cited;
* `empty` → the page really is blank; unchanged, counted;
* `failed` because the ENGINE was not there (connection refused, timeout,
  5xx, 429, a batch deadline) → not recorded: not the page's fault;
* `failed` because the engine REJECTED this page (anything else, e.g. a 400)
  → recorded as `{"status": "failed", "attempts": n}`. A page gets
  MAX_PAGE_ATTEMPTS (2) rejections, then it is final: counted in
  `ocr_failed_pages`, never read again, its text layer kept.

DURABLE PROGRESS. Every recorded page is appended to `ocr.jsonl` and fsynced
before its render is deleted, so a lost lease or a restart resumes with the
next unread page instead of re-spending GPU on 900 pages already read. The
merge into `pages.jsonl` happens once, at the end, atomically.

WHEN THE ENGINE IS NOT THERE. A gate refusal (only a bounded gate refuses;
the default one waits), or a batch with no success and at least one failure that
may still succeed later (unreachable, or a page's first rejection), raises
`EngineUnavailable`: the runner defers the blob (5 attempts, 300 s apart)
with the pages already recorded kept. One failed page among good ones is only
counted — an image the engine rejects must not block the other 999.

WHY PAGES REMEMBER THEIR REJECTIONS (review finding, 2026-09-13). Before, a
failed read was never recorded, and a resumed attempt planned only the unread
pages — so ONE page the engine always rejected sat alone in its batch on
every attempt, every batch was "all failed", and the fifth deferral failed
the whole PDF `processing_unavailable`, throwing away 8 good pages
(reproduced: attempts 1-6 each deferred with pages 1-8 recorded). Now that
page's second rejection is final and the stage completes.

WHEN OCR CAN NEVER COME. With OCR switched off on the deployment
(`OCR_ENABLED=false`, which makes every `read_images` call fail) the stage
does not run at all: every thin page keeps its text layer and is counted in
`ocr_skipped_pages` (`ocr_disabled` recorded). And on the blob's LAST attempt
(`final_attempt`), an engine that is still unreachable no longer fails the
file: the stage completes with what was read, the rest counted in
`ocr_failed_pages`.

WHY AN OCR OUTAGE STILL SPENDS AN ATTEMPT (2026-09-14). An embedding outage no
longer does (`apifiles/outage.py`): a file cannot be indexed without that
engine, so it waits for as long as the outage lasts. A PDF can be finished
without OCR, so here the count is the bound on how long a PDF waits for a
missing OCR engine (~25 minutes), and the last attempt completes the file
with its text layers instead of failing it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncContextManager, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from .extractors import PAGES_NAME, THIN_PAGES_NAME, AtomicTextWriter, atomic_write_json

log = logging.getLogger(__name__)

OCR_LOG_NAME = "ocr.jsonl"
SKIPPED_NAME = "ocr_skipped.json"
OCR_PROMPT = "OCR"
#: Pages rendered per child. Measured 2026-09-13 (render.py docstring): a
#: batch of 8 costs 62 ms/page against 135 ms/page for a child per page, and
#: eight A4 renders are ~2 MB on disk at once (252-265 KB each as PNG).
RENDER_BATCH = 8

#: One OCR read: a list of page image data URLs → `engines.ocr.OcrRead`s.
Reader = Callable[[Sequence[str]], Awaitable[Sequence[Any]]]
#: Opens the capacity gate for one unit.
Gate = Callable[[], AsyncContextManager[None]]
#: Renders pages to PNG; {page: path}.
Renderer = Callable[[Sequence[int]], Awaitable[Dict[int, str]]]


#: Rejections a page gets before its failure is final (module docstring).
MAX_PAGE_ATTEMPTS = 2

#: Exception class names (the `OcrRead.error` prefix `engines.ocr` writes)
#: that mean the ENGINE was unavailable rather than this page being refused.
_UNREACHABLE_ERRORS = frozenset({
    "APIConnectionError", "APITimeoutError", "InternalServerError", "RateLimitError", "NotFoundError",
    "AuthenticationError", "PermissionDeniedError", "ConnectError", "ConnectTimeout", "ReadTimeout",
    "ReadError", "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError", "TimeoutError",
    "ConnectionError", "ConnectionRefusedError", "ConnectionResetError", "CancelledError",
})
_UNREACHABLE_PHRASES = ("the OCR batch deadline", "the OCR batch ended", "OCR is not enabled")


class EngineUnavailable(Exception):
    """OCR could not be reached; defer the blob, keep what was recorded."""


def failure_kind(result: Any) -> str:
    """`unreachable` (not the page's fault) or `rejected` for a failed read.

    Only a reason that NAMES an outage is `unreachable`. `engines.ocr` always
    writes a reason for a failure, so a failed read without one is treated as
    a rejection: it costs that page one of its MAX_PAGE_ATTEMPTS, which keeps
    even an unexplained failure from deferring the whole PDF forever."""
    error = str(getattr(result, "error", "") or "")
    if any(phrase in error for phrase in _UNREACHABLE_PHRASES):
        return "unreachable"
    if error.split(":", 1)[0].strip() in _UNREACHABLE_ERRORS:
        return "unreachable"
    return "rejected"


def default_enabled() -> bool:
    try:
        from ..config import settings

        return bool(getattr(settings, "ocr_enabled", True))
    except Exception:  # noqa: BLE001
        return True


@dataclass(frozen=True)
class OcrPlan:
    pages: List[int]
    skipped: List[int]
    budget: int


@dataclass
class OcrOutcome:
    recorded: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    failed: List[int] = field(default_factory=list)


def plan(thin_pages: Sequence[int], *, budget: int, already: Sequence[int] = ()) -> OcrPlan:
    """Thin pages in page order up to `budget`; pages already recorded by an
    earlier attempt count toward the budget but are not read again."""
    ordered = sorted({int(p) for p in thin_pages if int(p) >= 1})
    budget = max(0, int(budget))
    within, beyond = ordered[:budget], ordered[budget:]
    done = {int(p) for p in already}
    return OcrPlan(pages=[p for p in within if p not in done], skipped=beyond, budget=budget)


def default_gate(abandon: Optional[asyncio.Event] = None) -> AsyncContextManager[None]:
    """The patient OCR gate (module docstring, THE READ). `abandon` ends the
    wait with `capacity.Abandoned`."""
    from ..publicapi import capacity

    return capacity.hold(capacity.GATE_OCR, wait_s=None, yield_to_chat=True, abandon=abandon)


async def default_reader(images: Sequence[str]) -> Sequence[Any]:
    from ..engines import ocr

    return await ocr.read_images(list(images), prompt=OCR_PROMPT)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def is_final(record: Mapping[str, Any]) -> bool:
    """A record that ends the page's OCR: a read (`ok`/`empty`/`degenerate`),
    or a failure that used up its attempts."""
    if record.get("status") != "failed":
        return True
    try:
        return int(record.get("attempts") or 0) >= MAX_PAGE_ATTEMPTS
    except (TypeError, ValueError):
        return True


def load_recorded(derived_dir: str) -> Dict[int, Dict[str, Any]]:
    """Pages recorded by earlier attempts, the LAST record per page winning
    (a page's second rejection follows its first). A torn last line (a crash
    while appending) is ignored: that page is simply read again."""
    out: Dict[int, Dict[str, Any]] = {}
    try:
        with open(os.path.join(derived_dir, OCR_LOG_NAME), "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                    out[int(record["page"])] = record
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return out


def repair_log(derived_dir: str) -> None:
    """Cut a torn final line (a crash mid-append) off `ocr.jsonl` before this
    attempt appends to it. Without this the next record is written onto the
    torn line's end and BOTH are unreadable — found by the resume test: the
    page appended after the tear was read again on every later attempt."""
    path = os.path.join(derived_dir, OCR_LOG_NAME)
    try:
        with open(path, "r+b") as fh:
            data = fh.read()
            if not data or data.endswith(b"\n"):
                return
            fh.truncate(data.rfind(b"\n") + 1)
            fh.flush()
            os.fsync(fh.fileno())
    except FileNotFoundError:
        return


def _append_record(derived_dir: str, record: Dict[str, Any]) -> None:
    with open(os.path.join(derived_dir, OCR_LOG_NAME), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _data_url(path: str) -> str:
    with open(path, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")


def merge(derived_dir: str, recorded: Dict[int, Dict[str, Any]]) -> int:
    """Fold `ok` OCR text into pages.jsonl (atomic rewrite); pages changed."""
    changed = 0
    source = os.path.join(derived_dir, PAGES_NAME)
    out = AtomicTextWriter(source)
    try:
        with open(source, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                record = json.loads(line)
                result = recorded.get(int(record.get("page") or 0))
                # `source: "ocr"` is only ever written here, so a page carrying
                # it was merged by an attempt that crashed before its stage
                # marker: merging again would append the transcript twice.
                if record.get("source") == "ocr":
                    changed += 1
                elif result and result.get("status") == "ok" and result.get("text"):
                    layer = str(record.get("text") or "").strip()
                    text = str(result["text"]).strip()
                    record["text"] = f"{layer}\n{text}" if layer else text
                    record["source"] = "ocr"
                    changed += 1
                out.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        out.commit()
    except BaseException:
        out.abort()
        raise
    return changed


async def run_stage(
    derived_dir: str,
    *,
    budget: int,
    render: Renderer,
    read: Optional[Reader] = None,
    gate: Optional[Gate] = None,
    concurrency: int = 2,
    checkpoint: Optional[Callable[[], Awaitable[None]]] = None,
    progress: Optional[Callable[[int], Awaitable[None]]] = None,
    final_attempt: bool = False,
    enabled: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Read the thin pages and merge; returns the stage's facts.

    `checkpoint` is the runner's unit boundary (raises to stop: blob being
    deleted, lease lost, shutdown); `progress` gets a 0-100 percent.
    `final_attempt`: the blob cannot be deferred again, so an unavailable
    engine ends the stage with what was read instead of raising.
    `enabled` defaults to the deployment's OCR switch when `read` is the
    default reader (an injected reader is its own engine)."""
    if enabled is None:
        enabled = default_enabled if read is None else (lambda: True)
    read = read or default_reader
    gate = gate or default_gate
    thin = _read_json(os.path.join(derived_dir, THIN_PAGES_NAME), [])
    await asyncio.to_thread(repair_log, derived_dir)
    log_records = await asyncio.to_thread(load_recorded, derived_dir)
    recorded = {page: rec for page, rec in log_records.items() if is_final(rec)}
    attempts = {
        page: int(rec.get("attempts") or 0) for page, rec in log_records.items() if rec.get("status") == "failed"
    }
    the_plan = plan(thin, budget=budget, already=list(recorded))
    if not enabled():
        # OCR can never come on this deployment: keep every text layer, skip
        # the stage, and still fold in what an earlier attempt already read.
        unread = sorted(set(the_plan.pages) | set(the_plan.skipped))
        await asyncio.to_thread(atomic_write_json, os.path.join(derived_dir, SKIPPED_NAME), unread)
        changed = await asyncio.to_thread(merge, derived_dir, recorded)
        return _facts(changed, recorded, skipped=len(unread), failed=set(), disabled=True)
    await asyncio.to_thread(atomic_write_json, os.path.join(derived_dir, SKIPPED_NAME), the_plan.skipped)
    total = len(the_plan.pages) + sum(1 for p in recorded if p not in set(the_plan.skipped))
    outcome = OcrOutcome(recorded=dict(recorded))
    workers = max(1, int(concurrency))

    async def report() -> None:
        if progress is not None and total:
            await progress(min(100, int(100 * (len(outcome.recorded)) / max(1, total))))

    for start in range(0, len(the_plan.pages), RENDER_BATCH):
        batch = the_plan.pages[start:start + RENDER_BATCH]
        if checkpoint is not None:
            await checkpoint()
        renders = await render(batch)
        queue: "asyncio.Queue[int]" = asyncio.Queue()
        for page in batch:
            if page in renders:
                queue.put_nowait(page)
            else:
                outcome.failed.append(page)  # the child could not render it
        unreachable: List[int] = []
        retry_rejected: List[int] = []
        final_rejected: List[int] = []
        successes: List[int] = []

        async def worker() -> None:
            from ..publicapi.errors import ApiError

            while True:
                try:
                    page = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if checkpoint is not None:
                    await checkpoint()
                path = renders[page]
                image = await asyncio.to_thread(_data_url, path)
                try:
                    async with gate():
                        reads = await read([image])
                except ApiError as exc:
                    # A bounded gate's 503 at capacity (an injected gate; the
                    # default one waits): not a verdict about the page, and not
                    # something to retry in this attempt.
                    raise EngineUnavailable("the OCR gate did not admit this unit") from exc
                result = reads[0] if reads else None
                status = str(getattr(result, "status", "failed") or "failed")
                if status == "failed":
                    if failure_kind(result) == "unreachable":
                        unreachable.append(page)
                        continue
                    count = attempts.get(page, 0) + 1
                    attempts[page] = count
                    record = {"page": page, "status": "failed", "attempts": count}
                    await asyncio.to_thread(_append_record, derived_dir, record)
                    if count >= MAX_PAGE_ATTEMPTS:
                        outcome.recorded[page] = record
                        final_rejected.append(page)
                        await report()
                    else:
                        retry_rejected.append(page)
                    continue
                record = {
                    "page": page,
                    "status": status,
                    # A degenerate loop is kept OUT of the record's text: it is
                    # never merged, and it must never reach a model later.
                    "text": str(getattr(result, "text", "") or "") if status == "ok" else "",
                }
                await asyncio.to_thread(_append_record, derived_dir, record)
                outcome.recorded[page] = record
                successes.append(page)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                await report()

        tasks = [asyncio.ensure_future(worker()) for _ in range(min(workers, max(1, queue.qsize())))]
        gate_refused = False
        try:
            await asyncio.gather(*tasks)
        except EngineUnavailable:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            _discard_renders(renders.values())
            if not final_attempt:
                raise
            gate_refused = True
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            _discard_renders(renders.values())
            raise
        if gate_refused:
            # The last attempt and the gate still refuses: stop reading, keep
            # every unread page's text layer, count them as not read.
            done = set(outcome.recorded)
            outcome.failed.extend(p for p in the_plan.pages[start:] if p not in done)
            break
        _discard_renders(renders.values())
        retryable = unreachable + retry_rejected
        if retryable and not successes and not final_attempt:
            raise EngineUnavailable(f"no OCR read in a batch of {len(batch)} page(s) succeeded")
        outcome.failed.extend(retryable + final_rejected)

    changed = await asyncio.to_thread(merge, derived_dir, outcome.recorded)
    return _facts(changed, outcome.recorded, skipped=len(the_plan.skipped), failed=set(outcome.failed))


def _facts(
    changed: int, recorded: Mapping[int, Mapping[str, Any]], *, skipped: int, failed: set, disabled: bool = False
) -> Dict[str, Any]:
    statuses = [r.get("status") for r in recorded.values()]
    failed_pages = set(failed) | {int(p) for p, r in recorded.items() if r.get("status") == "failed"}
    facts: Dict[str, Any] = {
        "ocr_pages": changed,
        "ocr_skipped_pages": int(skipped),
        "ocr_degenerate_pages": statuses.count("degenerate"),
        "ocr_empty_pages": statuses.count("empty"),
        "ocr_failed_pages": len(failed_pages),
    }
    if disabled:
        facts["ocr_disabled"] = True
    return facts


def _discard_renders(paths) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
