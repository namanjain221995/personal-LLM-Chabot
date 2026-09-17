"""Long synthetic inputs for the formatting-heavy cases (no user data).

JD_SAMPLE + JD_TARGET reproduce the SHAPE of the owner's Chat B: a structured
sample requirement, an instruction to rewrite a second, plain-lines job
description "in the same way", then the plain lines.
"""

JD_SAMPLE = """Data Engineer - Northwind Consumer Goods
Location: Pune (Hybrid, 3 days onsite)
Experience: 6-9 years
Employment Type: Full-time
Notice Period: Immediate to 30 days

Top Skills
Python, PySpark, SQL
Azure Data Factory, Databricks
Delta Lake, dbt
CI/CD with Azure DevOps

Summary
We are looking for a Data Engineer who designs and runs batch and streaming pipelines for our sales and supply-chain analytics platform. You will own ingestion from SAP and retail POS feeds, model curated layers for BI, and keep data quality measurable.

Key Responsibilities
Pipeline Development
Build and maintain ingestion pipelines in Azure Data Factory and Databricks.
Implement incremental loads and change data capture from SAP ECC.
Data Modelling
Design medallion (bronze/silver/gold) layers in Delta Lake.
Publish dimensional models consumed by Power BI.
Quality and Operations
Define data quality checks and alerting with Great Expectations.
Tune Spark jobs for cost and runtime; own on-call for the data platform.

Required Qualifications
Bachelor's degree in Computer Science or related field.
6+ years in data engineering, 3+ years on Azure.
Strong SQL and Python; production PySpark experience.

Nice to Have
Experience with Kafka or Event Hubs.
Exposure to retail or FMCG analytics.
"""

JD_TARGET = """Cloud Security Engineer (CTEM and Attack Surface Management) Brightline Financial Services
Bengaluru, hybrid two days a week in office
8 to 12 years experience
full time permanent role
notice period up to 45 days
skills needed are continuous threat exposure management, external attack surface management tools like Censys or Palo Alto Cortex Xpanse, vulnerability management with Qualys or Tenable, cloud security posture management on AWS and Azure, Python scripting for automation, MITRE ATT&CK mapping
about the role the engineer will run our CTEM programme end to end, discover and prioritise exposures across internet facing assets, cloud accounts and SaaS, and work with platform teams to close them before attackers find them
what you will do
scoping - define business critical attack surfaces with risk owners each quarter
discovery - run continuous discovery of domains, certificates, IPs, cloud resources and shadow IT
prioritisation - score exposures using exploitability, asset criticality and threat intel feeds
validation - run breach and attack simulation and coordinate red team validation of top exposures
mobilisation - drive remediation SLAs with engineering teams, report exposure trends to the CISO monthly
build automation in Python to push findings into Jira and ServiceNow
maintain dashboards for mean time to remediate and exposure backlog
must have
bachelor's degree in computer science, information security or similar
8 plus years in security engineering with at least 3 years in vulnerability or exposure management
hands on with at least one EASM platform and one CSPM tool
good understanding of AWS and Azure networking and IAM
good to have
OSCP, CISSP or cloud security certifications
experience in banking or regulated industries
knowledge of CISA KEV and EPSS scoring
"""

MEETING_NOTES = """product sync 12 sept
attendees ana, vikram, lee, priya, tom
agenda was q4 roadmap, onboarding drop off, pricing page test
roadmap - lee said sso for teams slips to november because of the audit work, vikram wants the usage dashboard first, agreed dashboard ships oct 20, sso nov 15
onboarding - priya showed 38 percent of new signups never finish step 3 (connect data source), hypothesis is the oauth screen is confusing, tom thinks it's the missing sample data option
decision add a sample dataset button, priya to design by sept 19, tom to build by sept 30
pricing test - variant b (annual toggle default) lifted paid conversion from 2.1 to 2.6 percent over 3 weeks, ana wants one more week because of a holiday dip, agreed to extend to sept 26
risks audit could slip again, only one designer for two projects
action items lee send audit timeline to vikram by friday, ana share final pricing numbers sept 27, priya book user interviews for onboarding (5 people)
next meeting sept 19
"""

RESUME_PLAIN = """rohan desai
senior backend engineer
rohan.desai@example.com  +91 98xxxxxx10  ahmedabad
summary backend engineer with 7 years building payment and ledger systems in go and java, led migration of a monolith to 14 services handling 3 million transactions a day
experience
finpay labs senior backend engineer jan 2022 to present
designed idempotent payment api used by 1200 merchants
cut p99 latency from 480 ms to 120 ms by moving reconciliation to kafka streams
mentored 4 engineers, ran the on call rotation
quickcart software engineer jun 2018 to dec 2021
built order service in java spring boot, 99.95 percent uptime
introduced contract tests that reduced integration bugs by 40 percent
education
b tech computer engineering, nirma university, 2018
skills go, java, spring boot, kafka, postgresql, redis, kubernetes, terraform, aws
certifications aws solutions architect associate 2023
"""

POLICY_PLAIN = """work from home policy draft
purpose this policy sets how employees can work remotely while keeping collaboration and security
eligibility all full time employees after probation, interns need manager approval, roles that need physical presence like lab staff are excluded
schedule up to 2 remote days per week, core hours 11 am to 4 pm local time must be online, team anchor day is wednesday for everyone
equipment company laptop only, personal devices not allowed for customer data, monthly internet allowance of 1000 rupees
security use vpn always, lock screen when away, no public wifi without vpn, report lost devices within 2 hours to it
requests submit in the hr portal by thursday for the next week, manager approves within 2 working days
violations first time written warning, repeated violation remote privilege removed for 3 months
review policy reviewed every 6 months by hr and it security
"""
