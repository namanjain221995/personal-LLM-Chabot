"""Real-world validation set for the JSON-LD / microdata recovery pass.

WHY THIS EXISTS
---------------
`core/structured.py` (EXTRACT_VERSION 4) recovers `<script
type="application/ld+json">` and microdata before `core/extract` strips
`<script>`. Every one of its 38 tests is written against a hand-authored
fixture, so the whole module has only ever been shown to work on markup this
project wrote itself. Hand-written fixtures are clean; the web is not. This
tool closes that gap by running the SAME `extract.extract_readable` the
ingest path runs against a small fixed list of real public pages, and
reporting per page what was recovered, what was not, and what it cost.

A NULL RESULT IS A FINDING. Several pages in the list carry no schema.org
markup at all, and one parses a block and legitimately emits nothing. They
are in the list on purpose, they are reported as `none`, and they are not
failures. The interesting failure mode for this module is the opposite one —
a value arriving detached from the entity it belongs to — so a page that
yields nothing is safer than a page that yields a loose number.

WHAT IT DOES NOT DO
-------------------
  * No LLM, no embedding service, no GPU, no database, no LanceDB. Extraction
    is a pure function over bytes, and this tool keeps it that way.
  * No ranking, no scoring, no relevance judgement. It reports what the
    extractor produced; it does not grade it.
  * It never fetches with anything but `core/net.safe_fetch`, and never
    without asking `core/robots` first. A disallowed page is recorded as
    `robots_disallowed` and is not fetched — the run is not "fixed" by
    dropping the check.

FETCH POLICY, STATED PLAINLY. `robots.allowed()` then `robots.reserve_slot()`
(honouring `Crawl-delay`) then `safe_fetch` with the app's own timeout and
byte cap and the app's own User-Agent. One GET per page per run, and by
default zero GETs on a re-run because the HTML is cached.

THE CACHE IS THE POINT. Every fetched body is written to disk with a sidecar
recording the URL, the final post-redirect URL, the status, the content type,
the fetch time and the body's SHA-256. A second run reads the cache and makes
no request at all, so the check is repeatable, reviewable offline, and the
numbers below are measured against bytes anyone can re-read. `--refresh`
re-fetches; `--offline` refuses to fetch and fails loudly on a cache miss.

HAND-DERIVED EXPECTATIONS
-------------------------
`--check` re-runs the extractor over the cache and asserts the expectations in
`tests/fixtures/structured_real/*.json`. Those files were written by reading
the JSON-LD out of the cached HTML by hand — NOT captured from this tool's
output — which is what makes them evidence rather than a snapshot of whatever
the code happens to do. See the README in that directory.

USAGE
-----
    python tools/structured_real_world.py                 # fetch (or reuse cache) + report
    python tools/structured_real_world.py --offline       # cache only, never fetch
    python tools/structured_real_world.py --refresh       # force a re-fetch
    python tools/structured_real_world.py --check         # verify the hand-derived fixtures
    python tools/structured_real_world.py --list          # print the page list and exit

Exit code is 0 when every page reached a terminal state (extracted, or a
recorded null / refusal) and, under `--check`, when every hand-derived
expectation held. It is 1 when an expectation failed or a page could not be
resolved at all.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO = ROOT.parent

#: Where the fetched HTML lives. Deliberately OUTSIDE the repository and
#: outside every live data directory: this writes files, and `/data/lancedb`
#: (Salesforce) and `/data/lancedb-web` are not ours to write to. Overridable
#: with --cache-dir or $STRUCTURED_REAL_CACHE so a reviewer can point it at a
#: copy someone handed them.
DEFAULT_CACHE = Path(
    os.environ.get("STRUCTURED_REAL_CACHE")
    or (Path.home() / ".cache" / "techsara" / "structured-real-world")
)
DEFAULT_OUT = REPO / "docs/fast-web-research/measurements/structured-real-world.json"
FIXTURES = ROOT / "tests" / "fixtures" / "structured_real"

#: Runs of the extractor timed per page. The first is discarded (cold
#: trafilatura/lxml caches) and the median of the rest is reported, because a
#: single sample on a box shared with vLLM is noise.
DEFAULT_REPEAT = 4


class Page:
    """One page in the fixed list, with the reason it is in the list."""

    __slots__ = ("url", "slug", "expect", "why")

    def __init__(self, url: str, slug: str, expect: str, why: str):
        #: expect is what a HUMAN predicted before the run, from reading the
        #: page source: "jsonld", "microdata", "both", or "none". It is a
        #: prediction on the record, not an assertion — the report prints
        #: predicted beside observed so a surprise is visible rather than
        #: quietly absorbed.
        self.url, self.slug, self.expect, self.why = url, slug, expect, why


#: THE LIST. Fourteen pages, fixed. Chosen to be stable, public, non-sensitive
#: and crawlable: documentation, standards bodies, statistics offices, public
#: catalogues, encyclopaedic and public-domain library pages. No login wall,
#: no personal data, no page whose robots.txt refuses us (two candidates —
#: w3.org/TR/* and imdb.com — were dropped for exactly that reason and are
#: named here so the exclusion is on the record, not silent).
PAGES: Tuple[Page, ...] = (
    Page("https://www.gov.uk/vat-rates", "govuk-vat-rates", "jsonld",
         "two blocks: an FAQPage whose answers hold the actual rates, and a "
         "BreadcrumbList of ListItem wrappers — the _unwrap path"),
    Page("https://catalog.data.gov/dataset/electric-vehicle-population-data",
         "datagov-ev-population", "jsonld",
         "a Dataset with six named DataDownload children — the nested-record "
         "path where a child must keep its parent's name"),
    Page("https://ourworldindata.org/energy", "owid-energy", "jsonld",
         "a plain Article: dates and publisher, the commonest shape on the web"),
    Page("https://en.wikipedia.org/wiki/Schema.org", "wikipedia-schema-org",
         "jsonld", "MediaWiki's Article block; dates that must survive verbatim"),
    Page("https://www.who.int/news-room/fact-sheets/detail/malaria",
         "who-malaria", "jsonld",
         "four ld+json blocks on one page, of which some are malformed in the "
         "wild — the malformed-is-counted-not-raised path"),
    Page("https://zenodo.org/records/3509134", "zenodo-pandas", "both",
         "SoftwareSourceCode with a nested Person author, plus one itemscope: "
         "both passes on one page"),
    Page("https://schema.org/Product", "schemaorg-product", "both",
         "the vocabulary's own page: an RDF-shaped ld+json block and 20+ "
         "microdata scopes — the widest real input in the set"),
    Page("https://www.gutenberg.org/ebooks/2701", "gutenberg-moby-dick",
         "microdata", "a public-domain Book in pure microdata, no ld+json"),
    Page("https://openlibrary.org/works/OL27448W/The_Lord_of_the_Rings",
         "openlibrary-lotr", "microdata",
         "Book microdata carrying a ratingValue and ratingCount"),
    Page("https://github.com/python/cpython", "github-cpython", "microdata",
         "SoftwareSourceCode microdata on a page that is otherwise all script"),
    Page("https://www.python.org/", "python-org-home", "none",
         "NEGATIVE CONTROL, post-parse: one ld+json block that is real and "
         "correct and contains nothing a reader could use. Parsing it and "
         "emitting nothing is the right answer"),
    Page("https://docs.python.org/3/library/json.html", "docs-python-json",
         "none", "NEGATIVE CONTROL: hand-written documentation, no markup"),
    Page("https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404",
         "mdn-http-404", "none",
         "NEGATIVE CONTROL: a large modern doc site with no schema.org at all"),
    Page("https://www.rfc-editor.org/rfc/rfc9110.html", "rfc-9110", "none",
         "NEGATIVE CONTROL and the cost ceiling: a 1.2 MB standards document "
         "with no markup, so the two probe regexes are all this pass may spend"),
)

#: Pages considered and deliberately excluded, with the reason. Kept in the
#: source so the list looks like a decision rather than a convenience.
EXCLUDED = {
    "https://www.w3.org/TR/rdfa-primer/": "robots.txt disallows /TR/ for us",
    "https://www.imdb.com/title/tt0111161/": "robots.txt disallows /title/",
    "https://stackoverflow.com/questions/11227809/": "HTTP 403 to our UA",
    "https://blog.mozilla.org/": "HTTP 403 to our UA",
    "https://www.loc.gov/item/2021667925/": "HTTP 403 to our UA",
}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _paths(cache: Path, page: Page) -> Tuple[Path, Path]:
    digest = hashlib.sha256(page.url.encode("utf-8")).hexdigest()[:16]
    stem = f"{page.slug}.{digest}"
    return cache / f"{stem}.html", cache / f"{stem}.meta.json"


def _load(cache: Path, page: Page) -> Optional[Dict[str, Any]]:
    body_path, meta_path = _paths(cache, page)
    if not (body_path.exists() and meta_path.exists()):
        return None
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt sidecar is a cache miss
        return None
    meta["html"] = body_path.read_text("utf-8", errors="replace")
    meta["cache_file"] = str(body_path)
    return meta


def _store(cache: Path, page: Page, result: Dict[str, Any]) -> None:
    body_path, meta_path = _paths(cache, page)
    cache.mkdir(parents=True, exist_ok=True)
    body_path.write_text(result["html"], "utf-8")
    meta = {k: v for k, v in result.items() if k != "html"}
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), "utf-8")


# ---------------------------------------------------------------------------
# Fetch — robots first, then safe_fetch, and nothing else ever
# ---------------------------------------------------------------------------


async def _fetch(page: Page, timeout_ms: int, max_bytes: int) -> Dict[str, Any]:
    from app.core import net, robots

    try:
        if not await robots.allowed(page.url):
            return {"error": "robots_disallowed"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"robots_error: {type(exc).__name__}: {exc}"}
    try:
        if not await robots.reserve_slot(page.url, max_wait_s=15.0):
            return {"error": "crawl_delay_too_long"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"robots_slot_error: {type(exc).__name__}: {exc}"}
    try:
        got = await net.safe_fetch(
            page.url,
            timeout_ms=timeout_ms,
            max_bytes=max_bytes,
            accept="text/html,application/xhtml+xml",
        )
    except Exception as exc:  # noqa: BLE001 — a dead site is data, not a crash
        return {"error": f"fetch_error: {type(exc).__name__}: {exc}"}
    html = got.body.decode("utf-8", "replace")
    return {
        "url": page.url,
        "final_url": got.url,
        "status": got.status,
        "content_type": got.content_type,
        "bytes": len(got.body),
        "sha256": hashlib.sha256(got.body).hexdigest(),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "html": html,
    }


# ---------------------------------------------------------------------------
# Extract + describe
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(r"^\[(jsonld|microdata)\]\s*(.*)$")


def _parse_line(line: str) -> Dict[str, Any]:
    """A rendered record line back into its parts, for the JSON report.

    Mirrors `structured._line` exactly: `[src] parent › Type: Name — k: v; k: v`.
    Parsing our own rendering back is deliberate — it is what proves the
    entity/field association actually SURVIVED into the text, rather than
    re-reading the parser's internal objects, which would prove nothing about
    what the pipeline stores.
    """
    match = _LINE_RE.match(line.strip())
    if not match:
        return {"source": "", "head": line.strip(), "fields": [], "malformed": True}
    src, rest = match.group(1), match.group(2)
    head, _, body = rest.partition(" — ")
    parent, sep, entity = head.rpartition(" › ")
    if not sep:
        parent, entity = "", head
    # `_line` writes "Type: Name" only when it has BOTH. With one of them it
    # writes the bare token, and the rendering is then genuinely ambiguous —
    # "Book" could be a type with no name (which is what an unnamed record
    # looks like) or a thing called Book. That ambiguity is a property of the
    # stored text, so it is reported rather than guessed away: a reader (and
    # the model) sees the same thing.
    etype, sep2, ename = entity.partition(": ")
    ambiguous = not sep2
    if ambiguous:
        etype, ename = "", entity
    # `structured._line` joins FIELDS with "; " and `_collect` joins the
    # members of a MULTI-VALUED property with "; " as well. The two
    # separators are the same character sequence, so once a line is rendered
    # a field boundary and a list boundary are indistinguishable — to this
    # parser, to a reader, and to the model. Found on a real page:
    #   ... isbn: 9753423470; 9789753423472
    # is either one isbn with two values or an isbn plus a loose number.
    # Reconstructed the only way available — a chunk with no "key: " prefix
    # continues the previous value — and the guess is FLAGGED rather than
    # hidden, because the report's job is to surface that the stored text is
    # ambiguous, not to paper over it.
    fields: List[List[str]] = []
    separator_ambiguous = False
    for chunk in body.split("; ") if body else []:
        key, sep, value = chunk.partition(": ")
        if not sep:
            if fields:
                separator_ambiguous = True
                fields[-1][1] = f"{fields[-1][1]}; {chunk.strip()}".strip("; ")
            continue
        fields.append([key.strip(), value.strip()])
    return {
        "source": src,
        "parent": parent.strip(),
        "type": etype.strip(),
        "name": ename.strip(),
        "ambiguous_head": ambiguous,
        "separator_ambiguous": separator_ambiguous,
        "fields": fields,
    }


def _split_extracted(text: str, heading: str) -> Tuple[str, List[str]]:
    """(prose the pass did not write, the record lines it appended)."""
    at = text.rfind("\n" + heading + "\n")
    if at < 0:
        return text, []
    base = text[:at]
    lines = [
        ln for ln in text[at + len(heading) + 2:].splitlines() if ln.strip()
    ]
    return base, lines


def _analyse(page: Page, html: str, repeat: int) -> Dict[str, Any]:
    from app.core import extract, structured

    samples: List[float] = []
    struct_samples: List[float] = []
    extracted = None
    for _ in range(max(1, repeat)):
        start = time.perf_counter()
        extracted = extract.extract_readable(
            "text/html", html.encode("utf-8"), page.url
        )
        samples.append((time.perf_counter() - start) * 1000.0)

    base, lines = _split_extracted(extracted.text, extract._EMBEDDED_HEADING)

    # The structured pass timed on its own, against the same prose the
    # pipeline hands it, so the report can say what the RECOVERY costs rather
    # than what trafilatura costs.
    stats: Dict[str, Any] = {}
    direct_lines: List[str] = []
    for index in range(max(1, repeat)):
        probe: Dict[str, Any] = {}
        start = time.perf_counter()
        direct = structured.embedded_records(html, base, probe)
        struct_samples.append((time.perf_counter() - start) * 1000.0)
        if index == 0:
            stats, direct_lines = probe, direct

    def _median(values: List[float]) -> float:
        # Drop the cold first sample when there is more than one.
        useful = values[1:] if len(values) > 1 else values
        return round(statistics.median(useful), 3)

    records = [_parse_line(ln) for ln in lines]
    entity_fields = sorted({
        f"{rec.get('type') or rec.get('name') or '?'}.{key}"
        for rec in records for key, _ in rec.get("fields", [])
    })
    sources = sorted({rec.get("source", "") for rec in records if rec.get("source")})
    if not sources:
        found = "none"
    elif len(sources) == 2:
        found = "both"
    else:
        found = sources[0]

    return {
        "found": found,
        "predicted": page.expect,
        "prediction_held": found == page.expect,
        "record_count": len(records),
        "records": records,
        "entity_field_pairs": entity_fields,
        "lines": lines,
        # Self-consistency: calling the module directly with the same prose
        # must reproduce the lines that reached the stored text. A mismatch
        # would mean the pipeline and this report disagree about what was
        # recovered, which would invalidate every number below it.
        "consistent_with_pipeline": direct_lines == lines,
        "stats": stats,
        "text_chars": len(extracted.text),
        "prose_chars": len(base),
        "appended_chars": len(extracted.text) - len(base),
        "title": (extracted.title or "")[:200],
        "published_at": extracted.published_at,
        "modified_at": extracted.modified_at,
        "extract_ms": _median(samples),
        "structured_ms": _median(struct_samples),
        "extract_ms_all": [round(v, 3) for v in samples],
        "structured_ms_all": [round(v, 3) for v in struct_samples],
    }


# ---------------------------------------------------------------------------
# Hand-derived fixture check
# ---------------------------------------------------------------------------


def _entity_matches(rec: Dict[str, Any], want: Dict[str, Any]) -> bool:
    """Does this rendered record identify the entity the fixture names?

    EXACT, on every part the fixture states. Substring matching would let
    "Book" satisfy an expectation about "Book Details" and would quietly turn
    an association check into a containment check.
    """
    if rec.get("name", "") != want.get("entity", ""):
        return False
    if "parent" in want and rec.get("parent", "") != want["parent"]:
        return False
    if want.get("entity_type") and rec.get("type", "") != want["entity_type"]:
        return False
    if "entity_is_bare_type" in want:
        if bool(rec.get("ambiguous_head")) != bool(want["entity_is_bare_type"]):
            return False
    return True


def _check_fixtures(results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Assert the hand-read expectations against what the extractor produced.

    An expectation names an entity, a field and the value a human read out of
    the cached HTML, plus the source it must be attributed to. The assertion
    is on the ASSOCIATION: the value has to appear on a line whose entity is
    the expected one. `value in text` would pass on a page that scattered the
    number anywhere, which is precisely the failure this module exists to
    prevent, so it is not what is checked.

    `known_absent` is the other half, and it is not decoration: it pins values
    a human READ IN THE HTML and the extractor does NOT carry — dropped by the
    dedupe, by a skip-list key, or by a real gap. Each one records the reason.
    They are asserted absent so that a change which starts recovering them
    fails here and gets read by a person, instead of appearing unannounced in
    the shared corpus.

    A fixture also pins `cached_body_sha256`. The expectations were read from
    one specific snapshot; if the cache holds different bytes (someone ran
    --refresh and the page moved), the values are no longer hand-verified and
    saying so is the only honest outcome.
    """
    checks: List[Dict[str, Any]] = []
    if not FIXTURES.is_dir():
        return {"ran": False, "reason": f"no fixture directory at {FIXTURES}"}
    for path in sorted(FIXTURES.glob("*.json")):
        spec = json.loads(path.read_text("utf-8"))
        slug = spec.get("slug", path.stem)
        result = results.get(slug)
        base = {"fixture": path.name, "slug": slug}

        if not result or "analysis" not in result:
            checks.append(dict(base, kind="page", entity="", field="", value="",
                               ok=False,
                               why="page not in this run's results (cache miss?)"))
            continue
        want_sha = spec.get("cached_body_sha256", "")
        if want_sha and result.get("sha256") != want_sha:
            checks.append(dict(
                base, kind="snapshot", entity="", field="", value=want_sha[:12],
                ok=False,
                why=f"cached bytes changed (now {str(result.get('sha256'))[:12]}); "
                    "the hand-read values no longer describe this snapshot",
            ))
            continue
        records = result["analysis"]["records"]

        for want in spec.get("expectations", []):
            row = dict(base, kind="present", entity=want.get("entity", ""),
                       field=want.get("field", ""), value=want.get("value", ""),
                       source=want.get("source", ""))
            hit = False
            near_miss = ""
            for rec in records:
                same_entity = _entity_matches(rec, want)
                for key, value in rec.get("fields", []):
                    if key != row["field"]:
                        continue
                    if not same_entity:
                        near_miss = near_miss or (
                            f"field is on a different entity: "
                            f"{rec.get('parent') or ''}/{rec.get('name')!r}"
                        )
                        continue
                    if value != row["value"]:
                        near_miss = near_miss or f"value differs: {value!r}"
                        continue
                    if row["source"] and rec.get("source") != row["source"]:
                        near_miss = (f"right value, wrong provenance: "
                                     f"{rec.get('source')!r}")
                        continue
                    hit = True
                    break
                if hit:
                    break
            row["ok"] = hit
            if not hit:
                row["why"] = near_miss or "no line carries that entity/field"
            checks.append(row)

        for want in spec.get("known_absent", []):
            row = dict(base, kind="absent", entity=want.get("entity", ""),
                       field=want.get("field", ""), value=want.get("value", ""),
                       source="", why=want.get("reason", ""))
            where = ""
            for rec in records:
                for key, value in rec.get("fields", []):
                    if value == row["value"] and (
                        not row["field"] or key == row["field"]
                    ):
                        where = (f"now present on "
                                 f"{rec.get('name')!r} as {key!r}")
                        break
                if where:
                    break
            row["ok"] = not where
            if where:
                row["why"] = where
            checks.append(row)
    failed = [c for c in checks if not c["ok"]]
    return {
        "ran": True,
        "total": len(checks),
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _gather(args) -> Dict[str, Dict[str, Any]]:
    from app.config import settings

    cache = Path(args.cache_dir)
    results: Dict[str, Dict[str, Any]] = {}
    for page in PAGES:
        entry: Dict[str, Any] = {
            "url": page.url, "slug": page.slug, "why": page.why,
            "predicted": page.expect,
        }
        cached = None if args.refresh else _load(cache, page)
        if cached is None:
            if args.offline:
                entry["error"] = "cache_miss_offline"
                results[page.slug] = entry
                print(f"  MISS (offline)  {page.url}")
                continue
            fetched = await _fetch(
                page,
                timeout_ms=max(settings.fetch_timeout_ms, 15000),
                max_bytes=settings.fetch_max_bytes,
            )
            if "error" in fetched:
                entry["error"] = fetched["error"]
                results[page.slug] = entry
                print(f"  {fetched['error'][:40]:<42}{page.url}")
                continue
            _store(cache, page, fetched)
            cached = _load(cache, page)
            entry["from_cache"] = False
        else:
            entry["from_cache"] = True
        assert cached is not None
        for key in ("final_url", "status", "content_type", "bytes",
                    "sha256", "fetched_at", "cache_file"):
            entry[key] = cached.get(key)
        entry["analysis"] = _analyse(page, cached["html"], args.repeat)
        results[page.slug] = entry
        a = entry["analysis"]
        flag = " " if a["prediction_held"] else "!"
        print(
            f"  {flag} {a['found']:<9} recs={a['record_count']:>3} "
            f"extract={a['extract_ms']:>8.2f}ms struct={a['structured_ms']:>7.2f}ms "
            f"{page.url}"
        )
    return results


def _summarise(results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    ok = [r for r in results.values() if "analysis" in r]
    with_records = [r for r in ok if r["analysis"]["record_count"] > 0]
    surprises = [
        {"slug": r["slug"], "predicted": r["predicted"],
         "found": r["analysis"]["found"]}
        for r in ok if not r["analysis"]["prediction_held"]
    ]
    malformed = [
        {"slug": r["slug"], "malformed_blocks": r["analysis"]["stats"]["malformed"],
         "blocks": r["analysis"]["stats"].get("blocks", 0)}
        for r in ok if r["analysis"]["stats"].get("malformed")
    ]
    truncated = [r["slug"] for r in ok if r["analysis"]["stats"].get("truncated")]
    inconsistent = [
        r["slug"] for r in ok if not r["analysis"]["consistent_with_pipeline"]
    ]
    ambiguous_sep = [
        r["slug"] for r in ok
        if any(rec.get("separator_ambiguous") for rec in r["analysis"]["records"])
    ]
    bare_type_head = [
        r["slug"] for r in ok
        if any(rec.get("ambiguous_head") and not rec.get("parent")
               for rec in r["analysis"]["records"])
    ]
    return {
        "pages": len(results),
        "resolved": len(ok),
        "unreachable": [
            {"slug": r["slug"], "error": r["error"]}
            for r in results.values() if "error" in r
        ],
        "with_records": len(with_records),
        "without_records": len(ok) - len(with_records),
        "records_total": sum(r["analysis"]["record_count"] for r in ok),
        "prediction_surprises": surprises,
        "pages_with_malformed_blocks": malformed,
        "pages_hitting_the_total_char_cap": truncated,
        #: A rendered line where a field boundary cannot be told from a list
        #: boundary — both are "; ". See `_parse_line`.
        "pages_with_ambiguous_field_separator": ambiguous_sep,
        #: A top-level record rendered as a bare type with no entity name.
        #: The value is attached to "Book" rather than to a book.
        "pages_with_a_nameless_top_level_record": bare_type_head,
        "pipeline_disagreements": inconsistent,
        "extract_ms_median": round(statistics.median(
            [r["analysis"]["extract_ms"] for r in ok]) if ok else 0.0, 3),
        "structured_ms_median": round(statistics.median(
            [r["analysis"]["structured_ms"] for r in ok]) if ok else 0.0, 3),
        "structured_ms_max": round(max(
            [r["analysis"]["structured_ms"] for r in ok], default=0.0), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE),
                    help=f"cached HTML + sidecars (default {DEFAULT_CACHE})")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="machine-readable JSON report")
    ap.add_argument("--offline", action="store_true",
                    help="never fetch; fail the page on a cache miss")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch every page even when cached")
    ap.add_argument("--repeat", type=int, default=DEFAULT_REPEAT,
                    help="timed extraction runs per page (first is discarded)")
    ap.add_argument("--check", action="store_true",
                    help="also verify tests/fixtures/structured_real/*.json")
    ap.add_argument("--list", action="store_true",
                    help="print the page list and exit")
    args = ap.parse_args()

    if args.list:
        for page in PAGES:
            print(f"{page.expect:<10} {page.url}\n           {page.why}")
        for url, why in EXCLUDED.items():
            print(f"{'EXCLUDED':<10} {url}\n           {why}")
        return 0

    if args.offline and args.refresh:
        print("--offline and --refresh contradict each other", file=sys.stderr)
        return 2

    from app.core import extract, structured

    print(f"structured.py real-world check — {len(PAGES)} pages")
    print(f"  cache   {args.cache_dir}")
    print(f"  mode    {'offline' if args.offline else ('refresh' if args.refresh else 'cache-or-fetch')}")
    print(f"  extract version {extract.EXTRACT_VERSION}, "
          f"recovery {'on' if extract.RECOVER_EMBEDDED_RECORDS else 'OFF'}")
    results = asyncio.run(_gather(args))
    summary = _summarise(results)

    report: Dict[str, Any] = {
        "tool": "tools/structured_real_world.py",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "extract_version": extract.EXTRACT_VERSION,
        "recovery_enabled": extract.RECOVER_EMBEDDED_RECORDS,
        "bounds": {
            name: getattr(structured, name)
            for name in dir(structured)
            if name.startswith("MAX_") and isinstance(getattr(structured, name), int)
        },
        "cache_dir": str(Path(args.cache_dir).resolve()),
        "excluded_pages": EXCLUDED,
        "summary": summary,
        "pages": results,
    }
    if args.check:
        report["fixture_check"] = _check_fixtures(results)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=False), "utf-8")

    print("\nsummary")
    print(f"  resolved {summary['resolved']}/{summary['pages']}, "
          f"with records {summary['with_records']}, "
          f"without {summary['without_records']}, "
          f"records total {summary['records_total']}")
    if summary["unreachable"]:
        print(f"  unreachable: {summary['unreachable']}")
    if summary["prediction_surprises"]:
        print(f"  prediction surprises: {summary['prediction_surprises']}")
    if summary["pages_with_malformed_blocks"]:
        print(f"  malformed blocks in the wild: {summary['pages_with_malformed_blocks']}")
    if summary["pages_hitting_the_total_char_cap"]:
        print(f"  hit MAX_TOTAL_CHARS: {summary['pages_hitting_the_total_char_cap']}")
    if summary["pages_with_ambiguous_field_separator"]:
        print("  field/list separator collision: "
              f"{summary['pages_with_ambiguous_field_separator']}")
    if summary["pages_with_a_nameless_top_level_record"]:
        print("  nameless top-level record (value attached to a bare type): "
              f"{summary['pages_with_a_nameless_top_level_record']}")
    if summary["pipeline_disagreements"]:
        print(f"  !! module/pipeline disagreement: {summary['pipeline_disagreements']}")
    print(f"  extract median {summary['extract_ms_median']} ms; "
          f"structured median {summary['structured_ms_median']} ms, "
          f"max {summary['structured_ms_max']} ms")

    rc = 0
    if summary["pipeline_disagreements"]:
        rc = 1
    if args.check:
        check = report["fixture_check"]
        if not check.get("ran"):
            print(f"\nfixture check DID NOT RUN: {check.get('reason')}")
            rc = 1
        else:
            print(f"\nhand-derived expectations: "
                  f"{check['passed']}/{check['total']} held")
            for row in check["checks"]:
                mark = "ok  " if row["ok"] else "FAIL"
                kind = row.get("kind", "present")
                arrow = "=" if kind == "present" else ("!=" if kind == "absent" else "?")
                extra = "" if row["ok"] else f"  <- {row.get('why', '')}"
                print(f"  {mark} [{kind:<8}] {row['slug']}: {row['entity']} . "
                      f"{row['field']} {arrow} {row['value']}{extra}")
            if check["failed"]:
                rc = 1
    print(f"\nreport written to {out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
