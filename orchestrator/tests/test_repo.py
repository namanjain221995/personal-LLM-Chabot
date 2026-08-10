"""GitHub repo analysis (Phase 3): URL detection, caps, chunking, indexing, search."""
import os

import pytest

from app import db
from app.config import settings
from app.core import repo as repolib
from app.core.repo import GithubRef, RepoError, detect_github
from app.core.repo_index import chunk_file, index_repo


def test_detect_repo_url():
    r = detect_github("look at https://github.com/duckdb/duckdb please")
    assert r and r.owner == "duckdb" and r.repo == "duckdb" and r.path is None
    assert r.clone_url == "https://github.com/duckdb/duckdb.git"


def test_detect_strips_dot_git_and_tree_ref():
    r = detect_github("https://github.com/foo/bar.git")
    assert r.repo == "bar"
    r2 = detect_github("https://github.com/foo/bar/tree/dev")
    assert r2.ref == "dev" and r2.path is None


def test_detect_blob_single_file():
    r = detect_github("https://github.com/foo/bar/blob/main/src/app.py")
    assert r.path == "src/app.py" and r.ref == "main"


def test_detect_none_for_non_github():
    assert detect_github("https://gitlab.com/x/y") is None
    assert detect_github("no url") is None


def test_chunk_file_tracks_line_ranges():
    text = "\n".join(f"line {i}" for i in range(1, 151))  # 150 lines
    chunks = chunk_file("a.py", text)
    assert chunks[0].start_line == 1
    assert chunks[0].end_line <= 60
    assert chunks[-1].end_line == 150
    # overlap: second chunk starts before the first ends
    assert chunks[1].start_line < chunks[0].end_line + 1


def test_index_and_overview(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def login(user):\n    return verify(user)\n" * 20
    )
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / "README.md").write_text("# Cool Project\nDoes things.")
    (tmp_path / "package.json").write_text("{}")
    ov = repolib.build_overview(str(tmp_path))
    assert ov.file_count >= 3
    assert any(lang == "Python" for lang, _ in ov.languages)
    assert "main.py" in ov.entry_points
    assert "package.json" in ov.key_configs
    assert "Cool Project" in ov.readme
    chunks = index_repo(str(tmp_path))
    assert any(c.path.endswith("auth.py") for c in chunks)


def test_clone_rejects_oversized_repo(monkeypatch):
    monkeypatch.setattr(settings, "repo_max_mb", 100)
    monkeypatch.setattr(repolib, "_github_repo_size_kb", lambda ref: 2_000_000)  # 2 GB
    with pytest.raises(RepoError, match="over the 100 MB limit"):
        repolib.shallow_clone(GithubRef("big", "repo"), "/tmp/nope")


def test_clone_rejects_too_many_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "repo_max_files", 3)
    monkeypatch.setattr(repolib, "_github_repo_size_kb", lambda ref: 10)

    dest = str(tmp_path / "clone")

    def fake_run(cmd, **kw):
        # simulate a clone that produced 5 files
        if cmd[:1] == ["git"] and "clone" in cmd:
            os.makedirs(os.path.join(dest, ".git", "hooks"), exist_ok=True)
            for i in range(5):
                open(os.path.join(dest, f"f{i}.py"), "w").close()

        class R:
            stdout = "abc123"
            stderr = ""

        return R()

    monkeypatch.setattr(repolib.subprocess, "run", fake_run)
    with pytest.raises(RepoError, match="over the 3 limit"):
        repolib.shallow_clone(GithubRef("o", "r"), dest)
    assert not os.path.exists(dest)  # cleaned up on rejection


@pytest.fixture()
def temp_db():
    """Kept as a name so the tests below read unchanged; the isolated database
    now comes from the autouse conftest fixture."""
    return None


def test_repo_chunk_storage_and_search(temp_db):
    db.save_repo("c1", "o/r", "https://github.com/o/r.git", "sha1")
    assert db.get_repo_keys("c1") == ["o/r"]
    db.replace_repo_chunks(
        "c1",
        "o/r",
        [
            {"path": "src/auth.py", "start_line": 10, "end_line": 40,
             "text": "def authenticate(token): verify_jwt(token)"},
            {"path": "src/utils.py", "start_line": 1, "end_line": 20,
             "text": "def add(a, b): return a + b"},
        ],
    )
    hits = db.search_repo_chunks("c1", ["authenticate", "jwt"], limit=5)
    assert hits and hits[0]["path"] == "src/auth.py"
    assert hits[0]["start_line"] == 10 and hits[0]["end_line"] == 40


def test_repo_chunk_search_weights_path(temp_db):
    db.save_repo("c2", "o/r", "u", "s")
    db.replace_repo_chunks(
        "c2",
        "o/r",
        [
            {"path": "auth/login.py", "start_line": 1, "end_line": 5, "text": "x = 1"},
            {"path": "misc.py", "start_line": 1, "end_line": 5, "text": "auth stuff here"},
        ],
    )
    # a path match (auth/login.py) should outrank a single body match
    hits = db.search_repo_chunks("c2", ["auth"], limit=5)
    assert hits[0]["path"] == "auth/login.py"
