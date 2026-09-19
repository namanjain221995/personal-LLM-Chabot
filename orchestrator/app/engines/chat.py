"""Chat engine (V2-DESIGN §3a): plain streamed completions, no data engines.

Two uses:
- mode="assistant": the router and data engines are bypassed entirely; the
  selected model answers as a helpful local assistant (general knowledge OK,
  never claims to have consulted Salesforce data).
- mode="salesforce" + router class "chat": the router's "chat" class is its
  catch-all for everything that is not sql/rag/vision/report, so it carries
  BOTH pleasantries and ordinary general questions. A real pleasantry
  (fast_lane.classify_pleasantry) gets the brief friendly reply; anything
  else gets the same assistant the other mode gets, plus the fact that this
  org's Salesforce data is available. A mode narrows where data comes from,
  never what the assistant is willing to discuss.

Streams vLLM reasoning deltas as `reasoning` events and answer deltas as
`token` events; emits the single final meta {route: "chat"} (mode/model/
effort are merged in centrally by the /chat endpoint).
"""
from __future__ import annotations

from typing import Awaitable, Callable, List, Sequence

from . import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION, FORMAT_INSTRUCTION, recent_turns
from .. import continuation, llm
from ..config import settings
from ..core import answer_sampling, best_of, rewrite_shape

Emit = Callable[[str, dict], Awaitable[None]]

#: How the assistant conducts itself, in EVERY mode. The sweep of 2026-09-17
#: measured the cost of leaving this unsaid: 12 of 26 turns carried a refusal,
#: deflection or hedge marker, and the same weights answered the same asks well
#: two lines of prompt away — so the shape of the failure was ours, not the
#: model's. Each clause below is one measured failure.
ASSISTANT_CONDUCT = (
    "Be helpful, clear, and concise, and use general knowledge freely.\n"
    # "I don't provide general business strategy or startup advice" — a mode
    # is a data source, not a subject filter.
    "ANSWER THE QUESTION YOU WERE ASKED. A mode narrows where data comes "
    "from; it never narrows what you are willing to discuss. Never tell the "
    "person a subject is outside what you do, and never redirect an ordinary "
    "question back to your own speciality.\n"
    # "I cannot recommend a single specific laptop model" — then it named one
    # the moment the person pushed back, so the answer was always there.
    "When asked to choose, recommend, rank or give an opinion, COMMIT: name "
    "one answer in the first sentence, then the reason and the one condition "
    "that would change it. Never open by saying you cannot choose, and never "
    "answer a request for one recommendation with a list of options. If the "
    "person tells you to stop hedging, still give the reason — one name and a "
    "price with nothing behind it is not an answer either.\n"
    # 6 of 26 answers carried an "I am an AI / consult a professional" block
    # and 2 led with it, in front of a genuinely good answer.
    "Never open with a disclaimer or an \"as an AI\" line, and never announce "
    "what you are not. Answer first; if a caveat is genuinely needed it is "
    "one short sentence at the end.\n"
    # An authorised security-awareness phishing simulation was refused 2 of 2
    # runs, in the same session that wrote a termination script and a legal
    # notice without a murmur.
    # The conditions line lives in the phishing sentence below ONLY (hotfix
    # 1.2, P5): said here, for all professional work, it opened 10 of 27 Fast
    # rewrites of a pasted job posting with "**Authorization & Debrief:**";
    # scoped to the simulation it measured 0 of 18.
    "You are a workplace tool used by employees of the operating company. "
    "Security-awareness tests on their own staff, termination and "
    "disciplinary scripts, incident response on systems they own, and legal "
    "notices they are sending themselves are ordinary professional work: do "
    "the work rather than declining.\n"
    # The general clause alone moved this from 0 of 2 to 1 of 3 (measured);
    # naming the artefact moved it to 3 of 3 answered, with the same wording
    # refusing 3 of 3 asks to deceive someone who is NOT the person's own
    # staff. Both halves are measured, so both halves stay.
    "A security-awareness phishing simulation is one of those: a test the "
    "person's own security team will send to their own staff, and a standard, "
    "purchasable corporate product. Write the simulated email itself, in "
    "full, with the pretext they named; an announcement, a training notice, a "
    "list of warning signs or advice about phishing is NOT what was asked for "
    "and is a wrong answer. Put the authorisation and debrief conditions in "
    "one line above it and the reporting footer below it. Deceiving someone "
    "who is NOT their own staff is a different thing and is out of bounds.\n"
    "Decline only something genuinely out of bounds. When you do, say so in "
    "one sentence — no lecture — and offer the nearest thing you can do."
)

ASSISTANT_SYSTEM = (
    "You are the TechSara local AI assistant, running entirely on this "
    "machine. " + ASSISTANT_CONDUCT + "\n"
    "You are NOT connected to Salesforce data in this mode — never claim to "
    "have looked something up in Salesforce or invent CRM numbers.\n"
    # This clause used to read "if asked about the user's Salesforce data,
    # suggest switching Salesforce mode on", and the model embroidered it into
    # "...or check your recruitment dashboard" — naming a different product as
    # the place to go, for data this platform holds a synced copy of. Loose
    # wording measured 2 of 4 runs still naming another tool (a dashboard, an
    # ATS, Greenhouse/Lever/Workday); the wording below measured 5 of 5 naming
    # the toggle and 0 of 5 naming anything else.
    "If asked about the user's own Salesforce, CRM, ATS, recruitment or "
    "hiring data, the whole answer is that you can pull those numbers from "
    "their Salesforce data as soon as they turn Salesforce mode on in the "
    "composer, because this platform holds a synced copy of that org. Say it "
    "in your own words, addressing them as \"you\". Do not name any other "
    "place to look: not a dashboard, not a report, not an export, not another "
    "product, and no steps for finding it elsewhere."
)

#: Salesforce mode, and the message really is a pleasantry.
SALESFORCE_CHAT_SYSTEM = (
    "You are the TechSara Local AI Analysis Platform for Salesforce data. "
    "The user sent a greeting, small talk, thanks, or a question about you "
    "rather than a data question. Reply briefly and warmly (a couple of "
    "sentences) and offer to answer questions about their Salesforce data. "
    "Never invent Salesforce numbers.\n"
    # This prompt used to say the platform had no Salesforce access, and the
    # model repeated it verbatim — "I don't have direct access to your live
    # Salesforce org" — to a user whose org it had been querying all session.
    "IMPORTANT: you DO have Salesforce access. This platform holds a synced "
    "copy of the org and can also query Salesforce live over the API. Never "
    "tell the user you cannot see their Salesforce data, and never suggest "
    "they check it themselves or run a script."
    # The clause that used to close this prompt told the model to ask the
    # person to re-send, "as a direct question", the question the system was
    # already holding — i.e. to do the routing by hand. Deleted 2026-09-18.
)

#: Salesforce mode, and the message is NOT a pleasantry: the router's "chat"
#: class is a catch-all, so this is where every general question in that mode
#: lands. Same assistant, same conduct; the org's data is an extra, not a
#: fence.
SALESFORCE_ASSISTANT_SYSTEM = (
    "You are the TechSara Local AI Analysis Platform, running entirely on "
    "this machine. " + ASSISTANT_CONDUCT + "\n"
    "You ALSO have this organisation's Salesforce data and can look things "
    "up in it: the platform holds a synced copy of the org and can query "
    "Salesforce live over the API. Never tell the user you cannot see their "
    "Salesforce data, never suggest they check it themselves, run a script "
    "or open another dashboard, and never invent Salesforce numbers. Having "
    "that data does not make anything else off-topic: answer the question "
    "that was asked, in full, and mention the data only when it would "
    "genuinely help."
)


def _lane_messages(message: str, history: Sequence[dict]) -> List[dict]:
    """The Fast small-talk lane's prompt (app/fast_lane.py): the persona, who
    is being assisted, the saved facts when main.py found them in time, and
    the last two exchanges clipped — no diagram or code rules, no grounding.
    Only THIS prompt is short; the stored conversation is untouched."""
    from .. import fast_lane
    from ..facts import FACTS_HEADER
    from ..identity import identity_line

    system = ASSISTANT_SYSTEM + identity_line()
    # main.py pins the saved-facts block as a system message; it is the one
    # system block the lane keeps. Recall and document blocks are never
    # assembled for a lane turn, and any other system message is dropped.
    for m in history:
        content = m.get("content")
        if m.get("role") == "system" and isinstance(content, str) and content.startswith(FACTS_HEADER):
            system = system + "\n\n" + content
            break
    turns = [
        {"role": m.get("role"), "content": str(m.get("content") or "")[: fast_lane.FAST_LANE_TURN_CHARS]}
        for m in recent_turns(
            [m for m in history if m.get("role") != "system"],
            fast_lane.FAST_LANE_HISTORY_TURNS * 2,
        )
    ]
    return [{"role": "system", "content": system}, *turns, {"role": "user", "content": message}]


def is_small_talk(message: str, mode: str) -> bool:
    """Salesforce mode AND the message really is a greeting, thanks, a
    farewell or laughter — the only turn that gets the two-sentence persona
    and the small ceiling. Pure: regex over the message, no network, no model.

    Assistant mode never lands here: main.py sends its pleasantries down the
    Fast small-talk lane instead (`lane`)."""
    from .. import fast_lane

    return mode != "assistant" and bool(fast_lane.classify_pleasantry(message))


def _messages(
    message: str, history: Sequence[dict], mode: str, grounding: str = "", lane: str = ""
) -> List[dict]:
    if lane:
        return _lane_messages(message, history)
    # A real pleasantry gets the short warm reply and nothing else — a diagram
    # would never belong in small talk. Everything else, in either mode, gets
    # the full assistant with the same capabilities.
    from ..identity import identity_line

    # FORMAT before DIAGRAMS and CODE: how an answer is written comes first,
    # and the two capability blocks qualify it. Both assistant modes get it —
    # a Salesforce-mode answer is no less an answer (sweep, 2026-09-18).
    if mode == "assistant":
        system = ASSISTANT_SYSTEM + FORMAT_INSTRUCTION + DIAGRAM_INSTRUCTION + CODE_INSTRUCTION
    elif is_small_talk(message, mode):
        # Small talk keeps the short prompt it has always had.
        system = SALESFORCE_CHAT_SYSTEM
    else:
        # Salesforce mode, ordinary question: the router's "chat" class is its
        # dustbin, and the old prompt asserted small talk as FACT here. That
        # one false premise produced the worst answers in the sweep. Same
        # assistant, same capabilities, as the other mode.
        system = SALESFORCE_ASSISTANT_SYSTEM + FORMAT_INSTRUCTION + DIAGRAM_INSTRUCTION + CODE_INSTRUCTION
    system = system + identity_line()
    # --- AS3 intent-capability BEGIN --- (the file capability line; not in the lane prompt)
    from .capability import capability_suffix as _as3_capability_suffix

    system = system + _as3_capability_suffix()
    # --- AS3 intent-capability END ---
    # Evidence goes AFTER the persona and the identity line, so the last thing
    # the model reads before the conversation is what today's date is and what
    # the sources actually say. Empty for a timeless question, which keeps an
    # ordinary chat exactly as cheap as it was.
    if grounding:
        system = system + "\n\n" + grounding
    return (
        [{"role": "system", "content": system}]
        # THE CONVERSATION, not a three-exchange slice. This was 6 — the
        # reason a 60-message French lesson answered "how to translate" with
        # a Python tutorial: the last six turns were a goodnight exchange and
        # the lesson itself was outside the window. Bounding history is
        # compaction's job (a rolling summary, on an absolute token budget)
        # and fit_request's (the physical window); an engine cutting on top
        # of both only throws away what they chose to keep.
        + recent_turns(history, settings.chat_history_turns)
        + [{"role": "user", "content": message}]
    )


async def run_chat_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    *,
    mode: str = "salesforce",
    model_choice: str = "smart",
    effort: str = "medium",
    grounding: str = "",
    lane: str = "",
) -> str:
    """Stream a plain completion from the selected model; meta route=chat.

    `lane` is the Fast small-talk lane's category (app/fast_lane.py) when
    main.py sent the turn down it: a short prompt, one call of at most
    FAST_LANE_MAX_TOKENS, thinking off (Fast), no grounding.

    `grounding` is the living-knowledge block (app/web_memory.py): source-backed
    passages this platform already read from the public web, plus today's date.
    It arrives ALREADY BUILT so this engine stays a plain completion — the
    decision about whether evidence was needed, and the cost of finding it,
    belong to the caller.
    """
    effort = llm.normalize_effort(effort)
    if lane:
        return await _run_lane(message, history, emit, model_choice=model_choice, effort=effort, lane=lane)
    # --- effort_policy (answer quality) begin ---
    # The thinking model spends a large, variable share of its budget on
    # reasoning before emitting a single answer token — a small ceiling makes
    # longer asks (e.g. "draw a flowchart of X") come back EMPTY. max_tokens is
    # only a cap, so a generous value costs nothing on short replies.
    # 6,000 is the SMALL-TALK ceiling, not the Salesforce one. It used to be
    # keyed on the mode, which was the same false premise as the persona:
    # everything the router's catch-all "chat" class hands this engine in
    # Salesforce mode was treated as a greeting, so an ordinary question asked
    # with the toggle on answered under a ceiling meant for "hello".
    small_talk = is_small_talk(message, mode)
    max_tokens = 6000 if small_talk else 8000
    # High is the level for hard questions — long code, real derivations — so
    # it gets room to finish. A ceiling that cuts the answer mid-function is
    # worse than a slow answer.
    if effort in ("think", "max") and not small_talk:
        max_tokens = 16000
    # Thinking levels are used for code and analysis, where 0.6 invents API
    # names and drifts. Fast/Low stay conversational.
    temperature = 0.3 if effort in ("think", "max") else 0.6
    total_max_tokens = continuation.budget_for(effort)
    # FAST (assistant and Salesforce): the sampling and length slice of the
    # answer plan (core/answer_sampling.py). Sampling defaults to exactly the
    # temperature above; the caps are one 8,000-token call for prose and up
    # to 64,000 across segments for long-form and structured asks. None —
    # Think, Max and every other mode — keeps the values above unchanged.
    answer_plan = answer_sampling.fast_sampling_for(
        message, history, mode=mode, effort=effort, model_choice=model_choice
    )
    if answer_plan is not None:
        max_tokens = answer_plan.segment_max_tokens
        temperature = answer_plan.sampling.get("temperature", temperature)
        # An operator who set CONTINUATION_BUDGET_FAST lower still wins.
        total_max_tokens = min(answer_plan.total_max_tokens, total_max_tokens)
    # --- effort_policy (answer quality) end ---

    # extra_high = best-of-N: EXTRA_HIGH_SAMPLES candidates generated
    # CONCURRENTLY, a thinking-off guided-JSON judge picks the winner, and
    # the winner's thinking + answer stream to the UI (core/best_of.py).
    # Zero usable candidates falls through to the ordinary single stream —
    # best-of-N must never make extra_high worse than high.
    if (
        effort == "max"
        and model_choice == "smart"
        and settings.extra_high_samples > 1
    ):
        prompt = _messages(message, history, mode, grounding)
        candidates = await best_of.generate_candidates(
            prompt,
            n=settings.extra_high_samples,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if any(c.usable for c in candidates):
            winner, reason = await best_of.select_best(message, candidates)
            best_of.log_losers(candidates, winner)
            for start in range(0, len(winner.reasoning), 1000):
                await emit(
                    "reasoning", {"text": winner.reasoning[start : start + 1000]}
                )
            answer = rewrite_shape.shape(message, winner.answer)
            for start in range(0, len(answer), 200):
                await emit("token", {"text": answer[start : start + 200]})
            await emit(
                "meta",
                {
                    "route": "chat",
                    "best_of": settings.extra_high_samples,
                    "best_of_winner": winner.index,
                    "best_of_reason": reason,
                },
            )
            return answer

    # LONG ANSWERS ARE MANY CALLS. `max_tokens` above is the ceiling on ONE
    # call and stays exactly that; the total an answer may run to is decided
    # by the effort the person chose (continuation.budget_for) — except at
    # Fast, where the answer plan sets it: equal to the call for prose, so a
    # Fast reply is one call, and larger only for long-form and structured
    # asks.
    #
    # The seams are invisible: deltas arrive here through `_out` in order,
    # with re-emitted openings already stripped, so the UI streams one answer.
    #
    # LOOP GUARD (core/answer_guard.py, 2026-09-15): every ANSWER delta passes
    # through the guard, which shows ordinary text at once and holds back only
    # text that re-treads what was already written. When the answer has begun
    # looping it raises StopGeneration, which ends the upstream stream exactly
    # as Stop does; the repeated tail is never shown, and what this returns —
    # the text that is stored — is precisely the text that was streamed.
    from ..core import answer_guard

    # A repetition the person asked for ("write it 50 times") is not a loop.
    guard = answer_guard.AnswerGuard(answer_guard.repetition_allowance(message))
    # A rewrite into a PASTED SAMPLE's format gets the sample's Markdown
    # mapping from a rule, not from the model (hotfix 1.2, P3: Fast followed
    # it in at most 1 of 3 runs). None for every other turn. Ahead of the
    # guard, so the text that is stored is the text that was streamed.
    shaper = rewrite_shape.for_message(message)

    async def _out(kind: str, text: str) -> None:
        if kind == "reasoning":
            await emit("reasoning", {"text": text})
            return
        if shaper is not None:
            text = shaper.feed(text)
        for piece in guard.feed(text):
            await emit("token", {"text": piece})
        if guard.verdict is not None:
            raise continuation.StopGeneration(continuation.STOP_REPETITION)

    # THINKING IS THE PERSON'S CHOICE, NOT THIS ENGINE'S (2026-09-17). Fast
    # turns used to be classified here (core/effort_policy.py) and a prompt
    # that looked like a multi-step reasoning task was given a bounded
    # thinking grant. In production every grant it opened was a false
    # positive — a pasted job description reads as a "measurement" problem
    # because it contains "can", "fill" and a rate per hour — and each one
    # cost the person 11-47 s of reasoning on a turn they had asked to be
    # fast. Fast now never thinks: this engine asks for the completion the
    # effort implies and nothing else. The loop guard below, not a thought,
    # is what stops an answer that reasons itself in circles.
    long = await continuation.stream_long_completion(
        _messages(message, history, mode, grounding),
        on_delta=_out,
        model_choice=model_choice,
        effort=effort,
        temperature=temperature,
        segment_max_tokens=max_tokens,
        total_max_tokens=total_max_tokens,
        deadline_s=settings.continuation_deadline_s or None,
        **({} if answer_plan is None else {"answer_plan": answer_plan}),
    )
    if shaper is not None and guard.verdict is None:
        for piece in guard.feed(shaper.finish()):
            await emit("token", {"text": piece})
    for piece in guard.finish():
        await emit("token", {"text": piece})

    # §10/V2 §2: the SINGLE final meta — no citations/sql keys on this route.
    # `continuation` is added only when there was something to say: a
    # one-segment answer looks exactly as it did before. A stopped loop says
    # so through it (stop_reason "repetition": "This answer stops here: it had
    # begun repeating itself.").
    meta = {"route": "chat"}
    if long.segment_count > 1 or long.truncated:
        meta["continuation"] = long.as_meta()
    if guard.verdict is not None:
        meta["loop_guard"] = guard.verdict.as_meta()
        await answer_guard.record(guard.verdict, effort=effort, route="chat")
    await emit("meta", meta)
    return guard.shown


async def _run_lane(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    *,
    model_choice: str,
    effort: str,
    lane: str,
) -> str:
    """One short streamed completion for a small-talk lane turn."""
    from .. import fast_lane

    async def _out(kind: str, text: str) -> None:
        if kind == "reasoning":
            await emit("reasoning", {"text": text})
        else:
            await emit("token", {"text": text})

    # Segment cap == total cap: one call, no continuation seam to hold back,
    # so every delta streams straight through. Thinking follows the effort,
    # and the lane only admits Fast, where llm.wants_thinking is False.
    long = await continuation.stream_long_completion(
        _messages(message, history, "assistant", lane=lane),
        on_delta=_out,
        model_choice=model_choice,
        effort=effort,
        temperature=0.6,
        segment_max_tokens=fast_lane.FAST_LANE_MAX_TOKENS,
        total_max_tokens=fast_lane.FAST_LANE_MAX_TOKENS,
        deadline_s=settings.continuation_deadline_s or None,
    )
    meta = {"route": "chat"}
    if long.segment_count > 1 or long.truncated:
        meta["continuation"] = long.as_meta()
    await emit("meta", meta)
    return long.text
