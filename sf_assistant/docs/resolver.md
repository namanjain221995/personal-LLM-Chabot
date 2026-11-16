# Term resolver (`knowledge/resolver.py`, `knowledge/terms.py`, `engine/vocabulary.py`)

> This repository holds the package, its prompts, the page, the tests and these docs. `kb_build/`, `docs/spec/`,
> `tests/parity/`, `knowledge/` and `.private/` stay on the owner's machine: where this page names them, they are not here.

One typo-tolerant matcher for every name the assistant knows: object, field, picklist value, stored record value,
record type, component (flow, Apex, validation rule, business process) and person name. A misspelt word gets
"did you mean `Completed`?" instead of a plain error or the full value list. When there is one clear match, the
caller may use it directly and say so.

Status: built, wired and tested (2026-10-20), behind the switch `SFA_RESOLVER`, which is `on` by default since
2026-10-27 (owner, 2026-10-26: misspelt words are read from now on; `SFA_RESOLVER=off` turns it off; see
[Wiring](#wiring) and [How to switch it off](#how-to-switch-it-off)). With the switch on, the question hints of both
agents, the BM25 searches, the record tools' object, field and value arguments, the person names and the answer's
"Read as" line use it ([resolver_integration.md](resolver_integration.md)). With it off, every code path is the old
one (parity).

## Contents

- [How it works](#how-it-works)
- [API](#api)
- [Vocabulary](#vocabulary)
- [Matching tiers](#matching-tiers)
- [Slip model](#slip-model)
- [Decisions](#decisions)
- [Guard rails](#guard-rails)
- [Design choices that were measured](#design-choices-that-were-measured)
- [Benchmark](#benchmark)
- [How to extend](#how-to-extend)
- [Wiring](#wiring)
  - [How to switch it off](#how-to-switch-it-off)
- [Files and tests](#files-and-tests)

## How it works

```
knowledge/*.json ─────┐                                    ┌─ resolve_term(text, kinds, context_object) -> Resolution
English word list ────┼─ build_vocabulary() ─ Vocabulary ─ build() ─ Resolver ─┤
snapshot (caller's ───┘   (terms + known words)             (indexes)  └─ spell_correct(question) -> [Correction]
 r/o connection)
```

1. `build_vocabulary(knowledge_dir, snapshot=None, lexicon="auto")` collects **terms** from data only. Each term is
   a name (`text`), the thing it names (`target`), its `kind`, its object and where it came from (`source`). It also
   collects the **known words**: every word of the knowledge text and a generic English word list. Known words are
   correct: they are never corrected and never suggested. There are no hand-written lists of business words.
2. `build(vocab)` indexes the terms once: normalised key -> terms, the key written together -> terms, the key with
   doubled letters collapsed -> terms, word -> terms, sound key -> words, second sound key -> words, and a delete
   index (SymSpell style) for edit distance. On the full vocabulary (about 20,100 terms and 76,000 known words) this
   takes about 0.5 s. The resolver is read-only afterwards, so threads can share one instance.
3. A lookup normalises the text, tries the tiers in order and returns ranked candidates with a decision. A tool
   lookup (`resolve_term`) ranks the close spellings by a noisy-channel [slip model](#slip-model) and also lists
   spellings a little further away as did-you-means. `spell_correct` keeps the strict budget and scores, because
   a question has no argument slot that says a word must be a name.

Standard library only. `knowledge/resolver.py` imports no other sf_assistant module; `knowledge/terms.py` (the
knowledge files and the word list) imports only it. Both sit in the knowledge layer (decision A1 of
[resolver_integration.md](resolver_integration.md)), so the knowledge side and a `--no-records` run can use them
without engine code. `engine/vocabulary.py` (the snapshot part) builds on `terms.py` and loads `duckdb` only when it
is given a snapshot path. Importing any of the three does not load duckdb.

## API

```python
from sf_assistant.engine.vocabulary import SnapshotPolicy, build_vocabulary
from sf_assistant.knowledge.resolver import build
from sf_assistant.knowledge.terms import knowledge_resolver, knowledge_vocabulary

# offline (benchmark, tests): a path opens its own read-only connection
vocab = build_vocabulary("knowledge/objects", ".private/okeval_run/warehouse.duckdb")
# in the app (tools/open_records.py attach_spelling_help): the warehouse's guarded query function, its columns and
# the catalog's policy (see Wiring)
vocab = build_vocabulary("knowledge/objects", lambda sql: wh.run(sql, max_rows=500_000, max_bytes=100_000_000).rows,
                         columns=wh.columns(),
                         policy=SnapshotPolicy.from_guard(cat.allowed, cat.blocked_columns, cat.blocked_fields))
r = build(vocab)
# the knowledge side (no stored values, no person names): a folder or parsed knowledge documents
k = build(knowledge_vocabulary("knowledge/objects"))
k, lines = knowledge_resolver([kn.raw for kn in kns])   # never raises; lines: "spelling help on (...)" / "... off: why"

res = r.resolve_term("Completd", kinds=["value"], context_object="Internal_Interview__c")
res.decision      # "corrected"
res.suggestion    # "Completed"
res.candidates[0] # Candidate(kind="value", target="Internal_Interview__c.Status__c=Completed", tier="sound", ...)

r.resolve_term("backgrond chech", kinds=["object"]).suggestion           # "Background Check"
r.resolve_term("internl intrview logs", kinds=["object"]).suggestions    # two objects -> "ambiguous"
r.resolve_term("Uncompleted", kinds=["value"]).decision                  # "ambiguous": a word, not a typo

for c in r.spell_correct("how does backgrond chech work for accont records"):
    c.span, c.text, c.decision, c.suggestion, c.kinds
# (9, 24) 'backgrond chech' corrected 'Background Check' ('object', 'field', 'value')
# (34, 40) 'accont' corrected 'Account' ('object', 'field', 'value')
```

| call | returns |
|---|---|
| `build_vocabulary(knowledge_dir, snapshot=None, *, lexicon="auto", columns=None, policy=None, connect_config=None, max_distinct=200, people=True, stored_values=True)` | `Vocabulary` (`terms`, `known_words`, `info` with counts, skip reasons and timings) |
| `knowledge_vocabulary(knowledge, lexicon="auto")` (`knowledge/terms.py`) | the same `Vocabulary` without a snapshot; `knowledge` is a folder or parsed knowledge documents |
| `knowledge_resolver(knowledge, lexicon="auto")` (`knowledge/terms.py`) | `(Resolver or None, start-up lines)`; never raises; None without a readable word list |
| `build(vocab)` | `Resolver` |
| `Resolver.resolve_term(text, kinds=None, context_object=None, within=None, limit=10, *, wide=True)` | `Resolution` (`wide=False`: the strict budget and scores of `spell_correct`) |
| `Resolver.spell_correct(question, include_exact=False, max_window=5)` | `list[Correction]`, left to right |
| `Resolver.word_candidates(word, kinds=None, fuzzy_only=False, wide=False)` | close vocabulary words of one word (for diagnostics) |
| `slip_cost(intended, typed)` | the slip model's cost of typing `typed` for `intended` (0 = the same word) |
| `Resolver.is_known_word(word)` | True when the word is already correct |
| `Resolver.key(text, person=False)` | the lookup key of a text |

- `snapshot`: a `query(sql) -> rows` function, an open DuckDB connection or cursor (read through, never closed), or a
  path (a separate read-only connection with `connect_config`; offline only, see [Wiring](#wiring)). A
  statement that fails, for example one the warehouse guard refuses, skips that field and is counted in
  `info["snapshot"]["query_errors"]`.
- `lexicon`: `"auto"` (the system lists `/usr/share/dict/american-english` and `british-english` when present), a
  path or a list of paths of one-word-per-line files, or `None`. With no list, `info["lexicon"]["warning"]` says so:
  ordinary English words may then be taken for typos (measured below).
- `kinds`: any of `object`, `field`, `value`, `record_type`, `component`, `person`. Only terms of these kinds are
  matched. A tool passes the kind it expects.
- `context_object`: an object API name, or a target such as `Obj.Field`. Its terms (the object itself, `Obj.X`,
  `Obj.Field=value`; matched on a name boundary, so `Obj.Name` never covers `Obj.Name_Link__c`) get a confidence
  bonus (+0.04) and are listed first.
- `within`: a hard filter on the target prefix, for example `"Obj.Field="` for the values of one field.
- `Resolution`: `decision`, `suggestion` (only when `exact` or `corrected`), `suggestions` (ranked spellings),
  `candidates` (ranked `Candidate`s, one per target), `tier`, `confidence`, `reason` (`protected: number`,
  `not corrected: single name`, `ignored: the` ...), `extra_words` (words of the text the best match does not use,
  fillers included), `missing_words` (words of the best match the text lacks). `as_dict()` is JSON-safe.
- `Correction`: `start`, `end` (`question[start:end] == text`), `decision`, `suggestion`, `suggestions`, `tier`,
  `confidence`, `kinds` (kinds of the matched terms), `candidates`. `as_dict()` gives `{"span": [start, end], ...}`.
- Target formats: `Obj` (object), `Obj.Field`, `Obj.Field=value`, `Obj.RecordType=DeveloperName`, `flow:Name`,
  `apex:Name`, `validation_rule:Obj.Name`, `process:id`, `person:Obj/Id`.

## Vocabulary

| kind | from | texts | target |
|---|---|---|---|
| object | knowledge `object` | API name, label, plural label | `Obj` |
| field | knowledge `fields[]` | API name (split on `_`/CamelCase, `__c` dropped), label | `Obj.Field` |
| value (picklist) | knowledge `fields[].picklist.values[]` | value, and label when different | `Obj.Field=value` |
| value (stored, `source="stored"`) | snapshot picklist and multipicklist fields (split on `;`) | stored values, record count as `weight` | `Obj.Field=value` |
| value (stored text, `source="stored_text"`) | snapshot: `Name` of small reference objects (at most 200 distinct names: niches, programs, interview types, departments ...); short text fields whose values repeat | values held by 2+ records (Names: any), no digit (Names may have one), not a person's name | `Obj.Field=value` |
| record_type | knowledge `record_types[]` | developer name, label | `Obj.RecordType=Dev` |
| component | knowledge `automation.flows[]`, `automation.apex[]`, `validation_rules[]`, `business_process_usage[]` | names (process name without its `F14 ` code) | `flow:`, `apex:`, `validation_rule:`, `process:` |
| person | snapshot `Name` of `Recruiter__c` (employees), human `User`s, person `Account`s, `Lead`s | full name; first and last word (titles, initials and words under 3 letters left out); first + last word of longer names | `person:Obj/Id` |
| known words | every string of the knowledge files; the English word list | words, never corrected, never suggested | - |

What the snapshot part never reads:

- tables: the catalog's sensitive objects, its
  never-read objects, `*History`, `*Share`, `*Feed`, and, with a `policy`, any table the guard does not allow;
- columns: any the `policy` blocks (the catalog's `blocked_columns` and `blocked_fields`), and names matching the
  catalog's secret and contact patterns plus identity numbers, issuing authority, login names, session ids, salary
  and transaction data (contact values stay out: they are no words users type as terms);
- picklists with the knowledge role `security_attribute` or `contact_attribute`;
- text fields with any of the catalog's profile drop roles (`identifier`, `integration_attribute`,
  `security_attribute`, `audit_attribute`, `system_attribute`, `configuration_attribute`, `contact_attribute`), with
  a free-text, name, link, score or time role (`display_name`, `feedback`, `ai_analysis`, `temporal_attribute` ...),
  named like free text or a person's name (`Summary`, `Remarks`, `Reason`, `Description`, `Notes`, `Error`,
  `Title`, `First_Name`, `Candidate_Name`, `City` ...), or named like an identifier (last word `Id`, `Number`,
  `Num`, `No`, `Token`, `Key`, `Hash`);
- text fields with more than 200 distinct values or more distinct values than half their filled rows;
- `Contact` (it mirrors person accounts), non-human users (`Guest`, `AutomatedProcess`, `CsnOnly`,
  `CloudIntegrationUser` ...), deleted rows, names with digits or `@`, and test, demo or system names.

Stored values and names exist only in memory. No tracked file holds any of them.

## Matching tiers

The tiers run best first.

| tier | rule | confidence |
|---|---|---|
| `exact` | Equal after normalising: transliteration of letters NFKD keeps whole (`ø æ ß ł đ þ`), NFKD accents dropped, case folded, possessive `'s` removed, API suffix (`__c`, `__mdt`, ...) dropped, `_` and CamelCase split, other punctuation read as space, plural -> singular for metadata (`logs`, `statuses`, `categories`; a plural in -es/-ies takes the stem that is a known word: `niches` -> `niche`, `causes` -> `cause`). **Person names are never singularised** (`Kelms` is no `Kelm`). The words written together also count (`gadgetorder`, `InterViews`), except a person name with an initial (`kora` is no `Kor A`). Filler words at the edges (`the X`, `X records`, `Mr. X`) are ignored when the whole text does not match, and listed in `extra_words`. | 1.0, decision `exact` |
| `normalized` | Equal once doubled letters are collapsed (`zorrvan` -> `Zorvan`, `enrollment` -> `enrolment`). Several spellings at the same distance: the one with fewest edits wins, then the one with the same number of words; a remaining tie is `ambiguous`. | 0.97 |
| `sound` | Same sound key: first letter (a first vowel reads as `a`) + consonant skeleton of the phonetic spelling. Phonetic rules: a `w` after a vowel and not before one = `u` (`rowvan` = `rouvan`), else `w`=v; `ph`=f, `z`=s, `q`=k, `x`=ks, `th dh bh kh gh sh` = plain, `ee`=i, `oo`=u, `c`=k (s before e/i/y), `ch` is its own sound. Within the edit limit below, with Jaro-Winkler >= 0.80 on the spelling or the phonetic form. Beyond the edit limit only when the phonetic forms are identical (`zalaxi` / `zalakshi`). A second sound key with `b`=v and `z`=j (`vorbel`/`borbel`, `zelvar`/`jelvar`) finds candidates whose phonetic forms are then identical, but never decides `corrected`. | 0.88·JW + bonuses |
| `edit` | Damerau-Levenshtein (adjacent swap = 1) of at most 1 for a typed word of up to 5 letters (or a vocabulary word of up to 4), else 2, and Jaro-Winkler >= 0.88 (>= 0.84 for typed words of 8+ letters). Candidates come from a delete index, so lookups stay fast. The typed form is compared with every word; its singular, when different, with metadata words only (`intervies` finds `interview`). | 0.88·JW + bonuses |
| `phrase` | Multi-word text: every text word is matched to a word of the term (best similarity first, any order). Unmatched text words cost 1, unmatched term words 0.5, connector words (`a an the of for to and`) 0.15 either way, a changed order 5%. A text word with no close vocabulary word is compared directly with the term's words when another word of the term matched (anchored): Jaro-Winkler >= 0.80, 3-letter words allowed, up to 2 edits for words of 5+ letters (`gadget order lne` -> `Gadget Order Line`). | aligned mean |

Fuzzy confidence: `0.88 x JW + 0.04 (same sound key) + 0.03 (one edit) + 0.01 (that edit is a likely slip: a
neighbouring key, another vowel, a swap, a doubled or neighbouring key added, a vowel or doubled letter left out) +
0.01 (same first letter) + up to 0.008 (words used by many terms)`, capped at 0.96. Doubled-letter matches stay above
every fuzzy match. Sound and edit matches are scored together (`STRICT_TIERS = False`); in a tie they count as one
tier. This is the strict scoring (`spell_correct`, `wide=False`); a tool lookup scores the same matches by the
[slip model](#slip-model) instead.

Jaro-Winkler here matches same-position letters first and only then searches the window. Plain greedy Jaro pairs a
repeated letter with the wrong copy and scores one substitution in `cinfidential`/`confidential` as 0.84. Matching
by position first scores it 0.95.

Candidates of one spelling are ranked by confidence, then the context object, tier, kind (metadata, then people, then
stored values), then the record count of the target (`Scheduled` on the field that holds it 40 times comes before the
one that holds it 3 times), then target name.

## Slip model

Tool lookups (`resolve_term`, `wide=True`, the default) score each `normalized`, `sound` and `edit` match of a word
by how likely a typist is to type the typed word for the term: a noisy-channel model over the kinds of slips (no
business words, no names). `slip_cost(intended, typed)` is the cheapest sum of slip costs along an alignment of the
two words. A cost is about -log of how often the slip happens, up to a constant: lower is likelier.

| slip | cost (`SLIP_COSTS`) |
|---|---|
| two neighbouring letters swapped | 1.6 |
| a doubled letter typed once | 2.0 |
| another spelling of the same sound, either way: `ph`/`f`, `ee`/`i`, `oo`/`u`, `x`/`ks`, `ck`/`k`, `kh`/`k`, `sh`/`s`, `th`/`t`, `dh`/`d`, `bh`/`b`, `gh`/`g`, `aa`/`a`, `ou`/`u`, `ai`/`e`, `au`/`o`, `tch`/`ch`, `ksh`/`ks` | 2.0 |
| a letter left out | 2.2 |
| a letter of the same sound typed: `c`/`k`, `s`/`z`, `v`/`w`, `i`/`y`, `q`/`k`, `j`/`z`, `b`/`v`, `d`/`t`, `f`/`v`, `g`/`j`, `c`/`s` | 2.2 |
| another vowel typed | 2.5 |
| a letter typed twice | 2.5 |
| a neighbouring key typed (QWERTY, same or next row, one column apart) | 2.7 |
| a neighbouring key typed as well | 2.9 |
| any other letter added | 4.0 |
| any other letter typed | 4.8 |
| the slip touches the first letter | +0.8 |
| the whole word written as it sounds (same phonetic form: `secion`/`session`, `phalor`/`falor`) | at most 2.2 |

- **Confidence** = 1.02 - 0.03 x cost, plus the common-word tie-breaker (at most 0.008), at most 0.99. One letter
  left out scores 0.954, a sound-alike first letter 0.93, two slips about 0.88. A match the strict scoring found
  keeps at least its strict confidence - 0.05, so a spelling the slips cannot explain (two slips on one letter) is
  still listed.
- **Wider budget.** A tool lookup also takes spellings up to 2 edits away when both words have 5 letters or more, or
  when the typed word lacks two letters of a longer one (`wide_edits`), and it drops the Jaro-Winkler floor. Their
  slips may cost at most 7.5 (`SLIP_MAX`). These extra spellings (`wide`) are did-you-means only, never `corrected`.
  A phrase word matched only by such spellings is also compared directly with the term's words (a stopword such as
  `before` is never a fuzzy candidate, but the phrase anchors it).
- **Decision.** When every word of the text aligns with the term, a correction needs the runner-up to cost at least
  0.4 more (`SLIP_GAP`, 0.012 confidence). The cost already counts the edits, so there is no separate tie rule. A
  phrase with words missing or extra keeps the usual margins. One clear spelling scoring 0.76-0.80 is a did-you-mean
  (`not corrected: too far for a correction`) instead of `none`. Every confirmation rule under
  [Decisions](#decisions) still applies: a single name is still corrected only for doubled letters or an identical
  sound.
- **Why tool lookups only.** A tool argument is known to be a name of the asked kind, so a near-miss is worth a
  did-you-mean. In a question every unknown word would get one: `spell_correct` keeps the strict budget, the
  Jaro-Winkler scores and every question guard (`wide=False` gives the same scoring to a caller, for example to
  expand search words).
- **How the costs were set.** Starting values from the usual typing-error findings (omissions, transpositions and
  neighbouring keys are the common slips; first letters are mistyped less often), then a coordinate search on seed 11
  of the benchmark, rounded. The first-letter cost was kept at 0.8; the search wanted 0.4, because 4 of the
  benchmark's 11 typo kinds hit the first letter, which is far more often than people do.
- The cost of a word pair is cached (`functools.lru_cache`, 65,536 pairs, thread-safe). After changing
  `SLIP_COSTS` at run time, call `slip_cost.cache_clear()`.

## Decisions

| decision | when | the caller |
|---|---|---|
| `exact` | the text already names vocabulary (after normalising) | uses it |
| `corrected` | one spelling with confidence >= 0.80, a lead of >= 0.03 over the next spelling (a tool lookup whose words all align: slips cheaper by >= 0.4, see [Slip model](#slip-model)), no tie, not loose, within the strict budget, and none of the confirmation rules below | uses it, saying so ("reading 'Completd' as 'Completed'") |
| `ambiguous` | a did-you-mean: several spellings within 0.03; a **tie** (a runner-up within 0.06 with no more edits from no worse tier, or with fewer edits: `secion` -> `Session`/`Section`); a loose match; one clear match that must be confirmed (below); a phrase whose words all match a term but one (confidence >= 0.55); in a tool lookup, a spelling beyond the strict budget, or one clear spelling scoring 0.76-0.80 | asks, or lists the `suggestions` |
| `none` | no close vocabulary; or one weak match only | says nothing was found, plus the closest names in `suggestions` when there are any; never guesses |

One clear best match is still only `ambiguous` (the `reason` says why) when:

- **a correct word**: a changed word of the text is itself a correct word (English, knowledge text or vocabulary):
  `Incomplete` is no `Complete`, `Employer` no `Employee`, `season` no `Session`, `Cancelled` must be confirmed as
  `Canceled`;
- **another word (affix)**: the typed word and the match differ by a whole affix (`un- in- im- ir- il- non- dis- bi-
  re- pre- de- mis- sub-`, `-ee`/`-er`/`-or`): `Uncompleted`/`Completed`, `Trainee`/`Trainer`;
- **words missing** or **extra words**: the text leaves out words of the term or keeps words it does not have
  (connectors aside): `Phone Number` for `Candidate Phone Number`, `Offer Letter Status` for `Offer Letter Payment
  Status`;
- **single name**: one person name turned into another. A single first or last name is corrected only for doubled
  letters (`zorrvan` -> `Zorvan`) or an identical sound (`kweena` -> `Kwina`, `zaxen` -> `Zacksen`); any other
  change (`talvon` -> `Talvin`) is a did-you-mean, because a correctly spelt name of someone who is not in the data is
  usually one edit from a stored name;
- **first sound changed**: a person name whose first sound changed (`barnik` for `Varnik`);
- **second sound key**: the match was found through `b`=v or `z`=j only.

A full name whose every word aligns is corrected (`zrvan quillby` -> `Zorvan Quillby`, `davor quel` -> `Davor Quell`).

`spell_correct` uses the same rules for each word, and:

- a run of up to 5 words that, once corrected, spells a whole term becomes one correction (`backgrond chech` ->
  `Background Check`). A first name that is close to two stored names (`dvor` -> `Davor` or `Divor`) is decided by
  the surname next to it (`dvor quell` -> `Davor Quell`);
- when the vocabulary word next to such a run belongs, with the run's words, to longer terms, the longer run is looked
  up as one term (`internl intrview logs` -> ambiguous between the Question Log and Section Log objects, never
  `Internal Interview`);
- two or three correct name words (a stored first name, then a stored surname) that are no stored name together and
  no part of one are looked up as one name: a misspelt surname that is someone else's surname (`<first> tuskovo` ->
  did you mean `<first> Tuskova`);
- a typed word of 4-5 letters is corrected only for doubled letters or the same sound key (`chech` alone is a
  did-you-mean for `Check`);
- a word of multi-word terms only (`persn`) lists the shortest terms that contain it, as a did-you-mean;
- a word that only rare stored values use (fewer than 5 records) and that is one edit from a correct word
  (`deliverble`, a data-entry typo) is a did-you-mean toward the metadata word, listing the stored value too.

## Guard rails

- Only vocabulary is ever suggested. Generic function words (`STOPWORDS`) and words under 4 letters are never
  suggested by sound or edits.
- Never corrected:
  - words that are already correct: vocabulary words, the English word list, words of the knowledge text, the
    function words in `STOPWORDS`, and any of these with a common English suffix or prefix (`handlers`, `managing`,
    `unassigned`, `incomplete`, `biweekly`). Only `-s -es -ed -ing -er -ers -ly -ment -ments -ness` and `un- re-
    non- pre- sub- in- im- dis- bi-`. The lost-e rule applies only to `-ed` and `-ing`, so typos such as `completd`
    or `enginer` are not taken for English words.
  - words of under 4 letters. Inside a phrase anchored by other words, 3 letters are allowed.
  - numbers, dates, Salesforce Ids, anything with a digit, e-mail addresses and URLs.
  - words longer than the longest vocabulary word + 2 (no fuzzy work on them), and texts of more words than any term
    + 2 (`too many words`): a 1,000-letter token costs about 2 ms.
- A word with 6 or more fuzzy spellings whose best one scores under 0.88 matches many targets only loosely. It is
  `ambiguous`, never corrected.
- A tie between two equally near spellings is `ambiguous`, never a coin flip.
- A negation or a preposition is never dropped as a filler (`not startd` -> `Not Started`, never `Started`).
- `STOPWORDS` holds generic English function and question words and month and day names only (the word lists spell
  those capitalised). No business words, no domain verbs: those are known through the English word list.

## Design choices that were measured

All offline with the benchmark below. Settings were compared on seed 11; the numbers reported come from seed 7.

| choice | compared with | result |
|---|---|---|
| Slip model ranks the matches of a tool lookup | Jaro-Winkler scores (`wide=False`) | one typo 97.5% -> 98.8% top-1 suggested (first names 93.0% -> 96.7%, last names 93.4% -> 97.4%); two typos 81.2% -> 95.2% (single names 45.2% -> 89.9%); wrong auto-corrections 0.2% -> 0.1% (one typo), 0.1% both (two typos); question controls unchanged (`spell_correct` keeps the strict scoring) |
| Wider budget in tool lookups (2 edits for 5-letter words, no Jaro-Winkler floor), did-you-means only | the strict budget | none of the "no close vocabulary" answers left for one-typo names (2.6% before: first-letter slips such as `celvo` for `Kelvo` fell under the Jaro-Winkler floor); absent names as person arguments: corrected 3 -> 4 (all identical sound), did-you-means 34 -> 62 |
| Phonetically identical spellings cost one slip (2.2) | the sum of their slips | `secion` stays a tie between `Session` and `Section`; leave-one-out names corrected unchanged (12 of 120, all identical sound or doubled letters) |
| First-letter slip cost 0.8 | 0.4 (best on seed 11) | about 0.3 points of single-name top-1 given up; kept because people mistype first letters less often than the benchmark's generator does |
| English word lists as known words | the knowledge text only | 6,000 most common English words: 75 corrected and 543 flagged -> 0 and 0; held-out question sets: 13 false corrections and 32 false flags -> 0 and 2 |
| Single person names corrected only for doubled letters or identical sound | corrected at 0.80 like other terms | leave-one-out (a stored first name whose people are removed): 40% of those names were corrected to another person -> 12% (all 12 to an identically sounding spelling, such as `-ee-`/`-i-` or `th`/`t`); top-1 suggestion unchanged |
| Names never singularised | singular() on every word | 15 pairs of stored names that differ by a final `s` no longer share one key |
| Missing or extra words only a did-you-mean | phrase completion corrected | `Phone Number`, `Training Result`, `Offer Letter Status` no longer bound to a longer, different field |
| `JW_MIN_LONG = 0.84`, anchored words up to 2 edits, phrase did-you-mean from 0.55 | 0.88, 1 edit, 0.76 | two typos 76.7% -> 80.0% top-1 suggested (v1 generator 84.4% -> 87.4%), controls unchanged, wrong auto-corrections 0.1% |
| Likely-slip tie-break (+0.01) | none | first names one typo 90.7% -> 91.6%, last names 93.5% -> 93.9%, wrong auto-corrections unchanged |
| Sound and edit scored together (`STRICT_TIERS = False`) | strict "first hit wins" | one typo 97.4% vs 97.1%, two typos 81.0% vs 80.8% |
| Sound match beyond the edit limit only when phonetically identical; tie rule; same-position Jaro-Winkler; bonus weights | (v1 measurements, still in place) | see git history of this file |

## Benchmark

Offline, no model. The script lives in the session scratchpad (`resolver/bench.py`). It writes `bench_result.md`
next to itself. Both are local only because they print real names. Vocabulary: the 71 knowledge files, the system
English word lists and the preprod snapshot of 2026-10-13. The typos are generated from the vocabulary itself: per
group, 400 distinct terms × each typo kind (drop, insert, double, swap, vowel change, sound-alike, keyboard
substitution, the same on the **first letter**: drop, insert, swap, keyboard; missing word; possessive). Typos that
spell a correct word or another term are skipped, because nothing could tell them apart. Tool mode is
`resolve_term(typo, kinds=[kind], context_object=<object>)`.

Metrics: **top-1 suggested** = the decision is `corrected` or `ambiguous` and its first suggestion is the original
term (what the tool shows first); **auto-corrected** = decision `corrected` (the tool binds it); **WRONG** =
auto-corrected to another term.

One typo (word typos, first-letter typos included), seed 7:

| | object | field | value (picklist) | value (stored) | value (stored text) | record type | component | person full | person first | person last | all |
|---|---|---|---|---|---|---|---|---|---|---|---|
| top-1 suggested % | 99.9 | 99.9 | 97.7 | 100 | 99.9 | 100 | 100 | 99.8 | 96.7 | 97.4 | **98.8** |
| auto-corrected WRONG % | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.3 | 0.6 | **0.1** |

- Without first-letter typos (the v1 generator): 99.0%. First-letter typos alone: 98.4%.
- Single first and last names: some typos are exactly as near (same slip cost) to another of the stored first
  names as to the original. They come back `ambiguous`; the original is in the top 3 for 98.6%. The best any
  ranker can do on this generator (the Bayes-optimal choice under a uniform prior, computed from the generator
  itself) is 97.3% for first names and 98.0% for last names.
- Wrong auto-corrections of single names (0.3-0.6%) are doubled-letter or identical-sound corrections, which the
  single-name rule allows (`torvel` -> `Torvell` when `Tarvel` was meant).
- Missing word: the right term is suggested first for 93.2% (a did-you-mean by design). Possessive: 99.8%.
- Open mode (no kind, no context): 98.7%.
- Inside a question (`spell_correct("what is <typo>")`, the strict scoring), the original is among the suggestions
  for 92.9%; metadata typos are fixed by a `corrected` span 86-96%; single names on purpose only 18% (they come back
  as did-you-means).

Two typos (two typo kinds, first-letter kinds included, in one word of 6+ letters or two words): **95.2%** top-1
suggested; wrong auto-corrections 0.1%. Without single first and last names: 97.0%. Single first and last names:
89.9% (first 88.1%, last 91.6%; the generator's Bayes-optimal ceiling is 95.5% and 96.8%). Without first-letter
typos (the v1 generator): 95.2%.

Slip model against the strict scoring (the same cases, `wide=False`):

| tool lookup | one typo top-1 % | first names | last names | two typos top-1 % | two typos, single names | WRONG auto (one / two typos) |
|---|---|---|---|---|---|---|
| strict scoring (`wide=False`) | 97.5 | 93.0 | 93.4 | 81.2 | 45.2 | 0.2 / 0.1 |
| slip model (`wide=True`, default) | **98.8** | **96.7** | **97.4** | **95.2** | **89.9** | 0.1 / 0.1 |

The other seeds agree: seed 11 (tuning) 98.8% / 94.5%, seed 13 98.7% / 94.6%, seed 21 98.8% / 94.3% (one / two
typos); first names 95.8-96.5%, last names 96.8-97.5%.

Controls (words already correct must stay unchanged):

- Sets the v1 resolver was tuned on (the 16 person-set controls, `easy.txt`, `records.txt`, the 40-question lookup
  set, the 44-question person dev set): **0** false corrections, 0 false flags.
- Held-out sets (person holdout, `medium`, `hard`, `twisted`, `sample`, the five `scripts/*.txt` sets, the
  reviewers' ordinary questions: 1,200 questions): **0** false corrections; flags: a city name (did you mean a
  stored name 1 edit away) and one stored record name that is itself misspelt.
- The intended typos in those sets are all fixed or flagged: three misspelt stored names and "intervews" corrected,
  a misspelt surname that is another stored surname flagged as a did-you-mean with the right person first.
- Word sweeps: the 6,000 most common English words: 0 corrected, 0 flagged. 88 common first names and surnames that
  are not stored: 3 corrected, all to an identically sounding stored spelling. Absent full names (a common first
  name + a stored surname, 600): 0 corrected.
- Tool lookups of correct words (the slip model): the 6,000 common English words and the 1,189 words of the control
  questions, passed to `resolve_term` with no kind and with each of `value`, `field`, `object` and `person`: no new
  correction against the strict scoring (the one `corrected` there is the same before and after). They get more
  did-you-means (for example 1,776 instead of 798 of the 6,000 words with no kind), which a tool shows only when an
  argument did not match. Absent names, places and companies as person arguments: 4 corrected (3 before), all to an
  identically sounding stored spelling; 62 did-you-means (34 before).
- English words missing from the word list (4,000 held out of it): 0.5% corrected, 3.5% flagged. The word list
  matters: ship one (see Wiring).

Speed: `build` 0.6 s (it ends with one full garbage collection, so the first lookups do not pause);
`build_vocabulary` 0.7 s from knowledge and word lists, plus 1.0 s from the snapshot. `resolve_term` median 0.14 ms
and p95 0.6 ms per word. Measured with the garbage collector paused and the slip cache empty, the slowest of all
35,500 benchmark lookups took 3.4 ms per word; inside the full benchmark run a few took up to 26 ms per word, which
were collector pauses on the benchmark's own data. `spell_correct` median 0.06 ms per question.

## How to extend

- **A new kind of name** (for example queues or report names): add terms in `knowledge/terms.py` (from the knowledge
  files) or `engine/vocabulary.py` (from the snapshot) with a new `source`, or a new kind in `KINDS` if callers must
  filter on it. Matching needs no change.
- **More stored values**: change `MAX_DISTINCT`, `MAX_TEXT_SHARE`, `MIN_TEXT_COUNT`, `MAX_VALUE_CHARS` and
  `MAX_VALUE_WORDS`, or the field rules in `_add_stored`. Never index a field the catalog policy blocks.
- **A sound rule** (another transliteration pair): add it to `_SOUND_RULES` in `knowledge/resolver.py`. Rules apply in order
  and must map both spellings to the same string. Rerun the benchmark: the controls must stay at 0 and one-typo
  top-1 must not drop.
- **A correct word that gets flagged**: it belongs in the English word list or the knowledge text. Change
  `STOPWORDS` only for a generic English function word, never for a business word.
- **Thresholds**: the module constants at the top of `knowledge/resolver.py` (`CORRECT_MIN`, `MARGIN`, `TIE_MARGIN`,
  `JW_MIN`, ...) and the slip model's (`SLIP_COSTS`, `SLIP_SCALE`, `SLIP_TOP`, `SLIP_GAP`, `SLIP_MAX`,
  `STRICT_DISCOUNT`). Every change must keep the controls at 0 false corrections. Tune on a different seed than the
  one you report. A slip cost is a kind of slip, never a word: do not add business words or names to it.
- **Tests**: `tests/test_resolver.py` uses made-up vocabularies only. Keep real names and stored values out of
  tracked files.

## Wiring

The plan and the owner's decisions are in [resolver_integration.md](resolver_integration.md). Done (2026-10-20):

1. **Build once per run, behind one switch.** `SFA_RESOLVER` (`off` | `on`, default `on` since 2026-10-27, unset or
   empty included; `Settings.resolver`, `config.ResolverConfig`) and `SFA_LEXICON` (word-list paths, default the
   system lists). Off: nothing is built or imported, and every result is the one without the resolver (the parity
   harness runs with it off: `no_model` sets `SFA_RESOLVER=off`). On:
   - `pipeline.build` builds the **knowledge resolver** (`knowledge_resolver`: the loaded knowledge files and the word
     list, never a stored value or a person name) and attaches it with `registry.attach_resolver`
     (`registry.resolver`; a `MultiToolRegistry` shares it with each per-object `ToolRegistry`);
   - `open_records` builds the **full resolver** right after `Catalog.build` (`attach_spelling_help`) and attaches it
     with `Catalog.attach_resolver` (`catalog.resolver`): the knowledge files, the word list and the snapshot, read
     through the warehouse's own guarded connection, `wh.run(sql, max_rows=500_000, max_bytes=100_000_000,
     timeout_s=rcfg.timeout_s)`, with `columns=wh.columns()` and the catalog's policy
     (`SnapshotPolicy.from_guard(cat.allowed, cat.blocked_columns, cat.blocked_fields, SENSITIVE_OBJECTS)`). A
     statement cut at those limits is counted (`info["snapshot"]["truncated"]`) and warned about. (The earlier draft
     said `max_rows=600`: that would cut the person statements, thousands of rows each on the current snapshot.) Never
     pass the path in the app: a second DuckDB connection to the same file with another configuration fails ("Can't
     open a connection to same database file with a different configuration"), and it would skip
     `check_warehouse_path`. On the 2026-10-13 snapshot the in-app vocabulary equals the offline build term for term
     (20,108 terms after de-duplication, 0 query errors, 0 cut statements).
   - Start-up lines (counts only): `spelling help on (knowledge files): ...` after the `objects:` line (cli), and
     `spelling help on (knowledge files + snapshot): ...` after the records lines (`open_record_session`); both end
     with `word list <n> words (sha <12 hex digits>)`, a hash of the loaded English word set. Any problem (a bad
     switch value, no readable word list, a failing build) leaves that resolver None with one line
     `spelling help off: <reason>`; records stay on, and nothing stops (`config.resolver_settings`: an invalid
     `SFA_RESOLVER` is `ResolverConfig(enabled=False, problem=...)`, off).
   - One source of truth: every entry point passes its `Settings.resolver` on (the cli, the page's every session
     open, the MCP server's record tools and knowledge registry); `open_records` and `pipeline.build` read the
     environment only when no settings are given. The evaluation's reference replay (`open_grading`) always runs
     with the spelling help off (it passes `ResolverConfig(enabled=False)`, whatever the switch says).
   - The knowledge and word-list part of the full resolver's vocabulary is kept per process
     (`terms.start_vocabulary`, keyed by the knowledge folder's and the word lists' file names, sizes and change
     times): a session reopened after a read-copy swap reads only the snapshot again.
2. **Ship an English word list.** `web/Dockerfile` installs `wamerican wbritish` (`/usr/share/dict/american-english`
   and `british-english`, the lists the benchmark used; Debian 13 gives the same words as the host's Ubuntu 24.04
   packages) and stops the build when the SHA-256 of both lists' sorted unique lines is not the benchmarked one
   (a newer base image with other lists fails loudly instead of changing the corrections). Without a readable list
   the spelling help stays off, because ordinary words would be flagged (measured above). Next to any English list,
   `src/sf_assistant/knowledge/spoken_words.txt` adds about 290 romanized Hindi and Gujarati question words
   ("kaunse", "kitne", "mahine") as known words: the team writes Hinglish questions, and without them "kaunse" was
   read as the stored value "Kansas" (review 2026-10-21).

The call sites (done 2026-10-20; every one reads the resolver at call time and runs the old code when it is None):

3. **Question hints** (step (1)). The record agent's hints (`RecordTools.object_hints`, an override of
   `NamesMixin.object_hints`) start with a section "Spelling (read by code, not from the user)"
   (`prompts/spelling_hints.md`): one line per misspelt word or phrase, `- 'accont' -> Account (object Account)` for
   a `corrected` span, `- 'persn' may mean: ... (say which one you used, or ask)` for an `ambiguous` one. Objects and
   stored values are then searched with the corrected question. Person names are not listed there; the people lines
   cover them. The knowledge agent gets the same block after the question (`pipeline.answer`,
   `pipeline.question_spelling`: the wiring layer reads the record session, never the knowledge side), with metadata
   names only (no stored value, no person name); with records off it keeps only the `corrected` spans.
4. **BM25 search** (step (2)). `knowledge.search.resolver_expander` is the `expand` of the knowledge tools' indexes
   (`ToolRegistry.attach_resolver`, `MultiToolRegistry.attach_resolver`) and of the catalog's object index and field
   indexes (`Catalog.attach_resolver`, metadata kinds). Only a query word that no chunk has and that is no correct
   word (`is_known_word`) is read: `spell_correct(query)` first, then `resolve_term(word, wide=False)`. A `corrected`
   reading adds its words, an `ambiguous` one the words all its suggestions share. A query of index words never
   reaches the expander and ranks as before. `search_knowledge`, `search_fields`, `search_all_knowledge` and
   `find_objects` add `read_as` (`{"backgrond chech": "background check"}`) when the expander added words; it gives
   no answer line.
5. **Record tools** (steps (3) and (4)): an object, field or value argument that no
   exact tier grounds calls `catalog.spelling(arg, kinds, context_object, within)` (`resolve_term`, cached;
   `within="Obj."` for fields, `"Obj.Field="` for values). Inside a record tool call (`READ_AS` set by
   `RecordTools.call`) one `corrected` reading is bound and stated (a note, `result["read_as"]`,
   `QueryEvidence.read_as`); otherwise, and for `ambiguous` or `exact`, the usual error (same code) names the
   did-you-means; `none` keeps today's error text. A value is never bound from a truncated value index or when it is
   a person's name. No reading ever changes a number (`resolver.numbers`; the doubled-letter tier keeps digits):
   `'01:55 PM'` is another time than `'01:15 PM'`, `'H11B'` another class than `'H1B'`, so such a `corrected`
   reading is only a did-you-mean, and a value typed with numbers gets only suggestions with the same numbers
   (`'9:30 AM'` -> `'09:30 AM'` first; none left: today's `Close:` list). The exact API name of another object's
   field is never read as a field of the queried object (the agent picked the wrong object: E2 keeps "is a field of
   B; check the object"). Text fields named for people (`*_Name__c` of a candidate, recruiter, interviewer, vendor,
   trainer, owner, contact, manager, POC/SPOC, employee, user, person) give no stored values, so a filter never
   binds a misspelt person through them.
6. **People** (step (5)). A misspelt name is read as a stored one only when the resolver says `corrected` (one word:
   doubled letters or the same sound, mapped back through the name index by name, never by the resolver's Ids):
   `_spot`, `resolve_person_name` and `about_people` (`_resolve_asked`) give "closest stored name", and the people
   hint states it first. An `ambiguous` name is a missing name with "did you mean X or Y?"; the E15a not-found list
   starts with the resolver's names. A filter never binds a misspelt person (E4h, no count). `Triage.as_dict` gets
   a `spelling` list. A one-word reading followed by a surname-like word (a stored last name, or no known word) is
   kept only when a stored record of that first name has that surname or a close spelling of it: an unstored full
   name is a missing name, never another person's first name. `question_people()` carries the reading
   (`read_as`), so the not-exact-name nudge also fires for a draft that writes only the stored first name.
7. **The answer** (step (6)). After the audits and the lookup footer, `pipeline.answer` adds one code line to
   `final` (never to `body`, which `score_records` scores), before a last `Source:` line:
   `Read as: 'accont' as Account; 'Completd' as Completed.` (`prompts/read_as_line.md`). Its pairs are the
   question's `corrected` metadata spans the answer rests on (the answer names the spelling, or a successful record
   or knowledge tool call has it among its arguments: a hint no one used is never stated as a reading) and the
   `read_as` of the successful record and knowledge tool calls, once per typed text; a pair the answer already
   states with both spellings is left out (`pipeline.spelling_pairs`). A person correction is the answer's lead
   sentence instead ("No record is named 'x'; the closest stored name is 'Y'."): the record side writes it, the
   merge's name note also covers a one-word name the record side corrected, and when the final answer still does
   not name the asked spelling, code puts that sentence first (`pipeline.with_person_lead`,
   `prompts/read_as_person_line.md`). The JSONL row gets `spelling` and `read_as_line` (true when code added the
   "Read as" line or the person lead sentence to `final`).
8. **Value spots** (2026-10-28, [value_spots.md](value_spots.md)). `RecordTools.spot_values` reads the question as
   typed first. Then it reads the question's `corrected` spans (`_spelling_spans`: stored values included, person
   names left out), never an `ambiguous` one and never into a cut field of the entity index. A spot read this way
   keeps the typed words as its span and gives the stored spelling as `read_as` ("read as 'X'" in the hints and the
   route reason), with the strength of the stored key (a typed word never counts as an unknown word). A value
   reaches a spot this way only when the spelling help's vocabulary holds it. The vocabulary is unchanged, so the
   large Name sets (companies, institutes, codes) are not read when misspelt (owner point). With
   `SFA_RESOLVER=off`, spotting reads the typed words alone.

Not wired (open): the knowledge per-object tools (`describe_object`, `get_field`, ...) still read their object and
field arguments exactly (the knowledge-side tiers of step (3)); the E4h close-spellings list (`LookupsMixin.
_close_parts`) does not put the resolver's names first; `people_nudges` does not yet nudge when several people share
a corrected first name. See the status table in [resolver_integration.md](resolver_integration.md).

### How to switch it off

The switch is on by default since 2026-10-27. The owner decided on 2026-10-26, after the evening evaluation of
2026-10-22 ([resolver_integration.md](resolver_integration.md) (8)): "make it true and the required changes as we will
use it from now to understand the miswritten words". Unset or empty is on, `SFA_RESOLVER=off` turns it off and
`SFA_RESOLVER=on` keeps it on. Two paths stay off whatever the switch says, because they must be exact: the evaluation's
reference replay of the golden tool calls (`open_grading`) and the parity harness (OLD has no resolver).

- **Terminal and evaluation runner:** `./ask.sh "how many interviews are Completd"` uses it;
  `SFA_RESOLVER=off ./ask.sh ...` runs without it. `python -m sf_assistant.evaluation run ...` answers through the
  same cli, so it follows the same switch (export `SFA_RESOLVER=off` for a run without it). The start-up lines say
  `spelling help on (...)` with the counts, or `spelling help off: <reason>`. A value other than `off`, `on` or empty
  never stops anything: the spelling help stays off and the start-up line says
  `spelling help off: SFA_RESOLVER must be off or on`. `web/run.sh up` and `up-mcp` refuse such a value before
  starting anything, so a typo never reaches the page.
- **Word list:** the system lists (`/usr/share/dict/american-english`, `british-english`; Debian/Ubuntu packages
  `wamerican wbritish`) or `SFA_LEXICON=<file>[:<file>...]`. Without a readable list the spelling help stays off.
- **The page and the MCP server:** `web/compose.yaml` passes `SFA_RESOLVER` to both containers (empty = on, the code
  default). The image gets the word lists from `web/Dockerfile` (an image without them leaves the spelling help off
  with a start-up line). A container started without a value takes the code default at its next start
  (`./run.sh restart-page`, `restart-mcp`: the code is mounted). `cd web && SFA_RESOLVER=off ./run.sh up` turns it
  off (rebuilds the image, recreates both containers and signs everyone out); `SFA_RESOLVER=off ./run.sh up-mcp`
  does the MCP server alone. To switch it on again, run the same command without the variable. Docker compose also
  reads a git-ignored `web/.env` file when there is one; a value in the shell wins over it. Check the start-up lines
  in `docker compose -p sf-assistant -f compose.yaml logs sf-assistant-web` (the `knowledge_built` line gets a
  `spelling` key; `records_opened` lists the full resolver's line) and in `sf-assistant-mcp`'s log (its knowledge
  tools get the knowledge resolver too, its record tools the full one).
- **Cost** (the one measured table; measured 2026-10-21 on the 2026-10-13 snapshot, one fresh process per run in the
  page's shape: `pipeline.build`, then a record session through the MCP server's `Records`, then 3 reopens as after a
  read-copy swap; the machine had a load of 6-7 from other users, so the absolute times are higher than on a quiet
  one):

  | what | switch off | switch on | difference |
  |---|---|---|---|
  | `pipeline.build` (the knowledge resolver) | 0.8 s, 123 MB resident | 1.9-2.0 s, 157-159 MB | +1.1 s, +35 MB |
  | page start: build plus the first record session open | open 3.2-3.4 s, 548-555 MB | open 5.8-6.1 s, 648-649 MB | +2.6 s on the open, +94-100 MB |
  | each reopen after a read-copy swap (the first question or MCP call after it waits for it) | 3.1-3.4 s | 4.8-5.5 s | +1.7-2.1 s |
  | resident after 3 reopens | 562-575 MB | 671-688 MB | +105-119 MB |
  | peak (high-water mark) of the process | 621-627 MB | 729-730 MB | +103-108 MB |

  A reopen releases the old session (and its resolver) before the new open, and it reads only the snapshot again:
  the knowledge and word-list part is kept per process (`terms.start_vocabulary`), which saves about 0.5-0.7 s of
  each reopen (the start-up line says 1.9 s on a reopen, 2.4-2.6 s on the first open). A full garbage collection
  takes about 55-80 ms with the switch on against 20-29 ms off (review 2026-10-20; negligible). Before the review
  fixes the old session stayed referenced during and after each reopen: about +145-160 MB for the page process.

## Files and tests

| file | what |
|---|---|
| `src/sf_assistant/knowledge/resolver.py` | data types, normalisation, similarity, slip model, indexes, `resolve_term`, `spell_correct` |
| `src/sf_assistant/knowledge/terms.py` | the knowledge files and the English word lists (`load_knowledge`, `add_knowledge`, `load_lexicon`, `knowledge_vocabulary`), `knowledge_resolver` and its start-up lines |
| `src/sf_assistant/engine/vocabulary.py` | `build_vocabulary`: the knowledge part plus the snapshot (stored values, person names); `SnapshotPolicy` |
| `tests/test_resolver_hints.py` | steps (1), (2), (6): off, no expander, no `read_as`, the mixin's hints and no `question_spelling`; on, misspelt query words searched as they read (a correct query never reaches the expander), the hints' Spelling section first (metadata only; people left to the people lines; the knowledge side never sees a stored value), the corrected search text keeping the session's people, the "Read as" line (once, before `Source:`, skipped when stated, never in `body`) and the one-word name note |
| `tests/test_resolver_people.py` | step (5): off, no spelling-help code runs; on, a doubled-letter or same-sound name is spotted and stated first, other one-word typos become missing names with did-you-means, `resolve_person_name` and `about_people` give "closest stored name", E15a lists the resolver's names first, a filter never binds a misspelt person, a broken resolver changes nothing, the triage's `spelling` list; a live twin |
| `tests/test_resolver_answer.py` | steps (1), (6) through `pipeline.answer` with a fake model: off, no block, no line, no JSONL key; on, the knowledge step gets the block (lean and full), `final` gets the line before `Source:` and `body` does not, the JSONL keeps the user's words plus `spelling` and `read_as_line`; a correct question answered as with the switch off |
| `tests/test_resolver_catalog.py` | steps (3)-(4): the switch off changes nothing (even inside a tool call); correct names never reach the resolver; object, field and value typos used only inside a tool call and stated (note, `read_as` with the argument path, evidence); did-you-means for ambiguous, `exact` and out-of-call readings with the old error text kept; E1b, E1c, E1d, E2b, E2c kept; a truncated index or a person's name never bound; the switch-on twins of the `Interviewz` and E4 `Close:` expectations; correct calls equal with the switch on and off; the catalog's object and field indexes with the expander (`find_objects` `read_as`; a correct word a small index lacks never read as another); a live twin (`OKEVAL_RECORDS_LIVE=1`, counts only) |
| `tests/test_resolver_wiring.py` | the build behind `SFA_RESOLVER`: unset or empty is on (the default); off builds and imports nothing; on, the knowledge resolver (metadata only, shared), the full resolver through the guard equal to the offline build, cut statements counted, failures and a missing word list leaving it off with a reason, the start-up lines, `--no-records` with no engine module; a live twin (`OKEVAL_RECORDS_LIVE=1`) |
| `tests/test_resolver.py` | 81 tests: numbers never read as other numbers, romanized Hindi question words known, the knowledge part read once per folder and process, people-name text fields giving no values (review 2026-10-21); the knowledge vocabulary equal to `build_vocabulary` without a snapshot, `knowledge_resolver` (word list required, never raises), and every tier, the slip model (slip costs, ranking by slips, wide did-you-means that are never corrected and never reach `spell_correct`), every guard rail and confirmation rule (English words, affixes, single names, first sound, missing and extra words, fillers, negations), names not singularised, plurals, ambiguity and ties, context and `within`, kinds, `spell_correct` spans, run extension, real-word name errors, rare stored typos, determinism (term order, hash seeds, surviving source), thread safety, speed (20k-term synthetic vocabulary; 1,000-letter tokens), vocabulary from fake knowledge, a fake word list and a fake DuckDB file (sensitive objects, blocked columns, roles, identifiers, free text, repetition, person names, non-human users, titles and initials), the caller's connection and query function with a policy, light import, standard-library-only imports |

Run: `.venv/bin/python -m pytest tests/test_resolver.py tests/test_resolver_*.py` (each wiring test sets the switch itself; the whole suite passes with the switch unset, which is on, with `SFA_RESOLVER=off` and with `SFA_RESOLVER=on`). The model-free switch-on checks on the real data (tool sweeps, question sets, answer lines) are in `.private/resolver_wiring_checks_2026-10-20.md` (real names: not tracked).
