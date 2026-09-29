/**
 * Hand-made browser parts for the live-dictation tests: a WebSocket whose
 * server side the test plays, and an audio graph (AudioContext with an
 * AudioWorklet, AudioWorkletNode) whose worklet the test plays. Nothing here
 * decides anything; every event happens when a test says so, and every call
 * the code under test makes is written to `log` in order, which is how the
 * tests check what happened BEFORE what (the socket's flush before the
 * context's close, above all).
 */

/** Everything the fakes saw, in order: 'worklet:flush', 'ws:flush', 'context:close', ... */
export const log: string[] = [];

export function resetLiveFakes(): void {
  log.length = 0;
  FakeWebSocket.instances = [];
  FakeWebSocket.refuse = false;
  FakeWorkletNode.instances = [];
  FakeLiveAudioContext.instances = [];
  FakeLiveAudioContext.startSuspended = false;
  FakeLiveAudioContext.withWorklet = true;
  FakeLiveAudioContext.addModule = async () => undefined;
}

// ---------------------------------------------------------------------------
// The socket
// ---------------------------------------------------------------------------

export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  /** The constructor throws, as a browser does for a URL or policy it refuses. */
  static refuse = false;
  readonly url: string;
  readonly protocols: string[];
  readyState = 0;
  bufferedAmount = 0;
  /** Keep what is sent in bufferedAmount (a slow uplink) instead of draining at once. */
  holdBuffer = false;
  binaryType = 'blob';
  sent: Array<string | ArrayBuffer> = [];
  closedWith: number | null = null;
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(url: string, protocols?: string | string[]) {
    if (FakeWebSocket.refuse) throw new SyntaxError('refused');
    this.url = url;
    this.protocols = typeof protocols === 'string' ? [protocols] : [...(protocols ?? [])];
    FakeWebSocket.instances.push(this);
    log.push('ws:new');
  }

  static get last(): FakeWebSocket {
    return FakeWebSocket.instances[FakeWebSocket.instances.length - 1]!;
  }

  send(data: string | ArrayBuffer): void {
    if (this.readyState !== 1) throw new Error('InvalidStateError: not open');
    this.sent.push(data);
    if (typeof data === 'string') {
      const type = (JSON.parse(data) as { type?: string }).type;
      log.push(`ws:${type}`);
    } else if (this.holdBuffer) {
      this.bufferedAmount += data.byteLength;
    }
  }

  close(code = 1000): void {
    if (this.closedWith === null) {
      this.closedWith = code;
      log.push(`ws:close:${code}`);
    }
    this.readyState = 3;
  }

  // -- the server's side ----------------------------------------------------

  accept(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  say(message: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  hangUp(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }

  /** accept + read the start message + answer ready at the resume point it asked for. */
  handshake(): Record<string, unknown> {
    this.accept();
    const start = this.texts[0]!;
    this.say({
      type: 'ready',
      v: 1,
      sample_rate: 16000,
      frame_ms: 40,
      max_frame_bytes: 16384,
      resume_from_sample: start.resume_from_sample,
      next_u: start.next_u,
    });
    return start;
  }

  get texts(): Array<Record<string, unknown>> {
    return this.sent.filter((s): s is string => typeof s === 'string').map((s) => JSON.parse(s));
  }

  get frames(): Int16Array[] {
    return this.sent.filter((s): s is ArrayBuffer => typeof s !== 'string').map((b) => new Int16Array(b));
  }

  /** Every PCM sample sent, in order. */
  get pcm(): number[] {
    return this.frames.flatMap((f) => [...f]);
  }
}

// ---------------------------------------------------------------------------
// The audio graph
// ---------------------------------------------------------------------------

class FakePort {
  posted: Array<Record<string, unknown>> = [];
  onmessage: ((event: { data: unknown }) => void) | null = null;
  closed = false;
  postMessage(message: Record<string, unknown>): void {
    this.posted.push(message);
    log.push(`worklet:${String(message.type)}`);
  }
  close(): void {
    this.closed = true;
  }
}

export class FakeWorkletNode {
  static instances: FakeWorkletNode[] = [];
  readonly port = new FakePort();
  readonly context: FakeLiveAudioContext;
  readonly name: string;
  readonly options: Record<string, unknown>;
  connections: unknown[] = [];
  disconnected = false;

  constructor(context: FakeLiveAudioContext, name: string, options: Record<string, unknown>) {
    this.context = context;
    this.name = name;
    this.options = options;
    FakeWorkletNode.instances.push(this);
  }

  static get last(): FakeWorkletNode {
    return FakeWorkletNode.instances[FakeWorkletNode.instances.length - 1]!;
  }

  connect(node: unknown): unknown {
    this.connections.push(node);
    return node;
  }

  disconnect(): void {
    this.disconnected = true;
  }

  // -- the worklet's side ---------------------------------------------------

  /** Sample 0's quantum: the context's rate. */
  started(rate = 48000): void {
    this.port.onmessage?.({ data: { type: 'start', contextTime: 0.1, sampleRate: rate } });
  }

  /** A 640-sample frame whose samples are their own index (mod 30000), as the worklet posts it. */
  frame(index: number, length = 640): void {
    const samples = Int16Array.from({ length }, (_, i) => (index + i) % 30000);
    this.port.onmessage?.({ data: { type: 'frame', index, samples: samples.buffer } });
  }

  frames(from: number, count: number): void {
    for (let k = 0; k < count; k += 1) this.frame(from + k * 640);
  }

  flushed(end: number): void {
    this.port.onmessage?.({ data: { type: 'flushed', end } });
  }
}

export class FakeNode {
  readonly kind: string;
  connections: unknown[] = [];
  disconnectedFrom: unknown[] = [];
  gain = { value: 1 };
  fftSize = 2048;
  smoothingTimeConstant = 0;
  constructor(kind: string) {
    this.kind = kind;
  }
  connect(node: unknown): unknown {
    this.connections.push(node);
    return node;
  }
  disconnect(node?: unknown): void {
    this.disconnectedFrom.push(node ?? 'all');
  }
  getByteTimeDomainData(out: Uint8Array): void {
    out.fill(128);
  }
}

export class FakeLiveAudioContext {
  static instances: FakeLiveAudioContext[] = [];
  static startSuspended = false;
  static withWorklet = true;
  static addModule: (url: string) => Promise<void> = async () => undefined;
  state: 'running' | 'suspended' | 'closed';
  sampleRate = 48000;
  readonly destination = new FakeNode('destination');
  sources: FakeNode[] = [];
  analysers: FakeNode[] = [];
  gains: FakeNode[] = [];
  sourceStream: MediaStream | null = null;
  resumed = 0;
  readonly audioWorklet?: { addModule: (url: string) => Promise<void>; urls: string[] };

  constructor() {
    this.state = FakeLiveAudioContext.startSuspended ? 'suspended' : 'running';
    FakeLiveAudioContext.instances.push(this);
    log.push('context:new');
    if (FakeLiveAudioContext.withWorklet) {
      const urls: string[] = [];
      this.audioWorklet = {
        urls,
        addModule: (url: string) => {
          urls.push(url);
          return FakeLiveAudioContext.addModule(url);
        },
      };
    }
  }

  static get last(): FakeLiveAudioContext {
    return FakeLiveAudioContext.instances[FakeLiveAudioContext.instances.length - 1]!;
  }

  createMediaStreamSource(stream: MediaStream): FakeNode {
    this.sourceStream = stream;
    const node = new FakeNode('source');
    this.sources.push(node);
    return node;
  }

  createAnalyser(): FakeNode {
    const node = new FakeNode('analyser');
    this.analysers.push(node);
    return node;
  }

  createGain(): FakeNode {
    const node = new FakeNode('gain');
    this.gains.push(node);
    return node;
  }

  resume(): Promise<void> {
    this.resumed += 1;
    this.state = 'running';
    return Promise.resolve();
  }

  close(): Promise<void> {
    this.state = 'closed';
    log.push('context:close');
    return Promise.resolve();
  }
}
