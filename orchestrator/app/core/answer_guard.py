"""Loop guard over a streamed ANSWER (2026-09-15).

THE FAILURE. A Fast answer to a reasoning puzzle stopped answering and started
cycling: the same three steps ("Fill Bottle 2 completely with Hot Water. /
Pour from Bottle 2 into Bottle 1 until Bottle 1 is full. / This doesn't
help."), an apology, the same three steps again — until max_tokens. Nothing
between the model and the person looked at the answer text: continuation.py
guards the seams BETWEEN calls, never the inside of one.

WHAT THIS IS. A synchronous, incremental detector fed the answer deltas (never
the reasoning stream). It recognises degenerate repetition and says where the
clean text ends; the caller stops the upstream generation and keeps only the
text before that point. It is pure — no I/O, no clock, no model call; `record`
is the one async function, called once after a fire.

WHAT MUST NOT FIRE. Markdown tables, code with repeated lines, numbered lists
of similar steps (a jug puzzle legitimately says "Fill the 5-litre jug." more
than once), songs with the chorus written out every time, cumulative verse (a
prefix-style "House That Jack Built" re-walks its whole previous stanza every
stanza), translations, JSON arrays, bullet lists with identical sub-bullets, an
apology letter. Each is a test. So the signals are about SHAPE, not about "a
lot of this was said before":

  cycle       the same block of sentences recurring BACK TO BACK: three copies
              of one sentence, four copies of a longer block. Restart phrases
              and trivial lines between copies do not break the period — that
              is the apology-sandwiched loop above. A chorus is separated by
              verses, a cumulative stanza grows: neither is back to back.
  restart     restart/apology phrases recurring (>= 3 in the last 40
              sentences) while at least half of the substantive text since the
              first of them repeats earlier sentences.
  ngram       the loop with no sentence breaks: the last 300 words >= 90%
              covered by 10-word spans seen before, inside at most three
              sentences. Text with structure is left to `cycle` and `restart`,
              which can tell a refrain from a loop; word spans cannot.
  code_cycle  inside code and tables only, an exact line block recurring back
              to back (10 copies; 40 for a single line — a zero-filled lookup
              table is legitimate).

WHAT THE PERSON SEES. The server stores what the engine returns and the browser
renders the token events, so text that was streamed cannot be taken back. The
guard therefore WITHHOLDS text once a loop is forming — from the third copy of
a back-to-back block, while restart phrases keep arriving over repeated text,
and inside a long run-on sentence that only re-treads earlier word spans — and
releases it, in the original delta pieces, the moment genuinely new text
follows. Nothing else is held, so an ordinary answer streams exactly as before.
When the guard fires, the withheld tail is dropped: at most two copies of a
looping block are ever shown.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

SIGNAL_CYCLE = "cycle"
SIGNAL_RESTART = "restart"
SIGNAL_NGRAM = "ngram"
SIGNAL_CODE_CYCLE = "code_cycle"
SIGNALS = frozenset({SIGNAL_CYCLE, SIGNAL_RESTART, SIGNAL_NGRAM, SIGNAL_CODE_CYCLE})

METRIC = "answer_loop_guard_total"
METRIC_HELP = (
    "Answers stopped because the streamed answer text had begun repeating "
    "itself (loop guard), by effort, route and detection signal."
)
TRACE_STAGE = "ANSWER_LOOP_STOPPED"

# Unit classes.
_NOVEL = 0
_REPEAT = 1
_MARKER = 2
_TRIVIAL = 3
_STRUCT = 4

#: A normalised sentence shorter than this carries no identity ("Yes.",
#: "**Pros:**", "Wait."): it neither repeats nor counts as new text.
TRIVIAL_CHARS = 8
#: Sentences at least this long are "substantive" for the restart share.
SUBSTANTIVE_CHARS = 24
#: Longest back-to-back block, in sentences, the cycle signal looks for.
MAX_PERIOD = 24
#: Copies needed: one sentence three times; a block of sentences four times.
#: Three copies of a block is not enough — a cumulative rhyme whose first
#: stanza the model duplicated (live sample, 2026-09-15) went on to be a good
#: poem. Two copies never are.
CYCLE_COPIES_SINGLE = 3
CYCLE_COPIES_BLOCK = 4
#: ...and the later copies must repeat at least this many normalised chars.
CYCLE_MIN_CHARS = 150
_MAX_RUN_UNITS = 400
RESTART_MIN_MARKERS = 3
RESTART_WINDOW_UNITS = 40
RESTART_MIN_REPEAT_SHARE = 0.5
#: A restart cut keeps text up to where the tail is at least this degenerate.
_RESTART_CUT_SHARE = 0.6
NGRAM_N = 10
NGRAM_WINDOW = 300
NGRAM_MIN_COVERAGE = 0.9
NGRAM_MAX_SENTENCES = 3
#: How far back (in words) an earlier span still counts as "seen before".
_NGRAM_HORIZON = 4000
CODE_MAX_PERIOD = 12
CODE_COPIES_SINGLE = 40
CODE_COPIES_BLOCK = 10
CODE_MIN_CHARS = 800
CODE_MIN_BLOCK_ALNUM = 30
#: Structured lines are withheld from this many back-to-back copies on.
CODE_HOLD_COPIES_SINGLE = 20
CODE_HOLD_COPIES_BLOCK = 3
#: Withheld text is released once new text at least this long follows it.
RELEASE_MIN_NOVEL = 40
#: Nothing is withheld past this many characters: a long legitimate re-walk
#: is released rather than stalled. Two further copies of a 3-sentence block
#: (what the cycle signal waits for after the hold starts) fit well inside.
HOLD_CAP_CHARS = 2400
#: A run-on sentence is withheld only once it is this long and its last
#: `ECHO_WORDS` words all re-tread earlier spans.
ECHO_MIN_OPEN_CHARS = 200
ECHO_WORDS = 16
#: Characters of one word that identify it; a base64 blob is one long word.
_MAX_WORD_CHARS = 64

#: List markers and step labels in front of a sentence. "1.", "-", "**Step 3:**"
#: and "Attempt 2:" all name the SAME step when the model loops.
_LEADING_MARKERS = re.compile(
    r"^\s*(?:(?:[-*+•>#]+|\(?\d{1,3}[.):]|\(?[a-z][.)](?=\s)"
    r"|(?:step|phase|stage|attempt|option|method|approach|try)\s*\d+\s*[:.)\-]?)\s*)+",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s")
#: Restart and apology phrases, matched on NORMALISED text.
_RESTART_PHRASE = re.compile(
    r"\b(?:i (?:sincerely |truly )?apologi[sz]e|my apologies|sorry (?:for|about|that|i)\b|"
    r"let me (?:try|start|re ?think|re ?evaluate|reconsider|re ?do|correct|recalculate|re ?check)|"
    r"let s (?:try|start over|re ?think|re ?evaluate|reconsider|re ?do|recalculate|re ?check)|"
    r"that (?:s|is) (?:not|in)correct|that (?:s|is) not right|"
    r"(?:this|that) (?:doesn t|does not|won t|will not|didn t|did not) (?:help|work)|"
    r"i made (?:a|an) (?:mistake|error))|^(?:wait|oops)\b"
)
_STRUCT_FIRST_CHARS = frozenset("|{}[]<`~")
_JSON_KEY_LINE = re.compile(r'^\s*"[^"\n]{1,80}"\s*:')
_FENCE = ("```", "~~~")
_SENTENCE_END = frozenset(".!?")
_CJK_SENTENCE_END = frozenset("。！？")
#: Characters that may follow a sentence terminator before the space: `."`, `.)`, `.**`
_CLOSERS = frozenset("\"')]*_”’")


#: REQUESTED REPETITION (verifier, 2026-09-15). "Write this sentence 50 times",
#: "ॐ नमः शिवाय 108 बार लिखो", a chant or a punishment line: the answer is a
#: back-to-back loop BY REQUEST, and its shape cannot be told from a
#: degenerate one. Only the person's message can. A stated count allows that
#: many copies (plus slack); a request to repeat without a count allows
#: `REPEAT_ALLOWANCE_UNCOUNTED`. Nothing else changes: restart cycles still
#: fire, and a copy count far past the request is still a loop.
REPEAT_ALLOWANCE_SLACK = 3
REPEAT_ALLOWANCE_UNCOUNTED = 60
REPEAT_ALLOWANCE_MAX = 5000
_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100, "thousand": 1000,
    "do": 2, "teen": 3, "char": 4, "paanch": 5, "panch": 5, "das": 10, "bees": 20,
    "sau": 100, "hazaar": 1000, "hazar": 1000,
}
_TIMES_WORDS = r"(?:times|baar|bar|bār|dafa|dafaa|बार|दफा|दफ़ा|વાર|વખત)"
_COUNTED_REPEAT = re.compile(
    r"(?<![\w.])(\d{1,5}|" + "|".join(_NUMBER_WORDS) + r")\s*[- ]?\s*" + _TIMES_WORDS + r"(?!\w)",
    re.IGNORECASE | re.UNICODE,
)
_UNCOUNTED_REPEAT = re.compile(
    r"\b(?:repeat|repeatedly|over and over|again and again|chant|jaap|jap|japa|baar baar|bar bar)\b"
    r"|दोहरा|बार[- ]बार|जाप|ફરી ફરી|વારંવાર|જાપ",
    re.IGNORECASE | re.UNICODE,
)


def repetition_allowance(message: str) -> int:
    """How many back-to-back copies the person's own message asks for (0: none)."""
    if not message:
        return 0
    text = message[:4000]
    best = 0
    for match in _COUNTED_REPEAT.finditer(text):
        raw = match.group(1).lower()
        try:
            count = int(raw) if raw[:1].isdigit() else _NUMBER_WORDS.get(raw, 0)
        except ValueError:  # a digit class int() does not read
            count = 0
        if count >= 2:
            best = max(best, min(count, REPEAT_ALLOWANCE_MAX) + REPEAT_ALLOWANCE_SLACK)
    if best == 0 and _UNCOUNTED_REPEAT.search(text):
        best = REPEAT_ALLOWANCE_UNCOUNTED
    return best


def normalize(text: str) -> str:
    """The identity of a sentence: markers stripped, case and punctuation folded."""
    stripped = _LEADING_MARKERS.sub("", text, count=1)
    return _NON_WORD.sub(" ", stripped.lower()).strip()


@dataclass
class Verdict:
    """Why the guard fired and where the kept answer ends."""

    signal: str
    #: Character offset in the generated answer: nothing at or after it is shown.
    cut: int
    #: Characters of generated answer text that were never shown.
    trimmed_chars: int = 0
    units: int = 0
    #: Length and sha256 of the repeated block — never the text itself.
    repeated: Dict[str, object] = field(default_factory=dict)

    def as_meta(self) -> Dict[str, object]:
        return {"signal": self.signal, "trimmed_chars": self.trimmed_chars}


class _Unit:
    """One sentence (prose) or one line (structured) of the answer."""

    __slots__ = ("start", "end", "cls", "fp", "size")

    def __init__(self, start: int, end: int, cls: int, fp: str, size: int) -> None:
        self.start = start
        self.end = end
        self.cls = cls
        self.fp = fp
        self.size = size


class AnswerGuard:
    """Feed answer deltas in order; show the pieces `feed` returns.

    `feed(delta)` returns the pieces that may be shown now, in order: `[delta]`
    for ordinary text, `[]` while text is withheld, and every withheld delta —
    each still its own piece — when a hold ends. Once `verdict` is set the
    caller must stop the generation; later feeds return `[]`. `finish()`
    releases whatever is still withheld when the stream ends on its own.
    `shown` is exactly the concatenation of every piece returned.
    """

    def __init__(self, repetition_allowance: int = 0) -> None:
        self.verdict: Optional[Verdict] = None
        #: Back-to-back copies the person asked for (see `repetition_allowance`).
        #: When set, the cycle and code signals wait for more copies than
        #: that, and the word-span signals (which cannot count copies) are off.
        self._allowance = max(0, int(repetition_allowance or 0))
        self._chunks: List[str] = []
        self._pos = 0
        self._emitted = 0
        self._held: List[str] = []
        self._shown: List[str] = []
        # The open sentence and line.
        #: The open unit as parts, joined once when it ends: `+=` on an
        #: attribute copies the whole string, which is quadratic on a long
        #: base64 line.
        self._open: List[str] = []
        self._open_len = 0
        self._open_start = 0
        self._line_kind = 0  # 0 undecided, 1 prose, 2 structured
        #: The first non-space characters of the line — enough to see a fence.
        self._line_head = ""
        self._in_fence = False
        self._terminal = False
        # Completed units.
        self._units: List[_Unit] = []
        self._counts: Dict[str, int] = {}
        #: Unit indices of the cycle sequence: NOVEL/REPEAT sentences and
        #: structured lines. Restart phrases and trivial units are left out,
        #: so they do not break a period.
        self._seq: List[int] = []
        self._struct_seq: List[int] = []
        # Withholding.
        self._hold = False
        self._novel_since_hold = 0
        # Words, for the n-gram signal.
        self._word = ""
        self._word_start = 0
        self._word_starts: List[int] = []
        self._recent: Deque[str] = deque(maxlen=NGRAM_N)
        self._shingles: Dict[int, int] = {}
        self._covered = bytearray()
        self._marked = -1

    # ------------------------------------------------------------------ API

    @property
    def shown(self) -> str:
        return "".join(self._shown)

    @property
    def text(self) -> str:
        """Everything fed so far, shown or not."""
        return "".join(self._chunks)

    def feed(self, delta: str) -> List[str]:
        if self.verdict is not None or not delta:
            return []
        self._chunks.append(delta)
        self._held.append(delta)
        for ch in delta:
            self._step(ch)
            if self.verdict is not None:
                return self._release_to_cut()
        if self._hold or (not self._allowance and self._echoing()):
            if self._pos - self._emitted <= HOLD_CAP_CHARS:
                return []
            self._end_hold()
        return self._release_all()

    def finish(self) -> List[str]:
        """The stream ended on its own: nothing more is coming, show the rest."""
        if self.verdict is not None:
            return []
        return self._release_all()

    # ------------------------------------------------------------- emission

    def _release_all(self) -> List[str]:
        pieces = self._held
        self._held = []
        self._emitted = self._pos
        self._shown.extend(pieces)
        return pieces

    def _release_to_cut(self) -> List[str]:
        """After a fire: show the withheld text before the cut, drop the rest."""
        verdict = self.verdict
        assert verdict is not None
        cut = verdict.cut
        if cut < self._emitted:
            # Part of the text past the ideal cut was already on screen: it
            # cannot be taken back. A hold starts when a sentence COMPLETES,
            # so the shown text may stop inside that sentence — finish it
            # rather than leave the answer ending mid-word.
            cut = self._emitted
            text = self.text
            containing = next((u for u in reversed(self._units) if u.start < cut < u.end), None)
            if containing is not None:
                cut = containing.end
            elif cut >= self._open_start:
                # Inside the open (run-on) sentence: at least finish the word.
                boundary = _WHITESPACE.search(text, cut)
                if boundary is not None:
                    cut = boundary.start()
        text = self.text
        extra = text[self._emitted : cut].rstrip(" \t") if cut > self._emitted else ""
        kept = self.shown + extra
        if _fence_open(kept):
            # Never leave the kept markdown inside an unterminated code block.
            extra += ("" if kept.endswith("\n") else "\n") + "```\n"
        verdict.cut = cut
        verdict.trimmed_chars = len(text) - cut
        self._held = []
        self._emitted = self._pos
        if not extra:
            return []
        self._shown.append(extra)
        return [extra]

    def _echoing(self) -> bool:
        """Is a long run-on sentence only re-treading earlier word spans?"""
        covered = self._covered
        if self._line_kind == 2 or self._open_len < ECHO_MIN_OPEN_CHARS or len(covered) < ECHO_WORDS:
            return False
        return covered.count(1, len(covered) - ECHO_WORDS) == ECHO_WORDS

    def _end_hold(self) -> None:
        self._hold = False
        self._novel_since_hold = 0

    def _note_novel(self, size: int) -> None:
        if self._hold:
            self._novel_since_hold += size
            if self._novel_since_hold >= RELEASE_MIN_NOVEL:
                self._end_hold()

    # ------------------------------------------------------- character level

    def _step(self, ch: str) -> None:
        pos = self._pos
        self._pos = pos + 1
        if self._line_kind == 0 and not ch.isspace():
            self._line_kind = 2 if (self._in_fence or ch in _STRUCT_FIRST_CHARS) else 1
        self._open.append(ch)
        self._open_len += 1
        if self._line_kind != 2:
            if ch.isalnum() or (ord(ch) > 0x2FF and unicodedata.category(ch)[0] == "M"):
                if not self._word:
                    self._word_start = pos
                if len(self._word) < _MAX_WORD_CHARS:
                    self._word += ch.lower()
            elif self._word:
                self._flush_word()
        if ch == "\n":
            self._flush_word()
            fence = self._line_head.startswith(_FENCE)
            self._end_unit(self._pos, line_end=True)
            if fence:
                self._in_fence = not self._in_fence
            self._line_kind = 0
            self._line_head = ""
            return
        if len(self._line_head) < 3 and not (ch.isspace() and not self._line_head):
            self._line_head += ch
        if self._line_kind == 2:
            return
        if ch in _CJK_SENTENCE_END:
            self._end_unit(self._pos)
        elif ch in _SENTENCE_END:
            self._terminal = True
        elif ch.isspace():
            if self._terminal:
                self._end_unit(self._pos)
            self._terminal = False
        elif ch not in _CLOSERS:
            self._terminal = False

    def _flush_word(self) -> None:
        word = self._word
        if not word:
            return
        self._word = ""
        k = len(self._word_starts)
        self._word_starts.append(self._word_start)
        self._covered.append(0)
        self._recent.append(word)
        if len(self._recent) < NGRAM_N:
            return
        key = hash(tuple(self._recent))
        prev = self._shingles.get(key)
        self._shingles[key] = k
        if prev is None or k - prev > _NGRAM_HORIZON:
            return
        for i in range(max(k - NGRAM_N + 1, self._marked + 1), k + 1):
            self._covered[i] = 1
        self._marked = k
        if not self._allowance and k + 1 >= NGRAM_WINDOW and (k & 7) == 0:
            self._check_ngram(k)

    # ------------------------------------------------------------ unit level

    def _end_unit(self, end: int, line_end: bool = False) -> None:
        raw = "".join(self._open)
        start = self._open_start
        self._open = []
        self._open_len = 0
        self._open_start = end
        self._terminal = False
        if not raw.strip():
            return
        if self._line_kind == 2 or (line_end and _JSON_KEY_LINE.match(raw)):
            self._struct_unit(start, end, raw.strip())
            return
        fp = normalize(raw)
        size = len(fp)
        seen = self._counts.get(fp, 0)
        if size < TRIVIAL_CHARS:
            cls = _TRIVIAL
        elif _RESTART_PHRASE.search(fp):
            cls = _MARKER
        elif seen:
            cls = _REPEAT
        else:
            cls = _NOVEL
        index = len(self._units)
        self._units.append(_Unit(start, end, cls, fp, size))
        if cls == _MARKER:
            self._counts[fp] = seen + 1
            self._check_restart()
            return
        # Trivial units stay in the sequence: "### Asana" and "### Trello"
        # break the period of the identical sub-bullets under them.
        self._seq.append(index)
        if cls == _TRIVIAL:
            return
        self._counts[fp] = seen + 1
        if cls == _NOVEL:
            self._note_novel(size)
            return
        if self._check_cycle() >= (max(2, self._allowance) if self._allowance else 2):
            # A block is being written for at least the second time back to
            # back: whatever follows may be the third copy, so withhold it.
            self._hold = True
            self._novel_since_hold = 0

    def _struct_unit(self, start: int, end: int, line: str) -> None:
        index = len(self._units)
        fp = "\x00" + line
        self._units.append(_Unit(start, end, _STRUCT, fp, len(line)))
        seen = self._counts.get(fp, 0)
        self._counts[fp] = seen + 1
        if not seen:
            self._note_novel(len(line))
        if line.startswith(_FENCE):
            return
        # In the cycle sequence too: a code line or a table between two
        # identical explanations breaks their period unless it repeats too.
        self._seq.append(index)
        self._struct_seq.append(index)
        self._check_code_cycle()

    # --------------------------------------------------------------- signals

    def _check_cycle(self) -> int:
        """Fire on a back-to-back block; return the copies seen (>= 1)."""
        seq = self._seq
        units = self._units
        i = len(seq) - 1
        last = units[seq[i]].fp
        best = 1
        tried = 0
        for j in range(i - 1, max(-1, i - 1 - MAX_PERIOD), -1):
            if units[seq[j]].fp != last:
                continue
            tried += 1
            p = i - j
            run = chars = 0
            while (
                run < _MAX_RUN_UNITS
                and i - run - p >= 0
                and units[seq[i - run]].fp == units[seq[i - run - p]].fp
            ):
                chars += units[seq[i - run]].size
                run += 1
            copies = 1 + run // p
            best = max(best, copies)
            need = CYCLE_COPIES_SINGLE if p == 1 else CYCLE_COPIES_BLOCK
            if self._allowance:
                need = max(need, self._allowance + 1)
            if copies >= need and chars >= CYCLE_MIN_CHARS:
                block = " ".join(units[seq[i - k]].fp for k in range(p - 1, -1, -1))
                # Keep the first copy: cut right after its last sentence.
                self._fire(SIGNAL_CYCLE, self._snap_back(seq[i - run] + 1), block)
                return copies
            if tried >= 3:
                break
        return best

    def _check_restart(self) -> None:
        units = self._units
        lo = max(0, len(units) - RESTART_WINDOW_UNITS)
        markers = [k for k in range(lo, len(units)) if units[k].cls == _MARKER]
        if len(markers) < 2:
            return
        first = markers[0]
        repeated = substantive = top = 0
        for k in range(first, len(units)):
            unit = units[k]
            if unit.cls in (_NOVEL, _REPEAT) and unit.size >= SUBSTANTIVE_CHARS:
                substantive += unit.size
                if unit.cls == _REPEAT:
                    repeated += unit.size
                    top = max(top, self._counts.get(unit.fp, 0))
        if not substantive or repeated < RESTART_MIN_REPEAT_SHARE * substantive:
            return
        if len(markers) < RESTART_MIN_MARKERS or top < 3:
            # The shape is forming (restarts over repeated text): withhold
            # what comes next until something new is written.
            self._hold = True
            self._novel_since_hold = 0
            return
        # Cut at the earliest repeat or restart from which the tail is mostly
        # degenerate.
        cut_index = len(units)
        degenerate = total = 0
        for k in range(len(units) - 1, first - 1, -1):
            unit = units[k]
            if unit.cls == _TRIVIAL:
                continue
            total += unit.size
            if unit.cls in (_REPEAT, _MARKER):
                degenerate += unit.size
                if degenerate >= _RESTART_CUT_SHARE * total:
                    cut_index = k
        self._fire(SIGNAL_RESTART, self._snap_back(cut_index), "")

    def _check_ngram(self, k: int) -> None:
        lo = k + 1 - NGRAM_WINDOW
        if self._covered.count(1, lo, k + 1) < NGRAM_MIN_COVERAGE * NGRAM_WINDOW:
            return
        window_start = self._word_starts[lo]
        sentences = 0
        for unit in reversed(self._units):
            if unit.end <= window_start:
                break
            sentences += 1
            if sentences > NGRAM_MAX_SENTENCES:
                return
        # The covered run ending here starts where the repetition began.
        r = k
        while r > 0 and self._covered[r - 1]:
            r -= 1
        repeat_start = self._word_starts[r]
        cut = repeat_start
        # Drop the whole sentence the repetition started in when that is
        # close — a sentence that turns into a loop half-way is not a clean
        # ending — but never a long run-on line, often the entire answer.
        units = self._units
        index = len(units)  # len(units) stands for the open sentence
        while index > 0 and units[index - 1].end > repeat_start:
            index -= 1
        start = units[index].start if index < len(units) else self._open_start
        if repeat_start - start <= 200:
            index = self._snap_back(index)
            cut = units[index].start if index < len(units) else self._open_start
        self._fire(SIGNAL_NGRAM, None, self.text[repeat_start:][:2000], cut=cut)

    def _check_code_cycle(self) -> None:
        seq = self._struct_seq
        units = self._units
        i = len(seq) - 1
        last = units[seq[i]].fp
        for p in range(1, min(CODE_MAX_PERIOD, i) + 1):
            if units[seq[i - p]].fp != last:
                continue
            block = [units[seq[i - k]].fp[1:] for k in range(p - 1, -1, -1)]
            if p > 1 and len(set(block)) == 1:
                continue  # one line repeated: that is period 1's question
            if sum(ch.isalnum() for line in block for ch in line) < CODE_MIN_BLOCK_ALNUM:
                continue
            need = CODE_COPIES_SINGLE if p == 1 else CODE_COPIES_BLOCK
            hold_at = CODE_HOLD_COPIES_SINGLE if p == 1 else CODE_HOLD_COPIES_BLOCK
            if self._allowance:
                need = max(need, self._allowance + 1)
                hold_at = max(hold_at, self._allowance)
            limit = (need - 1) * p
            run = 0
            while run < limit and i - run - p >= 0 and units[seq[i - run]].fp == units[seq[i - run - p]].fp:
                run += 1
            copies = 1 + run // p
            if copies >= need and sum(units[seq[i - k]].size for k in range(run)) >= CODE_MIN_CHARS:
                self._fire(SIGNAL_CODE_CYCLE, seq[i - run] + 1, "\n".join(block))
                return
            if copies >= hold_at:
                # Half-way to a code loop: withhold further lines until a
                # different one arrives. Legitimate code almost never writes
                # the same multi-line block three times back to back.
                self._hold = True
                self._novel_since_hold = 0

    def _snap_back(self, index: int) -> int:
        """Move a cut back over the restart phrases and trivial lines before it."""
        units = self._units
        while index > 0 and units[index - 1].cls in (_MARKER, _TRIVIAL):
            index -= 1
        return index

    def _fire(self, signal: str, cut_index: Optional[int], repeated: str, cut: Optional[int] = None) -> None:
        if cut is None:
            assert cut_index is not None
            cut = self._units[cut_index].start if cut_index < len(self._units) else self._open_start
        self.verdict = Verdict(
            signal=signal,
            cut=cut,
            units=len(self._units),
            repeated={
                "characters": len(repeated),
                "sha256": hashlib.sha256(repeated.encode("utf-8", errors="replace")).hexdigest(),
            },
        )


def _fence_open(text: str) -> bool:
    return sum(1 for line in text.split("\n") if line.strip().startswith(_FENCE)) % 2 == 1


def scan(text: str, piece: int = 4) -> Optional[Verdict]:
    """Run the guard over a finished text in `piece`-sized deltas (offline use)."""
    guard = AnswerGuard()
    for start in range(0, len(text), piece):
        guard.feed(text[start : start + piece])
        if guard.verdict is not None:
            return guard.verdict
    return None


async def record(verdict: Verdict, *, effort: str, route: str) -> None:
    """The metric and the trace detail for one fire. Never raises."""
    from .. import metrics
    from . import tracing

    signal = verdict.signal if verdict.signal in SIGNALS else "other"
    # Closed label values: a caller's typo folds to "other" instead of
    # minting a series (the metrics module's own rule).
    effort_label = effort if effort in metrics.CHAT_EFFORTS else "other"
    route_label = route if route in metrics.CHAT_ROUTES else "other"
    metrics.inc(METRIC, METRIC_HELP, effort=effort_label, route=route_label, signal=signal)
    try:
        await tracing.event(
            TRACE_STAGE,
            status="info",
            component="orchestrator.app.core.answer_guard",
            details={
                "signal": signal,
                "effort": effort_label,
                "route": route_label,
                "kept_characters": verdict.cut,
                "trimmed_characters": verdict.trimmed_chars,
                "units": verdict.units,
                "repeated_block": dict(verdict.repeated),
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never break an answer
        pass
