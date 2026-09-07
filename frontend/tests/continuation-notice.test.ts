/**
 * What the reader is told about an answer written across several model calls.
 *
 * The rule under test is a judgement, not a format: a long answer that
 * FINISHED gets no notice — "written in 7 parts" is an implementation detail
 * nobody asked about — while one that STOPPED early gets one, because the
 * reader would otherwise assume the text simply ends there.
 */
import { describe, expect, it } from 'vitest';

import { continuationNotice } from '@/lib/errors';

const base = { segments: 7, output_tokens: 52_400, stop_reason: 'complete', truncated: false };

describe('continuationNotice', () => {
  it('says nothing about an answer that finished, however many calls it took', () => {
    expect(continuationNotice(base)).toBeNull();
    expect(continuationNotice({ ...base, segments: 120 })).toBeNull();
  });

  it('says an answer stopped early, and why, in words a reader can act on', () => {
    const stopped = { ...base, truncated: true, stop_reason: 'budget' };
    const notice = continuationNotice(stopped)!;
    expect(notice).toContain('length limit');
    expect(notice).toContain('Ask for the next part');
    // The count is real, so it is shown.
    expect(notice).toContain('52,400');
  });

  it('never presents an unmeasured token count as a number', () => {
    const notice = continuationNotice({
      ...base,
      truncated: true,
      stop_reason: 'budget',
      output_tokens: null,
    })!;
    expect(notice).not.toMatch(/\d/);
    expect(notice).not.toContain('0 tokens');
  });

  it('distinguishes the reasons, because they need different responses', () => {
    const of = (stop_reason: string) =>
      continuationNotice({ ...base, truncated: true, stop_reason })!;
    expect(of('deadline')).toContain('time limit');
    expect(of('repetition')).toContain('repeating itself');
    expect(of('no_progress')).toContain('nothing further to add');
    expect(of('error')).toContain('failed');
    // Asking for "the next part" only makes sense where there IS a next part.
    expect(of('repetition')).not.toContain('Ask for the next part');
    expect(of('error')).not.toContain('Ask for the next part');
  });

  it('still says something useful for a reason it has never seen', () => {
    const notice = continuationNotice({ ...base, truncated: true, stop_reason: 'martian' })!;
    expect(notice).toContain('before it was finished');
  });
});
