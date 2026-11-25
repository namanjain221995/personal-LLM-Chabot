"""The ask log of 2026-11-25 (option R2): web/ask_log.py writes one file per business day and deletes its own daily
files past the limit; scripts/split_ask_log.py splits the old single file once; run.sh and compose.ask.yaml give the
service the folder. Made-up lines and names only, in pytest's temporary folders; no container, no docker, no model.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SCRIPT = ROOT / "scripts" / "split_ask_log.py"
sys.path.insert(0, str(WEB))

import ask_log  # noqa: E402


def _load_split():
    spec = importlib.util.spec_from_file_location("split_ask_log", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


split = _load_split()

UTC = dt.timezone.utc
NAME = "Echo One"                                   # a made-up person: never in any output of the tools
FIXED_PROBLEM = "SFA_ASK_LOG_DAYS must be a whole number of days from 1 to 3650; the ask log keeps 90 days"


@pytest.fixture(autouse=True)
def _no_days_setting(monkeypatch):
    """An SFA_ASK_LOG_DAYS from the developer's shell never reaches a test; a test that wants one sets it."""
    monkeypatch.delenv("SFA_ASK_LOG_DAYS", raising=False)


class Clock:
    """A settable clock: the aware moment of the next line."""

    def __init__(self, text: str) -> None:
        self.moment = dt.datetime.fromisoformat(text)

    def set(self, text: str) -> None:
        self.moment = dt.datetime.fromisoformat(text)

    def __call__(self) -> dt.datetime:
        return self.moment


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def lines_of(path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]


def make_log(tmp_path, when="2026-11-25T10:00:00+00:00", days=90):
    clock = Clock(when)
    folder = tmp_path / "ask_logs"
    return ask_log.RotatingAskLog(str(folder), days=days, clock=clock), clock, folder


def touch(folder: Path, name: str, text: str = "{}\n") -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


# ------------------------------------------------------------------------------------------------ names and dates
def test_the_business_date_is_kolkata_whatever_the_zone_of_the_moment():
    assert ask_log.business_date(dt.datetime(2026, 11, 25, 18, 29, 59, tzinfo=UTC)) == dt.date(2026, 11, 25)
    assert ask_log.business_date(dt.datetime(2026, 11, 25, 18, 30, 0, tzinfo=UTC)) == dt.date(2026, 11, 26)
    new_york = dt.timezone(dt.timedelta(hours=-5))
    assert ask_log.business_date(dt.datetime(2026, 11, 25, 13, 30, tzinfo=new_york)) == dt.date(2026, 11, 26)
    with pytest.raises(ValueError):
        ask_log.business_date(dt.datetime(2026, 11, 25, 10, 0))                   # no zone: no business date


def test_daily_names_and_only_real_dates_parse():
    assert ask_log.daily_name(dt.date(2026, 1, 5)) == "ask_log-2026-01-05.jsonl"
    assert ask_log.daily_date("ask_log-2026-01-05.jsonl") == dt.date(2026, 1, 5)
    for other in ("ask_log-2026-02-30.jsonl", "ask_log-2026-1-05.jsonl", "ask_log-2026-01-05.jsonl.bak",
                  "xask_log-2026-01-05.jsonl", "ask_log-2026-01-05.json", ask_log.UNKNOWN_NAME,
                  "ask_log.legacy.jsonl", "ask_log.jsonl", "ask_log-2026-01-05.jsonl\n"):
        assert ask_log.daily_date(other) is None, other


def test_the_days_come_from_the_setting_and_a_bad_value_keeps_90(monkeypatch):
    monkeypatch.delenv("SFA_ASK_LOG_DAYS", raising=False)
    assert ask_log.log_days() == (90, None)
    for value, days in (("", 90), ("  ", 90), ("30", 30), (" 7 ", 7), ("1", 1), ("3650", 3650)):
        assert ask_log.log_days({"SFA_ASK_LOG_DAYS": value}) == (days, None), value
    for bad in ("0", "-5", "abc", "1e3", "3651", "9.5", "٣"):
        days, problem = ask_log.log_days({"SFA_ASK_LOG_DAYS": bad})
        assert days == 90 and problem == FIXED_PROBLEM, bad                     # the value itself is never repeated


def test_a_bad_setting_says_so_once_and_keeps_90(monkeypatch, capfd, tmp_path):
    monkeypatch.setenv("SFA_ASK_LOG_DAYS", "forever")
    log = ask_log.RotatingAskLog(str(tmp_path / "ask_logs"))
    assert log.days == 90
    err = capfd.readouterr().err
    assert err.count("SFA_ASK_LOG_DAYS must be") == 1 and "forever" not in err
    monkeypatch.setenv("SFA_ASK_LOG_DAYS", "14")
    assert ask_log.RotatingAskLog(str(tmp_path / "ask_logs")).days == 14


@pytest.mark.parametrize("days", [0, -1, 3651, True, 1.5, "90"])
def test_a_bad_days_argument_is_refused(tmp_path, days):
    with pytest.raises(ValueError):
        ask_log.RotatingAskLog(str(tmp_path), days=days)


# ------------------------------------------------------------------------------------------------ the writer
def test_each_line_goes_to_the_file_of_its_business_date(tmp_path):
    log, clock, folder = make_log(tmp_path, "2026-11-25T18:29:59+00:00")
    log.check()
    log.write({"event": "ask", "question": f"How many interviews did {NAME} have?", "status": "answered"})
    clock.set("2026-11-25T18:30:00+00:00")
    log.write({"event": "ask", "question": "And last month?", "status": "answered"})
    assert sorted(os.listdir(folder)) == ["ask_log-2026-11-25.jsonl", "ask_log-2026-11-26.jsonl"]
    first, second = lines_of(folder / "ask_log-2026-11-25.jsonl"), lines_of(folder / "ask_log-2026-11-26.jsonl")
    assert [ln["question"] for ln in first] == [f"How many interviews did {NAME} have?"]
    assert [ln["question"] for ln in second] == ["And last month?"]
    # the old writer's form of ts (the container's local time with its offset); read back, it gives the file's date
    assert first[0]["ts"] == "2026-11-25T18:29:59+0000" and second[0]["ts"] == "2026-11-25T18:30:00+0000"
    for name, rows in (("ask_log-2026-11-25.jsonl", first), ("ask_log-2026-11-26.jsonl", second)):
        moment = dt.datetime.strptime(rows[0]["ts"], "%Y-%m-%dT%H:%M:%S%z")
        assert ask_log.daily_name(ask_log.business_date(moment)) == name
    assert list(first[0])[0] == "ts"                                            # ts first, then the event's keys


def test_a_container_in_another_zone_still_writes_by_the_kolkata_date(tmp_path):
    log, clock, folder = make_log(tmp_path, "2026-11-25T13:30:00-05:00")       # 00:00 on 2026-11-26 in Kolkata
    log.write({"event": "ask", "status": "answered"})
    assert os.listdir(folder) == ["ask_log-2026-11-26.jsonl"]
    assert lines_of(folder / "ask_log-2026-11-26.jsonl")[0]["ts"] == "2026-11-25T13:30:00-0500"


def test_files_are_600_and_the_folder_700_whatever_the_umask_or_what_was_there(tmp_path):
    before = os.umask(0o002)
    try:
        log, _clock, folder = make_log(tmp_path)
        log.check()                                                              # creates the folder
        assert mode(folder) == 0o700 and mode(folder / "ask_log-2026-11-25.jsonl") == 0o600
        folder.chmod(0o755)
        (folder / "ask_log-2026-11-25.jsonl").chmod(0o644)
        log.check()
        assert mode(folder) == 0o700 and mode(folder / "ask_log-2026-11-25.jsonl") == 0o600
        (folder / "ask_log-2026-11-25.jsonl").chmod(0o640)
        log.write({"event": "ask", "status": "answered"})
        assert mode(folder / "ask_log-2026-11-25.jsonl") == 0o600
    finally:
        os.umask(before)


def test_lines_are_appended_in_order_and_whole_from_many_threads(tmp_path):
    log, _clock, folder = make_log(tmp_path)
    log.check()
    big = "x" * 20000                                                           # longer than a pipe's buffer

    def worker(n):
        for i in range(40):
            log.write({"event": "ask", "worker": n, "i": i, "answer": big})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = lines_of(folder / "ask_log-2026-11-25.jsonl")
    assert len(rows) == 240 and all(r["answer"] == big for r in rows)
    for n in range(6):
        assert [r["i"] for r in rows if r["worker"] == n] == list(range(40))     # each thread's lines in its order


def test_the_writer_appends_to_what_is_there_and_never_truncates(tmp_path):
    log, _clock, folder = make_log(tmp_path)
    folder.mkdir(mode=0o700)
    touch(folder, "ask_log-2026-11-25.jsonl", '{"ts": "2026-11-25T01:00:00+0000", "event": "split"}\n')
    log.check()
    log.write({"event": "ask", "status": "answered"})
    assert [r["event"] for r in lines_of(folder / "ask_log-2026-11-25.jsonl")] == ["split", "ask"]


def test_a_value_json_cannot_hold_costs_the_line_its_text_and_keeps_the_fixed_words(tmp_path):
    log, _clock, folder = make_log(tmp_path)
    log.write({"event": "ask", "status": "answered", "question": NAME, "weird": {("tuple", NAME): 1}})
    (row,) = lines_of(folder / "ask_log-2026-11-25.jsonl")
    assert row == {"ts": "2026-11-25T10:00:00+0000", "event": "ask", "status": "answered", "log_fault": "TypeError"}


def test_half_a_surrogate_pair_is_written_escaped_not_lost(tmp_path):
    log, _clock, folder = make_log(tmp_path)
    log.write({"event": "ask", "answer": "a\ud800b"})
    raw = (folder / "ask_log-2026-11-25.jsonl").read_bytes()
    assert raw.endswith(b"\n") and b"\\ud800" in raw and raw.decode("utf-8")


def test_the_check_creates_a_missing_folder_and_raises_when_its_parent_is_missing(tmp_path):
    with pytest.raises(OSError):
        ask_log.RotatingAskLog(str(tmp_path / "nowhere" / "ask_logs"), days=90).check()
    assert not (tmp_path / "nowhere").exists()
    log, _clock, folder = make_log(tmp_path)
    log.check()
    assert folder.is_dir() and (folder / "ask_log-2026-11-25.jsonl").is_file()
    assert log.current_file() == str(folder / "ask_log-2026-11-25.jsonl")


def test_the_check_refuses_a_link_or_a_file_where_the_folder_should_be(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / "ask_logs").symlink_to(real)
    with pytest.raises(OSError):
        ask_log.RotatingAskLog(str(tmp_path / "ask_logs"), days=90).check()
    assert os.listdir(real) == []
    (tmp_path / "plain").write_text("x")
    with pytest.raises(OSError):
        ask_log.RotatingAskLog(str(tmp_path / "plain"), days=90).check()


def test_a_link_put_in_place_of_the_folder_later_is_never_written_through(tmp_path, capfd):
    log, clock, folder = make_log(tmp_path)
    log.check()
    log.write({"event": "ask"})
    real = tmp_path / "elsewhere"
    real.mkdir()
    folder.rename(tmp_path / "moved")
    folder.symlink_to(real)
    log.write({"event": "ask", "question": NAME})                               # same day: no pass, only the open
    clock.set("2026-11-26T10:00:00+00:00")
    log.write({"event": "ask", "question": NAME})                               # a new day: the pass and the open
    assert os.listdir(real) == [] and folder.is_symlink()
    err = capfd.readouterr().err
    assert err.count("could not write the log: NotADirectoryError") == 2 and NAME not in err   # Linux: ENOTDIR


def test_the_writer_never_raises_and_says_only_the_class_name(tmp_path, capfd):
    log, clock, folder = make_log(tmp_path)
    log.path = str(tmp_path / "nowhere" / "ask_logs")                          # a folder that cannot be made
    log.write({"event": "ask", "question": f"Where does {NAME} work?"})
    err = capfd.readouterr().err
    assert "could not write the log: FileNotFoundError" in err and NAME not in err
    clock.moment = dt.datetime(2026, 11, 25, 10, 0)                              # no zone: no business date
    log.write({"event": "ask", "question": NAME})
    assert "could not write the log: ValueError" in capfd.readouterr().err
    log.path = str(folder)
    clock.set("2026-11-25T10:00:00+00:00")
    log.write({"event": "ask"})                                                  # and it goes on afterwards
    assert len(lines_of(folder / "ask_log-2026-11-25.jsonl")) == 1


def test_a_link_named_like_todays_file_is_never_written_through(tmp_path, capfd):
    log, _clock, folder = make_log(tmp_path)
    folder.mkdir(mode=0o700)
    victim = tmp_path / "victim.txt"
    victim.write_text("kept")
    (folder / "ask_log-2026-11-25.jsonl").symlink_to(victim)
    log.write({"event": "ask", "question": NAME})
    assert victim.read_text() == "kept" and (folder / "ask_log-2026-11-25.jsonl").is_symlink()
    err = capfd.readouterr().err
    assert "could not write the log" in err and NAME not in err


def test_a_fifo_named_like_todays_file_fails_at_once_instead_of_holding_the_lock(tmp_path):
    log, _clock, folder = make_log(tmp_path)
    folder.mkdir(mode=0o700)
    os.mkfifo(folder / "ask_log-2026-11-25.jsonl")
    done = threading.Event()
    threading.Thread(target=lambda: (log.write({"event": "ask"}), done.set()), daemon=True).start()
    assert done.wait(5)


# ------------------------------------------------------------------------------------------------ deletion
def _old_and_other_files(folder: Path) -> dict[str, Path]:
    folder.mkdir(mode=0o700, exist_ok=True)
    outside = folder.parent / "outside.jsonl"
    outside.write_text("not ours")
    made = {name: touch(folder, name) for name in (
        "ask_log-2026-08-25.jsonl", "ask_log-2026-08-26.jsonl",         # before 2026-11-25 minus 90 days: deleted
        "ask_log-2026-08-27.jsonl", "ask_log-2026-11-24.jsonl",         # inside the 90 days: kept
        "ask_log-2026-12-01.jsonl",                                     # a later date: kept
        ask_log.UNKNOWN_NAME, "ask_log-2026-02-30.jsonl", "ask_log-2020-01-01.jsonl.bak", "notes.txt",
        "ask_log.legacy.jsonl", "xask_log-2020-01-01.jsonl", "ask_log-2020-01-01.json")}
    (folder / "ask_log-2020-01-02.jsonl").mkdir()                       # a folder with a daily name: not ours
    (folder / "ask_log-2020-01-03.jsonl").symlink_to(outside)           # a link with a daily name: not ours
    return made


def test_the_check_deletes_only_its_own_daily_files_past_the_limit(tmp_path, capfd):
    log, _clock, folder = make_log(tmp_path, "2026-11-25T10:00:00+00:00", days=90)
    _old_and_other_files(folder)
    log.check()
    left = set(os.listdir(folder))
    assert "ask_log-2026-08-25.jsonl" not in left and "ask_log-2026-08-26.jsonl" not in left
    assert left == {"ask_log-2026-08-27.jsonl", "ask_log-2026-11-24.jsonl", "ask_log-2026-12-01.jsonl",
                    "ask_log-2026-11-25.jsonl", ask_log.UNKNOWN_NAME, "ask_log-2026-02-30.jsonl",
                    "ask_log-2020-01-01.jsonl.bak", "notes.txt", "ask_log.legacy.jsonl", "xask_log-2020-01-01.jsonl",
                    "ask_log-2020-01-01.json", "ask_log-2020-01-02.jsonl", "ask_log-2020-01-03.jsonl"}
    assert (tmp_path / "outside.jsonl").read_text() == "not ours"
    assert (folder / "ask_log-2020-01-03.jsonl").is_symlink() and (folder / "ask_log-2020-01-02.jsonl").is_dir()
    err = capfd.readouterr().err
    assert "deleted 2 daily file(s) dated before 2026-08-27 (SFA_ASK_LOG_DAYS=90)" in err


def test_the_first_write_of_each_day_deletes_and_later_writes_that_day_do_not(tmp_path):
    log, clock, folder = make_log(tmp_path, "2026-11-25T10:00:00+00:00", days=90)
    log.check()
    touch(folder, "ask_log-2026-08-27.jsonl")                           # the last day kept on 2026-11-25
    log.write({"event": "ask"})
    assert (folder / "ask_log-2026-08-27.jsonl").exists()               # same day: no second pass
    clock.set("2026-11-26T03:00:00+00:00")                              # 08:30 on 2026-11-26 in Kolkata
    log.write({"event": "ask"})
    assert not (folder / "ask_log-2026-08-27.jsonl").exists()           # the first write of the new day
    touch(folder, "ask_log-2026-08-26.jsonl")                           # past the limit, but made after that pass
    log.write({"event": "ask"})
    assert (folder / "ask_log-2026-08-26.jsonl").exists()               # one pass a day: the next day's takes it


def test_a_first_write_with_no_check_also_deletes(tmp_path):
    log, _clock, folder = make_log(tmp_path, days=10)
    folder.mkdir(mode=0o700)
    touch(folder, "ask_log-2026-11-14.jsonl")
    touch(folder, "ask_log-2026-11-15.jsonl")
    log.write({"event": "ask"})
    assert sorted(os.listdir(folder)) == ["ask_log-2026-11-15.jsonl", "ask_log-2026-11-25.jsonl"]


def test_prune_never_raises_and_counts(tmp_path, monkeypatch):
    log, _clock, folder = make_log(tmp_path, days=1)
    assert log.prune() == 0                                             # no folder yet
    folder.mkdir(mode=0o700)
    for name in ("ask_log-2026-11-20.jsonl", "ask_log-2026-11-23.jsonl", "ask_log-2026-11-24.jsonl"):
        touch(folder, name)
    real_unlink = os.unlink

    def flaky(path, *a, **k):
        if str(path).endswith("2026-11-20.jsonl"):
            raise PermissionError("no")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(ask_log.os, "unlink", flaky)
    assert log.prune() == 1                                             # one refused, one deleted, one kept
    assert sorted(os.listdir(folder)) == ["ask_log-2026-11-20.jsonl", "ask_log-2026-11-24.jsonl"]
    monkeypatch.setattr(ask_log.os, "listdir", lambda *_: (_ for _ in ()).throw(OSError("gone")))
    assert log.prune() == 0
    assert log.prune(dt.date(2027, 1, 1)) == 0


# ------------------------------------------------------------------------------------------------ the split
def ln(ts: str | None, **extra) -> bytes:
    body = {"ts": ts, **extra} if ts is not None else extra
    return (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")


def legacy_log(private: Path) -> tuple[Path, bytes]:
    """A made-up single-file log: three business days, the midnight edge, every kind of unreadable ts."""
    rows = [
        ln("2026-11-16T04:00:00+0000", event="startup", ok=True),
        ln("2026-11-16T05:00:00+0000", event="ask", question=f"How many interviews did {NAME} have?"),
        ln("2026-11-16T18:29:59+0000", event="ask", question="q3"),                    # still 2026-11-16 in Kolkata
        ln("2026-11-16T18:30:00+0000", event="ask", question="q4"),                    # 2026-11-17 in Kolkata
        ln(None, event="ask", question="no ts at all"),
        b"\n",                                                                          # a blank line
        b"   \n",                                                                       # and one of spaces
        ln("2026-11-17T09:00:00+05:30", event="ask", question="q6"),
        ln("2026-11-17T10:00:00", event="ask", question="no zone"),                     # naive: unknown
        ln("not a time", event="ask", question="bad ts"),
        b'[1, 2, 3]\n',                                                                 # no object
        ln(None, event="ask", ts_missing=True).replace(b'{"event"', b'{"ts": 5, "event"'),   # ts no string
        b'{"ts": "2026-11-18T08:00:00Z", "event": "ask", "question": "Delta Tw',            # cut, readable ts
        b"\n",
        ln("2026-11-20T08:00:00Z", event="late_finish"),
        b'{"ts": "2026-11-20T09:00:00+0000", "event": "ask", "question": "last, no newline"}',
    ]
    data = b"".join(rows)
    private.mkdir(mode=0o700, exist_ok=True)
    path = private / "ask_log.jsonl"
    path.write_bytes(data)
    path.chmod(0o600)
    return path, data


def run_split(capsys, *args) -> tuple[int, str, str]:
    code = split.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_the_dry_run_counts_per_day_and_writes_nothing(tmp_path, capsys):
    private = tmp_path / ".private"
    source, data = legacy_log(private)
    code, out, err = run_split(capsys, "--private", str(private), "--today", "2026-11-25")
    assert code == 0 and err == ""
    assert "ask_log-2026-11-16.jsonl: 3 lines" in out and "ask_log-2026-11-17.jsonl: 2 lines" in out
    assert "ask_log-2026-11-18.jsonl: 1 lines" in out and "ask_log-2026-11-20.jsonl: 2 lines" in out
    assert f"{ask_log.UNKNOWN_NAME}: 7 lines" in out and "7 line(s) have no readable ts (2 blank)" in out
    assert "15 lines" in out and "the last line has no newline" in out and "dry run: nothing was written" in out
    assert "may be deleted by hand from 2027-02-19: its newest line is of 2026-11-20" in out
    assert NAME not in out and "q3" not in out and "Delta Tw" not in out              # never a line's text
    assert source.read_bytes() == data and sorted(os.listdir(private)) == ["ask_log.jsonl"]
    assert split.main(["--dry-run", "--private", str(private), "--today", "2026-11-25"]) == 0
    assert sorted(os.listdir(private)) == ["ask_log.jsonl"]


def test_apply_puts_every_line_in_the_file_of_its_ts_and_keeps_the_old_file(tmp_path, capsys):
    private = tmp_path / ".private"
    _source, data = legacy_log(private)
    code, out, err = run_split(capsys, "--apply", "--private", str(private), "--today", "2026-11-25")
    assert code == 0 and err == "", err
    folder = private / "ask_logs"
    assert sorted(os.listdir(private)) == ["ask_log.legacy.jsonl", "ask_logs"]
    assert (private / "ask_log.legacy.jsonl").read_bytes() == data and mode(private / "ask_log.legacy.jsonl") == 0o600
    assert mode(folder) == 0o700
    names = sorted(os.listdir(folder))
    assert names == ["ask_log-2026-11-16.jsonl", "ask_log-2026-11-17.jsonl", "ask_log-2026-11-18.jsonl",
                     "ask_log-2026-11-20.jsonl", ask_log.UNKNOWN_NAME]
    assert all(mode(folder / n) == 0o600 for n in names)
    text = {n: (folder / n).read_bytes() for n in names}
    assert [json.loads(x)["question"] for x in text["ask_log-2026-11-16.jsonl"].splitlines()[1:]] == \
        [f"How many interviews did {NAME} have?", "q3"]
    assert [json.loads(x)["question"] for x in text["ask_log-2026-11-17.jsonl"].splitlines()] == ["q4", "q6"]
    assert text["ask_log-2026-11-18.jsonl"] == b'{"ts": "2026-11-18T08:00:00Z", "event": "ask", "question": "Delta Tw\n'
    assert text["ask_log-2026-11-20.jsonl"].endswith(b'"last, no newline"}\n')    # a newline added, nothing else
    unknown = text[ask_log.UNKNOWN_NAME].splitlines()
    assert len(unknown) == 7 and unknown.count(b"") == 1 and b"   " in unknown and b'[1, 2, 3]' in unknown
    # nothing dropped, nothing changed, the order of each day kept: the old lines are exactly the new lines
    old = data.split(b"\n")
    new = [x for n in names for x in text[n].split(b"\n")[:-1]]
    assert sorted(old) == sorted(new) and len(new) == 15
    for n in names:
        mine = text[n].split(b"\n")[:-1]
        assert [x for x in old if x in mine] == [x for x in mine if x in old]
    # every daily file's lines read back to that file's date
    for n in names[:-1]:
        for x in text[n].splitlines():
            assert split.target_name(x) == n
    assert "done: 15 lines in 5 file(s)" in out and "restart-ask" in out


def test_the_daily_files_of_the_split_are_the_writers_files(tmp_path, capsys):
    private = tmp_path / ".private"
    legacy_log(private)
    assert run_split(capsys, "--apply", "--private", str(private), "--today", "2026-11-25")[0] == 0
    log = ask_log.RotatingAskLog(str(private / "ask_logs"), days=5, clock=Clock("2026-11-20T10:00:00+00:00"))
    log.check()                                                          # dated before 2026-11-15 go: none here
    log.write({"event": "ask", "question": "after the split"})
    rows = lines_of(private / "ask_logs" / "ask_log-2026-11-20.jsonl")
    assert [r.get("question") for r in rows] == [None, "last, no newline", "after the split"]
    assert log.prune(dt.date(2026, 11, 25)) == 3                         # 11-16, 11-17, 11-18; 11-20 is kept
    assert sorted(os.listdir(private / "ask_logs")) == ["ask_log-2026-11-20.jsonl", ask_log.UNKNOWN_NAME]


def test_a_folder_that_already_holds_log_files_is_refused_without_append(tmp_path, capsys):
    private = tmp_path / ".private"
    source, data = legacy_log(private)
    folder = private / "ask_logs"
    folder.mkdir(mode=0o700)
    kept = touch(folder, "ask_log-2026-11-20.jsonl", '{"ts": "2026-11-20T10:00:00+0000", "event": "new"}\n')
    for args in (["--apply"], [], ["--dry-run"]):
        code, out, err = run_split(capsys, *args, "--private", str(private), "--today", "2026-11-25")
        assert code == 2 and "already holds 1 log file(s)" in err and "Nothing was written" in err
    assert source.read_bytes() == data and os.listdir(folder) == ["ask_log-2026-11-20.jsonl"]
    assert kept.read_text() == '{"ts": "2026-11-20T10:00:00+0000", "event": "new"}\n'
    # a folder that holds other files only is no reason to refuse; they stay untouched
    kept.unlink()
    touch(folder, "README.txt", "notes")
    assert run_split(capsys, "--apply", "--private", str(private), "--today", "2026-11-25")[0] == 0
    assert (folder / "README.txt").read_text() == "notes"


def test_append_adds_each_days_lines_after_what_is_there_and_rewrites_nothing(tmp_path, capsys):
    private = tmp_path / ".private"
    _source, data = legacy_log(private)
    folder = private / "ask_logs"
    folder.mkdir(mode=0o700)
    before = b'{"ts": "2026-11-20T10:00:00+0000", "event": "new"}\n{"ts": "2026-11-20T11:00:00+0000", "event": "cu'
    (folder / "ask_log-2026-11-20.jsonl").write_bytes(before)                    # the service's cut last line
    touch(folder, ask_log.UNKNOWN_NAME, "earlier unknown\n")
    code, out, err = run_split(capsys, "--apply", "--append", "--private", str(private), "--today", "2026-11-25")
    assert code == 0, err
    assert "2 file(s) already there get lines at their end" in out
    day = (folder / "ask_log-2026-11-20.jsonl").read_bytes()
    assert day.startswith(before + b"\n") and day.endswith(b'"last, no newline"}\n')
    assert day.count(b"\n") == 4 and mode(folder / "ask_log-2026-11-20.jsonl") == 0o600
    assert (folder / ask_log.UNKNOWN_NAME).read_bytes().startswith(b"earlier unknown\n")
    assert (private / "ask_log.legacy.jsonl").read_bytes() == data and not (private / "ask_log.jsonl").exists()


def test_the_split_refuses_a_backup_that_exists_a_link_and_a_folder_that_is_no_folder(tmp_path, capsys):
    private = tmp_path / ".private"
    source, data = legacy_log(private)
    (private / "ask_log.legacy.jsonl").write_text("an earlier backup")
    code, _out, err = run_split(capsys, "--apply", "--private", str(private))
    assert code == 2 and "already exists and is never replaced" in err
    assert (private / "ask_log.legacy.jsonl").read_text() == "an earlier backup" and source.read_bytes() == data
    other = tmp_path / "second.legacy.jsonl"
    assert run_split(capsys, "--apply", "--private", str(private), "--legacy", str(other))[0] == 0
    assert other.read_bytes() == data and not source.exists()

    private2 = tmp_path / "p2"
    private2.mkdir()
    real = tmp_path / "real.jsonl"
    real.write_bytes(data)
    (private2 / "ask_log.jsonl").symlink_to(real)
    code, _out, err = run_split(capsys, "--apply", "--private", str(private2))
    assert code == 2 and "is a link or no regular file" in err and real.read_bytes() == data

    private3 = tmp_path / "p3"
    legacy_log(private3)
    (private3 / "ask_logs").symlink_to(tmp_path)
    code, _out, err = run_split(capsys, "--apply", "--private", str(private3))
    assert code == 2 and "is a link or no folder" in err


def test_no_old_file_is_nothing_to_split(tmp_path, capsys):
    private = tmp_path / ".private"
    private.mkdir()
    code, out, _err = run_split(capsys, "--apply", "--private", str(private))
    assert code == 0 and "nothing to split" in out and os.listdir(private) == []
    (private / "ask_log.legacy.jsonl").write_text("x")
    code, out, _err = run_split(capsys, "--apply", "--private", str(private))
    assert code == 0 and "split before" in out


def test_an_empty_old_file_becomes_an_empty_backup(tmp_path, capsys):
    private = tmp_path / ".private"
    private.mkdir()
    (private / "ask_log.jsonl").write_bytes(b"")
    code, out, _err = run_split(capsys, "--apply", "--private", str(private), "--today", "2026-11-25")
    assert code == 0 and "(0 bytes, 0 lines)" in out
    assert (private / "ask_log.legacy.jsonl").read_bytes() == b"" and os.listdir(private / "ask_logs") == []


def test_an_old_file_that_changed_while_split_takes_everything_back(tmp_path, capsys):
    private = tmp_path / ".private"
    source, data = legacy_log(private)
    groups, _counts = split.plan(data)
    stale = os.lstat(source)
    with source.open("ab") as fh:                                       # the service still running: one more line
        fh.write(ln("2026-11-21T08:00:00+0000", event="ask"))
    code = split._apply(groups, private / "ask_logs", False, source, stale, private / "ask_log.legacy.jsonl")
    err = capsys.readouterr().err
    assert code == 1 and "still running" in err and "were removed" in err
    assert os.listdir(private / "ask_logs") == [] and not (private / "ask_log.legacy.jsonl").exists()
    assert source.read_bytes().startswith(data)


def test_a_failed_write_takes_back_the_files_this_run_made(tmp_path, capsys, monkeypatch):
    private = tmp_path / ".private"
    source, data = legacy_log(private)
    groups, _counts = split.plan(data)
    real_open, calls = os.open, []

    def failing(path, flags, *a, **k):
        if str(path).endswith("2026-11-18.jsonl"):
            raise OSError(28, "No space left on device")
        calls.append(str(path))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(split.os, "open", failing)
    code = split._apply(groups, private / "ask_logs", False, source, os.lstat(source),
                        private / "ask_log.legacy.jsonl")
    assert code == 1 and "No space left" in capsys.readouterr().err
    assert any("2026-11-16" in c for c in calls) and os.listdir(private / "ask_logs") == []
    assert source.read_bytes() == data and not (private / "ask_log.legacy.jsonl").exists()


def test_the_notes_name_what_the_limit_will_delete(tmp_path, capsys):
    private = tmp_path / ".private"
    legacy_log(private)
    code, out, _err = run_split(capsys, "--private", str(private), "--today", "2026-11-25", "--days", "6")
    assert code == 0
    assert "3 daily file(s) are dated before 2026-11-19 (6 days before 2026-11-25)" in out
    assert "may be deleted by hand from 2026-11-27" in out
    assert run_split(capsys, "--private", str(private), "--days", "0")[0] == 2
    assert run_split(capsys, "--private", str(private), "--today", "25/11/2026")[0] == 2


def test_the_script_runs_alone_with_python_dash_I(tmp_path):
    private = tmp_path / ".private"
    _source, data = legacy_log(private)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    p = subprocess.run([sys.executable, "-I", str(SCRIPT), "--private", str(private), "--today", "2026-11-25"],
                       capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp_path))
    assert p.returncode == 0, p.stderr
    assert "ask_log-2026-11-16.jsonl: 3 lines" in p.stdout and NAME not in p.stdout + p.stderr
    p = subprocess.run([sys.executable, "-I", str(SCRIPT), "--apply", "--private", str(private)],
                       capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp_path))
    assert p.returncode == 0, p.stderr
    assert (private / "ask_log.legacy.jsonl").read_bytes() == data


# ------------------------------------------------------------------------------------------------ run.sh and compose
def _function(script: str, name: str) -> str:
    return re.search(rf"^{name}\(\) \{{.*?^\}}", script, re.S | re.M).group(0)


def _run_ensure(tmp_path: Path) -> subprocess.CompletedProcess:
    """ensure_ask_files alone, on a throwaway .private: nothing else of run.sh runs, so no docker call can happen."""
    script = (WEB / "run.sh").read_text(encoding="utf-8")
    body = "\n".join([f'PRIVATE="{tmp_path}/.private"', 'HERE="/nonexistent/web"', "set -euo pipefail",
                      _function(script, "ensure_private"), _function(script, "ensure_ask_files"), "ensure_ask_files"])
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin"})


def test_run_sh_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(WEB / "run.sh")], capture_output=True, timeout=60).returncode == 0


def test_ensure_ask_files_makes_the_log_folder_700_and_the_token_file_600(tmp_path):
    p = _run_ensure(tmp_path)
    assert p.returncode == 0, p.stderr
    private = tmp_path / ".private"
    assert sorted(os.listdir(private)) == ["ask_logs", "ask_token.json"]
    assert mode(private) == 0o700 and mode(private / "ask_logs") == 0o700 and mode(private / "ask_token.json") == 0o600
    assert "No token yet" in p.stderr
    (private / "ask_logs").chmod(0o755)
    touch(private / "ask_logs", "ask_log-2026-11-25.jsonl", "kept\n")
    assert _run_ensure(tmp_path).returncode == 0
    assert mode(private / "ask_logs") == 0o700 and (private / "ask_logs" / "ask_log-2026-11-25.jsonl").read_text() == \
        "kept\n"


def test_ensure_ask_files_starts_nothing_while_the_old_single_file_is_there(tmp_path):
    private = tmp_path / ".private"
    private.mkdir(mode=0o700)
    (private / "ask_log.jsonl").write_text("old lines\n")
    p = _run_ensure(tmp_path)
    assert p.returncode == 2 and "split_ask_log.py is the dry run" in p.stderr and "Nothing was started" in p.stderr
    assert os.listdir(private) == ["ask_log.jsonl"] and (private / "ask_log.jsonl").read_text() == "old lines\n"
    (private / "ask_log.jsonl").unlink()
    (private / "ask_logs").symlink_to(tmp_path)
    p = _run_ensure(tmp_path)
    assert p.returncode == 2 and "symbolic link" in p.stderr


def test_up_ask_and_restart_ask_make_the_files_before_any_docker_call():
    script = (WEB / "run.sh").read_text(encoding="utf-8")
    body = script.split('case "${1:-}" in', 1)[1]
    for command in ("  up-ask)\n", "  restart-ask)\n"):
        branch = body.split(command, 1)[1].split("\n    ;;\n", 1)[0]
        assert branch.index("check_switch") < branch.index("ensure_ask_files") < branch.index("docker")
    fn = _function(script, "ensure_ask_files")
    assert "ask_log.jsonl ask_token.json" not in fn and "docker" not in fn.replace("docker stop", "")
    help_end = re.search(r"sed -n '2,(\d+)p' \"\$0\"", script).group(1)
    header = script.splitlines()[1:int(help_end)]
    assert header[-1].startswith("# deliberately no") and all(x.startswith("#") for x in header)


def _ask_service() -> str:
    return (WEB / "compose.ask.yaml").read_text(encoding="utf-8")


def test_compose_mounts_the_log_folder_and_no_single_log_file():
    text = _ask_service()
    mounts = text.split("    volumes:\n", 1)[1].split("    healthcheck:", 1)[0]
    blocks = [m for m in re.split(r"\n?\s+- type: ", mounts) if m.strip()]
    found = {re.search(r"source: (\S+)", b).group(1): (re.search(r"target: (\S+)", b).group(1),
                                                         "read_only: true" in b) for b in blocks}
    private = {s: v for s, v in found.items() if ".private" in s}
    assert private == {"../.private/ask_token.json": ("/srv/sfa/.private/ask_token.json", True),
                       "../.private/ask_logs": ("/srv/sfa/.private/ask_logs", False)}
    writable_host = [s for s, (_t, ro) in found.items() if not ro and s.startswith("..")]
    assert writable_host == ["../.private/ask_logs"]
    block = next(b for b in blocks if "../.private/ask_logs" in b)
    assert "create_host_path: false" in block                           # run.sh makes it, with mode 700
    assert "ask_log.jsonl" not in text


def test_compose_caps_the_container_log_and_passes_the_days():
    text = _ask_service()
    assert re.search(r"\n    logging:[^\n]*\n      driver: json-file\n      options:\n        max-size: 10m\n"
                     r"        max-file: \"3\"\n", text)
    assert 'SFA_ASK_LOG_DAYS: "${SFA_ASK_LOG_DAYS:-}"' in text
    page = (WEB / "compose.yaml").read_text(encoding="utf-8")
    assert "ask_logs" not in page and "SFA_ASK_LOG_DAYS" not in page   # the page's and the MCP server's file: unchanged


# ------------------------------------------------------------------------------------ the stage-1 review fixes
def test_names_in_other_digits_are_no_daily_files_and_are_never_deleted(tmp_path):
    """F5: only the writer's own ASCII names count; Arabic-Indic, full-width or Devanagari digits name no daily file."""
    log, _clock, folder = make_log(tmp_path)
    folder.mkdir(mode=0o700)
    odd = ["ask_log-٢٠٢٠-٠١-٠١.jsonl", "ask_log-２０２０-０１-０１.jsonl", "ask_log-२०२०-०१-०१.jsonl"]
    for name in odd:
        touch(folder, name)
    assert all(ask_log.daily_date(n) is None for n in odd)
    touch(folder, "ask_log-2020-01-01.jsonl")
    assert log.prune() == 1
    assert all((folder / n).exists() for n in odd) and not (folder / "ask_log-2020-01-01.jsonl").exists()
    assert split.log_files(folder) == []   # the split reads the same names: none of these is a log file


def test_a_broken_stderr_never_makes_the_writer_raise(tmp_path, monkeypatch):
    """F10: a fault is reported on stderr; a stderr that cannot be written (a closed pipe) is ignored."""
    import io

    log, _clock, folder = make_log(tmp_path)
    folder.mkdir(mode=0o700)
    os.mkfifo(folder / "ask_log-2026-11-25.jsonl", 0o600)   # the write fails at once (a FIFO, opened non-blocking)

    class Broken(io.StringIO):
        def write(self, s):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stderr", Broken())
    log.write({"event": "ask"})                      # no raise
    touch(folder, "ask_log-2020-01-01.jsonl")
    assert log.prune() == 1                          # it deletes and would report the count: no raise either
    ask_log._say("anything")


def test_an_events_own_ts_never_replaces_the_stamp(tmp_path):
    """F11: the line's ts is the writer's stamp, first in the line, so it reads back to the date of its file."""
    log, _clock, folder = make_log(tmp_path, "2026-11-25T10:00:00+00:00")
    log.write({"ts": "2020-01-01T00:00:00+0000", "event": "ask", "status": "answered"})
    raw = (folder / "ask_log-2026-11-25.jsonl").read_text(encoding="utf-8")
    line = json.loads(raw)
    assert line == {"ts": "2026-11-25T10:00:00+0000", "event": "ask", "status": "answered"}
    assert raw.startswith('{"ts": "2026-11-25T10:00:00+0000"')
    assert split.target_name(raw.encode()) == "ask_log-2026-11-25.jsonl"


def _run_writer_check(tmp_path: Path, ask_mcp_text: str | None) -> subprocess.CompletedProcess:
    """check_ask_writer alone, with HERE a throwaway web folder holding the given ask_mcp.py (or none)."""
    here = tmp_path / "web"
    here.mkdir(exist_ok=True)
    if ask_mcp_text is not None:
        (here / "ask_mcp.py").write_text(ask_mcp_text, encoding="utf-8")
    script = (WEB / "run.sh").read_text(encoding="utf-8")
    body = "\n".join([f'HERE="{here}"', "set -euo pipefail", _function(script, "check_ask_writer"),
                      "check_ask_writer"])
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin"})


def test_up_ask_starts_nothing_while_the_writer_and_the_compose_disagree(tmp_path):
    """F3 / N2: compose.ask.yaml mounts only the folder; run.sh starts the ask service only when the ask_mcp.py next to
    it writes through RotatingAskLog, so a copy of a tree with the old writer never crash-loops."""
    old = _run_writer_check(tmp_path, 'log = AskLog(str(config.PRIVATE_DIR / "ask_log.jsonl"))\n')
    assert old.returncode == 2 and "RotatingAskLog" in old.stderr and "Nothing was started" in old.stderr
    new = _run_writer_check(tmp_path, 'log = RotatingAskLog(str(config.PRIVATE_DIR / "ask_logs"))\n')
    assert new.returncode == 0 and new.stderr == ""
    script = (WEB / "run.sh").read_text(encoding="utf-8")
    body = script.split('case "${1:-}" in', 1)[1]
    for command in ("  up-ask)\n", "  restart-ask)\n"):
        branch = body.split(command, 1)[1].split("\n    ;;\n", 1)[0]
        assert branch.index("check_switch") < branch.index("check_ask_writer") < branch.index("ensure_ask_files")
    # this tree: the guard agrees with what the compose file mounts and what ask_mcp.py opens
    mcp = (WEB / "ask_mcp.py").read_text(encoding="utf-8")
    writes_folder = "RotatingAskLog(" in mcp
    assert ("../.private/ask_logs" in _ask_service()) and "ask_log.jsonl" not in _ask_service()
    here = subprocess.run(["bash", "-c", "\n".join([f'HERE="{WEB}"', "set -euo pipefail",
                                                     _function(script, "check_ask_writer"), "check_ask_writer"])],
                          capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin"})
    assert here.returncode == (0 if writes_folder else 2), here.stderr
