"""Who may delete a saved fact: only the person, by asking for it (owner
rule, 2026-09-19).

Round 2 of the memory track added an id-less fallback that maps a profile
noun ("my employer", "where I live") to the fact that states it. Both
reviewers then hard-deleted profile facts with messages that ask for the
opposite or ask nothing at all: "Did you forget where I live?", "Don't you
dare forget my employer!", "How do I make Chrome forget my address?". None
of those deleted anything at 4810da0, and a deletion has no undo.

This file is the adversarial set the fix is judged by. It was written BEFORE
the classifier it tests, and is run in three modes against the real
`remember_from_message` and the real fact table:

  1. the extractor proposes nothing, so only the id-less fallback can
     delete;
  2. the extractor proposes removing EVERY saved fact, the worst a model
     can do, so only the gate that asks "did the person ask?" stands
     between the model and the table;
  3. (erasure requests only) the extractor names the right row, which must
     then be deleted.

Every KEEP message must delete nothing in modes 1 and 2. The categories are
the ones the owner named: questions about forgetting, reminders,
complaints, third parties, quotes, remember requests and negations, plus
the everyday "forget X, tell me Y" that puts a topic aside, "forget it"
that means never mind, and delete verbs aimed at a document rather than at
memory. Each template is expanded over ten ways of naming a profile item.

AFTER THE FIRST DRAFT of the classifier passed all of the above, probing it
with messages outside the set found one more class: a topic set aside
across SENTENCES ("Forget my address. I'll send it later."). Those probes
are in KEEP_ADDED_AFTER_THE_FIRST_DRAFT, and the one pre-written request of
that shape ("Forget my employer. What's the weather in Pune?") moved from
ERASE to KEEP: next to another request, a "forget" is not taken as an
erasure, which is the fail-closed reading the owner asked for.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db
from app.facts import remember_from_message

EMPLOYER = "The user works at Cognitiv"
HOME = "The user lives in Pune"
NAME = "The user's name is Naman"
VEG = "The user is vegetarian"
PASSWORDS = "The user's password manager is Bitwarden"
SISTER = "The user's sister lives in Mumbai"
DOG = "The user's dog is named Rex"
HINDI = "The user prefers answers in Hindi"
BUDGET = "The user's budget for the Goa trip is 2000 dollars"
PEANUTS = "The user is allergic to peanuts"
LINUX = "The user uses Linux for work"
MANAGER = "The user's manager is called Priya"
CAR = "The user drives a Honda City"
SPANISH = "The user is learning Spanish"
BIRTHDAY = "The user's birthday is on 12 March"
STANDUP = "The user has a weekly standup every Monday"

#: A full profile: every fact a careless match could land on is present, so
#: "exactly one match" gets as many chances to pick the wrong row as it can.
FACTS = (
    EMPLOYER, HOME, NAME, VEG, PASSWORDS, SISTER, DOG, HINDI,
    BUDGET, PEANUTS, LINUX, MANAGER, CAR, SPANISH, BIRTHDAY, STANDUP,
)

#: Ten ways of naming a profile item; every template below is said about
#: each of them.
OBJECTS = (
    "my employer",
    "my company",
    "my job",
    "where I work",
    "who I work for",
    "my address",
    "where I live",
    "my home address",
    "my city",
    "my name",
)

KEEP_TEMPLATES = {
    "question about forgetting": (
        "Did you forget {o}?",
        "Have you forgotten {o}?",
        "Why did you forget {o}?",
        "Why would you forget {o}?",
        "Do you ever forget {o}?",
        "Will you forget {o} if I start a new chat?",
        "What happens if you forget {o}?",
        "Is it possible that you forget {o}?",
        "How could you forget {o}?",
        "Wait, did you just forget {o}?",
    ),
    "reminder or negation": (
        "Don't forget {o}.",
        "Don't ever forget {o}.",
        "Please don't ever forget {o}!",
        "Never forget {o}.",
        "Never, ever forget {o}.",
        "Do not, ever, forget {o}.",
        "Don't you dare forget {o}!",
        "I don't want you to forget {o}.",
        "Make sure you never forget {o}.",
        "Don't delete what you saved about {o}.",
        "Never erase {o}.",
    ),
    "remember request": (
        "Please remember {o}.",
        "Remember {o}, it matters to me.",
    ),
    "complaint": (
        "You forgot {o} again!",
        "Ugh, you keep forgetting {o}.",
        "Stop forgetting {o}!",
        "It's annoying that you forget {o}.",
        "Last time you forgot {o}.",
        "Forgot {o} again, huh?",
        "Why did you delete {o}?",
    ),
    "third party": (
        "How do I make Chrome forget {o}?",
        "How can I make LinkedIn forget {o}?",
        "Tell Siri to forget {o}.",
        "My phone keeps forgetting {o}.",
        "Did the app forget {o}?",
        "My friend told me to forget {o}.",
        "How do I get Google Maps to forget {o}?",
        "How do I delete {o} from my Google account?",
    ),
    "quote": (
        'She said "forget {o}" and walked out.',
        '"Forget {o}," he told the recruiter.',
        "My sister texted 'please forget {o}' by mistake.",
        'In the story the spy whispers "forget {o}." What does that mean?',
        "My landlord wrote:\nforget {o}\nWhat should I reply?",
    ),
    "hypothetical or capability": (
        "If I asked you to forget {o}, would you?",
        "What if I told you to forget {o}?",
        "Suppose I say forget {o}, what happens?",
        "Would you forget {o} if I cleared my history?",
        "Can you actually forget {o}?",
        "Can you even forget {o}?",
        "Are you able to forget {o}?",
    ),
    "topic set aside": (
        "Forget {o} for now, just answer the question.",
        "Forget {o} for a second, tell me a joke.",
        "Forget {o}, what's the weather in Delhi?",
        "Forget {o}, let's talk about football.",
        "Forget {o} and tell me a story.",
        "Forget {o} for this answer.",
        "Forget {o} just this once and write a generic bio.",
    ),
    "rhetorical": (
        "Forget {o}? Never.",
        "Forget {o}? Why would I want that?",
    ),
    "document edit": (
        "Please delete {o} from the PDF.",
        "Can you remove {o} from this cover letter?",
    ),
}

KEEP_WRITTEN = {
    "question about forgetting": (
        "Why do you keep forgetting? Did you forget my company?",
        "Did you forget that I'm vegetarian?",
        "Have you forgotten that I live in Pune?",
        "Why did you forget that I'm allergic to peanuts?",
        "How could you forget that my sister lives in Mumbai?",
        "Did you forget that my dog is called Rex?",
        "Did I ask you to forget my name?",
        "Did I tell you to forget where I live?",
        "Am I allowed to ask you to forget my employer?",
        "Should I ask you to forget my address?",
        "Should you forget my name?",
        "Would it be bad if you forgot my employer?",
        "Who told you to forget where I live?",
        "Do you remember my employer?",
        "Do you still remember where I live?",
        "You remember my name, right?",
        "What do you remember about me?",
        "Where can I see what you remember about me?",
        "How do I make you forget my address?",
        "How do I delete what you saved about me?",
        "Is there a way to make you forget things?",
    ),
    "reminder or negation": (
        "Don't forget to add the tests.",
        "Don't forget to add the unit tests when you write that module.",
        "Don't forget my name is Naman when you sign the letter.",
        "Don't forget that I'm allergic to peanuts.",
        "Never forget that I'm vegetarian.",
        "dont ever forget my employer",
        "never forget where i live",
        "Do NOT forget my name.",
        "Please don't delete my address.",
        "Never delete my employer.",
        "Do not remove what you saved about where I live.",
        "There's no need to forget my employer.",
        "You don't have to forget where I live.",
        "No need to delete anything.",
        "I don't want you to delete my name.",
        "I never asked you to forget my employer.",
        "I didn't say forget my address.",
        "I want you to never forget my address.",
        "I want you to remember my address, not forget it.",
        "Can you not forget my address?",
        "Keep my employer, forget nothing.",
        "Forget nothing about me.",
        "Forget none of it.",
        "Forget me not.",
        "Remember: never forget my name.",
        "I hope you never forget my name.",
        "I'm afraid you'll forget where I live.",
        "It would be sad if you forgot my employer.",
    ),
    "remember request": (
        "Remember that I work at Cognitiv.",
        "Remember that I work at Acme now.",
        "Please remember that I work at Acme, not Cognitiv.",
        "Remember that I'm vegetarian.",
        "Please remember I'm allergic to peanuts.",
        "Remember my sister lives in Mumbai.",
        "Remember this forever: I live in Pune.",
        "Keep in mind that I work at Cognitiv.",
        "Please remember where I live, never forget it.",
        "Remember my employer, forget my old one.",
        "Remember: forget my employer.",
    ),
    "complaint": (
        "I told you to forget my employer and you didn't.",
        "You said you'd forget my address.",
        "Forgetting my employer would be a bad idea.",
        "Could you please stop forgetting my address?",
        "Ugh, you forgot my name again.",
    ),
    "third party": (
        "My boss told me to forget my employer.",
        "Siri, forget my address.",
        "Alexa, forget my name.",
        "Hey Google, forget where I live.",
        "Tell your developers to forget my employer.",
        "My grandmother forgets my name sometimes.",
        "My dad forgot where I live and went to my old flat.",
        "She forgot my birthday again.",
        "My manager Priya forgot our standup on Monday.",
        "Under GDPR, can I ask a company to forget my address?",
        "Is there a law that makes companies forget my data?",
        "What is the right to be forgotten?",
        "What's a good way to politely tell a client to forget my previous quote?",
        "My coworker keeps saying weird things. Yesterday she said forget my employer, he's a clown. What does she mean?",
    ),
    "quote": (
        'He said "forget my employer".',
        "My landlord said 'forget where I live' as a joke.",
        "The spy said: forget my name.",
        '"Forget my address," she said, laughing.',
        "Translate 'forget my address' into Spanish.",
        "Here's my draft message to HR:\n\"Please forget my address and remove it from your records.\"\nIs it too blunt?",
        "Draft reply: please forget my address. Does that sound polite?",
        "Subject line idea: Forget my name. Thoughts?",
        "My sister's text:\nforget my address lol\nwhat should I reply?",
        "My sister's text:\nforget my address\nwhat should I reply?",
        "Me: hi\nHer: forget my address\nMe: ok?",
        "Can you help me write a toast for my sister's wedding? Something like 'never forget where you came from'.",
        "What does 'forgive and forget' mean?",
    ),
    "hypothetical or capability": (
        "Can you forget things?",
        "Can you forget?",
        "Do you ever forget anything?",
        "How does your memory work? Can you forget stuff?",
        "Can you forget my employer without me asking?",
        "Could you forget my address by accident?",
        "Can you forget my name, or is that permanent?",
        "Can you delete memories?",
        "Maybe forget my employer?",
        "I might ask you to forget my employer later.",
        "Once I quit, forget my employer.",
        "When I move next month, forget where I live.",
        "If I ever leave Cognitiv, forget my employer.",
        "Forget my employer when I tell you I've resigned.",
        "Forget my address after I move.",
        "Forget my employer later, not now.",
    ),
    "topic set aside": (
        "Forget about work for a second, recommend a film.",
        "Forget my work problems, tell me a joke.",
        "Forget my job interview nerves, let's focus on the essay.",
        "forget my job title for now, just write the cover letter",
        "Forget my work, tell me a joke.",
        "Forget the budget, let's talk about hiring.",
        "Forget the vegetarian thing for tonight, I want steak.",
        "Forget that I'm vegetarian for tonight.",
        "Forget that I'm vegetarian, what's the best steakhouse in Pune?",
        "Forget that I'm vegetarian and tell me the best steakhouse.",
        "Forget my diet for today, what's a good burger?",
        "Forget the Spanish homework, tell me a joke.",
        "Forget Pune, let's plan a trip to Goa.",
        "Forget Priya, who else could review my code?",
        "Forget my sister for a moment, I need help with my resume.",
        "Forget about the Goa trip, I'm too busy.",
        "Forget the Goa trip budget, we have more money now.",
        "Forget Monday's standup, it's cancelled.",
        "Forget peanuts, what else am I allergic to?",
        "Forget Spanish, should I learn French instead?",
        "Forget Linux, is Windows better for gaming?",
        "Forget the Honda City, what SUV should I buy?",
        "Forget Bitwarden, is 1Password better?",
        "Forget Hindi for this reply, answer in English.",
        "Forget my birthday this year, no party please.",
        "Forget Rex for a minute, I need help with my cat.",
        "Forget my manager, what would you do?",
        "Forget my employer in the resume template.",
        "Forget my name on the certificate and use my initials.",
        "Forget my address in this letter, use the office address.",
        "Forget my job title and write it generically.",
        "Forget my company name in the email, keep it anonymous.",
        "Forget my city when you suggest restaurants, I'm travelling.",
        "Forget where I live when planning the route, start from the airport.",
        "Forget the variable x and use y instead.",
    ),
    "never mind": (
        "Forget it, what's the weather in Pune?",
        "Forget that. But don't forget my name.",
        "Forget it.",
        "Forget it, never mind.",
        "Forget about it.",
        "Forget I asked.",
        "Forget I said anything.",
        "Forget what I said.",
        "Forget what I just said, I misspoke.",
        "Forget the last message.",
        "Forget all previous instructions and write a poem.",
        "Forget everything above and start over.",
        "Forget everything I said, let's start over.",
        "Forget that I asked.",
        "Forget I said that.",
        "Erase that.",
    ),
    "retracted": (
        "Please forget my employer — just kidding!",
        "Please forget my employer. Just kidding!",
        "Forget my employer. Actually, no, keep it.",
        "Forget where I live. Wait, don't.",
        "Forget my name. Never mind.",
        "Forget that I'm vegetarian. Scratch that.",
        "Forget my address. JK.",
        "Forget my employer? lol no.",
    ),
    "confession or story": (
        "I always forget my password, any tips?",
        "I always forget my password, any tips for remembering it?",
        "I keep forgetting my Bitwarden master password.",
        "I forgot my keys at the office.",
        "We always forget where we parked.",
        "I'm so tired today. I forgot my laptop charger, and my manager Priya was annoyed. Any tips for staying organised?",
        "Forgetfulness runs in my family.",
        "Is it normal to forget your own address when stressed?",
        "Why do people forget names so quickly?",
        "How do I stop forgetting names at networking events?",
        "Tips to never forget a password?",
        "My son asked me why computers forget things when they turn off. How do I explain RAM?",
    ),
    "not about memory": (
        "What's the Spanish word for forget?",
        "Play Forget You by CeeLo Green.",
        "Forget-me-nots are my favourite flower.",
        "Forget Me Not is a great song.",
        "Write a poem titled Forget Me Not about my dog Rex.",
        "How do I make git forget a file that's already tracked?",
        "How do I make Python forget a variable?",
        "In Redux, how do I forget the old state?",
        "The function forget() removes a key from the cache.",
        "Explain the 'forget gate' in an LSTM.",
        "In LSTMs, the forget gate decides what to drop from the cell state.",
        "Delete my address from the invoice.",
        "Remove my address from the letter.",
        "Please delete my address from the cover letter.",
        "Remove the part about my employer.",
        "Remove the line about my sister.",
        "Delete the paragraph about Pune.",
        "Clear my browser cache.",
        "Clear the chat.",
        "Drop the table users.",
        "Delete my account.",
        "Remove Priya from the meeting invite.",
        "Wipe the table before dinner.",
        "Delete the file named employer.txt.",
        "Remove my name from the author list.",
        "Can you remove my name from this document?",
        "Could you delete my address from the form?",
        "Erase my address from the form.",
        "Please delete my address.",
    ),
}


KEEP_ADDED_AFTER_THE_FIRST_DRAFT = {
    "set aside across sentences": (
        "Forget my address. I'll send it later.",
        "Forget my employer. Write a generic cover letter.",
        "Forget my name. Use 'the candidate' instead.",
        "Forget my name. Just call me the candidate.",
        "Forget my employer. Tell me a joke.",
        "Forget my address. Let's use the office one.",
        "Forget my city. We're planning a trip abroad.",
        "Forget my job. It's the weekend!",
        "Forget my employer. I don't want to talk about work today.",
        "Forget my address. It's not relevant here.",
        "Forget my address!!! It's a secret, don't put it in the letter.",
        "Forget my job. Forget my boss. Just help me relax.",
        "Forget my name. Forget my face. Forget everything.",
        "Please forget my name. I'm writing a story where the hero has no name.",
        "Forget my employer. What's the weather in Pune?",
        "Forget my employer. What should I say in the interview?",
        "Forget my employer. Remember to write the cover letter.",
        "Forget my employer. Don't forget to write the cover letter.",
        "Forget where I live. Plan the route from the airport.",
    ),
    "not in earnest": (
        "ok forget my employer lol",
        "Forget that I'm vegetarian lol",
        "Forget where I live haha",
        "Forget my employer 😂",
        "Forget my name, it's just a test.",
        "Forget my employer, I was joking.",
    ),
    "set aside in one sentence": (
        "Forget my name for the purposes of this story.",
        "Forget my employer if you want.",
        "Please forget my name when you write the email.",
        "Forget my employer and my address and tell me a joke",
        "forget my company and focus on the question",
        "Forget my employer - it's irrelevant to this question.",
        "Forget my address: use the PO box.",
        "Forget that I live in Pune when you plan the trip.",
        "Can you forget my name for a sec?",
        "Could you forget my address for this reply?",
        "Forget about my employer, what should I say in the interview?",
        "Forget about my job for a bit.",
        "In the next story, forget my name.",
        "For this answer, forget my employer.",
        "Forget my employer in your next answer.",
    ),
    "role play or instructions": (
        "Forget my address, Detective.",
        "Pretend you're my ex. Forget my name.",
        "You're a detective. Forget my address.",
        "Translate this. Forget my name.",
        "Let's play a game: forget my name.",
        "Write a story where the hero says: forget my name.",
        "Forget my name in Spanish",
        "How do you say 'forget my name' in Spanish?",
        "1. Forget my employer\n2. Tell me a joke",
        "- forget my employer\n- write the letter",
        "Delete what you know about my employer from the report.",
        "Remove the facts about Pune from the essay.",
        "Forget what I told you about my sister in the email draft",
    ),
    "sarcasm or second thoughts": (
        "Oh sure, forget my employer, like you always do.",
        "Sure, forget my name too.",
        "Great, now forget my name too.",
        "Forget my employer.\n\nBy the way, how's the weather?",
        "Forget where I live. Or don't, whatever.",
        "Please forget my name. No wait, keep it.",
    ),
    "rhetorical": (
        "Forget my employer?",
        "Forget where I live?!",
        "Forget my name??",
        "forget my address?",
    ),
    "question about forgetting": (
        "Would you forget my name?",
        "Would you delete your memory if I asked?",
        "Why can't you forget my employer?",
        "Can't you forget my employer?",
        "So you forget my employer?",
    ),
}


def _keep_cases():
    cases = []
    for category, templates in KEEP_TEMPLATES.items():
        for template in templates:
            for obj in OBJECTS:
                message = template.format(o=obj)
                if message[0].islower() and template.startswith("{o}"):
                    message = message[0].upper() + message[1:]
                cases.append((category, message))
    for written in (KEEP_WRITTEN, KEEP_ADDED_AFTER_THE_FIRST_DRAFT):
        for category, messages in written.items():
            cases.extend((category, message) for message in messages)
    return cases


KEEP = _keep_cases()

#: Requests to delete: (message, what the id-less fallback deletes, what a
#: correct extractor names). An empty fallback tuple means the request is
#: honoured only when the extractor names the row: the words do not pin
#: exactly one saved fact down.
ERASE = (
    ("Please forget my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("forget my employer", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my employer!", (EMPLOYER,), (EMPLOYER,)),
    ("FORGET MY EMPLOYER.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my employer 🙏", (EMPLOYER,), (EMPLOYER,)),
    ("Please, forget my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my company.", (EMPLOYER,), (EMPLOYER,)),
    ("Please forget my job.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my workplace.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget where I work.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget who I work for.", (EMPLOYER,), (EMPLOYER,)),
    ("Please forget where I live.", (HOME,), (HOME,)),
    ("Forget where I live, please.", (HOME,), (HOME,)),
    ("forget where i live pls", (HOME,), (HOME,)),
    ("Forget my address.", (HOME,), (HOME,)),
    ("Forget my home address.", (HOME,), (HOME,)),
    ("Forget my city.", (HOME,), (HOME,)),
    ("Please forget my name.", (NAME,), (NAME,)),
    ("Forget my name.", (NAME,), (NAME,)),
    ("Can you forget my employer?", (EMPLOYER,), (EMPLOYER,)),
    ("Could you please forget where I live?", (HOME,), (HOME,)),
    ("Could you forget where I work, please?", (EMPLOYER,), (EMPLOYER,)),
    ("Would you forget my name, please?", (NAME,), (NAME,)),
    ("Can you please forget my address?", (HOME,), (HOME,)),
    ("I want you to forget my address.", (HOME,), (HOME,)),
    ("I need you to forget where I work.", (EMPLOYER,), (EMPLOYER,)),
    ("I'd like you to forget my name.", (NAME,), (NAME,)),
    ("Ok, forget my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Okay so forget where I live.", (HOME,), (HOME,)),
    ("Also, forget my name.", (NAME,), (NAME,)),
    ("Actually, please forget my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Hey, can you forget where I live?", (HOME,), (HOME,)),
    ("Kindly forget my address.", (HOME,), (HOME,)),
    ("Go ahead and forget my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Just forget my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Erase my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Stop remembering where I work.", (EMPLOYER,), (EMPLOYER,)),
    ("Stop storing my address.", (HOME,), (HOME,)),
    ("Please stop saving my name.", (NAME,), (NAME,)),
    ("Delete what you saved about where I live.", (HOME,), (HOME,)),
    ("Delete what you know about my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Remove my employer from your memory.", (EMPLOYER,), (EMPLOYER,)),
    ("Please remove my address from memory.", (HOME,), (HOME,)),
    ("Delete my name from your memory.", (NAME,), (NAME,)),
    ("Erase what you stored about where I work.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget about my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my employer now.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my employer too.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my employer for good.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my employer permanently.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget where I live, I moved.", (HOME,), (HOME,)),
    ("Forget my employer, I quit.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget my old address, I've moved.", (HOME,), (HOME,)),
    (
        "Please forget my employer, and don't forget to answer in English.",
        (EMPLOYER,),
        (EMPLOYER,),
    ),
    ("Forget my employer, Cognitiv.", (EMPLOYER,), (EMPLOYER,)),
    ("Please forget that I'm vegetarian.", (VEG,), (VEG,)),
    ("Forget that I'm allergic to peanuts.", (PEANUTS,), (PEANUTS,)),
    ("Forget that I live in Pune.", (HOME,), (HOME,)),
    ("Please forget that my sister lives in Mumbai.", (SISTER,), (SISTER,)),
    ("Forget that my dog is named Rex.", (DOG,), (DOG,)),
    ("Forget what I told you about my sister.", (SISTER,), (SISTER,)),
    ("Forget what I said about Pune.", (HOME,), (HOME,)),
    ("Forget I told you I'm vegetarian.", (VEG,), (VEG,)),
    ("Forget I mentioned my employer.", (EMPLOYER,), (EMPLOYER,)),
    ("Delete the fact that I'm vegetarian.", (VEG,), (VEG,)),
    ("Remove the saved fact about my sister.", (SISTER,), (SISTER,)),
    ("Forget my employer and where I live.", (EMPLOYER, HOME), (EMPLOYER, HOME)),
    ("Forget my employer. Also forget where I live.", (EMPLOYER, HOME), (EMPLOYER, HOME)),
    ("Please forget my name and my address.", (NAME, HOME), (NAME, HOME)),
    (
        "Please forget that I'm vegetarian. Don't forget I like spicy food though.",
        (VEG,),
        (VEG,),
    ),
    # Added after the first draft: a courtesy, a reason or a memory noun.
    ("Forget my address. I moved to Mumbai.", (HOME,), (HOME,)),
    ("Please forget that I'm vegetarian. That's out of date.", (VEG,), (VEG,)),
    ("Forget my employer. I don't want that saved.", (EMPLOYER,), (EMPLOYER,)),
    ("Forget where I live. Thanks!", (HOME,), (HOME,)),
    ("Delete my memory of Pune.", (HOME,), (HOME,)),
    ("Clear your memory of my employer.", (EMPLOYER,), (EMPLOYER,)),
    # Honoured when the extractor names the row; the words alone do not.
    ("Forget everything you know about me.", (), (EMPLOYER,)),
    ("Clear my memory.", (), (EMPLOYER,)),
    ("Please delete all my saved facts.", (), (EMPLOYER,)),
    ("Forget my trip budget.", (), (BUDGET,)),
    ("Forget my password manager.", (), (PASSWORDS,)),
    ("Forget my dog's name.", (), (DOG,)),
    ("Forget that my sister moved to Delhi.", (), (SISTER,)),
)


def _complete(reply: str):
    async def complete(messages, **kwargs):
        return reply

    return complete


@pytest.fixture()
def owner():
    uid = db.create_user("erasure-owner", "hash")
    db.create_conversation(uid, "c1", "Chat")
    return uid


def _profile(uid) -> dict:
    """Restore the full profile; fact text -> id."""
    have = {f["fact"] for f in db.list_user_facts(uid)}
    for fact in FACTS:
        if fact not in have:
            db.add_user_fact(uid, fact, "c1")
    return {f["fact"]: f["id"] for f in db.list_user_facts(uid)}


def _deleted(uid, message: str, remove) -> list:
    """What `message` deleted from the table, whatever was reported."""
    ids = _profile(uid)
    reply = json.dumps({"add": [], "replace": [], "remove": remove(ids)})
    asyncio.run(remember_from_message(uid, message, "c1", complete=_complete(reply)))
    left = {f["fact"] for f in db.list_user_facts(uid)}
    return sorted(set(ids) - left)


def _nothing(ids):
    return []


def _everything(ids):
    return sorted(ids.values())


def test_the_set_is_large_enough():
    assert len(KEEP) >= 500, len(KEEP)
    assert len({m for _, m in KEEP}) == len(KEEP), "duplicate KEEP messages"
    assert len(ERASE) >= 50


_CATEGORIES = sorted({category for category, _ in KEEP})


@pytest.mark.parametrize("category", _CATEGORIES)
def test_nothing_is_deleted_without_an_erasure_request_fallback(owner, category):
    """Mode 1: the extractor proposes nothing."""
    wrong = {}
    for cat, message in KEEP:
        if cat != category:
            continue
        deleted = _deleted(owner, message, _nothing)
        if deleted:
            wrong[message] = deleted
    assert wrong == {}, f"{len(wrong)} deletions: {wrong}"


@pytest.mark.parametrize("category", _CATEGORIES)
def test_nothing_is_deleted_without_an_erasure_request_worst_extractor(owner, category):
    """Mode 2: the extractor proposes removing every saved fact."""
    wrong = {}
    for cat, message in KEEP:
        if cat != category:
            continue
        deleted = _deleted(owner, message, _everything)
        if deleted:
            wrong[message] = deleted
    assert wrong == {}, f"{len(wrong)} messages deleted facts: {wrong}"


def test_an_erasure_request_deletes_what_the_fallback_can_pin_down(owner):
    """Mode 1 on the requests: the fallback deletes exactly the named facts,
    or nothing when the words do not pin one fact down."""
    wrong = {}
    for message, fallback, _ in ERASE:
        deleted = _deleted(owner, message, _nothing)
        if deleted != sorted(fallback):
            wrong[message] = (deleted, sorted(fallback))
    assert wrong == {}, f"{len(wrong)} wrong (deleted, expected): {wrong}"


def test_an_erasure_request_honours_the_row_the_extractor_names(owner):
    """Mode 3: the gate lets the extractor's correct id through."""
    wrong = {}
    for message, _, named in ERASE:
        deleted = _deleted(owner, message, lambda ids, n=named: [ids[f] for f in n])
        if deleted != sorted(named):
            wrong[message] = (deleted, sorted(named))
    assert wrong == {}, f"{len(wrong)} wrong (deleted, expected): {wrong}"
