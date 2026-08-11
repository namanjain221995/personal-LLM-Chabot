#!/usr/bin/env python3
"""Name the conversations that already exist.

New chats get an AI title automatically. Everything from before still carries
its first-message title — which is why the sidebar reads "hi", "hi", "hello"
and eleven copies of "who is the ceo of techsara s…". This renames them.

    # see what it WOULD do, touching nothing:
    orchestrator/.venv/bin/python orchestrator/scripts/backfill_titles.py --dry-run

    # do it:
    orchestrator/.venv/bin/python orchestrator/scripts/backfill_titles.py

SAFE BY CONSTRUCTION:
  * only conversations whose `title_source` is still 'auto' are considered, so
    anything you renamed yourself is skipped — the same guard the live path
    uses, enforced in the UPDATE statement rather than checked beforehand;
  * a conversation with no real exchange (a bare "hi") keeps its old title
    rather than being renamed to something invented;
  * one conversation at a time, so a long run cannot saturate the router model
    that routing and classification also depend on;
  * --dry-run prints every proposed rename and writes nothing.

RUNNING IT FROM THE HOST needs the router's PUBLISHED port, because
`http://vllm-router:30002` is a compose-network name that does not resolve
outside the containers — the symptom is every conversation "skipped" with a
name-resolution error buried in the logs:

    set -a; . ./.env; set +a
    export APP_DATABASE_URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:5432/$POSTGRES_DB"
    export ROUTER_BASE_URL="http://127.0.0.1:8002/v1"
    orchestrator/.venv/bin/python orchestrator/scripts/backfill_titles.py --dry-run

Re-running is harmless: titles it already set are 'generated' and no longer
match the guard. Budget roughly 3-5 seconds per conversation — the model call
dominates, and they are deliberately sequential.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, titling  # noqa: E402
from app.config import settings  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print, change nothing")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after examining N conversations (0 = no limit). "
                         "Counts WORK, not renames — a limit that only counted "
                         "renames would still call the model for every skipped "
                         "conversation, which is the expensive part.")
    ap.add_argument("--dsn", default=os.environ.get("APP_DATABASE_URL", ""))
    args = ap.parse_args()

    if args.dsn:
        settings.app_database_url = args.dsn
    if not settings.app_database_url:
        ap.error("APP_DATABASE_URL is not set (or pass --dsn)")

    with db.connection() as con:
        rows = con.execute(
            "SELECT c.id, c.user_id, c.title "
            "  FROM conversations c "
            " WHERE c.title_source = 'auto' "
            "   AND EXISTS (SELECT 1 FROM messages m "
            "                WHERE m.conversation_id = c.id AND m.role = 'assistant') "
            " ORDER BY c.updated_at DESC"
        ).fetchall()

    print(f"{len(rows)} conversation(s) still carry an auto-derived title\n")
    renamed = skipped = failed = 0

    for index, row in enumerate(rows):
        if args.limit and index >= args.limit:
            print(f"\nstopping after {args.limit} examined (--limit)")
            break

        messages = db.list_messages(row["id"])
        first_user = next((m for m in messages if m["role"] == "user"), None)
        first_assistant = next((m for m in messages if m["role"] == "assistant"), None)

        title = await titling.generate_title(
            (first_user or {}).get("content", ""),
            (first_assistant or {}).get("content", ""),
        )
        old = (row["title"] or "")[:34]

        if title is None:
            # No subject worth naming, or the model was unavailable. Keeping a
            # dull-but-true title beats inventing one.
            skipped += 1
            print(f"  skip    {old!r}")
            continue

        if args.dry_run:
            renamed += 1
            print(f"  would   {old!r}  ->  {title!r}")
            continue

        if db.set_generated_title(int(row["user_id"]), row["id"], title):
            renamed += 1
            print(f"  renamed {old!r}  ->  {title!r}")
        else:
            # Renamed by the owner between the SELECT and now — their title wins.
            failed += 1
            print(f"  guarded {old!r}  (renamed by hand; left alone)")

    db.close_pool()
    print(f"\n{renamed} renamed, {skipped} left alone, {failed} guarded")
    if args.dry_run:
        print("--dry-run: nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
