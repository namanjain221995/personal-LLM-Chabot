from evaluation.evaluators import evaluate_case


def _case(critical=None):
    return {
        "id": "SF-TEST-001",
        "expected": {
            "mode": "salesforce",
            "intent": "record_count",
            "entities": {
                "objects": {"required": ["Interview__c"], "forbidden": ["Candidate__c"]},
                "fields": {
                    "required": ["Interview_Outcome__c"],
                    "forbidden": ["Candidate_Status__c"],
                },
            },
            "filters": [
                {"field": "Interview_Outcome__c", "operator": "equals", "value": "Offer Received"}
            ],
            "source": {"type": "live_salesforce_records", "environment": "preprod"},
            "plan": {
                "operation": "count",
                "root_object": "Interview__c",
                "query_language": "SOQL_or_multi_query_plan",
                "filters": [
                    {"field": "Interview_Outcome__c", "operator": "equals", "value": "Offer Received"}
                ],
                "filter_logic": "AND",
                "group_by": [],
                "aggregate_fields": [],
            },
            "answer": {"type": "oracle_result"},
            "provenance": {
                "required": True,
                "expected_source_label": "live_salesforce_records",
                "expected_environment": "preprod",
                "must_indicate_freshness": True,
            },
        },
        "grading": {
            "critical_checks": critical or [
                "mode", "intent", "entities", "filters", "plan", "provenance"
            ]
        },
    }


def _trace():
    return {
        "resolved_mode": "salesforce",
        "intent": "record_count",
        "plan": {
            "result_mode": "count",
            "object_api_name": "Interview__c",
            "query_language": "SOQL",
            "filters": [
                {"field": "Interview_Outcome__c", "operator": "=", "value": "Offer Received"}
            ],
            "filter_logic": "and",
            "group_by": [],
            "aggregate_functions": [],
            "select_fields": ["Interview_Outcome__c"],
        },
        "provenance": {
            "source": "live_salesforce",
            "environment": "preprod",
            "freshness": "live",
        },
        "events": [],
    }


def test_all_implemented_deterministic_checks_pass():
    result = evaluate_case(_case(), _trace())
    assert result.passed is True
    assert result.first_incorrect_stage is None
    assert all(check.status == "passed" for check in result.checks)


def test_first_incorrect_stage_is_stable_pipeline_order():
    trace = _trace()
    trace["resolved_mode"] = "assistant"
    trace["plan"]["object_api_name"] = "Candidate__c"
    result = evaluate_case(_case(), trace)
    assert result.passed is False
    assert result.first_incorrect_stage == "mode"
    assert [check.stage for check in result.checks] == [
        "mode", "intent", "entities", "filters", "plan", "provenance"
    ]


def test_missing_categorical_intent_is_not_guessed():
    trace = _trace()
    trace.pop("intent")
    result = evaluate_case(_case(), trace)
    intent = next(check for check in result.checks if check.stage == "intent")
    assert intent.status == "not_evaluable"
    assert result.first_incorrect_stage == "intent"


def test_planner_normalized_prose_is_not_mistaken_for_categorical_intent():
    trace = _trace()
    trace.pop("intent")
    trace["events"] = [
        {
            "stage": "QUERY_PLAN_CREATED",
            "details": {
                "intent": {
                    "normalized_text": "count candidates with a received offer",
                    "router_action": "EXECUTE_QUERY",
                }
            },
        }
    ]
    result = evaluate_case(_case(), trace)
    intent = next(check for check in result.checks if check.stage == "intent")
    assert intent.status == "not_evaluable"


def test_explicit_categorical_intent_in_structured_event_is_scored():
    trace = _trace()
    trace.pop("intent")
    trace["events"] = [
        {"stage": "QUERY_PLAN_CREATED", "details": {"intent": {"name": "record_count"}}}
    ]
    result = evaluate_case(_case(), trace)
    assert next(check for check in result.checks if check.stage == "intent").status == "passed"


def test_wrong_business_definition_is_caught_in_entities_and_filters():
    trace = _trace()
    trace["plan"] = {
        "result_mode": "count",
        "object_api_name": "Candidate__c",
        "query_language": "SOQL",
        "filters": [{"field": "Candidate_Status__c", "operator": "=", "value": "Placed"}],
        "select_fields": ["Candidate_Status__c"],
    }
    result = evaluate_case(_case(), trace)
    assert result.first_incorrect_stage == "entities"
    assert next(c for c in result.checks if c.stage == "filters").status == "failed"
    assert next(c for c in result.checks if c.stage == "plan").status == "failed"


def test_unimplemented_critical_checks_cannot_inflate_end_to_end_accuracy():
    result = evaluate_case(
        _case(["mode", "intent", "entities", "filters", "source", "plan", "answer", "provenance"]),
        _trace(),
    )
    assert result.passed is False
    assert result.first_incorrect_stage == "source"
    assert next(c for c in result.checks if c.stage == "answer").status == "not_evaluable"


def test_missing_provenance_environment_fails_explicitly():
    trace = _trace()
    trace["provenance"].pop("environment")
    result = evaluate_case(_case(), trace)
    provenance = next(c for c in result.checks if c.stage == "provenance")
    assert provenance.status == "failed"
    assert "environment" in provenance.message


def test_query_execution_event_supplies_provenance():
    trace = _trace()
    trace.pop("provenance")
    trace["events"] = [
        {
            "stage": "QUERY_EXECUTED",
            "details": {
                "source": "live_salesforce",
                "environment": "preprod",
                "freshness": "live",
            },
        }
    ]
    result = evaluate_case(_case(), trace)
    assert next(check for check in result.checks if check.stage == "provenance").status == "passed"
