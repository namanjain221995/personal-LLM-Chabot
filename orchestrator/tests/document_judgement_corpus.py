"""An adversarial corpus for the document switch, written BEFORE its detector.

WHY THIS FILE EXISTS (2026-09-18, round 5). The round-4 verifier generated
11,475 "field ask + judgement ask" questions and found 8,676 routed to strict
extraction alone. Its conservative re-cut — 15 tails that unambiguously ask
for a verdict, a risk assessment or a caution — routed 300 of 300 to strict
extraction, and 15 of 15 of those tails carried no decision signal at all.
Live, "what is the annual fee in this contract, and is that in line with the
market?" came back "I cannot assess its competitiveness based on the provided
text".

The round-4 search could not have found that, by construction: its control
asserted that every tail ALREADY registered as a decision signal, so it only
ever searched wording the vocabulary knew. A search built from the detector's
own vocabulary can only return zero.

So this corpus is committed on its own, before the detector that round 5
builds, and nothing here is filtered through `source_use`. Each judgement
tail is labelled by hand, by its OWN wording: `kind` says what it asks for and
`because` quotes the words that make it a judgement ask. Nothing in a row says
whether any pattern recognises it, and the suite asserts the opposite of the
round-4 control: that a large share of these tails carry NO decision signal,
so the search keeps searching where the vocabulary is blind.

HELD OUT. `HELD_OUT_TAILS` and `HELD_OUT_FACT_FOLLOW_UPS` were written in the
same sitting and not measured until the detector was finished; they are the
honest estimate of how the detector does on wording it was not tuned against.
"""

#: What a tail can ask for. Every one of these is a judgement the strict
#: extraction block would refuse: it answers from the document alone, and no
#: document states whether its own fee is in line with the market.
KINDS = (
    "market",       # how a figure compares with what others charge or agree
    "fairness",     # whether it is fair, balanced, who it favours
    "verdict",      # would you accept / sign / live with it
    "opinion",      # your view, your take, how does it look
    "risk",         # what could go wrong, exposure, enforceability, legality
    "caution",      # what to watch out for, whether to be worried
    "feasibility",  # can we meet / afford / achieve it
    "action",       # what to do about it: negotiate, push back, pay early
)

#: (tail, kind, because). `because` is a fragment of the tail itself — the
#: words that make it a judgement ask — so a label can be checked against the
#: wording it describes.
JUDGEMENT_TAILS = (
    # --- market: a figure against the world outside the document ---------
    ("is that in line with the market?", "market", "in line with the market"),
    ("how does that stack up against the market?", "market", "stack up against the market"),
    ("is that above or below the going rate?", "market", "above or below the going rate"),
    ("is that what others in the industry pay?", "market", "what others in the industry pay"),
    ("is that on the high side?", "market", "on the high side"),
    ("is that competitive?", "market", "competitive"),
    ("how does that compare with industry norms?", "market", "compare with industry norms"),
    ("is that market rate?", "market", "market rate"),
    ("are we paying over the odds?", "market", "paying over the odds"),
    ("is that in the right ballpark?", "market", "in the right ballpark"),
    ("would a typical supplier charge less?", "market", "a typical supplier charge less"),
    ("is that cheap or pricey for this kind of deal?", "market", "cheap or pricey"),
    ("is that more than similar firms pay?", "market", "more than similar firms pay"),
    ("tell me if that's in line with the market", "market", "in line with the market"),
    ("whether that is in line with industry benchmarks", "market", "in line with industry benchmarks"),
    ("in line with the market?", "market", "in line with the market"),
    ("on the high side?", "market", "on the high side"),
    ("is that the norm for SaaS contracts?", "market", "the norm"),
    ("does that match what vendors usually ask for?", "market", "what vendors usually ask for"),
    ("is that on par with competitors?", "market", "on par with competitors"),
    ("is that short by industry standards?", "market", "by industry standards"),
    # --- fairness ---------------------------------------------------------
    ("is that fair to us?", "fairness", "fair to us"),
    ("is that one-sided?", "fairness", "one-sided"),
    ("who does that favour, us or them?", "fairness", "who does that favour"),
    ("is that generous or stingy?", "fairness", "generous or stingy"),
    ("is that a lot to ask?", "fairness", "a lot to ask"),
    ("am I being ripped off?", "fairness", "ripped off"),
    ("is that a sweet deal or a rip-off?", "fairness", "a sweet deal or a rip-off"),
    # --- verdict ----------------------------------------------------------
    ("is that a good deal?", "verdict", "a good deal"),
    ("would you accept that?", "verdict", "would you accept"),
    ("would you sign off on that?", "verdict", "would you sign off"),
    ("can we live with that?", "verdict", "can we live with"),
    ("is that acceptable for a company our size?", "verdict", "acceptable"),
    ("is that a sensible figure?", "verdict", "sensible"),
    ("would most companies push back on that?", "verdict", "push back"),
    ("would a lawyer be happy with that?", "verdict", "would a lawyer be happy"),
    ("does that make it a bad contract?", "verdict", "a bad contract"),
    ("good or bad?", "verdict", "good or bad"),
    ("and your verdict?", "verdict", "your verdict"),
    # --- opinion ----------------------------------------------------------
    ("does that seem right to you?", "opinion", "seem right to you"),
    ("does that look off to you?", "opinion", "look off to you"),
    ("what's your take on that?", "opinion", "your take"),
    ("what's your honest opinion of it?", "opinion", "your honest opinion"),
    ("how does that look from our side?", "opinion", "how does that look from our side"),
    ("your thoughts?", "opinion", "your thoughts"),
    ("is that negotiable, in your view?", "opinion", "in your view"),
    ("how would you rate that?", "opinion", "how would you rate"),
    ("give me your view on whether that's fair", "opinion", "your view on whether that's fair"),
    # --- risk ---------------------------------------------------------------
    ("does that put us at a disadvantage?", "risk", "put us at a disadvantage"),
    ("is that going to bite us later?", "risk", "going to bite us"),
    ("does that expose us to anything?", "risk", "expose us"),
    ("are there any hidden dangers in that?", "risk", "hidden dangers"),
    ("is that a trap?", "risk", "a trap"),
    ("could that come back to haunt us?", "risk", "come back to haunt us"),
    ("does that leave us vulnerable?", "risk", "leave us vulnerable"),
    ("is there a catch?", "risk", "a catch"),
    ("is it enforceable?", "risk", "enforceable"),
    ("would that hold up in court?", "risk", "hold up in court"),
    ("is that even legal?", "risk", "even legal"),
    ("is it aggressive?", "risk", "aggressive"),
    ("will that hurt our margins?", "risk", "hurt our margins"),
    ("how risky is that?", "risk", "how risky"),
    # --- caution ------------------------------------------------------------
    ("am I right to be uneasy about it?", "caution", "right to be uneasy"),
    ("should that ring alarm bells?", "caution", "ring alarm bells"),
    ("is that something to watch out for?", "caution", "watch out for"),
    ("anything we ought to be careful about there?", "caution", "ought to be careful"),
    ("does that raise any eyebrows?", "caution", "raise any eyebrows"),
    ("is that out of the ordinary?", "caution", "out of the ordinary"),
    ("flag it if that looks unusual", "caution", "looks unusual"),
    # --- feasibility ----------------------------------------------------------
    ("is that tight for us?", "feasibility", "tight for us"),
    ("is that realistic for a team of five?", "feasibility", "realistic"),
    ("can we actually meet that?", "feasibility", "can we actually meet"),
    ("is that achievable given our cash flow?", "feasibility", "achievable given our cash flow"),
    ("can we afford that?", "feasibility", "can we afford"),
    ("is that affordable for a startup?", "feasibility", "affordable for a startup"),
    ("is that long enough to find a replacement supplier?", "feasibility", "long enough"),
    # --- action ---------------------------------------------------------------
    ("is it something we should push back on?", "action", "should push back"),
    ("let me know if you'd negotiate it down", "action", "negotiate it down"),
    ("is it worth pushing back on?", "action", "worth pushing back"),
    ("would you advise against it?", "action", "advise against"),
    ("tell me whether that's a smart move", "action", "a smart move"),
    ("sanity-check it against what the market charges", "action", "sanity-check it against what the market charges"),
    ("is it better to pay it upfront?", "action", "better to pay it upfront"),
    ("should I be comfortable with that?", "action", "comfortable with that"),
)

#: Written in the same sitting as the list above, and NOT measured until the
#: detector was finished. Same labelling rule.
HELD_OUT_TAILS = (
    ("is that steep?", "market", "steep"),
    ("is that pricey?", "market", "pricey"),
    ("does that sound high to you?", "market", "sound high to you"),
    ("does that seem excessive for what we get?", "market", "excessive for what we get"),
    ("too high?", "market", "too high"),
    ("is that more or less what everyone charges?", "market", "what everyone charges"),
    ("how far off the usual is that?", "market", "off the usual"),
    ("is that a fair split between us?", "fairness", "a fair split"),
    ("is that balanced?", "fairness", "balanced"),
    ("does that tilt things towards the supplier?", "fairness", "tilt things towards the supplier"),
    ("would you be comfortable signing that?", "verdict", "would you be comfortable signing"),
    ("thumbs up or thumbs down?", "verdict", "thumbs up or thumbs down"),
    ("is that a win for us?", "verdict", "a win for us"),
    ("how does that strike you?", "opinion", "how does that strike you"),
    ("what's your gut feeling about it?", "opinion", "your gut feeling"),
    ("any thoughts on that?", "opinion", "thoughts on that"),
    ("could that land us in trouble?", "risk", "land us in trouble"),
    ("is that a liability for us?", "risk", "a liability for us"),
    ("is there a downside?", "risk", "a downside"),
    ("would that survive a legal challenge?", "risk", "survive a legal challenge"),
    ("is it safe to agree to that?", "caution", "safe to agree"),
    ("is there anything fishy about that?", "caution", "anything fishy"),
    ("does that ring true?", "caution", "ring true"),
    ("can a small team cope with that?", "feasibility", "can a small team cope"),
    ("will that stretch our budget?", "feasibility", "stretch our budget"),
    ("is that doable for us this quarter?", "feasibility", "doable for us"),
    ("would you try to get it lowered?", "action", "get it lowered"),
    ("is it smarter to walk away?", "action", "smarter to walk away"),
    ("do we push back or accept?", "action", "push back or accept"),
    ("tell me if you'd haggle over that", "action", "haggle over that"),
)

#: Field asks that each reach strict extraction ON THEIR OWN (a control in the
#: suite asserts it, so a combination cannot pass because the field half
#: quietly failed to register). Several shapes: value-wh, request, statement.
FIELD_HEADS = (
    "what is the annual fee in this contract",
    "what is the invoice total",
    "what's the due date",
    "what is the notice period",
    "what are the payment terms",
    "what is the governing law",
    "what is the termination fee",
    "how much is the late payment penalty",
    "what is the interest rate on late payment",
    "who are the parties to this agreement",
    "what is the term of this contract",
    "tell me the renewal date",
    "give me the grand total",
    "extract the payment terms",
    "list the line items",
    "what is the unit price of the cooling unit",
    "what is the subscription fee",
    "what is the tax amount on this invoice",
    "how long is the notice period",
    "when does this agreement expire",
    "what is the discount",
    "what is the setup fee",
    "what's the balance due",
    "what is the effective date",
    "what does the invoice say the total is",
    "what's the amount due on this invoice",
    "pull the fees from this contract",
    "what is the cancellation fee in the MSA",
    "I need the total from this invoice",
    "what's the service fee per month",
)

#: How the two asks are joined. The verifier's shapes (", and ", " - ", ". ",
#: " but ") plus the ones people also type.
JOINS = (", and ", " and ", " - ", " — ", ". ", "; ", " but ", "? ", ", also ", ", so ")

#: Follow-ups a document CAN answer: a fact about the field, not a judgement.
#: These build the opposite-direction corpus — a field ask plus one of these
#: is still a pure field ask, and strict extraction must survive it.
FACT_FOLLOW_UPS = (
    "is it payable in advance?",
    "is that per year?",
    "is that in USD?",
    "does it include VAT?",
    "is that before or after tax?",
    "when is it due?",
    "who pays it?",
    "is there a late fee?",
    "is it signed?",
    "which clause is it in?",
    "what page is that on?",
    "is it stated anywhere else?",
    "what currency is it in?",
    "is it refundable?",
    "is that fixed for the whole term?",
    "is it listed as a separate line?",
    "and the due date?",
    "and who signed it?",
    "and the PO number?",
    "is there a PO number?",
    "what date was it issued?",
    "and the vendor name?",
    "does the invoice show the tax separately?",
    "is that the total including VAT?",
    "on what date?",
)

#: Written in the same sitting, not measured until the detector was finished.
HELD_OUT_FACT_FOLLOW_UPS = (
    "is it paid monthly or annually?",
    "is that in euros?",
    "where is it stated?",
    "who is the payee?",
    "is it quoted per unit?",
    "does it mention a deposit?",
    "and the invoice number?",
    "is it dated?",
    "what section covers it?",
    "is that net of the discount?",
)

#: Field asks joined to field asks, with no judgement anywhere.
FIELD_PAIRS = (
    ("what is the invoice total", "what is the due date?"),
    ("what is the annual fee", "when is it payable?"),
    ("who are the parties", "what is the governing law?"),
    ("what is the notice period", "what is the termination fee?"),
    ("give me the vendor name", "the billing address"),
    ("what is the effective date", "when does it expire?"),
    ("list the line items", "the quantities"),
    ("what is the subtotal", "the tax amount?"),
    ("what is the PO number", "the invoice number?"),
    ("what are the payment terms", "is there a late payment penalty?"),
)
