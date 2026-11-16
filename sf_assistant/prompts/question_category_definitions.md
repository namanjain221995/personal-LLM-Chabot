---
id: question_category_definitions
version: 1
name: CATEGORY_DEFINITIONS
module: sf_assistant.agent.knowledge_prompts
source: "notebooks/object_knowledge_eval/okeval/prompts.py CATEGORY_DEFINITIONS (dict)"
placeholders: ""
fill: "parsed into the dict CATEGORY_DEFINITIONS (one line per category, in order, as '- <category>: <definition>')"
---
- object_identity: What the object is: its API name, label/plural label, description, business domain and core business concepts. E.g. 'What is an Internal Interview used for?'
- record_grain: What exactly ONE record represents and how records relate in number to other records (one interview per record, many attendees per interview). E.g. 'Does one record mean one interview or one attendee?'
- field_lookup: Whether a field exists and what its API name / label is, given a label, API name or close description. E.g. 'Is there a field that stores the JD link?'
- field_meaning: The business meaning of a specific field: what it stores and how it is used. E.g. 'What does Combined Score mean?'
- field_type: The data type of a field (picklist, reference, date, datetime, number, text, checkbox, auto_number, formula/calculated ...). E.g. 'What type of field is Scheduled Date?'
- picklist_values: The allowed values of a picklist field, listed completely. E.g. 'What statuses can an interview have?'
- picklist_value_meaning: What a specific picklist value means or when it applies, including lifecycle order. E.g. 'What does the Rescheduled status imply?'
- reference_target: Which object(s) a lookup / master-detail (reference) field points to. E.g. 'Which object does the Candidate field point to?'
- relationship_semantics: The business role, direction and cardinality of a parent relationship (who is the candidate, interviewer, recruiter, support person; many-to-one ...). E.g. 'How is the interviewer linked to an interview?'
- child_relationships: Which child objects point to this object, through which field, and what they mean. E.g. 'Which records hang off an interview?'
- temporal_audit: Which fields answer created/updated when/by whom questions (system audit fields) and other temporal semantics. E.g. 'Which field tells me when the interview record was created?'
- validation_rules: Validation rules on the object: name, condition, fields involved, what error/locking they enforce. E.g. 'Can a rescheduled interview still be edited?'
- automation_writes: Which flows / Apex classes write (update) which fields of this object. E.g. 'What automation sets the Status field?'
- automation_reads: Which fields (including cross-object fields like Session__c.Status__c) a flow or Apex class reads as input. E.g. 'What does the status update flow look at?'
- automation_triggers: What starts an automation: trigger object, events (create/update), screen vs autolaunched, schedule. E.g. 'What triggers the interview status sync?'
- business_process: Business processes this object participates in, its role in each, and which automation implements them. E.g. 'What role does the interview record play in rescheduling?'
- legacy_vs_current: Fields described as legacy/deprecated/older model versus the field or object that replaced them. E.g. 'Should I still use the Candidate field or the attendee rows?'
- typo_api_names: Fields whose API names are misspelled or truncated (e.g. 'Descion', 'Postion', 'Employement', 'Start_Tim') and which exact API name to use. E.g. 'What is the API name of the final decision field?'
- semantic_role_search: Finding fields by semantic role (identifier, lifecycle_state, score, feedback, assignment, timestamp ...). E.g. 'Which fields hold scores?'
- concept_to_field: Mapping a business concept phrased in plain words to the field API name that stores it, without naming the field. E.g. 'Where is the person who supports the interview stored?'
- provenance_quality: Where the knowledge came from (sources, environment, confidence, review status) and what must be verified elsewhere. E.g. 'How trustworthy is the requiredness info?'
- record_types: Record types defined on the object (possibly none). E.g. 'Does Internal Interview have record types?'
- cross_section: Questions that need two or more sections combined (field + automation, picklist + validation rule, relationship + child relationship, process + flow). E.g. 'Which status value locks the record and which flow can set it?'
- unanswerable_record_data: Questions about live record data: counts, specific records, current values, averages, who owns a record. The object knowledge cannot answer these; the correct answer says it needs record data. E.g. 'How many interviews were completed last week?'
- unanswerable_missing_knowledge: Metadata questions the knowledge file does not cover (page layouts, field-level security, sharing rules, requiredness left null, fields that do not exist). The correct answer says the knowledge file does not contain it. E.g. 'Which profiles can edit the Interviewer field?'
