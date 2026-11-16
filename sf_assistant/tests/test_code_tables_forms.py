"""Step-3 fixes T1, T3 and T5 on made-up data: the written forms of a typed copy, the Total line left of a typed block, the rows that come back when a check cuts a typed line, the label line before a block that opens the answer, the new counters and the kinds of a draft message."""
import pytest
from sf_assistant.agent import tables as T
from sf_assistant.agent.nudges import _lead
from sf_assistant.engine.types import QueryEvidence
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
LEAD = "36 orders in all (`Gadget_Order__c`)."

def ev(qid, tool, result, obj="Gadget_Order__c", kind="data"):
    return QueryEvidence(query_id=qid, tool=tool, kind=kind, object=obj, arguments={}, ok=True, error=None, applied={},
                         notes=[], statements=[], tables=[obj], synced_at={obj: "2026-10-13 14:30 UTC"}, summary="",
                         model_result={"query_id": qid, "object": obj, **result}, elapsed_ms=1.0, error_code=None)

TREND = ev("q1", "trend_records", {"grain": "month", "date_field": "Ordered_On__c", "buckets": [
    {"bucket": "2031-01", "value": 12, "by": {"Web": 7, "Store": 5}},
    {"bucket": "2031-02", "value": 9, "by": {"Web": 4, "Store": 5}},
    {"bucket": "2031-03", "value": 15, "by": {"Web": 10, "Store": 5}}], "total": 36,
    "split_totals": {"Web": 21, "Store": 15}})
TREND_LINES = ["- 2031-01: 12 (Web: 7, Store: 5)", "- 2031-02: 9 (Web: 4, Store: 5)", "- 2031-03: 15 (Web: 10, Store: 5)"]
TOTAL = "- Total: 36 (Web: 21, Store: 15)"
BLOCK = "\n".join(TREND_LINES + [TOTAL])
MONTHS = "- **January 2031**: 12 orders (Web 7, Store 5)\n- **Feb 2031**: 9 orders (Web 4, Store 5)\n- **March 2031**: 15 orders (Web 10, Store 5)"

def test_month_names_type_rows_and_the_total_line_stays():
    x = T.expand(LEAD + "\n\n" + MONTHS + "\n\n{{table q1}}\n\n" + SOURCE, [TREND])
    assert x.counts["dropped"] == {"typed": 1} and x.counts["built"] == 0 and x.counts["items"] == []
    assert x.text == LEAD + "\n\n" + MONTHS + "\n\n" + TOTAL + "\n\n" + SOURCE
    assert x.blocks == {"{{table q1}}": TOTAL}
    x = T.expand(LEAD + "\n\n{{table q1}}\n\n" + MONTHS + "\n\n" + SOURCE, [TREND])
    assert x.text == LEAD + "\n\n" + MONTHS + "\n\n" + TOTAL + "\n\n" + SOURCE
    typed = MONTHS + "\n- **Web**: 21\n- **Store**: 15"
    x = T.expand(LEAD + "\n\n" + typed + "\n\n{{table q1}}\n\n" + SOURCE, [TREND])
    assert x.blocks == {} and x.text == LEAD + "\n\n" + typed + "\n\n" + SOURCE and x.counts["dropped"] == {"typed": 1}

def test_a_total_line_left_after_another_block_has_its_label():
    tiers = ev("q2", "group_records", {"grouped_by": "Tier__c", "measure": "count", "groups": [
        {"group": "Gold", "value": 3}, {"group": "Silver", "value": 2}], "total_across_groups": 5})
    x = T.expand(LEAD + "\n\n" + MONTHS + "\n\n{{table q1}}\n\n{{table q2}}\n\n" + SOURCE, [TREND, tiers])
    assert "- Total: 5\n\nBy month:\n" + TOTAL + "\n\n" + SOURCE in x.text
    assert x.counts["built"] == 1 and x.counts["dropped"] == {"typed": 1}

def test_a_repeated_table_line_keeps_one_total_line():
    x = T.expand(LEAD + "\n\n" + MONTHS + "\n\n{{table q1}}\n\n{{table q1}}\n\n" + SOURCE, [TREND])
    assert x.text.count(TOTAL) == 1 and x.counts["dropped"] == {"typed": 2}

def test_a_bare_month_types_nothing():
    may = ev("q1", "trend_records", {"grain": "month", "buckets": [{"bucket": "2031-05", "value": 9}, {"bucket": "2031-06", "value": 3}], "total": 12})
    for line in ("- May: 9\n- June: 3", "- Orders may rise: 9\n- June: 3"):
        x = T.expand(LEAD + "\n\n" + line + "\n\n{{table q1}}\n\n" + SOURCE, [may])
        assert x.counts["built"] == 1 and x.counts["partly_typed"] == 0 and x.counts["dropped"] == {}, line
    x = T.expand(LEAD + "\n\n- **April-May 2031**: 9\n\n{{table q1}}\n\n" + SOURCE, [may])
    assert x.counts["built"] == 1

def test_the_new_key_forms_count_only_first_on_the_line():
    block = "- Gold: 5\n- Silver: 3\n- (blank): 2"
    assert T._typed(block, ["- Gold: 5", "- Silver: 3", "- Top 2 tiers only; blank tier left out"]) == (2, 3)
    assert T._typed(block, ["- Gold: 5 (Region not set on 1)", "- Silver: 3", "- Blank: 2"]) == (3, 3)
    month = "- 2031-05: 12\n- 2031-06: 3"
    assert T._typed(month, ["- Zed Quill joined 12 May 2031", "- Ana Vex joined 3 Jun 2031"]) == (0, 2)
    assert T._typed(month, ["- Compared with May 2031, 12 fewer", "- Compared with Jun 2031, 3 more"]) == (0, 2)
    assert T._typed(month, ["- **May 2031:** 12", "| Jun 2031 | 3 |"]) == (2, 2)

def test_dates_and_times_are_no_values():
    assert T._typed("- Open: 5\n- Closed: 12", ["- Open since 2031-05-01", "- Closed since 2031-12-01"]) == (0, 2)
    assert T._typed("- 2031-05: 1\n- 2031-06: 3", ["- 2031-05-01: first hire", "- 2031-06-03: second hire"]) == (0, 2)
    assert T._typed("- Open: 5\n- Closed: 30", ["- Open at 5:30", "- Closed at 6:30"]) == (0, 2)
    table = "| Name | Joined | Round | Score |\n|---|---|---|---|\n| R1 | 2031-05-12 | 5 | 12 |\n| R2 | 2031-06-01 | 6 | 1 |"
    assert T._typed(table, ["- Joined 2031-05-12", "- Joined 2031-06-01"]) == (0, 2)
    # a date cell is still a value, as a whole word
    assert T._typed("| Name | Joined |\n|---|---|\n| R1 | 2031-05-12 |\n| R2 | 2031-06-01 |",
                    ["- R1: 2031-05-12", "- R2: 2031-06-01"]) == (2, 2)
    # a year-month, a month and year or a record code is no value either: 5 and 12 would be nowhere in the answer
    rows = "- Open: 5\n- Closed: 12"
    assert T._typed(rows, ["- Open since 2031-05", "- Closed since 2031-12"]) == (0, 2)
    assert T._typed(rows, ["- Open since 05/2031", "- Closed since 12/2031"]) == (0, 2)
    assert T._typed(rows, ["- Open: latest GO-000005", "- Closed: latest GO-000012"]) == (0, 2)
    assert T._typed(rows, ["- Open: 5 jobs", "- Closed: $12.00"]) == (2, 2)
    listed = "| Name | Joined | Score |\n|---|---|---|\n| R1 | 2031-05-12 | 5 |\n| R2 | 2031-06-01 | 6 |"
    assert T._typed(listed, ["- R1 joined in 2031-05", "- R2 joined in 2031-06"]) == (0, 2)
    wide = "| N | A | B | C |\n|---|---|---|---|\n| R1 | 5 | 6 | 7 |\n| R2 | 15 | 16 | 17 |"
    assert T._whole_row(T._block_rows(wide), "- GO-000005 GO-000006 GO-000007") is None
    split = "- A: 11 (Web: 4, Store: 7)\n- B: 12 (Web: 5, Store: 7)\n- Total: 23 (Web: 9, Store: 14)"
    assert T._total_left(split, ["- Web latest GO-000009", "- Store latest GO-000014"]) == "- Total: 23 (Web: 9, Store: 14)"
    assert T._total_left(split, ["- Web: 9", "- Store: 14"]) is None

def test_currency_and_blank():
    sums = ev("q1", "group_records", {"grouped_by": "Tier__c", "measure": "sum", "groups": [
        {"group": None, "value": 1520.0}, {"group": "Gold", "value": 980.5}]})
    typed = "- **Blank (no tier):** $1,520.00\n- **Gold:** $980.50"
    x = T.expand(LEAD + "\n\n" + typed + "\n\n{{table q1}}\n\n" + SOURCE, [sums])
    assert x.counts["dropped"] == {"typed": 1} and x.text == LEAD + "\n\n" + typed + "\n\n" + SOURCE
    for word in ("no value", "not set", "1,520"):
        t = f"- {word}: 1,520\n- Gold: 980.5" if word != "1,520" else "- Gold: 980.5\n- Other: 1,520"
        x = T.expand(LEAD + "\n\n" + t + "\n\n{{table q1}}\n\n" + SOURCE, [sums])
        assert (x.counts["dropped"] == {"typed": 1}) == (word != "1,520"), word
    x = T.expand(LEAD + "\n\n- Blank: $1,521.00\n- Gold: 980.5\n\n{{table q1}}\n\n" + SOURCE, [sums])
    assert x.counts["built"] == 1 and x.counts["partly_typed"] == 1
    # rounded is another number, a negative value needs its sign
    assert T._typed("- A: 1234.4\n- B: 12.5", ["- A: $1,234", "- B: $13"]) == (0, 2)
    assert T._typed("- A: -5\n- B: -7", ["- A: 5", "- B: 7"]) == (0, 2)
    assert T._typed("- A: -5\n- B: -7", ["- A: -5", "- B: -7"]) == (2, 2)

def test_pivot():
    every = "| Month | Web | Store |\n|---|---|---|\n| January 2031 | 7 | 5 |\n| February 2031 | 4 | 5 |\n| March 2031 | 10 | 5 |"
    x = T.expand(LEAD + "\n\n" + every + "\n\n{{table q1}}\n\n" + SOURCE, [TREND])
    assert x.counts["dropped"] == {"typed": 1}
    half = "| Month | Web |\n|---|---|\n| January 2031 | 7 |\n| February 2031 | 4 |\n| March 2031 | 10 |"
    x = T.expand(LEAD + "\n\n" + half + "\n\n{{table q1}}\n\n" + SOURCE, [TREND])
    assert x.counts["built"] == 1 and x.counts["partly_typed"] == 0 and BLOCK in x.text
    same = ev("q1", "trend_records", {"grain": "month", "buckets": [
        {"bucket": "2031-01", "value": 24, "by": {"Web": 12, "Store": 12}},
        {"bucket": "2031-02", "value": 24, "by": {"Web": 12, "Store": 12}}], "total": 48, "split_totals": {"Web": 24, "Store": 24}})
    one = "| Month | Web |\n|---|---|\n| January 2031 | 12 |\n| February 2031 | 12 |"
    assert T.expand(LEAD + "\n\n" + one + "\n\n{{table q1}}\n\n" + SOURCE, [same]).counts["built"] == 1
    two = "| Month | Web | Store |\n|---|---|---|\n| January 2031 | 12 | 12 |\n| February 2031 | 12 | 12 |"
    assert T.expand(LEAD + "\n\n" + two + "\n\n{{table q1}}\n\n" + SOURCE, [same]).counts["dropped"] == {"typed": 1}

def test_a_row_with_a_split_needs_its_whole_split():
    block = "- 2031-05: 30 (Web: 12, Store: 18)\n- 2031-06: 9 (Web: 4, Store: 5)\n- Total: 39 (Web: 16, Store: 23)"
    assert T._typed(block, ["- May 2031: 30", "- June 2031: 9"]) == (0, 2)   # totals only: the split would be lost
    assert T._typed(block.replace("2031-05", "Done").replace("2031-06", "Open"), ["- Done: 30", "- Open: 9"]) == (0, 2)
    assert T._typed("- 2031-05: 30 (Web: 12)\n- 2031-06: 9 (Web: 4)", ["- May 2031: 12 Web", "- June 2031: 4 Web"]) == (0, 2)
    assert T._typed(block, ["- May 2031: 30 (Web 12, Store 18)", "- June 2031: 9 (Web 4, Store 5)"]) == (2, 2)
    assert T._typed(block, ["| May 2031 | 12 | 18 |", "| June 2031 | 4 | 5 |"]) == (2, 2)   # adds up to the row
    top = "- 2031-05: 90 (Web: 12, Store: 18)\n- 2031-06: 50 (Web: 4, Store: 5)"   # a top-10 split that does not add up
    assert T._typed(top, ["| May 2031 | 12 | 18 |", "| June 2031 | 4 | 5 |"]) == (0, 2)
    assert T._typed(top, ["- May 2031: 90 (12, 18)", "- June 2031: 50 (4, 5)"]) == (2, 2)
    assert T._typed("- 2031-05: 0.3 (W: 0.1, S: 0.21)\n- 2031-06: 2 (W: 1, S: 1)", ["| May 2031 | 0.1 | 0.21 |", "| Jun 2031 | 1 | 1 |"]) == (2, 2)
    # a split of one value equal to the row value: the number alone may be only the total, and the label would go
    one = "- Alpha: 7 (Web: 7)\n- Beta: 9 (Store: 9)"
    assert T._typed(one, ["- Alpha: 7", "- Beta: 9"]) == (0, 2)
    assert T._typed(one, ["- Alpha: 7 Web", "- Beta: 9 Store"]) == (2, 2)
    assert T._typed(one, ["- Alpha: 7 (Web 7)", "- Beta: 9 (Store 9)"]) == (2, 2)
    assert T._typed(one, ["- Alpha: 7 of 7", "- Beta: 9 of 9"]) == (2, 2)
    assert T._typed("- 2031-01: 7 (Web: 7)\n- 2031-02: 9 (Store: 9)\n- Total: 16 (Web: 7, Store: 9)", ["- Jan 2031: 7", "- Feb 2031: 9"]) == (0, 2)


def test_the_key_itself_is_no_value():
    assert T._typed("- 2031-05: 5\n- 2031-06: 6", ["- 2031-05: none recorded", "- 2031-06: none recorded"]) == (0, 2)
    assert T._typed("- Tier 5: 5\n- Tier 6: 6", ["- Tier 5: none recorded", "- Tier 6: none recorded"]) == (0, 2)
    assert T._typed("| Status | Status 2 |\n|---|---|\n| Open | Open |\n| Done | Done |", ["- Open", "- Done"]) == (0, 2)
    assert T._typed("| Job |\n|---|\n| J1 |\n| J2 |", ["- J1", "- J2"]) == (2, 2)   # a key-only row: its key types it


def test_null_values_and_a_key_that_is_no_month_never_raise():
    with_null = "- 2031-05: 5 (Web: 5, Store: (no value))\n- 2031-06: 9 (Web: 9, Store: (no value))"
    assert T._typed(with_null, ["- May 2031: 5 (Web 5)", "- June 2031: 9 (Web 9)"]) == (2, 2)
    no_value = "- 2031-05: (no value) (Web: 12, Store: 18)\n- 2031-06: (no value) (Web: 4, Store: 5)"
    assert T._typed(no_value, ["| May 2031 | 12 | 18 |", "| June 2031 | 4 | 5 |"]) == (0, 2)
    assert T._typed("- 2024-25: 5\n- 2025-26: 3", ["- 2024-25: 5", "- 2025-26: 3"]) == (2, 2)


def test_reading_a_split():
    partial = "- 2031-05: 5 (Web: 3, Store: 2) (partial)\n- 2031-06: 9 (Web: 4, Store: 5) (partial)"
    assert T._typed(partial, ["- May 2031: 5 (Web 3)", "- June 2031: 9 (Web 4)"]) == (0, 2)
    assert T._typed(partial, ["- May 2031: 5 (Web 3, Store 2)", "- June 2031: 9 (Web 4, Store 5)"]) == (2, 2)
    assert T._typed("- 2031-05: 5 (Web: -3, Store: 8)\n- 2031-06: 9 (Web: -1, Store: 10)", ["- May 2031: 5 (Store 8)", "- June 2031: 9 (Store 10)"]) == (0, 2)
    assert T._typed("- 2031-05: 5 (Full, Time: 3, A: B: 2)\n- 2031-06: 9 (Full, Time: 4, A: B: 5)", ["- May 2031: 5 (3)", "- June 2031: 9 (4)"]) == (0, 2)
    # just over the tolerance: the row value is needed
    assert T._typed("- 2031-05: 30 (Web: 12, Store: 18.1)\n- 2031-06: 9 (Web: 4, Store: 5)", ["| May 2031 | 12 | 18.1 |", "| June 2031 | 4 | 5 |"]) == (1, 2)


def test_the_total_line_rule_in_detail():
    assert T._total_left("- Total: 4 (Web: 4)\n- Other: 3\n- Total: 7 (Web: 7)", ["- Web: 7"]) is None   # the last Total line
    assert T._total_left(BLOCK, ["- 21 online, 15 in shops"]) == TOTAL   # the value without its label
    assert T._total_left(BLOCK, ["- Web launched 2031-05-21", "- Store opened 2031-06-15"]) == TOTAL   # the day of a date
    null = "- Total: 5 (Web: 5, Store: (no value))"
    assert T._total_left("- 2031-05: 5 (Web: 5)\n" + null, ["- Web: 5"]) == null
    # all the model's own lines count, not only its list lines, on both sides
    prose = LEAD + "\n\n" + MONTHS + "\n\nWeb had 21 and Store had 15 overall.\n\n{{table q1}}\n\n" + SOURCE
    assert TOTAL not in T.expand(prose, [TREND]).text
    assert TOTAL not in T.restore(prose, {"{{table q1}}": BLOCK})[0]

LIST = ev("q1", "list_records", {"columns": ["Name", "Status__c", "Owner__c (name)", "Due_Date__c"], "rows": [
    {"Name": "GO-0001 Quill trial kit", "Status__c": "Open", "Owner__c (name)": "Zed Quill", "Due_Date__c": "2031-02-01"},
    {"Name": "GO-0002 Beta refill", "Status__c": "Open", "Owner__c (name)": "Zed Quill", "Due_Date__c": "2031-02-09"}], "total_count": 2, "returned": 2})

def test_list_rows_keyed_on_another_column():
    b = "- Due 2031-02-01: Open, Zed Quill\n- Due 2031-02-09: Open, Zed Quill"
    x = T.expand(LEAD + "\n\n" + b + "\n\n{{table q1}}\n\n" + SOURCE, [LIST])
    assert x.counts["dropped"] == {"typed": 1} and x.text == LEAD + "\n\n" + b + "\n\n" + SOURCE
    shared = "- Open, owned by Zed Quill\n- Open, owned by Zed Quill"
    x = T.expand(LEAD + "\n\n" + shared + "\n\n{{table q1}}\n\n" + SOURCE, [LIST])
    assert x.counts["built"] == 1 and x.counts["partly_typed"] == 0 and "| GO-0001 Quill trial kit |" in x.text
    both = "- GO-0001 Quill trial kit and GO-0002 Beta refill: Open, Zed Quill"
    x = T.expand(LEAD + "\n\n" + both + "\n\n{{table q1}}\n\n" + SOURCE, [LIST])
    assert x.counts["built"] == 1 and x.counts["partly_typed"] == 0
    two_cells = "- Due 2031-02-01, Open\n- Due 2031-02-09, Open"
    assert T.expand(LEAD + "\n\n" + two_cells + "\n\n{{table q1}}\n\n" + SOURCE, [LIST]).counts["built"] == 1
    one_cell = "- Due 2031-02-01\n- Due 2031-02-09"
    assert T.expand(LEAD + "\n\n" + one_cell + "\n\n{{table q1}}\n\n" + SOURCE, [LIST]).counts["built"] == 1

def test_restore_takes_the_same_decision():
    blocks = {"{{table q1}}": BLOCK}
    merged = LEAD + "\n\n{{table q1}}\n\n" + MONTHS + "\n\n" + SOURCE
    assert T.restore(merged, blocks) == (LEAD + "\n\n" + MONTHS + "\n\n" + TOTAL + "\n\n" + SOURCE,
                                         {"lines": 0, "lost": 0, "retyped": 1, "repeated": 0, "partly_retyped": 0})
    assert T.restore(LEAD + "\n\n" + MONTHS + "\n\n" + SOURCE, blocks)[0] == LEAD + "\n\n" + MONTHS + "\n\n" + TOTAL + "\n\n" + SOURCE
    half = LEAD + "\n- **January 2031**: 12 (Web 7, Store 5)\n\n{{table q1}}\n\n" + SOURCE
    text, counts = T.restore(half, blocks)
    assert BLOCK in text and counts == {"lines": 1, "lost": 0, "retyped": 0, "repeated": 0, "partly_retyped": 1}
    text, counts = T.restore(LEAD + "\n\n{{table q1}}\n\n" + SOURCE, {"{{table q1}}": TOTAL})
    assert text == LEAD + "\n\n" + TOTAL + "\n\n" + SOURCE and counts == {"lines": 1, "lost": 0, "retyped": 0, "repeated": 0, "partly_retyped": 0}
    x = T.expand(LEAD + "\n\n" + MONTHS + "\n\n{{table q1}}\n\n" + SOURCE, [TREND])
    assert T.marked(x.text, x.blocks) == LEAD + "\n\n" + MONTHS + "\n\n{{table q1}}\n\n" + SOURCE
    assert T.restore(T.marked(x.text, x.blocks), x.blocks)[0] == x.text

TIERS = ev("q1", "group_records", {"grouped_by": "Tier__c", "measure": "count", "groups": [
    {"group": "Gold", "value": 3}, {"group": "Silver", "value": 2}], "total_across_groups": 5})

def test_t3_label_before_a_lines_block_that_opens_the_answer():
    reply = "{{table q1}}\n\n- **Object**: `Gadget_Order__c`\n" + SOURCE
    x = T.expand(reply, [TIERS])
    assert x.text.split("\n")[:4] == ["By Tier:", "- Gold: 3", "- Silver: 2", "- Total: 5"]
    assert x.blocks["{{table q1}}"] == "By Tier:\n- Gold: 3\n- Silver: 2\n- Total: 5"
    assert T.restore(T.marked(x.text, x.blocks), x.blocks)[0] == x.text
    assert _lead(x.text) == _lead(x.text.split("\n", 1)[1])
    digit = ev("q1", "group_records", {"grouped_by": "Tier_2__c", "measure": "count", "groups": TIERS.model_result["groups"], "total_across_groups": 5})
    x = T.expand(reply, [digit])
    assert x.text.startswith("- Gold: 3") and "By " not in x.text.split("\n")[0]
    long = ev("q1", "group_records", {"grouped_by": "A_Very_Long_Field_Name_That_Runs_Past_Forty_Letters__c", "measure": "count",
                                      "groups": TIERS.model_result["groups"], "total_across_groups": 5})
    assert T.expand(reply, [long]).text.startswith("- Gold: 3")   # a head _lead would not skip
    x = T.expand("{{table q1}}\n\n- **Object**: x\n" + SOURCE, [LIST])
    assert x.text.startswith("| Name |")
    x = T.expand(LEAD + "\n\n{{table q1}}\n\n" + SOURCE, [TIERS])
    assert x.text == LEAD + "\n\n- Gold: 3\n- Silver: 2\n- Total: 5\n\n" + SOURCE
    second = ev("q2", "group_records", {"grouped_by": "Status__c", "measure": "count", "groups": [{"group": "Open", "value": 3}, {"group": "Closed", "value": 2}], "total_across_groups": 5})
    x = T.expand("{{table q1}}\n{{table q2}}\n" + SOURCE, [TIERS, second])
    assert x.text.startswith("By Tier:\n- Gold: 3") and "\n\nBy Status:\n- Open: 3" in x.text

def test_t5_table_counters():
    x = T.expand(LEAD + "\n\n{{table q1}}\n{{table q2}}\n\n" + SOURCE, [ev("q1", "about_people", {"people": []}, kind="data"), ev("q2", "get_field_history", {"changes": []}, kind="data")])
    assert x.counts["no_rows_by"] == {"about_people": 1, "get_field_history": 1} and x.counts["dropped"] == {"no_rows": 2}
    x = T.expand(LEAD + "\n\n{{table q1}}\n\n" + SOURCE, [TIERS])
    assert "no_rows_by" not in x.counts
    counts = T.answer_counts(x, x.text, x.blocks, 0)
    assert counts["chars"] == len("- Gold: 3\n- Silver: 2\n- Total: 5") and counts["cut"] == 0
    counts = T.answer_counts(x, "no rows here", x.blocks, 0)   # only the blocks the answer holds
    assert counts["kept"] == 0 and counts["chars"] == 0
    second = ev("q2", "group_records", {"grouped_by": "Status__c", "measure": "count", "groups": [
        {"group": "Open", "value": 3}, {"group": "Closed", "value": 2}], "total_across_groups": 5})
    x = T.expand(LEAD + "\n\n{{table q1}}\n\n{{table q2}}\n\n" + SOURCE, [TIERS, second])
    counts = T.answer_counts(x, x.text, x.blocks, 0)
    assert counts["kept"] == 2 and counts["chars"] == sum(len(b) for b in x.blocks.values()) > len(next(iter(x.blocks.values())))

def test_t5_nudge_kinds_and_cut():
    from sf_assistant import prompts as P
    from sf_assistant.agent import nudges as N
    import re as _re
    ids = [i for i, _p in N._NUDGE_PARTS]
    assert ids and "records_tables_marker_message" in ids
    for pid in ids:
        filled = _re.sub(r"<<[A-Z0-9_]+>>", "Alpha Beta", P.text(pid).strip())
        assert N.nudge_kinds(filled) == [pid], pid
    a = _re.sub(r"<<[A-Z0-9_]+>>", "x", P.text("records_relation_word_message").strip())
    joined = a + "\n\n" + T.marker_message(["q9"])
    assert N.nudge_kinds(joined) == ["records_relation_word_message", "records_tables_marker_message"]
    other = _re.sub(r"<<[A-Z0-9_]+>>", "x", P.text("records_other_lookup_message").strip())
    assert N.nudge_kinds(other + P.text("records_lead_zero_note")) == ["records_other_lookup_message"]
    assert N.nudge_kinds(None) == [] and N.nudge_kinds("") == [] and N.nudge_kinds("Say which field you grouped by.") == ["other"]
    assert T.line_count("{{table q1}}\n**Answer:** 36\n- note\nSource: x") == 1 and T.line_count(None) == 0
    assert T.line_count("{{table}}\n{{table q1}}") == 2   # a malformed span counts, as in expand's markers
    from sf_assistant.agent.checks import strip_reasoning
    raw = "{{table q1}}\n**Answer:** 36\n- note\nSource: x"
    assert T.line_count(raw) - T.line_count(strip_reasoning(raw)) == 1

def test_a_line_with_no_key_must_hold_the_whole_row():
    wide = ("| Name | Status | Team | Owner | City | Stage |\n|---|---|---|---|---|---|\n"
            "| J1 | Open | Sales | Vex | Pune | Won |\n| J2 | Closed | Sales | Vex | Pune | Lost |")
    assert T._typed(wide, ["- Open, Sales, Vex", "- Closed, Sales, Vex"]) == (0, 2)   # a summary: three columns would go
    assert T._typed(wide, ["- Open, Sales, Vex, Pune, Won", "- Closed, Sales, Vex, Pune, Lost"]) == (2, 2)
    narrow = "| Job | Status | Team | Owner |\n|---|---|---|---|\n| J1 | Open | Sales | Vex |\n| J2 | Closed | Sales | Vex |"
    assert T._typed(narrow, ["- Open, Sales, Vex", "- Closed, Sales, Vex"]) == (2, 2)   # the first column goes (owner point)
    two = "| Job | Status | Owner |\n|---|---|---|\n| J1 | Open | Vex |\n| J2 | Closed | Vex |"
    assert T._typed(two, ["- Open, Vex", "- Closed, Vex"]) == (0, 2)   # fewer than 3 other cells: never by this rule
    one = "| Job | Status |\n|---|---|\n| J1 | Open |\n| J2 | Closed |"
    assert T._typed(one, ["- Open", "- Closed"]) == (0, 2)
    assert T._typed(narrow, ["- Open or Closed, Sales, Vex", "- Closed, Sales, Vex"]) == (1, 2)   # exactly one row must fit
    assert T._typed(narrow, ["- J1 and J2: Open, Sales, Vex", "- J1 and J2: Closed, Sales, Vex"]) == (0, 2)   # names keys


def test_put_back_brings_the_rows_back_when_a_check_cuts_a_line_of_the_copy():
    x = T.expand(LEAD + "\n\n" + MONTHS + "\n\n{{table q1}}\n\n" + SOURCE, [TREND])
    assert x.typed == {"{{table q1}}": BLOCK} and x.blocks == {"{{table q1}}": TOTAL}
    # nothing cut: the same object, no counter
    assert T.put_back(x.text, x) is x.text and x.blocks == {"{{table q1}}": TOTAL} and "put_back" not in x.counts
    # a typed line cut: the whole block in place of the kept Total line, which shows once, and it round-trips
    cut = x.text.replace("- **Feb 2031**: 9 orders (Web 4, Store 5)\n", "")
    text = T.put_back(cut, x)
    assert BLOCK in text and text.count(TOTAL) == 1 and text.endswith("\n\n" + SOURCE)
    assert x.blocks == {"{{table q1}}": BLOCK} and x.sources == [BLOCK] and x.counts["put_back"] == 1
    assert x.counts["built"] == 0 and x.counts["dropped"] == {"typed": 1}
    assert T.answer_counts(x, text, x.blocks, 0)["kept"] == 1
    assert T.restore(T.marked(text, x.blocks), x.blocks)[0] == text
    # no Total line left, and no Source line yet: at the end
    tiers = ev("q1", "group_records", {"grouped_by": "Tier__c", "measure": "count", "groups": [
        {"group": "Gold", "value": 3}, {"group": "Silver", "value": 2}], "total_across_groups": 5})
    x = T.expand(LEAD + "\n\n- Gold: 3\n- Silver: 2\n\n{{table q1}}", [tiers])
    assert x.blocks == {} and x.counts["dropped"] == {"typed": 1}
    assert T.put_back(LEAD + "\n\n- Silver: 2", x) == LEAD + "\n\n- Silver: 2\n\n- Gold: 3\n- Silver: 2\n- Total: 5"
    # the rows of another block are no copy of this one
    more = ev("q2", "group_records", {"grouped_by": "Tier__c", "measure": "count", "groups": [
        {"group": "Gold", "value": 3}, {"group": "Silver", "value": 2}, {"group": "Bronze", "value": 1}], "total_across_groups": 6})
    x = T.expand(LEAD + "\n\n- Gold: 3\n- Silver: 2\n\n{{table q1}}\n\n{{table q2}}\n\n" + SOURCE, [tiers, more])
    assert list(x.typed) == ["{{table q1}}"] and list(x.blocks) == ["{{table q2}}"]
    text = T.put_back(x.text.replace("- Gold: 3\n- Silver: 2\n\n- Gold: 3", "- Silver: 2\n\n- Gold: 3", 1), x)
    assert list(x.blocks) == ["{{table q2}}", "{{table q1}}"] and all(T.holds(text, b) for b in x.blocks.values())
    assert text.count("- Bronze: 1") == 1
    # a kept block no longer whole is left alone: only the rows that come back are added
    other = ev("q2", "group_records", {"grouped_by": "Tier__c", "measure": "count", "groups": [
        {"group": "Gold", "value": 30}, {"group": "Bronze", "value": 1}], "total_across_groups": 31})
    x = T.expand(LEAD + "\n\n- Gold: 3\n- Silver: 2\n\n{{table q1}}\n\n{{table q2}}\n\n" + SOURCE, [tiers, other])
    edited = x.text.replace("- Gold: 3\n", "", 1).replace("- Bronze: 1\n", "")
    text = T.put_back(edited, x)
    assert "- Bronze: 1" not in text and text.count("- Total: 31") == 1 and T.holds(text, x.blocks["{{table q1}}"])
    assert x.blocks["{{table q1}}"] == "- Gold: 3\n- Silver: 2\n- Total: 5"
    # never raises
    assert T.put_back("x", None) == "x"


def test_a_merge_label_above_a_table_line_shows_once():
    block = "By Tier:\n- Gold: 2\n- (blank): 3\n- Total: 5"
    for own in ("By Tier:", "**By Tier:**", "by tier"):
        text, counts = T.restore("Here is the split.\n" + own + "\n\n{{table q1}}\n- Counted: `G`.\n" + SOURCE, {"{{table q1}}": block})
        assert T.holds(text, block) and text.lower().count("by tier") == 1, own
        assert counts == {"lines": 1, "lost": 0, "retyped": 0, "repeated": 0, "partly_retyped": 0}
    text = T.restore("Here is the split.\nTiers below:\n{{table q1}}\n" + SOURCE, {"{{table q1}}": block})[0]
    assert "Tiers below:" in text and T.holds(text, block)
    rows = "- Gold: 2\n- (blank): 3"   # a block with no label line takes nothing away
    text = T.restore("Here is the split.\n- Gold: 2\n{{table q1}}\n" + SOURCE, {"{{table q1}}": rows})[0]
    assert text.count("- Gold: 2") == 2
