"""One known-good answer per coding case.

A checker that rejects correct code would score every model unfairly, and the
failure would look like a model regression. These answers are the control:
tests/test_aiq_harness.py runs each one through the sandbox and requires it to
pass, so a checker that is wrong, over-strict, or depends on something the ask
never stated is caught in CI instead of in a run.

They are written the way a careful engineer would answer, not the way the
checker is written, and they are never shown to the model.
"""
from __future__ import annotations

REFERENCE = {

    "CD01": '''```python
import re

_SECONDS = {"h": 3600, "m": 60, "s": 1}
_PART = re.compile(r"(\\d+)([hms])")


def parse_duration(text):
    """'1h30m' -> 5400. Raises ValueError on anything that is not a duration."""
    cleaned = (text or "").strip().lower()
    if not cleaned or not re.fullmatch(r"(\\d+[hms])+", cleaned):
        raise ValueError(f"not a duration: {text!r}")
    return sum(int(n) * _SECONDS[unit] for n, unit in _PART.findall(cleaned))
```''',

    "CD02": '''```python
import csv
from collections import Counter


def top_countries(path, n):
    with open(path, newline="", encoding="utf-8") as fh:
        counts = Counter(row["Country"] for row in csv.DictReader(fh) if row.get("Country"))
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:n]
```''',

    "CD03": '''```python
def tail_lines(path, n, block=8192):
    if n <= 0:
        return []
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        data = b""
        while end > 0 and data.count(b"\\n") <= n:
            step = min(block, end)
            end -= step
            fh.seek(end)
            data = fh.read(step) + data
    text = data.decode("utf-8", "replace")
    lines = text.split("\\n")
    if lines and lines[-1] == "":     # a trailing newline is not an empty last line
        lines.pop()
    return lines[-n:]
```''',

    "CD04": '''```python
def bsearch(values, target):
    lo, hi = 0, len(values) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if values[mid] == target:
            return mid
        if values[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
```''',

    "CD05": '''```python
def first_duplicate(values):
    seen = set()
    repeated = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    for value in values:               # the FIRST value that repeats, in input order
        if value in repeated:
            return value
    return None
```''',

    "CD06": '''```python
from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity):
        self.capacity = int(capacity)
        self._items = OrderedDict()

    def get(self, key):
        if key not in self._items:
            return -1
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key, value):
        if key in self._items:
            self._items.move_to_end(key)
        self._items[key] = value
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
```''',

    "CD07": '''```python
def merge_intervals(intervals):
    merged = []
    for start, end in sorted((list(i) for i in intervals), key=lambda i: (i[0], i[1])):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
```''',

    "CD08": '''```python
from collections import deque


def shortest_path(grid):
    if not grid or not grid[0]:
        return -1
    rows, cols = len(grid), len(grid[0])
    goal = (rows - 1, cols - 1)
    if grid[0][0] == "#" or grid[goal[0]][goal[1]] == "#":
        return -1
    seen = [[False] * cols for _ in range(rows)]
    seen[0][0] = True
    queue = deque([(0, 0, 0)])
    while queue:
        r, c, steps = queue.popleft()
        if (r, c) == goal:
            return steps
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and not seen[nr][nc] and grid[nr][nc] != "#":
                seen[nr][nc] = True
                queue.append((nr, nc, steps + 1))
    return -1
```''',

    "CD09": '''```sql
SELECT c.country,
       COALESCE(SUM(o.amount), 0) AS total_amount,
       COUNT(o.id)                AS order_count
FROM customers c
LEFT JOIN orders o
       ON o.customer_id = c.id
      AND o.created_at >= '2025-01-01'
      AND o.created_at <  '2026-01-01'
GROUP BY c.country
ORDER BY total_amount DESC, c.country ASC;
```''',

    "CD10": '''```sql
WITH distinct_salaries AS (
    SELECT DISTINCT dept, salary FROM employees
),
ranked AS (
    SELECT dept, salary,
           ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS position
    FROM distinct_salaries
)
SELECT dept, salary
FROM ranked
WHERE position = 2
ORDER BY dept ASC;
```''',

    "CD11": '''```sql
DELETE FROM contacts
WHERE id NOT IN (
    SELECT MIN(id) FROM contacts GROUP BY LOWER(email)
);
```''',

    "CD12": '''```sql
-- The two joins multiply each other: every item row is repeated once per
-- payment row, so both sums are counted as many times as the other table has
-- rows. Aggregate each side on its own first, then join the results.
SELECT o.id,
       COALESCE(i.items_total, 0) AS items_total,
       COALESCE(p.paid, 0)        AS paid
FROM orders o
LEFT JOIN (SELECT order_id, SUM(qty * unit_price) AS items_total FROM order_items GROUP BY order_id) i
       ON i.order_id = o.id
LEFT JOIN (SELECT order_id, SUM(amount) AS paid FROM payments GROUP BY order_id) p
       ON p.order_id = o.id
ORDER BY o.id;
```''',

    "CD13": '''```ts
export function groupBy<T, K extends string>(items: T[], key: (item: T) => K): Record<K, T[]> {
  const out = {} as Record<K, T[]>;
  for (const item of items) {
    const k = key(item);
    (out[k] ??= []).push(item);
  }
  return out;
}
```''',

    "CD14": '''```ts
export type Shape =
  | { kind: "circle"; radius: number }
  | { kind: "rectangle"; width: number; height: number }
  | { kind: "square"; side: number };

export function area(shape: Shape): number {
  switch (shape.kind) {
    case "circle":
      return Math.PI * shape.radius * shape.radius;
    case "rectangle":
      return shape.width * shape.height;
    case "square":
      return shape.side * shape.side;
    default: {
      const unhandled: never = shape;   // a new shape is a compile error here
      throw new Error(`unhandled shape: ${JSON.stringify(unhandled)}`);
    }
  }
}
```''',

    "CD15": '''```ts
export async function fetchAll(ids: number[], load: (id: number) => Promise<string>): Promise<string[]> {
  return Promise.all(ids.map((id) => load(id)));
}
```''',

    "CD16": '''```ts
export function chunk<T>(items: T[], size: number): T[][] {
  if (!Number.isInteger(size) || size < 1) {
    throw new RangeError(`size must be a whole number of at least 1, got ${size}`);
  }
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(items.slice(i, i + size));
  }
  return out;
}
```''',

    "CD17": '''```bash
#!/usr/bin/env bash
set -euo pipefail

dir=${1:-}
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
  echo "usage: $(basename "$0") <directory>" >&2
  exit 1
fi

find "$dir" -maxdepth 1 -type f -name '*.log' -printf '%s\\t%f\\n' \\
  | sort -rn \\
  | head -n 3 \\
  | awk -F'\\t' 'BEGIN { OFS = "\\t" } { print $2, $1 }'
```''',

    "CD18": '''```bash
#!/usr/bin/env bash
set -euo pipefail

dir=${1:-}
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
  echo "usage: $(basename "$0") <directory>" >&2
  exit 1
fi

shopt -s nullglob
for file in "$dir"/*.csv; do
  printf '%s\\t%s\\n' "$(wc -l < "$file")" "$(basename "$file")"
done | sort -rn | while IFS=$'\\t' read -r count name; do
  printf '%s: %s\\n' "$name" "$count"
done
```''',

    "CD19": '''```bash
#!/usr/bin/env bash
set -euo pipefail

src=${1:?usage: $0 <source dir> <destination dir>}
dest=${2:?usage: $0 <source dir> <destination dir>}

mkdir -p "$dest"
shopt -s nullglob
for file in "$src"/*.conf; do
  cp -- "$file" "$dest/"
done
echo done
```''',

    "CD20": '''```bash
#!/usr/bin/env bash
set -euo pipefail

dir=${1:-}
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
  echo "usage: $(basename "$0") <directory>" >&2
  exit 1
fi

tmp="$(mktemp "$dir/.report.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

shopt -s nullglob
for file in "$dir"/*.txt; do
  [ "$(basename "$file")" = "report.txt" ] && continue
  printf '%s: %s\\n' "$(basename "$file")" "$(wc -l < "$file")" >> "$tmp"
done
[ -e "$tmp" ] || : > "$tmp"
mv -- "$tmp" "$dir/report.txt"
```''',
}
