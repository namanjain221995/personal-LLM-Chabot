"""The ask service's log, one file per business day: ``.private/ask_logs/ask_log-YYYY-MM-DD.jsonl``. Standard
library only.

Option R2 of the conversation-memory design (2026-11-25, owner decision D5; the approved retention: the ask log is kept
90 days and rotated daily). Before it, the service appended to one file, ``.private/ask_log.jsonl``, through a file bind
mount, and nothing rotated it: a host ``mv`` leaves a file bind mount on the old inode, so the container would have kept
writing into the renamed file. Now ``web/compose.ask.yaml`` mounts the folder ``.private/ask_logs`` (mode 700) and
:class:`RotatingAskLog` picks the file of each line itself.

- **The file of a line** is named by the business date of the moment its ``ts`` stamp records, in
  :data:`BUSINESS_TZ_NAME` (Asia/Kolkata), whatever the container's own time zone: ``ts`` keeps the old form (the
  container's local time with its offset, ``2026-11-25T18:29:59+0000``), and that instant falls on 2026-11-25 there,
  while ``2026-11-25T18:30:00+0000`` already belongs to the file of 2026-11-26. Reading a line's ``ts`` back always
  gives the date of the file it is in: an event's own ``ts`` key never replaces the stamp.
- **Each line** is one ``O_APPEND`` write of the whole line under a thread lock, as before. The file is opened with
  ``O_NOFOLLOW`` (a symbolic link named like a daily file is never written through) and created with mode 600; a file
  found with another mode is set back to 600.
- **The folder** is created with mode 700 by :meth:`RotatingAskLog.check` when it is missing and its parent exists,
  and set back to 700 when it has another mode. A symbolic link there is refused.
- **Deletion** (:meth:`RotatingAskLog.prune`): at the start (:meth:`~RotatingAskLog.check`) and at the first write of
  each business day, the files whose name is exactly ``ask_log-YYYY-MM-DD.jsonl`` (:data:`DAILY_RE`, ASCII digits, a
  real calendar date) and whose date is before today minus :attr:`~RotatingAskLog.days` days are deleted. Nothing else is ever
  deleted: not :data:`UNKNOWN_NAME`, not a file with any other name, not a link, not a folder. With 90 days and today
  2026-11-25, the files of 2026-08-27 to 2026-11-25 stay and 2026-08-26 and older go: a line is kept 90 to 91 days
  while the service writes every day. A service that writes nothing deletes nothing until its next start or write, so
  a file is kept at least 90 days and is deleted at the first start or write after that.
- **Never raises** from :meth:`~RotatingAskLog.write` or :meth:`~RotatingAskLog.prune`: a fault in the log must not
  cost a finished answer. A fault prints the class name only to stderr (the text can hold names), and a stderr that
  cannot be written is ignored.
  :meth:`~RotatingAskLog.check` raises ``OSError`` when the log cannot be appended to, so the service refuses to start.

The days come from ``SFA_ASK_LOG_DAYS`` (:func:`log_days`; default :data:`DAYS_DEFAULT`, the approved 90), unless the
caller passes ``days``. A value that is not a whole number from 1 to :data:`DAYS_MAX` keeps 90 and says so once on
stderr; changing the 90 days is the owner's decision.

:class:`RotatingAskLog` is a drop-in for ``web/ask_mcp.py``'s ``AskLog``: the same ``path`` attribute (here the
folder, read at each write), ``check()`` and ``write(event)``, the same line (``{"ts": ..., **event}``, and on a value
JSON cannot hold, the fixed words with ``log_fault``) and the same stderr text on a fault. ``scripts/split_ask_log.py``
splits the old single file into these daily files once, with the service stopped (docs/runbook.md, "The ask log").
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import sys
import threading
from typing import Callable, Mapping

BUSINESS_TZ_NAME = "Asia/Kolkata"
#: the folder in ``.private`` that ``web/compose.ask.yaml`` mounts read-write, and ``run.sh`` creates (mode 700)
DIR_NAME = "ask_logs"
#: a daily file's name, exactly, in ASCII digits (the writer's own names); the only names
#: :meth:`RotatingAskLog.prune` deletes. Without ``re.ASCII`` ``\d`` and ``int`` would read Arabic-Indic,
#: Devanagari or full-width digits too, and a file the writer never makes would be deleted (stage-1 review fix F5)
DAILY_RE = re.compile(r"ask_log-([0-9]{4})-([0-9]{2})-([0-9]{2})\.jsonl", re.ASCII)
#: the split's file for lines with no readable ``ts`` (scripts/split_ask_log.py); never written or deleted here
UNKNOWN_NAME = "ask_log-unknown-date.jsonl"
DAYS_ENV_VAR = "SFA_ASK_LOG_DAYS"
DAYS_DEFAULT = 90              # the approved retention (owner, 2026-11-25)
DAYS_MAX = 3650
FILE_MODE = 0o600
DIR_MODE = 0o700
#: O_NONBLOCK: a FIFO named like a daily file fails at once instead of holding the lock (no effect on a regular file)
_OPEN_FLAGS = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_NONBLOCK", 0))
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _business_tz() -> dt.tzinfo:
    """Asia/Kolkata from the time zone database; without one, the fixed +05:30 it equals (India has no summer time)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(BUSINESS_TZ_NAME)
    except Exception:  # noqa: BLE001 - no tzdata in this Python: the offset is the same all year
        return dt.timezone(dt.timedelta(hours=5, minutes=30), BUSINESS_TZ_NAME)


BUSINESS_TZ = _business_tz()


def business_date(moment: dt.datetime) -> dt.date:
    """The business date (Asia/Kolkata) of an aware moment. Raises ValueError for a naive one: its zone is unknown."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("a moment with no time zone has no business date")
    return moment.astimezone(BUSINESS_TZ).date()


def daily_name(day: dt.date) -> str:
    """``ask_log-2026-11-25.jsonl``."""
    return f"ask_log-{day.isoformat()}.jsonl"


def daily_date(name: str) -> dt.date | None:
    """The date of a daily file's name, or None for any other name (also ``ask_log-2026-02-30.jsonl``)."""
    m = DAILY_RE.fullmatch(name)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def stamp(moment: dt.datetime) -> str:
    """A line's ``ts``, in the old writer's form (``time.strftime("%Y-%m-%dT%H:%M:%S%z")``)."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S%z")


def log_days(environ: Mapping[str, str] | None = None) -> tuple[int, str | None]:
    """``(days, problem)`` from ``SFA_ASK_LOG_DAYS``: unset or blank gives 90; a whole number from 1 to
    :data:`DAYS_MAX` gives itself; anything else gives 90 and a problem text (the value itself is not repeated)."""
    raw = (os.environ if environ is None else environ).get(DAYS_ENV_VAR)
    if raw is None or not raw.strip():
        return DAYS_DEFAULT, None
    text = raw.strip()
    if text.isascii() and text.isdigit() and 1 <= int(text) <= DAYS_MAX:
        return int(text), None
    return DAYS_DEFAULT, (f"{DAYS_ENV_VAR} must be a whole number of days from 1 to {DAYS_MAX}; "
                          f"the ask log keeps {DAYS_DEFAULT} days")


def _local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _say(line: str) -> None:
    """One line on stderr. Never raises: a broken stderr (a closed pipe of the container log) must not turn a fault of
    the log into an exception in the answer path (stage-1 review fix F10)."""
    try:
        print(f"sf-assistant-ask: {line}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - nowhere left to say it
        pass


class RotatingAskLog:
    """``.private/ask_logs/ask_log-YYYY-MM-DD.jsonl``, one file per business day, mode 600 in a folder of mode 700.
    Real names. ``clock`` (tests) returns the aware moment of each line."""

    def __init__(self, path: str, *, days: int | None = None,
                 clock: Callable[[], dt.datetime] | None = None) -> None:
        if days is None:
            days, problem = log_days()
            if problem:
                _say(problem)
        elif isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= DAYS_MAX:
            raise ValueError(f"days must be a whole number from 1 to {DAYS_MAX}")
        self.path = str(path)          # the folder; read at each write, as AskLog reads its file path
        self.days = days
        self._clock = clock or _local_now
        self._lock = threading.Lock()
        self._pruned_for: dt.date | None = None   # the business date of the last deletion pass

    # ---- names
    def file_for(self, day: dt.date) -> str:
        return os.path.join(self.path, daily_name(day))

    def current_file(self) -> str:
        """Today's file (by the clock), whether or not it exists yet."""
        return self.file_for(business_date(self._clock()))

    # ---- the interface of AskLog
    def check(self) -> None:
        """Raises OSError when the log cannot be appended to. Creates the folder (mode 700) when it is missing and its
        parent exists, deletes the daily files past the limit (the start's pass) and creates today's file (mode 600)."""
        with self._lock:
            self._ensure_dir()
            today = business_date(self._clock())
            self._prune(today)
            self._pruned_for = today
            os.close(self._open(today))

    def write(self, event: dict) -> None:
        """Append one line to the file of its business date. Never raises: a fault in the log must not cost a finished
        answer. A value JSON cannot hold costs the line its text, and the fixed words stay. The first write of each
        business day first deletes the daily files past the limit."""
        try:
            moment = self._clock()
            ts = stamp(moment)
            try:
                # the writer's stamp always: an event's own "ts" never replaces it, so a line's ts reads back to the
                # date of its file (stage-1 review fix F11); "ts" stays the first key
                line = json.dumps({"ts": ts, **{k: v for k, v in event.items() if k != "ts"}}, ensure_ascii=False,
                                  default=str)
            except Exception as exc:  # noqa: BLE001
                line = json.dumps({"ts": ts, "event": str(event.get("event")), "status": str(event.get("status")),
                                   "log_fault": type(exc).__name__})
            data = (line + "\n").encode("utf-8", "backslashreplace")
            day = business_date(moment)
            with self._lock:
                if day != self._pruned_for:
                    self._pruned_for = day
                    try:
                        self._ensure_dir()
                    except OSError:
                        pass                                   # the open below says so
                    self._prune(day)
                try:
                    fd = self._open(day)
                except FileNotFoundError:                      # the folder went away (or never came): make it again
                    self._ensure_dir()
                    fd = self._open(day)
                try:
                    view = memoryview(data)
                    while view:                                # one write for a regular file; a short one goes on
                        view = view[os.write(fd, view):]
                finally:
                    os.close(fd)
        except Exception as exc:  # noqa: BLE001 - the class name only: the text can hold names
            _say(f"could not write the log: {type(exc).__name__}")

    # ---- deletion
    def prune(self, today: dt.date | None = None) -> int:
        """Delete the daily files dated before ``today`` (default: the clock's business date) minus :attr:`days` days;
        the number deleted. Never raises."""
        try:
            with self._lock:
                return self._prune(today if today is not None else business_date(self._clock()))
        except Exception as exc:  # noqa: BLE001
            _say(f"could not clean up the log folder: {type(exc).__name__}")
            return 0

    def _prune(self, today: dt.date) -> int:
        cutoff = today - dt.timedelta(days=self.days)
        deleted = 0
        try:
            dfd = os.open(self.path, _DIR_FLAGS)
        except OSError:
            return 0                                           # no folder: nothing to delete; a write says so
        try:
            for name in os.listdir(dfd):
                day = daily_date(name)
                if day is None or day >= cutoff:
                    continue
                try:
                    if not stat.S_ISREG(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode):
                        continue                               # a link or a folder with that name is not ours
                    os.unlink(name, dir_fd=dfd)
                    deleted += 1
                except OSError:
                    continue
        except Exception as exc:  # noqa: BLE001
            _say(f"could not clean up the log folder: {type(exc).__name__}")
        finally:
            os.close(dfd)
        if deleted:   # counts and a date only
            _say(f"the ask log deleted {deleted} daily file(s) dated before {cutoff.isoformat()} "
                 f"({DAYS_ENV_VAR}={self.days})")
        return deleted

    # ---- files
    def _ensure_dir(self) -> None:
        """The folder exists, is no link, and has mode 700; raises OSError otherwise."""
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            os.mkdir(self.path, DIR_MODE)                      # FileNotFoundError again when its parent is missing
            st = os.lstat(self.path)
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(f"{self.path} is not a folder")
        if stat.S_IMODE(st.st_mode) != DIR_MODE:
            os.chmod(self.path, DIR_MODE)

    def _open(self, day: dt.date) -> int:
        """Today's file, opened for appending through the folder opened with ``O_NOFOLLOW``: a link in place of the
        folder or of the file is never written through. FileNotFoundError when the folder is missing."""
        dfd = os.open(self.path, _DIR_FLAGS)
        try:
            fd = os.open(daily_name(day), _OPEN_FLAGS, FILE_MODE, dir_fd=dfd)
        finally:
            os.close(dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(f"{daily_name(day)} is not a regular file")
            if stat.S_IMODE(st.st_mode) != FILE_MODE:
                os.fchmod(fd, FILE_MODE)
        except BaseException:
            os.close(fd)
            raise
        return fd
