'use client';

/**
 * A ref that keeps itself current.
 *
 * `meta.artifacts` is a snapshot: the single final meta of the chat turn,
 * hydrated verbatim after a reload (lib/history.ts). Almost always it is
 * already terminal. When it is not — the job outlived the turn (an
 * orchestrator restart mid-render, a viewer that closed the tab) — the card
 * must not sit on "Generating" forever, and nothing on the stream will ever
 * update it: live fields do not survive reload and a finished message gets no
 * further events. So the card asks the server itself: poll `status_url`
 * until the job is terminal, then fetch the ref again so the files, page
 * counts and preview URL are the real ones.
 *
 * The polled state lives HERE, in component state, and is never written back
 * into the message: the store's copy of meta is the server's, and a card
 * repainting itself from the server is not a reason to save the conversation.
 */

import { useEffect, useState } from 'react';
import {
  ArtifactRequestError,
  fetchArtifact,
  isTerminal,
  pollJob,
} from '@/lib/artifacts';
import type { ArtifactJob, ArtifactRef } from '@/lib/types';

export interface LiveArtifact {
  /** The freshest ref: the meta's, or the server's once the job finished. */
  ref: ArtifactRef;
  /** The last job answer while polling — carries the stage title. */
  job: ArtifactJob | null;
  /** A settled refusal (401/403/404) or exhausted retries, as a sentence. */
  error: string | null;
}

export function useLiveArtifact(initial: ArtifactRef): LiveArtifact {
  const [ref, setRef] = useState<ArtifactRef>(initial);
  const [job, setJob] = useState<ArtifactJob | null>(null);
  const [error, setError] = useState<string | null>(null);

  const jobId = initial.job_id;
  const artifactId = initial.artifact_id;
  const version = initial.version;
  const terminal = isTerminal(initial.status);

  // A different ref in the meta (a re-attach that replaced the message, a
  // branch switch, the final meta landing on a turn that was still running)
  // starts over from what it says. Keyed on IDENTITY AND STATUS, never on the
  // object: a forced history load (streams.ts adoptPersistedAnswer,
  // ChatApp loadInto(id, true)) rebuilds every message, and reconcileThread
  // hands back `{...s, meta: s.meta}` — a new object with the same values.
  // Keyed on the object, that fired this reset (back to the meta's stale
  // non-terminal snapshot) without restarting the poll below, whose deps are
  // the same primitives — so a card that had reached "Ready" by polling
  // regressed to "Working…" for good. With both effects on the same keys
  // they fire together or not at all.
  useEffect(() => {
    setRef(initial);
    setJob(null);
    setError(null);
    // `initial` is read on purpose only when one of its keys changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifactId, version, jobId, initial.status]);

  useEffect(() => {
    if (terminal || !jobId) return;
    const controller = new AbortController();
    const { signal } = controller;
    void (async () => {
      try {
        const done = await pollJob(jobId, { signal, onUpdate: setJob });
        if (!done || signal.aborted) return;
        if (done.artifact && typeof done.artifact === 'object') {
          setRef(done.artifact);
          return;
        }
        try {
          const fresh = await fetchArtifact(artifactId, version, signal);
          if (!signal.aborted) setRef(fresh);
        } catch (err) {
          if (signal.aborted) return;
          // The job is over but the version answers nothing (a failed job
          // publishes no version): the status is the truth we have.
          setRef((prev) => ({ ...prev, status: done.status }));
          if (err instanceof ArtifactRequestError && err.status !== 404) {
            setError(err.detail);
          }
        }
      } catch (err) {
        if (signal.aborted) return;
        setError(
          err instanceof ArtifactRequestError
            ? err.detail
            : 'Could not check on this file. Reload to try again.',
        );
      }
    })();
    return () => controller.abort();
  }, [terminal, jobId, artifactId, version]);

  return { ref, job, error };
}
