#!/usr/bin/env python3
"""Deterministic, SYNTHETIC fixtures for the QA baseline (no user data).

  fixtures/customers-100.csv   same 12 columns as the public "customers-100"
                               sample the owner's Chat A used, 100 rows,
                               skewed countries + 3 years of subscription dates
  fixtures/sales-2025.csv      a numeric table (month, region, product, units,
                               revenue, cost) — 144 rows — for sum/avg charts
  fixtures/incidents-2025.csv  a table with a STATUS column whose values are
                               style.STATUS_VALUES words (resolved / open /
                               in progress / failed), 180 rows — the colour
                               case (K01) needs a status chart and two
                               different chart subjects
  fixtures/truth.json          ground truth the checks compare against
                               (counts per country/year, sums per region …)

Run once; the harness calls ensure() before a run.
"""
from __future__ import annotations

import csv
import json
import os
import random
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")

FIRST = ["Aarav", "Maya", "Liam", "Sofia", "Noah", "Zara", "Ethan", "Priya", "Lucas", "Chloe", "Omar", "Hana",
         "Mateo", "Isla", "Ravi", "Emma", "Kenji", "Leah", "Diego", "Nina"]
LAST = ["Shah", "Patel", "Nguyen", "Garcia", "Smith", "Kim", "Rossi", "Müller", "Silva", "Brown", "Khan", "Lopez",
        "Tanaka", "Walker", "Mehta", "Dubois"]
COMPANY = ["Blue Harbor Ltd", "Nimbus Analytics", "Kestrel Foods", "Orchid Health", "Vertex Logistics",
           "Sparrow Retail", "Summit Energy", "Lumen Media", "Granite Works", "Pioneer Labs"]
COUNTRIES = [("India", 22), ("United States", 18), ("United Kingdom", 12), ("Germany", 10), ("Brazil", 9),
             ("Japan", 8), ("Canada", 7), ("Australia", 6), ("France", 5), ("South Africa", 3)]
CITIES = {"India": ["Mumbai", "Ahmedabad", "Bengaluru"], "United States": ["Austin", "Denver", "Boston"],
          "United Kingdom": ["Leeds", "Bristol"], "Germany": ["Berlin", "Munich"], "Brazil": ["Recife", "Curitiba"],
          "Japan": ["Osaka", "Sapporo"], "Canada": ["Calgary", "Halifax"], "Australia": ["Perth", "Hobart"],
          "France": ["Lyon", "Nantes"], "South Africa": ["Durban"]}


def customers(rng: random.Random):
    countries = [c for c, n in COUNTRIES for _ in range(n)]
    rng.shuffle(countries)
    rows = []
    for i in range(100):
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        country = countries[i]
        year = rng.choices([2020, 2021, 2022], weights=[25, 35, 40])[0]
        month, day = rng.randint(1, 12), rng.randint(1, 28)
        company = rng.choice(COMPANY)
        rows.append({
            "Index": i + 1,
            "Customer Id": "".join(rng.choice("0123456789ABCDEFabcdef") for _ in range(15)),
            "First Name": fn, "Last Name": ln, "Company": company,
            "City": rng.choice(CITIES[country]), "Country": country,
            "Phone 1": f"+{rng.randint(1, 99)}-{rng.randint(200, 999)}-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
            "Phone 2": f"({rng.randint(200, 999)}){rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
            "Email": f"{fn.lower()}.{ln.lower()}{i}@example.com".replace("ü", "u"),
            "Subscription Date": f"{year}-{month:02d}-{day:02d}",
            "Website": f"https://www.{company.split()[0].lower()}{rng.randint(1, 99)}.example.com",
        })
    return rows


def sales(rng: random.Random):
    rows = []
    regions = {"North": 1.3, "South": 0.8, "East": 1.0, "West": 1.6}
    products = {"Starter": 49.0, "Pro": 129.0, "Enterprise": 499.0}
    for m in range(1, 13):
        for region, rmul in regions.items():
            for product, price in products.items():
                season = 1.0 + 0.25 * (m in (10, 11, 12)) - 0.15 * (m in (6, 7))
                units = max(1, int(rng.gauss(40 if product != "Enterprise" else 6, 5) * rmul * season))
                revenue = round(units * price, 2)
                cost = round(revenue * rng.uniform(0.45, 0.7), 2)
                rows.append({"Month": f"2025-{m:02d}", "Region": region, "Product": product,
                             "Units": units, "Revenue": revenue, "Cost": cost})
    return rows


TEAMS = ["Platform", "Payments", "Search", "Mobile"]
SERVICES = ["checkout-api", "search-index", "auth-service", "mobile-gateway", "billing-worker"]
#: words style.STATUS_VALUES classifies as success / info / warning / danger
STATUSES = [("Resolved", 90), ("Open", 34), ("In Progress", 30), ("Failed", 26)]
SEVERITIES = ["Low", "Medium", "High"]


def incidents(rng: random.Random):
    """A status table: the colour case needs a chart whose CATEGORIES are
    status words, plus a second subject (hours lost) over the same rows."""
    statuses = [s for s, n in STATUSES for _ in range(n)]
    rng.shuffle(statuses)
    rows = []
    for i in range(len(statuses)):
        month, day = rng.randint(1, 12), rng.randint(1, 28)
        severity = rng.choices(SEVERITIES, weights=[45, 35, 20])[0]
        hours = round(abs(rng.gauss({"Low": 1.5, "Medium": 4.0, "High": 9.0}[severity], 1.2)) + 0.2, 1)
        rows.append({
            "Incident Id": f"INC-{1000 + i}",
            "Opened": f"2025-{month:02d}-{day:02d}",
            "Team": TEAMS[i % len(TEAMS)] if i % 7 else rng.choice(TEAMS),
            "Service": rng.choice(SERVICES),
            "Severity": severity,
            "Status": statuses[i],
            "Hours Lost": hours,
        })
    return rows


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def truth(cust, sal, inc):
    by_country = Counter(r["Country"] for r in cust)
    by_year = Counter(r["Subscription Date"][:4] for r in cust)
    by_company = Counter(r["Company"] for r in cust)
    rev_region = defaultdict(float)
    rev_month = defaultdict(float)
    units_product = defaultdict(int)
    for r in sal:
        rev_region[r["Region"]] += r["Revenue"]
        rev_month[r["Month"]] += r["Revenue"]
        units_product[r["Product"]] += r["Units"]
    inc_team = Counter(r["Team"] for r in inc)
    inc_status = Counter(r["Status"] for r in inc)
    inc_severity = Counter(r["Severity"] for r in inc)
    hours_team = defaultdict(float)
    for r in inc:
        hours_team[r["Team"]] += r["Hours Lost"]
    return {
        "customers-100.csv": {
            "rows": len(cust),
            "columns": list(cust[0].keys()),
            "count_by": {"Country": dict(by_country), "Company": dict(by_company),
                         "Subscription Year": dict(by_year)},
            "numeric_columns": ["Index"],
        },
        "sales-2025.csv": {
            "rows": len(sal),
            "columns": list(sal[0].keys()),
            "sum_by": {"Region": {"Revenue": {k: round(v, 2) for k, v in rev_region.items()}},
                       "Month": {"Revenue": {k: round(v, 2) for k, v in rev_month.items()}},
                       "Product": {"Units": dict(units_product)}},
            "total_revenue": round(sum(r["Revenue"] for r in sal), 2),
            "numeric_columns": ["Units", "Revenue", "Cost"],
        },
        "incidents-2025.csv": {
            "rows": len(inc),
            "columns": list(inc[0].keys()),
            "count_by": {"Team": dict(inc_team), "Status": dict(inc_status), "Severity": dict(inc_severity)},
            "sum_by": {"Team": {"Hours Lost": {k: round(v, 1) for k, v in hours_team.items()}}},
            "numeric_columns": ["Hours Lost"],
        },
    }


def ensure(force: bool = False) -> None:
    os.makedirs(FIX, exist_ok=True)
    names = ("customers-100.csv", "sales-2025.csv", "incidents-2025.csv", "truth.json")
    if not force and all(os.path.exists(os.path.join(FIX, n)) for n in names):
        return
    # customers and sales draw from one stream, in this order: the baseline run
    # (runs/baseline-20260917) is scored against these exact bytes, so a later
    # fixture must not disturb them. incidents has its own stream.
    rng = random.Random(20260917)
    cust, sal = customers(rng), sales(rng)
    inc = incidents(random.Random(20260918))
    write_csv(os.path.join(FIX, "customers-100.csv"), cust)
    write_csv(os.path.join(FIX, "sales-2025.csv"), sal)
    write_csv(os.path.join(FIX, "incidents-2025.csv"), inc)
    with open(os.path.join(FIX, "truth.json"), "w", encoding="utf-8") as fh:
        json.dump(truth(cust, sal, inc), fh, indent=1)


if __name__ == "__main__":
    ensure(force=True)
    print(open(os.path.join(FIX, "truth.json")).read()[:1500])
