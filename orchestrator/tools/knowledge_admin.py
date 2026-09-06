"""Operator CLI for the SHARED web corpus: inspect, quarantine, purge.

Runs inside the orchestrator container (``/app``):

    python -m tools.knowledge_admin pages   [--domain D] [--url U] [--origin O] [--introducer UID] [--quarantined] [--limit N]
    python -m tools.knowledge_admin claims  [--page-id ID] [--domain D] [--introducer UID] [--origin-user UID] [--limit N]
    python -m tools.knowledge_admin quarantine   (--id ID ... | --url U ... | --domain D | --introducer UID) --yes
    python -m tools.knowledge_admin unquarantine (--id ID ... | --url U ... | --domain D | --introducer UID) --yes
    python -m tools.knowledge_admin purge --introducer UID [--origin O] [--keep-vectors] --yes

WHY THIS EXISTS (ADR-0001 D7, migration V16). The web store is shared by
every account and became member-writable on 2026-09-03: a pasted link is
stored globally and its site crawled. A page one member introduced is
therefore evidence for everyone, and there was no way to see who put what
into the corpus, take a page out of retrieval without deleting it, or remove
everything an account introduced when that account is removed. The V16
columns (``origin``, ``introduced_by_user_id``, ``quarantined_at``) make
each of those one SQL statement; this tool is the operator's handle on them.

SAFETY RULES. Every mutating command prints the rows it would touch and
stops — ``--yes`` is what applies it. ``purge`` deletes ``web_claims`` and
``web_page_versions`` explicitly before the pages: the claims FK is
``ON DELETE SET NULL``, so without that a claim taken from a purged page
would survive as an orphan and still be rendered to users. Quarantine is
reversible and touches nothing but ``quarantined_at``; every retrieval path
now excludes quarantined rows (``web_memory`` by SQL, ``web_index.retrieve``
by asking PostgreSQL which pages may still be served), so the vectors can
stay put.

A PURGE DELETES THE VECTORS TOO — that is the default, not a flag (K7,
2026-09-06). It was ``--drop-vectors``, opt-in, so the ordinary outcome of a
purge was a LanceDB table full of chunks for pages that no longer existed.
Those chunks were not inert: ``crawl.site_hits_for`` reads the web index
directly and rendered chunk text into an answer, so an orphan was a citation
to a deleted page with no row left to date it, quarantine it or name it.

TWO CHANGES, NOT ONE, because either alone leaves a hole. Deletion is now
whole by default, AND ``web_index.retrieve`` asks PostgreSQL which page ids
may still be served, so an orphan that gets in some other way is not served.
``--keep-vectors`` is the deliberate exception, for an operator about to
rebuild the index anyway (``python -m tools.reindex_web build``); it says what
it left behind. A vector delete that FAILS never silently passes: purge exits
non-zero and names the rebuild, because the PostgreSQL rows are already gone
by then and only an operator can decide what to do about it.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

if os.path.isdir("/app"):
    sys.path.insert(0, "/app")

from app import db  # noqa: E402

#: How the V16 upsert labels a page's entry into the store (db.upsert_web_page).
ORIGINS = ("search", "refresh", "crawl", "share", "research")


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


def _domain_clause(domain: str, column: str = "domain") -> Tuple[str, List[Any]]:
    """The domain itself and every subdomain: ``example.com`` also selects
    ``docs.example.com`` — a crawl of one site lands under several hosts."""
    d = (domain or "").strip().lower().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return f"({column} = %s OR {column} = %s OR {column} LIKE %s)", [d, f"www.{d}", f"%.{d}"]


def page_selector(
    *,
    ids: Sequence[int] = (),
    urls: Sequence[str] = (),
    domain: str = "",
    origin: str = "",
    introducer: Optional[int] = None,
    quarantined: Optional[bool] = None,
) -> Tuple[str, List[Any], str]:
    """(WHERE clause over web_pages, params, human description)."""
    clauses: List[str] = []
    params: List[Any] = []
    words: List[str] = []
    if ids:
        clauses.append("id = ANY(%s)")
        params.append([int(i) for i in ids])
        words.append(f"id in {sorted(int(i) for i in ids)}")
    if urls:
        # An operator arrives holding a URL out of a citation panel, not a
        # page id. `url` is what was fetched and `canonical_url` is what the
        # page said it was; a citation can carry either, so both are matched.
        wanted = [str(u).strip() for u in urls if str(u).strip()]
        clauses.append("(url = ANY(%s) OR canonical_url = ANY(%s))")
        params.extend([wanted, wanted])
        words.append(f"{len(wanted)} url(s)")
    if domain:
        clause, p = _domain_clause(domain)
        clauses.append(clause)
        params.extend(p)
        words.append(f"domain {domain} (and subdomains)")
    if origin:
        clauses.append("origin = %s")
        params.append(origin)
        words.append(f"origin {origin}")
    if introducer is not None:
        clauses.append("introduced_by_user_id = %s")
        params.append(int(introducer))
        words.append(f"introduced by user {int(introducer)}")
    if quarantined is True:
        clauses.append("quarantined_at IS NOT NULL")
        words.append("quarantined")
    elif quarantined is False:
        clauses.append("quarantined_at IS NULL")
        words.append("not quarantined")
    where = " AND ".join(clauses) if clauses else "TRUE"
    return where, params, ", ".join(words) or "every page"


# ---------------------------------------------------------------------------
# Read-only reports
# ---------------------------------------------------------------------------


def list_pages(where: str, params: List[Any], limit: int = 50) -> List[dict]:
    with db.connection() as con:
        rows = con.execute(
            "SELECT id, url, domain, title, origin, introduced_by_user_id, "
            "introduced_in_conversation_id, quarantined_at, fetched_at, "
            "length(text) AS chars, indexed_at IS NOT NULL AS indexed "
            f"FROM web_pages WHERE {where} ORDER BY fetched_at DESC, id DESC LIMIT %s",
            [*params, int(limit)],
        ).fetchall()
    return [dict(r) for r in rows]


def page_counts(where: str, params: List[Any]) -> Dict[str, Any]:
    """What a selector covers, by origin — printed before every mutation."""
    with db.connection() as con:
        by_origin = con.execute(
            f"SELECT origin, count(*) AS n FROM web_pages WHERE {where} GROUP BY origin ORDER BY origin",
            params,
        ).fetchall()
        quarantined = con.execute(
            f"SELECT count(*) AS n FROM web_pages WHERE {where} AND quarantined_at IS NOT NULL",
            params,
        ).fetchone()["n"]
        claims = con.execute(
            "SELECT count(*) AS n FROM web_claims WHERE page_id IN "
            f"(SELECT id FROM web_pages WHERE {where})",
            params,
        ).fetchone()["n"]
        versions = con.execute(
            "SELECT count(*) AS n FROM web_page_versions WHERE page_id IN "
            f"(SELECT id FROM web_pages WHERE {where})",
            params,
        ).fetchone()["n"]
    origins = {str(r["origin"]): int(r["n"]) for r in by_origin}
    return {
        "pages": sum(origins.values()),
        "by_origin": origins,
        "quarantined": int(quarantined),
        "claims": int(claims),
        "versions": int(versions),
    }


def list_claims(
    *,
    page_id: Optional[int] = None,
    domain: str = "",
    introducer: Optional[int] = None,
    origin_user: Optional[int] = None,
    limit: int = 50,
) -> List[dict]:
    clauses: List[str] = []
    params: List[Any] = []
    if page_id is not None:
        clauses.append("c.page_id = %s")
        params.append(int(page_id))
    if domain:
        clause, p = _domain_clause(domain, column="p.domain")
        clauses.append(clause)
        params.extend(p)
    if introducer is not None:
        clauses.append("p.introduced_by_user_id = %s")
        params.append(int(introducer))
    if origin_user is not None:
        clauses.append("c.origin_user_id = %s")
        params.append(int(origin_user))
    where = " AND ".join(clauses) if clauses else "TRUE"
    with db.connection() as con:
        rows = con.execute(
            "SELECT c.id, c.page_id, c.research_id, c.kind, c.confidence, c.as_of, "
            "c.claim, c.value, c.quote, c.origin_user_id, c.origin_conversation_id, "
            "c.created_at, p.domain, p.origin AS page_origin, p.quarantined_at "
            "FROM web_claims c LEFT JOIN web_pages p ON p.id = c.page_id "
            f"WHERE {where} ORDER BY c.created_at DESC, c.id DESC LIMIT %s",
            [*params, int(limit)],
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Mutations (each returns the number of rows it changed)
# ---------------------------------------------------------------------------


def set_quarantine(where: str, params: List[Any], quarantine: bool) -> int:
    with db.connection() as con:
        if quarantine:
            cur = con.execute(
                f"UPDATE web_pages SET quarantined_at = now() WHERE {where} AND quarantined_at IS NULL",
                params,
            )
        else:
            cur = con.execute(
                f"UPDATE web_pages SET quarantined_at = NULL WHERE {where} AND quarantined_at IS NOT NULL",
                params,
            )
        return int(cur.rowcount or 0)


def purge_pages(where: str, params: List[Any]) -> Dict[str, Any]:
    """Delete the selected pages with their claims and versions, in ONE
    transaction. Returns the counts and the page ids removed (for the
    vector cleanup)."""
    with db.connection() as con:
        ids = [
            int(r["id"])
            for r in con.execute(f"SELECT id FROM web_pages WHERE {where}", params).fetchall()
        ]
        if not ids:
            return {"pages": 0, "claims": 0, "versions": 0, "ids": []}
        claims = con.execute("DELETE FROM web_claims WHERE page_id = ANY(%s)", (ids,)).rowcount
        versions = con.execute(
            "DELETE FROM web_page_versions WHERE page_id = ANY(%s)", (ids,)
        ).rowcount
        pages = con.execute("DELETE FROM web_pages WHERE id = ANY(%s)", (ids,)).rowcount
        return {
            "pages": int(pages or 0),
            "claims": int(claims or 0),
            "versions": int(versions or 0),
            "ids": ids,
        }


def drop_vectors(page_ids: Sequence[int]) -> Dict[str, Any]:
    """Remove the pages' chunks from the live web index.

    Thin wrapper over `web_index.delete_pages`, which owns the LanceDB write
    and the guard that refuses a directory overlapping the SALESFORCE corpus.
    A failure is REPORTED, not raised — the PostgreSQL rows are already gone
    by the time this runs, so the caller must be able to say what state the
    store is in and name the rebuild rather than die mid-way."""
    from app import web_index  # lazy: pulls in llm/metrics

    ids = [int(i) for i in page_ids]
    if not ids:
        return {"status": "skipped", "removed": 0, "rows": 0}
    try:
        return web_index.delete_pages(ids)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "removed": 0, "detail": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _short(value: Any, width: int) -> str:
    text = str(value or "")
    return text if len(text) <= width else text[: width - 1] + "…"


def _date(value: Any) -> str:
    return str(value)[:10] if value else "-"


def render_pages(rows: List[dict]) -> str:
    if not rows:
        return "(no pages)"
    lines = [
        f"{'id':>7} {'origin':<8} {'intro':>6} {'Q':<1} {'fetched':<10} {'chars':>7} {'domain':<30} title",
    ]
    for r in rows:
        lines.append(
            f"{r['id']:>7} {_short(r['origin'], 8):<8} "
            f"{(r['introduced_by_user_id'] if r['introduced_by_user_id'] is not None else '-'):>6} "
            f"{'Q' if r['quarantined_at'] else ' ':<1} {_date(r['fetched_at']):<10} "
            f"{r['chars']:>7} {_short(r['domain'], 30):<30} {_short(r['title'], 60)}"
        )
        lines.append(f"{'':>7} {r['url']}")
    return "\n".join(lines)


def render_claims(rows: List[dict]) -> str:
    if not rows:
        return "(no claims)"
    lines = []
    for r in rows:
        flags = " [page quarantined]" if r.get("quarantined_at") else ""
        lines.append(
            f"claim {r['id']} page={r['page_id'] if r['page_id'] is not None else '-'} "
            f"kind={r['kind']} conf={float(r['confidence'] or 0):.2f} as_of={_date(r['as_of'])} "
            f"domain={r['domain'] or '-'} origin_user={r['origin_user_id'] if r['origin_user_id'] is not None else '-'} "
            f"run={_short(r['research_id'], 24)}{flags}"
        )
        lines.append(f"    {_short(r['claim'], 160)}")
        if r.get("value"):
            lines.append(f"    value: {_short(r['value'], 120)}")
        if r.get("quote"):
            lines.append(f"    quote: {_short(r['quote'], 160)}")
    return "\n".join(lines)


def render_counts(counts: Dict[str, Any]) -> str:
    origins = ", ".join(f"{k}={v}" for k, v in sorted(counts["by_origin"].items())) or "-"
    return (
        f"web_pages: {counts['pages']} (by origin: {origins}; already quarantined: {counts['quarantined']})\n"
        f"web_claims on those pages: {counts['claims']}\n"
        f"web_page_versions of those pages: {counts['versions']}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_selector(parser: argparse.ArgumentParser, *, require_one: bool) -> None:
    parser.add_argument("--id", type=int, action="append", default=[], help="page id (repeatable)")
    parser.add_argument("--url", action="append", default=[], help="exact page url (repeatable)")
    parser.add_argument("--domain", default="", help="domain and its subdomains")
    parser.add_argument("--introducer", type=int, default=None, help="introduced_by_user_id")
    if require_one:
        parser.add_argument("--origin", default="", choices=("", *ORIGINS), help="narrow further by origin")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.knowledge_admin",
        description="Inspect, quarantine and purge pages of the shared web corpus.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pages = sub.add_parser("pages", help="list web_pages")
    _add_selector(pages, require_one=False)
    pages.add_argument("--origin", default="", choices=("", *ORIGINS))
    group = pages.add_mutually_exclusive_group()
    group.add_argument("--quarantined", action="store_true", help="only quarantined pages")
    group.add_argument("--not-quarantined", action="store_true", help="only pages in retrieval")
    pages.add_argument("--limit", type=int, default=50)

    claims = sub.add_parser("claims", help="show web_claims")
    claims.add_argument("--page-id", type=int, default=None)
    claims.add_argument("--domain", default="")
    claims.add_argument("--introducer", type=int, default=None, help="pages introduced by this user")
    claims.add_argument("--origin-user", type=int, default=None, help="claims produced by this user's research run")
    claims.add_argument("--limit", type=int, default=50)

    for name, help_text in (
        ("quarantine", "set quarantined_at: the pages leave every retrieval query but stay stored"),
        ("unquarantine", "clear quarantined_at"),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_selector(p, require_one=True)
        p.add_argument("--yes", action="store_true", help="apply; without it only the counts are printed")

    purge = sub.add_parser(
        "purge",
        help="DELETE pages an account introduced, with their claims, versions AND vectors",
    )
    purge.add_argument("--introducer", type=int, required=True, help="introduced_by_user_id")
    purge.add_argument("--origin", default="", choices=("", *ORIGINS), help="only pages still carrying this origin (e.g. share)")
    vectors = purge.add_mutually_exclusive_group()
    vectors.add_argument(
        "--keep-vectors", action="store_true",
        help="leave the pages' chunks in the live web index (retrieval refuses to serve "
             "them, but they cost scan time and disk until `tools.reindex_web build` runs)",
    )
    vectors.add_argument(
        "--drop-vectors", action="store_true",
        help="accepted for compatibility and now the DEFAULT; has no effect",
    )
    purge.add_argument("--yes", action="store_true", help="required: purge refuses to run without it")
    return parser


def _selector_from(args: argparse.Namespace) -> Tuple[str, List[Any], str]:
    return page_selector(
        ids=args.id,
        urls=getattr(args, "url", []) or [],
        domain=args.domain,
        origin=getattr(args, "origin", "") or "",
        introducer=args.introducer,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))

    if args.command == "pages":
        quarantined = True if args.quarantined else (False if args.not_quarantined else None)
        where, params, words = page_selector(
            ids=args.id, urls=args.url, domain=args.domain, origin=args.origin,
            introducer=args.introducer, quarantined=quarantined,
        )
        counts = page_counts(where, params)
        print(f"selector: {words}")
        print(render_counts(counts))
        print(render_pages(list_pages(where, params, limit=args.limit)))
        return 0

    if args.command == "claims":
        rows = list_claims(
            page_id=args.page_id, domain=args.domain, introducer=args.introducer,
            origin_user=args.origin_user, limit=args.limit,
        )
        print(f"{len(rows)} claim(s)")
        print(render_claims(rows))
        return 0

    if args.command in ("quarantine", "unquarantine"):
        if not (args.id or args.url or args.domain or args.introducer is not None):
            print("error: give at least one of --id, --url, --domain, --introducer", file=sys.stderr)
            return 2
        where, params, words = _selector_from(args)
        counts = page_counts(where, params)
        print(f"{args.command}: {words}")
        print(render_counts(counts))
        would = counts["pages"] - counts["quarantined"] if args.command == "quarantine" else counts["quarantined"]
        print(f"pages this would change: {would}")
        if not args.yes:
            print("nothing changed — re-run with --yes to apply")
            return 0
        changed = set_quarantine(where, params, quarantine=(args.command == "quarantine"))
        print(f"{args.command}d {changed} page(s)")
        return 0

    if args.command == "purge":
        where, params, words = page_selector(introducer=args.introducer, origin=args.origin)
        counts = page_counts(where, params)
        print(f"purge: {words}")
        print(render_counts(counts))
        if counts["pages"] == 0:
            print("nothing to purge")
            return 0
        if not args.yes:
            print("REFUSED: purge deletes rows permanently; re-run with --yes to apply", file=sys.stderr)
            return 2
        result = purge_pages(where, params)
        print(
            f"deleted web_pages={result['pages']} web_claims={result['claims']} "
            f"web_page_versions={result['versions']}"
        )
        if args.keep_vectors:
            print(
                f"WARNING: --keep-vectors: the {len(result['ids'])} purged page id(s) still have "
                "chunk rows in the web index. Retrieval will not serve them (web_index checks "
                "PostgreSQL for every hit), but they still cost scan time and disk and nothing "
                "else will remove them. Run `python -m tools.reindex_web build` to rebuild "
                "without them."
            )
            return 0
        dropped = drop_vectors(result["ids"])
        if dropped["status"] in ("ok", "empty", "skipped"):
            print(
                f"web index: removed {dropped['removed']} chunk row(s); "
                f"{dropped.get('rows', 0)} remain"
            )
            return 0
        # The rows are already gone from PostgreSQL. Leaving with 0 here would
        # report a clean purge over exactly the orphaned state K7 describes.
        print(
            f"web index: vectors NOT removed ({dropped.get('detail', dropped['status'])}); "
            f"the {len(result['ids'])} purged page id(s) still have chunk rows in the web "
            "index — run `python -m tools.reindex_web build` to rebuild without them",
            file=sys.stderr,
        )
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
