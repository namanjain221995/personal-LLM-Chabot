/**
 * The live transcript's pure parts (lib/voiceLive.ts, lib/voice.ts config):
 * what the browser shows while someone speaks, and what it keeps to resume.
 *
 *   - a partial is the WHOLE hypothesis of its utterance: it replaces, never
 *     appends; a final commits it; late and repeated events change nothing,
 *     so a replay after a reconnect never shows a word twice;
 *   - the durable (whisper) preview and the live words merge by time: live
 *     utterances already covered by `transcribed_ms` are not shown twice, and
 *     before the durable path has text the live words are all there is;
 *   - the ring keeps the last minute by absolute sample index;
 *   - the capture clock is not fooled by a main thread that stalls;
 *   - which transcript goes into the draft, and when that is known before the
 *     full pass (spec 13);
 *   - a piece that starts with the punctuation closing the one before joins
 *     it without a space, in every join (spec 14.3).
 */
import { describe, expect, it } from 'vitest';
import {
  describeOutcome,
  joinPreview,
  mergeTranscript,
  mergeTranscriptAt,
  parseLiveConfig,
  parseSessionConfig,
  parseSessionState,
  type SessionProgress,
} from '@/lib/voice';
import {
  CaptureClock,
  HINDI_SHARE,
  LiveTranscript,
  PcmRing,
  VOICE_LANGUAGE_KEY,
  chooseFinalText,
  devanagariShare,
  getVoiceLanguage,
  isHindiSession,
  joinPieces,
  liveChosenWithoutWhisper,
  liveMayStillBeChosen,
  liveSocketUrl,
  mergeLiveTranscript,
  setVoiceLanguage,
  spaceBetween,
  swapInDraft,
  withLiveWords,
  type LiveUtterance,
  type LiveView,
} from '@/lib/voiceLive';

class MemoryStorage {
  map = new Map<string, string>();
  getItem(k: string) {
    return this.map.has(k) ? this.map.get(k)! : null;
  }
  setItem(k: string, v: string) {
    this.map.set(k, v);
  }
  removeItem(k: string) {
    this.map.delete(k);
  }
}

const LIVE = { path: '/api/audio/sessions/{id}/live', sample_rate: 16000, frame_ms: 40, resume_max_s: 60 };
const SID = 'b'.repeat(32);

describe('the language the live stream asks for', () => {
  it('is "auto" unless this browser chose otherwise', () => {
    const storage = new MemoryStorage();
    expect(getVoiceLanguage(storage)).toBe('auto');
    setVoiceLanguage('en', storage);
    expect(storage.getItem(VOICE_LANGUAGE_KEY)).toBe('en');
    expect(getVoiceLanguage(storage)).toBe('en');
    setVoiceLanguage('hi', storage);
    expect(getVoiceLanguage(storage)).toBe('hi');
    setVoiceLanguage('auto', storage);
    expect(storage.getItem(VOICE_LANGUAGE_KEY)).toBeNull();
  });

  it('reads a stored value loosely but never passes on one the engines do not have', () => {
    const storage = new MemoryStorage();
    storage.setItem(VOICE_LANGUAGE_KEY, ' HI ');
    expect(getVoiceLanguage(storage)).toBe('hi');
    // Gujarati: neither model can transcribe it (FLEURS-gu WER 104%).
    storage.setItem(VOICE_LANGUAGE_KEY, 'gu');
    expect(getVoiceLanguage(storage)).toBe('auto');
    storage.setItem(VOICE_LANGUAGE_KEY, '{"lang":"en"}');
    expect(getVoiceLanguage(storage)).toBe('auto');
    setVoiceLanguage('gu', storage);
    expect(storage.getItem(VOICE_LANGUAGE_KEY)).toBeNull();
  });

  it('is "auto" when storage is missing or throws, and setting never throws', () => {
    const hostile = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
      removeItem: () => {
        throw new Error('SecurityError');
      },
    };
    expect(getVoiceLanguage(hostile)).toBe('auto');
    expect(getVoiceLanguage(null)).toBe('auto');
    expect(() => setVoiceLanguage('en', hostile)).not.toThrow();
    expect(() => setVoiceLanguage('en', null)).not.toThrow();
  });
});

describe('the live block of a session config', () => {
  it('is read from the create answer, and absent means none', () => {
    expect(parseSessionConfig({ config: { part_ms: 5000 } }).live).toBeNull();
    expect(parseSessionConfig({ config: { live: null } }).live).toBeNull();
    expect(parseSessionConfig({ config: { live: LIVE } }).live).toEqual({
      path: '/api/audio/sessions/{id}/live',
      sampleRate: 16000,
      frameMs: 40,
      resumeMaxS: 60,
    });
  });

  it('accepts the orchestrator’s path_template name for the path', () => {
    expect(parseLiveConfig({ path_template: '/api/audio/sessions/{id}/live' })?.path).toBe(
      '/api/audio/sessions/{id}/live',
    );
  });

  it('refuses a stream this tap cannot produce, rather than send audio the server would misread', () => {
    expect(parseLiveConfig({ ...LIVE, sample_rate: 48000 })).toBeNull();
    expect(parseLiveConfig({ ...LIVE, frame_ms: 20 })).toBeNull();
    expect(parseLiveConfig({ ...LIVE, path: '/api/audio/sessions/live' })).toBeNull();
    expect(parseLiveConfig({ ...LIVE, path: 'wss://elsewhere.example/{id}' })).toBeNull();
    expect(parseLiveConfig('yes')).toBeNull();
    expect(parseLiveConfig([LIVE])).toBeNull();
    expect(parseLiveConfig({ ...LIVE, resume_max_s: -1 })?.resumeMaxS).toBe(60);
  });
});

describe('the socket address', () => {
  const config = parseLiveConfig(LIVE)!;

  it('is this origin, wss behind https and ws on the plain-http LAN path', () => {
    expect(liveSocketUrl(config, SID, { protocol: 'https:', host: 'ai.example.com' })).toBe(
      `wss://ai.example.com/api/audio/sessions/${SID}/live`,
    );
    expect(liveSocketUrl(config, SID, { protocol: 'http:', host: '10.0.0.5:3000' })).toBe(
      `ws://10.0.0.5:3000/api/audio/sessions/${SID}/live`,
    );
  });

  it('is nothing the relay does not own, and nothing without a page', () => {
    const odd = parseLiveConfig({ ...LIVE, path: '/api/other/{id}/live' })!;
    expect(liveSocketUrl(odd, SID, { protocol: 'https:', host: 'a.example' })).toBeNull();
    expect(liveSocketUrl(config, '../../x', { protocol: 'https:', host: 'a.example' })).toBeNull();
    expect(liveSocketUrl(config, SID, null)).toBeNull();
  });
});

describe('the last minute of PCM', () => {
  const run = (from: number, n: number) => Int16Array.from({ length: n }, (_, i) => (from + i) % 30000);

  it('reads back exactly what was pushed, by absolute index, across the wrap', () => {
    const ring = new PcmRing(0.1); // 1,600 samples
    for (let i = 0; i < 10; i += 1) ring.push(i * 640, run(i * 640, 640));
    expect(ring.end).toBe(6400);
    expect(ring.start).toBe(6400 - 1600);
    expect(ring.read(ring.start, 1600)).toEqual(run(4800, 1600));
    expect(ring.read(5000, 640)).toEqual(run(5000, 640));
    // Older than the ring: gone, and said so.
    expect(ring.read(4799, 10)).toBeNull();
    expect(ring.overwritten).toBe(6400 - 1600);
  });

  it('keeps a repeated frame once and restarts after a hole', () => {
    const ring = new PcmRing(1);
    ring.push(0, run(0, 640));
    ring.push(320, run(320, 640));
    expect(ring.end).toBe(960);
    expect(ring.read(0, 960)).toEqual(run(0, 960));
    ring.push(2000, run(2000, 640));
    expect(ring.start).toBe(2000);
    expect(ring.read(960, 10)).toBeNull();
  });

  it('holds nothing once cleared, and takes nothing after', () => {
    const ring = new PcmRing(1);
    ring.push(0, run(0, 640));
    ring.clear();
    expect(() => ring.push(640, run(640, 640))).not.toThrow();
    expect(ring.read(0, 640)).toBeNull();
    expect(ring.read(640, 640)).toBeNull();
  });
});

describe('when a sample was captured', () => {
  it('knows nothing before the first frame', () => {
    const clock = new CaptureClock();
    expect(clock.running).toBe(false);
    expect(clock.timeOf(0)).toBeNull();
  });

  it('is each frame’s arrival less one render quantum', () => {
    const clock = new CaptureClock(48000);
    // Frame k (640 samples, 40 ms) arrives 40 ms after frame k-1.
    for (let k = 0; k < 50; k += 1) clock.note(k * 640, 640, 1000 + 40 * (k + 1));
    // Sample 639 left the worklet at 1040 ms, 2.67 ms after its capture.
    expect(clock.timeOf(639)).toBeCloseTo(1040 - 128 / 48, 6);
    expect(clock.timeOf(0)).toBeCloseTo(1040 - 128 / 48 - 639 / 16, 6);
  });

  it('is not moved by a main thread that stalled', () => {
    const clock = new CaptureClock(48000);
    for (let k = 0; k < 25; k += 1) clock.note(k * 640, 640, 1000 + 40 * (k + 1));
    const before = clock.timeOf(25 * 640)!;
    // Half a second of render: frames 25-37 arrive in a burst, late.
    for (let k = 25; k < 38; k += 1) clock.note(k * 640, 640, 1000 + 40 * 38 + 500 + k);
    expect(clock.timeOf(25 * 640)).toBeCloseTo(before, 6);
  });

  it('follows the audio clock’s drift by forgetting what is older than ten seconds', () => {
    const clock = new CaptureClock(48000);
    // Frames arriving 0.1% slower than their samples say (a slow audio clock).
    for (let k = 0; k < 1000; k += 1) clock.note(k * 640, 640, 1000 + 40.04 * (k + 1));
    const last = 1000 + 40.04 * 1000;
    // Anchored to the last ten seconds, not to the first frame 40 s ago.
    expect(Math.abs(clock.timeOf(999 * 640 + 639)! - (last - 128 / 48))).toBeLessThan(11);
  });

  it('uses the context’s real rate for the quantum', () => {
    const clock = new CaptureClock();
    clock.setRate(44100);
    clock.note(0, 640, 100);
    expect(clock.timeOf(639)).toBeCloseTo(100 - (128 / 44100) * 1000, 6);
  });
});

describe('what has been heard', () => {
  it('replaces the partial, never appends to it', () => {
    const t = new LiveTranscript();
    expect(t.partialUpdate(0, 'hel', 0, 3200)).toBe(true);
    expect(t.partialUpdate(0, 'hello wor', 0, 6400)).toBe(true);
    expect(t.partial?.text).toBe('hello wor');
    expect(t.text()).toBe('hello wor');
    // The same hypothesis again is no change at all (no redraw).
    const version = t.version;
    expect(t.partialUpdate(0, 'hello wor', 0, 6400)).toBe(false);
    expect(t.version).toBe(version);
  });

  it('commits a final, clears its partial, and moves on to the next utterance', () => {
    const t = new LiveTranscript();
    t.partialUpdate(0, 'hello wor', 0, 6400);
    expect(t.final(0, 'Hello world.', 0, 8000)).toBe(true);
    expect(t.partial).toBeNull();
    expect(t.committed.map((u) => u.text)).toEqual(['Hello world.']);
    expect(t.nextU).toBe(1);
    expect(t.committedUntil).toBe(8000);
    t.partialUpdate(1, 'how are', 9000, 12000);
    expect(t.text()).toBe('Hello world. how are');
  });

  it('ignores a late or repeated event for an utterance already committed', () => {
    const t = new LiveTranscript();
    t.final(0, 'One.', 0, 8000);
    t.final(1, 'Two.', 8000, 16000);
    const version = t.version;
    // A replay after a reconnect, or a straggler: nothing changes.
    expect(t.final(1, 'Two.', 8000, 16000)).toBe(false);
    expect(t.final(0, 'Uno.', 0, 8000)).toBe(false);
    expect(t.partialUpdate(1, 'Tw', 8000, 12000)).toBe(false);
    expect(t.version).toBe(version);
    expect(t.text()).toBe('One. Two.');
  });

  it('keeps the newer partial when an older one arrives late', () => {
    const t = new LiveTranscript();
    t.partialUpdate(3, 'newer', 0, 100);
    expect(t.partialUpdate(2, 'older', 0, 50)).toBe(false);
    expect(t.partial?.u).toBe(3);
  });

  it('clears a partial that became empty, and numbers on past a final with no words', () => {
    const t = new LiveTranscript();
    t.partialUpdate(0, 'um', 0, 100);
    expect(t.partialUpdate(0, '  ', 0, 200)).toBe(true);
    expect(t.partial).toBeNull();
    expect(t.final(0, '', 0, 300)).toBe(true);
    expect(t.committed).toHaveLength(0);
    expect(t.nextU).toBe(1);
  });

  it('joins without spaces where the script has none', () => {
    const t = new LiveTranscript();
    t.final(0, '今日は', 0, 100);
    t.final(1, '晴れです', 100, 200);
    expect(t.text()).toBe('今日は晴れです');
    expect(spaceBetween('今日は', '晴れ')).toBe('');
    expect(spaceBetween('Hello', 'world')).toBe(' ');
    expect(spaceBetween('Hello ', 'world')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// The merge
// ---------------------------------------------------------------------------

const progress = (over: Partial<SessionProgress> = {}): SessionProgress => ({
  preview: '',
  tentative: '',
  audioMs: 0,
  backlogMs: 0,
  waitingOn: 'none',
  pendingParts: 0,
  pendingMs: 0,
  offline: false,
  storageTrouble: false,
  lastAckAt: null,
  retentionDays: 0,
  progressive: true,
  ...over,
});

/** An utterance over [fromMs, toMs) of live time. */
const u = (n: number, text: string, fromMs: number, toMs: number): LiveUtterance => ({
  u: n,
  text,
  startSample: fromMs * 16,
  endSample: toMs * 16,
});

const view = (utterances: LiveUtterance[], partial: LiveUtterance | null = null, over: Partial<LiveView> = {}): LiveView => ({
  utterances,
  partial,
  clockOffsetMs: 0,
  active: true,
  version: 1,
  ...over,
});

describe('the durable preview and the live words, as one text', () => {
  const heard = [u(0, 'First sentence.', 0, 4000), u(1, 'Second sentence.', 4500, 9000), u(2, 'Third one.', 9500, 12000)];

  it('is the live words alone before whisper has anything', () => {
    const m = mergeLiveTranscript(progress({ audioMs: 10_000 }), view(heard, u(3, 'and a four', 12500, 14000)));
    expect(m).toEqual({
      settled: '',
      held: '',
      live: 'First sentence. Second sentence. Third one.',
      partial: 'and a four',
    });
  });

  it('shows only what whisper has not covered yet, by the middle of each utterance', () => {
    const p = progress({ preview: 'First sentence. Second', tentative: 'sentence.', transcribedMs: 9_200 });
    const m = mergeLiveTranscript(p, view(heard, u(3, 'and a four', 12500, 14000)));
    expect(m.settled).toBe('First sentence. Second');
    expect(m.held).toBe('sentence.');
    // "Second sentence." (middle 6.75 s) is covered; "Third one." (10.75 s) is not.
    expect(m.live).toBe('Third one.');
    expect(m.partial).toBe('and a four');
  });

  it('maps live time onto the recording with the clock offset', () => {
    // The tap started 1.5 s before the recorder: live 10.75 s is recording 9.25 s.
    const p = progress({ preview: 'First sentence. Second sentence.', transcribedMs: 9_300 });
    expect(mergeLiveTranscript(p, view(heard, null, { clockOffsetMs: -1500 })).live).toBe('');
    expect(mergeLiveTranscript(p, view(heard, null, { clockOffsetMs: 0 })).live).toBe('Third one.');
  });

  it('uses audio minus backlog where the server gave no transcribed_ms', () => {
    const p = progress({ preview: 'First sentence.', audioMs: 20_000, backlogMs: 15_000 });
    expect(mergeLiveTranscript(p, view(heard)).live).toBe('Second sentence. Third one.');
    // An absent field parses as 0; beside durable text that cannot be true,
    // and trusting it would repeat every word whisper already wrote.
    const zero = progress({ preview: 'First sentence.', audioMs: 20_000, backlogMs: 15_000, transcribedMs: 0 });
    expect(mergeLiveTranscript(zero, view(heard)).live).toBe('Second sentence. Third one.');
  });

  it('shows the durable preview alone once the live stream failed for good', () => {
    const p = progress({ preview: 'First sentence.', transcribedMs: 4_000 });
    expect(mergeLiveTranscript(p, view(heard, null, { active: false }))).toEqual({
      settled: 'First sentence.',
      held: '',
      live: '',
      partial: '',
    });
    expect(mergeLiveTranscript(p, null).live).toBe('');
  });

  it('keeps only a tail of an hour of live words, cut at a word', () => {
    const many = Array.from({ length: 400 }, (_, i) => u(i, `word${i} again.`, i * 1000, i * 1000 + 800));
    const m = mergeLiveTranscript(progress(), view(many), 120);
    expect(m.live.length).toBeLessThanOrEqual(120);
    expect(m.live.endsWith('word399 again.')).toBe(true);
    expect(m.live.startsWith('word')).toBe(true);
  });

  // The review's probe (2026-09-30): the tail was cut up to the first space,
  // and Japanese has none, so twelve Japanese utterances and "OK thanks" came
  // out as "OK thanks" (9 characters) where four lines of speech belonged.
  it('keeps a tail of Japanese, which has no spaces to cut at', () => {
    const japanese = '今日は会議の資料を確認してから午後の打ち合わせに参加します。明日の予定も共有してください。よろしくお願いします';
    const said = Array.from({ length: 12 }, (_, i) => u(i, japanese, i * 2000, i * 2000 + 1800));
    said.push(u(12, 'OK thanks', 24_000, 25_000));
    const m = mergeLiveTranscript(progress(), view(said));
    expect(m.live.length).toBe(600);
    expect(m.live.endsWith(`${japanese} OK thanks`)).toBe(true);
  });

  it('does not drop a whole word the cut did not fall inside, nor split an emoji', () => {
    // "we met today" cut to its last 9 characters lands exactly on the start
    // of "met": nothing more is dropped (the old trim left "today").
    const words = [u(0, 'we met', 0, 1000), u(1, 'today', 1000, 2000)];
    expect(mergeLiveTranscript(progress(), view(words), 'met today'.length).live).toBe('met today');
    // Inside a word, its short fragment still goes.
    expect(mergeLiveTranscript(progress(), view(words), 'et today'.length).live).toBe('today');
    // Forty utterances of sixteen emoji, 32 UTF-16 units each: the tail never
    // starts on the second half of a surrogate pair.
    const faces = Array.from({ length: 40 }, (_, i) => u(i, '😀'.repeat(16), i * 1000, i * 1000 + 800));
    for (const max of [599, 600, 601]) {
      const first = mergeLiveTranscript(progress(), view(faces), max).live.charCodeAt(0);
      expect(first >= 0xdc00 && first <= 0xdfff).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// Which transcript goes into the draft at Stop (build spec section 10)
// ---------------------------------------------------------------------------

describe('which transcript goes into the draft', () => {
  const HINGLISH = 'मैं आज office जा रहा हूँ, meeting दस बजे है।';
  const ENGLISH = 'I am going to the office today, the meeting is at ten.';
  /** What whisper-large-v3 made of HINGLISH: Urdu script, as it did for 73 of 364 lecture segments. */
  const WHISPER_URDU = 'میں آج آفس جا رہا ہوں، میٹنگ دس بجے ہے۔';

  it('measures Devanagari among the letters, leaving vowel signs out of both counts', () => {
    expect(devanagariShare('')).toBe(0);
    expect(devanagariShare('1, 2, 3!')).toBe(0);
    expect(devanagariShare(ENGLISH)).toBe(0);
    expect(devanagariShare('नमस्ते')).toBe(1);
    // न म स त are letters; the virama and the vowel sign are marks.
    expect(devanagariShare('नमस्ते abcd')).toBe(0.5);
    expect(devanagariShare(HINGLISH)).toBeGreaterThanOrEqual(HINDI_SHARE);
    expect(devanagariShare(WHISPER_URDU)).toBe(0);
  });

  it('is the full pass for an English session, as before live dictation', () => {
    for (const whisperLanguage of ['en', 'English', null, undefined]) {
      expect(chooseFinalText({ liveText: ENGLISH, liveComplete: true, userLanguage: 'auto', whisperLanguage })).toBe(
        'durable',
      );
    }
    expect(chooseFinalText({ liveText: ENGLISH, liveComplete: true, userLanguage: 'en', whisperLanguage: 'en' })).toBe(
      'durable',
    );
  });

  it('is the live one once a fifth of its letters are Devanagari, whatever whisper heard', () => {
    expect(
      chooseFinalText({ liveText: HINGLISH, liveComplete: true, userLanguage: 'auto', whisperLanguage: 'en' }),
    ).toBe('live');
    // Just under a fifth is not enough on its own.
    const mostlyEnglish = `${'a'.repeat(81)} ${'क'.repeat(19)}`;
    expect(devanagariShare(mostlyEnglish)).toBe(0.19);
    expect(
      chooseFinalText({ liveText: mostlyEnglish, liveComplete: true, userLanguage: 'auto', whisperLanguage: 'en' }),
    ).toBe('durable');
    const oneFifth = `${'a'.repeat(80)} ${'क'.repeat(20)}`;
    expect(chooseFinalText({ liveText: oneFifth, liveComplete: true, userLanguage: 'auto', whisperLanguage: null })).toBe(
      'live',
    );
  });

  it('is the live one when the person chose Hindi, even for words the live model wrote in Latin', () => {
    expect(
      chooseFinalText({ liveText: 'aaj hum office jayenge', liveComplete: true, userLanguage: 'hi', whisperLanguage: 'en' }),
    ).toBe('live');
  });

  it('is the live one when whisper heard Hindi or Urdu, as its Urdu-script answer to Hinglish shows', () => {
    for (const whisperLanguage of ['hi', 'ur', 'Hindi', 'Urdu', ' UR ']) {
      expect(
        chooseFinalText({ liveText: 'aaj hum office jayenge', liveComplete: true, userLanguage: 'auto', whisperLanguage }),
      ).toBe('live');
    }
    expect(isHindiSession({ liveText: HINGLISH, userLanguage: 'auto', whisperLanguage: 'ur' })).toBe(true);
  });

  it('is the full pass when the live transcript missed part of the recording, or has no words', () => {
    expect(
      chooseFinalText({ liveText: HINGLISH, liveComplete: false, userLanguage: 'hi', whisperLanguage: 'ur' }),
    ).toBe('durable');
    expect(chooseFinalText({ liveText: '  ', liveComplete: true, userLanguage: 'hi', whisperLanguage: 'hi' })).toBe(
      'durable',
    );
  });

  it('is the full pass for any language it was not measured on', () => {
    expect(
      chooseFinalText({ liveText: 'Bonjour à tous', liveComplete: true, userLanguage: 'auto', whisperLanguage: 'fr' }),
    ).toBe('durable');
  });

  // Spec 12 (2026-09-30): the English-only model scores ~80% WER on the MUCS
  // lectures, whisper 55.8%, so for someone who chose English whisper goes in.
  it('is the full pass when the person chose English and whisper heard Hindi: the English model cannot write it', () => {
    for (const whisperLanguage of ['hi', 'ur']) {
      expect(
        chooseFinalText({ liveText: 'Main aaj office ja raha hoon', liveComplete: true, userLanguage: 'en', whisperLanguage }),
      ).toBe('durable');
    }
    // Devanagari in the live words still decides: they came from the multilingual model before a switch to English.
    expect(chooseFinalText({ liveText: HINGLISH, liveComplete: true, userLanguage: 'en', whisperLanguage: 'hi' })).toBe(
      'live',
    );
  });

  // The review's finding 3: switched from English to Hindi mid-recording, the
  // English model's guesses at the opening went in as a "complete Hindi
  // transcript". Spec 12: the full pass goes in instead.
  it('is the full pass for a Hindi session any of whose words the English-only model wrote', () => {
    const switched = `May aaj of his jar a who. ${HINGLISH}`;
    for (const userLanguage of ['hi', 'auto'] as const) {
      expect(
        chooseFinalText({ liveText: switched, liveComplete: true, englishModelFinals: true, userLanguage, whisperLanguage: 'hi' }),
      ).toBe('durable');
      expect(
        chooseFinalText({ liveText: switched, liveComplete: true, englishModelFinals: false, userLanguage, whisperLanguage: 'hi' }),
      ).toBe('live');
    }
    expect(chooseFinalText({ liveText: HINGLISH, liveComplete: true, userLanguage: 'hi', whisperLanguage: 'hi' })).toBe(
      'live',
    );
  });
});

describe('whether the live stream’s last words are worth waiting for', () => {
  const HINGLISH = 'मैं आज office जा रहा हूँ, meeting दस बजे है।';

  it('is not once the English-only model wrote any of the words, whatever the language now', () => {
    for (const language of ['auto', 'en', 'hi'] as const) {
      expect(liveMayStillBeChosen({ liveText: HINGLISH, englishModelFinals: true, language })).toBe(false);
    }
  });

  it('is not for words heard in English that are under a fifth Devanagari: whatever arrives, the full pass goes in', () => {
    expect(liveMayStillBeChosen({ liveText: 'Hello there.', englishModelFinals: false, language: 'en' })).toBe(false);
    expect(liveMayStillBeChosen({ liveText: '', englishModelFinals: false, language: 'en' })).toBe(false);
  });

  it('is while more Devanagari may come, or the words so far already make a Hindi session', () => {
    expect(liveMayStillBeChosen({ liveText: 'Hello there.', englishModelFinals: false, language: 'auto' })).toBe(true);
    expect(liveMayStillBeChosen({ liveText: '', englishModelFinals: false, language: 'hi' })).toBe(true);
    // Hindi until a switch to English at the very end: complete, it goes in.
    expect(liveMayStillBeChosen({ liveText: HINGLISH, englishModelFinals: false, language: 'en' })).toBe(true);
  });

  it('is not without a stream that can still finish', () => {
    expect(liveMayStillBeChosen({ liveText: HINGLISH, englishModelFinals: false, language: null })).toBe(false);
  });
});

describe('the live words as one text, at any length', () => {
  /** What text() did until 2026-09-30: joinPreview folded over the growing text. */
  const fold = (pieces: string[]) => pieces.reduce((out, piece) => joinPreview(out, piece), '');

  it('joins exactly as folding joinPreview does, space or none by the script on each side', () => {
    const scripts = ['Hello there.', 'मीटिंग दस बजे है', '今日は', '晴れです', 'สวัสดี', 'ครับ', '😀 ok', 'OK', 'ア'];
    let seed = 7;
    const next = () => (seed = (seed * 48271) % 2147483647);
    for (let round = 0; round < 200; round += 1) {
      const pieces = Array.from({ length: 1 + (next() % 12) }, () => scripts[next() % scripts.length]!);
      expect(joinPieces(pieces)).toBe(fold(pieces));
    }
    expect(joinPieces(['a', '', 'b'])).toBe(fold(['a', '', 'b']));
    expect(joinPieces([])).toBe('');
  });

  // The review's finding 6: 10,000 utterances (216,659 characters) took about
  // a second of main thread at Stop, and twice as many four times as long.
  it('takes linear time: 20,000 utterances are joined in well under a second', () => {
    const t = new LiveTranscript();
    for (let i = 0; i < 20_000; i += 1) t.final(i, `utterance number ${i} said.`, i * 1000, i * 1000 + 800);
    t.partialUpdate(20_000, 'and one more', 20_000_000, 20_000_500);
    const started = performance.now();
    const text = t.text();
    const took = performance.now() - started;
    expect(text.startsWith('utterance number 0 said. utterance number 1 said.')).toBe(true);
    expect(text.endsWith('utterance number 19999 said. and one more')).toBe(true);
    expect(took).toBeLessThan(500);
  });
});

describe('swapping the other transcript in', () => {
  const DRAFT = 'Notes: we met on monday. Thanks';
  const span = { start: 7, end: 24 }; // "we met on monday."

  it('replaces exactly the words that went in, and says where the new ones are', () => {
    expect(DRAFT.slice(span.start, span.end)).toBe('we met on monday.');
    const swapped = swapInDraft(DRAFT, span, 'we met on monday.', 'हम सोमवार को मिले।')!;
    expect(swapped.text).toBe('Notes: हम सोमवार को मिले। Thanks');
    expect(swapped.text.slice(swapped.span.start, swapped.span.end)).toBe('हम सोमवार को मिले।');
    // And back again, from where the first swap left it.
    expect(swapInDraft(swapped.text, swapped.span, 'हम सोमवार को मिले।', 'we met on monday.')!.text).toBe(DRAFT);
  });

  it('swaps nothing once the words were changed, and never merges edits into the other text', () => {
    const edited = 'Notes: we met on Monday. Thanks';
    expect(swapInDraft(edited, span, 'we met on monday.', 'हम सोमवार को मिले।')).toBeNull();
  });

  it('swaps nothing without a place, or without words', () => {
    expect(swapInDraft(DRAFT, null, 'we met on monday.', 'x')).toBeNull();
    expect(swapInDraft(DRAFT, { start: 7, end: 99 }, 'we met on monday.', 'x')).toBeNull();
    expect(swapInDraft(DRAFT, span, 'we met on monday.', '  ')).toBeNull();
  });
});

describe('the language the full pass heard, on its result', () => {
  const done = (over: Record<string, unknown>) =>
    describeOutcome(
      parseSessionState({
        session_id: SID,
        status: 'done',
        outcome: 'transcribed',
        text: 'میں آج آفس جا رہا ہوں',
        segments: [],
        gaps: [],
        ...over,
      })!,
    );

  it('carries the session’s language and code when the server said', () => {
    expect(done({ language: 'Urdu', language_code: 'ur' })).toMatchObject({
      kind: 'text',
      language: 'Urdu',
      languageCode: 'ur',
    });
  });

  it('carries nothing when it did not', () => {
    const result = done({});
    expect(result.kind).toBe('text');
    expect('language' in result).toBe(false);
    expect('languageCode' in result).toBe(false);
  });
});

describe('the progress the bar draws', () => {
  it('is the session’s own when there are no live words', () => {
    const p = progress({ preview: 'x' });
    expect(withLiveWords(p, null)).toBe(p);
    expect(withLiveWords(null, null)).toBeNull();
  });

  it('carries the live words after the session’s own text', () => {
    const p = progress({ preview: 'Earlier.', transcribedMs: 1000 });
    const shown = withLiveWords(p, view([u(0, 'Later words.', 2000, 4000)], u(1, 'still go', 4200, 5000)));
    expect(shown).toEqual({ ...p, live: { committed: 'Later words.', partial: 'still go' } });
  });

  it('stands the live words on a blank progress before the session’s first answer, claiming nothing saved', () => {
    const shown = withLiveWords(null, view([], u(0, 'hello', 0, 800)))!;
    expect(shown.live).toEqual({ committed: '', partial: 'hello' });
    expect(shown.preview).toBe('');
    // "Saved to your account" is drawn only once the server has audio.
    expect(shown.savedMs).toBe(0);
    expect(shown.offline).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// The live words before the full pass (build spec 13, 2026-09-30)
// ---------------------------------------------------------------------------

describe('whether the live words go in before the full pass is done', () => {
  const HINGLISH = 'मैं आज office जा रहा हूँ, meeting दस बजे है।';
  const ENGLISH = 'I am going to the office today, the meeting is at ten.';
  /** Mostly Latin: whisper's language would decide it, so it must wait for whisper. */
  const LATIN_HINGLISH = 'Main aaj office ja raha hoon, meeting दस baje hai.';

  it('is when they decide by themselves: Hindi chosen, or a fifth of the letters Devanagari', () => {
    expect(liveChosenWithoutWhisper({ liveText: HINGLISH, liveComplete: true, userLanguage: 'auto' })).toBe(true);
    expect(liveChosenWithoutWhisper({ liveText: 'aaj hum office jayenge', liveComplete: true, userLanguage: 'hi' })).toBe(
      true,
    );
    // Devanagari decides whatever was chosen: only the multilingual model writes it.
    expect(liveChosenWithoutWhisper({ liveText: HINGLISH, liveComplete: true, userLanguage: 'en' })).toBe(true);
    expect(liveChosenWithoutWhisper({ liveText: HINGLISH, liveComplete: true, userLanguage: null })).toBe(true);
    // Still waiting for whisper: English, and Auto whose letters are under a fifth Devanagari.
    expect(liveChosenWithoutWhisper({ liveText: ENGLISH, liveComplete: true, userLanguage: 'auto' })).toBe(false);
    expect(liveChosenWithoutWhisper({ liveText: ENGLISH, liveComplete: true, userLanguage: 'en' })).toBe(false);
    expect(liveChosenWithoutWhisper({ liveText: LATIN_HINGLISH, liveComplete: true, userLanguage: 'auto' })).toBe(false);
    expect(liveChosenWithoutWhisper({ liveText: LATIN_HINGLISH, liveComplete: true, userLanguage: null })).toBe(false);
  });

  it('is never for a live transcript that missed part of the recording, has no words, or holds English-model words', () => {
    expect(liveChosenWithoutWhisper({ liveText: HINGLISH, liveComplete: true, userLanguage: 'hi' })).toBe(true);
    expect(liveChosenWithoutWhisper({ liveText: HINGLISH, liveComplete: false, userLanguage: 'hi' })).toBe(false);
    expect(liveChosenWithoutWhisper({ liveText: '   ', liveComplete: true, userLanguage: 'hi' })).toBe(false);
    expect(
      liveChosenWithoutWhisper({ liveText: HINGLISH, liveComplete: true, englishModelFinals: true, userLanguage: 'hi' }),
    ).toBe(false);
  });

  it('never disagrees with what the full pass would decide, whatever language whisper then hears', () => {
    const texts = [HINGLISH, ENGLISH, LATIN_HINGLISH, 'aaj hum office jayenge', 'नमस्ते', '', 'Bonjour à tous'];
    const whispers = ['hi', 'ur', 'Hindi', 'Urdu', 'en', 'English', 'fr', '', null, undefined];
    let early = 0;
    for (const liveText of texts) {
      for (const liveComplete of [true, false]) {
        for (const englishModelFinals of [true, false]) {
          for (const userLanguage of ['auto', 'en', 'hi', null] as const) {
            const input = { liveText, liveComplete, englishModelFinals, userLanguage };
            if (!liveChosenWithoutWhisper(input)) continue;
            early += 1;
            for (const whisperLanguage of whispers) {
              expect(chooseFinalText({ ...input, whisperLanguage })).toBe('live');
            }
          }
        }
      }
    }
    // Not a vacuous check: HINGLISH and नमस्ते with any choice (8), and each
    // of the four other texts with words when Hindi was chosen (4).
    expect(early).toBe(12);
  });
});

// ---------------------------------------------------------------------------
// A piece that closes the one before it (build spec 14.3, 2026-09-30)
// ---------------------------------------------------------------------------

describe('joining a piece that starts with the punctuation closing the one before it', () => {
  /** Every mark spec 14.3 names, with the full-width forms and the ideographic comma and full stop. */
  const CLOSING = [',', '.', ';', ':', '!', '?', '।', '॥', ')', ']', '}', '…', '，', '．', '；', '：', '！', '？', '）', '］', '｝', '、', '。'];

  it('puts no space before it, and keeps the space before a word or an opening mark', () => {
    // The engine keeps the danda or comma that ends an utterance on the next one.
    expect(joinPreview('है', '। जंगल')).toBe('है। जंगल');
    expect(joinPreview('told', ', and')).toBe('told, and');
    expect(joinPreview('told', 'and')).toBe('told and');
    for (const mark of CLOSING) {
      expect(joinPreview('words', `${mark} more`)).toBe(`words${mark} more`);
      expect(spaceBetween('words', `${mark} more`)).toBe('');
    }
    for (const opening of ['(and', '[and', '"and', '“and', '-and', '#1']) {
      expect(spaceBetween('told', opening)).toBe(' ');
    }
    // Whatever space a side already has is kept as it is.
    expect(joinPreview('told ', ', and')).toBe('told , and');
    expect(joinPreview('told', ' , and')).toBe('told , and');
  });

  it('in the live transcript, which is what goes into the draft', () => {
    const t = new LiveTranscript();
    t.final(0, 'जंगल में है', 0, 100);
    t.final(1, '। फिर हम घर गए', 100, 200);
    t.partialUpdate(2, ', और सो गए', 200, 300);
    expect(t.text()).toBe('जंगल में है। फिर हम घर गए, और सो गए');
  });

  it('in the live words the bar merges with the stored recording’s', () => {
    const heard = [u(0, 'I told them', 0, 2000), u(1, ', and they agreed', 2000, 4000), u(2, '. Then we left', 4000, 6000)];
    const m = mergeLiveTranscript(progress({ audioMs: 6000 }), view(heard, u(3, '! Really', 6000, 7000)));
    expect(m.live).toBe('I told them, and they agreed. Then we left');
    expect(m.partial).toBe('! Really');
    // What whisper already covers is dropped; the rest joins as before.
    const after = mergeLiveTranscript(progress({ preview: 'Stored.', transcribedMs: 2500 }), view(heard));
    expect(after.live).toBe(', and they agreed. Then we left');
  });

  it('in the draft the transcript is merged into', () => {
    expect(mergeTranscript('Draft: है', '। जंगल')).toBe('Draft: है। जंगल');
    expect(mergeTranscript('I told them', ', and then')).toBe('I told them, and then');
    expect(mergeTranscript('I told', 'them')).toBe('I told them');
    const placed = mergeTranscriptAt('Draft: है', '। जंगल');
    expect(placed.text).toBe('Draft: है। जंगल');
    expect(placed.text.slice(placed.span!.start, placed.span!.end)).toBe('। जंगल');
  });

  it('still in one pass, exactly as folding joinPreview does', () => {
    const fold = (pieces: string[]) => pieces.reduce((out, piece) => joinPreview(out, piece), '');
    const kinds = ['Hello there', '। मीटिंग', ', and', 'दस बजे है', '今日は', '。晴れ', '… so', '(aside)', 'OK'];
    let seed = 11;
    const next = () => (seed = (seed * 48271) % 2147483647);
    for (let round = 0; round < 200; round += 1) {
      const pieces = Array.from({ length: 1 + (next() % 12) }, () => kinds[next() % kinds.length]!);
      expect(joinPieces(pieces)).toBe(fold(pieces));
    }
    expect(joinPieces(['दस बजे है', '। मीटिंग', ', and', '… so'])).toBe('दस बजे है। मीटिंग, and… so');
  });
});
