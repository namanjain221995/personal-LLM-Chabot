"""The workspace sweep, per upload (F-01 / INF-4), and /health's work section.

On 2026-09-09 a 20 MB upload's own housekeeping deleted a 400 MB sibling's
in-flight parts, because `core/repo.enforce_quota_and_ttl` judged the single
TOP-LEVEL `uploads/` directory instead of the uploads inside it. These tests
pin the new unit of judgement — one `uploads/<conversation>/<upload_id>` — and
the three things the sweep must never remove: a live session's parts, a
recent upload's original, and anything under VIDEO_DATA_DIR.

Real filesystem in tmp_path and a real database: the whole point of the
hard-link test is that an inode survives an unlink, which no mock can
demonstrate.

The second half of the file covers `health._check_work` — the read-only
snapshot of what this box is working on. It lives here rather than in its own
module because it is the same slice of work (the SRE changes of
2026-09-11 own exactly one new test file) and it reads the same three tables
the sweep protects.
"""
from __future__ import annotations

import os
import time

import json

import pytest

from app import db
from app.config import settings
from app.core import repo as repolib

_HOUR = 3600.0
_TTL_HOURS = 24


@pytest.fixture()
def owner():
    user_id = int(db.create_user("sweep-owner", "hash"))
    db.create_conversation(user_id, "conv-sweep", "t")
    yield user_id
    db.delete_conversation(user_id, "conv-sweep")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A workspace and a video store that are NOT nested, exactly as deployed
    (`/data/workspaces` and `/data/video`), on one filesystem so hard links
    between them are possible — which is the deployed situation too."""
    ws = tmp_path / "workspaces"
    video = tmp_path / "video"
    ws.mkdir()
    video.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(ws))
    monkeypatch.setattr(settings, "video_data_dir", str(video))
    monkeypatch.setattr(settings, "workspace_ttl_hours", _TTL_HOURS)
    monkeypatch.setattr(settings, "workspace_quota_gb", 20)
    return ws


def _make_upload(ws, conversation_id: str, upload_id: str, *, parts=(), original=None):
    """Build `uploads/<conversation>/<upload_id>` the way uploads.py does."""
    root = ws / "uploads" / conversation_id / upload_id
    if parts:
        (root / "_parts").mkdir(parents=True, exist_ok=True)
        for index, payload in enumerate(parts):
            (root / "_parts" / str(index)).write_bytes(payload)
    if original is not None:
        name, payload = original
        (root / "_original").mkdir(parents=True, exist_ok=True)
        (root / "_original" / name).write_bytes(payload)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _age(path, hours: float) -> None:
    """Backdate every file and directory in a tree. Deepest first, so setting
    a parent's mtime is not undone by writing to a child afterwards."""
    when = time.time() - hours * _HOUR
    for root, dirs, files in os.walk(str(path), topdown=False):
        for name in files:
            os.utime(os.path.join(root, name), (when, when))
        for name in dirs:
            os.utime(os.path.join(root, name), (when, when))
    os.utime(str(path), (when, when))


# ---------------------------------------------------------------- the TTL --


def test_an_old_upload_with_no_record_is_swept(workspace):
    """The behaviour that must survive the rewrite: genuinely abandoned bytes
    still go. Nothing in the database refers to this upload."""
    root = _make_upload(workspace, "conv-sweep", "a" * 32, original=("x.pdf", b"old"))
    _age(workspace / "uploads", _TTL_HOURS + 2)

    repolib.enforce_quota_and_ttl()

    assert not root.exists()
    # The tree above it stays: `uploads/` itself is never a sweep target, and
    # removing it is what F-01 was.
    assert (workspace / "uploads").is_dir()


def test_a_sibling_upload_is_not_swept_with_its_neighbour(workspace, owner):
    """THE 2026-09-09 INCIDENT, as a test.

    Two uploads in one conversation: one abandoned a day ago, one whose
    chunked session is still `uploading` with four parts on disk. The old
    sweep deleted `uploads/` and took both. Each is now judged alone.
    """
    abandoned = _make_upload(
        workspace, "conv-sweep", "a" * 32, original=("small.mp4", b"done")
    )
    live = _make_upload(
        workspace, "conv-sweep", "b" * 32, parts=[b"0" * 16, b"1" * 16, b"2" * 16, b"3" * 16]
    )
    _age(workspace / "uploads", _TTL_HOURS + 2)
    db.create_upload_session(
        "b" * 32, owner, "conv-sweep", "big.mp4", "video", expected_parts=7
    )

    repolib.enforce_quota_and_ttl()

    assert not abandoned.exists()
    assert live.is_dir()
    assert sorted(os.listdir(live / "_parts")) == ["0", "1", "2", "3"]


def test_a_finalizing_session_survives_even_when_backdated(workspace, owner):
    """`finalizing` is the state a `complete` call holds while it concatenates
    parts. Removing them under it is the worst possible moment."""
    root = _make_upload(workspace, "conv-sweep", "c" * 32, parts=[b"p" * 8, b"q" * 8])
    _age(workspace / "uploads", _TTL_HOURS + 5)
    db.create_upload_session("c" * 32, owner, "conv-sweep", "big.mp4", "video")
    db.try_begin_upload_finalize("c" * 32)

    repolib.enforce_quota_and_ttl()

    assert root.is_dir() and (root / "_parts" / "0").exists()


def test_an_expired_session_no_longer_protects_its_parts(workspace, owner):
    """The protection is "open AND not expired", not "has a row". A session
    past its TTL has already given its parts back by contract (API.md,
    Expiry), so the directory is ordinary old bytes."""
    root = _make_upload(workspace, "conv-sweep", "d" * 32, parts=[b"z" * 8])
    _age(workspace / "uploads", _TTL_HOURS + 5)
    db.create_upload_session(
        "d" * 32, owner, "conv-sweep", "gone.mp4", "video", ttl_hours=0
    )

    repolib.enforce_quota_and_ttl()

    assert not root.exists()


def test_a_recent_uploads_row_protects_a_backdated_directory(workspace, owner):
    """The finished upload a conversation is about to ask questions of. Its
    `_original` bytes are the answer's input; the row's age is the authority,
    not the directory's."""
    root = _make_upload(
        workspace, "conv-sweep", "e" * 32, original=("report.pdf", b"%PDF-1.7")
    )
    _age(workspace / "uploads", _TTL_HOURS + 5)
    db.save_upload("e" * 32, "conv-sweep", "report.pdf", 8, "ready")

    repolib.enforce_quota_and_ttl()

    assert (root / "_original" / "report.pdf").read_bytes() == b"%PDF-1.7"


def test_a_repository_clone_is_still_judged_as_one_directory(workspace):
    """The other half of the workspace is unchanged: a clone is one top-level
    entry and is swept as one."""
    clone = workspace / "conv-sweep__acme__widgets"
    (clone / "src").mkdir(parents=True)
    (clone / "src" / "main.py").write_text("print('hi')\n")
    _age(clone, _TTL_HOURS + 1)

    repolib.enforce_quota_and_ttl()

    assert not clone.exists()


# -------------------------------------------------------------- the quota --


def test_the_quota_evicts_the_oldest_upload_and_stops(workspace, owner, monkeypatch):
    """Oldest first, per upload, until under the quota — and no further."""
    # 0 GB: unsatisfiable on purpose, so the loop runs to the end and shows
    # what it refuses to evict rather than stopping early on arithmetic.
    monkeypatch.setattr(settings, "workspace_quota_gb", 0)

    oldest = _make_upload(workspace, "conv-sweep", "1" * 32, original=("a.bin", b"a" * 64))
    _age(oldest, 10)
    middle = _make_upload(workspace, "conv-sweep", "2" * 32, original=("b.bin", b"b" * 64))
    _age(middle, 5)
    live = _make_upload(workspace, "conv-sweep", "3" * 32, parts=[b"c" * 64])
    _age(live, 3)
    db.create_upload_session("3" * 32, owner, "conv-sweep", "big.mp4", "video")

    repolib.enforce_quota_and_ttl()

    # Both idle uploads go (the quota is unsatisfiable), the live session's
    # parts do not: a quota is a reason to delete old bytes, never a reason to
    # break an upload in flight.
    assert not oldest.exists()
    assert not middle.exists()
    assert live.is_dir() and (live / "_parts" / "0").exists()


def test_nothing_under_the_video_data_dir_is_swept(tmp_path, monkeypatch):
    """VIDEO_DATA_DIR is outside the workspace by design. This pins the
    explicit guard for a deployment that ever points it inside one."""
    ws = tmp_path / "workspaces"
    ws.mkdir()
    video = ws / "video"
    (video / "abc").mkdir(parents=True)
    (video / "abc" / "source.mp4").write_bytes(b"frames")
    monkeypatch.setattr(settings, "workspace_dir", str(ws))
    monkeypatch.setattr(settings, "video_data_dir", str(video))
    monkeypatch.setattr(settings, "workspace_ttl_hours", _TTL_HOURS)
    monkeypatch.setattr(settings, "workspace_quota_gb", 0)
    _age(video, _TTL_HOURS + 48)

    repolib.enforce_quota_and_ttl()

    assert (video / "abc" / "source.mp4").read_bytes() == b"frames"


# ------------------------------------------------------- failure posture --


def test_a_database_that_cannot_answer_keeps_everything(workspace, monkeypatch):
    """Unknown means keep. A sweep that deletes on a failed query is exactly
    the incident, with a different trigger."""
    root = _make_upload(workspace, "conv-sweep", "f" * 32, parts=[b"y" * 8])
    _age(workspace / "uploads", _TTL_HOURS + 9)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("pool is closed")

    monkeypatch.setattr(db, "get_uploads", _boom)
    monkeypatch.setattr(db, "get_upload_session", _boom)

    repolib.enforce_quota_and_ttl()

    assert root.is_dir() and (root / "_parts" / "0").exists()


# ------------------------------------------------- the hard-link guarantee --


def test_the_analysis_source_survives_the_sweep_because_it_is_a_hard_link(workspace):
    """`video/store.adopt_source` HARD-LINKS the upload into
    `<VIDEO_DATA_DIR>/<sha256>/source.<ext>`, so the sweep unlinking the
    workspace copy drops ONE link and the bytes stay.

    This runs on a real filesystem on purpose: the claim is about inode link
    counts, and a mocked filesystem would prove nothing. It is also the reason
    an analysis may safely outlive its upload's 24 h.
    """
    from app.video import store as video_store

    payload = b"\x00\x01mp4 bytes" * 512
    root = _make_upload(workspace, "conv-sweep", "9" * 32, original=("clip.mp4", payload))
    source = root / "_original" / "clip.mp4"
    content_hash = video_store.hash_file(str(source))

    adopted = video_store.adopt_source(content_hash, str(source), "clip.mp4")
    assert os.stat(adopted).st_nlink == 2, "adopt_source did not hard-link"
    assert os.stat(adopted).st_ino == os.stat(source).st_ino

    _age(workspace / "uploads", _TTL_HOURS + 3)
    repolib.enforce_quota_and_ttl()

    assert not root.exists(), "the workspace copy should have been swept"
    assert os.path.exists(adopted)
    with open(adopted, "rb") as fh:
        assert fh.read() == payload
    assert os.stat(adopted).st_nlink == 1


# =========================================================================
# GET /health -> work: what this box is currently doing.
#
# Nothing on this box could answer "is anything stuck" without a psql
# session. These pin the three properties that make the answer usable: it is
# correct, it never says "nothing" when it means "I could not look", and it
# carries no identifiers (Prometheus scrapes /health unauthenticated).
# =========================================================================


@pytest.fixture()
def fresh_work_cache():
    """The snapshot is cached for five seconds in a module global, which
    would otherwise leak between tests in either direction."""
    from app import health

    health._work_cache = (0.0, {})
    yield
    health._work_cache = (0.0, {})


def test_work_counts_what_is_in_flight(owner, fresh_work_cache):
    from app import health

    db.upsert_video_analysis("a" * 64, 10, "video/mp4", "one.mp4")
    running = db.upsert_video_analysis("b" * 64, 10, "video/mp4", "two.mp4")
    db.update_video_analysis(int(running["id"]), status="running")
    db.create_upload_session("1" * 32, owner, "conv-sweep", "big.mp4", "video")
    db.create_upload_session("2" * 32, owner, "conv-sweep", "doc.pdf", "document")
    db.try_begin_upload_finalize("2" * 32)
    db.create_chat_request("intent-1", owner, "conv-sweep", "gen-1", {"question": "hi"})
    db.set_chat_request_status("intent-1", "interrupted")

    work = health._check_work()

    assert work["status"] == "ok"
    assert work["video"]["queued"] == 1 and work["video"]["running"] == 1
    assert work["uploads"] == {"uploading": 1, "finalizing": 1}
    assert work["chat_requests_interrupted"] == 1
    # An analysis that has just started has not been silent for long — this is
    # the number VideoAnalysisStalled watches, so it has to move with the row.
    assert work["video"]["oldest_running_age_s"] < 60


def test_work_carries_no_identifiers(owner, fresh_work_cache):
    """Counts only. /health is public inside the Docker network, so a
    conversation id, an intent id or a filename appearing here would be a
    disclosure, not a convenience."""
    import json

    from app import health

    db.upsert_video_analysis("c" * 64, 10, "video/mp4", "private-name.mp4")
    db.create_upload_session("3" * 32, owner, "conv-sweep", "salary.pdf", "document")
    db.create_chat_request("intent-secret", owner, "conv-sweep", "gen-2", {"q": "x"})

    rendered = json.dumps(health._check_work())

    for leak in ("private-name", "salary", "intent-secret", "conv-sweep", "gen-2"):
        assert leak not in rendered


def test_work_publishes_the_gauges_the_alerts_read(owner, fresh_work_cache):
    """The alert rules in monitoring/prometheus/rules/alerts.yml name these
    series. If this test fails, those alerts are querying nothing."""
    from app import health, metrics

    metrics.reset()
    db.upsert_video_analysis("d" * 64, 10, "video/mp4", "x.mp4")
    db.create_upload_session("4" * 32, owner, "conv-sweep", "x.mp4", "video")

    health._check_work()
    rendered = metrics.render()

    assert 'video_queue_depth{state="queued"} 1' in rendered
    assert 'upload_sessions_open{state="uploading"} 1' in rendered
    assert "chat_requests_interrupted 0" in rendered
    assert "live_generations 0" in rendered
    assert "video_running_stage_age_seconds 0" in rendered
    metrics.reset()


def test_work_says_unknown_when_it_cannot_look(fresh_work_cache, monkeypatch):
    """"I could not ask" must never render as "nothing is running" — that
    confusion is INF-2 in a different endpoint."""
    from app import health

    def _boom():
        raise RuntimeError("pool is closed")

    monkeypatch.setattr(health, "_read_work", _boom)

    work = health._check_work()

    assert work["status"] == "unknown"
    assert "RuntimeError" in work["detail"]


def test_work_is_cached_so_a_polling_probe_is_cheap(owner, fresh_work_cache, monkeypatch):
    """/health is called by the container healthcheck every 30 s AND by the
    blackbox probe every 15 s, and deploy.sh polls it in a loop."""
    from app import health

    calls = []
    real = health._read_work
    monkeypatch.setattr(health, "_read_work", lambda: (calls.append(1), real())[1])

    first = health._check_work()
    second = health._check_work()

    assert first is second
    assert len(calls) == 1


def test_the_health_route_actually_returns_the_work_section(login_client):
    """`check_dependencies` computed `work` and the /health ROUTE dropped it,
    so nothing outside the process could see it — the same way `web_index`
    was computed and dropped before 2026-09-06. Found by reading the live
    payload after the deploy, not by reading the code."""
    body = login_client("health-work").get("/health").json()
    assert "work" in body, f"/health returned {sorted(body)} — no work section"
    work = body["work"]
    assert isinstance(work, dict)
    # What an operator needs to tell busy from stalled from failed.
    for key in ("live_generations", "video", "uploads", "chat_requests_interrupted"):
        assert key in work, f"work is missing {key}: {sorted(work)}"
    assert set(work["video"]) >= {"queued", "running"}
    assert set(work["uploads"]) >= {"uploading", "finalizing"}
    # No identifiers, ever: this endpoint is unauthenticated inside the network.
    blob = json.dumps(work)
    assert "@" not in blob and "conv-" not in blob
