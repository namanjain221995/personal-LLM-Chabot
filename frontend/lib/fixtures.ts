/**
 * MOCK_MODE fixtures — one canned response per engine, following the SSE
 * contract EXACTLY (§10 + V2 §2: optional reasoning deltas and step events
 * → token deltas → single final meta → done).
 *
 * Consumed only by app/api/chat/route.ts when MOCK_MODE=true so the full UI
 * is demo-able before any model exists. The meta payloads are shaped
 * precisely like the orchestrator's (§8 / V2 §3): sql/data/truncated/chart,
 * citations, report_files, mode/model/effort, agent steps.
 */

import type { AgentStep, Engine, Meta } from './types';

export interface Fixture {
  text: string;
  meta: Meta;
  /** Streamed as `reasoning` deltas before the answer (smart model only). */
  reasoning?: string;
  /** Animated as `step` events: running → final status (V2 §4e). */
  steps?: AgentStep[];
}

/** Served model ids the mock reports in meta (V2 §2). */
export const MOCK_MODEL_IDS = {
  smart: 'openai/gpt-oss-120b',
  fast: 'Qwen/Qwen3-4B-Instruct-2507',
} as const;

const MONTHS = [
  '2025-08', '2025-09', '2025-10', '2025-11', '2025-12', '2026-01',
  '2026-02', '2026-03', '2026-04', '2026-05', '2026-06', '2026-07',
];
const CREATED = [42, 55, 61, 48, 39, 74, 68, 71, 59, 66, 80, 73];
const CLOSED = [38, 49, 57, 52, 41, 60, 65, 69, 62, 58, 71, 70];

const sqlFixture: Fixture = {
  reasoning:
    'The user wants monthly created vs closed case counts for the last 12 months.\n' +
    'Plan: group Case rows by strftime(CreatedDate) for created; count Status = Closed for closed.\n' +
    'One SELECT, two aggregates, ordered by month — a grouped bar chart fits best.',
  text:
    'Here is the monthly picture of case volume for the last 12 months.\n\n' +
    'Cases **created** outpaced cases **closed** in 9 of the 12 months, with the ' +
    'gap widest in January 2026 (74 created vs 60 closed). The support team ' +
    'caught up in February–April, closing within 4 cases of intake each month.\n\n' +
    'Totals for the period:\n\n' +
    '| Metric | Count |\n| --- | --- |\n| Cases created | 736 |\n| Cases closed | 692 |\n| Net backlog growth | +44 |\n\n' +
    'The chart below breaks this down month by month; the full row set is in ' +
    'the Data tab.',
  meta: {
    route: 'sql',
    sql:
      "SELECT strftime(CreatedDate, '%Y-%m') AS month,\n" +
      '       COUNT(*) FILTER (WHERE TRUE) AS created,\n' +
      "       COUNT(*) FILTER (WHERE Status = 'Closed') AS closed\n" +
      'FROM Case\n' +
      "WHERE CreatedDate >= DATE '2025-08-01'\n" +
      'GROUP BY 1\n' +
      'ORDER BY 1;',
    data: MONTHS.map((month, i) => ({
      month,
      created: CREATED[i],
      closed: CLOSED[i],
    })),
    truncated: false,
    chart: {
      type: 'bar',
      x_key: 'month',
      y_keys: ['created', 'closed'],
      title: 'Cases created vs closed by month (last 12 months)',
      stacked: false,
    },
  },
};

const ragFixture: Fixture = {
  text:
    '## Acme Corporation — account summary\n\n' +
    'Acme Corporation is an enterprise manufacturing customer, active since ' +
    '2021, with **$1.42M in open pipeline** across three opportunities.\n\n' +
    '**Open opportunities**\n' +
    '- *Acme Plant Expansion — Phase 2* ($850k, Negotiation) is the largest deal; ' +
    'the next step recorded is a security review with procurement.\n' +
    '- *Acme Analytics Add-on* ($420k, Proposal) is waiting on a revised quote.\n' +
    '- *Acme Support Renewal* ($150k, Commit) renews in September.\n\n' +
    '**Support health**\n' +
    'Two cases were opened in the last 30 days. The escalated one concerns ' +
    'intermittent sensor-sync failures on the Fremont line; engineering has a ' +
    'patch scheduled. Overall case volume is down 18% quarter over quarter.\n\n' +
    '**Relationship**\n' +
    'Primary contact is VP Operations Dana Wells; the last logged executive ' +
    'touchpoint was a QBR on July 2, 2026. Sentiment in recent case notes is ' +
    'positive after the Q2 firmware fix.',
  meta: {
    route: 'rag',
    citations: [
      {
        record_id: '0015g00000AbCdEfGh',
        object: 'Account',
        url: 'https://techsara.lightning.force.com/0015g00000AbCdEfGh',
      },
      {
        record_id: '0065g00000QrStUvWx',
        object: 'Opportunity',
        url: 'https://techsara.lightning.force.com/0065g00000QrStUvWx',
      },
      {
        record_id: '0065g00000YzAbCdEf',
        object: 'Opportunity',
        url: 'https://techsara.lightning.force.com/0065g00000YzAbCdEf',
      },
      {
        record_id: '5005g00000GhIjKlMn',
        object: 'Case',
        url: 'https://techsara.lightning.force.com/5005g00000GhIjKlMn',
      },
      {
        record_id: '0035g00000OpQrStUv',
        object: 'Contact',
        url: 'https://techsara.lightning.force.com/0035g00000OpQrStUv',
      },
    ],
  },
};

const visionFixture: Fixture = {
  text:
    'This is an invoice from **Northwind Traders** to Acme Corporation. ' +
    'I extracted the following fields:\n\n' +
    '```json\n' +
    '{\n' +
    '  "vendor": "Northwind Traders",\n' +
    '  "invoice_number": "INV-2026-0791",\n' +
    '  "invoice_date": "2026-07-14",\n' +
    '  "due_date": "2026-08-13",\n' +
    '  "currency": "USD",\n' +
    '  "subtotal": 12400.00,\n' +
    '  "tax": 1054.00,\n' +
    '  "total": 13454.00,\n' +
    '  "payment_terms": "Net 30",\n' +
    '  "line_items": [\n' +
    '    { "description": "Sensor array, model SA-220", "qty": 8, "unit_price": 1200.00 },\n' +
    '    { "description": "Installation & calibration", "qty": 1, "unit_price": 2800.00 }\n' +
    '  ]\n' +
    '}\n' +
    '```\n\n' +
    'Two things worth checking: the **due date is Net 30** from the invoice ' +
    'date, and the tax line (8.5%) matches the California rate on file for ' +
    'this vendor.',
  meta: {
    route: 'vision',
  },
};

const reportFixture: Fixture = {
  text:
    'I generated the one-page pipeline review. It covers:\n\n' +
    '1. **Pipeline by stage** — $8.9M total open, weighted $4.1M; a stage-by-stage bar chart is embedded.\n' +
    '2. **Quarter outlook** — 14 opportunities with close dates this quarter, $2.6M combined.\n' +
    '3. **Risks** — 6 open opportunities have close dates in the past and need re-dating.\n' +
    '4. **Wins** — win rate is 31% fiscal-year-to-date vs 27% last year.\n\n' +
    'Both formats are ready below — the Word file is editable for your own ' +
    'commentary; the PDF is share-ready.',
  meta: {
    route: 'report',
    report_files: [
      { filename: 'pipeline-review-2026-07-22.docx', type: 'docx', size: 48213 },
      { filename: 'pipeline-review-2026-07-22.pdf', type: 'pdf', size: 91427 },
    ],
  },
};

const chatFixture: Fixture = {
  text:
    "Hi! I'm TechSara's local assistant — everything I do runs on this " +
    'machine.\n\n' +
    'With **Salesforce mode off** I answer general questions from the ' +
    "model's own knowledge: drafting emails, explaining concepts, quick " +
    'math, brainstorming. Toggle **Salesforce** back on when you want ' +
    'numbers computed from your synced org data with SQL proof attached.\n\n' +
    'What can I help with?',
  meta: {
    route: 'chat',
    mode: 'assistant',
    model: MOCK_MODEL_IDS.smart,
    effort: 'medium',
  },
};

const agentFixture: Fixture = {
  reasoning:
    'This needs more than one query: pipeline totals, win rate and account ' +
    'risk signals live in different places.\n' +
    'Plan three steps — two SQL aggregations and one records search — then ' +
    'synthesize a single briefing from the step outputs.',
  steps: [
    {
      id: 1,
      title: 'Plan the analysis',
      status: 'done',
      detail: '3 steps: pipeline SQL · win-rate SQL · account-notes search.',
    },
    {
      id: 2,
      title: 'Query open pipeline by stage',
      status: 'done',
      detail:
        "SELECT StageName, SUM(Amount) FROM Opportunity WHERE IsClosed = FALSE GROUP BY 1 → 5 rows.",
    },
    {
      id: 3,
      title: 'Compute win rate, this FY vs last',
      status: 'done',
      detail: '31% FYTD vs 27% last year (won / closed, excluding open).',
    },
    {
      id: 4,
      title: 'Search account notes for churn risk',
      status: 'done',
      detail: '6 chunks across 2 accounts mention renewal pushback.',
    },
  ],
  text:
    'Here is the pipeline health briefing, assembled from three data ' +
    'passes.\n\n' +
    '**Pipeline** — $8.9M open across 5 stages; Negotiation holds the ' +
    'largest share ($3.2M). **Win rate** is 31% fiscal-year-to-date, up ' +
    'from 27% last year.\n\n' +
    '**Risks** — notes on two accounts (Acme Corporation, Initech) mention ' +
    'renewal pushback tied to the Q2 pricing change; both have open ' +
    'opportunities in Commit.\n\n' +
    'The exact SQL, the stage rows and the cited records are in the proof ' +
    'drawer below.',
  meta: {
    route: 'agent',
    mode: 'salesforce',
    model: MOCK_MODEL_IDS.smart,
    effort: 'high',
    steps: [
      { id: 1, title: 'Plan the analysis', status: 'done' },
      { id: 2, title: 'Query open pipeline by stage', status: 'done' },
      { id: 3, title: 'Compute win rate, this FY vs last', status: 'done' },
      { id: 4, title: 'Search account notes for churn risk', status: 'done' },
    ],
    sql:
      'SELECT StageName, SUM(Amount) AS open_value\n' +
      'FROM Opportunity\n' +
      'WHERE IsClosed = FALSE\n' +
      'GROUP BY 1\n' +
      'ORDER BY open_value DESC;',
    data: [
      { StageName: 'Negotiation', open_value: 3200000 },
      { StageName: 'Proposal', open_value: 2400000 },
      { StageName: 'Commit', open_value: 1600000 },
      { StageName: 'Discovery', open_value: 1100000 },
      { StageName: 'Qualification', open_value: 600000 },
    ],
    truncated: false,
    citations: [
      {
        record_id: '0015g00000AbCdEfGh',
        object: 'Account',
        url: 'https://techsara.lightning.force.com/0015g00000AbCdEfGh',
      },
      {
        record_id: '0015g00000InItEcHx',
        object: 'Account',
        url: 'https://techsara.lightning.force.com/0015g00000InItEcHx',
      },
    ],
  },
};

const searchFixture: Fixture = {
  text:
    'Based on the latest sources, Salesforce announced its Agentforce updates and ' +
    'a new quarterly results date this week [1]. Analysts highlighted continued ' +
    'momentum in its AI product line [2].',
  meta: {
    route: 'search',
    sources: [
      {
        n: 1,
        title: 'Salesforce Newsroom — latest announcements',
        url: 'https://www.salesforce.com/news/',
        domain: 'salesforce.com',
      },
      {
        n: 2,
        title: 'Market coverage of Salesforce AI momentum',
        url: 'https://example.com/salesforce-ai',
        domain: 'example.com',
      },
    ],
  },
};

const urlFixture: Fixture = {
  text:
    'From the page you shared, the product is a local analytics platform. Its ' +
    'pricing section lists a Pro plan at $49/month with unlimited seats [1].',
  meta: {
    route: 'url',
    sources: [
      {
        n: 1,
        title: 'Example — Pricing',
        url: 'https://example.com/pricing',
        domain: 'example.com',
      },
    ],
  },
};

const repoFixture: Fixture = {
  text:
    'Authentication is handled in `app/auth.py`: `require_user` validates the ' +
    'signed session cookie [app/auth.py:L112-L118], and `login` verifies the ' +
    'password hash [app/auth.py:L126-L140].',
  meta: {
    route: 'repo',
    code_sources: [
      {
        path: 'app/auth.py',
        start_line: 112,
        end_line: 118,
        snippet: 'def require_user(request):\n    user = current_user(request)\n    if user is None:\n        raise HTTPException(401)\n    return user',
      },
    ],
  },
};

/**
 * Salesforce Intelligence Mode asking one question back. Present so the card,
 * its keyboard handling and the resume flow are demo-able in MOCK_MODE — a UI
 * that can only be seen with a live org is a UI nobody reviews.
 *
 * `pickFixtureEngine` returns this for a pipeline question with no period,
 * which is exactly the ambiguity the real planner asks about.
 */
const clarifyFixture: Fixture = {
  text:
    'Which period should I use for the pipeline?\n\n' +
    '**1.** This month\n**2.** This quarter\n**3.** This year\n' +
    '**4.** All currently open opportunities\n' +
    '**5.** Something else — let me type it',
  meta: {
    route: 'clarify',
    salesforce_mode: 'intelligence',
    clarification: {
      clarification_id: 'clr_mock0001',
      conversation_id: 'mock',
      run_id: 'run_mock',
      root_user_message_id: 'msg_mock',
      intent_id: 'int_mock',
      source: 'salesforce',
      // A TOPIC, not a source — the card reads "Clarification · Time period".
      header: 'Time period',
      question: 'Which period should I use for the pipeline?',
      slot: 'date_range',
      options: [
        { id: 'this_month', label: 'This month', value: 'THIS_MONTH' },
        { id: 'this_quarter', label: 'This quarter', value: 'THIS_QUARTER' },
        { id: 'this_year', label: 'This year', value: 'THIS_YEAR' },
        {
          id: 'all_open',
          label: 'All currently open opportunities',
          description: 'No date filter — everything still open.',
          value: 'ALL_OPEN',
        },
      ],
      allow_custom: true,
      custom_placeholder: 'Enter another date range…',
      multi_select: false,
      round_number: 1,
      created_at: '2026-08-11T09:00:00+00:00',
      state: 'pending',
      resume_token: 'mock-resume-token',
      question_fingerprint: 'mock-fingerprint',
    },
  },
};

export const FIXTURES: Record<Engine, Fixture> = {
  clarify: clarifyFixture,
  sql: sqlFixture,
  rag: ragFixture,
  vision: visionFixture,
  report: reportFixture,
  chat: chatFixture,
  agent: agentFixture,
  search: searchFixture,
  url: urlFixture,
  repo: repoFixture,
};

/**
 * Mimic the router (§8 + V2 §3a): agent=true forces the agent engine;
 * mode=assistant bypasses the router entirely (chat); an image forces
 * vision; report/rag/sql inferred from phrasing; greetings/small talk hit
 * the V2 "chat" router class; default sql (the analytics engine).
 */
export function pickFixtureEngine(
  lastUserMessage: string,
  hasImage: boolean,
  options?: { mode?: string; agent?: boolean },
): Engine {
  if (options?.agent) return 'agent';
  if (options?.mode === 'assistant') return 'chat';
  if (hasImage) return 'vision';
  const q = lastUserMessage.toLowerCase().trim();
  if (
    /^(hi|hello|hey|yo|thanks|thank you|good (morning|afternoon|evening))\b/.test(
      q,
    ) ||
    /\b(who are you|what can you do)\b/.test(q)
  ) {
    return 'chat';
  }
  // Salesforce Intelligence Mode: a pipeline question with no period is the
  // canonical ambiguity, and the one the real planner asks about.
  if (
    options?.mode !== 'assistant' &&
    /\bpipeline\b/.test(q) &&
    !/\b(month|quarter|year|week|today|open|all time|q[1-4]|\d{4})\b/.test(q)
  ) {
    return 'clarify';
  }
  if (/\b(report|word file|docx|pdf|one-page|deliverable)\b/.test(q)) {
    return 'report';
  }
  if (/\b(summarize|summary|about|notes|why|history|tell me)\b/.test(q)) {
    return 'rag';
  }
  return 'sql';
}

/** Mock rows for GET /reports so the Reports page is demo-able too. */
export const MOCK_REPORTS = [
  {
    name: 'pipeline-review-2026-07-22.docx',
    size: 48213,
    mtime: '2026-07-22T09:14:00Z',
    type: 'docx',
  },
  {
    name: 'pipeline-review-2026-07-22.pdf',
    size: 91427,
    mtime: '2026-07-22T09:14:05Z',
    type: 'pdf',
  },
  {
    name: 'open-opportunities-2026-07-21.xlsx',
    size: 23088,
    mtime: '2026-07-21T16:40:12Z',
    type: 'xlsx',
  },
];
