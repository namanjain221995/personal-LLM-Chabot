#!/usr/bin/env python3
"""Classify every row of `user_facts` against the memory write rules, and —
only when an operator asks, for one account, with a backup — delete the bad
ones.

    scripts/memory_audit.py --dsn "$APP_DATABASE_URL"                # whole store
    scripts/memory_audit.py --dsn "$APP_DATABASE_URL" --user 29      # one account
    scripts/memory_audit.py --dsn "$APP_DATABASE_URL" --no-rows      # counts only
    scripts/memory_audit.py --dsn "$APP_DATABASE_URL" --user 29 \\
        --delete --yes --backup /path/user-29-facts.json

WHY THIS EXISTS. Release 1 (2026-09-18) stopped NEW pastes from becoming
facts, but every row written before it is still read back on every turn under
"treat as true for this user". One account holds 13 rows from a single
"Interview Simulation" paste — a stranger's name, the role that stranger was
interviewing for, and the script's formatting rules — so the assistant
answered "what is my name?" with the candidate's name and "hi ??" with the
script's 150-180 word self-introduction. The rules cannot repair the past;
somebody has to look at the rows and remove them.

WHAT IT CLASSIFIES AGAINST. The live rules in orchestrator/app/facts.py, not
a copy of them: `own_words` (was the message the person speaking at all?),
`pasted_instructions` (was it a script pasted for the assistant?),
`is_durable` (a one-off task request is not a fact), `ungrounded_in` (a number
or name the message never contained) and `states_profile_attribute` (is this
row the person's identity?). When those rules change, this tool changes with
them.

SAFETY. Read-only is the default and the connection says so to PostgreSQL
(`default_transaction_read_only`), so a bug cannot write. Deleting takes
three flags that no environment variable can supply — `--delete`, `--yes` and
`--user` — plus a `--backup` path, and it prints the exact rows first. The
only statement it ever runs that is not a SELECT is one DELETE against
`user_facts`, scoped to that one user_id and that explicit list of ids.

PRIVACY. Row lines carry the first 80 characters of somebody's saved fact.
That is the point of the tool for the operator running it, and the reason
`--no-rows` exists: an audit that leaves the machine should be counts only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "orchestrator"))

from app import facts  # noqa: E402

#: The classes, worst first. A row is given the FIRST one that fits, so an
#: identity row taken from a document is reported as that and not as a
#: duplicate of the identity it overwrote.
IDENTITY_FROM_DOCUMENT = "identity_from_document"
PASTED_PROMPT = "pasted_prompt"
PASTED_DOCUMENT = "pasted_document"
TASK_REQUEST = "task_request"
UNGROUNDED = "ungrounded"
DUPLICATE = "duplicate"
UNVERIFIABLE = "unverifiable"
OK = "ok"

CLASSES = (
    IDENTITY_FROM_DOCUMENT,
    PASTED_PROMPT,
    PASTED_DOCUMENT,
    TASK_REQUEST,
    UNGROUNDED,
    DUPLICATE,
    UNVERIFIABLE,
    OK,
)

#: What `--delete` removes when the operator names no classes. `unverifiable`
#: is deliberately absent: a row that predates the provenance columns (V40)
#: cannot be judged, and "we cannot check it" is not "it is wrong". `ok` is
#: absent for the obvious reason.
DELETABLE = (
    IDENTITY_FROM_DOCUMENT,
    PASTED_PROMPT,
    PASTED_DOCUMENT,
    TASK_REQUEST,
    UNGROUNDED,
    DUPLICATE,
)

_EXCERPT_CHARS = 80

_SELECT = (
    "SELECT id, user_id, fact, source, source_excerpt, source_conversation_id,"
    "       created_at, updated_at"
    "  FROM user_facts{where} ORDER BY user_id, id"
)


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _from_a_paste(row: dict) -> Optional[str]:
    """Why release 1 would refuse the message this row came from, or None.

    "prompt" when the message handed the assistant a role and its rules (or
    carried a template slot), "document" when it was refused for any other
    reason — its length, or the ALL-CAPS/e-mail layout of a pasted CV. None
    when the message is the person speaking, or when there is no excerpt to
    judge.
    """
    excerpt = _text(row.get("source_excerpt"))
    if not excerpt:
        return None
    if facts.own_words(excerpt) is not None:
        return None
    return "prompt" if facts.pasted_instructions(excerpt) else "document"


def classify(row: dict) -> str:
    """The one class this row belongs to, judged from the row alone.

    Duplication is the one class a single row cannot show; `audit` adds it
    afterwards, over the rows nothing else caught.
    """
    fact = _text(row.get("fact"))
    excerpt = _text(row.get("source_excerpt"))
    paste = _from_a_paste(row)
    identity = facts.states_profile_attribute(fact)

    if identity and paste:
        return IDENTITY_FROM_DOCUMENT
    if paste == "prompt" or facts.pasted_instructions(fact):
        return PASTED_PROMPT
    if paste == "document":
        return PASTED_DOCUMENT
    if not facts.is_durable(fact):
        return TASK_REQUEST
    if excerpt and facts.ungrounded_in(fact, excerpt) is not None:
        return UNGROUNDED
    if not _text(row.get("source")):
        return UNVERIFIABLE
    return OK


def _duplicates(rows: Sequence[dict]) -> List[int]:
    """The ids of rows that repeat something the account already says.

    Two shapes. The same sentence twice — the unique index is on `lower(fact)`
    alone, so a trailing full stop or a doubled space makes a second row of
    the same fact. And the same part of the profile stated twice ("The user's
    name is X", "The user's name is Y"), which is how a pasted CV leaves an
    account with two names. The FIRST row of each group is kept: it is the
    older one (rows are read in id order), the one the person most likely
    wrote themselves.

    Only rows no other class caught are offered here, so the CV's name — a
    row already bound for deletion — cannot make the person's OWN name the
    duplicate of it.
    """
    seen_text: dict = {}
    seen_attribute: dict = {}
    duplicates: List[int] = []
    for row in rows:
        fact = _text(row.get("fact"))
        key = " ".join(fact.lower().split()).rstrip(".")
        attribute = facts.states_profile_attribute(fact)
        if key in seen_text or (attribute and attribute in seen_attribute):
            duplicates.append(row["id"])
            continue
        seen_text[key] = row["id"]
        if attribute:
            seen_attribute[attribute] = row["id"]
    return duplicates


def audit(rows: Sequence[dict]) -> List[dict]:
    """Every row with a `class`, account by account (duplicates are only
    duplicates WITHIN one account)."""
    out = [{**row, "class": classify(row)} for row in rows]
    by_id = {row["id"]: row for row in out}
    by_user: dict = {}
    for row in out:
        if row["class"] in (OK, UNVERIFIABLE):
            by_user.setdefault(row["user_id"], []).append(row)
    for account in by_user.values():
        for fact_id in _duplicates(account):
            by_id[fact_id]["class"] = DUPLICATE
    return out


def counts(classified: Sequence[dict]) -> dict:
    """{class: n} over `classified`, every class present, zeros included."""
    tally = {name: 0 for name in CLASSES}
    for row in classified:
        tally[row["class"]] += 1
    return tally


# --- reporting --------------------------------------------------------------


def _summary_lines(classified: Sequence[dict], show_rows: bool) -> List[str]:
    users = sorted({row["user_id"] for row in classified})
    lines = [
        f"{len(classified)} rows, {len(users)} account(s)",
        "",
        "per account:",
    ]
    for user_id in users:
        account = [r for r in classified if r["user_id"] == user_id]
        tally = counts(account)
        detail = "  ".join(
            f"{name} {n}" for name, n in tally.items() if n
        )
        lines.append(f"  user {user_id:<6} {len(account):>4} rows   {detail}")
    lines += ["", "totals:"]
    for name, n in counts(classified).items():
        lines.append(f"  {name:<24} {n:>5}")
    if show_rows:
        lines += ["", f"rows (first {_EXCERPT_CHARS} characters):"]
        for row in classified:
            head = " ".join(_text(row["fact"]).split())[:_EXCERPT_CHARS]
            lines.append(
                f"  [{row['id']:>7}] user {row['user_id']:<6} "
                f"{row['class']:<24} {head}"
            )
    return lines


# --- database ---------------------------------------------------------------


def _connect(dsn: str, *, read_only: bool):
    import psycopg

    con = psycopg.connect(dsn, connect_timeout=10)
    if read_only:
        # Belt as well as braces: the tool issues only SELECTs on this path,
        # and the server refuses anything else even if that stops being true.
        # It must be psycopg's `read_only`, which opens each transaction READ
        # ONLY. `SET default_transaction_read_only = on` looks equivalent and
        # is not: it only affects transactions started AFTER it, and psycopg
        # has already opened one to run the SET — measured 2026-09-21, a
        # DELETE went straight through that version of this guard.
        con.read_only = True
    return con


def _rows(con, user_id: Optional[int]) -> List[dict]:
    where, params = ("", ())
    if user_id is not None:
        where, params = (" WHERE user_id = %s", (user_id,))
    cur = con.execute(_SELECT.format(where=where), params)
    columns = [c.name for c in cur.description]
    return [dict(zip(columns, values)) for values in cur.fetchall()]


def _backup(path: str, rows: Sequence[dict]) -> None:
    """Every column of every row about to go, as JSON, before anything is
    deleted. Refuses to overwrite: a second run must not eat the first run's
    only copy of the rows."""
    if os.path.exists(path):
        raise SystemExit(f"refusing to overwrite an existing backup: {path}")
    payload = [
        {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
        for row in rows
    ]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _delete(con, user_id: int, ids: Sequence[int]) -> int:
    """The only statement this tool runs that is not a SELECT. One table, one
    user, an explicit list of ids."""
    cur = con.execute(
        "DELETE FROM user_facts WHERE user_id = %s AND id = ANY(%s) RETURNING id",
        (user_id, list(ids)),
    )
    removed = len(cur.fetchall())
    con.commit()
    return removed


# --- command line -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory_audit.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("APP_DATABASE_URL", "").strip(),
        help="PostgreSQL DSN (default: $APP_DATABASE_URL)",
    )
    parser.add_argument("--user", type=int, default=None, help="one account's user id")
    parser.add_argument(
        "--no-rows",
        dest="rows",
        action="store_false",
        help="counts only — no fragment of anybody's saved facts is printed",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="remove the classified rows (requires --yes, --user and --backup)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="the operator's confirmation; --delete does nothing without it",
    )
    parser.add_argument("--backup", default=None, help="where to write the deleted rows as JSON")
    parser.add_argument(
        "--classes",
        default=",".join(DELETABLE),
        help="which classes --delete removes (default: %(default)s)",
    )
    return parser


def _delete_classes(raw: str) -> List[str]:
    chosen = [c.strip() for c in (raw or "").split(",") if c.strip()]
    unknown = [c for c in chosen if c not in CLASSES]
    if unknown:
        raise SystemExit(f"unknown class(es): {', '.join(unknown)}")
    refused = [c for c in chosen if c in (OK, UNVERIFIABLE)]
    if refused:
        raise SystemExit(
            f"refusing to delete class(es) {', '.join(refused)}: an unjudgeable "
            "or clean row is not a bad row"
        )
    if not chosen:
        raise SystemExit("--classes named nothing to delete")
    return chosen


def check_delete_request(args) -> List[str]:
    """What `--delete` needs before it may touch anything, or SystemExit.

    Read-only is the default and stays the default: deletion is never reached
    by an environment variable, a config file or a bare `--delete`. It takes
    the flag, the operator's `--yes`, ONE named account and somewhere to put
    the backup — four things a person types on purpose.
    """
    if not args.delete:
        return []
    missing = []
    if not args.yes:
        missing.append("--yes")
    if args.user is None:
        missing.append("--user <id>")
    if not args.backup:
        missing.append("--backup <path>")
    if missing:
        raise SystemExit(
            "refusing to delete: " + ", ".join(missing) + " required. "
            "This tool never deletes unattended."
        )
    return _delete_classes(args.classes)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    classes = check_delete_request(args)
    if not args.dsn:
        raise SystemExit("no database: pass --dsn or set APP_DATABASE_URL")

    with _connect(args.dsn, read_only=not args.delete) as con:
        classified = audit(_rows(con, args.user))
        mode = "DELETE" if args.delete else "read-only"
        print(f"memory_audit — {mode}")
        print("\n".join(_summary_lines(classified, args.rows)))
        if not args.delete:
            return 0

        doomed = [r for r in classified if r["class"] in classes]
        print("")
        print(f"would delete {len(doomed)} of {len(classified)} rows for user {args.user}:")
        for row in doomed:
            head = " ".join(_text(row["fact"]).split())[:_EXCERPT_CHARS]
            print(f"  [{row['id']:>7}] {row['class']:<24} {head}")
        if not doomed:
            print("nothing to delete.")
            return 0
        _backup(args.backup, doomed)
        print(f"backup written: {args.backup}")
        removed = _delete(con, args.user, [r["id"] for r in doomed])
        print(f"deleted {removed} rows from user_facts.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
