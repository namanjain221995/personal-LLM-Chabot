// @vitest-environment jsdom
/**
 * VIDEO (2026-09-09) — a video is its own attachment kind and travels by
 * reference.
 *
 * Before this, an .mp4 fell into the composer's "upload anything" fallback:
 * accepted, streamed as purpose=document, and answered on the server as
 * "[Binary file … not readable as text]". Now it is classified BEFORE that
 * fallback, chipped as VIDEO, uploaded with purpose=video (which starts the
 * analysis on the server), and the chat request carries `video_uploads`
 * references — never `pdf_uploads`, never inline bytes.
 *
 * The same real-wire harness as the document tests: the real ChatApp through
 * the real startStream with only the network stubbed.
 */

import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  attach,
  box,
  chatBodies,
  mockHistory,
  renderApp,
  resetHarnessState,
  stubEnv,
  userTurns,
  waitForAnswers,
} from './_wireHarness';

mockHistory();

const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');
const { clearAttachments } = await import('@/lib/attachments');

const video = (name = 'standup.mp4') =>
  new File(['\x00\x00\x00\x18ftypmp42'], name, { type: 'video/mp4' });

/** The harness's upload stub does not record the form; this one does. */
function stubUploads(): FormData[] {
  const forms: FormData[] = [];
  const real = globalThis.fetch;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url) === '/api/upload') {
        const form = init?.body as FormData;
        forms.push(form);
        const file = form.get('file');
        const name = file instanceof File ? file.name : 'video.mp4';
        const upload_id = `${'0'.repeat(31)}${forms.length.toString(16)}`;
        return {
          ok: true,
          status: 200,
          json: async () => ({ upload_id, filename: name, files: 1, video: { analysis_id: 1, status: 'queued' } }),
        };
      }
      return real(url, init);
    }),
  );
  return forms;
}

beforeEach(() => {
  stubEnv();
  resetHarnessState();
  clearAttachments();
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(async () => {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  cleanup();
  vi.unstubAllGlobals();
  clearAttachments();
});

describe('a video attachment', () => {
  it('is chipped as VIDEO, not as a document', async () => {
    renderApp(ChatApp, Providers);
    await attach([video('standup.mp4')]);
    const chip = screen.getByLabelText('Remove attachment standup.mp4').parentElement!;
    expect(chip.textContent).toContain('VIDEO');
    expect(chip.textContent).not.toContain('PDF');
  });

  it('uploads with purpose=video and sends video_uploads, not pdf_uploads', async () => {
    const forms = stubUploads();
    renderApp(ChatApp, Providers);
    await attach([video('standup.mp4')]);
    await act(async () => {
      fireEvent.change(box(), { target: { value: 'what was decided?' } });
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });
    await waitFor(() => expect(chatBodies.length).toBe(1), { timeout: 4000 });
    await waitForAnswers(1);

    expect(forms.length).toBe(1);
    expect(forms[0].get('purpose')).toBe('video');

    const body = chatBodies[0] as Record<string, unknown>;
    const refs = body.video_uploads as { upload_id: string; name: string }[];
    expect(refs).toHaveLength(1);
    expect(refs[0].name).toBe('standup.mp4');
    expect(refs[0].upload_id).toMatch(/^[0-9a-f]{32}$/);
    expect(body.pdf_uploads).toBeUndefined();
    expect(body.pdf).toBeUndefined();

    // The sent turn remembers the video by kind and by its durable id.
    const meta = userTurns()[0]?.meta;
    expect(meta?.attachments?.[0]).toMatchObject({ name: 'standup.mp4', kind: 'video' });
    expect(meta?.attachments?.[0]?.id).toBe(refs[0].upload_id);
  });

  it('is refused with a toast when the account may not use video understanding', async () => {
    renderApp(ChatApp, Providers);
    // The composer reads /auth/me's features; the harness mocks fetchMe with
    // no features at all, which reads as "allowed" — flip it via the prop
    // path the app uses by rendering the Composer directly.
    const { Composer } = await import('@/components/Composer');
    cleanup();
    const { render } = await import('@testing-library/react');
    render(
      <Providers>
        <Composer
          streaming={false}
          prefs={{ salesforce: false, sfLive: false, model: 'smart', effort: 'think', agent: false, webSearch: 'off', deepResearch: false }}
          features={{ video_analysis: false }}
          onPrefsChange={() => undefined}
          onSend={() => undefined}
          onStop={() => undefined}
        />
      </Providers>,
    );
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { files: [video('nope.mp4')] } });
    });
    await waitFor(() =>
      expect(screen.getByText(/Video understanding is turned off/i)).toBeTruthy(),
    );
    expect(screen.queryByLabelText('Remove attachment nope.mp4')).toBeNull();
  });
});
