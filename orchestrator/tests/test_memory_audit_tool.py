"""scripts/memory_audit.py — the clean-up an operator can run (2026-09-21).

The write rules can only stop the NEXT bad row. Every row written before
release 1 is still read back on each turn under "treat as true for this
user", which is why one account answers "what is my name?" with a stranger's
name. This tool is how somebody looks at those rows and removes them, so its
two halves both need holding down: the classification must be right about
every class, and the delete path must be impossible to reach by accident.

The tool talks to a real PostgreSQL — the suite's test database — because its
whole job is what it does to `user_facts`, and a mocked connection would
prove nothing about the read-only guard.
"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

from app import db
from app.config import settings

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TOOL = os.path.join(_REPO, "scripts", "memory_audit.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("memory_audit", _TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_tool = _load_tool()


#: The paste behind the owner's 13 rows, rebuilt from their shape.
_INTERVIEW = """Interview Simulation

Act as an interviewer for a designation_of_candidate role at Company_name.
My name is Aa m a n i Nand e n d l a.

Rules:
1. Bold all the keywords in your responses.
2. Answers must be paragraphs, no meta-text.
3. The first answer should be a professional self-introduction of 150-180 words."""

_CV = (
    "NAVEEN R. VELUMALA\n"
    "AI/ML Engineer | San Francisco, CA | naveen.v@example.invalid\n"
    "Senior Machine Learning Engineer at Cognitiv, 2021-present.\n"
)


def _row(**kwargs) -> dict:
    row = {
        "id": 1,
        "user_id": 1,
        "fact": "",
        "source": "stated",
        "source_excerpt": None,
        "source_conversation_id": "c1",
    }
    row.update(kwargs)
    return row


# --- 1. one test per class -------------------------------------------------


def test_an_identity_taken_from_a_pasted_script_is_identity_from_document():
    row = _row(
        fact="The user's name is Aa m a n i Nand e n d l a", source_excerpt=_INTERVIEW
    )
    assert audit_tool.classify(row) == audit_tool.IDENTITY_FROM_DOCUMENT


def test_an_identity_taken_from_a_pasted_cv_is_identity_from_document():
    row = _row(fact="The user's name is Naveen R. Velumala", source_excerpt=_CV)
    assert audit_tool.classify(row) == audit_tool.IDENTITY_FROM_DOCUMENT


def test_a_rule_from_a_pasted_script_is_a_pasted_prompt():
    row = _row(
        fact="The user requires answers to be formatted as paragraphs without meta-text",
        source_excerpt=_INTERVIEW,
    )
    assert audit_tool.classify(row) == audit_tool.PASTED_PROMPT


def test_a_row_holding_a_template_slot_is_a_pasted_prompt_without_its_excerpt():
    """The 2026-09-16 rows have provenance; rows older than V40 have none, and
    the fact itself still says where it came from."""
    row = _row(
        fact="The user is a professional interviewing for a"
        " designation_of_candidate role at Company_name",
        source=None,
        source_excerpt=None,
    )
    assert audit_tool.classify(row) == audit_tool.PASTED_PROMPT


def test_a_fact_from_a_pasted_document_is_pasted_document():
    row = _row(fact="The user is a Senior Machine Learning Engineer", source_excerpt=_CV)
    assert audit_tool.classify(row) == audit_tool.PASTED_DOCUMENT


def test_a_one_off_request_is_a_task_request():
    row = _row(
        fact="The user is asking for 200 LeetCode questions",
        source_excerpt="Give me 200 LeetCode questions.",
    )
    assert audit_tool.classify(row) == audit_tool.TASK_REQUEST


def test_a_number_the_message_never_carried_is_ungrounded():
    row = _row(
        fact="The user's team at Halcyon Rail has 29 engineers",
        source_excerpt="I'm at Halcyon Rail now.",
    )
    assert audit_tool.classify(row) == audit_tool.UNGROUNDED


def test_a_row_with_no_provenance_is_unverifiable_not_condemned():
    row = _row(fact="The user is vegetarian", source=None, source_excerpt=None)
    assert audit_tool.classify(row) == audit_tool.UNVERIFIABLE


def test_a_genuine_preference_is_ok():
    row = _row(
        fact="The user prefers answers in Hindi",
        source_excerpt="Please always answer in Hindi.",
    )
    assert audit_tool.classify(row) == audit_tool.OK


def test_the_same_fact_stated_twice_is_a_duplicate_and_the_first_is_kept():
    rows = [
        _row(id=1, fact="The user's name is Bob Rivera", source_excerpt="My name is Bob Rivera."),
        _row(id=2, fact="The user's name is Bob Rivera.", source_excerpt="My name is Bob Rivera."),
    ]
    classified = audit_tool.audit(rows)
    assert [r["class"] for r in classified] == [audit_tool.OK, audit_tool.DUPLICATE]


def test_two_different_names_for_one_account_is_a_duplicate():
    rows = [
        _row(id=1, fact="The user's name is Bob Rivera", source_excerpt="My name is Bob Rivera."),
        _row(id=2, fact="The user's name is Robert R Rivera", source_excerpt="I'm Robert R Rivera."),
    ]
    assert [r["class"] for r in audit_tool.audit(rows)][1] == audit_tool.DUPLICATE


def test_a_name_taken_from_a_cv_does_not_make_the_persons_own_name_the_duplicate():
    """The worst possible outcome for this tool: condemning the real row and
    keeping the stranger's. The CV row is judged first and never seeds the
    duplicate search."""
    rows = [
        _row(id=1, fact="The user's name is Naveen R. Velumala", source_excerpt=_CV),
        _row(id=2, fact="The user's name is Bob Rivera", source_excerpt="My name is Bob Rivera."),
    ]
    assert [r["class"] for r in audit_tool.audit(rows)] == [
        audit_tool.IDENTITY_FROM_DOCUMENT,
        audit_tool.OK,
    ]


def test_two_accounts_do_not_duplicate_each_other():
    rows = [
        _row(id=1, user_id=1, fact="The user's name is Sam", source_excerpt="My name is Sam."),
        _row(id=2, user_id=2, fact="The user's name is Sam", source_excerpt="My name is Sam."),
    ]
    assert [r["class"] for r in audit_tool.audit(rows)] == [audit_tool.OK, audit_tool.OK]


def test_counts_name_every_class_including_the_empty_ones():
    tally = audit_tool.counts(audit_tool.audit([_row(fact="The user prefers Hindi answers")]))
    assert set(tally) == set(audit_tool.CLASSES)
    assert tally[audit_tool.PASTED_DOCUMENT] == 0


# --- 2. the delete path refuses to be reached by accident ------------------


def _args(argv):
    return audit_tool.build_parser().parse_args(argv)


def test_the_default_run_is_read_only():
    args = _args(["--dsn", "postgresql://x/y"])
    assert args.delete is False
    assert audit_tool.check_delete_request(args) == []


@pytest.mark.parametrize(
    "argv,missing",
    [
        (["--delete"], "--yes"),
        (["--delete", "--yes"], "--user <id>"),
        (["--delete", "--user", "29"], "--yes"),
        (["--delete", "--yes", "--user", "29"], "--backup <path>"),
        (["--delete", "--backup", "/tmp/b.json"], "--yes"),
    ],
)
def test_delete_refuses_without_every_flag(argv, missing):
    with pytest.raises(SystemExit) as excinfo:
        audit_tool.check_delete_request(_args(["--dsn", "postgresql://x/y"] + argv))
    assert missing in str(excinfo.value)
    assert "never deletes unattended" in str(excinfo.value)


def test_delete_refuses_to_remove_clean_or_unjudgeable_rows():
    for name in (audit_tool.OK, audit_tool.UNVERIFIABLE):
        with pytest.raises(SystemExit) as excinfo:
            audit_tool.check_delete_request(
                _args(
                    [
                        "--dsn", "postgresql://x/y", "--delete", "--yes",
                        "--user", "29", "--backup", "/tmp/b.json",
                        "--classes", name,
                    ]
                )
            )
        assert "refusing to delete class" in str(excinfo.value)


def test_delete_refuses_an_unknown_class():
    with pytest.raises(SystemExit) as excinfo:
        audit_tool.check_delete_request(
            _args(
                [
                    "--dsn", "postgresql://x/y", "--delete", "--yes",
                    "--user", "29", "--backup", "/tmp/b.json",
                    "--classes", "everything",
                ]
            )
        )
    assert "unknown class" in str(excinfo.value)


def test_the_full_delete_request_is_accepted():
    assert audit_tool.check_delete_request(
        _args(
            [
                "--dsn", "postgresql://x/y", "--delete", "--yes",
                "--user", "29", "--backup", "/tmp/b.json",
            ]
        )
    ) == list(audit_tool.DELETABLE)


# --- 3. against the real table ---------------------------------------------


@pytest.fixture()
def store():
    alice = db.create_user("alice", "hash")
    bob = db.create_user("bob", "hash")
    for fact in (
        "The user's name is Aa m a n i Nand e n d l a",
        "The user requires answers to be formatted as paragraphs without meta-text",
        "The user wants the first answer to be a professional self-introduction"
        " (~150-180 words)",
    ):
        db.add_user_fact(alice, fact, "c1", source="stated", source_excerpt=_INTERVIEW)
    db.add_user_fact(
        alice, "The user prefers answers in Hindi", "c2",
        source="stated", source_excerpt="Please always answer in Hindi.",
    )
    db.add_user_fact(
        bob, "The user's name is Bob Rivera", "c3",
        source="stated", source_excerpt="My name is Bob Rivera.",
    )
    return alice, bob


def test_a_read_only_run_cannot_write(store):
    import psycopg

    con = audit_tool._connect(settings.app_database_url, read_only=True)
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            con.execute("DELETE FROM user_facts WHERE id = -1")
    finally:
        con.close()


def test_a_read_only_run_changes_nothing(store, capsys):
    alice, _bob = store
    before = [f["id"] for f in db.list_user_facts(alice)]
    assert audit_tool.main(["--dsn", settings.app_database_url]) == 0
    assert [f["id"] for f in db.list_user_facts(alice)] == before
    printed = capsys.readouterr().out
    assert "read-only" in printed
    assert "identity_from_document 1" in printed


def test_no_rows_prints_counts_without_anybodys_words(store, capsys):
    assert audit_tool.main(["--dsn", settings.app_database_url, "--no-rows"]) == 0
    printed = capsys.readouterr().out
    assert "identity_from_document" in printed
    assert "Hindi" not in printed
    assert "Rivera" not in printed


def test_the_delete_backs_up_first_and_touches_one_account_only(store, tmp_path, capsys):
    alice, bob = store
    backup = tmp_path / "alice.json"
    assert audit_tool.main(
        [
            "--dsn", settings.app_database_url, "--delete", "--yes",
            "--user", str(alice), "--backup", str(backup),
        ]
    ) == 0
    # the three interview rows go, the person's own preference stays
    assert [f["fact"] for f in db.list_user_facts(alice)] == [
        "The user prefers answers in Hindi"
    ]
    # the other account is untouched
    assert len(db.list_user_facts(bob)) == 1
    saved = json.loads(backup.read_text())
    assert len(saved) == 3
    assert {r["class"] for r in saved} == {
        audit_tool.IDENTITY_FROM_DOCUMENT,
        audit_tool.PASTED_PROMPT,
    }
    # every column is in the backup, so a row can be put back by hand
    assert {"id", "user_id", "fact", "source", "source_excerpt"} <= set(saved[0])
    printed = capsys.readouterr().out
    assert "would delete 3 of 4 rows" in printed
    assert "deleted 3 rows" in printed


def test_the_delete_refuses_to_overwrite_an_earlier_backup(store, tmp_path):
    alice, _bob = store
    backup = tmp_path / "alice.json"
    backup.write_text("[]\n")
    with pytest.raises(SystemExit) as excinfo:
        audit_tool.main(
            [
                "--dsn", settings.app_database_url, "--delete", "--yes",
                "--user", str(alice), "--backup", str(backup),
            ]
        )
    assert "refusing to overwrite" in str(excinfo.value)
    assert len(db.list_user_facts(alice)) == 4  # nothing was deleted
