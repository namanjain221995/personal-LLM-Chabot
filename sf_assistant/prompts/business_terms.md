---
id: business_terms
version: 2
name: BUSINESS_TERMS_TEXT
module: sf_assistant.engine.business_terms
source: "new on 2026-11-16 (the owner's definition of placement): the record hints add a term's line when a question word names it (engine.business_terms); version 2 (review of 2026-11-16): a question that counts candidates counts different candidates, and the never-rule covers what is counted, not a filter the question names"
placeholders: ""
fill: "one line per term as '<key>: <definition>', read into a dict by engine.business_terms; the hints show the definition"
---
placement: Placement (placed, placements, placement rate) has one meaning here, set by the owner: a placement is a client interview (Interview__c) whose Interview_Outcome__c is 'Offer Received'. "How many placements" counts those interviews; when the question counts candidates ("number of candidates placed", "how many candidates were placed"), use measure count_distinct with measure_field Candidate__c on the same filter. Placement rate = those interviews / all client interviews (Interview__c with no outcome filter) in the same period, as a percentage: percentage_of_records with part_filters Interview_Outcome__c eq 'Offer Received', or, per month, trend_records with the same part_filters. Monthwise / per month = by the interview month: date_field Date_of_Interview__c, grain month. Never count Account.Candidate_Status__c = 'Placed', Onboarding__c completion or its placement services agreement as placements (a filter the question itself names on those fields still applies).
