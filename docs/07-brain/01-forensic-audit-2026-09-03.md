# Forensic audit: why one user "knew" the CEO and another did not (2026-09-03)

Scope: the master task's Phase 1. The symptom was two accounts asking the same
public question ("who is ceo of techsara") in Fast mode and getting different
answers — one named the person, the other said the sources did not name
anyone and cited unrelated Wikipedia pages. The owner's diagnosis was "one
user's memory has it, the other's does not". That diagnosis is *partly*
right and mostly misleading; the real causes are below, each reproduced on
the live deployment and traced to a line of code.

Everything here was reproduced with `scratchpad/forensic.py`,
`candidates.py`, `partition.py`, `ab-recall.py` and `rerank-sanity.py`, run
inside the orchestrator container against production data, with throwaway
sessions and conversations that were deleted afterwards. No hard-coded
people, sites or answers are used anywhere in the fix; the entity below is
only the reproduction case.

## 1. The reproduction

Same question, two accounts, three settings each (the two accounts are the
owner and one ordinary member):

| account | effort / web search | route | TTFT | cited sources | answer |
|---|---|---|---|---|---|
| owner | fast / auto | chat, grounded from local store | 1.0 s | company LinkedIn page, a company-register page | names the CEO |
| member | fast / auto | chat, grounded from local store | 0.9 s | **identical** two pages | "sources do not explicitly name the CEO" |
| owner | think / auto | live search | 17.5 s | 15 live pages (incl. baidu, an unrelated defence-contractor article) | names the CEO |
| member | think / auto | live search | 18.9 s | 15 live pages (incl. zhihu) | names the CEO |
| owner | fast / on | live search | 3.7 s | 8 live pages | names the CEO |
| member | fast / on | live search | 7.1 s | 8 live pages (incl. "Electric current — Wikipedia") | names the CEO |

Two facts fall out immediately:

1. In Fast/auto the two accounts received the **same grounding block**
   (4,568 characters from the same two stored pages). Neither page contains
   the CEO's name. So the owner's correct answer was *not* supported by the
   evidence shown to the model.
2. Every live-search variant answers correctly, but takes 4–19 s and cites
   noise, even though the local store already held pages that name the CEO
   (the org-chart site, read 2026-09-02 during a research run, plus a
   company-register page listing the board).

## 2. Cause A — the local retrieval drops the pages that answer

`web_memory.retrieve` builds candidates from two halves and then partitions
them for "supersession" before cutting to `top_k`.

**A1. Undated pages are treated as the freshest evidence and supersede dated
ones.** `core/provenance.effective_time` (`orchestrator/app/core/provenance.py:152`)
returns `published or modified or fetched`. A page whose HTML states a
publication date gets that date; a page that states none gets its fetch
time. For a RECENT question, `web_memory._partition`
(`orchestrator/app/web_memory.py`, "Conflict / supersession") drops any
evidence older than `newest + 45 days` when a fresher page of equal or
higher authority exists.

Measured on the live corpus for the reproduction question:

| page | contains the answer | content date used | age | outcome |
|---|---|---|---|---|
| org-chart office page | yes ("Founder and CEO …") | published 2026-06-15 (from the page) | 80 d | **dropped as superseded** |
| org-chart main page | yes | published 2026-01-01 (year-only guess from the page) | 245 d | dropped |
| company LinkedIn page (`in.` mirror) | no | none → fetch time | 1 d | kept, ranked #1 |
| company LinkedIn page (`www.`) | no | published 2026-06-30 | 65 d | dropped — the *same content* as the kept copy |
| company-register page | no | none → fetch time | 1 d | kept, ranked #2 |

The supersession rule was written for "the office changed hands" and it fires
on any dated page whenever an undated page exists. With no date, a page
cannot be superseded and always looks new. The pages that answer the question
were removed *because* they were honest about their date.

**A2. The lexical half ranks by term frequency across the whole corpus.**
`_lexical_candidates` (`orchestrator/app/web_memory.py:406`) ORs the
question's terms and orders by `ts_rank_cd`. For "who is ceo of techsara
solutions" the top lexical candidates are:

```
rank=67.4  www.microsoft.com   Recognized Solution Architects | Microsoft Dynamics
rank=27.2  en.wikipedia.org    Chief executive officer - Wikipedia
rank=27.2  en.m.wikipedia.org  Chief executive officer - Wikipedia
rank=19.6  prospectoo.com      Techsara Solutions - …
rank=17.6  en.wikipedia.org    Jensen Huang - Wikipedia
…
```

The org-chart pages are not in the top 15 at all. The AND form of the same
query (`websearch_to_tsquery`) ranks them 1–9. The OR form was introduced to
fix a different case (a question word absent from a page) and traded away
precision. This is also where the "Sundar Pichai / Google" citations in the
owner's screenshot came from: before the relevance gate existed, those
Wikipedia pages were passed straight through as sources.

**A3. "Sufficient" is judged on entity overlap, not on whether the passage
answers.** `Retrieval.sufficient` (`orchestrator/app/web_memory.py:199`)
accepts the result when two "relevant" passages exist, where relevant means
lexical overlap ≥ 0.34 (two of three question terms: the entity name) or a
strong dense match. Both kept pages are *about* the entity and neither
mentions the office asked about, yet the result is "sufficient", so
`living_knowledge.prepare` (`orchestrator/app/living_knowledge.py:134`)
returns without any live lookup and the grounding block tells the model:
"Answer from these sources. If they do not actually contain the answer, say
what you do not know". The member's answer is therefore the *correct*
behaviour for the evidence given.

## 3. Cause B — the owner's answer came from the recall of an earlier answer

`memory_semantic.cross_chat_block` (`orchestrator/app/memory_semantic.py:159`)
injects, as a system message *ahead of* the grounding, snippets from the
user's other conversations, including the assistant's own earlier replies.
The owner had previously asked this question in a conversation that ran a
live search, so the recall block contained:

```
- From "hi" (you answered): The CEO of TechSara is **Sahil Patel**.
```

The member's recall block contained nothing about the question.

Controlled A/B (`ab-recall.py`, same grounding, three runs each, Fast):

| prompt | runs naming the CEO |
|---|---|
| grounding only | 0 / 3 |
| grounding + owner's recall block | 3 / 3 |

So the owner's "knowledge" is the model repeating its own prior answer, and
it attached citations `[1] [2]` to sources that do not contain the claim.
That is a faithfulness failure: the citation format implies evidence that is
not there. Cross-chat recall is per-user by design (correct — it is private
memory), which is exactly why one account "knew" and the other did not.
Public knowledge learned in one user's live search must live in the *shared*
store and be retrievable by everyone; it must not live only in a private
recall snippet.

## 4. Cause C — the Think and forced-search paths ignore the store for "who is"

`engines/search._memory_sources` (`orchestrator/app/engines/search.py:696`)
skips stored knowledge entirely when the question matches `_FRESH_RE`
(`…|who is|what is the|…`, line 133). Think/auto and Fast/on therefore
answer only from live results. Live results for this question included
"Chief Electoral Officer" state sites and "Electric current — Wikipedia"
(the reranker used at that stage scores raw title+snippet pairs, see D).
The answer was right this time because two live pages happened to name the
person, at a cost of 4–19 s and a source list the user rightly distrusts.

The same question, in the same deployment, thus takes three different
evidence paths depending on a toggle and an effort setting:

| path | evidence | store consulted | reranker |
|---|---|---|---|
| Fast/auto | stored pages, top 5 hybrid, no cross-encoder | yes (dated pages penalised) | no |
| Think/auto | live pages only | no (fresh-intent skip) | raw snippets |
| Fast/on | live pages only | no | raw snippets |

## 5. Cause D — the reranker is deployed but effectively unused

A cross-encoder (`Qwen3-Reranker-0.6B`, its own vLLM container, `/score`)
is available and cheap (82 ms for 8 passages, 172 ms for 30, measured), but:

- the local knowledge path (`web_memory.retrieve`) never calls it;
- the two callers that do (`engines/rag._remote_rerank`,
  `engines/search._rerank_results`) send **raw text** rather than the
  instruction template the model was trained with.

Measured with six controlled passages for the reproduction question:

| passage | raw `/score` | templated |
|---|---|---|
| org chart naming the CEO | 0.890 | **0.9997** |
| register listing the board (names the person) | 0.345 | **0.9947** |
| company careers page | 0.702 | 0.088 |
| company social post | 0.487 | 0.648 |
| placement PDF | 0.477 | 0.0000 |
| "Chief executive officer" (Wikipedia) | 0.269 | 0.004 |

Raw scoring puts a careers page above the board listing; templated scoring
separates answering from non-answering passages by three orders of magnitude.
This affects every reranked path on the platform (Salesforce RAG, search
candidate selection), not only this question.

## 6. What was *not* the cause

- Not the vector index: the org-chart chunks are present in LanceDB
  (`indexed_at` set, index backlog 0) and appear in the dense top-15.
- Not user permissions or workspace scoping: the web store is global and
  both accounts read the same rows.
- Not model non-determinism: the without-recall condition was 0/3, the
  with-recall condition 3/3.
- Not the freshness classifier: both accounts classified the question
  RECENT via the office-holder rule, as intended.

## 7. Consequences for the design (see the ADR)

1. One evidence pipeline for every route (Fast chat, Think search, forced
   search, research), so the same question yields the same evidence.
2. Answerability, judged by the templated cross-encoder, decides
   sufficiency — not entity overlap.
3. Content-date handling: an undated page can never supersede a dated one;
   year-only dates are low precision; supersession applies only among
   passages that actually answer.
4. Recall of the assistant's *own earlier answers* is not evidence for a
   time-sensitive or factual question; it is labelled as such or excluded,
   and citations may only point at numbered sources.
5. Public knowledge learned through any user's live search is written to
   the shared store (already true) **and** is preferred over a live search
   when it answers and is fresh enough (new).
6. The templated reranker becomes the single shared scorer.
