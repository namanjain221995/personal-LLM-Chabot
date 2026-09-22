"""The parity gate: one machine-checkable definition of "understood the prompt".

WHY THIS IS A PACKAGE AND NOT A SCRATCH DIRECTORY. Every track on the
prompt-comprehension programme changes how an answer is written. Without a
scorer that lives in the repository, runs in CI and cannot be quietly
softened, each track grades its own homework.

WHAT IS IN HERE

  ``checklist.py``   the 17 prompt checks and 2 extras, each traced to the
                     clause of the request it comes from, and every floor
                     carrying the measurement that justifies it.
  ``normalise.py``   DocumentSpec -> the Markdown it is equivalent to, so one
                     scorer judges the file route and the chat route fairly.
  ``score.py``       the Markdown parser and the checks. Reports what it
                     OBSERVED, not just pass/fail.
  ``test_parity.py`` the calibration guards and the EXACT-SCORE pins on the
                     frozen baselines. Green in CI forever.
  ``test_parity_gate.py``
                     ``PARITY_MIN`` applied to the candidate runs the tracks
                     produce. Vacuously green until a track lands one.
  ``run_live.py`` / ``run_live_chat.py`` / ``runs/diagram_probe.py``
                     the live producers. They match neither ``test_*.py`` nor
                     ``*_test.py``, which is how they stay out of the default
                     collection: they need a GPU and a live engine. Do not
                     rename them.

THE TWO TEST FILES ARE A DELIBERATE SPLIT.
``.github/workflows/scripts/shard_tests.py`` discovers by ``rglob`` at any
depth, so everything here named ``test_*.py`` runs in a CI shard. A file that
is permanently red is a pipeline the first person under deadline pressure
fixes by lowering the floor. So every floor's VALUE is pinned by name in
``test_parity.py`` -- move one in either direction and the failure names it --
and so is every WORD of the checklist: the 15 section names in order, the
title and the 12 context items, because deleting a requirement is otherwise
an easier way to soften the bar than moving a floor. The baselines are pinned
by EXACT score on top of both, which is what catches the scorer's LOGIC
drifting while the checklist stands still. Only the candidate runs, which do
not exist yet, are measured against ``PARITY_MIN``.
"""
