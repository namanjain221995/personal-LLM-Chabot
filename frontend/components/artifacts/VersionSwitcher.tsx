'use client';

/**
 * Versions of ONE artifact (AS3 prompt-edits): the list GET /artifacts/{id}
 * answers, a "superseded by vN" marker for a card that is not the newest,
 * and the "Edit with a prompt" box.
 *
 * WHY THE EDIT BOX DISPATCHES INSTEAD OF POSTING. An edit is a normal chat
 * turn: the person's words and the answer's card must land in the
 * conversation, or the history and the cards go stale (critic correction 4
 * of the AS3 design). The box therefore never calls an /edit route — there
 * is none. It dispatches `ARTIFACT_EDIT_EVENT` with the artifact id and the
 * text; the chat host sends them as the next message with `artifact_id` on
 * the /chat body. "Restore this version" is the same path with the words
 * "Restore version N", so a restore also appears in the thread.
 *
 * The version list is fetched lazily — only when a person opens the
 * switcher, through a per-artifact cache — so a long thread of cards does
 * not fan out one request per card. The "superseded" marker needs no
 * request at all: every card notes its version in a page-wide registry
 * (lib/artifacts.ts noteArtifactVersion) and an older card reads it.
 */

import { useEffect, useId, useState, type FormEvent } from 'react';
import {
  artifactEditHostReady,
  cachedArtifactVersions,
  editInstruction,
  isTerminal,
  requestArtifactEdit,
  restoreInstruction,
  versionLabel,
  versionList,
} from '@/lib/artifacts';
import type { ArtifactRef } from '@/lib/types';

/** Every version of `artifactId`, loaded once when `enabled`; [] until then. */
export function useArtifactVersions(artifactId: string, enabled: boolean): { versions: ArtifactRef[]; error: boolean } {
  const [versions, setVersions] = useState<ArtifactRef[]>([]);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    cachedArtifactVersions(artifactId)
      .then((res) => {
        if (alive) setVersions(versionList(res.versions));
      })
      .catch(() => {
        if (alive) setError(true);
      });
    return () => {
      alive = false;
    };
  }, [artifactId, enabled]);
  return { versions, error };
}

/** "Superseded by v3" under a card whose artifact has a newer published version. */
export function SupersededMarker({ version, newest }: { version: number; newest: number | null }) {
  if (newest === null || newest <= version) return null;
  return (
    <span
      className="inline-flex items-center rounded-full border border-border px-1.5 py-px text-[10.5px] font-medium text-muted"
      data-testid="artifact-superseded"
      title={`A newer version (v${newest}) of this file exists`}
    >
      Superseded by v{newest}
    </span>
  );
}

export function VersionSwitcher({
  artifactId,
  version,
  versions,
  onSelect,
  onRestore,
}: {
  artifactId: string;
  /** The version on show. */
  version: number;
  versions: readonly ArtifactRef[];
  /** Show another version (the panel moves to it). */
  onSelect?: (version: number) => void;
  /** Restore a version — by default a chat turn "Restore version N" with artifact_id. */
  onRestore?: (version: number) => void;
}) {
  const list = versionList(versions);
  const selectId = useId();
  if (list.length <= 1) return null;
  const canRestore = Boolean(onRestore) || artifactEditHostReady();
  const restore = onRestore ?? ((v: number) => requestArtifactEdit(artifactId, restoreInstruction(v)));
  const newest = list[list.length - 1]?.version ?? version;
  return (
    <div className="flex items-center gap-1.5" data-testid="artifact-version-switcher">
      <label htmlFor={selectId} className="sr-only">
        Version
      </label>
      <select
        id={selectId}
        value={String(version)}
        onChange={(e) => onSelect?.(Number(e.target.value))}
        className="h-7 rounded-md border border-border bg-surface px-1.5 text-xs text-ink"
        data-testid="artifact-version-select"
      >
        {list.map((v) => (
          <option key={v.version} value={String(v.version)} disabled={!isTerminal(v.status) || v.status === 'failed' || v.status === 'cancelled'}>
            {versionLabel(v)}
            {v.status === 'failed' ? ' · failed' : ''}
          </option>
        ))}
      </select>
      {version !== newest && canRestore && (
        <button
          type="button"
          onClick={() => restore(version)}
          className="h-7 rounded-md border border-border bg-surface px-2 text-xs font-medium text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          data-testid="artifact-version-restore"
        >
          Restore v{version}
        </button>
      )}
    </div>
  );
}

/** The "Edit with a prompt" box: one line, sent as a chat message with artifact_id. */
export function EditPromptBox({
  artifactId,
  title,
  onSubmit,
}: {
  artifactId: string;
  title: string;
  /** Replaces the default (the ARTIFACT_EDIT_EVENT dispatch) — the host's send. */
  onSubmit?: (artifactId: string, text: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const inputId = useId();

  function submit(e: FormEvent) {
    e.preventDefault();
    const instruction = editInstruction(text);
    if (!instruction) return;
    if (onSubmit) onSubmit(artifactId, instruction);
    else requestArtifactEdit(artifactId, instruction);
    setText('');
    setOpen(false);
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center rounded-lg border border-border bg-surface px-2 py-1 text-xs font-medium text-muted transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2 hover:text-ink"
        data-testid="artifact-edit-open"
        aria-label={`Edit ${title} with a prompt`}
      >
        Edit with a prompt
      </button>
    );
  }
  return (
    <form onSubmit={submit} className="flex w-full items-center gap-1.5" data-testid="artifact-edit-form">
      <label htmlFor={inputId} className="sr-only">
        What should change in {title}?
      </label>
      <input
        id={inputId}
        autoFocus
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            setOpen(false);
          }
        }}
        maxLength={4000}
        placeholder="e.g. make the headings dark blue"
        className="h-8 min-w-0 flex-1 rounded-lg border border-border bg-surface px-2 text-sm text-ink"
        data-testid="artifact-edit-input"
      />
      <button
        type="submit"
        disabled={!editInstruction(text)}
        className="h-8 rounded-lg bg-accent px-2.5 text-xs font-medium text-white disabled:opacity-50"
        data-testid="artifact-edit-send"
      >
        Send
      </button>
    </form>
  );
}
