---
id: followup_unclear
version: 1
name: FOLLOWUP_UNCLEAR_TEXT
module: sf_assistant.agent.followup
source: "development B of 2026-11-27 (an unclear follow-up says what is missing): the parts code writes when the follow-up rewrite, or the memory resolver, replies UNCLEAR and names the missing part with a closed code (who, object, number, period); the caller's fixed unclear line follows them, and a reply with no code gets that line alone (agent.followup.unclear_text)"
placeholders: "<<WORD>> <<NOUN>> <<PHRASE>> <<UNIT>> <<READING>>"
fill: "one line per key as '<key>: <text>', read into a dict by agent.followup (bound as this raw text). The who_, object, number_ and period_ lines are clauses: code joins those of the codes given (in the order who, object, number, period) with ', and ', starts the sentence with a capital and ends it with '?'. offer goes before that sentence. WORD and PHRASE are words the person typed in the new question (letters and spaces only), NOUN the typed word after a typed comparison word, UNIT a plural time word of PHRASE, READING words of an earlier question the person typed, as the rewrite returned them and code checked them"
---
who_person: who do you mean by "<<WORD>>"
who_thing: what do you mean by "<<WORD>>"
who_group: which ones do you mean by "<<WORD>>"
who_plain: who or what is this about
object: which records do you mean
number_fewer: fewer than how many <<NOUN>>
number_more: more than how many <<NOUN>>
number_fewer_plain: fewer than how many
number_more_plain: more than how many
number_plain: which number do you mean
period_unit: which <<UNIT>> do you mean by "<<PHRASE>>"
period_phrase: which period do you mean by "<<PHRASE>>"
period_plain: over which period
offer: Do you mean <<READING>>?
fewer_words: less, fewer, lower, lesser, below, kam, kum
more_words: more, greater, higher, above, zyada, jyada, zyaada, adhik
