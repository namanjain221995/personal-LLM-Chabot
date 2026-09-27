"""The bar a candidate build must clear. Vacuously green until one lands.

WHAT THIS MEASURES, AND WHAT IT DOES NOT

`test_parity.py` asserts the scorer is still the calibrated scorer and that
the frozen baselines still score what they scored. This file asserts
something else entirely: that the answers this programme's tracks PRODUCE
clear `PARITY_MIN`.

Those answers do not exist yet. `runs/candidates/INDEX.json` declares which
ones are expected, and each track adds its own entry in the commit that
produces the recording. Until then the index is empty and both tests below
pass while printing that they measured nothing -- which is the honest state,
and visibly different from a skip.

THERE IS NO `pytest.skip` IN THIS FILE, ON PURPOSE. A gate that skips when
its inputs are missing is a gate that reports success for work that was never
checked, and it does it in green. A declared candidate whose recording is
absent FAILS, and the failure names the track that owes it.

`PARITY_MIN` is the integrator's dial and nobody else's. It is raised as
tracks land. It is never lowered to make a build green: if a candidate cannot
clear it, that is the measurement, and the measurement is the deliverable.
"""
from __future__ import annotations

import json
import os
import pathlib

from . import normalise
from . import score as SC

HERE = pathlib.Path(__file__).resolve().parent
CANDIDATES = HERE / "runs" / "candidates"
INDEX = CANDIDATES / "INDEX.json"

#: The bar a candidate build must clear on the PROMPT checks (of 17).
#: Measured 2026-09-22 on dev: file route 10 (fast), 11 (think), 12 (max);
#: chat route 14 (think). The gate starts at the best number the system
#: reaches TODAY so it can only be moved up.
PARITY_MIN = int(os.environ.get("PARITY_MIN", "14"))


def _index() -> list[dict]:
    return json.loads(INDEX.read_text(encoding="utf-8"))["candidates"]


def test_the_index_is_well_formed():
    """Every declaration names its file and the track that owes it."""
    for entry in _index():
        assert entry.get("file"), f"a candidate entry has no file: {entry}"
        assert entry.get("track"), (
            f"candidate {entry['file']} names no owing track; an unowned "
            "candidate is one nobody can be asked about")


def test_every_declared_candidate_is_present(capsys):
    """A declared recording that is missing FAILS, and names who owes it."""
    entries = _index()
    missing = [f"{e['file']} (owed by track {e['track']})"
               for e in entries if not (CANDIDATES / e["file"]).is_file()]
    with capsys.disabled():
        print(f"\n[parity gate] {len(entries)} candidate(s) declared in "
              f"{INDEX.relative_to(HERE)}")
    assert not missing, (
        "declared candidate recording(s) are not in runs/candidates/: "
        + "; ".join(missing))


def test_no_candidate_is_undeclared():
    """A recording nobody declared is a recording nobody is measured on."""
    present = {p.name for p in CANDIDATES.iterdir()
               if p.is_file() and p.suffix in (".json", ".md")
               and p.name != "INDEX.json"}
    declared = {e["file"] for e in _index()}
    assert present == declared, (
        "runs/candidates/ and INDEX.json disagree. Undeclared: "
        f"{sorted(present - declared)}; declared but absent: "
        f"{sorted(declared - present)}")


def test_every_declared_candidate_clears_the_bar(capsys):
    """`PARITY_MIN` applied to what the programme actually produced."""
    entries = _index()
    short = []
    for entry in entries:
        path = CANDIDATES / entry["file"]
        if not path.is_file():
            continue  # named by test_every_declared_candidate_is_present
        s = SC.score(normalise.load(path), entry["file"])
        with capsys.disabled():
            print("\n" + SC.report(s))
        if s.passed < PARITY_MIN:
            short.append(
                f"{entry['file']} (track {entry['track']}) scored "
                f"{s.passed}/{s.total}, floor {PARITY_MIN}; failing: "
                + ", ".join(f"{r.check_id} [{r.observed}]"
                            for r in s.prompt_results if not r.passed))
    with capsys.disabled():
        print(f"\n[parity gate] PARITY_MIN={PARITY_MIN}, "
              f"{len(entries)} candidate(s) measured")
    assert not short, "candidate(s) below the bar: " + " | ".join(short)
