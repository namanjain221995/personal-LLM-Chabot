"""20 coding cases whose answers are COMPILED AND RUN, not read.

The owner asked for an assistant that writes good code, so "good" here means
the program does what the ask said, including the edge case the ask named.
Every case carries a checker the harness wrote: `check.py` (Python and SQL),
`check.ts` (TypeScript) or `check.sh` (bash). code_sandbox.py extracts the
answer's fenced block, builds it, and runs the checker in a scratch venv with
no network. A checker that passes on a plausible wrong answer is worthless, so
each one asserts the boundary the ask states, and the three performance cases
assert a time budget that an accidentally quadratic answer cannot meet.

Languages: Python (CD01-CD05), algorithms (CD06-CD08), SQL (CD09-CD12),
TypeScript (CD13-CD16), bash (CD17-CD20). Debugging and refactoring cases hand
the model working-but-wrong code and ask for the fix.

Nothing here is user data, and no case reaches the network or the repository.
"""
from __future__ import annotations

from typing import Any, Dict, List

#: Appended to every ask. It states the output shape the sandbox needs without
#: telling the model how to solve anything.
ONE_BLOCK = ("\n\nReply with the complete, runnable {lang} in ONE fenced code block. "
             "Standard library only, no third-party packages and no network access.")


def code_case(cid: str, lang: str, message: str, code: Dict[str, Any], note: str = "", **expect) -> dict:
    label = {"python": "Python", "sql": "SQL (SQLite)", "typescript": "TypeScript", "bash": "bash"}[lang]
    exp = {"artifact": False, "min_code_blocks": 1, "code": {"lang": lang, **code}}
    exp.update(expect)
    return {"id": cid, "category": "coding", "upload": None, "effort": "fast", "note": note,
            "turns": [{"message": message + ONE_BLOCK.format(lang=label), "expect": exp}]}


# ------------------------------------------------------------------ Python --

CD01 = code_case(
    "CD01", "python",
    "Write a Python function `parse_duration(text)` that turns a duration string into a whole number of seconds: "
    "'1h30m' is 5400, '45s' is 45, '2h' is 7200, '90m' is 5400, '1h2m3s' is 3723. Text that is empty or is not a "
    "duration must raise ValueError.",
    {"files": {"check.py": '''\
from solution import parse_duration

for text, want in {"1h30m": 5400, "45s": 45, "2h": 7200, "90m": 5400, "1h2m3s": 3723, "0s": 0, "10m": 600}.items():
    got = parse_duration(text)
    assert got == want, f"parse_duration({text!r}) = {got!r}, expected {want}"
    assert isinstance(got, int), f"parse_duration({text!r}) returned {type(got).__name__}, expected int"

for bad in ["", "   ", "abc", "1x", "h", "m30"]:
    try:
        got = parse_duration(bad)
    except ValueError:
        continue
    raise AssertionError(f"parse_duration({bad!r}) returned {got!r} instead of raising ValueError")
print("ok")
'''}},
    note="the stated edge case is the ValueError, which a regex-free split silently skips")

CD02 = code_case(
    "CD02", "python",
    "Write a Python function `top_countries(path, n)` that reads a CSV file with a `Country` column and returns the "
    "n most common countries as a list of (country, count) tuples, ordered by count descending and, where two "
    "countries have the same count, by country name A to Z.",
    {"files": {"check.py": '''\
import csv

from solution import top_countries

rows = ([{"Country": "India", "City": "Pune"}] * 5 + [{"Country": "Brazil", "City": "Recife"}] * 3
        + [{"Country": "Angola", "City": "Luanda"}] * 3 + [{"Country": "Japan", "City": "Osaka"}] * 1)
with open("people.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["Country", "City"])
    w.writeheader()
    w.writerows(rows)

got = [tuple(x) for x in top_countries("people.csv", 3)]
assert got == [("India", 5), ("Angola", 3), ("Brazil", 3)], f"ties must break A-Z, got {got}"
assert [tuple(x) for x in top_countries("people.csv", 99)] == [
    ("India", 5), ("Angola", 3), ("Brazil", 3), ("Japan", 1)], "n larger than the data must return everything"
assert list(top_countries("people.csv", 0)) == [], "n=0 must return nothing"

with open("empty.csv", "w", newline="", encoding="utf-8") as fh:
    fh.write("Country,City\\n")
assert list(top_countries("empty.csv", 3)) == [], "a header-only file must return nothing"
print("ok")
'''}},
    note="ties must break by name, and a header-only file must not raise")

CD03 = code_case(
    "CD03", "python",
    "Write a Python function `tail_lines(path, n)` that returns the last n lines of a text file as a list of strings "
    "without their trailing newlines, in the order they appear in the file. The file can be gigabytes, so read it "
    "from the end in blocks instead of loading it into memory.",
    {"timeout": 120, "files": {"check.py": '''\
from solution import tail_lines

with open("big.txt", "w", encoding="utf-8") as fh:
    for i in range(200_000):
        fh.write(f"line {i}\\n")
got = tail_lines("big.txt", 3)
assert got == ["line 199997", "line 199998", "line 199999"], got
assert len(tail_lines("big.txt", 5000)) == 5000
assert tail_lines("big.txt", 1) == ["line 199999"]

with open("short.txt", "w", encoding="utf-8") as fh:
    fh.write("a\\nb\\nc")                      # no trailing newline
assert tail_lines("short.txt", 10) == ["a", "b", "c"], "n larger than the file must return every line"
assert tail_lines("short.txt", 2) == ["b", "c"], "a file without a final newline must not lose its last line"

open("empty.txt", "w").close()
assert tail_lines("empty.txt", 5) == [], "an empty file must return nothing"
print("ok")
'''}},
    note="the last line without a trailing newline is what a naive seek-and-split drops")

CD04 = code_case(
    "CD04", "python",
    "This function is supposed to return the index of `target` in a sorted list, or -1 when it is not there. "
    "It hangs on some inputs and returns the wrong index on others. Fix it and give me the whole corrected function.\n\n"
    "```python\n"
    "def bsearch(values, target):\n"
    "    lo, hi = 0, len(values)\n"
    "    while lo <= hi:\n"
    "        mid = (lo + hi) / 2\n"
    "        if values[mid] == target:\n"
    "            return mid\n"
    "        if values[mid] < target:\n"
    "            lo = mid\n"
    "        else:\n"
    "            hi = mid\n"
    "    return -1\n"
    "```",
    {"files": {"check.py": '''\
import random

from solution import bsearch

for values, target, want in [([1, 3, 5, 7], 5, 2), ([1, 3, 5, 7], 1, 0), ([1, 3, 5, 7], 7, 3),
                             ([1, 3, 5, 7], 4, -1), ([], 1, -1), ([2], 2, 0), ([2], 1, -1), ([2], 3, -1)]:
    got = bsearch(list(values), target)
    assert got == want, f"bsearch({values}, {target}) = {got}, expected {want}"

rng = random.Random(7)
for _ in range(300):
    values = sorted(rng.sample(range(2000), rng.randint(1, 80)))
    target = rng.choice(values + [-1, 5000])
    want = values.index(target) if target in values else -1
    got = bsearch(values, target)
    assert got == want, f"bsearch({values}, {target}) = {got}, expected {want}"
print("ok")
'''}},
    note="debugging: float mid, the len() upper bound, and the loop that never narrows")

CD05 = code_case(
    "CD05", "python",
    "This helper is too slow on large lists. Refactor it so it runs in linear time, keeping the name, the signature "
    "and exactly the same return value: the first value that has a duplicate later in the list, or None, so "
    "[1, 2, 3, 2, 1] still returns 1.\n\n"
    "```python\n"
    "def first_duplicate(values):\n"
    "    for i in range(len(values)):\n"
    "        for j in range(i + 1, len(values)):\n"
    "            if values[i] == values[j]:\n"
    "                return values[i]\n"
    "    return None\n"
    "```",
    {"timeout": 120, "files": {"check.py": '''\
import time

from solution import first_duplicate

assert first_duplicate([1, 2, 3, 2, 1]) == 1, "the original returns the first value that repeats LATER, not the repeat"
assert first_duplicate(["a", "b", "a"]) == "a"
assert first_duplicate([]) is None
assert first_duplicate([1, 2, 3]) is None
assert first_duplicate([9, 9]) == 9
assert first_duplicate([3, 1, 3, 1]) == 3
assert first_duplicate([5, 2, 2, 5]) == 5, "5 appears first, even though 2's duplicate comes earlier"

big = list(range(300_000)) + [12345]
started = time.perf_counter()
got = first_duplicate(big)
elapsed = time.perf_counter() - started
assert got == 12345, got
assert elapsed < 3.0, f"still quadratic: 300k values took {elapsed:.1f}s"
print(f"ok in {elapsed:.2f}s")
'''}},
    note="refactor: linear time, identical semantics including which duplicate is 'first'")

# -------------------------------------------------------------- algorithms --

CD06 = code_case(
    "CD06", "python",
    "Implement an LRU cache in Python as a class `LRUCache` with `__init__(self, capacity)`, `get(self, key)` "
    "returning -1 when the key is absent, and `put(self, key, value)`. Reading or writing a key makes it the most "
    "recently used; when the cache is full the least recently used key is evicted. get and put must both be O(1).",
    {"timeout": 120, "files": {"check.py": '''\
import time

from solution import LRUCache

c = LRUCache(2)
c.put(1, 1); c.put(2, 2)
assert c.get(1) == 1
c.put(3, 3)                      # evicts 2, because get(1) refreshed 1
assert c.get(2) == -1, "the least recently used key must be the one evicted"
assert c.get(3) == 3
c.put(4, 4)                      # evicts 1
assert c.get(1) == -1
assert c.get(3) == 3 and c.get(4) == 4

one = LRUCache(1)
one.put("a", 1); one.put("b", 2)
assert one.get("a") == -1 and one.get("b") == 2

upd = LRUCache(2)
upd.put(1, 1); upd.put(2, 2); upd.put(1, 10)   # an update also refreshes
upd.put(3, 3)
assert upd.get(2) == -1 and upd.get(1) == 10 and upd.get(3) == 3

big = LRUCache(1000)
started = time.perf_counter()
for i in range(200_000):
    big.put(i, i)
    big.get(i - 500)
elapsed = time.perf_counter() - started
assert elapsed < 3.0, f"200k operations took {elapsed:.1f}s, which is not O(1) per operation"
print(f"ok in {elapsed:.2f}s")
'''}})

CD07 = code_case(
    "CD07", "python",
    "Write a Python function `merge_intervals(intervals)` that takes a list of [start, end] pairs, which may be "
    "unsorted, and returns the merged, non-overlapping intervals sorted by start. Intervals that only touch, such as "
    "[1, 2] and [2, 3], count as overlapping and must be merged.",
    {"files": {"check.py": '''\
from solution import merge_intervals


def norm(result):
    return [list(x) for x in result]


assert norm(merge_intervals([[1, 3], [2, 6], [8, 10], [15, 18]])) == [[1, 6], [8, 10], [15, 18]]
assert norm(merge_intervals([[1, 2], [2, 3]])) == [[1, 3]], "touching intervals must merge"
assert norm(merge_intervals([[5, 6], [1, 4]])) == [[1, 4], [5, 6]], "the input can be unsorted"
assert norm(merge_intervals([[1, 10], [2, 3]])) == [[1, 10]], "a contained interval must not shorten the result"
assert norm(merge_intervals([])) == []
assert norm(merge_intervals([[4, 4]])) == [[4, 4]]
assert norm(merge_intervals([[1, 4], [0, 4]])) == [[0, 4]]
before = [[3, 5], [1, 2]]
merge_intervals(before)
assert before == [[3, 5], [1, 2]], "the input list must not be mutated"
print("ok")
'''}})

CD08 = code_case(
    "CD08", "python",
    "Write a Python function `shortest_path(grid)` where grid is a list of equal-length strings of '.' (open) and "
    "'#' (wall). Return the number of steps on the shortest path from the top-left cell to the bottom-right cell, "
    "moving up, down, left or right, or -1 when there is no path. An empty grid, or a blocked start or end, is -1.",
    {"timeout": 150, "files": {"check.py": '''\
import time

from solution import shortest_path

assert shortest_path(["..", ".."]) == 2
assert shortest_path(["."]) == 0, "start and end are the same cell"
assert shortest_path(["#"]) == -1
assert shortest_path([".#", "#."]) == -1, "diagonal moves are not allowed"
assert shortest_path(["....", ".##.", "...."]) == 5
assert shortest_path(["#...", "....", "...."]) == -1, "a blocked start is -1"
assert shortest_path(["...", "...", "..#"]) == -1, "a blocked end is -1"
assert shortest_path([]) == -1

n = 300
open_grid = ["." * n for _ in range(n)]
started = time.perf_counter()
got = shortest_path(open_grid)
elapsed = time.perf_counter() - started
assert got == 2 * (n - 1), got
assert elapsed < 4.0, f"a {n}x{n} grid took {elapsed:.1f}s, which is not a breadth-first search"
print(f"ok in {elapsed:.2f}s")
'''}})

# ----------------------------------------------------------------- SQL --

_SQL_SCHEMA = """\
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                     amount REAL NOT NULL, created_at TEXT NOT NULL);
INSERT INTO customers (id, name, country) VALUES
  (1, 'Aarav', 'India'), (2, 'Maya', 'India'), (3, 'Liam', 'Brazil'), (4, 'Sofia', 'Japan'), (5, 'Noah', 'Canada');
INSERT INTO orders (id, customer_id, amount, created_at) VALUES
  (1, 1, 100.0, '2025-01-14'), (2, 1, 250.0, '2025-06-02'), (3, 2, 50.0, '2025-03-30'),
  (4, 3, 300.0, '2025-11-11'), (5, 3, 100.0, '2024-12-31'), (6, 4, 75.0, '2024-05-05');
"""

_SQL_CHECK_HEAD = '''\
import sqlite3
import sys

from sqlsplit import statements

con = sqlite3.connect(":memory:")
con.executescript(open("schema.sql").read())
stmts = statements(open("solution.sql").read())


def run_script():
    """Every statement but the last, then the last one as the query."""
    for s in stmts[:-1]:
        con.execute(s)
    return con.execute(stmts[-1].rstrip(";")).fetchall()


def rounded(rows, places=2):
    return [tuple(round(c, places) if isinstance(c, float) else c for c in row) for row in rows]
'''

CD09 = code_case(
    "CD09", "sql",
    "SQLite tables: customers(id, name, country) and orders(id, customer_id, amount, created_at) where created_at is "
    "a 'YYYY-MM-DD' string. Write ONE query returning every country with the total amount and the number of orders "
    "placed in 2025. A country whose customers placed no 2025 order must still appear, with 0 and 0. Order by the "
    "total descending, then by country A to Z.",
    {"schema": _SQL_SCHEMA, "files": {"check.py": _SQL_CHECK_HEAD + '''
rows = rounded(run_script())
want = [("India", 400.0, 3), ("Brazil", 300.0, 1), ("Canada", 0.0, 0), ("Japan", 0.0, 0)]
assert len(rows) == 4, f"expected one row per country, got {rows}"
assert all(len(r) == 3 for r in rows), f"expected (country, total, orders), got {rows}"
assert rows == want, f"got {rows}, expected {want}"
print("ok")
'''}},
    note="the 2025 filter has to live in the join, or the LEFT JOIN drops Japan and Canada")

CD10 = code_case(
    "CD10", "sql",
    "SQLite table employees(id, name, dept, salary). Write ONE query returning each department's second highest "
    "DISTINCT salary as (dept, salary), ordered by dept A to Z. A department with fewer than two distinct salaries "
    "must not appear.",
    {"schema": """\
CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT NOT NULL, dept TEXT NOT NULL, salary INTEGER NOT NULL);
INSERT INTO employees (id, name, dept, salary) VALUES
  (1, 'Aarav', 'eng', 120), (2, 'Maya', 'eng', 120), (3, 'Liam', 'eng', 90), (4, 'Sofia', 'eng', 150),
  (5, 'Noah', 'sales', 70), (6, 'Zara', 'sales', 70),
  (7, 'Ethan', 'support', 60), (8, 'Priya', 'support', 80), (9, 'Omar', 'support', 55);
""", "files": {"check.py": _SQL_CHECK_HEAD + '''
rows = rounded(run_script())
want = [("eng", 120), ("support", 60)]
assert rows == want, f"got {rows}, expected {want} (ties are one distinct salary; sales has only one)"
print("ok")
'''}},
    note="DISTINCT salaries, and the department with a single distinct salary must drop out")

CD11 = code_case(
    "CD11", "sql",
    "SQLite table contacts(id, email, name). Some contacts are duplicated by email, ignoring case. Write SQL that "
    "deletes the duplicates, keeping the row with the lowest id for each email, and leaves everything else untouched.",
    {"schema": """\
CREATE TABLE contacts (id INTEGER PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL);
INSERT INTO contacts (id, email, name) VALUES
  (1, 'a@example.com', 'Aarav'), (2, 'A@Example.com', 'Aarav Shah'), (3, 'b@example.com', 'Maya'),
  (4, 'c@example.com', 'Liam'), (5, 'B@example.com', 'Maya S'), (6, 'a@EXAMPLE.com', 'A Shah');
""", "files": {"check.py": _SQL_CHECK_HEAD + '''
for s in stmts:                        # a delete script: run all of it
    con.execute(s)
rows = con.execute("SELECT id, email FROM contacts ORDER BY id").fetchall()
want = [(1, "a@example.com"), (3, "b@example.com"), (4, "c@example.com")]
assert rows == want, f"got {rows}, expected {want}"
print("ok")
'''}},
    note="case-insensitive duplicates, keep the lowest id")

CD12 = code_case(
    "CD12", "sql",
    "This SQLite query is supposed to show each order's total item value and how much has been paid, but the totals "
    "come out too high when an order has several items and several payments. Explain the cause in one sentence and "
    "give me the corrected query.\n\n"
    "```sql\n"
    "SELECT o.id, SUM(i.qty * i.unit_price) AS items_total, SUM(p.amount) AS paid\n"
    "FROM orders o\n"
    "JOIN order_items i ON i.order_id = o.id\n"
    "JOIN payments p ON p.order_id = o.id\n"
    "GROUP BY o.id\n"
    "ORDER BY o.id;\n"
    "```",
    {"schema": """\
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT NOT NULL);
CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, qty INTEGER NOT NULL,
                          unit_price REAL NOT NULL);
CREATE TABLE payments (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, amount REAL NOT NULL);
INSERT INTO orders (id, customer) VALUES (1, 'Aarav'), (2, 'Maya'), (3, 'Liam');
INSERT INTO order_items (id, order_id, qty, unit_price) VALUES
  (1, 1, 2, 50.0), (2, 1, 1, 100.0), (3, 2, 3, 20.0), (4, 3, 1, 10.0);
INSERT INTO payments (id, order_id, amount) VALUES (1, 1, 120.0), (2, 1, 80.0), (3, 2, 60.0);
""", "files": {"check.py": _SQL_CHECK_HEAD + '''
rows = rounded(run_script())
want = [(1, 200.0, 200.0), (2, 60.0, 60.0), (3, 10.0, 0.0)]
assert len(rows) >= 2 and all(len(r) == 3 for r in rows), f"expected (id, items_total, paid) rows, got {rows}"
paid_three = [r for r in rows if r[0] == 3]
assert rows[:2] == want[:2], f"orders 1 and 2 are wrong: got {rows[:2]}, expected {want[:2]}"
if paid_three:
    assert paid_three[0][1] == 10.0, f"order 3 items_total is wrong: {paid_three[0]}"
    assert (paid_three[0][2] or 0.0) == 0.0, f"an unpaid order must show 0, got {paid_three[0]}"
print("ok")
'''}},
    note="debugging: join fan-out; the unpaid order must survive the fix")

# ---------------------------------------------------------------- TypeScript --

CD13 = code_case(
    "CD13", "typescript",
    "Write and export a TypeScript function `groupBy` with the signature "
    "`groupBy<T, K extends string>(items: T[], key: (item: T) => K): Record<K, T[]>`. It groups the items by the key "
    "the callback returns, keeping each group in input order. It must type-check under `strict`.",
    {"files": {"check.ts": '''\
import { groupBy } from "./solution";

const people = [
  { name: "Aarav", city: "Pune" },
  { name: "Maya", city: "Osaka" },
  { name: "Liam", city: "Pune" },
];
const grouped = groupBy(people, (p) => p.city);
const pune = (grouped["Pune"] as typeof people | undefined ?? []).map((p) => p.name);
const osaka = (grouped["Osaka"] as typeof people | undefined ?? []).map((p) => p.name);
if (JSON.stringify(pune) !== JSON.stringify(["Aarav", "Liam"])) {
  throw new Error(`groups must keep input order, got ${JSON.stringify(pune)}`);
}
if (JSON.stringify(osaka) !== JSON.stringify(["Maya"])) {
  throw new Error(`Osaka group wrong: ${JSON.stringify(osaka)}`);
}
if (Object.keys(groupBy([] as { city: string }[], (p) => p.city)).length !== 0) {
  throw new Error("an empty input must give an empty object");
}
console.log("ok");
'''}})

CD14 = code_case(
    "CD14", "typescript",
    "In TypeScript, define a discriminated union `Shape` with a `kind` field holding 'circle' (radius), "
    "'rectangle' (width, height) or 'square' (side), and export a function `area(shape: Shape): number`. Write the switch so that adding a fourth "
    "shape later is a COMPILE error until area handles it, and export both `Shape` and `area`.",
    {"files": {"check.ts": '''\
import { area } from "./solution";
import type { Shape } from "./solution";

const close = (a: number, b: number) => Math.abs(a - b) < 1e-9;
const circle = { kind: "circle", radius: 2 } as unknown as Shape;
const rect = { kind: "rectangle", width: 3, height: 4 } as unknown as Shape;
const square = { kind: "square", side: 5 } as unknown as Shape;

if (!close(area(circle), Math.PI * 4)) throw new Error(`circle area wrong: ${area(circle)}`);
if (!close(area(rect), 12)) throw new Error(`rectangle area wrong: ${area(rect)}`);
if (!close(area(square), 25)) throw new Error(`square area wrong: ${area(square)}`);
console.log("ok");
'''}},
    note="the exhaustiveness check (a never-typed default) is the point")

CD15 = code_case(
    "CD15", "typescript",
    "This TypeScript function always returns an empty array, however long the ids list is. Fix it and give me the "
    "corrected function. It must keep the input order and keep the signature.\n\n"
    "```ts\n"
    "export async function fetchAll(ids: number[], load: (id: number) => Promise<string>): Promise<string[]> {\n"
    "  const out: string[] = [];\n"
    "  ids.forEach(async (id) => {\n"
    "    out.push(await load(id));\n"
    "  });\n"
    "  return out;\n"
    "}\n"
    "```",
    {"files": {"check.ts": '''\
import { fetchAll } from "./solution";

const load = (id: number): Promise<string> =>
  new Promise((resolve) => setTimeout(() => resolve(`v${id}`), (5 - (id % 5)) * 4));

async function main(): Promise<void> {
  const got = await fetchAll([1, 2, 3, 4, 5], load);
  const want = ["v1", "v2", "v3", "v4", "v5"];
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    throw new Error(`order or contents wrong: ${JSON.stringify(got)}`);
  }
  const empty = await fetchAll([], load);
  if (empty.length !== 0) throw new Error("an empty list must resolve to an empty array");
  console.log("ok");
}

main().catch((err) => {
  console.error(err);
  throw err;                      // an unhandled rejection exits non-zero
});
'''}},
    note="debugging: forEach with an async callback never awaits")

CD16 = code_case(
    "CD16", "typescript",
    "Write and export a TypeScript function `chunk<T>(items: T[], size: number): T[][]` that splits an array into "
    "chunks of at most `size`, keeping order, with the last chunk shorter when it does not divide evenly. A size "
    "below 1, or one that is not a whole number, must throw a RangeError. It must type-check under `strict`.",
    {"files": {"check.ts": '''\
import { chunk } from "./solution";

const eq = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
if (!eq(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])) throw new Error("uneven split wrong");
if (!eq(chunk([1, 2, 3, 4], 2), [[1, 2], [3, 4]])) throw new Error("even split wrong");
if (!eq(chunk([1, 2], 5), [[1, 2]])) throw new Error("a size larger than the array gives one chunk");
if (!eq(chunk([] as number[], 3), [])) throw new Error("an empty array gives no chunks");

for (const bad of [0, -1, 1.5]) {
  let threw: unknown = null;
  try {
    chunk([1, 2, 3], bad);
  } catch (err) {
    threw = err;
  }
  if (!(threw instanceof RangeError)) throw new Error(`size ${bad} must throw RangeError, got ${String(threw)}`);
}
console.log("ok");
'''}})

# --------------------------------------------------------------------- bash --

CD17 = code_case(
    "CD17", "bash",
    "Write a bash script that takes a directory as its only argument and prints the three largest *.log files in it "
    "as 'name<TAB>bytes', largest first. File names can contain spaces. If the argument is missing or is not a "
    "directory, print a message to stderr and exit 1.",
    {"files": {"check.sh": '''\
set -u
mkdir -p logs "logs/sub"
printf '%*s' 300 '' > "logs/alpha.log"
printf '%*s' 900 '' > "logs/big one.log"
printf '%*s' 600 '' > "logs/gamma.log"
printf '%*s' 100 '' > "logs/delta.log"
printf '%*s' 5000 '' > "logs/notalog.txt"

out="$(bash solution.sh logs)" || { echo "the script failed on a good directory"; exit 1; }
first="$(printf '%s\\n' "$out" | sed -n 1p)"
case "$first" in *"big one.log"*900*) ;; *) echo "largest first failed: [$first]"; exit 1 ;; esac
lines="$(printf '%s\\n' "$out" | grep -c . || true)"
[ "$lines" = 3 ] || { echo "expected 3 lines, got $lines: [$out]"; exit 1; }
printf '%s\\n' "$out" | grep -q "notalog.txt" && { echo "non-.log files must not appear"; exit 1; }
printf '%s\\n' "$out" | grep -q "delta.log" && { echo "the smallest .log must not appear"; exit 1; }
printf '%s\\n' "$out" | awk -F'\\t' 'NF < 2 { exit 1 }' || { echo "name and size must be separated by a tab"; exit 1; }

if err="$(bash solution.sh /nonexistent-directory-xyz 2>&1 >/dev/null)"; then
  echo "a missing directory must exit non-zero"; exit 1
fi
[ -n "$err" ] || { echo "a missing directory must say something on stderr"; exit 1; }
if bash solution.sh >/dev/null 2>&1; then echo "no argument must exit non-zero"; exit 1; fi
echo ok
'''}},
    note="names with spaces are what an unquoted for-loop gets wrong")

CD18 = code_case(
    "CD18", "bash",
    "Write a bash script that takes a directory and prints, for every *.csv file directly inside it, the line "
    "'<file name>: <number of lines>', sorted by the number of lines from largest to smallest. File names can "
    "contain spaces. Print nothing and exit 0 when the directory holds no CSV file.",
    {"files": {"check.sh": '''\
set -u
mkdir -p data
seq 1 12 > "data/a.csv"
seq 1 3 > "data/b file.csv"
seq 1 7 > "data/c.csv"
seq 1 99 > "data/ignore.txt"
mkdir -p empty

out="$(bash solution.sh data)" || { echo "failed on a good directory"; exit 1; }
want="a.csv: 12
c.csv: 7
b file.csv: 3"
got="$(printf '%s\\n' "$out" | sed 's#^.*/##')"
[ "$got" = "$want" ] || { echo "got:"; printf '%s\\n' "$got"; echo "expected:"; printf '%s\\n' "$want"; exit 1; }

out2="$(bash solution.sh empty)" || { echo "an empty directory must exit 0"; exit 1; }
[ -z "$out2" ] || { echo "an empty directory must print nothing, got [$out2]"; exit 1; }
echo ok
'''}})

CD19 = code_case(
    "CD19", "bash",
    "This backup script breaks when a file name contains a space: it copies nothing and reports success. Fix it and "
    "give me the corrected script. It takes a source directory and a destination directory and copies every *.conf "
    "file, creating the destination if it does not exist.\n\n"
    "```bash\n"
    "#!/bin/bash\n"
    "SRC=$1\n"
    "DEST=$2\n"
    "mkdir $DEST\n"
    "for f in `ls $SRC/*.conf`; do\n"
    "  cp $f $DEST/\n"
    "done\n"
    "echo done\n"
    "```",
    {"files": {"check.sh": '''\
set -u
mkdir -p src
printf 'a\\n' > "src/app.conf"
printf 'b\\n' > "src/my service.conf"
printf 'c\\n' > "src/other.txt"

bash solution.sh src "dest dir" >/dev/null 2>&1 || { echo "the script failed on a valid run"; exit 1; }
[ -f "dest dir/app.conf" ] || { echo "app.conf was not copied"; exit 1; }
[ -f "dest dir/my service.conf" ] || { echo "the file with a space was not copied"; exit 1; }
[ -f "dest dir/other.txt" ] && { echo "only *.conf files may be copied"; exit 1; }
[ "$(cat "dest dir/my service.conf")" = "b" ] || { echo "the copy is not the original content"; exit 1; }

# a second run over an existing destination must still work
bash solution.sh src "dest dir" >/dev/null 2>&1 || { echo "a re-run over an existing destination failed"; exit 1; }
echo ok
'''}},
    note="debugging: word splitting, ls in a for loop, mkdir without -p")

CD20 = code_case(
    "CD20", "bash",
    "Write a bash script that takes a directory and writes a file report.txt in it containing the line count of "
    "every *.txt file in that directory. The write must be atomic (a reader must never see a half-written "
    "report.txt), the temporary file must be cleaned up even when the script fails partway, and the script must use "
    "set -euo pipefail and exit non-zero when the directory does not exist.",
    {"files": {"check.sh": '''\
set -u
grep -q "set -euo pipefail" solution.sh || { echo "the script must use set -euo pipefail"; exit 1; }
grep -qE "\\btrap\\b" solution.sh || { echo "the temporary file must be cleaned up with a trap"; exit 1; }
grep -qE "\\bmv\\b" solution.sh || { echo "an atomic write means writing a temporary file and mv-ing it"; exit 1; }

mkdir -p work
seq 1 4 > work/one.txt
seq 1 6 > work/two.txt
want_after="one.txt report.txt two.txt "

bash solution.sh work >/dev/null 2>&1 || { echo "the script failed on a good directory"; exit 1; }
[ -f work/report.txt ] || { echo "report.txt was not written"; exit 1; }
grep -q "one.txt" work/report.txt || { echo "report.txt does not mention one.txt"; exit 1; }
grep -qE "(^|[^0-9])4([^0-9]|$)" work/report.txt || { echo "report.txt does not hold the line count 4"; exit 1; }
grep -qE "(^|[^0-9])6([^0-9]|$)" work/report.txt || { echo "report.txt does not hold the line count 6"; exit 1; }

after="$(ls -A work | sort | tr '\\n' ' ')"
[ "$after" = "$want_after" ] || { echo "leftover files in the directory: [$after], expected [$want_after]"; exit 1; }

if bash solution.sh /nonexistent-directory-xyz >/dev/null 2>&1; then
  echo "a missing directory must exit non-zero"; exit 1
fi
echo ok
'''}},
    note="atomicity, the trap and the missing-directory exit are all asserted, not read")

CODING_CASES: List[dict] = [CD01, CD02, CD03, CD04, CD05, CD06, CD07, CD08, CD09, CD10,
                            CD11, CD12, CD13, CD14, CD15, CD16, CD17, CD18, CD19, CD20]

assert len(CODING_CASES) == 20, len(CODING_CASES)
assert len({c["id"] for c in CODING_CASES}) == 20
