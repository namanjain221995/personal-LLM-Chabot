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


# ---------------------------------------------------------------------------
# ROUND 6 (2026-09-18). Written BEFORE the round-6 detector, the same way as
# the rows above. The round-5 verifier's blind search found five SHAPES the
# clause test did not see; the dev rows below carry the verifier's own
# failing wordings for each shape AND new ones, and the held-out rows use only
# new wording and were not measured until the detector was finished. Every
# row still names, in `because`, the words that make it a judgement ask.
# ---------------------------------------------------------------------------

#: Dev rows, grouped by the shape that hid them.
ROUND6_TAILS = (
    # an imperative with a NOUN object (round 5 needed an object pronoun)
    ("assess the risk", "risk", "assess the risk"),
    ("evaluate the terms", "verdict", "evaluate the terms"),
    ("flag any risks", "risk", "flag any risks"),
    ("highlight any concerns", "caution", "highlight any concerns"),
    ("assess our exposure", "risk", "assess our exposure"),
    ("critique the pricing", "opinion", "critique the pricing"),
    ("size up the deal for us", "verdict", "size up the deal"),
    ("score the terms out of ten", "opinion", "score the terms"),
    ("look for anything unfair", "fairness", "anything unfair"),
    ("comment on the pricing", "opinion", "comment on the pricing"),
    ("point out the weak spots", "risk", "the weak spots"),
    ("rate it out of 10", "opinion", "rate it"),
    ("stress-test the payment schedule", "feasibility", "stress-test"),
    # a fragment led by a word that is ALSO filler ("okay", "ok")
    ("okay for us?", "verdict", "okay for us"),
    ("ok to sign?", "verdict", "ok to sign"),
    ("okay value?", "market", "okay value"),
    ("ok by you?", "opinion", "ok by you"),
    ("so, fair?", "fairness", "fair"),
    ("well, too high?", "market", "too high"),
    ("now, safe to accept?", "caution", "safe to accept"),
    # a modal + the person + a verb the fact lexicon also holds
    ("would we sign it?", "verdict", "would we sign"),
    ("would I pay that?", "verdict", "would I pay"),
    ("could we renew on these terms?", "verdict", "could we renew"),
    ("might we be charged less elsewhere?", "market", "charged less elsewhere"),
    ("should we sign it as is?", "verdict", "should we sign"),
    # a statement of the person's own view, with no question in it
    ("that seems high to me", "market", "seems high"),
    ("our CFO thinks it's excessive", "market", "excessive"),
    ("feels like a lot", "fairness", "a lot"),
    ("I think we are overpaying", "market", "overpaying"),
    ("my gut says it's aggressive", "risk", "aggressive"),
    ("legal reckons it's one-sided", "fairness", "one-sided"),
    # "is there ..." with the "is there" and the question mark left off
    ("anything here that would stop you", "caution", "would stop you"),
    ("any red flags", "caution", "red flags"),
    ("anything unusual in it", "caution", "unusual"),
)

#: Held out: new wording only, not measured until the detector was finished.
ROUND6_HELD_OUT_TAILS = (
    ("grade the offer", "verdict", "grade the offer"),
    ("vet the pricing for us", "market", "vet the pricing"),
    ("poke holes in the terms", "risk", "poke holes"),
    ("scrutinise the penalties", "risk", "scrutinise the penalties"),
    ("weigh the fee against the market", "market", "against the market"),
    ("audit the charges for anything odd", "caution", "anything odd"),
    ("walk me through the risks", "risk", "the risks"),
    ("summarise the downsides", "risk", "the downsides"),
    ("okay to go ahead?", "verdict", "okay to go ahead"),
    ("ok by industry standards?", "market", "industry standards"),
    ("okay or not?", "verdict", "okay or not"),
    ("so, a good price?", "market", "a good price"),
    ("would we renew it?", "verdict", "would we renew"),
    ("could I sign it today?", "verdict", "could I sign"),
    ("might we owe more than that?", "risk", "owe more"),
    ("would we be paying twice?", "risk", "paying twice"),
    ("the fee seems a bit rich", "market", "a bit rich"),
    ("that sounds expensive to me", "market", "expensive"),
    ("feels like we're overpaying", "market", "overpaying"),
    ("my CFO believes it's steep", "market", "steep"),
    ("I reckon it's too high", "market", "too high"),
    ("the board thinks the notice period is short", "feasibility", "is short"),
    ("anything we should push back on", "action", "push back"),
    ("any concerns from your side", "caution", "any concerns"),
    ("anything that looks off", "caution", "looks off"),
    ("where are the risks?", "risk", "the risks"),
    ("how would a buyer see it?", "opinion", "how would a buyer see it"),
)

#: Follow-ups a document CAN answer, and courtesies that ask nothing. The
#: first two are the verifier's: beside a field they cost strict extraction
#: 192 of 1,440 times on the round-5 detector, and "appreciate it" / "love
#: it" were read as imperatives.
ROUND6_FACT_FOLLOW_UPS = (
    "is there a grace period?",
    "does it renew automatically?",
    "is it billed monthly?",
    "is there an auto-renewal?",
    "does it auto-renew?",
    "appreciate it",
    "love it",
    "thanks so much",
    "cheers",
    "much appreciated",
)

#: Held out, same sitting, not measured until the detector was finished.
ROUND6_HELD_OUT_FACT_FOLLOW_UPS = (
    "is there a cure period?",
    "does the fee renew each year?",
    "is the deposit refundable?",
    "is there a cap on the late fee?",
    "is the price fixed?",
    "does it roll over automatically?",
    "is there a notice requirement?",
    "thank you kindly",
    "ta",
    "you're a star",
    "is it billed in arrears?",
    "who is it addressed to?",
)

#: Context a person gives beside a field ask. None of it asks anything, so a
#: field ask with one of these is still a pure field ask. They measure what a
#: rule for statements of view costs (the held-out half has the verbs such a
#: rule would key on: "I think", "I believe", "looks like").
ROUND6_CONTEXT = (
    "we got this invoice yesterday",
    "our accountant needs it by Friday",
    "this is for the Leeds office",
    "we're a five-person startup",
    "I'm reconciling our accounts",
)
ROUND6_HELD_OUT_CONTEXT = (
    "I think this is the final version",
    "I believe it was sent last week",
    "it looks like a standard template",
    "my manager asked me to check",
    "the supplier sent it this morning",
)

#: Natural field asks the round-5 verifier found reaching NO strict block at
#: all. A gap here costs a sentence (they go to neutral), so they are recorded
#: and measured, not a bar.
ROUND6_UNREACHED_FIELD_HEADS = (
    "what's the liability cap",
    "what is the indemnity limit",
    "what's the minimum commitment",
    "how long is the non-compete",
    "what's the monthly rent in the lease",
)

#: Written AFTER the view rule's document-identity exemption was fitted to the
#: held-out context rows above (which therefore no longer measure unseen
#: wording), and measured once, to estimate what the view rule costs on
#: context it was not fitted to. Every one has an attitude word and no
#: judgement of a value.
ROUND6_FRESH_CONTEXT = (
    "I think the supplier emailed it on Monday",
    "it seems to be the signed copy",
    "I guess this is page two",
    "looks like it's in USD",
    "I believe our office manager uploaded it",
    "I think it covers the Leeds site",
    "it sounds like they want payment soon",
    "I feel this is the one legal approved",
    "I suspect there's a second page",
    "it looks like an older layout",
)

#: Written after ROUND6_FRESH_CONTEXT showed the first view rule costing 7 of
#: those 10, and BEFORE the narrower view rule was designed; not measured
#: until it was finished. Some are deliberately hard: "the scan is a bit
#: blurry" and "the second page is missing" are evaluative predicates about
#: the PAPER, not the value.
ROUND6_FRESH_CONTEXT_2 = (
    "I think accounts payable already has a copy",
    "it seems they changed the address",
    "I guess the vendor is based in Leeds",
    "I believe finance forwarded it yesterday",
    "looks like the scan is a bit blurry",
    "I suspect this came from their old system",
    "I think the second page is missing",
    "it sounds like the PO was raised in June",
    "I reckon this is the one from March",
    "my manager thinks it arrived late",
)


# ---------------------------------------------------------------------------
# ROUND 7 (2026-09-19): WHOLE QUESTIONS, on three documents, for the live bar.
#
# Six rounds tuned a router that picked between a strict extraction block
# (which forbade judgement) and extract+advise, and each blind verifier still
# found 10-25% of judgement asks routed to the strict block. Round 7 stops
# making the router carry that decision: one document block serves every
# field-bearing question and answers any judgement the person asked for.
# These rows are the bar that design is measured against, LIVE: a sample of
# 40 must give a verdict or an explicit judgement for the judgement half, and
# none may open with a refusal.
#
# Written BLIND: this block was committed before the round-7 prompt was
# written, and nothing here was filtered through `source_use`. Every row is a
# COMPLETE question as a person types it -- a field ask and a judgement ask
# together -- and `shape` says how the judgement half is phrased, so the live
# sample can be drawn across shapes rather than from the easy ones.
# ---------------------------------------------------------------------------

#: How the judgement half is phrased.
R7_SHAPES = (
    "question",          # "is that fair?"
    "imperative-det",    # "assess the risk"
    "imperative-bare",   # "flag concerns", "assess risk" (no determiner)
    "fragment",          # "fair?", "too steep?"
    "okay",              # "okay for us?", "ok to pay?"
    "modal",             # "shall we sign it?", "would we be mad to renew?"
    "view",              # "that seems steep to me"
    "desire",            # "I want to know if we should pay early"
    "compare",           # "how does it compare with ..."
    "decision",          # "pay now or wait?"
)

#: Three synthetic documents, plain text (the engine reads UTF-8 text uploads
#: exactly as it reads a PDF's text layer). Invented firms, invented figures.
R7_INVOICE = """\
HARTWELL PRINT & SUPPLY LTD
Unit 9, Canal Wharf, Leicester LE1 4QP
VAT registration: GB 318 4471 09

TAX INVOICE
Invoice number: HPS-24117
Invoice date: 2026-08-28
Due date: 2026-09-27
Payment terms: Net 30. Late payments incur interest at 2% per month.

Bill to: Oakridge Dental Group, 41 Mill Street, Nottingham NG1 6FF
Customer PO: OD-5521

Qty  Description                                   Unit price   Line total
 4   A4 laser toner, high yield (black)              GBP 96.00   GBP 384.00
 2   Colour toner set (CMY)                          GBP 210.00  GBP 420.00
10   Recycled copier paper, box of 5 reams           GBP 24.50   GBP 245.00
 1   On-site printer service visit                   GBP 145.00  GBP 145.00

Subtotal:        GBP 1,194.00
VAT (20%):       GBP 238.80
Total due:       GBP 1,432.80

Bank: Lloyds, sort code 30-94-57, account 41882310. Please quote the invoice number.
"""

R7_CONTRACT = """\
SUBSCRIPTION SERVICES AGREEMENT

Between Lumenfold Analytics Inc. ("Provider") and Tarn Valley Logistics Ltd
("Customer"). Effective Date: 2026-10-01.

1. Services. Provider grants Customer access to the Lumenfold fleet analytics
   platform for up to 150 named users.
2. Term. The initial term is 36 months from the Effective Date. Thereafter the
   Agreement renews automatically for successive 12-month periods unless
   either party gives at least 90 days written notice before the end of the
   then-current term.
3. Fees. Customer shall pay an annual subscription fee of USD 64,800, invoiced
   annually in advance. Provider may increase fees on each anniversary by up
   to 9%. Implementation fee: USD 12,500, one time.
4. Payment. Invoices are payable within 45 days. Overdue amounts bear interest
   at 1.5% per month.
5. Service levels. Provider targets 99.5% monthly availability. If
   availability falls below the target, Customer receives a service credit of
   5% of the monthly fee, capped at 15% per month.
6. Termination. Either party may terminate for material breach not cured
   within 30 days of notice. Customer may not terminate for convenience during
   the initial term.
7. Liability. Each party's total liability is capped at the fees paid in the
   6 months before the claim.
8. Data. Customer data is hosted in the United States. On termination Provider
   deletes Customer data after 30 days.
9. Governing law. The laws of the State of Delaware.
"""

R7_BROCHURE = """\
SOLACE SX-12 HOME AND SMALL-BUSINESS BATTERY STORAGE

Store your solar energy and keep the lights on through outages.

Usable capacity: 12.0 kWh (lithium iron phosphate)
Continuous output: 5.0 kW; peak output 8.0 kW for 10 seconds
Round-trip efficiency: 92%
Warranty: 10 years or 6,000 full cycles, whichever comes first, with at least
70% of capacity retained
Backup: whole-home or selected-circuit backup, switchover in under 20 ms
Expansion: up to 3 units in parallel (36 kWh)
Enclosure: IP55, wall or floor mounted, indoor or outdoor
Operating temperature: -10 to 50 C
Dimensions: 1,150 x 620 x 240 mm; weight 118 kg
Price: EUR 8,400 per unit, installation not included
"""

R7_DOCUMENTS = {"invoice": R7_INVOICE, "contract": R7_CONTRACT, "brochure": R7_BROCHURE}

#: (document, question, shape). Each question carries a FIELD ask and a
#: JUDGEMENT ask. Hand-written; not generated, not filtered.
R7_JUDGEMENT_QUESTIONS = (
    # --- the invoice --------------------------------------------------------
    ("invoice", "What's the total on this invoice, and is that fair for office supplies?", "question"),
    ("invoice", "what is the VAT amount - is it calculated correctly?", "question"),
    ("invoice", "how much is the service visit and is that reasonable for a printer call-out?", "question"),
    ("invoice", "When is it due? Should we pay it early?", "decision"),
    ("invoice", "what's the unit price of the toner, is it overpriced?", "question"),
    ("invoice", "give me the invoice number and assess whether anything looks off", "imperative-det"),
    ("invoice", "list the line items and flag anything unusual", "imperative-det"),
    ("invoice", "pull the payment terms and assess risk", "imperative-bare"),
    ("invoice", "what's the late payment interest? flag concerns", "imperative-bare"),
    ("invoice", "total due and subtotal please, sanity check numbers", "imperative-bare"),
    ("invoice", "what does the paper cost per box? good price?", "fragment"),
    ("invoice", "Total? Reasonable?", "fragment"),
    ("invoice", "the late interest rate - steep?", "fragment"),
    ("invoice", "what's the grand total, okay for a dental practice?", "okay"),
    ("invoice", "what are the payment terms. ok for us?", "okay"),
    ("invoice", "who is the supplier and shall we keep using them?", "modal"),
    ("invoice", "what's the colour toner cost? would we be better off buying elsewhere?", "modal"),
    ("invoice", "what's the due date, could we get away with paying in 60 days?", "modal"),
    ("invoice", "What is the total? That seems a lot for one month to me", "view"),
    ("invoice", "what's the service visit charge, my practice manager thinks it's too much", "view"),
    ("invoice", "I need the VAT figure and I'd like to know if we can reclaim it", "desire"),
    ("invoice", "tell me the PO number, I want to know whether to query this with the supplier", "desire"),
    ("invoice", "what's the price of the paper and how does it compare with typical office prices?", "compare"),
    ("invoice", "what is the toner unit price vs what we'd pay online?", "compare"),
    ("invoice", "when's it due - pay now or at the deadline?", "decision"),
    ("invoice", "what are the line totals and which item should we cut back on?", "decision"),
    ("invoice", "what is the interest on late payment, is that even legal in the UK?", "question"),
    ("invoice", "what's the bill-to address and is anything wrong with this invoice?", "question"),
    ("invoice", "total please. worth disputing any of it?", "fragment"),
    ("invoice", "vat number and your view on whether this supplier looks legit", "desire"),
    ("invoice", "what does the service visit cost; point out anything we should push back on", "imperative-det"),
    ("invoice", "subtotal and VAT, then judge if the maths holds up", "imperative-det"),
    ("invoice", "what's the paper line total? seems high to me", "view"),
    ("invoice", "how many toner cartridges did we buy and is that a sensible quantity for 3 printers?", "question"),
    ("invoice", "what's the payment deadline, should I be worried about the interest?", "modal"),
    ("invoice", "invoice date and due date. fine by you?", "okay"),
    ("invoice", "give me the total, then recommend whether to set up a direct debit with them", "imperative-det"),
    ("invoice", "what's charged for colour toner - bargain or rip-off?", "fragment"),
    ("invoice", "what's the grand total and would you pay it as is?", "modal"),
    ("invoice", "I want the line items and I want to know if any look duplicated or padded", "desire"),
    ("invoice", "what is the total, compared to last quarter's 1,100 pounds is this a jump worth asking about?", "compare"),
    ("invoice", "what are the bank details? safe to pay to that account?", "question"),
    ("invoice", "how much VAT did they charge, right rate for these goods?", "fragment"),
    ("invoice", "service visit price and tell me if it's worth keeping the service contract", "imperative-det"),
    # --- the contract -------------------------------------------------------
    ("contract", "What is the annual fee, and is that competitive for fleet analytics?", "question"),
    ("contract", "how long is the initial term? is 36 months too long for us?", "question"),
    ("contract", "what's the notice period to stop the renewal, and is that reasonable?", "question"),
    ("contract", "what is the liability cap - does it protect us enough?", "question"),
    ("contract", "what's the price increase cap? is 9% normal?", "question"),
    ("contract", "extract the termination terms and assess the risk for us", "imperative-det"),
    ("contract", "list the fees and evaluate the overall cost over the term", "imperative-det"),
    ("contract", "what are the service credits? assess adequacy", "imperative-bare"),
    ("contract", "pull the data clauses and flag compliance issues", "imperative-bare"),
    ("contract", "governing law and payment terms, review for red flags", "imperative-bare"),
    ("contract", "SLA target? decent?", "fragment"),
    ("contract", "the implementation fee - negotiable?", "fragment"),
    ("contract", "Auto renewal terms? Customer-friendly?", "fragment"),
    ("contract", "what's the late interest, okay for us?", "okay"),
    ("contract", "who are the parties and what's the governing law, ok for a UK company?", "okay"),
    ("contract", "what's the term, and shall we sign it?", "modal"),
    ("contract", "what's the annual fee? could we negotiate it down?", "modal"),
    ("contract", "how many users are included, would we outgrow it?", "modal"),
    ("contract", "what's the liability cap? It feels low to me", "view"),
    ("contract", "what's the termination clause, our lawyer says it's one-sided", "view"),
    ("contract", "what are the payment terms? I think 45 days is generous", "view"),
    ("contract", "I need the renewal terms and I want to know if we should push for 60 days notice", "desire"),
    ("contract", "tell me the fees, I'd like your honest take on the value", "desire"),
    ("contract", "What is the SLA target, and how does it compare with what AWS or Azure offer?", "compare"),
    ("contract", "what is the liability cap vs industry standard?", "compare"),
    ("contract", "what's the annual fee - sign now or wait for a better quote?", "decision"),
    ("contract", "what's the data hosting location, and is that a problem under UK GDPR?", "question"),
    ("contract", "what's the total cost over the initial term if they apply the maximum increases, and is that affordable for a firm our size?", "question"),
    ("contract", "effective date and term. any gotchas?", "fragment"),
    ("contract", "what are the service credits and would you accept them?", "modal"),
    ("contract", "what's the data deletion period; point out the weak spots in the data section", "imperative-det"),
    ("contract", "what does clause 6 say, and should we ask for termination for convenience?", "decision"),
    ("contract", "fees and increases please, then tell me the downsides", "imperative-det"),
    ("contract", "what is the implementation fee? seems steep to me for setup", "view"),
    ("contract", "what's the credit cap, good or bad?", "fragment"),
    ("contract", "renewal notice period. thumbs up or down?", "fragment"),
    ("contract", "what's the term, fine for a startup?", "okay"),
    ("contract", "who's liable for what, and would a lawyer object to that?", "modal"),
    ("contract", "what is the annual fee and I want to know if we are overpaying", "desire"),
    ("contract", "the uptime target - better or worse than the norm?", "compare"),
    ("contract", "what's the interest on overdue amounts; negotiate or accept?", "decision"),
    ("contract", "who is the provider and should we trust a US vendor with our data?", "decision"),
    ("contract", "what's the renewal length, and is auto-renewal a trap here?", "question"),
    ("contract", "what's the user limit, is 150 enough for 220 drivers?", "question"),
    # --- the brochure -------------------------------------------------------
    ("brochure", "What's the usable capacity, and is it enough to run a bakery overnight?", "question"),
    ("brochure", "how much does it cost and is that good value for 12 kWh?", "question"),
    ("brochure", "what's the continuous output - can it handle two ovens?", "question"),
    ("brochure", "what's the warranty, is 10 years standard for home batteries?", "question"),
    ("brochure", "how heavy is it and can two people install it?", "question"),
    ("brochure", "list the specs and assess the fit for a small office", "imperative-det"),
    ("brochure", "extract the price and evaluate the payback period", "imperative-det"),
    ("brochure", "what's the efficiency? assess value", "imperative-bare"),
    ("brochure", "operating temperature range, check suitability for an unheated garage in Finland", "imperative-bare"),
    ("brochure", "give me the dimensions and weight, advise on placement", "imperative-bare"),
    ("brochure", "peak output? enough for a heat pump?", "fragment"),
    ("brochure", "Price per unit - worth it?", "fragment"),
    ("brochure", "the switchover time. fast enough for computers?", "fragment"),
    ("brochure", "what's the IP rating, okay outdoors in rain?", "okay"),
    ("brochure", "what's the max expansion? ok for a farm?", "okay"),
    ("brochure", "what's the capacity, shall we buy two?", "modal"),
    ("brochure", "how many cycles is it rated for, would it last 15 years for us?", "modal"),
    ("brochure", "what does it cost, should I wait for prices to drop?", "modal"),
    ("brochure", "what's the price? That sounds expensive to me", "view"),
    ("brochure", "what's the capacity, my installer thinks one unit is plenty", "view"),
    ("brochure", "warranty terms, I reckon 70% retention is weak", "view"),
    ("brochure", "I need the peak output and I want to know if it can start a well pump", "desire"),
    ("brochure", "tell me the efficiency, I'd like to know if it beats a Tesla Powerwall", "desire"),
    ("brochure", "What's the price and how does it compare with a Powerwall 3?", "compare"),
    ("brochure", "usable capacity vs a typical 10 kWh system - better?", "compare"),
    ("brochure", "what's the warranty - buy this or a lead-acid bank?", "decision"),
    ("brochure", "what are the dimensions, and should it go indoors or outdoors for us?", "decision"),
    ("brochure", "what's the chemistry, is LFP safe to keep in a house with kids?", "question"),
    ("brochure", "what's the output and how many units do we need for a 9 kW peak load?", "question"),
    ("brochure", "price and warranty. any catches?", "fragment"),
    ("brochure", "what's the backup mode and would you trust it for a home medical device?", "modal"),
    ("brochure", "capacity and output; point out the limits for an EV charger", "imperative-det"),
    ("brochure", "what's the weight? seems heavy for a wall mount to me", "view"),
    ("brochure", "max expansion capacity - good or bad for growth?", "fragment"),
    ("brochure", "IP rating. fine by you for a coastal site?", "okay"),
    ("brochure", "how much is it, would we be mad to buy three?", "modal"),
    ("brochure", "I want the operating temps and I want to know if it'll survive a Dubai summer outside", "desire"),
    ("brochure", "round-trip efficiency vs a pumped hydro scheme?", "compare"),
    ("brochure", "what's the price, and is it smarter to add more solar panels instead?", "decision"),
    ("brochure", "what's the capacity; tell me whether it's right for a 3-bedroom house", "imperative-det"),
    ("brochure", "what's the output; recommend a setup for a café with an espresso machine", "imperative-det"),
    ("brochure", "warranty cycles and years, tell me the downsides", "imperative-det"),
    ("brochure", "what does the brochure say about installation, and is DIY install a bad idea?", "question"),
    ("brochure", "what's the price in euros and roughly what is that in pounds, affordable for a small shop?", "question"),
)

#: PURE field asks on the same three documents: no judgement anywhere. The
#: live bar reads these for UNASKED advice. `absent` names a field the
#: document does not state, where the only right answer is "not stated".
R7_FIELD_ONLY = (
    ("invoice", "what is the invoice number and the due date?", ""),
    ("invoice", "list the line items with their quantities", ""),
    ("invoice", "what's the VAT registration number?", ""),
    ("invoice", "who is the invoice billed to?", ""),
    ("invoice", "what's the customer PO and the invoice date?", ""),
    ("invoice", "what discount was applied?", "discount"),
    ("invoice", "what are the bank details for payment?", ""),
    ("invoice", "what is the delivery address?", "delivery address"),
    ("invoice", "what's the subtotal and the VAT rate?", ""),
    ("invoice", "what's the supplier's email address?", "email"),
    ("contract", "who are the parties to this agreement?", ""),
    ("contract", "what is the effective date and the initial term?", ""),
    ("contract", "what is the annual subscription fee?", ""),
    ("contract", "what's the notice period for non-renewal?", ""),
    ("contract", "what is the governing law?", ""),
    ("contract", "what is the liability cap?", ""),
    ("contract", "what is the SLA availability target and the service credit?", ""),
    ("contract", "what is the termination fee?", "termination fee"),
    ("contract", "who signed this agreement?", "signatories"),
    ("contract", "what are the payment terms and the late interest rate?", ""),
    ("brochure", "what is the usable capacity?", ""),
    ("brochure", "what's the price per unit?", ""),
    ("brochure", "what are the dimensions and weight?", ""),
    ("brochure", "what is the warranty?", ""),
    ("brochure", "what's the round-trip efficiency?", ""),
    ("brochure", "what's the peak output and for how long?", ""),
    ("brochure", "what's the installation cost?", "installation cost"),
    ("brochure", "what is the IP rating?", ""),
    ("brochure", "what's the maximum number of units in parallel?", ""),
    ("brochure", "what certifications does it have?", "certifications"),
)
