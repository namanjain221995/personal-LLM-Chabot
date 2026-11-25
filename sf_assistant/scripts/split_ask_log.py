#!/usr/bin/env python3
"""Split the ask service's old single-file log into its daily files, once, with the service stopped. Standard library.

    python3 -I scripts/split_ask_log.py                    # dry run (the default): lines per day; writes nothing
    python3 -I scripts/split_ask_log.py --apply            # write the daily files, then keep the old file as backup
    python3 -I scripts/split_ask_log.py --apply --append   # the same into a folder that already holds log files

Options: ``--private DIR`` (default: the ``.private`` folder beside ``scripts/``, so run the copy in the folder the
service runs from), ``--legacy PATH`` (the backup's name, default ``.private/ask_log.legacy.jsonl``), ``--today
YYYY-MM-DD`` and ``--days N`` (only for the notes on what the 90-day rule will delete; default: today in Asia/Kolkata
and ``SFA_ASK_LOG_DAYS`` or 90).

Before 2026-11-25 the service appended to ``.private/ask_log.jsonl`` through a file bind mount; now it writes
``.private/ask_logs/ask_log-YYYY-MM-DD.jsonl`` (``web/ask_log.py``, docs/runbook.md "The ask log"). Run this between the
stop of the old service and the start of the new one, so no line lands in the old file after the split:

- every line goes, in its order, to the daily file of the business date (Asia/Kolkata) of its ``ts``; a line with no
  readable ``ts`` (a blank line, a cut line whose ``ts`` is unreadable, a ``ts`` with no time zone) goes to
  ``ask_log-unknown-date.jsonl``. No line is dropped, changed or reordered; a last line with no newline gets one, so
  the next line written stays a line of its own. The files are mode 600, the folder mode 700;
- after every file is written and read back equal, the old file is kept as the backup ``.private/ask_log.legacy.jsonl``
  (a hard link, then the old name is removed; mode 600). The service never sees it (it mounts only ``ask_logs/``) and
  never deletes it: the note printed here gives the day from which it may be deleted by hand, the day its newest line's
  daily file is deleted by the 90-day rule;
- **the safe rule for a folder that already holds log files** (a daily file or ``ask_log-unknown-date.jsonl``, for
  instance because the new service ran before the split): without ``--append`` the split refuses (exit 2) and writes
  nothing, so it never mixes into files it did not make. With ``--append`` each day's lines are added at the end of
  that day's file with ``O_APPEND`` (a file whose last line has no newline gets one first); nothing there is rewritten,
  truncated or removed, so a line the service wrote meanwhile is never lost. The split's lines of a day then follow the
  lines already in its file, and each keeps its own ``ts``;
- without ``--append`` the files are created with ``O_EXCL``: when anything fails, or the old file changed while it was
  split (the service still running), the files this run created are removed again, the old file stays as it was, and
  the exit code is 1. With ``--append`` nothing can be taken back: the output says which files got lines;
- it refuses (exit 2) an old file that is a link or no regular file, a backup name that already exists, and an
  ``ask_logs`` that is a link or no folder. No old file: nothing to split (exit 0).

It prints file names, dates, counts and sizes only, never a line's text: the log holds real names.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # the sf_assistant folder
sys.path.insert(0, str(ROOT / "web"))
import ask_log  # noqa: E402 - web/ask_log.py: the names, the business date and the days, shared with the writer

SOURCE_NAME = "ask_log.jsonl"
LEGACY_NAME = "ask_log.legacy.jsonl"
#: a cut line's ``ts``: the writer always puts it first
_TS_RE = re.compile(rb'\A\s*\{\s*"ts"\s*:\s*"([^"\\]{1,64})"')
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def line_ts(line: bytes) -> str | None:
    """A line's ``ts`` text: from the JSON object, or for a line that is no JSON (a cut write) from its start."""
    try:
        obj = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        m = _TS_RE.match(line)
        return m.group(1).decode("ascii", "replace") if m else None
    value = obj.get("ts") if isinstance(obj, dict) else None
    return value if isinstance(value, str) else None


def parse_ts(text: str) -> dt.datetime | None:
    """An aware moment, or None: the writer's ``%Y-%m-%dT%H:%M:%S%z`` and the ISO forms; a time with no zone is None."""
    text = text.strip()
    for parse in (lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z"),
                  lambda s: dt.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)):
        try:
            moment = parse(text)
        except (ValueError, TypeError, OverflowError):
            continue
        if moment.tzinfo is not None and moment.utcoffset() is not None:
            return moment
    return None


def target_name(line: bytes) -> str:
    """The file a line goes to: the daily file of its ``ts``'s business date, or the unknown-date file."""
    if not line.strip():
        return ask_log.UNKNOWN_NAME
    raw = line_ts(line)
    moment = parse_ts(raw) if raw else None
    if moment is None:
        return ask_log.UNKNOWN_NAME
    try:
        return ask_log.daily_name(ask_log.business_date(moment))
    except (ValueError, OverflowError):
        return ask_log.UNKNOWN_NAME


def plan(data: bytes) -> tuple[dict[str, list[bytes]], dict]:
    """``({file name: [line + newline, ...] in the old order}, counts)``."""
    pieces = data.split(b"\n")
    cut_last = bool(pieces) and pieces[-1] != b""
    if not cut_last:
        pieces.pop()                       # the empty piece after the last newline is no line
    groups: dict[str, list[bytes]] = {}
    for piece in pieces:
        groups.setdefault(target_name(piece), []).append(piece + b"\n")
    blank = sum(1 for piece in pieces if not piece.strip())
    return groups, {"lines": len(pieces), "blank": blank, "cut_last_line": cut_last}


def log_files(folder: Path) -> list[str]:
    """The log files a folder already holds: daily names and the unknown-date name; nothing else counts."""
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    return sorted(n for n in names if ask_log.daily_date(n) is not None or n == ask_log.UNKNOWN_NAME)


def _read(path: Path) -> tuple[bytes, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        st = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), st
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_ino, a.st_size, a.st_mtime_ns) == (b.st_ino, b.st_size, b.st_mtime_ns)


def _order(name: str) -> tuple[int, str]:
    return (1, name) if name == ask_log.UNKNOWN_NAME else (0, name)


def _report(groups: dict[str, list[bytes]], counts: dict, size: int, source: Path, today: dt.date, days: int) -> None:
    print(f"old log: {source} ({size} bytes, {counts['lines']} lines)")
    for name in sorted(groups, key=_order):
        print(f"  {name}: {len(groups[name])} lines")
    unknown = len(groups.get(ask_log.UNKNOWN_NAME, []))
    if unknown:
        print(f"{unknown} line(s) have no readable ts ({counts['blank']} blank): they go to {ask_log.UNKNOWN_NAME}, "
              "which the service never deletes")
    if counts["cut_last_line"]:
        print("the last line has no newline (a cut write): it is kept and gets one")
    dates = [d for d in (ask_log.daily_date(n) for n in groups) if d is not None]
    cutoff = today - dt.timedelta(days=days)
    old = [d for d in dates if d < cutoff]
    if old:
        print(f"{len(old)} daily file(s) are dated before {cutoff.isoformat()} ({days} days before "
              f"{today.isoformat()}): the service deletes them at its start")
    newest = max(dates) if dates else today
    print(f"the backup (and {ask_log.UNKNOWN_NAME}, if made) may be deleted by hand from "
          f"{(newest + dt.timedelta(days=days + 1)).isoformat()}: its newest line is of {newest.isoformat()}")


def _apply(groups: dict[str, list[bytes]], target: Path, append: bool, source: Path, before: os.stat_result,
           legacy: Path) -> int:
    if not target.exists():
        os.mkdir(target, ask_log.DIR_MODE)
    os.chmod(target, ask_log.DIR_MODE)
    created: list[Path] = []
    appended: list[str] = []
    starts: dict[str, int] = {}

    def undo(why: str) -> int:
        for path in created:
            try:
                os.unlink(path)
            except OSError:
                pass
        if append and appended:
            print(f"{why}. Lines were added to {', '.join(appended)} and stay there; the old file is unchanged",
                  file=sys.stderr)
        else:
            print(f"{why}. The files this run created were removed; the old file is unchanged", file=sys.stderr)
        return 1

    try:
        for name in sorted(groups, key=_order):
            path = target / name
            data = b"".join(groups[name])
            if append:
                fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW, ask_log.FILE_MODE)
            else:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, ask_log.FILE_MODE)
                created.append(path)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(f"{name} is not a regular file")
                os.fchmod(fd, ask_log.FILE_MODE)
                size = os.fstat(fd).st_size
                if append and size and os.pread(fd, 1, size - 1) != b"\n":
                    _write_all(fd, b"\n")              # the service's cut last line ends here; nothing is lost
                    size += 1
                starts[name] = size
                _write_all(fd, data)
                os.fsync(fd)
                if append:
                    appended.append(name)
            finally:
                os.close(fd)
        for name in groups:                              # read back: every line is where it should be
            data = b"".join(groups[name])
            written, _st = _read(target / name)
            if written[starts[name]:starts[name] + len(data)] != data:
                return undo(f"{name} does not read back as written")
        _fsync_dir(target)
        if not _same(before, os.lstat(source)):
            return undo("the old log changed while it was split: is sf-assistant-ask still running? Stop it "
                        "(docker stop sf-assistant-ask) and run this again")
        os.link(source, legacy, follow_symlinks=False)   # EEXIST: a backup of that name is never replaced
    except OSError as exc:
        return undo(f"the split stopped: {type(exc).__name__}: {exc.strerror or exc}")
    try:                                                 # the backup holds every line now: nothing is undone
        os.chmod(legacy, ask_log.FILE_MODE)
        os.unlink(source)
        _fsync_dir(source.parent)
    except OSError as exc:
        print(f"the daily files and the backup {legacy} are written, but the old name could not be removed "
              f"({type(exc).__name__}); remove {source} by hand (it is the same file as the backup)", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="count the lines per day and write nothing (the default)")
    mode.add_argument("--apply", action="store_true", help="write the daily files and keep the old file as the backup")
    p.add_argument("--append", action="store_true",
                   help="also when ask_logs/ already holds log files: add each day's lines at the end of its file")
    p.add_argument("--private", default=str(ROOT / ".private"), help="the .private folder (default: %(default)s)")
    p.add_argument("--legacy", default=None, help=f"the backup's path (default: <private>/{LEGACY_NAME})")
    p.add_argument("--today", default=None, help="today's business date, YYYY-MM-DD (default: today in Asia/Kolkata)")
    p.add_argument("--days", type=int, default=None, help="the retention in days (default: SFA_ASK_LOG_DAYS or 90)")
    a = p.parse_args(argv)

    private = Path(a.private)
    source = private / SOURCE_NAME
    target = private / ask_log.DIR_NAME
    legacy = Path(a.legacy) if a.legacy else private / LEGACY_NAME
    try:
        today = dt.date.fromisoformat(a.today) if a.today else ask_log.business_date(dt.datetime.now().astimezone())
    except ValueError:
        print("--today must be YYYY-MM-DD", file=sys.stderr)
        return 2
    if a.days is None:
        days, problem = ask_log.log_days()
        if problem:
            print(problem, file=sys.stderr)
    elif not 1 <= a.days <= ask_log.DAYS_MAX:
        print(f"--days must be from 1 to {ask_log.DAYS_MAX}", file=sys.stderr)
        return 2
    else:
        days = a.days

    try:
        st = os.lstat(source)
    except FileNotFoundError:
        if os.path.lexists(legacy):
            print(f"nothing to split: no {source}; the backup {legacy} is there (split before)")
        else:
            print(f"nothing to split: no {source}")
        return 0
    if not stat.S_ISREG(st.st_mode):
        print(f"refusing: {source} is a link or no regular file; nothing was written", file=sys.stderr)
        return 2
    if os.path.lexists(legacy):
        print(f"refusing: the backup {legacy} already exists and is never replaced; name another with --legacy. "
              "Nothing was written", file=sys.stderr)
        return 2
    if os.path.lexists(target) and (os.path.islink(target) or not target.is_dir()):
        print(f"refusing: {target} is a link or no folder; nothing was written", file=sys.stderr)
        return 2

    data, before = _read(source)
    if not _same(before, st):
        print(f"refusing: {source} changed while it was read; stop sf-assistant-ask first", file=sys.stderr)
        return 2
    groups, counts = plan(data)
    _report(groups, counts, len(data), source, today, days)
    existing = log_files(target)
    if existing and not a.append:
        print(f"refusing: {target} already holds {len(existing)} log file(s) ({existing[0]} ... {existing[-1]}). The "
              "split writes only into a folder with none; with --append each day's lines are added at the end of its "
              "file. Nothing was written", file=sys.stderr)
        return 2
    both = sorted(set(groups) & set(existing))
    if both:
        print(f"--append: {len(both)} file(s) already there get lines at their end: {', '.join(both)}")
    if not a.apply:
        print(f"dry run: nothing was written. --apply writes {len(groups)} file(s) into {target} and keeps the old "
              f"file as {legacy}")
        return 0
    code = _apply(groups, target, a.append, source, before, legacy)
    if code == 0:
        print(f"done: {counts['lines']} lines in {len(groups)} file(s) in {target}; the old file is now {legacy} "
              "(mode 600). Start the service: ./run.sh restart-ask")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
