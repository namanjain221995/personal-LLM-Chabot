# Indexing coverage: unit, measurements, and the recommended configuration

Nothing here is committed. Baseline `dev` @ `29aa0ab`, 0 staged. Salesforce untouched.

## 1. The coverage unit, defined

"92%" was previously quoted without a unit. Three are meaningful and they disagree, so all three
are reported. The analytic model behind the projections was validated against the REAL
`web_index.chunk_page` on 240 real pages (largest 40 + first 200 by id): **0 mismatches**.

* **U1 — text coverage** (primary): stored characters falling inside the indexed window ÷ total
  stored characters. This is the retrieval-relevant one, because retrieval matches text.
* **U2 — page completeness**: pages with nothing dropped ÷ indexable pages. What an operator feels.
* **U3 — chunk realization**: chunks written ÷ chunks an uncapped chunker would write.

Corpus: 2,208 pages with text, 2,063 indexable (≥200 chars), 57,807,111 characters.

**The earlier framing was misleading and is corrected here.** "26.5% of the corpus is invisible"
is true by U1 and gives the wrong impression: only **59 of 2,063 pages (2.9%)** are cut, and those
59 hold **44.9% of every character in the corpus**. U1 is dominated by a handful of outliers — a
2.4 MB SEC filing, a 2 MB `plan.txt`, atom feeds, journal archives. One wants **846 chunks** against
a corpus median of 2-3.

## 2. Retrieval benefit — measured, and it refuted my hypothesis

I expected those tails to be feed furniture and therefore not worth indexing. They are not.

| | |
| --- | --- |
| pages over the cap, scanned | 59 |
| **yielding a plausible probe from the unindexed tail** | **51 (86.4%)** |
| yielding a *novel* probe | 50 (84.7%) |
| yielding only junk | 8 |
| probes extracted | 232, offsets self-verified, 0 failures |
| probe kinds | 112 numeric, 52 dated, 26 entity-attribute, 22 labelled value, 20 table cell |

**Independently verified by the manager**: 60 probes sampled at random, their surrounding context
checked against the live corpus — **60/60 are present in the tail AND absent from the indexed
region**. So indexing the tail adds facts that are genuinely unreachable today, rather than
duplicating what the head already covers.

**The honest caveat.** The probes are cloze completions of real sentences, not natural questions,
and quality is mixed: `"You are not a non-U.S. holder ... 183 days"` (an SEC tax threshold) and
`63.7%` from a GPT-5.2 benchmark page are things a user would ask; `"what is Why Obama?"` is link
text dressed as a fact. **86.4% is an upper bound on "useful", not a measure of it.**

## 3. Serving capacity — measured on the ten largest real pages, real embedding service

One process per configuration, because peak RSS cannot be reset within a process.

| cap | batch | chunks | seconds | ms/chunk | peak RSS |
| --- | --- | --- | --- | --- | --- |
| 64 | 8 | 640 | 14.1 | 22.03 | 371.6 MB |
| 256 | 8 | 2,385 | 50.9 | 21.35 | 507.2 MB |
| 512 | 8 | 3,475 | 80.7 | 23.22 | 573.4 MB |
| 1024 | 8 | 4,017 | 103.3 | 25.71 | 572.9 MB |
| 1024 | 2 | 4,017 | 102.3 | 25.47 | **519.4 MB** |

* **ms/chunk RISES with the cap** (21.35 → 25.71): the extra chunks come from progressively deeper,
  denser tails. An earlier figure of 12.2 ms/chunk came from a small-page probe and is the WRONG
  rate for this decision; projections below use the measured 21-26.
* **Peak memory plateaus** after ~512 — the ten pages saturate — while time does not.
* **Batch size is a real but modest memory lever**: −54 MB (9%) at no time cost. Peak memory is
  dominated by one giant page's chunk set, not by how many pages share a batch.

## 4. Corpus-wide projection

| cap | U1 text | U2 pages whole | U3 chunks | +chunks | +index time | +storage |
| --- | --- | --- | --- | --- | --- | --- |
| **64 (today)** | 73.4% | 97.1% | 74.4% | — | — | — |
| 128 | 85.1% | 98.8% | 85.7% | +2,418 | 52 s | 9.9 MB |
| **256** | **92.1%** | **99.7%** | **92.4%** | **+3,872** | **83 s** | **15.9 MB** |
| 512 | 97.4% | 99.9% | 97.5% | +4,962 | 115 s | 20.3 MB |
| 1024 | 100.0% | 100.0% | 100.0% | +5,504 | 142 s | 22.5 MB |

## 5. Fast-mode impact — the dominant cost, and it is NOT the cap

TTFT p50 / completion p50, ms. 12 conversations per level. Baseline is a quiet box.

| arm | cached c=1 | cached c=8 | search c=1 | search c=8 | search c=8 total | quality |
| --- | --- | --- | --- | --- | --- | --- |
| **baseline (no drain)** | 480 | 1,526 | 274 | 1,265 | 3,280 | 12/12 |
| batch 8, no pause | 978 | 3,162 | 1,337 | **4,950** | **10,011** | 11/12 |
| batch 2, no pause | 705 | 2,702 | 1,114 | 5,100 | 9,190 | 11/12 |
| batch 2, 2 s pause | 756 | 3,013 | 969 | 2,101 | 7,231 | 11/12 |
| **batch 1, 5 s pause** | 689 | 2,723 | 584 | **2,089** | **4,388** | 11/12 |

* An **unpaced drain nearly quintuples search TTFT** and triples completion time.
* **Pacing is the effective lever, not batch size.** Batch 8 → 2 barely helps search
  (4,950 → 5,100); adding a pause halves it (→ 2,089).
* **Even the gentlest pacing does not make a drain invisible**: search at c=8 remains 1.65× TTFT
  and 1.34× completion against baseline.
* **CPU per turn is flat throughout (~58-92 ms).** The orchestrator is not doing more work; users
  are queueing behind indexing on the shared embedding service. Judged on CPU alone this would
  read as "no impact" while users saw a 3-5x slowdown — which is why the two are reported apart.
* Search answer quality was 11/12 in **all four** drain arms against 12/12 baseline. Consistent,
  therefore suggestive of a load effect — but the baseline is n=1 and live web search is variable,
  so this is **not proven**.

## 6. Answer-context budget is structurally unaffected

Raising the cap increases what is FINDABLE, never what is SENT. `web_index.retrieve` returns
`top_k=6`; the search path applies `_TIER_A_SOURCES=10` and `_TIER_B_CHARS=2500`. No consumer of
`_MAX_CHUNKS_PER_PAGE` or `INDEXED_CHARS_PER_PAGE` exists outside `web_index.py` (the only other
reference is a comment in `core/provenance.py`). The property holds by construction.

## 7. Recommendation

**Cap 256, batch 2, paced, drained off-peak.**

* **256, not 512/1024** — 92.1% U1 and 99.7% U2 for the lowest measured ms/chunk (21.35) and a peak
  RSS of 507 MB. Above 256 the marginal chunks come from the deepest tails of the very largest
  pages, which is where retrieval-skew risk is highest and value lowest: 512 buys +5.3 pp of U1 for
  +32 s and +66 MB of RSS; 1024 buys +2.6 pp more.
* **Batch 2** — −54 MB peak RSS at no time cost.
* **Paced and off-peak** — this is the load-bearing part. The full drain is ~83 s of embedding at
  full tilt, but run against live traffic it costs users 1.65-4x TTFT. Off-peak it costs nothing.
* **Not 64** — the retrieval benefit is real and verified: 86.4% of cut pages hold facts that are
  absent from the indexed region today.

## 8. Remaining uncertainty

* Probe *usefulness* is an upper bound (§2). A judged eval — real questions, graded answers — would
  measure it properly; the cloze set does not.
* The 11/12 search quality under load is suggestive, not proven (n=1 baseline).
* Drain throughput per pacing arm was not captured — the drains were killed before printing, so the
  duration/impact trade-off is described qualitatively, not quantified.
* Capacity was measured on the ten largest pages, which is the right sample for the marginal chunks
  but not a corpus-wide average.
* Two measurement errors of mine were caught and corrected mid-run: `kill` on a subshell left
  Python children alive so drains accumulated and contaminated two arms (discarded, redone); and a
  cleanup sweep matched `drain_load.py` inside its own shell's argv and killed the parent. Both are
  process-management faults, not defects in the system under test, and neither touched production.

---

# APPLIED 2026-09-07 — and what the post-change verification actually showed

## Deployed

Cap 64 → **256**, `CHUNKER_VERSION` 2 → **3**, migration **V27**. Orchestrator recreated;
healthy in 6 s; schema 27. Data intact (450 conversations / 1,856 messages / 138 uploads).

**V27 avoided re-embedding the whole corpus.** `chunk_page` stops on text length, not on the cap,
so a page under the OLD ceiling chunks byte-identically at either cap — proven on 120 real pages,
0 differing. V27 therefore stamped **2,149 pages** as chunker 3 without touching them and queued
only the **59 that actually changed**, rehearsed first on a real restore of production.

**Drain**: 59 pages, 5,523 chunks, 232.5 s (23.8 chunks/s), peak RSS **351.2 MiB** — below the
507 MB measured at batch 8, as the batch-2 choice predicted. Index: 16,015 → **19,887 rows**
(projection said 19,875 — within 12), `chunker_version 3`, both backlogs 0.

**Availability during the deploy, from request evidence not container metadata**: 1,463 probes,
**2.5 s unavailable window**, 12 `truncated` (connection opened, response never completed) and 2
`refused`. Container start times would have shown only "orchestrator restarted".

**Exclusion invariants hold at the deployed cap**: 23 tests across caps 2, 64 and 256 — quarantined,
purged and private content stay out regardless of coverage.

## The verification that qualifies the decision

The cap raise put **210 probe facts into the newly covered band** (chars 179,600-717,200) that were
unreachable before. Indexing worked. **Retrieval of them is much weaker than the coverage figure
suggests**, and that must not be glossed:

* **Homogeneous tables — indexed but unrankable.** `cbinsights.com/research-unicorn-companies`
  now holds 65 chunks and exactly one contains `VulcanForms`. The page IS returned for "unicorn
  companies". The specific chunk never ranks — not at top_k 6, 20 or 60, and not even for a query
  copied verbatim from the chunk itself. 65 chunks of one table carrying the same repeated header
  are near-identical in embedding space. The V24 header carry is working correctly (each chunk
  opens with `| Company | Valuation ($B) | ... |` and the rule); that is precisely what makes them
  indistinguishable.
* **Prose tails — mixed.** Of 5 prose probes sampled from the new band, **1 of 5** had its answer
  in the retrieved chunks. One query returned no pages at all.

So the honest ordering of the three numbers:

| claim | value | what it means |
| --- | --- | --- |
| facts exist in the unindexed tail | 86.4% of cut pages | measured, verified 60/60 |
| those facts are now IN the index | yes | 210 probes in the new band, chunks confirmed present |
| those facts are RETRIEVABLE | **~1 in 5 for prose, ~0 for big tables** | small sample, but consistent |

**The cap change is still right** — it costs 232 s once, 15.9 MB, and 351 MB of transient RSS, and
it removes a hard ceiling that guaranteed those facts could never be found. But it is an enabling
change, not a retrieval win, and the remaining barrier is ranking within a large single page, not
coverage. Claiming a retrieval improvement on the strength of the 86.4% figure would have been
wrong.

**Follow-on this suggests (not done, not in scope here):** a large page's chunks compete with each
other; per-page diversity in retrieval, or treating a giant table as rows rather than windows,
would address it. That is a ranking change and the standing instruction is not to introduce
arbitrary ranking adjustments, so it is recorded rather than attempted.
