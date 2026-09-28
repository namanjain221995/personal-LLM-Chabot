"""core/steps.py — the step timeline shared by the Max loop and best-of-N."""
import asyncio

from app.core.steps import Steps


def _collect():
    frames = []

    async def emit(kind, data):
        frames.append((kind, dict(data)))

    return frames, emit


def test_progress_updates_the_open_row_in_place_and_is_silent_when_none_is_open():
    frames, emit = _collect()

    async def go():
        steps = Steps(emit)
        await steps.progress("nobody is listening")
        await steps.open("Drafting", "0 of 3")
        await steps.progress("1 of 3")
        await steps.done("3 of 3")
        await steps.progress("after close")
        return steps

    steps = asyncio.run(go())
    assert frames == [
        ("step", {"id": 1, "title": "Drafting", "status": "running", "detail": "0 of 3"}),
        ("step", {"id": 1, "title": "Drafting", "status": "running", "detail": "1 of 3"}),
        ("step", {"id": 1, "title": "Drafting", "status": "done", "detail": "3 of 3"}),
    ]
    # Only the CLOSE is recorded for the persisted meta: one row per step.
    assert steps.finished == [{"id": 1, "title": "Drafting", "status": "done", "detail": "3 of 3"}]


def test_detail_is_clipped_to_what_the_ui_shows():
    frames, emit = _collect()

    async def go():
        steps = Steps(emit)
        await steps.open("Judging")
        await steps.progress("x" * 500)
        await steps.done("y" * 500)

    asyncio.run(go())
    assert len(frames[1][1]["detail"]) == 200
    assert len(frames[2][1]["detail"]) == 200
