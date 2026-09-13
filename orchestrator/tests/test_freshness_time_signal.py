"""Which Fast questions may skip the freshness router (performance plan item 2,
revised 2026-09-13).

The first version skipped the router for any undecided question with no
recency word. The prover measured the cost: "euro to dollar", "is AWS down",
"Tesla share value" and "score of india vs australia" were answered STATIC from
model weights, where HEAD's RECENT router verdict made the Fast live lookup on
4 of 4. No recency word is not the same as timeless.

The skip is now an allowlist (`freshness.clearly_timeless`): a positively
timeless task with no live-value signal. These tests pin both sides over the
question sets below — every live-value question must reach the router (or be
decided time-sensitive by the regex pass), and plainly timeless tasks may skip.
"""
import asyncio

import pytest

from app import freshness
from app.freshness import Freshness, Verdict, clearly_timeless, router_would_be_asked

NOW_YEAR = 2026

#: Live-value questions. The first nine are the prover's; none carries a
#: _RECENT word, which is exactly why the first version skipped them.
LIVE_VALUE = [
    "euro to dollar",
    "is AWS down",
    "Tesla share value",
    "score of india vs australia",
    "upcoming iPhone release date",
    "is the vLLM 0.12 out",
    "GPT-5.2 benchmark score",
    "what did Sam Altman say",
    "tell me about OpenAI o5",
    # Rates and prices without "price".
    "usd to inr",
    "dollar to rupee",
    "1 btc in usd",
    "bitcoin value",
    "gold rate in mumbai",
    "petrol rate in delhi",
    "how much is gold per gram",
    "apple stock",
    "nvidia earnings",
    "sensex",
    # Scores, results, standings, rankings.
    "india vs pakistan result",
    "man city vs arsenal",
    "ipl points table",
    "f1 standings",
    "mistral large benchmark results",
    "best gpu for llm inference",
    # Status and outages.
    "is chatgpt down",
    "is github working",
    "is the bank open on saturday",
    "flight AI 101 status",
    "is it raining in london",
    # Releases, versions, products.
    "when does gta 6 come out",
    "pixel 10 specs",
    "did openai release gpt-6",
    "is ps5 pro out",
    "rust 1.90 features",
    "claude opus 5 context window",
    # People, statements, news.
    "what did elon musk tweet",
    "is taylor swift married",
    "how old is lionel messi",
    "who is sundar pichai",
    "who is the pope",
    "tell me about anthropic",
    "trump tariffs",
    "is the ceasefire holding",
    # A timeless SHAPE wrapped around a live value must not ride through.
    "hi, is aws down",
    "hello, which team is leading the ipl",
    "write a tweet about the india vs australia score",
    "summarize what sam altman said",
    "write a poem about the GPT-5.2 benchmark",
    "tell me a joke about the new iPhone",
    "write an email asking if the euro to dollar rate changed",
    "translate the headline about the fed rate cut",
    "write me a haiku about bitcoin",
    "write a short summary of the Apple event",
    # The second prover pass (2026-09-13): 20 live values in timeless shapes
    # with no word from the noun veto. 15 of these skipped the router on the
    # first allowlist (fast_lookup_probe24.py: HEAD 20/20 attempted, 5/20).
    "write a short poem congratulating the chief justice of india",
    "compose a tweet thanking the finance minister of india",
    "draft a letter to the pope about climate change",
    "write a speech welcoming the secretary general of nato to delhi",
    "tell me a joke about the defending champions of the champions trophy",
    "give me a funny story about the highest grossing movie of all time",
    "write an email to my accountant about the gst on laptops",
    "draft a message to my team about the tariffs on indian exports to the us",
    "write a limerick about the speaker of the lok sabha",
    "write a short essay on the defence minister of india",
    "summarize the plot of the highest grossing bollywood film",
    "write a python function that returns the gst slab for mobile phones",
    "write a toast for the reigning miss universe",
    "write a short story about india's chess world champion",
    "give me some slogans for the ruling party in bihar",
    "draft an email about the h1b lottery changes",
    "write a haiku about the fuel surcharge on indigo tickets",
    "chief justice of india",
    "gst on laptops",
    "tallest building in the world",
    # The same structure, written for the structural veto (closing engineer):
    # a definite object, a possessive, or a role/record/rate word.
    "write a thank-you note to apple's ceo",
    "draft a speech for the incumbent mayor of london",
    "write a haiku about the world's richest man",
    "write a poem for the governor of the reserve bank",
    "write a limerick about the vat on books",
    "write a toast to the newly elected speaker",
    "give me a funny poem about the tallest building",
    "write a letter to the queen of england",
    "write a joke about the opposition leader",
    "draft an email to the tax commissioner about customs duty on phones",
    "write a poem for chief justice gavai",
    "compose a song about the reigning chess champion",
]

#: Plainly timeless tasks. Each is either settled STATIC by the regex pass or
#: allowed to skip the router at Fast.
TIMELESS = [
    "write me a haiku",
    "write me a haiku about autumn",
    "Write a poem about the ocean",
    "compose a limerick about a sleepy cat",
    "can you write a short story about a dragon",
    "tell me a joke about cats",
    "tell me a bedtime story",
    "write a birthday wish for my mom",
    "suggest some names for my cat",
    "hello how are you",
    "hello, how are you?",
    "good morning",
    "hi",
    "thanks!",
    "thank you so much",
    "explain recursion",
    "translate this sentence",
    "translate 'good night' into French",
    "rewrite this paragraph to sound more formal",
    "make this more formal",
    "proofread my essay",
    "summarize the plot of hamlet",
    "write a python function to reverse a string",
    "how do I reverse a list in python",
    "How do I center a div in CSS",
    "write a regex to match email addresses",
    "write a sql query to find duplicate rows",
    "write a function to find the largest number in a list",
    "fix this code",
    "solve 2x + 3 = 7",
]


def test_the_question_sets_are_big_enough_to_mean_something():
    assert len(LIVE_VALUE) >= 40 and len(TIMELESS) >= 10
    assert len(set(LIVE_VALUE)) == len(LIVE_VALUE) and len(set(TIMELESS)) == len(TIMELESS)


@pytest.mark.parametrize("question", LIVE_VALUE)
def test_a_live_value_question_is_never_clearly_timeless(question):
    assert not clearly_timeless(question, now_year=NOW_YEAR)


@pytest.mark.parametrize("question", LIVE_VALUE)
def test_a_live_value_question_reaches_the_router_or_is_decided_time_sensitive(monkeypatch, question):
    """What classify does with it: the regex verdict, or the router's. With a
    router that says RECENT, every one needs evidence."""
    calls = []

    async def ask(q):
        calls.append(q)
        return Verdict(Freshness.RECENT, freshness._MAX_AGE[Freshness.RECENT], "router")

    monkeypatch.setattr(freshness, "_ask_router", ask)
    verdict = asyncio.run(freshness.classify(question, now_year=NOW_YEAR, allow_router=True))
    assert verdict.needs_evidence, (question, verdict)
    assert bool(calls) == router_would_be_asked(question, now_year=NOW_YEAR)


@pytest.mark.parametrize("question", TIMELESS)
def test_a_plainly_timeless_task_never_costs_a_router_call_at_fast(question):
    decided_static = (
        not router_would_be_asked(question, now_year=NOW_YEAR)
        and not freshness.classify_offline(question, now_year=NOW_YEAR).needs_evidence
    )
    assert decided_static or clearly_timeless(question, now_year=NOW_YEAR), question


@pytest.mark.parametrize("question", [
    "what is the price of a used bicycle",   # ambiguous: timeless shape AND a recency word
    "who is the ceo of nvidia",              # decided: office
    "What is photosynthesis?",               # decided: static
    "latest vllm release",                   # decided: strong recent
    "stock price right now",                 # decided: realtime
    "write a poem about the 2024 election",  # a year inside a creative task
    "",
])
def test_an_ambiguous_or_decided_question_is_not_a_skip_candidate(question):
    assert not clearly_timeless(question, now_year=NOW_YEAR)


@pytest.mark.parametrize("question", [
    "hello, what is the dollar rate",
    "thanks, and who won",
    "hi there is aws down",
])
def test_small_talk_skips_only_when_the_whole_message_is_small_talk(question):
    assert not clearly_timeless(question, now_year=NOW_YEAR)


@pytest.mark.parametrize("question", [
    "what's the capital of france",   # timeless, but not a TASK shape: the router decides
    "sam altman",                     # a bare name
    "o5",
    "recipe for pancakes",
])
def test_a_question_this_code_has_no_opinion_on_goes_to_the_router(question):
    """The allowlist, not the veto, is the primary guard: an unrecognised shape
    costs one router call, never an answer from weights."""
    assert router_would_be_asked(question, now_year=NOW_YEAR)
    assert not clearly_timeless(question, now_year=NOW_YEAR)


def test_the_skip_verdict_is_static_and_names_its_rule():
    v = freshness.static_timeless_task()
    assert v.requirement is Freshness.STATIC and not v.needs_evidence
    assert v.reason == freshness.TIMELESS_TASK_REASON == "timeless_task"


def test_the_skip_rule_is_in_the_freshness_metric_vocabulary():
    from app import metrics

    assert freshness.TIMELESS_TASK_REASON in metrics._ALLOWED["rule"]


@pytest.mark.parametrize("question", [
    "hello, how are you?", "what is the price of a used bicycle", "who is the ceo of nvidia",
    "What is photosynthesis?", "the 2019 election", "summarize the plot of hamlet", "euro to dollar",
])
def test_router_would_be_asked_is_exactly_when_classify_asks_it(monkeypatch, question):
    calls = []

    async def ask(q):
        calls.append(q)
        return Verdict(Freshness.STATIC, 1, "router")

    monkeypatch.setattr(freshness, "_ask_router", ask)
    asyncio.run(freshness.classify(question, now_year=NOW_YEAR, allow_router=True))
    assert bool(calls) == router_would_be_asked(question, now_year=NOW_YEAR)


def test_a_long_message_is_never_a_skip_candidate_however_it_opens():
    """A paste can hide a live value far past its opening words, and the
    skip's regexes must not scan an unbounded paste on the event loop."""
    long_task = "translate this paragraph: " + "the cat sat on the mat and looked around. " * 20
    assert len(long_task) > freshness._SKIP_MAX_CHARS
    assert router_would_be_asked(long_task, now_year=NOW_YEAR)
    assert not clearly_timeless(long_task, now_year=NOW_YEAR)
    assert clearly_timeless("translate this paragraph: the cat sat on the mat.", now_year=NOW_YEAR)


# ── the object of the task (second prover pass, 2026-09-13) ─────────────────


@pytest.mark.parametrize("question", [
    "translate this paragraph: the cat sat on the mat.",
    "proofread this: the report is attached and the figures are final",
    "Write a poem about the ocean",
    "summarize the plot of hamlet",
    "write a function to find the largest number in a list",
    "write a birthday wish for my mom's 60th",
    "fix the grammar in the following sentence",
])
def test_text_the_person_supplied_and_timeless_objects_do_not_trip_the_object_rules(question):
    assert clearly_timeless(question, now_year=NOW_YEAR), question


@pytest.mark.parametrize("question", [
    "write a poem about: the chief justice of india",  # a role word reads the whole message
    "write a poem about the tax commissioner",
    "write a haiku about google's founder",
    "write a toast for the defending champions",
])
def test_a_definite_object_a_possessive_or_a_role_sends_a_timeless_shape_to_the_router(question):
    assert router_would_be_asked(question, now_year=NOW_YEAR)
    assert not clearly_timeless(question, now_year=NOW_YEAR)
