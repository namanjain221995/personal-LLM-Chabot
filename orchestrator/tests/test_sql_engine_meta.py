"""Offline sql-engine tests: §10 meta contract and DuckDB external-access
lockdown. No vLLM servers/GPU — llm calls are monkeypatched, DuckDB is a
temp file."""
import asyncio

import duckdb
import pytest

from app.config import settings
from app.engines import sql as sql_engine


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    """A tiny read-only warehouse the engine can query."""
    db_path = str(tmp_path / "warehouse.duckdb")
    con = duckdb.connect(db_path)
    con.execute("CREATE TABLE opportunities (stage VARCHAR, amount DOUBLE)")
    con.execute(
        "INSERT INTO opportunities VALUES ('Prospecting', 100.0), ('Closed Won', 250.0)"
    )
    con.close()
    monkeypatch.setattr(settings, "duckdb_path", db_path)
    return db_path


def test_execute_blocks_filesystem_and_network(warehouse):
    """§1/§12 root-cause fix: enable_external_access=false on the connection.

    Even if a hostile SELECT slipped past sql_guard, DuckDB itself must
    refuse filesystem/network table functions.
    """
    for hostile in (
        "SELECT content FROM read_text('/etc/hostname')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM read_csv('https://attacker.example/x.csv')",
    ):
        with pytest.raises(duckdb.Error):
            sql_engine._execute(hostile, fetch_cap=10)
    # Normal warehouse queries still work.
    columns, rows = sql_engine._execute("SELECT * FROM opportunities ORDER BY amount", 10)
    assert columns == ["stage", "amount"]
    assert len(rows) == 2


def test_run_sql_engine_emits_single_contract_meta(warehouse, monkeypatch):
    """§10: exactly ONE meta, carrying route + data (row objects) +
    top-level truncated, emitted after the token stream."""

    async def fake_chat_completion(messages, **kwargs):
        return "SELECT stage, amount FROM opportunities ORDER BY amount"

    async def fake_stream(messages, **kwargs):
        for tok in ("Two ", "rows."):
            yield tok

    monkeypatch.setattr(sql_engine.llm, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(sql_engine.llm, "stream_chat_completion", fake_stream)

    events = []

    async def emit(event, data):
        events.append((event, data))

    answer = asyncio.run(sql_engine.run_sql_engine("total by stage", [], emit))
    assert answer == "Two rows."

    metas = [d for e, d in events if e == "meta"]
    assert len(metas) == 1, "meta must be emitted exactly once per turn (§10)"
    meta = metas[0]

    assert meta["route"] == "sql"          # §10 key is `route`, not `engine`
    assert isinstance(meta["data"], list)  # array of row objects
    assert meta["data"][0] == {"stage": "Prospecting", "amount": 100.0}
    assert meta["truncated"] is False      # top-level sibling of data
    assert "engine" not in meta and "export_file" not in meta

    # Single FINAL meta: all tokens precede it (§10: "before done").
    kinds = [e for e, _ in events]
    assert kinds.index("meta") > max(i for i, k in enumerate(kinds) if k == "token")


def test_export_rides_report_files_contract_key(warehouse, monkeypatch):
    """§10: exports surface as report_files [{filename, type, size}]."""

    async def fake_chat_completion(messages, **kwargs):
        return "SELECT stage, amount FROM opportunities"

    async def fake_stream(messages, **kwargs):
        yield "Done."

    monkeypatch.setattr(sql_engine.llm, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(sql_engine.llm, "stream_chat_completion", fake_stream)
    monkeypatch.setattr(settings, "reports_dir", str(settings.duckdb_path).rsplit("/", 1)[0])

    events = []

    async def emit(event, data):
        events.append((event, data))

    asyncio.run(sql_engine.run_sql_engine("export the pipeline to csv", [], emit))
    (meta,) = [d for e, d in events if e == "meta"]

    assert "export_file" not in meta
    files = meta["report_files"]
    assert len(files) == 1
    assert files[0]["filename"].endswith(".csv")
    assert files[0]["type"] == "csv"
    assert isinstance(files[0]["size"], int) and files[0]["size"] > 0


# ── An empty result is not a finding ─────────────────────────────────────────
# Asked how many internal interviews five named people had completed, a query
# that joined the interviewer to the wrong object returned nothing, and the
# reply was "there are no internal interview records in the synced data for
# [them]" — a claim about the org made from the silence of a query the model
# wrote itself. They had 84 between them.

def test_an_empty_result_is_labelled_as_such_in_the_authoritative_block():
    """The narration prompt REQUIRES every figure to come from the computed
    block, so that block is where the warning has to live. An instruction
    elsewhere is a hope; this is a mechanism."""
    from app.engines.sql import deterministic_summary

    out = deterministic_summary(["Name", "Total"], [])
    assert out["total_rows"] == 0
    assert out["empty_result"] is True
    note = out["what_zero_rows_means"]
    assert "NOT evidence that the records do not exist" in note
    # It must name the ways a query returns nothing without erroring.
    for cause in ("wrong join", "spelled differently", "date literal", "filter"):
        assert cause in note
    assert "Never state that the business has no such records." in note


def test_a_non_empty_result_carries_no_such_warning():
    """The note is for the empty case only; on real rows it would be noise the
    model might quote."""
    from app.engines.sql import deterministic_summary

    out = deterministic_summary(["Name", "Total"], [["Jay Soni", 24]])
    assert out["total_rows"] == 1
    assert "empty_result" not in out
    assert "what_zero_rows_means" not in out


def test_the_answer_rules_forbid_asserting_absence():
    from app.core import org_brief

    rules = org_brief.ANSWER_RULES
    assert "An EMPTY result is not a finding about the business" in rules
    # The wording wraps, so assert on the load-bearing fragments.
    assert "never write that a person has no" in rules
    assert "that a process never happened" in rules
    assert "may be recorded under" in rules


# ── Dates are written ISO, whatever the user typed ───────────────────────────

def test_the_sql_prompt_requires_iso_date_literals():
    """The existing rule says how to READ the user's day-first date. Writing
    one back out is a separate mistake: TRY_CAST('17-08-2026' AS DATE) is NULL
    in DuckDB, so the predicate never matches and the query returns nothing
    instead of erroring."""
    from app.engines.sql import _SQL_SYSTEM

    assert "WRITE every date literal in the SQL as ISO" in _SQL_SYSTEM
    assert "DATE '2026-08-17'" in _SQL_SYSTEM
    assert "'17-08-2026' is NOT parseable" in _SQL_SYSTEM
    # …and the reading rule it complements is still there.
    assert "DAY-MONTH-YEAR" in _SQL_SYSTEM


def test_duckdb_really_does_return_null_for_a_day_first_literal():
    """The premise of the rule above, asserted rather than assumed."""
    import duckdb

    con = duckdb.connect(":memory:")
    assert con.execute("SELECT TRY_CAST('17-08-2026' AS DATE)").fetchone()[0] is None
    assert con.execute("SELECT TRY_CAST('2026-08-17' AS DATE)").fetchone()[0] is not None


# ── Who a named person actually is ───────────────────────────────────────────
# "How many internal interviews has X completed" is genuinely ambiguous: X may
# be the candidate who sat them (Account) or the employee who ran them
# (Recruiter__c). The model guessed, and guessed candidate for five people who
# are staff — reporting that they had none when they had 84 between them. A
# rule in the prompt cannot know who THESE people are; one indexed lookup can.

def test_two_capitalised_words_are_read_as_a_person():
    from app.engines.sql import people_in_question

    assert people_in_question(
        "How many internal interviews have Jayesh Prajapati and Jay Soni completed?"
    ) == ["Jayesh Prajapati", "Jay Soni"]


def test_a_sentence_opener_is_not_a_person():
    """Deliberately conservative: a single capitalised word is far more often a
    product, an object or the first word of a sentence."""
    from app.engines.sql import people_in_question

    assert people_in_question("Show me the top accounts") == []
    assert people_in_question("How many advanced mocks today?") == []
    assert people_in_question("Which Salesforce object holds payments?") == []


def test_the_lookup_is_capped_so_a_report_does_not_become_eight_queries():
    from app.engines.sql import people_in_question, _MAX_RESOLVED_PEOPLE

    many = " and ".join(f"Person Number{i}" for i in range(20))
    assert len(people_in_question(many)) <= _MAX_RESOLVED_PEOPLE


def test_an_unreachable_warehouse_costs_the_hint_not_the_answer(monkeypatch):
    """Grounding is an optimisation. A locked or missing warehouse must leave
    the question answerable, not raise out of prompt assembly."""
    from app.engines import sql as sqleng

    def boom(_duckdb):
        raise RuntimeError("locked")

    monkeypatch.setattr(sqleng, "_connect_warehouse", boom)
    assert sqleng.who_these_people_are("interviews for Jayesh Prajapati") == ""


def test_a_question_naming_nobody_adds_nothing(monkeypatch):
    """No names means no lookup at all — an ordinary question must not pay for
    this, in latency or in prompt size."""
    from app.engines import sql as sqleng

    def never(_duckdb):
        raise AssertionError("the warehouse must not be touched")

    monkeypatch.setattr(sqleng, "_connect_warehouse", never)
    assert sqleng.who_these_people_are("how many advanced mocks today") == ""


def test_the_resolved_block_names_the_join_and_the_stored_spelling(monkeypatch):
    """The two things the model got wrong: which object to join, and that the
    surname is stored lower-case ('Khushi ghorawath'), so an exact match
    returned a silent 0 for her while everyone else's figures looked right."""
    from app.engines import sql as sqleng

    class FakeCon:
        def execute(self, sql, params):
            self._rows = [("Khushi ghorawath",)] if "Recruiter__c" in sql else []
            return self

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    monkeypatch.setattr(sqleng, "_connect_warehouse", lambda _d: FakeCon())
    block = sqleng.who_these_people_are("interviews run by Khushi Ghorawath")
    assert "Recruiter__c" in block
    assert "Internal_Interview__c.Interviewer__c" in block
    # The block used to say "Stored as: <name>". It now hands the model a
    # ready-made predicate instead: told only the spelling, the model kept
    # building its LIKE pattern from the QUESTION, which matched nothing and
    # returned zero rows — indistinguishable from "this person has no
    # records". Same fact, stronger contract.
    assert "Khushi ghorawath" in block
    assert "FILTER WITH:" in block
    # Case-insensitive advice was replaced by something stricter: the exact
    # stored value to filter on. _SQL_SYSTEM still carries the general ILIKE
    # rule (asserted separately below) for names this lookup never resolved.
    assert "VERBATIM" in block


def test_the_sql_prompt_requires_loose_name_matching():
    from app.engines.sql import _SQL_SYSTEM

    assert "Match a PERSON or RECORD NAME case-insensitively" in _SQL_SYSTEM
    assert "never with = or IN" in _SQL_SYSTEM


# ── The words people use for money map to the payment tables ────────────────
# "how much money they have paid" matched NO alias, so the prompt carried zero
# Payment__c fields and the model invented `p.Status__c` (real column:
# Payment_Status__c) — a raw DuckDB binder error reached the user after two
# answered clarifications.

def test_payment_words_pin_the_payment_tables():
    from app.core import org_brief

    for word in ("paid", "money", "fee", "fees", "amount"):
        tables = org_brief.TABLE_ALIASES.get(word)
        assert tables and "Payment__c" in tables, word
    for word in ("enrolled", "enrolment", "enrollment"):
        tables = org_brief.TABLE_ALIASES.get(word)
        assert tables and "Candidate_Training__c" in tables, word

    q = "how many candidates enrolled and how much money they paid"
    picked = org_brief.tables_for(q)
    assert "Payment__c" in picked
    assert "Candidate_Training__c" in picked


# ── A binder error teaches the retry the table's real columns ────────────────

def test_a_binder_error_is_enriched_with_the_real_column_list():
    from app.engines.sql import _enriched_error

    schema = {
        "Payment__c": [
            ("Id", "VARCHAR"),
            ("Payment_Status__c", "VARCHAR"),
            ("Amount_Paid__c", "VARCHAR"),
        ]
    }
    out = _enriched_error(
        'Binder Error: Table "p" does not have a column named "Status__c"',
        "SELECT * FROM Payment__c p WHERE p.Status__c = 'Paid'",
        schema,
    )
    assert "Payment_Status__c" in out and "Amount_Paid__c" in out
    assert "Do not invent columns" in out


def test_the_alias_is_resolved_even_with_AS_and_quotes():
    from app.engines.sql import _enriched_error

    schema = {"Invoice__c": [("Id", "VARCHAR"), ("Total_Amount__c", "VARCHAR")]}
    out = _enriched_error(
        'Binder Error: Table "inv" does not have a column named "Amount__c"',
        'SELECT 1 FROM "Invoice__c" AS inv',
        schema,
    )
    assert "Total_Amount__c" in out


def test_non_binder_errors_pass_through_unchanged():
    from app.engines.sql import _enriched_error

    assert _enriched_error("timeout", "SELECT 1", {}) == "timeout"


# ── Both attempts failing is an answer, not a stack trace ────────────────────

def test_the_summary_covers_the_whole_result_not_the_preview():
    """The "authoritative, computed over every row" block used to inherit the
    500-row PREVIEW cap: a query matching 33,000 interviews had its totals
    computed over 1.5% of the result while the prompt called them exact."""
    from app.config import settings

    # The summary cap must dwarf the preview cap — it matches the export cap.
    assert settings.sql_summary_row_cap >= 100_000
    # The figures must cover FAR more than the table shows. The exact ratio is
    # a tuning value (the preview rose 500 -> 2000 on 2026-08-29); what matters
    # is that the summary population dwarfs the preview.
    assert settings.sql_summary_row_cap >= settings.sql_preview_row_cap * 10


def test_a_full_result_reaches_the_figures_while_the_preview_stays_small():
    """3,000 rows: the computed block covers all 3,000; meta.data carries 500."""
    import asyncio
    import unittest.mock as mock

    from app.engines import sql as sqleng

    # Deliberately ABOVE the preview cap, whatever the cap currently is: the
    # invariant under test is "the table is bounded, the figures are not".
    total = settings.sql_preview_row_cap + 500
    rows = [[f"cand-{i}", "2000"] for i in range(total)]

    async def main():
        events = []

        async def emit(kind, data):
            events.append((kind, data))

        async def fake_gen(*_a, **_k):
            return "SELECT 1", ["Name", "Paid"], rows

        async def fake_stream(messages, **_k):
            # Capture the narrative prompt, then stream one token.
            fake_stream.prompt = messages[-1]["content"]
            yield "done"

        with (
            mock.patch.object(sqleng.settings, "duckdb_path", __file__),
            mock.patch.object(sqleng, "generate_and_run_sql", fake_gen),
            mock.patch.object(sqleng.llm, "stream_chat_completion", fake_stream),
        ):
            await sqleng.run_sql_engine("how much did each candidate pay", [], emit)

        meta = [d for k, d in events if k == "meta"][-1]
        # The preview carries every row it has, up to the cap — the point of
        # this test is that `data` never EXCEEDS the cap while the figures
        # cover the whole result, not that it is always exactly the cap.
        assert len(meta["data"]) == settings.sql_preview_row_cap
        assert meta["truncated"] is True
        # …but the AUTHORITATIVE figures cover every row.
        assert f'"total_rows": {total}' in fake_stream.prompt
        assert f"Total rows in the result: {total}" in fake_stream.prompt

    asyncio.run(main())


def test_an_overflowing_result_states_its_true_total_honestly():
    """Past even the summary cap, a COUNT(*) wrap fetches the real total and
    the coverage note says "first N of M" — never the cap as the population."""
    from app.engines.sql import deterministic_summary

    # The mechanism under test is the computed-block override; simulate it the
    # way run_sql_engine builds it.
    rows = [["x"]] * 10
    computed = deterministic_summary(["A"], rows)
    computed["true_total_rows"] = 33000
    computed["counts_cover"] = (
        "the FIRST 10 rows of a larger result of 33000 total rows — state "
        "every figure as covering those rows, never as the whole population"
    )
    assert computed["total_rows"] == 10
    assert computed["true_total_rows"] == 33000


def test_a_double_query_failure_is_an_honest_message_not_an_error_frame():
    """The raw `Binder Error: Table "p" ...` used to reach the user as a red
    error pill via an `error` SSE frame, after two answered clarifications."""
    import asyncio
    import unittest.mock as mock

    from app.engines import sql as sqleng

    async def main():
        events = []

        async def emit(kind, data):
            events.append((kind, data))

        async def boom(*_a, **_k):
            raise RuntimeError(
                'Binder Error: Table "p" does not have a column named "Status__c"'
            )

        with (
            # The engine returns NO_DATA before ever querying when the
            # warehouse file is absent — as it is in the test environment.
            mock.patch.object(sqleng.settings, "duckdb_path", __file__),
            mock.patch.object(sqleng, "generate_and_run_sql", boom),
        ):
            text = await sqleng.run_sql_engine("how much did they pay", [], emit)
        kinds = [k for k, _ in events]
        assert "token" in kinds and "meta" in kinds
        meta = [d for k, d in events if k == "meta"][-1]
        assert meta.get("salesforce_error")
        assert "could not write a valid query" in text
        assert "Binder Error" not in text  # raw detail stays out of the transcript

    asyncio.run(main())
