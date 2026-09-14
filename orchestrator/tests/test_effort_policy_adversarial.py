"""Adversarial cases for the Fast adaptive-thinking classifier (verifier, 2026-09-15).

Written blind by the verifier, not by the classifier's author: 20 must-think and 20
must-not prompts across English, Hindi (Devanagari), Gujarati and Hinglish, plus a
stress set of number-heavy LOOKUPS and near-miss wording (rates, prices, "faster
than", clock hands in CSS, "labels wrong" about a product) that must stay direct.

Scored blind against the author's patch: must-think 12/20 found, must-not 0/20
fired, stress 4/22 fired (rates and prices with two numbers). The classifier was
then extended for exactly these; this file pins that, so they are NOT held out.
"""
from __future__ import annotations

import pytest

from app.core import effort_policy

MUST_THINK = [
 "A farmer has 17 sheep. All but 9 run away. How many are left?",
 "If 3 cats catch 3 mice in 3 minutes, how long do 100 cats take to catch 100 mice?",
 "Ek dukaandaar ne 800 ka saaman 20% discount pe becha aur phir bhi 10% munafa kamaya. Cost price kya thi?",
 "ek ghadi mein 3 baje ghante aur minute ki sui ke beech kitna angle hota hai?",
 "एक पिता की उम्र अपने बेटे की उम्र की तीन गुनी है। 10 वर्ष बाद वह दुगनी होगी। दोनों की वर्तमान उम्र ज्ञात करें।",
 "એક ટાંકી 6 કલાકમાં ભરાય છે અને બીજી નળીથી 8 કલાકમાં ખાલી થાય છે. બંને સાથે ખુલ્લા હોય તો ટાંકી કેટલા કલાકમાં ભરાશે?",
 "I have a 5 litre can and a 3 litre can. How do I get exactly 4 litres?",
 "My recursive fibonacci in python returns None for n=5, here is the code:\ndef fib(n):\n    if n < 2:\n        return n\n    fib(n-1) + fib(n-2)",
 "Is 1001 prime? explain",
 "Three boxes are labelled apples, oranges and mixed, and every label is wrong. You can pick one fruit from one box. Which box do you pick from to relabel all of them?",
 "A bat and a ball cost 1.10 in total. The bat costs 1.00 more than the ball. How much does the ball cost?",
 "What is the sum of all integers from 1 to 1000 that are divisible by 3 or 5?",
 "Why exactly does the Monty Hall switch strategy win 2/3 of the time?",
 "Amit Ravi se lamba hai, Ravi Sohan se lamba hai. Kya Sohan Amit se lamba hai? logic samjhao",
 "If today is Wednesday, what day will it be after 100 days?",
 "sabse chhoti sankhya batao jo 12, 18 aur 30 se poori tarah divisible ho",
 "મારી પાસે 2 દીકરા અને 3 દીકરી છે, દરેક દીકરીને 2 ભાઈ છે. ઘરમાં કુલ કેટલા બાળકો છે?",
 "Water and milk are mixed in the ratio 3:2 in a 40 litre can. How much water must be added to make it 1:1?",
 "This SQL returns duplicate rows, why? SELECT o.id, c.name FROM orders o JOIN customers c ON c.region = o.region",
 "Two trains 150 m and 100 m long run at 60 km/h and 40 km/h in opposite directions. How long do they take to cross each other?",
]

MUST_NOT = [
 "what is 4G vs 5G difference",
 "top 5 IPL teams 2025 by titles",
 "Samsung Galaxy S24 Ultra price in India 128GB",
 "GST rate on gold jewellery 2025",
 "Nifty 50 closing value today",
 "1 USD to INR aaj ka rate",
 "Write a 300 word essay on the 1947 partition",
 "translate: a train leaves at 5 pm and travels 60 km per hour",
 "summarise this: Revenue grew 12% to 4,500 crore in Q2 FY25 while margins fell 150 bps",
 "kem cho, majama?",
 "PM Kisan 19th installment kab aayega?",
 "Class 10 CBSE result 2025 date",
 "what is the population of India in 2024",
 "iPhone 16 Pro vs 15 Pro camera comparison",
 "Top 10 richest people in the world 2025 list",
 "how many states are there in India",
 "how old is Virat Kohli",
 "मौसम आज दिल्ली में कैसा रहेगा? तापमान कितना है?",
 "અમદાવાદ થી સુરત કેટલા કિલોમીટર છે?",
 "Why did the Roman Empire fall? explain exactly the main causes",
]

LOOKUP_STRESS = ["Is Virat Kohli older than Rohit Sharma?",
"Which phone is the fastest right now, and is it faster than the iPhone 15 and lighter than the S24?",
"Tata Motors share price 2 saal pehle kitna tha aur ab kitna hai",
"what does LCM mean in maths",
"SQL query to select top 10 customers from orders table",
"मेरी उम्र कितनी होनी चाहिए सरकारी नौकरी के लिए, तीन साल की छूट मिलती है क्या?",
"IPL 2024 final ma CSK e ketla run banavya ane ketli wicket padi",
"Why? Just why is the sky blue",
"Mere bhai ki umar kya honi chahiye army join karne ke liye",
"What is the interest rate on SBI FD for 5 years and 3 years",
"explain what SELECT * FROM users does",
"best laptop under 50000 with 16GB RAM and 512GB SSD",
"Delhi to Mumbai train 12952 timing and fare 3AC",
"आज सोने का भाव 10 ग्राम कितना है और चांदी 1 किलो कितनी है",
"એક તોલા સોનાનો ભાવ આજે કેટલો છે? 22 કેરેટ અને 24 કેરેટ",
"how many calories in 2 eggs and 1 banana",
"distance between earth and moon in km",
"Rohit is taller than Virat. Write a funny poem about it",
"What's the capital gains tax on 10 lakh profit from shares held 2 years",
"convert 5 feet 8 inches to cm",
"My code has hour and minute hands drawn with CSS, how do I style them",
"kya ye dono labels wrong hain? product label aur price label",
]


def test_the_set_is_forty_prompts_in_every_language():
    assert len(MUST_THINK) == 20 and len(MUST_NOT) == 20
    joined = " ".join(MUST_THINK + MUST_NOT)
    assert any("\u0900" <= ch <= "\u097f" for ch in joined)
    assert any("\u0a80" <= ch <= "\u0aff" for ch in joined)


@pytest.mark.parametrize("prompt", MUST_THINK)
def test_adversarial_reasoning_prompts_think(prompt):
    decision = effort_policy.classify(prompt)
    assert decision.think, (decision.reason, decision.signals)
    assert decision.reason in effort_policy.REASONS


@pytest.mark.parametrize("prompt", MUST_NOT + LOOKUP_STRESS)
def test_adversarial_non_reasoning_prompts_stay_direct(prompt):
    decision = effort_policy.classify(prompt)
    assert not decision.think, (decision.reason, decision.signals)
