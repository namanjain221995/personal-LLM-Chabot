"""The query-plan compiler is the security boundary, so it is tested like one.

`engines/live_sf.py` lets the model write SOQL and guards the string afterwards.
This path never does: the model supplies an object, some field names, an
operator from an allowlist and a value, and every character of syntax is written
by `core/sf_intel/plan.py`. These tests are the proof of that claim — most of
them assert a REFUSAL, because the interesting behaviour here is what does not
get compiled.
"""
import pytest

from app.core.sf_intel.models import OrderBy, QueryFilter, SalesforceQueryPlan
from app.core.sf_intel.plan import (
    FieldInfo,
    ObjectSchema,
    PlanRejected,
    build_object_schema,
    calculate,
    compile_plan,
    escape_soql_string,
    normalize_date_operand,
)


def _fields(*specs):
    out = {}
    for spec in specs:
        name, ftype = spec if isinstance(spec, tuple) else (spec, "string")
        out[name.lower()] = FieldInfo(name=name, type=ftype)
    return out


OPPORTUNITY = ObjectSchema(
    name="Opportunity",
    label="Opportunity",
    fields={
        **_fields(
            "Id",
            ("Name", "string"),
            ("StageName", "picklist"),
            ("Amount", "currency"),
            ("CloseDate", "date"),
            ("IsWon", "boolean"),
            ("Probability", "percent"),
            ("Region__c", "picklist"),
        ),
        "accountid": FieldInfo(
            name="AccountId",
            type="reference",
            relationship_name="Account",
            reference_to=("Account",),
        ),
        "ownerid": FieldInfo(
            name="OwnerId",
            type="reference",
            relationship_name="Owner",
            reference_to=("User",),
        ),
    },
)

ACCOUNT = ObjectSchema(
    name="Account",
    fields={
        **_fields("Id", "Name", "Industry"),
        "ownerid": FieldInfo(
            name="OwnerId",
            type="reference",
            relationship_name="Owner",
            reference_to=("User",),
        ),
    },
)

USER = ObjectSchema(name="User", fields=_fields("Id", "Name", "Email"))

PARENTS = {"Account": ACCOUNT, "User": USER}


def _resolve(name):
    return PARENTS.get(name)


def _plan(**kwargs) -> SalesforceQueryPlan:
    kwargs.setdefault("object_api_name", "Opportunity")
    return SalesforceQueryPlan(**kwargs)


# ── The happy path ───────────────────────────────────────────────────────────

def test_a_clear_plan_compiles_to_the_query_it_describes():
    compiled = compile_plan(
        _plan(
            select_fields=["Name", "StageName", "Amount"],
            filters=[
                QueryFilter(field="IsWon", operator="eq", value="false"),
                QueryFilter(
                    field="CloseDate", operator="eq", value="THIS_QUARTER",
                    is_date_literal=True,
                ),
            ],
            order_by=[OrderBy(field="Amount", direction="desc")],
            limit=50,
        ),
        OPPORTUNITY,
        resolve_object=_resolve,
    )
    assert compiled.soql == (
        "SELECT Id, Name, StageName, Amount FROM Opportunity "
        "WHERE IsWon = false AND CloseDate = THIS_QUARTER "
        "ORDER BY Amount DESC LIMIT 50"
    )
    assert compiled.columns[0] == "Id"


def test_id_is_always_selected_so_live_rows_can_be_matched_against_local_ones():
    """core/salesforce.merge_rows keys on Id; a result without it cannot be
    deduplicated against the warehouse copy of the same record."""
    compiled = compile_plan(
        _plan(select_fields=["Name"]), OPPORTUNITY, resolve_object=_resolve
    )
    assert compiled.soql.startswith("SELECT Id, Name FROM Opportunity")


def test_a_parent_traversal_is_checked_against_the_parents_real_describe():
    compiled = compile_plan(
        _plan(select_fields=["Name"], relationship_paths=["Account.Industry"]),
        OPPORTUNITY,
        resolve_object=_resolve,
    )
    assert "Account.Industry" in compiled.soql


def test_a_two_hop_traversal_resolves_through_both_describes():
    compiled = compile_plan(
        _plan(select_fields=["Owner.Name"]), OPPORTUNITY, resolve_object=_resolve
    )
    assert "Owner.Name" in compiled.soql


def test_api_casing_is_taken_from_the_describe_not_from_the_model():
    """A plan that writes `stagename` must not produce a query that says so —
    SOQL tolerates it, but the returned column key would not match."""
    compiled = compile_plan(
        _plan(select_fields=["stagename"]), OPPORTUNITY, resolve_object=_resolve
    )
    assert "StageName" in compiled.soql
    assert "stagename" not in compiled.soql


# ── Refusals ─────────────────────────────────────────────────────────────────

def test_an_unknown_object_is_refused():
    with pytest.raises(PlanRejected, match="was supplied for a plan"):
        compile_plan(_plan(object_api_name="Invoice__c"), OPPORTUNITY)


def test_an_object_this_connection_cannot_query_is_refused():
    locked = ObjectSchema(name="Opportunity", queryable=False, fields=_fields("Id"))
    with pytest.raises(PlanRejected, match="not queryable"):
        compile_plan(_plan(select_fields=["Id"]), locked)


def test_an_unknown_field_is_refused():
    with pytest.raises(PlanRejected, match="is not a field on Opportunity"):
        compile_plan(
            _plan(select_fields=["SecretMargin__c"]),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


def test_a_field_the_connection_cannot_read_is_refused():
    guarded = ObjectSchema(
        name="Opportunity",
        fields={
            **_fields("Id"),
            "amount": FieldInfo(name="Amount", type="currency", readable=False),
        },
    )
    with pytest.raises(PlanRejected, match="not readable"):
        compile_plan(_plan(select_fields=["Amount"]), guarded)


def test_an_invented_relationship_is_refused():
    with pytest.raises(PlanRejected, match="not a relationship"):
        compile_plan(
            _plan(select_fields=["Parent.Name"]),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


def test_a_relationship_whose_parent_cannot_be_described_is_refused():
    """Better to refuse than to assume: an undescribable parent means the field
    on the far side was never checked against anything."""
    with pytest.raises(PlanRejected, match="cannot read the schema"):
        compile_plan(
            _plan(select_fields=["Account.Industry"]),
            OPPORTUNITY,
            resolve_object=lambda _name: None,
        )


def test_relationship_depth_is_bounded():
    with pytest.raises(PlanRejected, match="the limit is"):
        compile_plan(
            _plan(select_fields=["A.B.C.D.E"]),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


def test_an_operator_outside_the_allowlist_never_becomes_a_model():
    """Rejected by the CONTRACT, before any compiler sees it."""
    with pytest.raises(ValueError, match="unsupported filter operator"):
        QueryFilter(field="Amount", operator="regexp", value="x")


def test_like_is_refused_on_a_date_field():
    with pytest.raises(PlanRejected, match="LIKE is not supported"):
        compile_plan(
            _plan(filters=[QueryFilter(field="CloseDate", operator="like", value="x")]),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


def test_a_date_operand_that_is_not_a_real_literal_is_refused():
    """A bogus operand quoted as a string would MATCH NOTHING, silently — the
    worst outcome available, so it is refused instead."""
    with pytest.raises(PlanRejected, match="not a Salesforce date literal"):
        compile_plan(
            _plan(
                filters=[
                    QueryFilter(field="CloseDate", operator="eq", value="soonish")
                ]
            ),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


@pytest.mark.parametrize(
    "placeholder", ["CURRENT_USER_ID", "current_user", "me", "UserInfo.getUserId()"]
)
def test_a_placeholder_that_stands_for_the_asker_is_refused(placeholder):
    """Found live 2026-08-11: asked for "opportunities I own", the planner wrote
    `OwnerId = 'CURRENT_USER_ID'`. Quoted, that is a perfectly valid query that
    matches NOTHING — and reports zero with full confidence. There is also no
    honest substitute here: this connection authenticates as an integration
    user, not as the person asking."""
    with pytest.raises(PlanRejected, match="placeholder rather than a value"):
        compile_plan(
            _plan(
                filters=[
                    QueryFilter(field="Name", operator="eq", value=placeholder)
                ]
            ),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


def test_a_real_value_that_merely_contains_the_word_me_is_not_refused():
    compiled = compile_plan(
        _plan(filters=[QueryFilter(field="Name", operator="eq", value="Mercury Ltd")]),
        OPPORTUNITY,
        resolve_object=_resolve,
    )
    assert "'Mercury Ltd'" in compiled.soql


def test_a_non_numeric_operand_on_a_currency_field_is_refused():
    with pytest.raises(PlanRejected, match="is not a number"):
        compile_plan(
            _plan(filters=[QueryFilter(field="Amount", operator="gt", value="lots")]),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


def test_a_selected_field_outside_group_by_is_refused_rather_than_dropped():
    """Dropping it silently would answer a DIFFERENT question."""
    with pytest.raises(PlanRejected, match="not in GROUP BY"):
        compile_plan(
            _plan(
                select_fields=["Name"],
                aggregate_functions=["count"],
                group_by=["StageName"],
            ),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


def test_having_without_group_by_is_refused():
    with pytest.raises(PlanRejected, match="HAVING requires GROUP BY"):
        compile_plan(
            _plan(having=[QueryFilter(field="Amount", operator="gt", value="1")]),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


def test_select_and_filter_counts_are_bounded():
    with pytest.raises(PlanRejected, match="filters; the limit is"):
        compile_plan(
            _plan(
                filters=[
                    QueryFilter(field="Name", operator="eq", value=str(i))
                    for i in range(25)
                ]
            ),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


# ── Injection ────────────────────────────────────────────────────────────────

def test_a_quote_in_a_value_cannot_close_the_literal():
    compiled = compile_plan(
        _plan(
            filters=[
                QueryFilter(
                    field="Name", operator="eq", value="O'Brien' OR Id != null--"
                )
            ]
        ),
        OPPORTUNITY,
        resolve_object=_resolve,
    )
    assert "WHERE Name = 'O\\'Brien\\' OR Id != null--'" in compiled.soql
    # One WHERE, one predicate: nothing the value contained became syntax.
    assert compiled.soql.count("WHERE") == 1
    assert " OR " not in compiled.soql.replace("OR Id != null--'", "")


def test_a_backslash_is_escaped_before_the_quote_is():
    r"""Escaping the quote first turns \' into \\' — which CLOSES the literal.
    This ordering bug is the whole reason the function does it explicitly."""
    assert escape_soql_string("a\\'b") == "a\\\\\\'b"


def test_like_wildcards_in_user_text_are_matched_literally():
    """The % belongs to US. A search for "50% off" must not become a wildcard
    that matches every record."""
    compiled = compile_plan(
        _plan(filters=[QueryFilter(field="Name", operator="contains", value="50% off")]),
        OPPORTUNITY,
        resolve_object=_resolve,
    )
    assert r"LIKE '%50\% off%'" in compiled.soql


def test_model_authored_soql_cannot_reach_the_org_through_a_field_name():
    """The classic bypass attempt: put the query in the field name."""
    with pytest.raises(PlanRejected):
        compile_plan(
            _plan(select_fields=["Id FROM Opportunity WHERE Id != null--"]),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


def test_an_object_name_containing_syntax_is_refused():
    with pytest.raises(PlanRejected):
        compile_plan(
            _plan(object_api_name="Opportunity WHERE 1=1"),
            OPPORTUNITY,
            resolve_object=_resolve,
        )


# ── SOQL's own rules ─────────────────────────────────────────────────────────

def test_an_ungrouped_aggregate_gets_no_limit():
    """Salesforce: "Non-grouped query that uses overall aggregate functions
    cannot also use LIMIT" — found live on 2026-08-06."""
    compiled = compile_plan(
        _plan(aggregate_functions=["count"], result_mode="count"),
        OPPORTUNITY,
        resolve_object=_resolve,
    )
    assert compiled.soql == "SELECT COUNT(Id) FROM Opportunity"
    assert "LIMIT" not in compiled.soql


def test_a_grouped_aggregate_keeps_its_limit_because_rows_are_per_group():
    compiled = compile_plan(
        _plan(
            aggregate_functions=["sum(Amount)"],
            group_by=["StageName"],
            result_mode="aggregate",
            limit=25,
        ),
        OPPORTUNITY,
        resolve_object=_resolve,
    )
    assert compiled.soql == (
        "SELECT StageName, SUM(Amount) FROM Opportunity "
        "GROUP BY StageName LIMIT 25"
    )


def test_the_limit_is_capped_whatever_the_plan_asked_for():
    compiled = compile_plan(
        _plan(select_fields=["Name"], limit=2000),
        OPPORTUNITY,
        resolve_object=_resolve,
        limit_cap=200,
    )
    assert compiled.soql.endswith("LIMIT 200")


def test_result_mode_count_supplies_its_own_aggregate():
    compiled = compile_plan(
        _plan(result_mode="count"), OPPORTUNITY, resolve_object=_resolve
    )
    assert "COUNT(Id)" in compiled.soql


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("today", "TODAY"),
        ("THIS_QUARTER", "THIS_QUARTER"),
        ("last_n_days:30", "LAST_N_DAYS:30"),
        ("2026-08-11", "2026-08-11"),
        ("2026-08-11T09:00:00Z", "2026-08-11T09:00:00Z"),
    ],
)
def test_real_date_operands_are_normalized(raw, expected):
    assert normalize_date_operand(raw) == expected


@pytest.mark.parametrize("raw", ["soon", "LAST_N_DAYS", "2026-13-45x", "'; DROP"])
def test_bogus_date_operands_are_refused(raw):
    with pytest.raises(PlanRejected):
        normalize_date_operand(raw)


# ── Describe adaptation ──────────────────────────────────────────────────────

def test_a_describe_payload_becomes_a_usable_schema():
    schema = build_object_schema(
        {
            "name": "Case",
            "label": "Case",
            "queryable": True,
            "fields": [
                {"name": "Id", "type": "id", "label": "Case ID"},
                {
                    "name": "AccountId",
                    "type": "reference",
                    "relationshipName": "Account",
                    "referenceTo": ["Account"],
                },
            ],
        }
    )
    assert schema.name == "Case"
    assert schema.field_named("id") is not None
    assert schema.relationship("Account").reference_to == ("Account",)


def test_a_describe_without_the_new_keys_still_yields_readable_fields():
    """The trimmed shape core/salesforce.describe_object returned before
    2026-08-11 must keep working — a describe cache can outlive a deploy."""
    schema = build_object_schema(
        {"name": "Task", "fields": [{"name": "Subject", "type": "string"}]}
    )
    assert schema.field_named("subject").readable is True


# ── Deterministic calculation ────────────────────────────────────────────────

ROWS = [
    {"Id": "1", "StageName": "Won", "Amount": "100"},
    {"Id": "2", "StageName": "Won", "Amount": "300"},
    {"Id": "3", "StageName": "Lost", "Amount": "200"},
]


def test_counts_and_shares_are_computed_in_code():
    result = calculate(ROWS, numeric_fields=["Amount"], group_by="StageName")
    assert result["record_count"] == 3
    assert result["totals"]["Amount"]["sum"] == 600.0
    assert result["totals"]["Amount"]["average"] == 200.0
    won = next(g for g in result["groups"] if g["value"] == "Won")
    assert won["count"] == 2
    assert won["share_percent"] == pytest.approx(66.67, abs=0.01)


def test_the_true_total_wins_over_the_page_size():
    """A summary that quotes the page size as the total is the single most
    common way a data answer becomes wrong — 314 rows reported as 29."""
    result = calculate(ROWS, total_records=314)
    assert result["record_count"] == 314
    assert result["rows_examined"] == 3


def test_a_non_numeric_column_is_not_totalled():
    result = calculate(ROWS, numeric_fields=["StageName"])
    assert "totals" not in result


def test_group_shares_always_total_one_hundred():
    result = calculate(ROWS, group_by="StageName")
    assert sum(g["share_percent"] for g in result["groups"]) == pytest.approx(100.0)


# ── Deterministic figures for the WAREHOUSE path ─────────────────────────────
# Owner report, 2026-08-11: asked for slot 128's mocks, the answer said
# "Total Mocks: 3, Cleared: 2, Failed: 0, Pass Ratio: 0.67" — three statements
# that cannot all be true. They were read off the 30 sample rows the model is
# shown. The prompt had always forbidden that; an instruction is not a
# mechanism, so the figures are now computed in code over EVERY row.

from app.engines.sql import deterministic_summary


def test_value_counts_are_exact_over_every_row_not_the_sample():
    columns = ["Name", "Outcome"]
    rows = [[f"c{i}", "Cleared" if i % 3 else "Failed"] for i in range(60)]
    summary = deterministic_summary(columns, rows)
    assert summary["total_rows"] == 60
    outcomes = {
        v["value"]: v["count"] for v in summary["value_counts"]["Outcome"]["values"]
    }
    assert outcomes == {"Cleared": 40, "Failed": 20}
    assert sum(outcomes.values()) == 60


def test_every_percentage_carries_the_denominator_it_was_taken_from():
    """"Pass Ratio: 0.67" next to "Cleared: 2, Failed: 0" is unanswerable —
    the reader cannot tell what the 0.67 is a proportion of."""
    summary = deterministic_summary(
        ["Outcome"], [["Cleared"], ["Cleared"], ["Failed"]]
    )
    block = summary["value_counts"]["Outcome"]
    assert block["denominator"] == 3
    shares = {v["value"]: v["percent_of_non_empty"] for v in block["values"]}
    assert shares["Cleared"] == pytest.approx(66.67, abs=0.01)
    assert sum(shares.values()) == pytest.approx(100.0)


def test_blanks_are_reported_rather_than_folded_into_a_category():
    summary = deterministic_summary(
        ["Outcome"], [["Cleared"], [None], [""], ["Failed"]]
    )
    block = summary["value_counts"]["Outcome"]
    assert block["denominator"] == 2
    assert block["empty_or_null"] == 2


def test_numeric_columns_are_totalled_over_every_row():
    summary = deterministic_summary(
        ["Amount"], [["100"], ["300"], ["200"]]
    )
    assert summary["numeric_totals"]["Amount"]["sum"] == 600.0
    assert summary["numeric_totals"]["Amount"]["average"] == 200.0
    assert "value_counts" not in summary


def test_a_high_cardinality_column_is_not_turned_into_noise():
    summary = deterministic_summary(
        ["Id"], [[f"rec-{i}"] for i in range(200)]
    )
    assert summary["total_rows"] == 200
    assert "Id" not in summary.get("value_counts", {})


def test_an_empty_result_still_reports_an_honest_zero():
    summary = deterministic_summary(["Name"], [])
    assert summary["total_rows"] == 0
    assert "value_counts" not in summary


def test_the_narrative_prompt_makes_the_computed_block_authoritative():
    from app.engines.sql import _narrative_messages

    messages = _narrative_messages(
        "how many cleared?", ["Outcome"], [["Cleared"]], [],
        total_rows=18,
        computed=deterministic_summary(["Outcome"], [["Cleared"], ["Failed"]]),
    )
    system = messages[0]["content"]
    user = messages[-1]["content"]
    assert "MUST be taken from the 'Computed figures' block" in system
    assert "Do not count, add up, or work out a proportion from the sample" in system
    assert "AUTHORITATIVE" in user
    assert "illustration only" in user


def test_the_prompt_still_refuses_to_call_the_synced_copy_live():
    from app.engines.sql import _narrative_messages

    system = _narrative_messages("q", ["a"], [["b"]], [])[0]["content"]
    assert "LOCAL SYNCED COPY" in system
    assert "Never say the result is live" in system


# ── One-row aggregates: the value is the answer, not the row count ───────────
# `SELECT count(*) ... WHERE Status__c = 'Locked'` returned [[866]] and the
# summary published "total_rows: 1" as an authoritative figure next to
# "sum: 866" — the narrative model quoted the 1 (sf_intel did, 2026-08-17) or
# reasoned aloud about the contradiction. The summary now states the aggregate
# meaning itself.


def test_a_one_row_count_promotes_the_value_to_record_count():
    summary = deterministic_summary(["count(*)"], [[866]])
    assert summary["record_count"] == 866
    assert summary["aggregate_result"] == {"count(*)": 866.0}
    assert "not of records" in summary["counts_cover"]


def test_a_one_row_sum_states_the_aggregate_without_inventing_a_count():
    summary = deterministic_summary(["total_amount"], [[123456.78]])
    assert summary["aggregate_result"] == {"total_amount": 123456.78}
    assert "record_count" not in summary


def test_a_single_data_row_with_text_is_not_mistaken_for_an_aggregate():
    summary = deterministic_summary(["Name", "Amount"], [["Acme", 500]])
    assert "aggregate_result" not in summary
    assert summary["total_rows"] == 1


def test_grouped_results_keep_their_row_semantics():
    summary = deterministic_summary(
        ["Status__c", "n"], [["Locked", 866], ["Active", 107]]
    )
    assert "aggregate_result" not in summary
    assert summary["total_rows"] == 2


def test_a_grouped_count_result_pairs_each_label_with_its_value():
    """"Completed 72 / Voided 33" was summarized as "Completed: 1 (50%)" —
    occurrence counts of unique labels quoted as real counts."""
    summary = deterministic_summary(
        ["dfsle__Status__c", "count"], [["Completed", 72], ["Voided", 33]]
    )
    assert summary["row_breakdown"] == {"Completed": 72, "Voided": 33}
    assert "value_counts" not in summary
    assert "sum across rows is 105" in summary["counts_cover"]


def test_repeated_labels_keep_the_normal_value_counts_profile():
    """Real record rows (statuses repeat) are NOT a grouped result."""
    summary = deterministic_summary(
        ["Status__c", "Amount"], [["Open", 10], ["Open", 20], ["Closed", 5]]
    )
    assert "row_breakdown" not in summary
    assert "value_counts" in summary


# ── ID fields take Ids, not names ────────────────────────────────────────────
# Live failure (2026-08-18): after TWO answered clarifications, the planner
# emitted `RecordTypeId != 'Internal_Interview__c'` — an object NAME quoted
# into an ID comparison. It compiled cleanly here and Salesforce rejected it
# at runtime ("invalid ID field"), ending the request with a raw SOQL error.

def test_an_id_field_compared_to_a_name_is_rejected_at_compile_time():
    plan = SalesforceQueryPlan(
        object_api_name="Opportunity",
        select_fields=["Id"],
        filters=[QueryFilter(field="OwnerId", operator="ne",
                             value="Internal_Interview__c")],
    )
    with pytest.raises(PlanRejected) as err:
        compile_plan(plan, OPPORTUNITY, resolve_object=_resolve)
    # The message teaches the repair — it is fed back verbatim on the retry.
    assert "ID field" in str(err.value)
    assert "RecordType.Name" in str(err.value)


def test_a_real_id_still_passes_an_id_field():
    plan = SalesforceQueryPlan(
        object_api_name="Opportunity",
        select_fields=["Id"],
        filters=[QueryFilter(field="OwnerId", operator="eq",
                             value="005Ps000001abcdIAA")],
    )
    compiled = compile_plan(plan, OPPORTUNITY, resolve_object=_resolve)
    assert "OwnerId = '005Ps000001abcdIAA'" in compiled.soql


def test_the_record_type_idiom_compiles_through_the_relationship():
    """What the planner is told to write instead: RecordType.Name (a dotted
    path through the lookup) — same machinery as Account.Name below."""
    plan = SalesforceQueryPlan(
        object_api_name="Opportunity",
        select_fields=["Id"],
        filters=[QueryFilter(field="Account.Name", operator="eq", value="Acme")],
    )
    compiled = compile_plan(plan, OPPORTUNITY, resolve_object=_resolve)
    assert "Account.Name = 'Acme'" in compiled.soql
