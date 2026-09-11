'use client';

/**
 * The cards for `meta.artifacts` under an assistant turn: one card per
 * artifact VERSION, grouped by artifact so "make slide 4 shorter" (a new
 * version of the same deck) reads as a revision of the file above it, not
 * as an unrelated second file.
 *
 * Each card owns its own liveness (useLiveArtifact): a ref the meta left
 * non-terminal polls the server on its own and repaints itself, so a
 * hundred-message thread never runs a hundred polls — only the cards that
 * are actually unfinished ask, and they stop the moment they hear.
 */

import { useMemo } from 'react';
import type { ArtifactRef } from '@/lib/types';
import { ArtifactCard } from './ArtifactCard';
import { useLiveArtifact } from './useLiveArtifact';

export type OpenArtifact = (artifact: ArtifactRef, originId: string) => void;

/** `artifact_id:version` — what the panel and the cards compare. */
export function artifactKey(artifactId: string, version: number): string {
  return `${artifactId}:${version}`;
}

/** Group by artifact, in first-seen order; versions ascending within each. */
export function groupArtifacts(refs: ArtifactRef[]): ArtifactRef[][] {
  const order: string[] = [];
  const groups = new Map<string, ArtifactRef[]>();
  for (const ref of refs) {
    if (!ref || typeof ref !== 'object' || typeof ref.artifact_id !== 'string') continue;
    const bucket = groups.get(ref.artifact_id);
    if (bucket) bucket.push(ref);
    else {
      groups.set(ref.artifact_id, [ref]);
      order.push(ref.artifact_id);
    }
  }
  return order.map((id) =>
    [...(groups.get(id) ?? [])].sort((a, b) => (a.version ?? 0) - (b.version ?? 0)),
  );
}

function LiveCard({
  artifact,
  active,
  onOpen,
}: {
  artifact: ArtifactRef;
  active: boolean;
  onOpen: OpenArtifact;
}) {
  const live = useLiveArtifact(artifact);
  return (
    <ArtifactCard
      artifact={live.ref}
      job={live.job}
      error={live.error}
      active={active}
      onOpen={onOpen}
    />
  );
}

export function ArtifactCards({
  artifacts,
  onOpen,
  activeKey = null,
}: {
  artifacts: ArtifactRef[];
  onOpen: OpenArtifact;
  /** `artifactKey(id, version)` of the version the panel is showing. */
  activeKey?: string | null;
}) {
  const groups = useMemo(() => groupArtifacts(artifacts), [artifacts]);
  if (groups.length === 0) return null;
  return (
    <section aria-label="Generated files" className="mt-3 flex flex-col gap-3" data-testid="artifact-cards">
      {groups.map((group) => (
        <div key={group[0].artifact_id} className="flex flex-col gap-2">
          {group.length > 1 && (
            <p className="text-[11px] font-medium uppercase tracking-wide text-faint">
              {group[0].title} · {group.length} versions
            </p>
          )}
          <ul className="flex flex-col gap-2">
            {group.map((ref) => (
              <li key={artifactKey(ref.artifact_id, ref.version)}>
                <LiveCard
                  artifact={ref}
                  active={activeKey === artifactKey(ref.artifact_id, ref.version)}
                  onOpen={onOpen}
                />
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
