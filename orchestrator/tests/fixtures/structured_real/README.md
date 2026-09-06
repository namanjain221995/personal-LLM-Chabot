# Hand-derived expectations for `core/structured.py`, read from real pages

## What these files are, and what makes them evidence

Every value in every `*.json` file here was **read out of the raw cached HTML by a
human process** — opening the page source, finding the `<script
type="application/ld+json">` block or the `itemprop=` attribute, and transcribing
what it says. **Nothing here was captured from the extractor's output.** That is
the entire point: a snapshot of what the code currently produces can only ever
prove the code has not changed. These files can disagree with the code, and when
they do, the HTML is right until someone shows otherwise.

The `quoted_source` array in each file carries the markup fragment the values were
read from, so a reviewer can check the transcription without re-reading a 200 KB
page. `read_from` on each expectation says why that value is the interesting one.

## Why they exist

`core/structured.py` was verified only against fixtures this project wrote itself
(`tests/test_structured_data.py`, 38 tests, every input hand-authored). Clean
markup proves the parser parses; it says nothing about what real publishers emit.
These seven pages are the real-world half.

## The shape of a fixture

```jsonc
{
  "slug": "...",                      // matches a page in tools/structured_real_world.py
  "url": "...",
  "cached_body_sha256": "...",        // the exact snapshot the values were read from
  "markup": "jsonld" | "microdata",
  "quoted_source": [ "...the markup, quoted..." ],
  "expectations": [ ... ],            // must appear, attached to this entity
  "known_absent": [ ... ]             // read in the HTML, must NOT appear, with the reason
}
```

**`expectations` assert an association, not a substring.** A check passes only when
one rendered line carries *all* of: the entity name, the parent (for a nested
record), the entity type, the field key, the exact value, and the `[jsonld]` /
`[microdata]` provenance marker. `value in text` would pass on a page that
scattered a number anywhere, which is precisely the failure the module exists to
prevent.

**`known_absent` is the other half and is not decoration.** It pins values a human
read in the markup that the extractor does *not* carry — dropped by the dedupe
(often correctly, when the prose already states them), by a skip-list key, or by a
real gap. Each entry records which. They are asserted absent so that a change
which starts recovering them **fails here and gets read by a person**, rather than
appearing unannounced in the shared corpus.

`entity_is_bare_type: true` records that the rendered head is a bare type with no
entity name. On `openlibrary-lotr` that is a *defect* being pinned, not an
approval — see that file's `note`.

## One assertion in here rests on a guess, and it is flagged

`structured._line` joins **fields** with `"; "`, and `structured._collect` joins the
members of a **multi-valued property** with `"; "` as well. Once a line is rendered the
two separators are the same characters, so

```
... isbn: 9753423470; 9789753423472
```

is either one `isbn` with two values or an `isbn` followed by a loose number — and
nothing in the stored text decides which. That is a property of what gets chunked,
embedded and shown to the model, not merely of this checker.

The checker reconstructs it the only way available (a chunk with no `key: ` prefix
continues the previous value), **flags every record where it had to guess** as
`separator_ambiguous` in the JSON report, and lists the affected pages under
`summary.pages_with_ambiguous_field_separator`. Two `openlibrary-lotr` expectations
(`name`, `isbn`) pass only because of that reconstruction; the fixture says so in its
`separator_ambiguity` field. Three of the fourteen real pages hit this.


## Running them

```
cd orchestrator
python3 tools/structured_real_world.py --offline --check
```

`--offline` makes the run local-only: it reads the cached HTML and never opens a
socket. Exit code is non-zero if any expectation fails.

The cache lives outside the repository (default
`~/.cache/techsara/structured-real-world`, override with `--cache-dir` or
`$STRUCTURED_REAL_CACHE`). If it is missing, re-fetch once with
`python3 tools/structured_real_world.py` — that goes through `core/net.safe_fetch`
and asks `core/robots` first, exactly as ingest does.

## The snapshot pin, and the one value that will rot

`cached_body_sha256` is checked before any expectation. **If the cached bytes are
not the bytes these values were read from, the whole file is reported as
unverified rather than being checked against a page nobody read.** That is what
turns "the web changed" from a mysterious failure into a stated one.

One expectation is known to be perishable: `wikipedia-schema-org.dateModified` is
`2026-06-11T15:37:39Z`, and any edit to that article moves it. A `--refresh` run
will therefore trip the snapshot pin for that page. The correct repair is to
re-read the new HTML by hand and update the file — not to copy the number out of
the tool.

## Provenance

| | |
| --- | --- |
| Pages | 7 of the 14 in `tools/structured_real_world.py` |
| Snapshots fetched | 2026-09-06 (see each `cached_body_sha256`) |
| Values transcribed | 2026-09-07 |
| Assertions | 24 present, 17 absent |
| Extractor at the time | `EXTRACT_VERSION = 4`, `core/structured.py` as of this working tree |

All seven pages are public, non-sensitive reference material (a government tax
page, a public dataset catalogue, an encyclopaedia article, a research-institute
article, a software deposit record, and two public-domain / open library
catalogue pages). None required a login. Each was fetched once, through
`safe_fetch`, after `robots.allowed()` returned true.
