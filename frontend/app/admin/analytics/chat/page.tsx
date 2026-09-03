'use client';

/**
 * Chat analytics — the conversational half of the platform.
 *
 * Counted from conversation history rather than from usage events, so it
 * covers the whole life of the workspace instead of starting on the day
 * request telemetry was deployed. That is why the numbers here can be larger
 * than the request counts on Usage: they are not the same measurement, and
 * the hints say so.
 */

import { AnalyticsChart } from '@/components/admin/analytics/AnalyticsChart';
import { ConsoleHeader, RangePicker, useRange } from '@/components/admin/analytics/filters';
import {
  BarList,
  ChartFrame,
  Section,
  Stat,
  StatRow,
  TopPeople,
} from '@/components/admin/analytics/ui';
import { compact, exact } from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import { routeLabel, type ChatAnalytics } from '@/components/admin/analytics/types';

export default function ChatAnalyticsPage() {
  const [range] = useRange();
  const { data, loading, error, reload } = useAnalytics<ChatAnalytics>(
    'analytics/chat',
    { range },
  );
  const series = data?.series ?? [];
  const totals = data?.totals;
  const bucket = data?.range.bucket ?? 'day';
  const feedback = (totals?.thumbs_up ?? 0) + (totals?.thumbs_down ?? 0);

  return (
    <>
      <ConsoleHeader
        title="Chat"
        description="Conversations, messages and the engines that answered them."
      >
        <RangePicker />
      </ConsoleHeader>

      <Section
        first
        title="Messages"
        value={compact(totals?.messages ?? null)}
        valueTitle={exact(totals?.messages ?? null)}
        delta={data?.deltas.messages ?? null}
        stamp={
          totals ? `${compact(totals.answers)} answers returned` : undefined
        }
      >
        <ChartFrame
          height={230}
          loading={loading && !data}
          error={error}
          onRetry={reload}
          empty={!loading && series.every((p) => p.messages === 0)}
          emptyMessage="No messages in this period."
        >
          <AnalyticsChart
            labels={series.map((p) => p.bucket)}
            bucket={bucket}
            height={230}
            ariaLabel="Messages and answers over time"
            series={[
              { name: 'Questions', area: true, data: series.map((p) => p.messages) },
              { name: 'Answers', data: series.map((p) => p.answers) },
            ]}
          />
        </ChartFrame>
      </Section>

      <Section
        title="Conversations"
        value={compact(totals?.conversations ?? null)}
        valueTitle={exact(totals?.conversations ?? null)}
        delta={data?.deltas.conversations ?? null}
        stamp={
          totals ? `${compact(totals.new_conversations)} started in this period` : undefined
        }
      >
        <ChartFrame
          height={200}
          loading={loading && !data}
          error={error}
          onRetry={reload}
          empty={!loading && series.every((p) => p.conversations === 0)}
          emptyMessage="No conversations in this period."
        >
          <AnalyticsChart
            labels={series.map((p) => p.bucket)}
            bucket={bucket}
            height={200}
            ariaLabel="Active conversations over time"
            series={[
              {
                name: 'Conversations',
                area: true,
                data: series.map((p) => p.conversations),
              },
            ]}
          />
        </ChartFrame>
      </Section>

      <Section title="Shape of the conversation">
        <StatRow columns={5}>
          <Stat
            label="People chatting"
            value={compact(totals?.users ?? null)}
          />
          <Stat
            label="Messages per conversation"
            value={
              totals?.messages_per_conversation == null
                ? '—'
                : totals.messages_per_conversation.toFixed(1)
            }
          />
          <Stat
            label="New conversations"
            value={compact(totals?.new_conversations ?? null)}
          />
          <Stat
            label="Rated answers"
            value={compact(feedback)}
            sub={
              feedback
                ? `${totals?.thumbs_up ?? 0} up · ${totals?.thumbs_down ?? 0} down`
                : 'nobody rated an answer'
            }
          />
          <Stat
            label="Answers returned"
            value={compact(totals?.answers ?? null)}
          />
        </StatRow>
      </Section>

      <Section
        title="Which engine answered"
        hint="From request telemetry, so it covers the period since that was deployed."
      >
        <BarList
          loading={loading && !data}
          rows={(data?.routes ?? []).map((r) => ({
            label: routeLabel(r.route),
            value: r.requests,
          }))}
          emptyMessage="No routed requests recorded in this period."
        />
      </Section>

      <Section title="Most active people">
        <TopPeople
          loading={loading && !data}
          rows={data?.top_users ?? []}
          pick={(r) => r.messages}
          unit="messages"
          emptyMessage="Nobody sent a message in this period."
        />
      </Section>
    </>
  );
}
