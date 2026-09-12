'use client';

/**
 * The cards for `meta.artifacts` under an assistant turn: one GROUP per
 * artifact version (ArtifactCard — a header over one FileCard per file),
 * grouped by artifact so "make slide 4 shorter" (a new version of the same
 * deck) reads as a revision of the files above it, not as an unrelated
 * second set.
 *
 * Each group owns its own liveness (useLiveArtifact): a ref the meta left
 * non-terminal polls the server on its own and repaints itself, so a
 * hundred-message thread never runs a hundred polls — only the groups that
 * are actually unfinished ask, and they stop the moment they hear.
 *
 * The whole section is clamped to the width of one card (680 px): the cards
 * belong to the assistant's column of prose, not to the viewport.
 */

import { useMemo } from 'react';
import type { ArtifactRef } from '@/lib/types';
import { ArtifactCard } from './ArtifactCard';
import { useLiveArtifact } from './useLiveArtifact';

/**
 * Open the panel on one file. `siblings` is every ref of the same message,
 * in meta order, so the panel can step prev/next across the message's
 * versions and not only within one (CONTRACT-2 §9).
 */
export type OpenArtifact = (
  artifact: ArtifactRef,
  originId: string,
  fileKey: string,
  siblings: ArtifactRef[],
) => void;

/** `artifact_id:version` — what identifies one version group. */
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

function LiveGroup({
  artifact,
  siblings,
  activeKey,
  onOpen,
}: {
  artifact: ArtifactRef;
  siblings: ArtifactRef[];
  activeKey: string | null;
  onOpen: OpenArtifact;
}) {
  const live = useLiveArtifact(artifact);
  return (
    <ArtifactCard
      artifact={live.ref}
      job={live.job}
      error={live.error}
      activeKey={activeKey}
      onOpen={(ref, originId, key) => onOpen(ref, originId, key, siblings)}
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
  /** The key (lib/artifacts.ts fileKey) of the FILE the panel is showing. */
  activeKey?: string | null;
}) {
  const groups = useMemo(() => groupArtifacts(artifacts), [artifacts]);
  if (groups.length === 0) return null;
  return (
    <section
      aria-label="Generated files"
      className="mt-3 flex w-full max-w-[680px] flex-col gap-4"
      data-testid="artifact-cards"
    >
      {groups.map((group) => (
        <div key={group[0].artifact_id} className="flex flex-col gap-3">
          {group.map((ref) => (
            <LiveGroup
              key={artifactKey(ref.artifact_id, ref.version)}
              artifact={ref}
              siblings={artifacts}
              activeKey={activeKey}
              onOpen={onOpen}
            />
          ))}
        </div>
      ))}
    </section>
  );
}
