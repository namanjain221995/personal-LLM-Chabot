'use client';

/**
 * Voice dictation analytics.
 *
 * Every figure comes from `voice_transcriptions` (V19), which the audio
 * endpoint writes once per ATTEMPT — the failures included, because an error
 * rate computed from successes only is not an error rate, and "dictation
 * never works for me" is a claim this page should be able to settle.
 *
 * NO TRANSCRIPT TEXT EXISTS BEHIND THIS PAGE. A recording is a draft: it
 * becomes a message only when the person presses Send, and then it lives in
 * their own conversation like anything they typed. This page can say a clip
 * was four seconds of English that came back in 900ms; it cannot say a word
 * of what was in it, and there is nothing to filter out because nothing is
 * stored.
 *
 * The language ranking is the reason the language is recorded at all: a
 * self-hosted deployment that turns out to be dictating in four languages is
 * a different product decision from one dictating in one.
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
import {
  compact,
  duration,
  exact,
  percent,
} from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import type { VoiceAnalytics } from '@/components/admin/analytics/types';

/** Minutes, with a decimal only while that decimal still carries meaning. */
function minutes(value: number | null | undefined): string {
  if (value == null) return '—';
  return `${value < 10 ? value.toFixed(1) : Math.round(value).toLocaleString()} min`;
}

export default function VoiceAnalyticsPage() {
  const [range] = useRange();
  const { data, loading, error, reload } = useAnalytics<VoiceAnalytics>(
    'analytics/voice',
    { range },
  );
  const t = data?.totals;
  const series = data?.series ?? [];

  // A deployment where nobody has EVER dictated gets a sentence, not eleven
  // stats of nothing. The lifetime count is what makes that distinction
  // possible: a windowed zero could just be a quiet fortnight.
  if (data && data.coverage.transcriptions === 0) {
    return (
      <>
        <ConsoleHeader
          title="Voice"
          description="Dictation in the composer, transcribed on this deployment."
        >
          <RangePicker />
        </ConsoleHeader>
        <div className="rounded-lg border border-dashed border-[var(--admin-separator)] px-6 py-16 text-center">
          <p className="text-sm text-ink">Nobody has dictated here yet.</p>
          <p className="mx-auto mt-2 max-w-lg text-xs leading-relaxed text-faint">
            The microphone in the composer records one row per attempt — how
            long someone spoke, how long they waited, which language the model
            heard, and whether it worked. Nothing appears on this page until
            somebody uses it, and no transcript text is ever kept.
          </p>
        </div>
      </>
    );
  }

  return (
    <>
      <ConsoleHeader
        title="Voice"
        description="Dictation in the composer. Metadata only — how long, how fast, which language, and whether it worked."
      >
        <RangePicker />
      </ConsoleHeader>

      <Section
        first
        title="Transcriptions"
        hint="Every attempt is counted, including the ones that never returned words."
        value={compact(t?.transcriptions ?? null)}
        valueTitle={exact(t?.transcriptions ?? null)}
        delta={data?.deltas.transcriptions ?? null}
        stamp={t?.transcriptions ? `${percent(t.success_rate)} succeeded` : undefined}
      >
        <ChartFrame
          height={230}
          loading={loading && !data}
          error={error}
          onRetry={reload}
          empty={!loading && series.every((p) => p.transcriptions === 0)}
          emptyMessage="Nobody dictated in this period."
        >
          <AnalyticsChart
            labels={series.map((p) => p.bucket)}
            bucket={data?.range.bucket ?? 'day'}
            height={230}
            ariaLabel="Voice transcriptions over time"
            series={[
              { name: 'Transcribed', bar: true, data: series.map((p) => p.ok) },
              {
                name: 'Failed',
                bar: true,
                tone: 'danger',
                data: series.map((p) => p.failed),
              },
            ]}
          />
        </ChartFrame>
      </Section>

      <Section
        title="Outcomes"
        hint="Busy means the transcription pool was full; unavailable means the engine could not be reached at all."
      >
        <StatRow columns={6}>
          <Stat label="Transcribed" value={compact(t?.ok ?? null)} />
          <Stat label="Too busy" value={compact(t?.busy ?? null)} />
          <Stat
            label="Rejected"
            value={compact(t?.rejected ?? null)}
            sub="audio the engine refused"
          />
          <Stat label="Engine unavailable" value={compact(t?.unavailable ?? null)} />
          <Stat label="Errors" value={compact(t?.error ?? null)} />
          <Stat
            label="Degraded"
            value={compact(t?.degraded ?? null)}
            sub="the fallback answered"
          />
        </StatRow>
      </Section>

      <Section
        title="Speech and waiting"
        hint="Clip length is what the browser measured while someone spoke. The wait is the orchestrator's own wall clock — upload, queue and engine together — and it is measured on failures too."
      >
        <StatRow columns={6}>
          <Stat
            label="Recorded"
            value={minutes(t?.total_minutes)}
            title={
              t?.total_duration_ms == null
                ? undefined
                : `${exact(t.total_duration_ms)} ms`
            }
            sub={t ? `across ${exact(t.transcriptions)} clips` : undefined}
          />
          <Stat label="Average clip" value={duration(t?.avg_duration_ms)} />
          <Stat label="P95 clip" value={duration(t?.p95_duration_ms)} />
          <Stat label="Average wait" value={duration(t?.avg_processing_ms)} />
          <Stat label="P95 wait" value={duration(t?.p95_processing_ms)} />
          <Stat label="People dictating" value={compact(t?.users ?? null)} />
        </StatRow>
      </Section>

      <Section
        title="Languages heard"
        hint="The language the model named for itself. A clip it did not identify is left out rather than filed under 'unknown' — that would be a count of the parser's silence, not of a language anyone spoke."
        stamp={
          t?.transcriptions
            ? `${compact(t.language_identified)} of ${compact(t.transcriptions)} clips identified`
            : undefined
        }
      >
        <BarList
          loading={loading && !data}
          rows={(data?.languages ?? []).map((l) => ({
            label: l.language,
            sublabel:
              l.share == null
                ? undefined
                : `${percent(l.share)} of identified clips`,
            value: l.transcriptions,
          }))}
          emptyMessage="No language was identified in this period."
        />
      </Section>

      <Section title="Who dictates">
        <TopPeople
          loading={loading && !data}
          rows={data?.top_users ?? []}
          pick={(r) => r.transcriptions ?? 0}
          unit="clips"
          emptyMessage="Nobody used voice input in this period."
        />
      </Section>
    </>
  );
}
