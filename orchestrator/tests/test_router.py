

def test_a_named_persons_records_route_to_sql():
    """"give me details for <person>'s training" went to rag, which searched
    record TEXT and answered "no training details" for a candidate with five
    enrolments. Records are fields, and fields are sql."""
    from app.engines import router

    assert "any request for the RECORDS of a named person" in router._SYSTEM
    shots = dict(router.FEW_SHOTS)
    assert shots["give me details for Rakshith Bodakuntla's training"] == "sql"
    assert shots["show me everything about Priya Sharma"] == "sql"


def test_free_text_about_a_named_person_still_routes_to_rag():
    """The fix must not swallow the rag case: what someone SAID is still rag."""
    from app.engines import router

    shots = dict(router.FEW_SHOTS)
    assert shots["what did the client say in their feedback about Priya Sharma?"] == "rag"
