// @vitest-environment jsdom
/**
 * Versions and prompt edits in the browser (AS3 prompt-edits).
 *
 * Pinned: the version switcher lists every version of the artifact; an older
 * card shows "Superseded by vN" once a newer version's card is on the page
 * (with no request per card); the panel's prev/next crosses into the
 * artifact's other versions; the "Edit with a prompt" box never calls an API
 * route — it hands the text and the artifact_id to the chat host, which
 * sends a normal /chat turn; an image file is previewed through <img src>
 * only.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ArtifactCard } from '@/components/artifacts/ArtifactCard';
import { ArtifactCards } from '@/components/artifacts/ArtifactCards';
import { ArtifactPanel } from '@/components/artifacts/ArtifactPanel';
import { EditPromptBox, VersionSwitcher } from '@/components/artifacts/VersionSwitcher';
import {
  ARTIFACT_EDIT_EVENT,
  invalidateArtifactVersions,
  registerArtifactEditHost,
  resetNewestVersions,
  withArtifactId,
  type ArtifactEditRequest,
} from '@/lib/artifacts';
import type { ArtifactFile, ArtifactRef } from '@/lib/types';

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';

const file = (version: number, format: string, fileId: string): ArtifactFile => ({
  file_id: fileId,
  role: format === 'docx' ? 'primary' : 'companion',
  format,
  filename: `audit-v${version}.${format}`,
  title: 'Audit',
  mime_type: 'application/octet-stream',
  size: 2048,
  download_url: `/artifacts/${ID}/v/${version}/f/${fileId}?disposition=attachment`,
  inline_url: `/artifacts/${ID}/v/${version}/f/${fileId}?disposition=inline`,
  preview_url: '',
});

const ref = (version: number, over: Partial<ArtifactRef> = {}): ArtifactRef => ({
  artifact_id: ID,
  version,
  job_id: JOB,
  title: 'Audit',
  kind: 'document',
  status: 'completed',
  files: [file(version, 'docx', `${version}`.padStart(16, 'a'))],
  preview_kind: 'none',
  preview_pages: 0,
  preview_url: '',
  thumbnail_url: '',
  warnings: [],
  created_at: '2026-09-15T10:00:00Z',
  operation: version === 1 ? 'create' : 'edit',
  parent_version: version > 1 ? version - 1 : undefined,
  status_url: `/artifacts/jobs/${JOB}`,
  ...over,
});

function stubFetch(versions: ArtifactRef[]) {
  const fetchMock = vi.fn(async (url: string) => {
    const u = String(url);
    if (u === `/api/artifacts/${ID}`) {
      return new Response(JSON.stringify({ artifact: { id: ID, title: 'Audit', kind: 'document', current_version: versions.length }, versions }), { status: 200 });
    }
    const m = new RegExp(`/api/artifacts/${ID}/v/(\\d+)$`).exec(u);
    if (m) {
      const v = versions.find((x) => x.version === Number(m[1]));
      return new Response(JSON.stringify(v), { status: v ? 200 : 404 });
    }
    return new Response('{}', { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

beforeEach(() => {
  resetNewestVersions();
  invalidateArtifactVersions();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('VersionSwitcher', () => {
  it('lists every version with how it came to be and moves to the one picked', () => {
    const onSelect = vi.fn();
    render(<VersionSwitcher artifactId={ID} version={2} versions={[ref(1), ref(2), ref(3, { operation: 'convert' })]} onSelect={onSelect} />);
    const options = Array.from((screen.getByTestId('artifact-version-select') as HTMLSelectElement).options).map((o) => o.textContent);
    expect(options).toEqual(['v1 · Created', 'v2 · Updated', 'v3 · Converted']);
    fireEvent.change(screen.getByTestId('artifact-version-select'), { target: { value: '1' } });
    expect(onSelect).toHaveBeenCalledWith(1);
  });

  it('offers a restore of an older version as a chat turn, never an API call', () => {
    const fetchMock = stubFetch([]);
    const heard: ArtifactEditRequest[] = [];
    const listener = (e: Event) => heard.push((e as CustomEvent<ArtifactEditRequest>).detail);
    window.addEventListener(ARTIFACT_EDIT_EVENT, listener);
    const release = registerArtifactEditHost();
    render(<VersionSwitcher artifactId={ID} version={1} versions={[ref(1), ref(2)]} />);
    fireEvent.click(screen.getByTestId('artifact-version-restore'));
    release();
    window.removeEventListener(ARTIFACT_EDIT_EVENT, listener);
    expect(heard).toEqual([{ artifactId: ID, text: 'Restore version 1' }]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('hides the restore button and the edit box while no chat host handles them', () => {
    stubFetch([]);
    render(<VersionSwitcher artifactId={ID} version={1} versions={[ref(1), ref(2)]} />);
    expect(screen.queryByTestId('artifact-version-restore')).toBeNull();
    render(<ArtifactCard artifact={ref(1)} onOpen={() => undefined} />);
    expect(screen.queryByTestId('artifact-edit-open')).toBeNull();
  });

  it('shows nothing for an artifact with one version', () => {
    const { container } = render(<VersionSwitcher artifactId={ID} version={1} versions={[ref(1)]} />);
    expect(container.textContent).toBe('');
  });
});

describe('cards', () => {
  it('marks the older card superseded once the newer version is on the page, without a request per card', async () => {
    const fetchMock = stubFetch([ref(1), ref(2)]);
    render(<ArtifactCards artifacts={[ref(1), ref(2)]} onOpen={() => undefined} />);
    await waitFor(() => expect(screen.getAllByTestId('artifact-superseded')).toHaveLength(1));
    expect(screen.getByTestId('artifact-superseded').textContent).toBe('Superseded by v2');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('opens the versions list on demand and opens the panel on the picked version', async () => {
    const versions = [ref(1), ref(2), ref(3)];
    const fetchMock = stubFetch(versions);
    const onOpen = vi.fn();
    render(<ArtifactCards artifacts={[ref(3)]} onOpen={onOpen} />);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('artifact-versions-open'));
    await waitFor(() => expect(screen.getByTestId('artifact-version-select')).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByTestId('artifact-version-select'), { target: { value: '1' } });
    expect(onOpen).toHaveBeenCalledTimes(1);
    const [opened, , , siblings] = onOpen.mock.calls[0];
    expect(opened.version).toBe(1);
    expect((siblings as ArtifactRef[]).map((r) => r.version)).toEqual([3, 1]);
  });

  it('sends "Edit with a prompt" as the host chat turn with the artifact id', () => {
    const fetchMock = stubFetch([]);
    const onEditPrompt = vi.fn();
    render(<ArtifactCard artifact={ref(1)} onOpen={() => undefined} onEditPrompt={onEditPrompt} />);
    fireEvent.click(screen.getByTestId('artifact-edit-open'));
    fireEvent.change(screen.getByTestId('artifact-edit-input'), { target: { value: '  make the headings dark blue  ' } });
    fireEvent.click(screen.getByTestId('artifact-edit-send'));
    expect(onEditPrompt).toHaveBeenCalledWith(ID, 'make the headings dark blue');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(withArtifactId({ message: 'make the headings dark blue' }, ID)).toEqual({ message: 'make the headings dark blue', artifact_id: ID });
  });

  it('dispatches the edit event when no host send is wired, and refuses an empty prompt', () => {
    const heard: ArtifactEditRequest[] = [];
    const listener = (e: Event) => heard.push((e as CustomEvent<ArtifactEditRequest>).detail);
    window.addEventListener(ARTIFACT_EDIT_EVENT, listener);
    render(<EditPromptBox artifactId={ID} title="Audit" />);
    fireEvent.click(screen.getByTestId('artifact-edit-open'));
    expect((screen.getByTestId('artifact-edit-send') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByTestId('artifact-edit-input'), { target: { value: 'add a column for owner' } });
    fireEvent.submit(screen.getByTestId('artifact-edit-form'));
    window.removeEventListener(ARTIFACT_EDIT_EVENT, listener);
    expect(heard).toEqual([{ artifactId: ID, text: 'add a column for owner' }]);
  });

  it('shows no edit box on a failed version', () => {
    stubFetch([]);
    render(<ArtifactCard artifact={ref(2, { status: 'failed', files: [] })} onOpen={() => undefined} />);
    expect(screen.queryByTestId('artifact-edit-open')).toBeNull();
  });
});

describe('panel', () => {
  it('steps from the version on show into the artifact’s other versions', async () => {
    const versions = [ref(1), ref(2)];
    stubFetch(versions);
    const onNavigate = vi.fn();
    render(<ArtifactPanel refs={[ref(2)]} artifactId={ID} version={2} originId={null} onClose={() => undefined} onNavigate={onNavigate} />);
    await waitFor(() => expect(screen.getByTestId('artifact-version-select')).toBeTruthy());
    const prev = screen.getByRole('button', { name: 'Previous file' }) as HTMLButtonElement;
    await waitFor(() => expect(prev.disabled).toBe(false));
    await act(async () => {
      fireEvent.click(prev);
    });
    expect(onNavigate).toHaveBeenCalledWith(ID, 1, expect.any(String));
    await waitFor(() => expect(screen.getByTestId('artifact-panel-subtitle').textContent).toContain('v1'));
  });

  it('previews an SVG through <img src> only', async () => {
    const svgRef = ref(1, { files: [file(1, 'svg', 'bbbbbbbbbbbbbbbb')], kind: 'document' });
    stubFetch([svgRef]);
    render(<ArtifactPanel refs={[svgRef]} artifactId={ID} version={1} originId={null} onClose={() => undefined} />);
    const box = await screen.findByTestId('artifact-panel-image');
    const img = box.querySelector('img');
    expect(img?.getAttribute('src')).toBe(`/api/artifacts/${ID}/v/1/f/bbbbbbbbbbbbbbbb?disposition=attachment`);
    expect(box.querySelector('svg')).toBeNull();
    expect(box.innerHTML).not.toContain('<svg');
  });
});
