/**
 * The dev-only error simulator.
 *
 * The important assertions are the production ones. A route that can
 * manufacture failures is harmless in dev and unacceptable in a deployment,
 * so the gate is tested from both sides — and tested to be a 404, not a 403,
 * because a production build must look as though the file does not exist.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  isSimulatableStatus,
  parseSimulateCommand,
  parseSimulation,
  simulationEnabled,
} from '../lib/devErrors';

let errors: string[] = [];

beforeEach(() => {
  errors = [];
  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    errors.push(args.map(String).join(' '));
  });
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function GET(url: string) {
  const mod = await import('../app/api/debug/error/route');
  return mod.GET(new Request(url));
}

const debug = (q: string) => `http://localhost:3001/api/debug/error?status=${q}`;

describe('production safety', () => {
  it('is closed in a production build', () => {
    vi.stubEnv('NODE_ENV', 'production');
    expect(simulationEnabled()).toBe(false);
  });

  it('is open in development and test', () => {
    vi.stubEnv('NODE_ENV', 'development');
    expect(simulationEnabled()).toBe(true);
    vi.stubEnv('NODE_ENV', 'test');
    expect(simulationEnabled()).toBe(true);
  });

  it('answers 404 in production — as if the route did not exist', async () => {
    vi.stubEnv('NODE_ENV', 'production');
    const res = await GET(debug('503'));
    expect(res.status).toBe(404);
    // Not 403: a 403 would advertise that something is there.
    expect(res.status).not.toBe(403);
    expect(await res.text()).toBe('');
  });

  it('logs nothing in production', async () => {
    vi.stubEnv('NODE_ENV', 'production');
    await GET(debug('500'));
    expect(errors).toHaveLength(0);
  });

  it('cannot be re-opened by any request input', async () => {
    vi.stubEnv('NODE_ENV', 'production');
    const mod = await import('../app/api/debug/error/route');
    for (const attempt of [
      new Request(debug('503'), { headers: { 'x-debug': 'true' } }),
      new Request(debug('503'), { headers: { cookie: 'debug=1' } }),
      new Request(`${debug('503')}&force=1&dev=true`),
    ]) {
      expect((await mod.GET(attempt)).status).toBe(404);
    }
  });

  it('refuses to simulate the composer command in production', () => {
    vi.stubEnv('NODE_ENV', 'production');
    // The gate lives in the caller; the parser stays pure. This asserts the
    // pairing that the chat route relies on.
    expect(simulationEnabled()).toBe(false);
    expect(parseSimulateCommand('/simulate 503')).toEqual({
      kind: 'status',
      status: 503,
    });
  });
});

describe('simulated statuses', () => {
  beforeEach(() => vi.stubEnv('NODE_ENV', 'development'));

  it.each([
    [404, 'NOT_FOUND'],
    [500, 'APPLICATION_ERROR'],
    [502, 'MODEL_UNAVAILABLE'],
    [503, 'ORCHESTRATOR_UNAVAILABLE'],
    [504, 'TIMEOUT'],
  ])('?status=%i returns %s', async (status, code) => {
    const res = await GET(debug(String(status)));
    expect(res.status).toBe(status);
    await expect(res.json()).resolves.toEqual({ code });
  });

  it('?status=network has no HTTP status of its own', async () => {
    const res = await GET(debug('network'));
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toEqual({ code: 'NETWORK_ERROR' });
  });

  it('returns the SAME body shape as the real chat proxy', async () => {
    const res = await GET(debug('503'));
    expect(Object.keys(await res.json())).toEqual(['code']);
  });

  it('leaks nothing, exactly like the real path', async () => {
    const text = await (await GET(debug('500'))).text();
    expect(text).not.toMatch(/traceback|orchestrator|vllm|localhost:8080/i);
  });

  it.each(['200', '302', '600', 'abc', '', 'DROP TABLE'])(
    'rejects %s with usage rather than simulating it',
    async (value) => {
      expect((await GET(debug(value))).status).toBe(400);
    },
  );
});

describe('SIMULATED_ERROR log marker', () => {
  beforeEach(() => vi.stubEnv('NODE_ENV', 'development'));

  it('marks the line so it can never be mistaken for a real outage', async () => {
    await GET(debug('503'));
    const line = errors.join('\n');
    expect(line).toContain('SIMULATED_ERROR');
    expect(line).toContain('simulated=true');
    expect(line).toContain('status=503');
    expect(line).toContain('category="ORCHESTRATOR_UNAVAILABLE"');
    expect(line).toContain('route="/api/debug/error"');
  });

  it('is greppable in both directions', async () => {
    await GET(debug('504'));
    const lines = errors.filter((l) => l.includes('chat-proxy:error'));
    expect(lines.every((l) => l.includes('SIMULATED_ERROR'))).toBe(true);
    expect(lines.filter((l) => !l.includes('SIMULATED_ERROR'))).toHaveLength(0);
  });
});

describe('parsers', () => {
  it('accepts 4xx/5xx only', () => {
    expect(isSimulatableStatus(404)).toBe(true);
    expect(isSimulatableStatus(599)).toBe(true);
    expect(isSimulatableStatus(200)).toBe(false);
    expect(isSimulatableStatus(600)).toBe(false);
    expect(isSimulatableStatus(4.5)).toBe(false);
  });

  it('parses the status parameter', () => {
    expect(parseSimulation('503')).toEqual({ kind: 'status', status: 503 });
    expect(parseSimulation(' NETWORK ')).toEqual({ kind: 'network' });
    expect(parseSimulation('nope')).toBeNull();
    expect(parseSimulation(null)).toBeNull();
  });

  it('parses the composer command and nothing that merely mentions it', () => {
    expect(parseSimulateCommand('/simulate 504')).toEqual({
      kind: 'status',
      status: 504,
    });
    expect(parseSimulateCommand('  /SIMULATE network ')).toEqual({
      kind: 'network',
    });
    // An ordinary message must never be hijacked.
    expect(parseSimulateCommand('how do I simulate 503 errors?')).toBeNull();
    expect(parseSimulateCommand('what does /simulate 503 do')).toBeNull();
    expect(parseSimulateCommand('/simulate')).toBeNull();
    expect(parseSimulateCommand('')).toBeNull();
  });
});
