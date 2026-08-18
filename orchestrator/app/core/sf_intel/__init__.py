"""Salesforce Intelligence Mode — contextual clarification and grounded answers.

The Salesforce pill used to be a retrieval filter: "answer from my data" versus
"answer from the model". This package turns it into a context-aware agent that
resolves a request against the conversation before it queries anything, asks ONE
targeted question when a missing slot would materially change the answer, and
resumes the ORIGINAL request once that question is answered.

The pieces, in the order a request meets them:

    interpret.py  the deterministic reading of the request: domain words spelled
                  the way this org spells them, and the slots the sentence
                  already settles — so a question the user answered in their own
                  first line is never asked back at them
    resume.py   is this message an answer to the question we just asked, or a
                new topic? (deterministic signals first, model second)
    planner.py  execute / ask / answer-generally / unsupported — one validated
                AgentDecision, never prose parsed for control flow
    plan.py     a structured SalesforceQueryPlan compiled to SOQL by US, so
                model output can never become query text
    state.py    the pending intent + per-conversation Salesforce state that make
                "what about EMEA?" mean something
    phases.py   the progress labels the UI shows — summaries, never reasoning

Nothing here streams, stores, or logs raw chain-of-thought (§10 of the
implementation directive): only structured state, tool results, assumptions and
a compact decision summary ever leave this package.
"""
