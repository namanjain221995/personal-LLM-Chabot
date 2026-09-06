"""Exclusion invariants: what the public corpus must never serve, and what it
must never hold.

Two safety properties, deliberately in one file because they are the same
promise read from both ends.

WITHHOLDING (parts 1-3). ``web_pages`` is the source of truth; the LanceDB web
index is DERIVED state that can outlive it. A purge deletes the row and (since
K7) the vectors; a quarantine deletes nothing at all and is meant to be
reversible. Neither fact is visible in a LanceDB row, so a retrieval path that
reads the index without asking PostgreSQL will serve both — which is exactly
what ``engines/crawl.site_hits_for`` did: it renders ``web_index.retrieve``
output straight into an answer with no database round trip of its own, so a
quarantined page was still being answered from and a purged one still cited.
``web_index._servable_page_ids`` now filters where the rows come from. These
tests exercise all three public reads over one seeded corpus — the raw index,
the shared-corpus entry point, and the site-Q&A entry point where the leak was
found — and they run at TWO values of the per-page chunk cap, because raising
coverage is exactly the change about to be made and an exclusion invariant that
holds only at today's cap is not an invariant.

The cap parametrisation is proved non-vacuous twice over: the two values
produce a materially different index (6 chunks against 33), and a phrase 25,378
characters into a page is absent at the low cap and present at the high one. If
both caps produced the same index, parts 1 and 2 would be one test run twice
and this file would be claiming something it had not shown.

ADMISSION (part 4). The other end: private text must never get INTO the shared
corpus in the first place, because once it is there every member's next Fast
answer can quote it, and no viewer check exists to stop that — the corpus is
global BY DESIGN. That is proved structurally rather than by example, because a
leak of this kind is an omission at a call site and only reading the call sites
can see an omission. The chain, each link an assertion below:

  1. exactly one SQL statement inserts into ``web_pages`` (db.upsert_web_page),
     and exactly one line in ``app/`` adds rows to the web vector table;
  2. exactly four call sites reach the first, in three fetch modules;
  3. each takes its body from a ``net.safe_fetch`` of a public URL, by a
     hand-derived caller chain pinned here;
  4. no private-source module — conversation memory, uploads, Salesforce —
     can reach any of them, and no store path reads a private accessor;
  5. ``conversation_chunks`` lives in PostgreSQL and never in LanceDB;
  6. the public read takes no viewer and the corpus has no owner column, so
     there is no per-user content in it to leak.

A test that FAILS if someone later wires a private source in: an
``upsert_web_page`` call anywhere else fails (2); moving one behind a different
caller fails (3); storing conversation, upload or CRM text fails (4) and (5);
an ``owner_id``/``visibility`` column on ``web_pages`` fails (6).

Everything here is offline: a deterministic in-process embedder, a fake
cross-encoder, a temporary LanceDB directory and the conftest test database.
Nothing calls the embedding service, the reranker, the model or the network,
and the SALESFORCE corpus directory is asserted never to be created rather than
assumed untouched.
"""
from __future__ import annotations

import array
import ast
import asyncio
import hashlib
import inspect
import os
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from app import db, llm, rerank, web_index, web_memory
from app.config import settings
from app.engines import crawl
from app.freshness import Freshness

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "app"

#: The site the withholding tests crawl. One host, three pages, so the site
#: Q&A path — which scopes by normalized-URL prefix — sees all three.
SITE_ROOT = "https://handbook.example/"
QUESTION = "what is the data retention window"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Offline stand-ins for the services this path would otherwise call
# ---------------------------------------------------------------------------


def _vector(text: str) -> List[float]:
    """A deterministic 4-dim vector, always inside `web_index.MAX_DISTANCE`.

    Deliberately NOT a similarity model. This file is about what retrieval
    WITHHOLDS; if a page could go missing because it embedded badly, a pass
    would prove nothing. Every vector sits at [1.0, <0.1, 0.0, 0.0], so the
    largest possible L2 distance between any two is 0.099 against a floor of
    1.0 — no hit is ever dropped for being a weak match, and anything absent is
    absent because something withheld it.
    """
    spread = int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16) % 100
    return [1.0, 0.001 * spread, 0.0, 0.0]


@pytest.fixture()
def offline_services(monkeypatch):
    """No embedding service, no cross-encoder, no vLLM, no network."""

    async def embed_texts(texts, **_kwargs):
        return [_vector(t) for t in texts]

    async def embed_query(text, **_kwargs):
        return _vector(text)

    async def score(_query, docs, **_kwargs):
        # Every candidate answers: the cross-encoder must not be the reason a
        # page is missing either.
        return [0.95 for _ in docs]

    monkeypatch.setattr(llm, "embed_texts", embed_texts)
    monkeypatch.setattr(llm, "embed_query", embed_query)
    monkeypatch.setattr(rerank, "score", score)


@pytest.fixture()
def salesforce_dir(tmp_path, monkeypatch):
    """A CRM corpus path that DOES NOT EXIST, so "untouched" is checkable.

    LanceDB creates its directory on connect, so if any code path below opened
    `settings.lancedb_dir` the directory would exist afterwards.
    """
    crm = str(tmp_path / "lancedb-salesforce")
    monkeypatch.setattr(settings, "lancedb_dir", crm)
    assert not os.path.exists(crm)
    return crm


@pytest.fixture()
def web_dir(tmp_path, monkeypatch):
    """An isolated web index directory, nowhere near /data/lancedb-web."""
    live = str(tmp_path / "lancedb-web")
    monkeypatch.setattr(settings, "lancedb_web_dir", live)
    return live


# ---------------------------------------------------------------------------
# Corpus fixtures
# ---------------------------------------------------------------------------


def _long_page(marker: str, seed: str, paragraphs: int = 300) -> str:
    """A page well past the chunk cap, whose answer is in its FIRST chunk.

    The identifying sentence is at character 0 on purpose: at cap 2 only the
    first 6,000 characters are indexed, so a page whose answer sat in the tail
    would be absent at the low cap for a reason that has nothing to do with
    exclusion. `seed` differs per page so `_collapse_duplicates` cannot fold
    two of them into one piece of evidence.
    """
    head = (
        f"The data retention window for the {marker} archive is stated here. "
        f"The {marker} archive keeps its records for the period named above. "
    )
    body = " ".join(
        f"{seed}{i:04d} a paragraph describing the {marker} archive, its {seed} "
        f"procedures and the people who maintain them."
        for i in range(paragraphs)
    )
    return head + body


def _store_public_page(url: str, text: str, *, title: str = "Handbook") -> int:
    key = url.replace("https://", "").replace("http://", "")
    return int(
        db.upsert_web_page(
            url_key=key,
            url=url,
            canonical_url=url,
            title=title,
            text=text,
            content_type="text/html",
            fetch_status=200,
            content_hash=hashlib.sha1(text.encode("utf-8")).hexdigest(),
            origin="crawl",
        )["id"]
    )


def _index_rows() -> List[dict]:
    """Every row of the live web index, straight from LanceDB."""
    _conn, table, _meta = web_index._open()
    if table is None:
        return []
    return table.search().limit(10_000).to_list()


def _indexed_page_ids() -> Set[int]:
    return {int(r["page_id"]) for r in _index_rows()}


def _seed_site(monkeypatch, cap: int) -> Dict[str, int]:
    """Three pages of one site, stored and indexed at `cap` chunks per page.

    Indexing runs through the PRODUCTION indexer (`web_index.index_pending`),
    not the offline rebuild tool, so the vectors under test are the shape a
    real deployment holds.
    """
    monkeypatch.setattr(web_index, "_MAX_CHUNKS_PER_PAGE", cap)
    ids = {
        "alpha": _store_public_page(SITE_ROOT + "alpha", _long_page("alpha", "aa")),
        "beta": _store_public_page(SITE_ROOT + "beta", _long_page("beta", "bb")),
        "gamma": _store_public_page(SITE_ROOT + "gamma", _long_page("gamma", "cc")),
    }
    written = run(web_index.index_pending(limit=10))
    assert written > 0, "nothing was indexed; the rest of this test proves nothing"
    assert _indexed_page_ids() == set(ids.values())
    return ids


def _crawled_conversation(conversation_id: str = "conv-exclusion") -> str:
    """A finished crawl of the site — what site Q&A keys its scope on."""
    _host, prefix = crawl._scope_of(SITE_ROOT)
    crawl_id = db.create_web_crawl(conversation_id, SITE_ROOT, prefix)
    db.finish_web_crawl(crawl_id, "done", 3, 3, 0, 0)
    return conversation_id


# --- the three public reads, each reduced to the set of pages it would show --


def _dense_ids(question: str = QUESTION) -> Set[int]:
    """`web_index.retrieve` — the raw vector index."""
    return {int(h["page_id"]) for h in run(web_index.retrieve(question, top_k=10))}


def _public_ids(question: str = QUESTION) -> Set[int]:
    """`web_memory.retrieve` — the public knowledge entry point.

    What `living_knowledge.prepare` and the search engine read the shared
    corpus through: hybrid dense + PostgreSQL full text, judged and ranked.
    `use_cache=False` so a result cached in an earlier phase of the same test
    can never be what an assertion is really about.
    """
    result = run(
        web_memory.retrieve(
            question,
            level=Freshness.RECENT,
            top_k=10,
            use_cache=False,
            effort="fast",
        )
    )
    return {int(e.page_id) for e in result.evidence if e.page_id}


def _site_ids(conversation_id: str, question: str = QUESTION) -> Set[int]:
    """`crawl.site_hits_for` — where the leak was actually found."""
    hits, host = run(crawl.site_hits_for(conversation_id, question, top_k=10))
    assert host == "handbook.example"
    return {int(h["page_id"]) for h in hits}


def _all_reads(conversation_id: str) -> Dict[str, Set[int]]:
    return {
        "web_index.retrieve": _dense_ids(),
        "web_memory.retrieve": _public_ids(),
        "crawl.site_hits_for": _site_ids(conversation_id),
    }


#: Three coverage regimes, spanning the change this engagement made. 2 is
#: deliberately far below anything shipped; 64 was the cap through 2026-09-06;
#: 256 is the cap deployed on 2026-09-07. The point of the parametrisation is
#: that the exclusion invariant does not depend on how much of a page reaches
#: the index — raising coverage must never make a quarantined, purged or
#: private page reachable.
CAPS = [2, 64, 256]

#: Hand-derived from the chunker's arithmetic, not from running it: the window
#: is 3,200 chars advancing by 3,200 - 400 = 2,800, and each fixture page is
#: 30,125-30,427 chars, so an uncapped page yields 11 chunks. Three pages: 6 at
#: cap 2, 33 at cap 64. Any cap >= 11 leaves the pages uncapped, so 256 yields
#: the same 33 — which is the point: the invariant is independent of the cap.
_EXPECTED_CHUNKS = {2: 6, 64: 33, 256: 33}


# ---------------------------------------------------------------------------
# 1. A quarantined page is withheld from every public read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", CAPS)
def test_a_quarantined_page_is_withheld_from_every_public_read(
    cap, monkeypatch, offline_services, web_dir, salesforce_dir
):
    """Quarantine deletes nothing — that is the point of it.

    The row, its text and its provenance stay put so the decision is reversible
    and auditable, and the vectors stay in LanceDB. So the ONLY thing keeping a
    quarantined page out of an answer is that every read asks PostgreSQL first.
    `web_memory` has always done that in SQL; the index and the site-Q&A path
    had no round trip at all.
    """
    ids = _seed_site(monkeypatch, cap)
    conversation = _crawled_conversation()

    for where, seen in _all_reads(conversation).items():
        assert seen == set(ids.values()), f"{where} did not start with all three pages"

    assert db.set_web_page_quarantine([ids["beta"]], quarantined=True) == 1
    web_memory.cache_clear()

    neighbours = {ids["alpha"], ids["gamma"]}
    for where, seen in _all_reads(conversation).items():
        assert ids["beta"] not in seen, f"{where} served a quarantined page"
        assert seen == neighbours, f"{where} lost a neighbour of the quarantined page"

    # The vectors are still there: this is a filter, not a cleanup. Had the
    # chunks been deleted, the assertions above would pass for the wrong reason
    # and quarantine would not be reversible.
    assert ids["beta"] in _indexed_page_ids()

    assert db.set_web_page_quarantine([ids["beta"]], quarantined=False) == 1
    web_memory.cache_clear()
    for where, seen in _all_reads(conversation).items():
        assert seen == set(ids.values()), f"{where} did not restore the page"

    assert not os.path.exists(salesforce_dir), "the CRM corpus was opened"


# ---------------------------------------------------------------------------
# 2. A purged page is withheld — the orphan case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", CAPS)
def test_a_purged_page_with_orphan_vectors_is_withheld_from_every_public_read(
    cap, monkeypatch, offline_services, web_dir, salesforce_dir
):
    """The row is deleted and the vectors are deliberately left behind.

    Not a hypothetical state: it is what `purge --keep-vectors` leaves on
    purpose, what every purge left before K7 made dropping vectors the default,
    and what any interrupted purge leaves. An orphan chunk is worse than a
    stale one — with no row left, nothing can date it, name it or say who
    introduced it, and `site_hits_for` renders it into an answer as a citation
    to a page that no longer exists.
    """
    ids = _seed_site(monkeypatch, cap)
    conversation = _crawled_conversation()
    for where, seen in _all_reads(conversation).items():
        assert seen == set(ids.values()), f"{where} did not start with all three pages"

    with db.connection() as con:
        con.execute("DELETE FROM web_pages WHERE id = %s", (ids["beta"],))
    web_memory.cache_clear()

    # The chunks of the deleted page are still in LanceDB — the orphan state.
    assert ids["beta"] in _indexed_page_ids()

    neighbours = {ids["alpha"], ids["gamma"]}
    for where, seen in _all_reads(conversation).items():
        assert ids["beta"] not in seen, f"{where} served a purged page"
        assert seen == neighbours, f"{where} lost a neighbour of the purged page"

    assert not os.path.exists(salesforce_dir), "the CRM corpus was opened"


@pytest.mark.parametrize("cap", CAPS)
def test_a_corpus_of_nothing_but_orphans_answers_with_nothing(
    cap, monkeypatch, offline_services, web_dir, salesforce_dir
):
    """The degenerate end of the same property, and the honest one to state.

    Withholding must not be a per-hit best effort that still lets something
    through when everything is orphaned — that is the shape a filter takes when
    it is really a ranking tweak.
    """
    ids = _seed_site(monkeypatch, cap)
    conversation = _crawled_conversation()
    with db.connection() as con:
        con.execute("DELETE FROM web_pages")
    web_memory.cache_clear()

    assert _indexed_page_ids() == set(ids.values()), "vectors deliberately left behind"
    for where, seen in _all_reads(conversation).items():
        assert seen == set(), f"{where} answered from an all-orphan corpus"


# ---------------------------------------------------------------------------
# 3. The cap parametrisation is real
# ---------------------------------------------------------------------------


def test_the_chunk_cap_parametrisation_is_not_vacuous(
    monkeypatch, offline_services, web_dir, salesforce_dir
):
    """Raising the cap must actually change the index, or parts 1-2 are one
    test run twice."""
    counts = {}
    for cap in CAPS:
        with db.connection() as con:
            con.execute("DELETE FROM web_pages")
        monkeypatch.setattr(settings, "lancedb_web_dir", f"{web_dir}-cap{cap}")
        ids = _seed_site(monkeypatch, cap)
        counts[cap] = len(_index_rows())
        assert _indexed_page_ids() == set(ids.values())

    assert counts == _EXPECTED_CHUNKS, (
        "the two cap values did not produce different indexes, so the "
        f"exclusion tests were not proved at two coverage levels: {counts}"
    )


def test_raising_the_cap_reaches_text_the_lower_cap_never_indexed(
    monkeypatch, offline_services, web_dir, salesforce_dir
):
    """The coverage the parametrisation stands in for, stated directly.

    A phrase 25,378 characters into a page is absent from the index at cap 2
    and present at cap 64. That is the change the exclusion invariant has to
    survive — more of each page reachable — not a change in ranking.
    """
    tail_marker = "cc0250"

    def _indexed_text(cap: int) -> str:
        with db.connection() as con:
            con.execute("DELETE FROM web_pages")
        monkeypatch.setattr(settings, "lancedb_web_dir", f"{web_dir}-reach{cap}")
        _seed_site(monkeypatch, cap)
        return "\n".join(r["text"] for r in _index_rows())

    assert tail_marker not in _indexed_text(2)
    assert tail_marker in _indexed_text(64)


# ---------------------------------------------------------------------------
# 4. Private content never enters the public corpus at all
# ---------------------------------------------------------------------------

#: (module, function holding the upsert call) -> (function that supplies its
#: body, function whose body performs the fetch). Hand-derived by reading the
#: four call sites, and pinned deliberately: a wiring change has to come back
#: through this table and be re-argued, which is the whole point.
#:
#: `refetch_page` is its own supplier — it fetches the bytes it stores, so no
#: caller can inject a body into it and its callers are unconstrained.
_STORE_CHAIN: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("engines/crawl.py", "_store"): ("_crawl_site", "_fetch_page"),
    ("engines/search.py", "_store_page"): ("_fetch_source", "_fetch_source"),
    ("engines/search.py", "refetch_page"): ("refetch_page", "refetch_page"),
    ("engines/url.py", "_remember_globally"): ("fetch_and_store", "fetch_and_store"),
}

#: Reading any of these inside a store path would mean private text had been
#: wired into the shared corpus. Accessor names, not module names, because the
#: leak is a call.
_PRIVATE_ACCESSORS = (
    "get_conversation_chunks",
    "add_conversation_chunks",
    "get_summary",
    "get_uploads",
    "get_upload",
    "get_url_documents",
    "get_documents",
    "get_sf_intent",
    "get_sf_conversation_state",
    "recent_turns",
    "conversation_turns",
    "org_brief",
    "soql",
)

#: Modules that hold private text: conversation memory, uploads, and every
#: Salesforce path. None may reach the public corpus. Named explicitly rather
#: than only implied by the allowlist, so a failure says WHICH private source
#: got wired in.
_PRIVATE_SOURCE_MODULES = (
    "recall.py",
    "memory.py",
    "memory_recall.py",
    "memory_semantic.py",
    "compaction.py",
    "context.py",
    "history.py",
    "uploads.py",
    "summarize.py",
    "core/brain.py",
    "core/org_brief.py",
    "core/salesforce.py",
    "core/sf_dictionary.py",
    "engines/rag.py",
    "engines/report.py",
    "engines/sf_intel.py",
    "engines/live_sf.py",
    "engines/sql.py",
    "engines/document.py",
    "engines/dataset.py",
    "engines/ocr.py",
    "engines/vision.py",
)


def _module_source(relative: str) -> str:
    return (_APP / relative).read_text(encoding="utf-8")


def _enclosing_functions(tree: ast.AST) -> Dict[int, List[str]]:
    """{id(node): [innermost def name, ..., outermost def name]}."""
    parents: Dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    chains: Dict[int, List[str]] = {}
    for node in ast.walk(tree):
        chain: List[str] = []
        cur = node
        while id(cur) in parents:
            cur = parents[id(cur)]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chain.append(cur.name)
        chains[id(node)] = chain
    return chains


def _upsert_call_sites() -> List[Tuple[str, int, str]]:
    """[(path relative to app/, line, outermost enclosing function)]."""
    sites = []
    for path in sorted(_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        chains = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "upsert_web_page":
                continue
            chain = chains[id(node)]
            sites.append(
                (str(path.relative_to(_APP)), node.lineno, chain[-1] if chain else "<module>")
            )
    return sites


def _references_to(name: str) -> Set[Tuple[str, str]]:
    """{(path relative to app/, outermost enclosing function)} mentioning `name`.

    Every mention, not only `ast.Call` nodes: `db.run_in_thread(_store, ...)`
    is a call site where the function is passed as a value, and a proof that
    missed it would be no proof at all.
    """
    found: Set[Tuple[str, str]] = set()
    for path in sorted(_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        chains = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found_name = node.id
            elif isinstance(node, ast.Attribute):
                found_name = node.attr
            else:
                continue
            if found_name != name:
                continue
            chain = chains[id(node)]
            outer = chain[-1] if chain else "<module>"
            if outer == name:  # inside the definition itself
                continue
            found.add((str(path.relative_to(_APP)), outer))
    return found


def _function_source(relative: str, name: str) -> str:
    source = _module_source(relative)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{relative} has no function {name!r}")


def _identifiers(relative: str) -> Set[str]:
    """Every NAME and ATTRIBUTE in a module — code only, never prose.

    Substring matching a file would trip over its own docstring; `recall.py`
    explains at length why conversation vectors are NOT in LanceDB, and a
    grep cannot tell that sentence from an import.
    """
    tree = ast.parse(_module_source(relative))
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
            names.update((a.asname or "") for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
            if node.module:
                names.update(node.module.split("."))
    names.discard("")
    return names


def test_the_public_corpus_has_exactly_one_writer_of_text():
    """One INSERT, in `db.upsert_web_page`. Nothing else can add a page.

    This is the narrow neck the rest of part 4 rests on: with a second INSERT,
    enumerating callers of `upsert_web_page` would no longer be enumerating
    everything that can put text into the shared corpus.
    """
    inserts = []
    for path in sorted([*_APP.rglob("*.py"), *(_ROOT / "tools").rglob("*.py")]):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "INSERT INTO web_pages" in line:
                inserts.append(f"{path.relative_to(_ROOT)}:{lineno}")
    assert len(inserts) == 1 and inserts[0].startswith("app/db.py:"), (
        "the public corpus gained a second INSERT path; every admission "
        f"argument in this file is scoped to the first one: {inserts}"
    )
    assert "INSERT INTO web_pages" in inspect.getsource(db.upsert_web_page)


def test_every_upsert_web_page_caller_is_one_of_the_four_fetch_call_sites():
    """The allowlist. A new call site anywhere in `app/` fails this.

    Enumerated from the source rather than from imports, because the failure
    mode is somebody adding a store call in a module that already imports `db`
    — which is nearly all of them.
    """
    sites = _upsert_call_sites()
    assert sorted((path, fn) for path, _line, fn in sites) == sorted(_STORE_CHAIN), (
        "the set of functions that write the shared web corpus changed; each "
        f"one must be shown to take its body from a safe_fetch: {sites}"
    )


def test_every_upsert_web_page_caller_takes_its_body_from_a_safe_fetch():
    """Each of the four, traced to the fetch that produced its bytes.

    `_store` and `_store_page` receive already-extracted text, so the proof is
    that their ONLY caller in `app/` is the named fetch function, and that that
    function fetches through `net.safe_fetch` — the guarded path (SSRF
    blocklist, DNS pinning, redirect revalidation, byte caps, timeouts). A
    second caller appearing is a wiring change that has to be re-argued here.
    """
    problems = []
    for (module, store_fn), (supplier, fetcher) in _STORE_CHAIN.items():
        if supplier != store_fn:
            callers = _references_to(store_fn)
            if callers != {(module, supplier)}:
                problems.append(
                    f"{module}:{store_fn} is reached from {sorted(callers)}, "
                    f"not only from {supplier}"
                )
                continue
        if "net.safe_fetch(" not in _function_source(module, fetcher):
            problems.append(f"{module}:{fetcher} does not fetch through net.safe_fetch")
        if fetcher != supplier and fetcher not in _function_source(module, supplier):
            problems.append(f"{module}:{supplier} does not call {fetcher}")
    assert not problems, problems


def test_the_pasted_link_path_refuses_a_url_that_is_itself_a_credential():
    """`safe_fetch` is necessary and not sufficient for the SHARED corpus.

    A pre-signed S3 or SAS URL, a `user:pass@` URL or an OAuth callback passes
    every SSRF check and still returns a body that is private to whoever held
    the link. The pasted-link path is the only store path a member aims by
    hand, so it checks the pasted URL AND the one the fetch landed on before
    anything reaches `web_pages`.
    """
    source = _function_source("engines/url.py", "_remember_globally")
    assert source.index("check_shareable") < source.index("upsert_web_page"), (
        "the shareability decision must be made before the page is stored"
    )
    assert source.count("check_shareable") >= 2, (
        "both the pasted URL and the landed URL must be checked — a short link "
        "that redirects to a signed object is the same leak with one hop"
    )
    from app.core.urls import check_shareable

    assert check_shareable("https://example.com/page").url
    for private in (
        "https://user:secret@example.com/page",
        "http://127.0.0.1:8000/internal",
        "https://bucket.s3.amazonaws.com/x?X-Amz-Signature=deadbeef",
    ):
        assert check_shareable(private).url is None, private


def test_no_store_path_reads_a_private_source():
    """The store functions and their suppliers touch no conversation, upload
    or Salesforce accessor.

    The allowlist says a private module cannot call the store; this says the
    store cannot call a private module. Both directions, because either alone
    is a hole.
    """
    offenders = []
    for (module, store_fn), (supplier, fetcher) in _STORE_CHAIN.items():
        for fn in sorted({store_fn, supplier, fetcher}):
            source = _function_source(module, fn)
            offenders.extend(
                f"{module}:{fn} reads {accessor}"
                for accessor in _PRIVATE_ACCESSORS
                if accessor in source
            )
    assert not offenders, offenders


def test_no_private_source_module_can_reach_the_public_corpus():
    """Conversation memory, uploads and every Salesforce path, by name.

    Salesforce is the case with the least margin: `engines/report.py`,
    `core/org_brief.py`, `core/brain.py` and the `sf_*` modules read CRM
    records for one authenticated user, and the shared corpus has no viewer
    check that could contain them.
    """
    offenders = []
    for relative in _PRIVATE_SOURCE_MODULES:
        assert (_APP / relative).exists(), f"{relative} moved; update this list"
        names = _identifiers(relative)
        offenders.extend(
            f"{relative} references {forbidden}"
            for forbidden in ("upsert_web_page", "index_pending", "lancedb_web_dir")
            if forbidden in names
        )
    assert not offenders, offenders

    # And every Salesforce module in the tree, not only the ones named above.
    sf_modules = sorted([*_APP.rglob("sf_*.py"), *_APP.rglob("*salesforce*.py")])
    assert sf_modules, "no Salesforce modules found; this check would be empty"
    for path in sf_modules:
        source = path.read_text(encoding="utf-8")
        assert "upsert_web_page" not in source, f"{path} writes the public corpus"


def test_the_web_vector_index_has_one_writer_and_it_reads_web_pages():
    """The other store the corpus lives in.

    Text can also reach a reader by being embedded into the LanceDB web table
    directly, without ever touching `web_pages`. Exactly one line in `app/`
    adds rows to that table, and the rows it adds are built from
    `db.get_unindexed_web_pages` — from `web_pages`, and nothing else.
    """
    adds = []
    for path in sorted(_APP.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "table.add(" in line:
                adds.append(f"{path.relative_to(_APP)}:{lineno}")
    assert len(adds) == 1 and adds[0].startswith("web_index.py:"), adds

    indexer = inspect.getsource(web_index.index_pending)
    assert "db.get_unindexed_web_pages" in indexer
    select = inspect.getsource(db.get_unindexed_web_pages)
    assert "FROM web_pages" in select
    for private in ("conversation_chunks", "uploads", "url_documents", "messages"):
        assert private not in select, f"the indexer's source query reads {private}"
        assert private not in indexer, f"the indexer reads {private}"


def test_conversation_chunks_live_in_postgresql_and_never_lancedb():
    """Session isolation is structural, and it depends on the storage choice.

    Chat vectors live in `conversation_chunks`, keyed by conversation_id, read
    only through `db.get_conversation_chunks(conversation_id)`. Putting them in
    a LanceDB table instead would put private turns in a store whose readers do
    not scope by conversation at all: the CRM table renders every hit as a
    Salesforce citation, and the web table is global by design.
    """
    with db.connection() as con:
        columns = {
            r["column_name"]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'conversation_chunks'"
            ).fetchall()
        }
    assert {"conversation_id", "ordinal", "role", "text", "embedding"} <= columns

    assert "INSERT INTO conversation_chunks" in inspect.getsource(
        db.add_conversation_chunks
    )
    reader = inspect.getsource(db.get_conversation_chunks)
    assert "FROM conversation_chunks" in reader
    assert "WHERE conversation_id = %s" in reader

    # The modules that own chat vectors reach no vector store at all.
    for relative in ("recall.py", "memory_semantic.py"):
        names = _identifiers(relative)
        for forbidden in ("lancedb", "web_index", "lancedb_web_dir", "lancedb_dir"):
            assert forbidden not in names, (
                f"app/{relative} references {forbidden}; conversation vectors "
                "must stay in PostgreSQL"
            )


def test_private_text_is_never_admitted_by_the_indexer(
    monkeypatch, offline_services, web_dir, salesforce_dir, as_user
):
    """The runtime half: seed every private store, then run the real indexer.

    The marker is unique to the private rows, so its presence anywhere in
    `web_pages` or the LanceDB web table is a leak — and its absence from the
    private tables would mean the test had proved nothing.
    """
    marker = "PRIVATEMARKER7f31"
    user = as_user("alice")
    conversation = "conv-private"
    db.create_conversation(int(user["id"]), conversation, "Private thread")

    db.add_message(int(user["id"]), conversation, "user", f"my passphrase is {marker}")
    db.add_conversation_chunks(
        conversation,
        [
            {
                "ordinal": 0,
                "role": "user",
                "text": f"folded turn containing {marker}",
                "embedding": array.array("f", _vector(marker)).tobytes(),
            }
        ],
    )
    db.save_upload(
        f"up-{marker}", conversation, f"{marker}.csv", 128, "ready", notes=marker
    )
    db.save_url_document(
        conversation, "https://intranet.invalid/report", marker, f"body {marker}"
    )

    # One genuinely public page, so the indexer has real work and an empty
    # index cannot be what makes the assertions pass.
    public_id = _store_public_page(SITE_ROOT + "public", _long_page("public", "pp", 40))
    assert run(web_index.index_pending(limit=20)) > 0

    with db.connection() as con:
        leaked = con.execute(
            "SELECT id FROM web_pages "
            "WHERE text LIKE %s OR title LIKE %s OR url LIKE %s",
            (f"%{marker}%", f"%{marker}%", f"%{marker}%"),
        ).fetchall()
    assert leaked == [], "private text reached web_pages"

    rows = _index_rows()
    assert {int(r["page_id"]) for r in rows} == {public_id}
    for row in rows:
        blob = f"{row.get('url', '')} {row.get('title', '')} {row.get('text', '')}"
        assert marker not in blob, "private text reached the web vector index"

    # Non-vacuous: the private rows really are there, in PostgreSQL.
    assert db.get_conversation_chunks(conversation)[0]["text"].endswith(marker)
    assert db.get_uploads(conversation)[0]["filename"] == f"{marker}.csv"
    assert db.get_url_documents(conversation)[0]["title"] == marker

    # Nothing in this test opened the CRM corpus.
    assert not os.path.exists(salesforce_dir)


def test_the_public_retrieval_entry_point_takes_no_viewer():
    """No viewer parameter on any public read, at any layer.

    The corpus is shared on purpose — one member's fetched page answers the
    next member's question — which is only defensible while there is nothing
    per-user in it. A viewer argument appearing on these signatures would mean
    somebody had begun scoping the shared corpus, i.e. that it had stopped
    being purely public. That is a design change, not a patch.
    """
    viewerish = {
        "user",
        "user_id",
        "viewer",
        "viewer_id",
        "owner",
        "owner_id",
        "principal",
        "workspace",
        "workspace_id",
        "member_id",
        "session_id",
    }
    for fn in (web_memory.retrieve, web_index.retrieve, crawl.site_hits_for):
        params = set(inspect.signature(fn).parameters)
        assert not (params & viewerish), f"{fn.__qualname__} gained {params & viewerish}"

    # The retrieval SQL itself: no identity predicate on the read path. The two
    # provenance columns exist (see below) and must never become a scope.
    for fn in (
        web_memory._lexical_candidates,
        web_memory._page_meta,
        db.servable_web_page_ids,
        web_index._servable_page_ids,
    ):
        source = inspect.getsource(fn)
        for column in ("introduced_by_user_id", "introduced_in_conversation_id"):
            assert column not in source, (
                f"{fn.__qualname__} reads {column}; provenance is not a "
                "retrieval scope and must not become one silently"
            )


def test_the_public_corpus_has_no_owner_column():
    """`web_pages` models no private content, so there is none to leak.

    Exactly two identity-bearing columns, both provenance: who introduced a
    page and in which conversation. Both nullable, neither read on the
    retrieval path (asserted above), and together they are the known residual —
    a globally readable row records who brought it in, visible to DB/CLI
    access. An `owner_id`, `workspace_id`, `visibility` or `private` column
    appearing here would mean private content had been modelled into the
    shared corpus, and every argument in this file would need re-making.
    """
    with db.connection() as con:
        columns = [
            r["column_name"]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'web_pages' ORDER BY ordinal_position"
            ).fetchall()
        ]
    identity = [
        c
        for c in columns
        if c.endswith(("_user_id", "_conversation_id"))
        or c in {"user_id", "owner_id", "workspace_id", "visibility", "private", "scope"}
    ]
    assert identity == ["introduced_by_user_id", "introduced_in_conversation_id"], (
        f"web_pages gained an ownership or visibility column: {identity}"
    )


def test_the_evidence_cache_refuses_anything_that_is_not_public(monkeypatch):
    """The last door, and the one thing here that is shared between users.

    Every `Evidence` this module builds is `scope="public"` because it only
    ever reads `web_pages`. The cache asserts that rather than assuming it, so
    a future private source cannot leak through a process-wide cache whose key
    has no viewer in it. This pins that assertion.
    """
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 60)
    web_memory.cache_clear()

    def _evidence(scope: str) -> web_memory.Evidence:
        return web_memory.Evidence(
            url="https://public.example/p",
            title="p",
            text="a passage",
            domain="public.example",
            authority=40,
            fetched_at=None,
            scope=scope,
        )

    public = web_memory.Retrieval(query="q", freshness=Freshness.RECENT)
    public.evidence = [_evidence("public")]
    web_memory._cache_put("public-key", public)
    assert web_memory._cache_get("public-key") is not None

    private = web_memory.Retrieval(query="q", freshness=Freshness.RECENT)
    private.evidence = [_evidence("conversation")]
    web_memory._cache_put("private-key", private)
    assert web_memory._cache_get("private-key") is None, (
        "the shared evidence cache accepted non-public evidence"
    )

    # And the key carries no viewer either — it is a shared cache by design.
    key_source = inspect.getsource(web_memory._cache_key)
    assert "user" not in key_source and "viewer" not in key_source


# ---------------------------------------------------------------------------
# 5. OPEN GAP — the evidence cache sits in front of the SQL filter
#
# Parts 1-3 prove that every RETRIEVAL asks PostgreSQL before it serves a page.
# They prove it with `web_memory.cache_clear()` in hand, and with quarantine
# applied through `db.set_web_page_quarantine`, which bumps the corpus
# generation the cache keys on. Neither of those is what production does.
#
# `tools/knowledge_admin.py` — the only quarantine and purge interface that
# exists, and the one the operator runbook names — issues
# `UPDATE web_pages SET quarantined_at = now()` directly. It never calls
# `db.set_web_page_quarantine` (which has zero callers anywhere in `app/` or
# `tools/`), so the generation is never bumped; and it runs as a separate
# process, so even if it did bump, `_web_corpus_generation` is a module global
# in the ORCHESTRATOR's process and would not move there.
#
# The result is that `web_memory.retrieve` keeps serving the quarantined
# page's TEXT and URL from `_cache` until the entry ages out —
# KNOWLEDGE_EVIDENCE_CACHE_TTL_S, 60 seconds by default and unset in this
# deployment's .env. Measured here: the SQL filter is correct (the same read
# with `use_cache=False` returns nothing), and the in-process API does close
# the cache (the control below). The hole is only between the two.
#
# Bounded, but real: for that window an operator who has just pulled a page
# has been told it is out of retrieval while it is still being quoted and
# cited. Not fixed here — the change belongs in `web_memory` or in the admin
# tool, neither of which is this workstream's to edit.
# ---------------------------------------------------------------------------


def _cached_public_ids(question: str = QUESTION) -> Set[int]:
    """`web_memory.retrieve` WITH its cache, which is how every real caller
    reads it — `_public_ids` above passes `use_cache=False` on purpose."""
    result = run(
        web_memory.retrieve(question, level=Freshness.RECENT, top_k=10, effort="fast")
    )
    return {int(e.page_id) for e in result.evidence if e.page_id}


def _quarantine_like_the_cli(page_id: int) -> None:
    """Byte-for-byte the statement `tools/knowledge_admin.py` runs."""
    with db.connection() as con:
        con.execute(
            "UPDATE web_pages SET quarantined_at = now() WHERE id = %s "
            "AND quarantined_at IS NULL",
            (page_id,),
        )


# FIXED 2026-09-07. This was a strict xfail: the operator CLI quarantines with
# raw SQL in another process, so the corpus generation the evidence cache keys
# on never moved and the page kept being served for the cache TTL (60 s
# default). The generation counter could never fix it -- it is a module global,
# so a bump in the CLI's process is invisible to the server's. `web_memory`
# now revalidates a cache HIT against the database instead
# (`_cache_entry_still_servable` -> `db.servable_web_page_ids`, off the event
# loop), evicting and recomputing when a page has been withdrawn, and failing
# CLOSED when it cannot check. Cost is one indexed lookup per cache hit.
def test_a_quarantine_applied_the_way_the_cli_applies_it_takes_effect_at_once(
    monkeypatch, offline_services, web_dir, salesforce_dir
):
    ids = _seed_site(monkeypatch, 64)
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 60.0)
    web_memory.cache_clear()

    assert _cached_public_ids() == set(ids.values()), "the corpus did not start whole"
    _quarantine_like_the_cli(ids["beta"])

    assert ids["beta"] not in _cached_public_ids(), (
        "a quarantined page was served out of the evidence cache; its text and "
        "its URL reach the prompt and the citation panel"
    )


def test_the_sql_filter_itself_is_sound_and_the_in_process_api_closes_the_cache(
    monkeypatch, offline_services, web_dir, salesforce_dir
):
    """The control that localises the gap above to the cache, not the filter.

    Two things must hold for the xfail to mean what it claims: reading past
    the cache must already exclude the page, and quarantining through the
    in-process API must close the cache by itself.
    """
    ids = _seed_site(monkeypatch, 64)
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 60.0)
    web_memory.cache_clear()

    assert _cached_public_ids() == set(ids.values())
    _quarantine_like_the_cli(ids["beta"])

    # The SQL is right: nothing is wrong with the filter.
    assert ids["beta"] not in _public_ids(), "the uncached read served it too"

    # And the API that bumps the generation does close the cache — it is
    # simply not what quarantines a page in this deployment.
    with db.connection() as con:
        con.execute("UPDATE web_pages SET quarantined_at = NULL WHERE id = %s",
                    (ids["beta"],))
    web_memory.cache_clear()
    assert _cached_public_ids() == set(ids.values())
    before = db.web_corpus_generation()
    assert db.set_web_page_quarantine([ids["beta"]], quarantined=True) == 1
    assert db.web_corpus_generation() > before, "the API stopped bumping"
    assert ids["beta"] not in _cached_public_ids()


def test_the_generation_bump_has_no_caller_on_the_quarantine_path():
    """Names the omission, so the fix is not mistaken for a cache-tuning job.

    `db.set_web_page_quarantine` is dead code: nothing in `app/` or `tools/`
    calls it. Pointing the CLI at it would still leave the deeper half of the
    problem — `_web_corpus_generation` is a per-process counter, so a bump in
    the CLI's process says nothing to the orchestrator's — but it is where a
    reader will look first, so the state of it is pinned here.
    """
    callers = _references_to("set_web_page_quarantine") - {("db.py", "set_web_page_quarantine")}
    assert callers == set(), (
        f"set_web_page_quarantine now has callers ({sorted(callers)}); if the "
        "quarantine path was rewired, revisit the xfail above"
    )

    admin = (_ROOT / "tools" / "knowledge_admin.py").read_text(encoding="utf-8")
    assert "UPDATE web_pages SET quarantined_at" in admin
    assert "bump_web_corpus_generation" not in admin
    assert "set_web_page_quarantine" not in admin

    # The counter itself: a module global, therefore per process.
    assert "_web_corpus_generation = 0" in inspect.getsource(db)
