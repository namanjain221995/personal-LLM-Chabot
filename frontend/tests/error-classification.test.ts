/**
 * Status → category → public copy, and the log sanitizer.
 *
 * These exist because the old path decided what had happened by running a
 * regex over the error SENTENCE, so a real 404, a backend 500 and a model
 * timeout were all reported as "the orchestrator is unreachable". The
 * assertions below are mostly about that: the status is a fact, and the page
 * must never show a number the service did not send.
 */
import { describe, expect, it } from 'vitest';

import {
  categoryForStatus,
  copyForCategory,
  parseCategory,
  sanitizeForLog,
  toClientError,
  type ErrorCategory,
} from '../lib/errorTypes';

describe('categoryForStatus', () => {
  it.each([
    [404, 'NOT_FOUND'],
    [408, 'TIMEOUT'],
    [504, 'TIMEOUT'],
    [502, 'MODEL_UNAVAILABLE'],
    [503, 'ORCHESTRATOR_UNAVAILABLE'],
    [500, 'APPLICATION_ERROR'],
    [501, 'APPLICATION_ERROR'],
    [400, 'APPLICATION_ERROR'],
  ])('maps %i to %s', (status, expected) => {
    expect(categoryForStatus(status)).toBe(expected);
  });

  it('has no status at all for a transport failure', () => {
    expect(categoryForStatus(null)).toBe('NETWORK_ERROR');
  });
});

describe('toClientError', () => {
  it('shows the REAL status, never a stand-in', () => {
    expect(toClientError(500).display).toBe('500');
    expect(toClientError(404).display).toBe('404');
    expect(toClientError(504).display).toBe('504');
  });

  it('never labels a non-404 failure as 404', () => {
    for (const status of [500, 502, 503, 504, 408]) {
      expect(toClientError(status).display).not.toBe('404');
      expect(toClientError(status).code).not.toBe('NOT_FOUND');
    }
  });

  it('says "Error" rather than inventing a number when there is none', () => {
    const err = toClientError(null);
    expect(err.display).toBe('Error');
    expect(err.status).toBeNull();
    expect(err.title).toBe('Connection unavailable');
  });

  it('lets an explicit category override the status', () => {
    // The proxy answers 502 for "I could not complete this upstream call",
    // but the code says the socket never connected.
    const err = toClientError(502, 'NETWORK_ERROR');
    expect(err.code).toBe('NETWORK_ERROR');
    expect(err.title).toBe('Connection unavailable');
  });

  it('ignores an unrecognized code and falls back to the status', () => {
    expect(toClientError(503, 'NONSENSE').code).toBe('ORCHESTRATOR_UNAVAILABLE');
    expect(toClientError(503, undefined).code).toBe('ORCHESTRATOR_UNAVAILABLE');
  });

  it.each([
    [503, 'AI service unavailable'],
    [502, 'Model server unavailable'],
    [504, 'Request timed out'],
    [500, 'Something went wrong'],
    [404, "We couldn't find the page"],
  ])('gives %i the approved title', (status, title) => {
    expect(toClientError(status).title).toBe(title);
  });

  it('carries no field that could hold internals', () => {
    const err = toClientError(502, 'MODEL_UNAVAILABLE');
    expect(Object.keys(err).sort()).toEqual([
      'code',
      'display',
      'message',
      'retryable',
      'status',
      'title',
    ]);
  });

  it('offers retry for every category', () => {
    const categories: ErrorCategory[] = [
      'NOT_FOUND',
      'ORCHESTRATOR_UNAVAILABLE',
      'MODEL_UNAVAILABLE',
      'TIMEOUT',
      'APPLICATION_ERROR',
      'NETWORK_ERROR',
      'UNKNOWN_ERROR',
    ];
    for (const c of categories) {
      expect(copyForCategory(c).retryable).toBe(true);
      expect(copyForCategory(c).title.length).toBeGreaterThan(0);
    }
  });

  it('never puts a URL, a host or a code number into the copy', () => {
    for (const status of [null, 400, 404, 500, 502, 503, 504]) {
      const { title, message } = toClientError(status);
      expect(`${title} ${message}`).not.toMatch(
        /https?:\/\/|localhost|vllm|orchestrator|:\d{4}|traceback/i,
      );
    }
  });
});

describe('parseCategory', () => {
  it('accepts known names only', () => {
    expect(parseCategory('TIMEOUT')).toBe('TIMEOUT');
    expect(parseCategory('timeout')).toBeNull();
    expect(parseCategory(503)).toBeNull();
    expect(parseCategory(null)).toBeNull();
  });
});

describe('sanitizeForLog', () => {
  it.each([
    ['Authorization: Bearer abcdef1234567890', /abcdef1234567890/],
    ['authorization=Bearer xyz987654321', /xyz987654321/],
    ['cookie: ts_session=super-secret-value', /super-secret-value/],
    ['api_key=AKIA1234567890ABC', /AKIA1234567890ABC/],
    ['HF_TOKEN=hf_aaaaaaaaaaaaaaaaaaaa', /hf_aaaaaaaaaaaaaaaaaaaa/],
    ['password: hunter2hunter2', /hunter2hunter2/],
    ['client_secret=abc123def456', /abc123def456/],
    ['consumer_secret: sfdcTopSecret99', /sfdcTopSecret99/],
    ['postgres://app:p4ssw0rd@db:5432/techsara', /p4ssw0rd/],
    ['https://user:pw123456@example.com/x', /pw123456/],
    ['token sk-abcdefghijklmnop', /sk-abcdefghijklmnop/],
  ])('redacts %s', (raw, secret) => {
    const out = sanitizeForLog(raw);
    expect(out).not.toMatch(secret);
    expect(out).toMatch(/redacted/i);
  });

  it('redacts a JWT wholesale', () => {
    const jwt =
      'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U';
    expect(sanitizeForLog(`auth ${jwt}`)).not.toContain(jwt);
  });

  it('keeps the useful part of an upstream error', () => {
    expect(sanitizeForLog('connect ECONNREFUSED 127.0.0.1:8080')).toBe(
      'connect ECONNREFUSED 127.0.0.1:8080',
    );
  });

  it('flattens newlines so a traceback cannot break the log format', () => {
    const out = sanitizeForLog('Traceback:\n  File "x.py"\n    boom\n');
    expect(out).not.toContain('\n');
  });

  it('caps runaway payloads', () => {
    expect(sanitizeForLog('x'.repeat(5000)).length).toBeLessThanOrEqual(301);
  });

  it('is empty for non-strings', () => {
    expect(sanitizeForLog(undefined)).toBe('');
    expect(sanitizeForLog(null)).toBe('');
    expect(sanitizeForLog({ a: 1 })).toBe('');
  });
});
