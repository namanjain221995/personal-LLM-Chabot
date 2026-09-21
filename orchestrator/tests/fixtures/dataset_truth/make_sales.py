"""Seeded synthetic sales tables for tests/test_profile_aggregates.py.

Nothing here is real data. `python make_sales.py` rewrites sales_200.csv and
sales_3000.csv next to this file; the tests read the committed CSVs and compute
their own truth from the file TEXT with Python Decimal, so a regenerated file
still carries its own correct answer.

The shape is the one the 2026-09 audit got wrong live: cents-valued revenue, a
low-cardinality region, a date, and an order_id that is a key, not a number to
add. The larger file also has a few blank revenues, so the tests pin how nulls
are counted (rows per group) and skipped (sums and averages).
"""
from __future__ import annotations

import csv
import os
import random
from datetime import date, timedelta

HEADER = ("order_id", "order_date", "region", "channel", "quantity", "revenue")
# Unequal weights so regions differ in both count and revenue rank.
REGIONS = (("North", 5), ("South", 3), ("East", 4), ("West", 2), ("Central", 1))
CHANNELS = ("online", "retail", "partner")
SEEDS = {200: 20260918, 3000: 20260919}


def rows(n: int, seed: int):
    rng = random.Random(seed)
    start = date(2023, 1, 1)
    names = [r for r, _ in REGIONS]
    weights = [w for _, w in REGIONS]
    for i in range(n):
        region = rng.choices(names, weights=weights)[0]
        day = start + timedelta(days=rng.randrange(730))
        cents = rng.randrange(100, 2_500_000)  # 1.00 .. 24,999.99
        revenue = f"{cents // 100}.{cents % 100:02d}"
        if n > 1000 and rng.random() < 0.01:
            revenue = ""
        yield (100001 + i, day.isoformat(), region, rng.choice(CHANNELS), rng.randint(1, 20), revenue)


def write(path: str, n: int) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADER)
        writer.writerows(rows(n, SEEDS[n]))


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    for n in SEEDS:
        write(os.path.join(here, f"sales_{n}.csv"), n)
