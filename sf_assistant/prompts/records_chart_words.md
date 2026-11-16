---
id: records_chart_words
version: 5
name: RECORDS_CHART_WORDS
module: sf_assistant.agent.charts
source: "new in the charts of 2026-11-03 (SFA_CHARTS=on only): the words code writes on a chart and in its reason line"
placeholders: "<<COUNT>> <<SHOWN>> <<TOTAL>> <<UNIT>> <<LABELS>> <<FIELD>> <<WHY>> <<ASKED>> <<TYPE>> <<FROM>> <<TO>> <<FIELDS>> <<OBJECT>> <<TITLE>>"
fill: "one line per word as '<key>: <text>', read into a dict by agent.charts (bound as this raw text), each filled with prompts.fill"
---
bare: A chart needs the data named in the same question, for example: bar chart of interviews by status.
records_off: No chart: record data is switched off for this answer.
no_data: No chart: this answer did not use record data.
failed: No chart: the data could not be read.
answer_failed: No chart: the figures could not be computed this time.
people: No chart: answers about named people are not drawn. Ask for counts by a field.
one_number: No chart: the answer is one number, so there is nothing to compare.
one_share: No chart: the answer is one percentage, not a set of parts.
one_row: No chart: there is only one row to draw.
list: No chart: these are single records, not counts. Ask for counts by a field, such as by status.
field_values: No chart: this reads a field's stored values or change history, not counts of the records asked about. Ask for counts by that field.
no_rows: No chart: no records match.
no_rows_min: No chart: no named group reaches the minimum asked for.
no_rows_top: No chart: there is no named group to rank.
only_blank: No chart: every matching record has no value in the grouped field, or points to a record not in this copy.
no_field_data: No chart: the data does not record this: a field it filters on is blank on every record.
all_zero: No chart: every value is 0.
no_values: No chart: none of these rows has a value.
no_positive: No chart: no row has a value above 0.
too_many: No chart: <<COUNT>> rows are too many to draw; ask for the top 10 or a narrower filter.
hidden: No chart: the labels are hidden values.
same_labels: No chart: two labels read the same once cleaned or shortened, so the bars cannot be told apart.
negative: No chart: negative values are not drawn.
dates: No chart: these values are dates or text, not amounts.
too_large: No chart: this result is too large to draw.
fault: No chart: it could not be drawn this time.
rate_counts: No chart: a rate was asked, and these results hold counts, not a rate per period.
overrule: a question that asks for a chart
showing_top: Showing the top <<SHOWN>> of <<TOTAL>> <<UNIT>>.
showing_low: Showing the lowest <<SHOWN>> of <<TOTAL>> <<UNIT>>.
showing: Showing <<SHOWN>> of <<TOTAL>> <<UNIT>>.
groups: groups
periods: periods
segments: segments
blank_out: <<COUNT>> records have no value and are not drawn.
blank_out_value: The blank group (<<COUNT>>) is not drawn.
unresolved_out: <<COUNT>> records point to a record that is not in this copy and are not drawn.
ties: <<COUNT>> more groups tie with the last one shown.
min_value: Only groups with a value of at least <<COUNT>> are drawn.
overlap: A record can be in several groups; <<COUNT>> records matched.
no_date: <<COUNT>> records have no date and are not drawn.
partial: * Partial period: <<LABELS>>.
running: The period runs past today; records dated after today are counted.
several: <<COUNT>> results could be drawn; this chart shows the last one.
split_left: The split by <<FIELD>> is in the rows, not on the chart: <<WHY>>.
split_many: it has more than 8 values
split_adds: its values do not add up to each row's total
split_measure: this measure cannot be stacked
split_same: The split by <<FIELD>> is left out: two of its names read the same once cleaned.
type_no_fit: A <<ASKED>> chart does not fit this data, so this is a <<TYPE>> chart.
type_other: This chart type is not available, so this is a <<TYPE>> chart.
records: Records
sum: Sum of <<FIELD>>
avg: Average of <<FIELD>>
min: Lowest <<FIELD>>
max: Highest <<FIELD>>
count_distinct: Distinct <<FIELD>>
percent: Percent
rate_part: Part (records)
rate_base: Base (records)
rate_of: Percent of the records in each period that match the condition on <<FIELDS>>
rate_overall: Whole period: <<COUNT>>% (<<SHOWN>> of <<TOTAL>> records)
no_base: No records in <<LABELS>>, so no rate there (not 0%).
total: Total
compared: Compared
segment: Segment <<COUNT>>
title_by: <<OBJECT>> by <<FIELD>>
title_per: <<OBJECT>> per <<FIELD>>
title_compared: <<OBJECT>> compared
title_measure: <<OBJECT>>: <<UNIT>>
period: Period: <<FROM>> to <<TO>> (by <<FIELD>>)
period_from: Period: from <<FROM>> (by <<FIELD>>)
period_to: Period: to <<TO>> (by <<FIELD>>)
period_by: Periods by <<FIELD>>
filtered: Filtered on: <<FIELDS>>
other_field: another field
related: related <<OBJECT>> records
no_value: no value
null_no_column: Rows with no value have no column.
cli_chart: Chart (<<TYPE>>): <<TITLE>>
cli_rows: Rows:
