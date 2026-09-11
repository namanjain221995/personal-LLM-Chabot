"""The shapes every stage speaks. Plain dataclasses with JSON round-trips.

Everything here is serialised to a file at the end of the stage that made it
and read back by the stages that need it, so a crash between stages costs the
stage that was running and nothing before it. The JSON is the contract; the
dataclasses are a convenience over it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class Segment:
    """One stretch of speech, as Whisper timed it, in VIDEO time."""

    start_s: float
    end_s: float
    text: str
    language: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "start": round(self.start_s, 3),
            "end": round(self.end_s, 3),
            "text": self.text,
            "language": self.language,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Segment":
        return cls(
            start_s=float(d.get("start", 0.0)),
            end_s=float(d.get("end", 0.0)),
            text=str(d.get("text") or ""),
            language=(str(d["language"]) if d.get("language") else None),
        )


@dataclass(frozen=True)
class OcrSpan:
    """What was on screen, for how long, and what a reader made of it.

    `text` is the OCR transcript (may be empty on a picture with no words);
    `caption` is the vision model's one-paragraph description (may be None
    when captioning is off); `kind` is the caption's frame type (slide, code,
    terminal, webpage, document, person, diagram, other) or '' when unknown.
    """

    start_s: float
    end_s: float
    text: str
    kind: str = ""
    caption: Optional[str] = None
    frame: str = ""
    phash: int = 0

    def to_json(self) -> dict:
        return {
            "start": round(self.start_s, 3),
            "end": round(self.end_s, 3),
            "text": self.text,
            "kind": self.kind,
            "caption": self.caption,
            "frame": self.frame,
            "phash": int(self.phash),
        }

    @classmethod
    def from_json(cls, d: dict) -> "OcrSpan":
        return cls(
            start_s=float(d.get("start", 0.0)),
            end_s=float(d.get("end", 0.0)),
            text=str(d.get("text") or ""),
            kind=str(d.get("kind") or ""),
            caption=(str(d["caption"]) if d.get("caption") else None),
            frame=str(d.get("frame") or ""),
            phash=int(d.get("phash") or 0),
        )


@dataclass(frozen=True)
class Chapter:
    start_s: float
    end_s: float
    title: str
    summary: str = ""

    def to_json(self) -> dict:
        return {
            "start": round(self.start_s, 3),
            "end": round(self.end_s, 3),
            "title": self.title,
            "summary": self.summary,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Chapter":
        return cls(
            start_s=float(d.get("start", 0.0)),
            end_s=float(d.get("end", 0.0)),
            title=str(d.get("title") or "").strip(),
            summary=str(d.get("summary") or "").strip(),
        )


CONTENT_TYPES = ("meeting", "lecture", "demo", "screen_recording", "interview", "other")


@dataclass(frozen=True)
class Limitation:
    """One thing the analysis could NOT see, and the sentence that says so.

    A stage that fails is recorded on the row, but the row is not what a
    person reads: they read the summary and the answer. An unread screen and
    a blank screen produce the same silence in both unless the limitation
    travels with the understanding — so it does, from the stage that hit it
    through fusion into `summary.md`.
    """

    stage: str
    sentence: str

    def to_json(self) -> dict:
        return {"stage": self.stage, "sentence": self.sentence}

    @classmethod
    def from_json(cls, d: dict) -> "Limitation":
        return cls(stage=str(d.get("stage") or ""), sentence=str(d.get("sentence") or "").strip())


@dataclass
class Understanding:
    """What the main model concluded from the whole evidence pack."""

    content_type: str = "other"
    summary: str = ""
    chapters: List[Chapter] = field(default_factory=list)
    key_points: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    action_items: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    #: What a reader might expect that the evidence does not contain — the
    #: honest complement of `summary`, and the thing that keeps Q&A from
    #: inventing a decision that was never made.
    not_covered: str = ""
    #: How it was produced: 'direct' (one pass) or 'map_reduce:N'.
    method: str = ""
    #: Which stages could not contribute. Empty on a complete analysis; the
    #: sentences are also folded into `not_covered`, because that is the
    #: field every reader of an Understanding already looks at.
    limitations: List[Limitation] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["chapters"] = [c.to_json() for c in self.chapters]
        d["limitations"] = [lim.to_json() for lim in self.limitations]
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Understanding":
        d = dict(d or {})
        chapters = [Chapter.from_json(c) for c in (d.get("chapters") or []) if isinstance(c, dict)]
        return cls(
            content_type=str(d.get("content_type") or "other"),
            summary=str(d.get("summary") or ""),
            chapters=chapters,
            key_points=_str_list(d.get("key_points")),
            decisions=_str_list(d.get("decisions")),
            action_items=_str_list(d.get("action_items")),
            entities=_str_list(d.get("entities")),
            not_covered=str(d.get("not_covered") or ""),
            method=str(d.get("method") or ""),
            limitations=[
                Limitation.from_json(lim) for lim in (d.get("limitations") or []) if isinstance(lim, dict)
            ],
        )


def _str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        text = str(item).strip() if item is not None else ""
        if text:
            out.append(text)
    return out


#: The stages, in order. `pipeline.py` runs them in this order and the UI
#: shows them in this order; a stage's index is its `step` id in the chat.
STAGES: Sequence[str] = (
    "probe",
    "audio",
    "transcript",
    "frames",
    "ocr",
    "vision",
    "fusion",
    "index",
    "artifacts",
)

STAGE_TITLES: Dict[str, str] = {
    "probe": "Probing the file",
    "audio": "Extracting audio",
    "transcript": "Transcribing",
    "frames": "Picking frames",
    "ocr": "Reading on-screen text",
    "vision": "Describing frames",
    "fusion": "Understanding the video",
    "index": "Indexing evidence",
    "artifacts": "Writing transcripts",
}
