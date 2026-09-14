"""The answer loop guard (app/core/answer_guard.py, 2026-09-15).

The owner's report: a Fast answer to a reasoning puzzle repeated the same three
steps — "Fill Bottle 2 completely with Hot Water. / Pour from Bottle 2 into
Bottle 1 until Bottle 1 is full. / This doesn't help." — with apologies in
between, until the token ceiling. These tests pin four things:

1. every loop shape stops, and what is kept is a clean prefix of the answer
   with at most two copies of the looping block;
2. every legitimate repetition shape (tables, code, similar list items,
   choruses, cumulative verse, translations, JSON, identical sub-bullets, an
   apology letter) never fires AND streams in exactly the pieces the model
   produced — the guard costs an ordinary answer nothing;
3. a fire ends the upstream stream through continuation.StopGeneration — not
   an error, the generator closed the way Stop closes it;
4. through the chat engine and POST /chat, the stored answer is exactly the
   text that was streamed, the person is told why it stopped, and the metric
   and trace detail are recorded.

All prompts and outputs here are written for these tests.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from app import continuation, db, llm, metrics
from app import main as app_main
from app.core import answer_guard
from app.core.answer_guard import AnswerGuard, normalize
from app.main import _live_generations, app


def run_guard(text: str, piece: int = 4) -> Tuple[AnswerGuard, List[str], List[str]]:
    """Feed `text` in `piece`-sized deltas until it ends or the guard fires.

    Returns (guard, input deltas fed, output pieces)."""
    guard = AnswerGuard()
    fed: List[str] = []
    out: List[str] = []
    for start in range(0, len(text), piece):
        delta = text[start : start + piece]
        fed.append(delta)
        out.extend(guard.feed(delta))
        if guard.verdict is not None:
            break
    else:
        out.extend(guard.finish())
    assert "".join(out) == guard.shown
    return guard, fed, out


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

STEP_A = "Fill Bottle 2 completely with Hot Water."
STEP_B = "Pour from Bottle 2 into Bottle 1 until Bottle 1 is full."


def owner_shaped_loop(rounds: int = 8) -> str:
    """The reported shape: the same steps, "This doesn't help.", an apology."""
    apologies = [
        "I apologize for the confusion. Let me try again.",
        "Sorry, let me re-think this.",
        "My apologies. Let's try a different approach.",
    ]
    out = [
        "Let's think about how to get a 2:5 ratio.",
        "",
        "1. Fill Bottle 1 completely with Cold Water.",
        f"2. {STEP_A}",
        f"3. {STEP_B}",
        "This doesn't help.",
        "",
    ]
    n = 4
    for r in range(rounds):
        out += [apologies[r % 3], "", f"{n}. {STEP_A}", f"{n + 1}. {STEP_B}", "This doesn't help.", ""]
        n += 2
    return "\n".join(out)


def verbatim_block_loop() -> str:
    block = (
        "Let x be the amount of hot water in the bottle.\n"
        "Then the cold water is 5x/2, so the total is 7x/2.\n"
        "Setting 7x/2 equal to the capacity gives x = 2C/7.\n"
    )
    return "We can set up an equation.\n\n" + block * 8


def single_sentence_loop() -> str:
    return "The answer depends on the bottle. " + "You need to measure the water carefully before mixing it together. " * 10


def varied_restart_loop() -> str:
    steps = [
        "Fill the bottle with hot water up to the neck.",
        "Pour out about two sevenths of it into the sink.",
        "Top the bottle up with cold water from the tap.",
    ]
    apologies = [
        "Wait, that is not correct.",
        "I apologize, I made a mistake.",
        "Sorry about that, let me start over.",
        "Oops, this does not work either.",
        "My apologies, let me reconsider.",
    ]
    out = ["Here is one way to do it:", ""]
    for r in range(7):
        # A different step order each round, so there is no fixed period —
        # only the restart shape.
        order = steps[r % 3 :] + steps[: r % 3]
        out += [f"- {s}" for s in order] + ["", apologies[r % len(apologies)], ""]
    return "\n".join(out)


def run_on_loop() -> str:
    clause = "and then you pour the hot water into the bottle and wait for it to settle "
    return "The trick is that you fill it slowly " + clause * 50


def paragraph_loop() -> str:
    para = (
        "The key insight is that the bottle itself can act as the unit of measure. "
        "By filling it completely and pouring it into a larger container, you create a "
        "known reference volume that can be repeated as many times as needed. "
        "Two fills of hot and five of cold give the exact ratio when mixed.\n\n"
    )
    return "Here is the idea.\n\n" + para * 6


def code_loop() -> str:
    fn = (
        "def pour(source, target):\n"
        "    moved = min(source.level, target.capacity - target.level)\n"
        "    source.level -= moved\n"
        "    target.level += moved\n"
    )
    return "Here is a helper:\n\n```python\n" + fn * 20 + "```\n"


LOOPS = [
    ("owner_shaped", owner_shaped_loop(), {"cycle", "restart"}, STEP_B),
    ("verbatim_block", verbatim_block_loop(), {"cycle"}, "Then the cold water is 5x/2"),
    ("single_sentence", single_sentence_loop(), {"cycle"}, "You need to measure the water"),
    ("varied_restart", varied_restart_loop(), {"restart"}, "Pour out about two sevenths"),
    ("run_on", run_on_loop(), {"ngram"}, "wait for it to settle"),
    ("paragraph", paragraph_loop(), {"cycle"}, "The key insight is that"),
    ("code", code_loop(), {"code_cycle"}, "source.level -= moved"),
]


@pytest.mark.parametrize("name,text,signals,marker", LOOPS, ids=[x[0] for x in LOOPS])
@pytest.mark.parametrize("piece", [1, 4, 9])
def test_every_loop_shape_is_stopped_with_a_clean_prefix_kept(name, text, signals, marker, piece):
    guard, fed, out = run_guard(text, piece)
    verdict = guard.verdict
    assert verdict is not None, f"{name} was not detected"
    assert verdict.signal in signals
    shown = guard.shown
    # Stopped early: the rest of the stream was never consumed.
    assert len("".join(fed)) < len(text)
    # A clean prefix of what the model wrote (a code cut also closes the fence).
    kept = shown[: -len("```\n")] if name == "code" else shown
    assert text.startswith(kept.rstrip("\n"))
    assert verdict.trimmed_chars > 0
    # At most two copies of a looping block reached the person (three for a
    # code block, whose lines are withheld from the third copy on; a run-on
    # clause is judged on word spans, a little later).
    limit = {"run_on": 3, "code": 3}.get(name, 2)
    assert 1 <= shown.count(marker) <= limit, shown
    if name == "code":
        assert shown.endswith("```\n"), "the kept markdown must not end inside a code block"
    # Never mid-word: the cut does not fall between two letters of one word.
    end = len(kept)
    assert not (kept[end - 1].isalnum() and text[end].isalnum()), kept[-80:]


def test_the_owner_shaped_loop_keeps_the_first_attempt_and_drops_the_rest():
    guard, _, _ = run_guard(owner_shaped_loop(), 5)
    shown = guard.shown
    assert "1. Fill Bottle 1 completely with Cold Water." in shown
    assert shown.count(STEP_A) <= 2 and shown.count(STEP_B) <= 2
    # The apologies after the second copy never appear.
    assert "Let's try a different approach." not in shown


def test_after_a_fire_nothing_more_is_shown():
    guard, _, _ = run_guard(single_sentence_loop())
    assert guard.verdict is not None
    shown = guard.shown
    assert guard.feed("More text that must not appear. ") == []
    assert guard.finish() == []
    assert guard.shown == shown


def test_scan_reports_the_same_verdict_offline():
    verdict = answer_guard.scan(verbatim_block_loop())
    assert verdict is not None and verdict.signal == "cycle"
    assert answer_guard.scan("A perfectly ordinary answer. It says two things.") is None


def test_the_verdict_carries_a_fingerprint_never_the_text():
    guard, _, _ = run_guard(verbatim_block_loop())
    repeated = guard.verdict.repeated
    assert set(repeated) == {"characters", "sha256"}
    assert len(repeated["sha256"]) == 64
    assert guard.verdict.as_meta() == {"signal": "cycle", "trimmed_chars": guard.verdict.trimmed_chars}


# ---------------------------------------------------------------------------
# Thresholds, pinned on both sides
# ---------------------------------------------------------------------------

SENTENCE = "You need to measure the water carefully before you mix it together in the bottle. "


def test_a_sentence_twice_back_to_back_is_not_a_loop_and_three_times_is():
    two, _, _ = run_guard("Intro line here. " + SENTENCE * 2 + "Then something new happens next.")
    assert two.verdict is None
    three, _, _ = run_guard("Intro line here. " + SENTENCE * 3 + "Then something new happens next.")
    assert three.verdict is not None and three.verdict.signal == "cycle"


def test_a_block_three_times_is_not_a_loop_and_four_times_is():
    block = "Fill the jug with water from the tap.\nPour the jug into the bottle slowly.\nCheck the level against the mark.\n"
    tail = "Finally, close the bottle and shake it well.\n"
    assert run_guard("Steps:\n" + block * 3 + tail)[0].verdict is None
    four = run_guard("Steps:\n" + block * 4 + tail)[0]
    assert four.verdict is not None and four.verdict.signal == "cycle"


def test_a_short_sentence_repeated_needs_enough_repeated_text():
    # Three copies of a 20-character sentence repeat too little to judge...
    assert run_guard("Start. " + "Keep going forward. " * 3 + "Done now, thanks.")[0].verdict is None
    # ...but it does not get to repeat for ever.
    assert run_guard("Start. " + "Keep going forward. " * 40)[0].verdict is not None


def test_a_single_code_line_needs_forty_copies_and_a_block_ten():
    line = "    buffer[index] = compute_checksum(buffer, index, polynomial);\n"
    fence = "```c\n{}```\n"
    assert run_guard(fence.format(line * 39))[0].verdict is None
    assert run_guard(fence.format(line * 41))[0].verdict is not None
    block = "    value = read_register(device, offset);\n    write_register(device, offset, value | FLAG_ENABLED);\n"
    assert run_guard(fence.format(block * 9))[0].verdict is None
    fired = run_guard(fence.format(block * 11))[0].verdict
    assert fired is not None and fired.signal == "code_cycle"


def test_restart_phrases_without_repeated_text_never_fire():
    text = "\n".join(
        [
            "Wait, let me check the first equation again: 2c + 4k = 74.",
            "That is not correct, the second term should be 4k.",
            "Let me recalculate with k = 7, which gives 28 legs from cows.",
            "Sorry for the slip, 46 legs from chickens makes 74 in total.",
            "Let me re-check the heads: 23 + 7 = 30, which matches.",
            "I apologize for the detour; the answer is 23 chickens and 7 cows.",
        ]
    )
    assert run_guard(text)[0].verdict is None


def test_normalisation_names_the_same_step_whatever_its_number():
    assert normalize("1. **Fill** Bottle 2.") == normalize("Step 7: Fill bottle 2") == "fill bottle 2"
    assert normalize("- Fill bottle 2!") == normalize("Attempt 3: FILL BOTTLE 2")
    # Digits inside the sentence are kept: jug states differ only by numbers.
    assert normalize("The 5-litre jug has 3 L.") != normalize("The 5-litre jug has 2 L.")


# ---------------------------------------------------------------------------
# Legitimate repetition
# ---------------------------------------------------------------------------


def legit_table() -> str:
    rows = "\n".join(f"| Laptop {i} | Yes | No | Yes | 1.{i} kg |" for i in range(1, 15))
    return "Here is the comparison:\n\n| Model | Touch | Pen | Backlit | Weight |\n|---|---|---|---|---|\n" + rows + "\n\nPrices vary."


def legit_zero_lut() -> str:
    rows = "\n".join("    {" + ", ".join(["0x00"] * 16) + "}," for _ in range(16))
    return "Here is the table:\n\n```c\nstatic const unsigned char LUT[16][16] = {\n" + rows + "\n};\n```\n"


def legit_bash_separators() -> str:
    body = ""
    for check in ["disk", "memory", "cpu", "network", "dns", "ntp", "swap", "load", "users", "services"]:
        body += f'echo "----------------------------------------"\necho "Checking {check}..."\ncheck_{check}\n'
    return "```bash\n#!/usr/bin/env bash\nset -euo pipefail\n" + body + "```\n"


def legit_css() -> str:
    out = ""
    for variant in ["primary", "secondary", "success", "danger", "warning", "info"]:
        out += (
            f".btn-{variant} {{\n  padding: 0.5rem 1rem;\n  border-radius: 6px;\n  border: none;\n"
            f"  font-weight: 600;\n  cursor: pointer;\n  background: var(--{variant});\n}}\n\n"
        )
    return "```css\n" + out + "```"


def legit_similar_numbered() -> str:
    things = ["type hints", "docstrings", "small functions", "f-strings", "pathlib", "logging", "unit tests", "constants"]
    return "Tips:\n\n" + "\n".join(
        f"{i}. **Use {t}** — use {t} to keep the code readable and easy to maintain." for i, t in enumerate(things, 1)
    )


def legit_jug_puzzle() -> str:
    return "\n".join(
        [
            "Here's how to measure 4 litres with a 3-litre jug and a 5-litre jug:",
            "",
            "1. Fill the 5-litre jug.",
            "   - 3-litre jug: 0 L, 5-litre jug: 5 L",
            "2. Pour from the 5-litre jug into the 3-litre jug until the 3-litre jug is full.",
            "   - 3-litre jug: 3 L, 5-litre jug: 2 L",
            "3. Empty the 3-litre jug.",
            "   - 3-litre jug: 0 L, 5-litre jug: 2 L",
            "4. Pour from the 5-litre jug into the 3-litre jug.",
            "   - 3-litre jug: 2 L, 5-litre jug: 0 L",
            "5. Fill the 5-litre jug.",
            "   - 3-litre jug: 2 L, 5-litre jug: 5 L",
            "6. Pour from the 5-litre jug into the 3-litre jug until the 3-litre jug is full.",
            "   - 3-litre jug: 3 L, 5-litre jug: 4 L",
            "",
            "Now the 5-litre jug holds exactly 4 litres.",
        ]
    )


CHORUS = (
    "Oh, the summer nights are calling out my name,\n"
    "And the city lights will never look the same,\n"
    "So come on, come on, let the music play,\n"
    "We'll be dancing till the break of day.\n"
)


def legit_song() -> str:
    verses = [
        "Walking down the road where the river bends,\nTalking with the stars and my oldest friends,\n",
        "The morning sun is rising on the hill,\nThe world is turning but my heart stands still,\n",
        "Headlights flashing on the empty street,\nFootsteps keeping to a steady beat,\n",
        "Letters on the table that I never sent,\nWondering where all the golden summers went,\n",
    ]
    out = ""
    for n, verse in enumerate(verses, 1):
        out += f"**Verse {n}**\n{verse}\n**Chorus**\n{CHORUS}\n"
    # The outro sings the chorus twice, back to back.
    return out + "**Outro**\n" + CHORUS + CHORUS


def legit_cumulative_with_a_duplicated_first_stanza() -> str:
    """The live sample that taught the block threshold: a prefix-style rhyme
    whose model wrote its first stanza twice, then grew every stanza."""
    base = [
        "This is the house where little Jack lived,",
        "Where the sleepy cat did hide,",
        "The cat that chased the mouse that nibbled the cheese,",
        "The cheese that lay in the house where little Jack lived.",
    ]
    additions = [
        ["The house that the dog barked at,", "The dog that chased the cat,"],
        ["The house that the farmer tended,", "The farmer that owned the dog,"],
        ["The house that the milkman delivered to,", "The milkman that served the farmer,"],
        ["The house that the baker made the cheese in,", "The baker that sold the milkman,"],
    ]
    stanzas = [list(base), list(base)]
    current = list(base)
    for add in additions:
        current = current[:-1] + [current[-1].rstrip(".") + ","] + add + [
            "The cat that chased the mouse that nibbled the cheese,",
            "The cheese that lay in the house where little Jack lived.",
        ]
        stanzas.append(list(current))
    return "**The Cat That Chased the Mouse**\n\n" + "\n\n".join("\n".join(s) for s in stanzas)


def legit_twelve_days() -> str:
    gifts = [
        "a partridge in a pear tree", "two turtle doves", "three French hens", "four calling birds",
        "five golden rings", "six geese a-laying", "seven swans a-swimming", "eight maids a-milking",
        "nine ladies dancing", "ten lords a-leaping", "eleven pipers piping", "twelve drummers drumming",
    ]
    ordinals = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth"]
    stanzas = []
    for day in range(12):
        lines = [f"On the {ordinals[day]} day of Christmas my true love gave to me"]
        for g in range(day, -1, -1):
            lines.append(("and " if g == 0 and day else "") + gifts[g] + ("." if g == 0 else ","))
        stanzas.append("\n".join(lines))
    return "\n\n".join(stanzas)


def legit_refrain_poem() -> str:
    lines = [
        ("The willows lean to hear the water speak,", "and summer fades to gold along the creek,"),
        ("A heron waits where reeds grow tall and thin,", "the evening folds its wings and settles in,"),
        ("The mill wheel turns though no one grinds the grain,", "and clouds come down to whisper of the rain,"),
        ("Beneath the bridge the shadows gather in,", "the old stones hum of places they have been,"),
        ("The children skip their stones and count to ten,", "and call the swallows home to roost again,"),
        ("The autumn leaves go spinning out to sea,", "and every eddy keeps a memory,"),
    ]
    return "\n\n".join(f"{a}\n{b}\nand still the river runs." for a, b in lines)


def legit_translations() -> str:
    return "\n".join(
        [
            'Here is "Where is the train station? I would like a ticket to the city centre, please." in several languages:',
            "",
            "**French:** Où est la gare ? Je voudrais un billet pour le centre-ville, s'il vous plaît.",
            "**Spanish:** ¿Dónde está la estación de tren? Quisiera un billete para el centro, por favor.",
            "**German:** Wo ist der Bahnhof? Ich hätte gern eine Fahrkarte ins Stadtzentrum, bitte.",
            "**Hindi:** रेलवे स्टेशन कहाँ है? मुझे शहर के केंद्र का एक टिकट चाहिए।",
            "**Japanese:** 駅はどこですか。市内中心部までの切符を一枚ください。",
            "",
            "**Hindi (I am hungry. I am tired. I am happy.):** मुझे भूख लगी है। मैं थका हुआ हूँ। मैं खुश हूँ।",
        ]
    )


def legit_json_array_unfenced() -> str:
    names = ["Alice", "Bob", "Chen", "Divya", "Emeka", "Fatima", "Goran", "Hana", "Ivan", "Jia", "Kofi", "Lena"]
    objects = []
    for i, name in enumerate(names, 1):
        objects.append(
            f'  {{\n    "id": {i},\n    "name": "{name}",\n    "role": "viewer",\n    "active": true\n  }}'
        )
    return "Here is the data:\n\n[\n" + ",\n".join(objects) + "\n]"


def legit_json_array_fenced() -> str:
    rows = ",\n".join(f'  {{"id": {i}, "role": "viewer", "active": true, "team": "sales"}}' for i in range(1, 25))
    return "```json\n[\n" + rows + "\n]\n```"


def legit_identical_sub_bullets() -> str:
    out = ["Here's a summary of each tool:", ""]
    for tool in ["Asana", "Trello", "Jira", "ClickUp", "Notion", "Basecamp"]:
        out += [
            f"### {tool}",
            "- **Price:** Free tier available, paid plans from $10 per user per month",
            "- **Best for:** Small teams that need simple task boards",
            "- **Integrations:** Slack, Google Drive, GitHub",
            "",
        ]
    return "\n".join(out)


def legit_apology_letter() -> str:
    return "\n\n".join(
        [
            "Dear Ms. Patel,",
            "I sincerely apologize for the delay in delivering your order. I apologize as well for the lack of updates while it was held at our warehouse.",
            "I am sorry that this happened twice. Your order was shipped this morning by express courier and should arrive by Friday.",
            "As an apology for the inconvenience, we have refunded the shipping cost and added a voucher to your account.",
            "Once again, I apologize for the trouble, and thank you for your patience.",
            "Kind regards,\nDaniel",
        ]
    )


LEGIT = [
    ("markdown_table", legit_table()),
    ("code_zero_lookup_table", legit_zero_lut()),
    ("code_repeated_separator_lines", legit_bash_separators()),
    ("code_css_variants", legit_css()),
    ("numbered_similar_items", legit_similar_numbered()),
    ("jug_puzzle_repeated_steps", legit_jug_puzzle()),
    ("song_chorus_and_outro", legit_song()),
    ("cumulative_rhyme", legit_cumulative_with_a_duplicated_first_stanza()),
    ("twelve_days", legit_twelve_days()),
    ("refrain_poem", legit_refrain_poem()),
    ("translations", legit_translations()),
    ("json_array_unfenced", legit_json_array_unfenced()),
    ("json_array_fenced", legit_json_array_fenced()),
    ("identical_sub_bullets", legit_identical_sub_bullets()),
    ("apology_letter", legit_apology_letter()),
]


@pytest.mark.parametrize("name,text", LEGIT, ids=[x[0] for x in LEGIT])
@pytest.mark.parametrize("piece", [1, 4, 11])
def test_legitimate_repetition_never_fires_and_is_shown_whole(name, text, piece):
    guard, fed, out = run_guard(text, piece)
    assert guard.verdict is None, f"{name} fired: {guard.verdict}"
    assert guard.shown == text
    # Every piece shown is one of the model's own deltas, in order.
    assert out == fed


#: Legitimate texts that DO write something twice back to back — the song's
#: outro chorus, the rhyme's duplicated first stanza — are briefly withheld
#: after the second copy (it could be a loop's third); everything else
#: streams untouched.
_WRITES_A_BLOCK_TWICE = {"song_chorus_and_outro", "cumulative_rhyme"}


@pytest.mark.parametrize(
    "name,text",
    [x for x in LEGIT if x[0] not in _WRITES_A_BLOCK_TWICE],
    ids=[x[0] for x in LEGIT if x[0] not in _WRITES_A_BLOCK_TWICE],
)
def test_legitimate_repetition_streams_without_being_held(name, text):
    """No stutter: each delta is shown the moment it arrives."""
    guard = AnswerGuard()
    for start in range(0, len(text), 4):
        delta = text[start : start + 4]
        assert guard.feed(delta) == [delta], f"{name} held text at offset {start}"


def test_an_ordinary_answer_streams_delta_by_delta():
    deltas = ["Hi", " there", "!", " TCP", " is", " connection", "-oriented", ".", " UDP", " is", " not", "."]
    guard = AnswerGuard()
    assert [guard.feed(d) for d in deltas] == [[d] for d in deltas]
    assert guard.finish() == []


def test_a_back_to_back_repeat_is_withheld_and_released_in_its_own_pieces():
    """Two copies back to back: the text after them is withheld (it may be a
    third copy) and released, piece by piece, when something new arrives."""
    text = "Intro sentence first. " + SENTENCE * 2
    guard = AnswerGuard()
    fed = [text[i : i + 5] for i in range(0, len(text), 5)]
    for delta in fed:
        guard.feed(delta)
    assert guard.feed("Then the ") == []
    new = "next part of the answer explains something new entirely. "
    released = guard.feed(new)
    # Released as the model's own pieces, in order, ending with the new text.
    assert released[-2:] == ["Then the ", new]
    assert guard.finish() == []
    assert guard.shown == text + "Then the " + new
    assert guard.verdict is None


def test_withheld_text_is_released_when_the_stream_ends():
    guard = AnswerGuard()
    first = "Intro sentence first. " + SENTENCE * 2
    guard.feed(first)
    assert guard.feed("Last") == []
    assert guard.finish()[-1] == "Last"
    assert guard.shown == first + "Last"


def test_nothing_is_withheld_past_the_cap():
    guard = AnswerGuard()
    guard.feed("Intro sentence first. " + SENTENCE * 2)
    # Text that keeps the hold (no complete new sentence) but grows past the cap.
    released = []
    for _ in range(answer_guard.HOLD_CAP_CHARS // 10 + 5):
        released += guard.feed("word word ")
    assert released, "a hold must not stall the answer past HOLD_CAP_CHARS"


def test_a_long_single_line_costs_linear_time_and_is_shown_whole():
    """A data URI or a base64 blob is one enormous line and one enormous
    "word". Accumulating either with `+=` would be quadratic — minutes of CPU
    on the event loop for one answer."""
    import base64
    import random
    import time

    blob = base64.b64encode(random.Random(7).randbytes(150_000)).decode()  # 200 k chars
    text = "Here is the file: data:application/octet-stream;base64," + blob + "\n\nDone."
    started = time.perf_counter()
    guard, fed, out = run_guard(text, 4)
    elapsed = time.perf_counter() - started
    assert guard.verdict is None and guard.shown == text
    # ~51 k deltas; measured ~0.07 s. The bound only has to catch quadratic.
    assert elapsed < 5.0, f"{elapsed:.2f}s for {len(fed)} deltas"


def test_the_guard_costs_well_under_a_twentieth_of_a_millisecond_per_delta():
    """The budget is 0.05 ms per delta (the engine decodes ~100 tok/s: 10 ms
    per token). Measured mean ~0.001 ms; asserted with a wide margin for slow
    CI runners."""
    import time

    text = "\n\n".join(t for _, t in LEGIT) * 3
    deltas = [text[i : i + 4] for i in range(0, len(text), 4)]
    guard = AnswerGuard()
    started = time.perf_counter()
    for delta in deltas:
        guard.feed(delta)
    per_delta_ms = (time.perf_counter() - started) * 1000 / len(deltas)
    assert per_delta_ms < 0.05, f"{per_delta_ms:.4f} ms per delta"


def test_code_lines_repeating_as_a_block_are_withheld_from_the_third_copy():
    block = "    value = read_register(device, offset);\n    write_register(device, offset, value | FLAG_ENABLED);\n"
    guard = AnswerGuard()
    shown_before = guard.feed("```c\n" + block * 2)
    assert "".join(shown_before) == "```c\n" + block * 2
    assert guard.feed(block) == [], "the third copy of a code block is withheld"
    # A different line releases it: three copies of a block can be legitimate.
    released = guard.feed("    return finalize_register_value(device, value);\n")
    assert "".join(released) == block + "    return finalize_register_value(device, value);\n"
    assert guard.verdict is None


def test_reasoning_like_text_with_restarts_is_left_alone():
    guard, _, _ = run_guard(
        "Try w = 5: legs = 70. That's not correct.\nTry w = 6: legs = 72. That's not correct either.\n"
        "Try w = 7: legs = 74. That works.\nWait, let me check it another way: 2w = 14, so w = 7.\n"
        "So there are **23 chickens and 7 cows**."
    )
    assert guard.verdict is None


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_record_counts_by_effort_route_and_signal_and_traces_no_text(monkeypatch):
    metrics.reset()
    events = []

    async def fake_event(stage, **kwargs):
        events.append((stage, kwargs))

    from app.core import tracing

    monkeypatch.setattr(tracing, "event", fake_event)
    guard, _, _ = run_guard(verbatim_block_loop())
    asyncio.run(answer_guard.record(guard.verdict, effort="fast", route="chat"))
    asyncio.run(answer_guard.record(guard.verdict, effort="not-an-effort", route="chat"))
    rendered = metrics.render()
    assert 'answer_loop_guard_total{effort="fast",route="chat",signal="cycle"} 1\n' in rendered
    assert 'answer_loop_guard_total{effort="other",route="chat",signal="cycle"} 1\n' in rendered
    stage, kwargs = events[0]
    assert stage == answer_guard.TRACE_STAGE and kwargs["status"] == "info"
    details = kwargs["details"]
    assert details["signal"] == "cycle" and details["trimmed_characters"] > 0
    assert "Let x be" not in json.dumps(details), "the trace must never carry answer text"


def test_record_never_raises_when_tracing_fails(monkeypatch):
    from app.core import tracing

    async def broken(stage, **kwargs):
        raise RuntimeError("trace store down")

    monkeypatch.setattr(tracing, "event", broken)
    guard, _, _ = run_guard(verbatim_block_loop())
    asyncio.run(answer_guard.record(guard.verdict, effort="fast", route="chat"))


# ---------------------------------------------------------------------------
# continuation.StopGeneration: a consumer ends the run on purpose
# ---------------------------------------------------------------------------


class ScriptedModel:
    """`llm.stream_chat_events` + finish reason + usage, from a script of
    (deltas, finish_reason) per call. Records how far each stream was read and
    whether it was closed."""

    def __init__(self, script: List[Tuple[List[str], Optional[str]]], reasoning: Optional[List[str]] = None):
        self.script = script
        self.reasoning = reasoning or []
        self.calls = 0
        self.yielded = 0
        self.closed = 0
        self.reason: Optional[str] = None

    async def stream(self, messages, **kwargs):
        deltas, reason = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        self.reason = None
        try:
            for piece in self.reasoning:
                yield "reasoning", piece
            for piece in deltas:
                self.yielded += 1
                yield "token", piece
            self.reason = reason
        finally:
            self.closed += 1

    def finish_reason(self):
        return self.reason

    def usage(self):
        return None


@pytest.fixture()
def scripted(monkeypatch):
    def install(script, reasoning=None) -> ScriptedModel:
        fake = ScriptedModel(script, reasoning)
        monkeypatch.setattr(llm, "stream_chat_events", fake.stream)
        monkeypatch.setattr(llm, "get_finish_reason", fake.finish_reason)
        monkeypatch.setattr(llm, "get_usage", fake.usage)
        return fake

    return install


def pieces(text: str, size: int = 6) -> List[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def test_stop_generation_ends_the_run_without_an_error_and_closes_the_stream(scripted, caplog):
    fake = scripted([(pieces("one two three four five six seven eight nine ten " * 20), "stop")])
    seen: List[str] = []

    async def on_delta(kind, text):
        seen.append(text)
        if len(seen) == 5:
            raise continuation.StopGeneration(continuation.STOP_REPETITION)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            continuation.stream_long_completion([{"role": "user", "content": "go"}], on_delta=on_delta)
        )
    assert result.stop_reason == continuation.STOP_REPETITION
    assert result.truncated is True
    assert result.errors == []
    assert result.segment_count == 1
    assert fake.calls == 1
    assert fake.closed == 1, "the upstream stream must be closed, as Stop closes it"
    assert fake.yielded == 5, "nothing past the stop was read from the engine"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_stop_generation_raised_while_flushing_the_last_word_is_handled(scripted):
    """With a continuation possible, the final partial word is emitted AFTER
    the stream loop; a stop raised there must end the run the same way."""
    scripted([(["Alpha beta gamma ", "delta"], "stop")])
    calls = []

    async def on_delta(kind, text):
        calls.append(text)
        if text == "delta":
            raise continuation.StopGeneration(continuation.STOP_REPETITION)

    result = asyncio.run(
        continuation.stream_long_completion(
            [{"role": "user", "content": "go"}],
            on_delta=on_delta,
            segment_max_tokens=100,
            total_max_tokens=10_000,
        )
    )
    assert "delta" in calls
    assert result.stop_reason == continuation.STOP_REPETITION
    assert result.errors == []


# ---------------------------------------------------------------------------
# Repetition the person asked for (verifier, 2026-09-15)
# ---------------------------------------------------------------------------

REQUESTED = [
    (
        "Write 'I will not talk during the lecture.' 50 times, numbered.",
        "Here you go:\n\n" + "".join(f"{i}. I will not talk during the lecture.\n" for i in range(1, 51)),
    ),
    ("ॐ नमः शिवाय 108 बार लिखो", "ज़रूर, यह रहा जाप:\n\n" + "ॐ नमः शिवाय।\n" * 108),
    (
        "Write the Hare Krishna maha mantra 16 times",
        "Here it is:\n\n"
        + "Hare Krishna Hare Krishna, Krishna Krishna Hare Hare, Hare Rama Hare Rama, Rama Rama Hare Hare.\n" * 16,
    ),
    (
        "Type 'All work and no play makes Jack a dull boy.' thirty times in one paragraph",
        "Sure: " + "All work and no play makes Jack a dull boy. " * 30,
    ),
    ("જય શ્રી કૃષ્ણ 21 વાર લખો", "આ રહ્યો મંત્ર:\n\n" + "જય શ્રી કૃષ્ણ, જય શ્રી કૃષ્ણ.\n" * 21),
    ("ise 25 baar likho: Main roz padhai karunga.", "Main roz padhai karunga.\n" * 25),
    ("Chant it for me, please: Om Mani Padme Hum.", "Om Mani Padme Hum, Om Mani Padme Hum.\n" * 40),
]


@pytest.mark.parametrize("message,answer", REQUESTED, ids=[m[:24] for m, _ in REQUESTED])
@pytest.mark.parametrize("piece", [1, 4, 9])
def test_repetition_the_person_asked_for_is_never_cut(message, answer, piece):
    # Fails before the allowance: every one of these fired as a `cycle` loop
    # and the person got 1-5 copies and "it had begun repeating itself".
    assert answer_guard.scan(answer, piece) is not None, "without the request this IS a loop shape"
    guard = AnswerGuard(answer_guard.repetition_allowance(message))
    out: List[str] = []
    for start in range(0, len(answer), piece):
        out.extend(guard.feed(answer[start : start + piece]))
    out.extend(guard.finish())
    assert guard.verdict is None
    assert "".join(out) == answer == guard.shown


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Write 'I will not talk during the lecture.' 50 times", 50 + answer_guard.REPEAT_ALLOWANCE_SLACK),
        ("ॐ नमः शिवाय १०८ बार लिखो", 108 + answer_guard.REPEAT_ALLOWANCE_SLACK),
        ("જય શ્રી કૃષ્ણ 21 વાર લખો", 21 + answer_guard.REPEAT_ALLOWANCE_SLACK),
        ("say it thirty times", 30 + answer_guard.REPEAT_ALLOWANCE_SLACK),
        ("ise 10 baar likho", 10 + answer_guard.REPEAT_ALLOWANCE_SLACK),
        ("repeat after me: I am calm", answer_guard.REPEAT_ALLOWANCE_UNCOUNTED),
        ("I have hot and cold water in a 2:5 ratio and one bottle. How do I mix it?", 0),
        ("What is 3x4?", 0),
        ("run it 2x faster", 0),
        ("multiply 1000 x 1000", 0),
        ("Pehli baar kya hua?", 0),
        ("", 0),
    ],
)
def test_repetition_allowance_reads_the_request_and_nothing_else(message, expected):
    assert answer_guard.repetition_allowance(message) == expected


def test_a_requested_count_does_not_license_an_endless_loop():
    guard = AnswerGuard(answer_guard.repetition_allowance("Say 'It is impossible.' 5 times"))
    text = "Sure:\n\n" + "It is impossible.\n" * 80
    out: List[str] = []
    for start in range(0, len(text), 4):
        out.extend(guard.feed(text[start : start + 4]))
        if guard.verdict is not None:
            break
    assert guard.verdict is not None and guard.verdict.signal == answer_guard.SIGNAL_CYCLE
    assert text.startswith(guard.shown) and guard.shown.count("It is impossible.") <= 10


def test_an_allowance_leaves_the_restart_signal_armed():
    loop = owner_shaped_loop(40)
    guard = AnswerGuard(answer_guard.repetition_allowance("repeat the steps for me"))
    for start in range(0, len(loop), 5):
        guard.feed(loop[start : start + 5])
        if guard.verdict is not None:
            break
    assert guard.verdict is not None and guard.verdict.signal == answer_guard.SIGNAL_RESTART


# ---------------------------------------------------------------------------
# The chat engine hook
# ---------------------------------------------------------------------------


def run_engine(effort: str = "fast", mode: str = "assistant"):
    from app.engines.chat import run_chat_engine

    events: List[Tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    answer = asyncio.run(run_chat_engine("How do I mix 2:5?", [], emit, mode=mode, effort=effort))
    return answer, events


def tokens(events) -> List[str]:
    return [data["text"] for event, data in events if event == "token"]


@pytest.mark.parametrize("effort", ["fast", "think"])
def test_the_chat_engine_stops_a_loop_and_returns_exactly_what_it_streamed(scripted, effort):
    metrics.reset()
    loop = owner_shaped_loop(40)
    fake = scripted([(pieces(loop, 5), "length")])
    answer, events = run_engine(effort)
    shown = "".join(tokens(events))
    assert answer == shown
    assert loop.startswith(shown.rstrip("\n")) and len(shown) < len(loop) // 4
    assert fake.closed == fake.calls == 1, "one call, closed, no continuation after a loop"
    assert fake.yielded < len(pieces(loop, 5)) // 3, "the engine stopped reading the stream promptly"
    meta = [data for event, data in events if event == "meta"]
    assert len(meta) == 1
    assert meta[0]["route"] == "chat"
    assert meta[0]["continuation"]["stop_reason"] == "repetition"
    assert meta[0]["continuation"]["truncated"] is True
    assert meta[0]["loop_guard"]["signal"] in {"cycle", "restart"}
    assert meta[0]["loop_guard"]["trimmed_chars"] > 0
    assert f'answer_loop_guard_total{{effort="{effort}",route="chat",signal="{meta[0]["loop_guard"]["signal"]}"}} 1\n' in metrics.render()


def test_the_chat_engine_streams_an_ordinary_answer_in_the_models_own_pieces(scripted, monkeypatch):
    # One call's budget, so continuation passes deltas straight through (a
    # continuable run re-chunks them at word boundaries — its own contract,
    # tested in test_continuation.py). What is under test is that the guard
    # adds nothing and holds nothing.
    monkeypatch.setattr(continuation, "budget_for", lambda effort: 8000)
    text = legit_jug_puzzle() + "\n\n" + legit_identical_sub_bullets()
    deltas = pieces(text, 3)
    scripted([(deltas, "stop")], reasoning=["thinking about jugs"])
    answer, events = run_engine("fast")
    assert tokens(events) == deltas
    assert answer == text
    assert [e for e, _ in events].count("reasoning") == 1
    # The guard adds no meta of its own. ("How do I mix 2:5?" is a ratio
    # prompt, so at Fast the adaptive-thinking policy reports its grant.)
    meta = dict(events[-1][1])
    assert events[-1][0] == "meta" and "loop_guard" not in meta
    meta.pop("adaptive_thinking", None)
    assert meta == {"route": "chat"}


def test_the_chat_engine_passes_the_persons_repetition_request_to_the_guard(scripted, monkeypatch):
    # POST /chat hands run_chat_engine the person's message; a requested
    # repetition must stream and store in full, untouched.
    from app.engines.chat import run_chat_engine

    monkeypatch.setattr(continuation, "budget_for", lambda effort: 8000)
    text = "Here you go:\n\n" + "".join(f"{i}. I will not talk during the lecture.\n" for i in range(1, 41))
    deltas = pieces(text, 4)
    fake = scripted([(deltas, "stop")])
    events: List[Tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    answer = asyncio.run(
        run_chat_engine("Write 'I will not talk during the lecture.' 40 times", [], emit, effort="fast")
    )
    assert answer == text == "".join(tokens(events))
    assert fake.calls == 1
    assert events[-1] == ("meta", {"route": "chat"})


def test_a_user_stop_during_a_hold_closes_the_stream_and_keeps_only_what_was_shown(monkeypatch):
    # Verifier: Stop (task cancellation) arriving while the guard is
    # withholding a forming loop. The held copy was never shown, so it is not
    # in the tokens the worker keeps as the partial; the upstream stream is
    # closed by the same `finally` as ever.
    from app.engines.chat import run_chat_engine

    monkeypatch.setattr(continuation, "budget_for", lambda effort: 8000)
    block = "Fill the bottle with hot water. Pour it into the sink until it is empty. This does not get us closer. "
    text = "Let us think.\n\n" + block * 2 + block[:60]
    state = {"closed": 0, "held_seen": asyncio.Event()}

    async def stream(messages, **kwargs):
        try:
            for piece in pieces(text, 5):
                yield "token", piece
            state["held_seen"].set()
            await asyncio.sleep(3600)
        finally:
            state["closed"] += 1

    monkeypatch.setattr(llm, "stream_chat_events", stream)
    monkeypatch.setattr(llm, "get_finish_reason", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    events: List[Tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    async def scenario():
        task = asyncio.create_task(run_chat_engine("Mix 2:5 please", [], emit, effort="fast"))
        await asyncio.wait_for(state["held_seen"].wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    shown = "".join(tokens(events))
    assert state["closed"] == 1
    assert shown.count(block.strip()) >= 1 and text.startswith(shown)
    assert len(shown) < len(text), "withheld text is not streamed when Stop arrives"
    assert not shown.endswith(block[:60]), "the start of a third copy was never streamed"
    assert shown.count("Fill the bottle with hot water.") <= 2


def test_the_chat_engine_never_guards_the_reasoning_stream(scripted):
    looping_thought = ["I should fill bottle 2 with hot water and pour it into bottle 1. "] * 60
    scripted([(["The ratio needs a reference volume."], "stop")], reasoning=looping_thought)
    answer, events = run_engine("think")
    assert answer == "The ratio needs a reference volume."
    assert len([1 for e, _ in events if e == "reasoning"]) == 60
    assert "loop_guard" not in events[-1][1]


# ---------------------------------------------------------------------------
# POST /chat: what is stored is what was shown (durable intent + regenerate)
# ---------------------------------------------------------------------------


def _parse_sse(text: str):
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


@pytest.fixture()
def clean_registry(monkeypatch):
    _live_generations.clear()
    metrics.reset()
    monkeypatch.setattr(app_main, "_shutting_down", False)
    yield
    _live_generations.clear()


def _loop_stream(deltas):
    async def fake(messages, **kwargs):
        for piece in deltas:
            yield "token", piece

    return fake


def test_a_stopped_loop_is_stored_exactly_as_it_was_streamed(monkeypatch, clean_registry):
    loop = owner_shaped_loop(30)
    monkeypatch.setattr(llm, "stream_chat_events", _loop_stream(pieces(loop, 7)))
    branch = {"self": "b-loop-regen", "parent": "b-question"}
    body = {
        "message": "How do I get 2:5 with one bottle?",
        "mode": "assistant",
        "effort": "fast",
        "conversation_id": "loop-1",
        "intent_id": "int-loop-1",
        "answer_branch": branch,
    }
    with TestClient(app) as client:
        events = _parse_sse(client.post("/chat", json=body).text)
    assert events[-1][0] == "done"
    streamed = "".join(data["text"] for event, data in events if event == "token")
    assert streamed and len(streamed) < len(loop) // 3
    meta = [data for event, data in events if event == "meta"][-1]
    assert meta["continuation"]["stop_reason"] == "repetition"
    stored = app_main._persisted_answer("loop-1", meta["generation_id"])
    assert stored is not None
    assert stored["content"] == streamed, "the stored answer must be exactly what the person saw"
    assert stored["meta"]["continuation"]["stop_reason"] == "repetition"
    assert stored["meta"]["loop_guard"]["signal"] in {"cycle", "restart"}
    assert stored["meta"]["branch"] == branch
    assert db.get_chat_request("int-loop-1")["status"] == "completed"
    # Verifier: the trace detail reaches the real query_trace_events table
    # (status 'info' passes its CHECK) and carries no answer text; the counter
    # is on /metrics.
    with db.connection() as con:
        rows = con.execute(
            "SELECT status, details FROM query_trace_events WHERE trace_id = %s AND stage = %s",
            (meta["generation_id"], answer_guard.TRACE_STAGE),
        ).fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "info"
    details = rows[0]["details"] if isinstance(rows[0]["details"], dict) else json.loads(rows[0]["details"])
    assert details["signal"] == stored["meta"]["loop_guard"]["signal"]
    assert details["trimmed_characters"] == stored["meta"]["loop_guard"]["trimmed_chars"] > 0
    assert "Bottle" not in json.dumps(details)
    assert f'answer_loop_guard_total{{effort="fast",route="chat",signal="{details["signal"]}"}} 1\n' in metrics.render()


def test_an_ordinary_answer_through_chat_is_stored_and_streamed_unchanged(monkeypatch, clean_registry):
    text = legit_identical_sub_bullets()
    deltas = pieces(text, 4)
    monkeypatch.setattr(llm, "stream_chat_events", _loop_stream(deltas))
    body = {"message": "Compare the tools", "mode": "assistant", "effort": "fast", "conversation_id": "plain-1", "intent_id": "int-plain-1"}
    with TestClient(app) as client:
        events = _parse_sse(client.post("/chat", json=body).text)
    assert "".join(data["text"] for event, data in events if event == "token") == text
    meta = [data for event, data in events if event == "meta"][-1]
    assert "continuation" not in meta and "loop_guard" not in meta
    stored = app_main._persisted_answer("plain-1", meta["generation_id"])
    assert stored is not None and stored["content"] == text
